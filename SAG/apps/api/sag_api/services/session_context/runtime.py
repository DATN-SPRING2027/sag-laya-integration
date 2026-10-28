"""Runtime integration bridging PostgreSQL `messages`, Continuum ACL, SiliconFlow routing,
and the SAG-ECCE Session Context Engine (`off` | `shadow` | `on`).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models.agent import Message
from sag_api.enums import MessageRole, MessageStatus
from sag_api.services.decision_router import route_query
from sag_api.services.session_context.models import AblationMode, ContextAuditTrace, SessionContextPack
from sag_api.services.session_context.optimizer import build_ecce_context_pack
from sag_api.services.session_context.substrate import SESSION_INDEX_CACHE

log = get_logger("sag.session_context")

HIDDEN_SCOPE_PLACEHOLDER = "[Answer hidden because its evidence is unavailable in the current source scope.]"
KEYSET_BATCH_SIZE = 500


class _ConfiguredHttpEmbedder:
    """Lightweight OpenAI/SiliconFlow-compatible HTTP embedder (0 local models)."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 5.0) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/embeddings"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self._model,
            "input": texts,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        data = sorted(body.get("data") or [], key=lambda item: int(item.get("index", 0)))
        return [[float(v) for v in (row.get("embedding") or [])] for row in data]


def resolve_session_embedder(explicit_embedder: Any | None = None) -> Any | None:
    """Resolve optional HTTP embedder when `session_context_embedding_mode == 'configured_http'`."""
    if explicit_embedder is not None:
        return explicit_embedder
    if settings.session_context_embedding_mode != "configured_http":
        return None
    base_url = settings.effective_embedding_base_url
    api_key = settings.effective_embedding_api_key
    model = (settings.embedding_model or "").strip()
    if not base_url or not api_key or not model:
        return None
    return _ConfiguredHttpEmbedder(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=min(8.0, float(settings.embedding_timeout)),
    )


def log_session_audit_trace(thread_id: str, trace: ContextAuditTrace) -> None:
    """Emit structured telemetry log for `shadow` and `on` observability."""
    log.info(
        "ECCE session context trace thread=%s mode=%s scope=%s temporal=%s turns=%d "
        "ev_tokens=%d recent_tokens=%d budget=%d selected=%d rejected=%d latency_ms=%.2f",
        thread_id,
        trace.mode,
        trace.memory_scope,
        trace.temporal_mode,
        trace.total_indexed_turns,
        trace.used_evidence_tokens,
        trace.used_recent_tokens,
        trace.budget_tokens,
        len(trace.selected_groups),
        len(trace.rejected_candidates),
        trace.latency_ms,
    )


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


async def count_thread_messages(
    session: AsyncSession,
    *,
    thread_id: str,
    exclude_id: str | None = None,
) -> int:
    """Fast index-only count of active user/assistant turns in a thread."""
    stmt = select(func.count(Message.id)).where(
        Message.thread_id == thread_id,
        Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
        Message.status == MessageStatus.OK,
    )
    if exclude_id:
        stmt = stmt.where(Message.id != exclude_id)
    res = await session.execute(stmt)
    return int(res.scalar_one() or 0)


async def load_full_thread_messages_keyset(
    session: AsyncSession,
    *,
    thread_id: str,
    exclude_id: str | None = None,
    max_messages: int | None = None,
    after_created_at: Any | None = None,
    after_id: str | None = None,
) -> list[Message]:
    """Load chronological thread history (full or O(Delta) cursor tail) via ix_messages_thread_created_id."""
    hard_cap = max_messages if max_messages is not None else settings.session_context_max_messages
    if hard_cap <= 0:
        return []
    collected: list[Message] = []
    last_created_at = after_created_at
    last_id = after_id

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


def _extract_visible_records(scoped_messages: list[Message]) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    had_hidden = False
    for msg in scoped_messages:
        msg_id = str(getattr(msg, "id", "") or "")
        content = str(getattr(msg, "content", "") or "")
        if content == HIDDEN_SCOPE_PLACEHOLDER:
            had_hidden = True
            continue
        if not msg_id or not content:
            continue
        role_val = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        cited_ids: set[str] = {
            str(c["source_id"])
            for c in (getattr(msg, "citations", None) or [])
            if isinstance(c, dict) and c.get("source_id")
        }
        for step in (getattr(msg, "steps", None) or []):
            if not isinstance(step, dict):
                continue
            details = step.get("details")
            if isinstance(details, dict) and details.get("scope") == "knowledge":
                for src in (details.get("sources") or []):
                    if isinstance(src, dict) and src.get("id"):
                        cited_ids.add(str(src["id"]))
        records.append(
            {
                "id": msg_id,
                "role": role_val,
                "content": content,
                "created_at": getattr(msg, "created_at", None),
                "cited_source_ids": sorted(cited_ids),
            }
        )
    return records, had_hidden


