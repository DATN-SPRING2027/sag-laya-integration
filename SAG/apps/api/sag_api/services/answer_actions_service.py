"""Domain services for AI answer feedback, project-scoped sharing, regeneration, and admin queries."""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models import Agent, AnswerFeedback, AnswerShare, Message, Thread
from sag_api.enums import MessageRole, MessageStatus
from sag_api.schemas.answer_actions import (
    AdminFeedbackPageOut,
    AdminFeedbackRowOut,
    AdminFeedbackTotalsOut,
    FeedbackOut,
    FeedbackUpsertIn,
    OwnFeedbackOut,
    SharedAnswerOut,
    SharedPublicAnswerOut,
    SharedQuestionOut,
    ShareOut,
)
from sag_api.services.agent_domain import (
    AskPlan,
    _message_source_ids,
    _reject_json_constant,
    _urlsafe_decode,
    _urlsafe_encode,
    build_ask_context,
    compress_history,
    filter_messages_for_scope,
    get_agent,
    get_thread,
)
from sag_api.services.session_context import (
    HIDDEN_SCOPE_PLACEHOLDER,
    IncrementalSessionIndex,
    build_ecce_context_pack,
)
from sag_api.services.source_service import get_authorized_source_ids

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_ADMIN_CURSOR_MAX_LENGTH = 1024
_ACTIVE_THREAD_GENERATIONS: set[str] = set()

_PUBLIC_CITATION_KEYS = (
    "n",
    "number",
    "kind",
    "source_id",
    "source_name",
    "document_id",
    "document_version_id",
    "document_name",
    "version_no",
    "chunk_id",
    "heading",
    "snippet",
    "summary",
    "label",
    "page_from",
    "page_to",
    "anchor",
    "url",
    "title",
    "source",
    "mapped",
    "claim_level",
)


def _has_active_runtime_thread_run(runtime: AgentRuntime | None, thread_id: str) -> bool:
    if runtime is None:
        return False
    active_map = getattr(runtime, "_active", None)
    if not isinstance(active_map, dict):
        return False
    for handle in active_map.values():
        if handle is not None and not handle.done:
            meta = getattr(handle.context, "metadata", None) or {}
            if meta.get("thread_id") == thread_id:
                return True
    return False


def claim_thread_generation(thread_id: str, runtime: AgentRuntime | None) -> None:
    if thread_id in _ACTIVE_THREAD_GENERATIONS or _has_active_runtime_thread_run(runtime, thread_id):
        raise ConflictError("Another generation is already active in this conversation")
    _ACTIVE_THREAD_GENERATIONS.add(thread_id)


def release_thread_generation(thread_id: str) -> None:
    _ACTIVE_THREAD_GENERATIONS.discard(thread_id)


def _resolve_single_project_scope(principal: VerifiedPrincipal) -> tuple[str, str]:
    tenant_id = (principal.tenant_id or principal.organization_id or "").strip()
    if not tenant_id or len(principal.allowed_project_ids) != 1:
        raise ForbiddenError("Principal does not carry an exact single-Project scope")
    project_id = next(iter(principal.allowed_project_ids))
    if not project_id.strip():
        raise ForbiddenError("Principal Project scope is empty")
    return tenant_id, project_id


