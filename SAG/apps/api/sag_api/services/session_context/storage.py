"""Authorized keyset reads and rebuildable metadata; raw messages remain authoritative."""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import Message, SessionContextIndex, SessionMessageProvenance
from sag_api.enums import MessageRole
from sag_api.services.agent_domain import get_thread
from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.graph import text_hash
from sag_api.services.session_context.scope import _check_time
from sag_api.services.session_context.types import (
    ContextContractError,
    MessageReference,
    SessionKey,
    SessionScope,
    TraceMessage,
    require,
)


async def source_dependencies(session: AsyncSession, *, key: SessionKey) -> frozenset[str]:
    await get_thread(session, key.agent_id, key.thread_id, owner_key=key.owner_key)
    result = await session.execute(
        select(SessionMessageProvenance.source_ids)
        .join(Message, Message.id == SessionMessageProvenance.message_id)
        .where(Message.thread_id == key.thread_id, SessionMessageProvenance.owner_key == key.owner_key)
    )
    return frozenset(
        value
        for values in result.scalars()
        if isinstance(values, list)
        for value in values
        if isinstance(value, str) and value
    )


async def read_records(
    session: AsyncSession, *, scope: SessionScope, budget: BudgetState, exclude_id: str | None = None
) -> dict[str, TraceMessage]:
    _check_time(scope, None)
    await get_thread(session, scope.key.agent_id, scope.key.thread_id, owner_key=scope.key.owner_key)
    records = {}
    after = None
    while True:
        budget.consume_index_units(1)
        statement = (
            select(Message, SessionMessageProvenance)
            .outerjoin(SessionMessageProvenance, Message.id == SessionMessageProvenance.message_id)
            .where(
                Message.thread_id == scope.key.thread_id, Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT))
            )
        )
        if exclude_id is not None:
            statement = statement.where(Message.id != exclude_id)
        if after is not None:
            at, message_id = after
            statement = statement.where(
                or_(Message.created_at > at, and_(Message.created_at == at, Message.id > message_id))
            )
        rows = (await session.execute(statement.order_by(Message.created_at, Message.id).limit(128)
                                      .execution_options(populate_existing=True))).all()
        for message, provenance in rows:
            budget.consume_index_units(1 + len(message.content))
            role = message.role.value
            status = message.status.value
            origin, partition, sources, dependencies = "unknown", None, frozenset(), ()
            if (
                provenance is not None
                and provenance.owner_key == scope.key.owner_key
                and provenance.content_hash == text_hash(message.content)
            ):
                try:
                    require(
                        isinstance(provenance.source_ids, list) and isinstance(provenance.dependencies, list),
                        "stored_provenance",
                    )
                    sources = frozenset(provenance.source_ids)
                    dependencies = tuple(MessageReference(**ref) for ref in provenance.dependencies)
                    origin, partition = provenance.origin, provenance.partition_fingerprint
                except (ContextContractError, TypeError, ValueError):
                    origin, partition, sources, dependencies = "unknown", None, frozenset(), ()
            elif (
                provenance is None
                and role == "user"
                and scope.issuer == "continuum-sag-session"
                and len(scope.project_ids) == 1
            ):
                # This issuer's conversation owner hash includes the signed Project.
                # General BE assertions do NOT bind historical project membership.
                origin, partition = "recorded", scope.partition_fingerprint
            try:
                record = TraceMessage(
                    scope.key,
                    message.id,
                    role,
                    message.content,
                    message.created_at.astimezone(UTC),
                    status,
                    origin,
                    sources,
                    dependencies,
                    partition,
                )
            except ContextContractError:
                record = TraceMessage(
                    scope.key, message.id, role, message.content, message.created_at.astimezone(UTC), status, "unknown"
                )
            records[message.id] = record
        if len(rows) < 128:
            break
        after = (rows[-1][0].created_at, rows[-1][0].id)
    _check_time(scope, None)
    return records


async def record_provenance(
    session: AsyncSession,
    message: Message,
    *,
    scope: SessionScope,
    dependencies: tuple[MessageReference, ...] = (),
    source_ids: frozenset[str] = frozenset(),
) -> None:
    _check_time(scope, None)
    await get_thread(session, scope.key.agent_id, scope.key.thread_id, owner_key=scope.key.owner_key)
    require(message.thread_id == scope.key.thread_id and source_ids <= scope.source_ids, "provenance_scope")
    # Validate shape before serializing; never infer assistant independence from [] citations.
    trace = TraceMessage(
        scope.key,
        message.id,
        message.role.value,
        message.content,
        message.created_at,
        message.status.value,
        "recorded",
        source_ids,
        dependencies,
        scope.partition_fingerprint,
    )
    existing = await session.get(SessionMessageProvenance, message.id)
    if existing is not None:
        require(
            existing.owner_key == scope.key.owner_key
            and existing.content_hash == trace.reference.content_hash
            and existing.partition_fingerprint == scope.partition_fingerprint
            and existing.source_ids == sorted(source_ids)
            and existing.dependencies
            == [{"message_id": ref.message_id, "content_hash": ref.content_hash} for ref in dependencies],
            "provenance_immutable",
        )
        return
    session.add(
        SessionMessageProvenance(
            message_id=message.id,
            owner_key=scope.key.owner_key,
            content_hash=trace.reference.content_hash,
            partition_fingerprint=scope.partition_fingerprint,
            source_ids=sorted(source_ids),
            dependencies=[{"message_id": ref.message_id, "content_hash": ref.content_hash} for ref in dependencies],
            origin="recorded",
        )
    )
    await session.flush()


async def invalidate(session: AsyncSession, thread_id: str) -> None:
    await session.execute(
        update(SessionContextIndex)
        .where(SessionContextIndex.thread_id == thread_id)
        .values(generation=uuid4().hex, state="stale", features={}, checkpoint={})
    )