async def _cached_sources_still_authorized(
    session: AsyncSession,
    *,
    cached_source_ids: set[str],
    principal: VerifiedPrincipal,
    requested_source_ids: list[str] | None,
) -> bool:
    if not cached_source_ids:
        return True
    from sag_api.services.source_service import get_authorized_source_ids

    authorized = await get_authorized_source_ids(
        session,
        principal=principal,
        requested_source_ids=sorted(cached_source_ids),
    )
    if requested_source_ids is not None:
        authorized.intersection_update(requested_source_ids)
    return cached_source_ids.issubset(authorized)


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
    """Build ACL-enforced, SiliconFlow-routed ECCE context pack with O(Delta) DB cursor reads."""
    from sag_api.services.agent_domain import filter_messages_for_scope

    active_embedder = resolve_session_embedder(embedder)
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

    can_use_delta = (
        bool(index.turns)
        and index.last_db_created_at is not None
        and index.last_db_id is not None
        and not index.had_hidden_acl_turns
    )
    if can_use_delta:
        db_count = await count_thread_messages(
            session,
            thread_id=thread_id,
            exclude_id=exclude_message_id,
        )
        sources_ok = await _cached_sources_still_authorized(
            session,
            cached_source_ids=index.cached_cited_source_ids,
            principal=principal,
            requested_source_ids=requested_source_ids,
        )
        if db_count < index.seen_db_count or not sources_ok:
            SESSION_INDEX_CACHE.invalidate_thread(thread_id)
            index = SESSION_INDEX_CACHE.get_or_create(
                scope_key=scope_key,
                thread_id=thread_id,
                scope_signature=scope_sig,
            )
            can_use_delta = False

    if can_use_delta:
        remaining_cap = max(0, settings.session_context_max_messages - index.seen_db_count)
        delta_raw = await load_full_thread_messages_keyset(
            session,
            thread_id=thread_id,
            exclude_id=exclude_message_id,
            max_messages=remaining_cap,
            after_created_at=index.last_db_created_at,
            after_id=index.last_db_id,
        )
        if delta_raw:
            delta_scoped = await filter_messages_for_scope(
                session,
                delta_raw,
                principal=principal,
                requested_source_ids=requested_source_ids,
            )
            delta_records, delta_hidden = _extract_visible_records(delta_scoped)
            if delta_hidden:
                index.had_hidden_acl_turns = True
            await index.append_messages(delta_records, embedder=active_embedder)
            index.last_db_created_at = delta_raw[-1].created_at
            index.last_db_id = str(delta_raw[-1].id)
            index.seen_db_count += len(delta_raw)
        allowed_turn_ids = set(index.turns_by_id.keys())
    else:
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
        visible_records, had_hidden = _extract_visible_records(scoped_messages)
        allowed_turn_ids = {r["id"] for r in visible_records}

        stale_cached_ids = set(index.turns_by_id.keys()) - allowed_turn_ids
        if stale_cached_ids:
            SESSION_INDEX_CACHE.invalidate_thread(thread_id)
            index = SESSION_INDEX_CACHE.get_or_create(
                scope_key=scope_key,
                thread_id=thread_id,
                scope_signature=scope_sig,
            )

        await index.append_messages(visible_records, embedder=active_embedder)
        index.had_hidden_acl_turns = had_hidden
        index.seen_db_count = len(raw_messages)
        if raw_messages:
            index.last_db_created_at = raw_messages[-1].created_at
            index.last_db_id = str(raw_messages[-1].id)

    # SiliconFlow System One routing (run off event loop; deterministic fallback if disabled/unconfigured)
    route_ctx = {
        "has_thread_history": bool(allowed_turn_ids),
        "turn_count": len(allowed_turn_ids),
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
        embedder=active_embedder,
        keep_recent_default=settings.history_keep_recent,
        legacy_load_limit=settings.history_load_limit,
    )
    return pack