def _sanitize_public_citations(raw_citations: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_citations, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for item in raw_citations:
        if not isinstance(item, dict):
            continue
        cleaned = {k: item[k] for k in _PUBLIC_CITATION_KEYS if k in item and item[k] is not None}
        if cleaned:
            sanitized.append(cleaned)
    return sanitized


async def _ensure_authorized_assistant_message(
    session: AsyncSession,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    owner_key: str,
    principal: VerifiedPrincipal,
    require_non_empty: bool = False,
) -> tuple[Thread, Message, set[str]]:
    agent = await get_agent(session, agent_id, owner_key=owner_key)
    thread = await get_thread(session, agent.id, thread_id, owner_key=owner_key)
    message = await session.get(Message, message_id)
    if message is None or message.thread_id != thread.id:
        raise NotFoundError("Message not found")
    if message.role != MessageRole.ASSISTANT:
        raise BadRequestError("Target message must be an assistant answer")
    if message.status != MessageStatus.OK:
        raise BadRequestError("Target assistant message is not in a completed state")
    if require_non_empty and not (message.content or "").strip():
        raise BadRequestError("Assistant answer has no shareable content")
    if (message.content or "").strip() == HIDDEN_SCOPE_PLACEHOLDER:
        raise NotFoundError("Message not found")

    source_ids = _message_source_ids(message)
    if source_ids is None:
        raise NotFoundError("Message not found")
    if source_ids:
        authorized = await get_authorized_source_ids(
            session,
            principal=principal,
            requested_source_ids=sorted(source_ids),
        )
        if not source_ids.issubset(authorized):
            raise NotFoundError("Message not found")
    return thread, message, source_ids


async def resolve_parent_question(
    session: AsyncSession,
    *,
    thread_id: str,
    answer_message: Message,
) -> Message:
    """Resolve the exact original USER question for an assistant message without guessing."""
    cursor_msg = answer_message
    visited: set[str] = set()
    for _ in range(32):
        if cursor_msg.id in visited:
            raise BadRequestError("Detected cyclic message regeneration relation")
        visited.add(cursor_msg.id)
        if cursor_msg.reply_to_message_id:
            question = await session.get(Message, cursor_msg.reply_to_message_id)
            if question is None or question.thread_id != thread_id or question.role != MessageRole.USER:
                raise BadRequestError("Original user question is unavailable")
            return question
        if not cursor_msg.regenerated_from_message_id:
            break
        prior = await session.get(Message, cursor_msg.regenerated_from_message_id)
        if prior is None or prior.thread_id != thread_id or prior.role != MessageRole.ASSISTANT:
            raise BadRequestError("Original parent answer lineage is broken")
        cursor_msg = prior

    # Legacy fallback: only succeed when the immediately preceding message in ordered thread history
    # is unambiguously a single USER turn.
    rows = await session.execute(
        select(Message)
        .where(
            Message.thread_id == thread_id,
            or_(
                Message.created_at < cursor_msg.created_at,
                and_(Message.created_at == cursor_msg.created_at, Message.id < cursor_msg.id),
            ),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(2)
    )
    preceding = list(rows.scalars().all())
    if not preceding or preceding[0].role != MessageRole.USER:
        raise BadRequestError("Cannot resolve an unambiguous parent question for this legacy answer")
    if (
        len(preceding) > 1
        and preceding[1].role == MessageRole.USER
        and preceding[1].created_at == preceding[0].created_at
    ):
        raise BadRequestError("Ambiguous parent question timestamps for this legacy answer")
    return preceding[0]


# ── Feedback ──────────────────────────────────────────────────────────
async def get_own_feedback(
    session: AsyncSession,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    owner_key: str,
    principal: VerifiedPrincipal,
) -> OwnFeedbackOut:
    _resolve_single_project_scope(principal)
    _, message, _ = await _ensure_authorized_assistant_message(
        session,
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        owner_key=owner_key,
        principal=principal,
    )
    row = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.message_id == message.id,
            AnswerFeedback.voter_subject == principal.subject,
        )
    )
    return OwnFeedbackOut(feedback=FeedbackOut.model_validate(row) if row is not None else None)


async def upsert_own_feedback(
    session: AsyncSession,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    owner_key: str,
    principal: VerifiedPrincipal,
    payload: FeedbackUpsertIn,
) -> FeedbackOut:
    tenant_id, project_id = _resolve_single_project_scope(principal)
    _, message, _ = await _ensure_authorized_assistant_message(
        session,
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        owner_key=owner_key,
        principal=principal,
    )
    reason_category = payload.reason_category if payload.rating == "DISLIKE" else None
    reason_text = (
        payload.reason_text.strip()
        if payload.rating == "DISLIKE" and payload.reason_text is not None
        else None
    )
    now = datetime.now(UTC)

    existing = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.message_id == message.id,
            AnswerFeedback.voter_subject == principal.subject,
        )
    )
    if existing is not None:
        existing.rating = payload.rating
        existing.reason_category = reason_category
        existing.reason_text = reason_text
        existing.tenant_id = tenant_id
        existing.project_id = project_id
        existing.updated_at = now
        await session.commit()
        await session.refresh(existing)
        return FeedbackOut.model_validate(existing)

    created = AnswerFeedback(
        message_id=message.id,
        voter_subject=principal.subject,
        tenant_id=tenant_id,
        project_id=project_id,
        rating=payload.rating,
        reason_category=reason_category,
        reason_text=reason_text,
        created_at=now,
        updated_at=now,
    )
    session.add(created)
    try:
        await session.commit()
        await session.refresh(created)
        return FeedbackOut.model_validate(created)
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(
            select(AnswerFeedback).where(
                AnswerFeedback.message_id == message.id,
                AnswerFeedback.voter_subject == principal.subject,
            )
        )
        if winner is None:
            raise
        winner.rating = payload.rating
        winner.reason_category = reason_category
        winner.reason_text = reason_text
        winner.tenant_id = tenant_id
        winner.project_id = project_id
        winner.updated_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(winner)
        return FeedbackOut.model_validate(winner)


