"""Runtime integration bridging PostgreSQL `messages`, Continuum ACL, SiliconFlow routing,
and the SAG-ECCE Session Context Engine (`off` | `shadow` | `on`).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models.agent import Message
from sag_api.enums import MessageRole, MessageStatus
from sag_api.services.decision_router import route_query
from sag_api.services.session_context.models import AblationMode, SessionContextPack
from sag_api.services.session_context.optimizer import build_ecce_context_pack
from sag_api.services.session_context.substrate import SESSION_INDEX_CACHE

log = get_logger("sag.session_context")

HIDDEN_SCOPE_PLACEHOLDER = "[Answer hidden because its evidence is unavailable in the current source scope.]"
KEYSET_BATCH_SIZE = 500


def invalidate_session_thread_cache(thread_id: str) -> int:
    """Immediately evict cached session substrate for a thread on message/thread deletion."""
    return SESSION_INDEX_CACHE.invalidate_thread(thread_id)


def _compute_scope_signature(
    *,
    owner_key: str,
    principal: VerifiedPrincipal,
    requested_source_ids: list[str] | None,
) -> str:
    tenant_id = str(getattr(principal, "tenant_id", "") or "")
    org_id = str(getattr(principal, "organization_id", "") or "")
    projects = ",".join(sorted(getattr(principal, "allowed_project_ids", ()) or ()))
    partitions = ",".join(sorted(getattr(principal, "allowed_partition_ids", ()) or ()))
    req_part = ",".join(sorted(requested_source_ids)) if requested_source_ids is not None else "*ALL*"
    raw = f"{tenant_id}|{org_id}|{projects}|{partitions}|{owner_key}|{req_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def load_full_thread_messages_keyset(
    session: AsyncSession,
    *,
    thread_id: str,
    exclude_id: str | None = None,
    max_messages: int | None = None,
) -> list[Message]:
    """Load complete chronological thread history via keyset pagination on ix_messages_thread_created_id."""
    hard_cap = max_messages if max_messages is not None else settings.session_context_max_messages
    collected: list[Message] = []
    last_created_at = None
    last_id = None

    while len(collected) < hard_cap:
        batch_limit = min(KEYSET_BATCH_SIZE, hard_cap - len(collected))
        stmt = select(Message).where(
            Message.thread_id == thread_id,
            Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
            Message.status == MessageStatus.OK,
        )
        if exclude_id:
            stmt = stmt.where(Message.id != exclude_id)
        if last_created_at is not None and last_id is not None:
            stmt = stmt.where(
                or_(
                    Message.created_at > last_created_at,
                    and_(Message.created_at == last_created_at, Message.id > last_id),
                )
            )
        rows = await session.execute(stmt.order_by(Message.created_at.asc(), Message.id.asc()).limit(batch_limit))
        batch = list(rows.scalars().all())
        if not batch:
            break
        collected.extend(batch)
        last_created_at = batch[-1].created_at
        last_id = batch[-1].id
        if len(batch) < batch_limit:
            break

    return collected


async def prepare_session_context_for_ask(
    session: AsyncSession,
    *,
    thread_id: str,
    exclude_message_id: str,
    query: str,
    principal: VerifiedPrincipal,
    owner_key: str,
    requested_source_ids: list[str] | None,
    budget_tokens: int,
    ablation: AblationMode = "V5_ecce_full",
    embedder: Any | None = None,
) -> SessionContextPack:
    """Build ACL-enforced, SiliconFlow-routed ECCE context pack for `agent_domain.prepare_ask`."""
    from sag_api.services.agent_domain import filter_messages_for_scope

    raw_messages = await load_full_thread_messages_keyset(
        session,
        thread_id=thread_id,
        exclude_id=exclude_message_id,
    )
    scoped_messages = await filter_messages_for_scope(
        session,
        raw_messages,
        principal=principal,
        requested_source_ids=requested_source_ids,
    )

    visible_records: list[dict[str, Any]] = []
    allowed_turn_ids: set[str] = set()
    for msg in scoped_messages:
        msg_id = str(getattr(msg, "id", "") or "")
        content = str(getattr(msg, "content", "") or "")
        if not msg_id or not content or content == HIDDEN_SCOPE_PLACEHOLDER:
            continue
        role_val = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        cited_ids = [
            str(c["source_id"])
            for c in (getattr(msg, "citations", None) or [])
            if isinstance(c, dict) and c.get("source_id")
        ]
        visible_records.append(
            {
                "id": msg_id,
                "role": role_val,
                "content": content,
                "created_at": getattr(msg, "created_at", None),
                "cited_source_ids": cited_ids,
            }
        )
        allowed_turn_ids.add(msg_id)

    projects_sig = ",".join(sorted(getattr(principal, "allowed_project_ids", ()) or ())) or "default"
    scope_key = f"{projects_sig}:{owner_key}"
    scope_sig = _compute_scope_signature(
        owner_key=owner_key,
        principal=principal,
        requested_source_ids=requested_source_ids,
    )
    index = SESSION_INDEX_CACHE.get_or_create(
        scope_key=scope_key,
        thread_id=thread_id,
        scope_signature=scope_sig,
    )

    # Transitive ACL & deletion guard: if any previously cached turn disappeared from allowed_turn_ids,
    # purge and rebuild cleanly from visible_records so 0 revoked/deleted signal remains in graph/episodes.
    stale_cached_ids = set(index.turns_by_id.keys()) - allowed_turn_ids
    if stale_cached_ids:
        SESSION_INDEX_CACHE.invalidate_thread(thread_id)
        index = SESSION_INDEX_CACHE.get_or_create(
            scope_key=scope_key,
            thread_id=thread_id,
            scope_signature=scope_sig,
        )

    await index.append_messages(visible_records, embedder=embedder)

    # SiliconFlow System One routing (run off event loop; deterministic fallback if disabled/unconfigured)
    route_ctx = {
        "has_thread_history": bool(visible_records),
        "turn_count": len(visible_records),
        "active_decisions_count": len(index.active_decisions),
    }
    decision = await asyncio.to_thread(route_query, query, route_ctx)

    pack = await build_ecce_context_pack(
        index,
        query=query,
        budget_tokens=budget_tokens,
        ablation=ablation,
        route_decision=decision,
        allowed_turn_ids=allowed_turn_ids,
        embedder=embedder,
        keep_recent_default=settings.history_keep_recent,
        legacy_load_limit=settings.history_load_limit,
    )
    return pack
