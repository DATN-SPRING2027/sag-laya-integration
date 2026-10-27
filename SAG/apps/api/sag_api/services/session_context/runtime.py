"""Thread-only context orchestration. Authority is revalidated before dispatch/publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from threading import Lock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.errors import ServiceUnavailableError, ValidationError
from sag_api.db.models import (
    Message,
    SessionContextIndex,
    SessionMessageProvenance,
    Source,
    SourceProjectMapping,
    Thread,
)
from sag_api.services.session_context.budget import BudgetLimits, BudgetState
from sag_api.services.session_context.graph import LocalFeatureEncoder, text_hash
from sag_api.services.session_context.index import _INFERENCE_LOCK, IndexedSession, sync_index
from sag_api.services.session_context.model import BudgetedLLM, ModelTokenCounter
from sag_api.services.session_context.packing import build_memory_messages, fit_memory_messages
from sag_api.services.session_context.reader import FixtureTraceStore, read_history
from sag_api.services.session_context.scope import (
    eligible_message_ids,
    make_snapshot,
    resolve_scope,
    validate_selection,
)
from sag_api.services.session_context.selection import SelectionCaps, select_context
from sag_api.services.session_context.storage import read_records, record_provenance, source_dependencies
from sag_api.services.session_context.types import (
    ContextSelection,
    Coverage,
    EvidenceSpan,
    MessageReference,
    ResolvedEvidence,
    SessionKey,
    SessionScope,
    require,
)
from sag_api.services.source_service import get_authorized_source_ids

CAPS = SelectionCaps(4, 2, 1, 16, 16000, 4, 20, 0.6, 0.6)


_ENCODER = None
_ENCODER_LOCK = Lock()


def configured_encoder():
    global _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            try:
                import torch

                torch.set_num_threads(4)
                work = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 32000000, time.monotonic() + 180))
                _ENCODER = LocalFeatureEncoder(chunk_codepoints=384, budget=work)
            except (OSError, ImportError) as error:
                raise ServiceUnavailableError("Mô hình context session chưa sẵn sàng trên máy chủ.") from error
        return _ENCODER


@dataclass
class SessionContext:
    scope: SessionScope
    indexed: IndexedSession
    selection: ContextSelection
    original: tuple[ResolvedEvidence, ...]
    resolved: tuple[ResolvedEvidence, ...]
    groups: tuple[tuple[str, ...], ...]
    budget: BudgetState
    counter: ModelTokenCounter
    user_ref: MessageReference
    own_answers: set[str] = field(default_factory=set)
    lease_session: AsyncSession | None = field(default=None, repr=False)
    active_model: BudgetedLLM | None = field(default=None, repr=False)
    first_model_messages: list = field(default_factory=list, repr=False)
    budget_profile: str = "memory"
    tool_source_ids: set[str] = field(default_factory=set, repr=False)
    dispatched_evidence: dict[str, ResolvedEvidence] = field(default_factory=dict, repr=False)
    dispatched_evidence_messages: list[str] = field(default_factory=list, repr=False)
    dispatched_calls: list[dict] = field(default_factory=list, repr=False)
    request_evidence: tuple[ResolvedEvidence, ...] = field(default=(), repr=False)

    def enable_tool_budget(self, max_steps):
        require(self.budget.calls == 0, "late_budget_profile")
        if self.selection.mode == "normal":
            calls = min(7, max(1, int(max_steps)) + 1)
            self.budget.limits = replace(
                self.budget.limits, max_calls=calls, max_total_tokens=calls * self.budget.limits.context_window_tokens
            )
            self.budget_profile = "tool_assisted"

    def stage_request(self, evidence):
        self.request_evidence = tuple(evidence)

    def note_dispatch(self, evidence=None, *, messages=None):
        from sag_api.services.session_context.packing import PREFIX

        items = self.request_evidence if evidence is None else tuple(evidence)
        if not items and self.selection.mode != "full_session":
            items = self.resolved
        self.dispatched_evidence.update((item.span.evidence_id, item) for item in items)
        if messages is not None:
            normalized_msgs = [dict(m) for m in messages if isinstance(m, Mapping)]
            ev_msgs = [
                str(m.get("content", ""))
                for m in normalized_msgs
                if str(m.get("content", "")).startswith(PREFIX)
            ]
            msg_sha = hashlib.sha256(
                json.dumps(normalized_msgs, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            ev_sha = hashlib.sha256(
                json.dumps(ev_msgs, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            self.dispatched_evidence_messages.extend(ev_msgs)
            self.dispatched_calls.append(
                {
                    "call_index": len(self.dispatched_calls) + 1,
                    "messages_sha256": msg_sha,
                    "evidence_messages_sha256": ev_sha,
                    "evidence_messages": list(ev_msgs),
                    "fitted_evidence": [
                        {
                            "message_id": item.span.ref.message_id,
                            "kind": item.kind,
                            "text": item.text,
                        }
                        for item in items
                    ],
                }
            )

    def messages(self, agent, query):
        if self.selection.mode == "full_session":
            from sag_api.generation.prompt import build_agent_messages

            return build_agent_messages(agent.name, agent.persona or {}, query)
        return build_memory_messages(agent, query, self.original, records=self.indexed.records)

    def fit(self, messages, tools):
        from sag_api.services.session_context.packing import PREFIX, evidence_block

        limit = self.input_limit
        if self.budget_profile == "tool_assisted" and not any(item.get("role") == "tool" for item in messages):
            empty = [dict(item) for item in messages]
            for item in empty:
                if item.get("role") == "user" and str(item.get("content", "")).startswith(PREFIX):
                    item["content"] = evidence_block(())
            limit -= min(4096, max(0, limit - self.counter.count(empty, tools)))
        fitted, kept = fit_memory_messages(
            messages,
            tools,
            self.original,
            self.groups,
            counter=self.counter,
            input_limit=limit,
            records=self.indexed.records,
        )
        if kept != self.resolved:
            self.resolved = kept
            self.selection = replace(
                self.selection, spans=tuple(item.span for item in kept), state="partial", reason="prompt_budget"
            )
        self.request_evidence = kept
        return fitted

    @property
    def input_limit(self):
        return self.budget.limits.context_window_tokens - self.budget.limits.output_reserve_tokens

    async def revalidate(self, session, principal):
        dependencies = frozenset(source for record in self.indexed.records.values() for source in record.source_ids)
        current_scope = await resolve_scope(
            session,
            key=self.scope.key,
            principal=principal,
            candidate_source_ids=dependencies,
            requested_source_ids=self.scope.requested_source_ids,
        )
        records = await read_records(
            session, scope=current_scope, budget=self.budget, exclude_id=self.user_ref.message_id
        )
        for answer_id in self.own_answers:
            records.pop(answer_id, None)
        generation = self.indexed.generation
        if self.indexed.index_id:
            row = await session.get(SessionContextIndex, self.indexed.index_id, populate_existing=True)
            require(
                row is not None and row.generation == generation and row.state == "ready", "index_generation_changed"
            )
        user = await session.get(Message, self.user_ref.message_id, populate_existing=True)
        require(
            user is not None
            and user.thread_id == self.scope.key.thread_id
            and text_hash(user.content) == self.user_ref.content_hash,
            "current_query_changed",
        )
        return validate_selection(
            self.selection,
            scope=current_scope,
            records=records,
            current_snapshot=make_snapshot(current_scope, records, generation=generation),
        )

    async def lock_sources(self, source_ids, principal):
        if not source_ids:
            return
        session = self.lease_session
        require(session is not None, "session_lease_required")
        await session.execute(
            select(Source).where(Source.id.in_(sorted(source_ids))).order_by(Source.id).with_for_update(read=True)
        )
        await session.execute(
            select(SourceProjectMapping)
            .where(SourceProjectMapping.source_id.in_(sorted(source_ids)))
            .order_by(SourceProjectMapping.id)
            .with_for_update(read=True)
        )
        allowed = await get_authorized_source_ids(session, principal=principal, requested_source_ids=sorted(source_ids))
        if self.scope.requested_source_ids is not None:
            allowed &= self.scope.requested_source_ids
        require(set(source_ids) <= allowed, "session_source_revoked")

    @asynccontextmanager
    async def lease(self, factory, principal):
        async with asyncio.timeout_at(self.budget.limits.deadline):
            async with self._lease(factory, principal) as session:
                yield session

    @asynccontextmanager
    async def _lease(self, factory, principal):
        # PostgreSQL shared row locks serialize selected-evidence deletion/revocation
        # through publication. They are released on close/cancel, bounded by deadline.
        async with factory() as session:
            self.lease_session = session
            try:
                await session.execute(
                    select(Thread).where(Thread.id == self.scope.key.thread_id).with_for_update(read=True)
                )
                if self.indexed.index_id:
                    await session.execute(
                        select(SessionContextIndex)
                        .where(SessionContextIndex.id == self.indexed.index_id)
                        .with_for_update(read=True)
                    )
                lease_evidence = self.original if self.selection.mode == "full_session" else self.resolved
                ids = sorted(
                    {ref.message_id for item in lease_evidence for ref in item.dependency_refs}
                    | {self.user_ref.message_id}
                )
                for start in range(0, len(ids), 500):
                    batch = ids[start : start + 500]
                    await session.execute(
                        select(Message).where(Message.id.in_(batch)).order_by(Message.id).with_for_update(read=True)
                    )
                    await session.execute(
                        select(SessionMessageProvenance)
                        .where(SessionMessageProvenance.message_id.in_(batch))
                        .order_by(SessionMessageProvenance.message_id)
                        .with_for_update(read=True)
                    )
                await self.lock_sources(
                    frozenset(source for item in lease_evidence for source in item.source_ids), principal
                )
                await self.revalidate(session, principal)
                yield session
            finally:
                self.lease_session = None

    async def capture(self, session, message, *, principal, tool_source_ids):
        self.own_answers.add(message.id)
        await self.revalidate(self.lease_session, principal)
        material = {item.span.evidence_id: item for item in self.resolved} | self.dispatched_evidence
        sources = frozenset(tool_source_ids) | frozenset(
            source for item in material.values() for source in item.source_ids
        )
        await self.lock_sources(sources, principal)
        dependencies = {ref for item in material.values() for ref in item.dependency_refs} | {self.user_ref}
        write_scope = replace(self.scope, source_ids=self.scope.source_ids | sources)
        await record_provenance(
            session,
            message,
            scope=write_scope,
            dependencies=tuple(sorted(dependencies, key=lambda ref: ref.message_id)),
            source_ids=sources,
        )

    def metadata(self):
        return {
            "mode": self.selection.mode,
            "content_scope": "stored_text_only",
            "budget_profile": "full_session" if self.selection.mode == "full_session" else self.budget_profile,
            "state": self.selection.state,
            "reason": self.selection.reason,
            "eligible": len(self.selection.coverage.eligible_ids),
            "visited": len(self.selection.coverage.visited_ids),
            "evidence": [
                {
                    "message_id": item.span.ref.message_id,
                    "content_hash": item.span.ref.content_hash,
                    "start": item.span.start,
                    "end": item.span.end,
                }
                for item in self.resolved
            ],
        }


async def prepare_context(
    session,
    *,
    key,
    principal,
    user_message,
    query,
    requested_source_ids=None,
    mode="normal",
    encoder=None,
    counter=None,
    deadline=None,
):
    from sag_api.core.config import settings

    require(mode in {"normal", "full_session"}, "session_mode")
    full = mode == "full_session"
    if full and requested_source_ids:
        raise ValidationError(
            "Chế độ tổng hợp toàn phiên chưa hỗ trợ lọc theo @Source.",
            code="session_context_source_filter_unsupported",
        )
    deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + min(360 if full else 60, max(0, principal.expires_at - time.time())),
    )
    budget = BudgetState(
        BudgetLimits(
            settings.llm_context_window if full else min(settings.llm_context_window, 12288),
            4096,
            12 if full else 1,
            262144 if full else 12288,
            32000000 if full else 16000000,
            deadline,
        )
    )
    scope = await resolve_scope(
        session,
        key=key,
        principal=principal,
        candidate_source_ids=await source_dependencies(session, key=key),
        requested_source_ids=None if requested_source_ids is None else frozenset(requested_source_ids),
    )
    await record_provenance(session, user_message, scope=scope)
    await session.commit()
    if counter is None:
        try:
            counter = await asyncio.to_thread(
                ModelTokenCounter, settings.llm_model, template_kwargs={"enable_thinking": False}
            )
        except (OSError, ImportError) as error:
            raise ServiceUnavailableError("Mô hình context session chưa sẵn sàng trên máy chủ.") from error
    if full:
        records = await read_records(session, scope=scope, budget=budget, exclude_id=user_message.id)
        generation = make_snapshot(scope, records, generation="full").history_digest
        history = read_history(
            FixtureTraceStore(tuple(records.values()), scope=scope, generation=generation, budget=budget),
            scope=scope,
            page_size=128,
            budget=budget,
        )
        indexed = IndexedSession("", generation, records, history, ())
        spans = tuple(
            EvidenceSpan(message.reference, 0, len(message.content)) for message in history.messages if message.content
        )
        # A temporary normal-mode raw-input validation, never used as full-answer
        # traversal coverage. Full selection below still has zero visited units.
        raw_coverage = Coverage(history.coverage.eligible_ids, history.coverage.eligible_ids)
        raw_selection = ContextSelection(history.snapshot, "normal", "ready", spans, raw_coverage)
        selection = ContextSelection(history.snapshot, mode, "partial", (), history.coverage, "not_aggregated")
        groups = tuple((message.message_id,) for message in history.messages)
    else:
        if encoder is None:
            try:
                encoder = await asyncio.to_thread(configured_encoder)
            except (ImportError, OSError) as error:
                raise ServiceUnavailableError("Mô hình context session chưa sẵn sàng trên máy chủ.") from error
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            indexed = await sync_index(session, scope=scope, encoder=encoder, budget=budget, exclude_id=user_message.id)
        retrieval = BudgetState(
            replace(
                budget.limits,
                max_index_units=budget.limits.max_index_units - budget.index_units,
                deadline=min(deadline, time.monotonic() + 5),
            )
        )

        def retrieve():
            graph, hierarchy = indexed.views(scope=scope, budget=retrieval)
            with _INFERENCE_LOCK:
                query_features = encoder.featurize(query, budget=retrieval)
            return select_context(graph, hierarchy, query, query_features, scope=scope, budget=retrieval, caps=CAPS)

        async with asyncio.timeout(5):
            result = await asyncio.to_thread(retrieve)
        budget.consume_index_units(retrieval.index_units)
        selection, groups = result.selection, result.groups
    original = validate_selection(
        raw_selection if full else selection,
        scope=scope,
        current_snapshot=indexed.history.snapshot,
        records=indexed.records,
    )
    return SessionContext(
        scope,
        indexed,
        selection,
        original,
        () if full else original,
        groups,
        budget,
        counter,
        user_message_ref(user_message),
    )


def user_message_ref(message):
    from sag_api.services.session_context.types import MessageReference

    return MessageReference(message.id, text_hash(message.content))


async def visible_memory_ids(session, messages, *, principal, owner_key, requested_source_ids=None):
    visible = set()
    budget = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 16000000, time.monotonic() + 5))
    for thread_id in sorted({message.thread_id for message in messages}):
        thread = await session.get(Thread, thread_id, populate_existing=True)
        require(thread is not None and thread.owner_key == owner_key, "history_owner")
        key = SessionKey(owner_key, thread.agent_id, thread_id)
        scope = await resolve_scope(
            session,
            key=key,
            principal=principal,
            candidate_source_ids=await source_dependencies(session, key=key),
            requested_source_ids=None if requested_source_ids is None else frozenset(requested_source_ids),
        )
        records = await read_records(session, scope=scope, budget=budget)
        visible.update(eligible_message_ids(scope, records))
    return visible


def select_mode(query, requested=None):
    if requested is not None:
        require(requested in {"normal", "full_session"}, "session_mode")
        return requested
    normalized = " ".join(query.casefold().split())
    return (
        "full_session"
        if any(
            phrase in normalized
            for phrase in (
                "tất cả quyết định",
                "toàn bộ quyết định",
                "mọi quyết định",
                "tổng hợp toàn",
                "toàn bộ cuộc chat",
                "từ đầu cuộc chat",
                "all decisions",
                "entire session",
                "whole conversation",
            )
        )
        else "normal"
    )