async def delete_own_feedback(
    session: AsyncSession,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    owner_key: str,
    principal: VerifiedPrincipal,
) -> None:
    _resolve_single_project_scope(principal)
    _, message, _ = await _ensure_authorized_assistant_message(
        session,
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        owner_key=owner_key,
        principal=principal,
    )
    existing = await session.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.message_id == message.id,
            AnswerFeedback.voter_subject == principal.subject,
        )
    )
    if existing is not None:
        await session.delete(existing)
        await session.commit()


# ── Shares ────────────────────────────────────────────────────────────
async def create_or_get_share(
    session: AsyncSession,
    *,
    agent_id: str,
    thread_id: str,
    message_id: str,
    owner_key: str,
    principal: VerifiedPrincipal,
) -> tuple[ShareOut, bool]:
    tenant_id, project_id = _resolve_single_project_scope(principal)
    thread, message, source_ids = await _ensure_authorized_assistant_message(
        session,
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        owner_key=owner_key,
        principal=principal,
        require_non_empty=True,
    )
    question = await resolve_parent_question(session, thread_id=thread.id, answer_message=message)

    existing = await session.scalar(
        select(AnswerShare).where(
            AnswerShare.creator_subject == principal.subject,
            AnswerShare.message_id == message.id,
            AnswerShare.revoked_at.is_(None),
        )
    )
    if existing is not None:
        if existing.tenant_id != tenant_id or existing.project_id != project_id:
            raise NotFoundError("Message not found")
        return ShareOut.model_validate(existing), False

    share = AnswerShare(
        creator_subject=principal.subject,
        message_id=message.id,
        question_id=question.id,
        tenant_id=tenant_id,
        project_id=project_id,
        question_content=question.content or "",
        answer_content=message.content or "",
        citations=_sanitize_public_citations(message.citations),
        source_ids=sorted(source_ids),
    )
    session.add(share)
    try:
        await session.commit()
        await session.refresh(share)
        return ShareOut.model_validate(share), True
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(
            select(AnswerShare).where(
                AnswerShare.creator_subject == principal.subject,
                AnswerShare.message_id == message.id,
                AnswerShare.revoked_at.is_(None),
            )
        )
        if winner is None:
            raise
        return ShareOut.model_validate(winner), False


async def read_project_share(
    session: AsyncSession,
    *,
    project_id: str,
    share_id: str,
    principal: VerifiedPrincipal,
) -> SharedAnswerOut:
    if not _SAFE_ID_RE.fullmatch(project_id) or not _SAFE_ID_RE.fullmatch(share_id):
        raise NotFoundError("Shared answer not found")
    tenant_id = (principal.tenant_id or principal.organization_id or "").strip()
    if not tenant_id or project_id not in principal.allowed_project_ids:
        raise NotFoundError("Shared answer not found")

    share = await session.get(AnswerShare, share_id)
    if (
        share is None
        or share.revoked_at is not None
        or share.project_id != project_id
        or share.tenant_id != tenant_id
    ):
        raise NotFoundError("Shared answer not found")

    answer_msg = await session.get(Message, share.message_id)
    question_msg = await session.get(Message, share.question_id)
    if (
        answer_msg is None
        or question_msg is None
        or answer_msg.role != MessageRole.ASSISTANT
        or answer_msg.status != MessageStatus.OK
        or question_msg.role != MessageRole.USER
        or answer_msg.thread_id != question_msg.thread_id
    ):
        raise NotFoundError("Shared answer not found")

    live_source_ids = _message_source_ids(answer_msg)
    if live_source_ids is None:
        raise NotFoundError("Shared answer not found")
    stored_source_ids = {
        str(sid) for sid in (share.source_ids or []) if isinstance(sid, str) and sid.strip()
    }
    required_sources = live_source_ids | stored_source_ids
    if required_sources:
        authorized = await get_authorized_source_ids(
            session,
            principal=principal,
            requested_source_ids=sorted(required_sources),
        )
        if not required_sources.issubset(authorized):
            raise NotFoundError("Shared answer not found")

    return SharedAnswerOut(
        share=ShareOut.model_validate(share),
        question=SharedQuestionOut(content=share.question_content),
        answer=SharedPublicAnswerOut(
            id=share.message_id,
            content=share.answer_content,
            citations=_sanitize_public_citations(share.citations),
        ),
    )


async def revoke_project_share(
    session: AsyncSession,
    *,
    project_id: str,
    share_id: str,
    principal: VerifiedPrincipal,
) -> None:
    if not _SAFE_ID_RE.fullmatch(project_id) or not _SAFE_ID_RE.fullmatch(share_id):
        raise NotFoundError("Shared answer not found")
    tenant_id = (principal.tenant_id or principal.organization_id or "").strip()
    if not tenant_id or project_id not in principal.allowed_project_ids:
        raise NotFoundError("Shared answer not found")

    share = await session.get(AnswerShare, share_id)
    if (
        share is None
        or share.project_id != project_id
        or share.tenant_id != tenant_id
        or share.creator_subject != principal.subject
    ):
        raise NotFoundError("Shared answer not found")

    if share.revoked_at is None:
        now = datetime.now(UTC)
        share.revoked_at = now
        share.updated_at = now
        await session.commit()


# ── Regenerate ("Try again") ──────────────────────────────────────────
def _deduplicate_prior_regenerations(messages: list[Message]) -> list[Message]:
    """Keep only the latest chronological answer per parent question in prior turns."""
    latest_by_question: dict[str, str] = {}
    for msg in messages:
        if msg.role == MessageRole.ASSISTANT and msg.reply_to_message_id:
            latest_by_question[msg.reply_to_message_id] = msg.id
    deduped: list[Message] = []
    for msg in messages:
        if (
            msg.role == MessageRole.ASSISTANT
            and msg.reply_to_message_id
            and latest_by_question.get(msg.reply_to_message_id) != msg.id
        ):
            continue
        deduped.append(msg)
    return deduped


async def prepare_regenerate(
    session: AsyncSession,
    *,
    agent: Agent,
    thread: Thread,
    target_message_id: str,
    principal: VerifiedPrincipal,
    owner_key: str,
    llm: Any = None,
) -> AskPlan:
    from sag_api.api.v1.attachments import attachment_path
    from sag_api.services.decision_router import route_query
    from sag_api.services.session_context.runtime import _extract_visible_records

    await get_thread(session, agent.id, thread.id, owner_key=owner_key)
    target = await session.get(Message, target_message_id)
    if target is None or target.thread_id != thread.id:
        raise NotFoundError("Message not found")
    if target.role != MessageRole.ASSISTANT:
        raise BadRequestError("Only assistant answers can be regenerated")

    question = await resolve_parent_question(session, thread_id=thread.id, answer_message=target)
    query_text = question.content or ""
    raw_attachments = question.attachments if isinstance(question.attachments, list) else []
    if not query_text.strip() and not raw_attachments:
        raise BadRequestError("Original question has no content to regenerate")

    resolved_attachments: list[dict[str, Any]] = []
    for item in raw_attachments:
        aid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(aid, str) or not aid:
            continue
        path = attachment_path(aid, owner_key=owner_key)
        if path is not None:
            resolved_attachments.append(
                {
                    "id": aid,
                    "media_type": item.get("media_type") or "image/png",
                    "path": path,
                }
            )

    rows = await session.execute(
        select(Message)
        .where(
            Message.thread_id == thread.id,
            Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
            Message.status == MessageStatus.OK,
            or_(
                Message.created_at < question.created_at,
                and_(Message.created_at == question.created_at, Message.id < question.id),
            ),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(settings.history_load_limit)
    )
    prior_chronological = _deduplicate_prior_regenerations(list(reversed(rows.scalars().all())))
    scoped_prior = await filter_messages_for_scope(
        session,
        prior_chronological,
        principal=principal,
        requested_source_ids=None,
    )

    budget_tokens = int(settings.llm_context_window * 0.4)
    audit_trace: dict[str, Any] | None = None
    route_decision: dict[str, Any] | None = None
    session_citations: list[dict[str, Any]] = []

    if settings.session_context_mode == "on" and scoped_prior:
        prior_records, _ = _extract_visible_records(scoped_prior)
        ephemeral_index = IncrementalSessionIndex(thread.id)
        await ephemeral_index.append_messages(prior_records)
        allowed_turns = set(ephemeral_index.turns_by_id.keys())
        decision = await asyncio.to_thread(
            route_query,
            query_text,
            {
                "has_thread_history": bool(allowed_turns),
                "turn_count": len(allowed_turns),
                "active_decisions_count": len(ephemeral_index.active_decisions),
            },
        )
        pack = await build_ecce_context_pack(
            ephemeral_index,
            query=query_text,
            budget_tokens=budget_tokens,
            route_decision=decision,
            allowed_turn_ids=allowed_turns,
            keep_recent_default=settings.history_keep_recent,
            legacy_load_limit=settings.history_load_limit,
        )
        history = pack.history_messages
        audit_trace = pack.audit_trace.to_dict()
        route_decision = dict(pack.audit_trace.route_decision)
        session_citations = list(pack.session_citations)
    else:
        raw_history = [{"role": m.role.value, "content": m.content} for m in scoped_prior]
        history = await compress_history(raw_history, llm=llm, budget_tokens=budget_tokens)

    plan = build_ask_context(
        agent=agent,
        query=query_text,
        history=history,
        attachments=resolved_attachments or None,
        source_ids=None,
    )
    plan.user_message_id = question.id
    plan.regenerated_from_message_id = target.id
    plan.session_audit_trace = audit_trace
    plan.route_decision = route_decision
    plan.session_citations = session_citations
    return plan


# ── Admin Feedback Telemetry ──────────────────────────────────────────
def _admin_filter_scope_hash(
    *,
    organization_id: str | None,
    project_id: str | None,
    rating: str | None,
    from_iso: str | None,
    to_iso: str | None,
) -> str:
    canonical = json.dumps(
        {
            "org": organization_id or "",
            "proj": project_id or "",
            "rating": rating or "",
            "from": from_iso or "",
            "to": to_iso or "",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _encode_admin_feedback_cursor(scope_hash: str, row: AnswerFeedback) -> str:
    payload = {
        "v": 1,
        "kind": "admin_feedback",
        "scope": scope_hash,
        "created_at": _as_utc(row.created_at).isoformat(timespec="microseconds"),
        "id": row.id,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sig = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_urlsafe_encode(raw)}.{_urlsafe_encode(sig)}"


def _decode_admin_feedback_cursor(scope_hash: str, value: str) -> tuple[datetime, str]:
    def _invalid() -> BadRequestError:
        return BadRequestError("Invalid feedback cursor", code=ErrorCode.INVALID_CURSOR)

    if not value or len(value) > _ADMIN_CURSOR_MAX_LENGTH or value.count(".") != 1:
        raise _invalid()
    try:
        enc_payload, enc_sig = value.split(".", 1)
        raw = _urlsafe_decode(enc_payload)
        sig = _urlsafe_decode(enc_sig)
        if len(raw) > 512 or len(sig) != hashlib.sha256().digest_size:
            raise ValueError("invalid cursor length")
        expected = hmac.new(settings.secret_key.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("invalid cursor signature")
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "kind", "scope", "created_at", "id"}
            or payload["v"] != 1
            or isinstance(payload["v"], bool)
            or payload["kind"] != "admin_feedback"
            or payload["scope"] != scope_hash
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["id"], str)
            or not _SAFE_ID_RE.fullmatch(payload["id"])
        ):
            raise ValueError("invalid cursor claims")
        dt = datetime.fromisoformat(payload["created_at"])
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("cursor timestamp missing timezone")
        return dt.astimezone(UTC), payload["id"]
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise _invalid() from exc


def _parse_iso_utc(raw: str | None, field_name: str) -> tuple[datetime | None, str | None]:
    if raw is None:
        return None, None
    value = raw.strip()
    if not value:
        raise BadRequestError(f"Query parameter '{field_name}' cannot be blank")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BadRequestError(f"Query parameter '{field_name}' must be an ISO-8601 timestamp") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise BadRequestError(f"Query parameter '{field_name}' must include a UTC offset")
    utc_dt = dt.astimezone(UTC)
    return utc_dt, utc_dt.isoformat(timespec="microseconds")


async def list_admin_feedback(
    session: AsyncSession,
    *,
    organization_id: str | None = None,
    project_id: str | None = None,
    rating: str | None = None,
    from_raw: str | None = None,
    to_raw: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> AdminFeedbackPageOut:
    if organization_id is not None and not _SAFE_ID_RE.fullmatch(organization_id):
        raise BadRequestError("Invalid organizationId filter")
    if project_id is not None and not _SAFE_ID_RE.fullmatch(project_id):
        raise BadRequestError("Invalid projectId filter")
    if rating is not None and rating not in ("LIKE", "DISLIKE"):
        raise BadRequestError("Invalid rating filter")
    if limit < 1 or limit > 100:
        raise BadRequestError("Limit must be between 1 and 100")

    from_dt, from_iso = _parse_iso_utc(from_raw, "from")
    to_dt, to_iso = _parse_iso_utc(to_raw, "to")
    if from_dt is not None and to_dt is not None and from_dt >= to_dt:
        raise BadRequestError("'from' must be strictly earlier than 'to'")

    scope_hash = _admin_filter_scope_hash(
        organization_id=organization_id,
        project_id=project_id,
        rating=rating,
        from_iso=from_iso,
        to_iso=to_iso,
    )

    filters = []
    if organization_id is not None:
        filters.append(AnswerFeedback.tenant_id == organization_id)
    if project_id is not None:
        filters.append(AnswerFeedback.project_id == project_id)
    if rating is not None:
        filters.append(AnswerFeedback.rating == rating)
    if from_dt is not None:
        filters.append(AnswerFeedback.created_at >= from_dt)
    if to_dt is not None:
        filters.append(AnswerFeedback.created_at < to_dt)

    totals_stmt = select(
        func.coalesce(func.sum(case((AnswerFeedback.rating == "LIKE", 1), else_=0)), 0),
        func.coalesce(func.sum(case((AnswerFeedback.rating == "DISLIKE", 1), else_=0)), 0),
    )
    if filters:
        totals_stmt = totals_stmt.where(*filters)
    likes_count, dislikes_count = (await session.execute(totals_stmt)).one()

    items_stmt = select(AnswerFeedback)
    if filters:
        items_stmt = items_stmt.where(*filters)
    if cursor is not None:
        cur_dt, cur_id = _decode_admin_feedback_cursor(scope_hash, cursor)
        items_stmt = items_stmt.where(
            or_(
                AnswerFeedback.created_at < cur_dt,
                and_(AnswerFeedback.created_at == cur_dt, AnswerFeedback.id < cur_id),
            )
        )
    rows = list(
        (
            await session.execute(
                items_stmt.order_by(AnswerFeedback.created_at.desc(), AnswerFeedback.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        _encode_admin_feedback_cursor(scope_hash, page_rows[-1])
        if has_more and page_rows
        else None
    )

    return AdminFeedbackPageOut(
        totals=AdminFeedbackTotalsOut(
            likes=int(likes_count or 0),
            dislikes=int(dislikes_count or 0),
        ),
        items=[
            AdminFeedbackRowOut(
                id=row.id,
                organization_id=row.tenant_id,
                project_id=row.project_id,
                message_id=row.message_id,
                rating=row.rating,  # type: ignore[arg-type]
                reason_category=row.reason_category,  # type: ignore[arg-type]
                reason_text=row.reason_text,
                created_at=_as_utc(row.created_at),
                updated_at=_as_utc(row.updated_at),
            )
            for row in page_rows
        ],
        next_cursor=next_cursor,
    )
