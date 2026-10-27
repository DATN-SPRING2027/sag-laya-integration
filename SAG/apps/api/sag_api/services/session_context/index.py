"""Resumable, scope-bound feature checkpoints with compare-and-swap publication."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import SessionContextIndex
from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.graph import EntityMention, TextFeatures, build_graph, entity_keys
from sag_api.services.session_context.hierarchy import build_hierarchy
from sag_api.services.session_context.reader import FixtureTraceStore, HistoryRead, read_history
from sag_api.services.session_context.scope import make_snapshot
from sag_api.services.session_context.storage import read_records
from sag_api.services.session_context.types import (
    ContextContractError,
    SessionScope,
    TraceMessage,
    digest_json,
    require,
)

_INFERENCE_LOCK = threading.Lock()


@dataclass(frozen=True)
class IndexedSession:
    index_id: str
    generation: str
    records: dict[str, TraceMessage] = field(repr=False)
    history: HistoryRead = field(repr=False)
    features: tuple[TextFeatures, ...] = field(repr=False)

    def views(self, *, scope: SessionScope, budget: BudgetState):
        return (
            build_graph(self.history, self.features, scope=scope, budget=budget, gap_seconds=1800),
            build_hierarchy(
                self.history,
                scope=scope,
                budget=budget,
                window_turns=4,
                episode_windows=4,
                local_radius=1,
                gap_seconds=1800,
            ),
        )


def _encode(encoder, texts, budget, chunk_cache=None):
    # Only weights are shared. Inputs/results stay inside one authorized job.
    with _INFERENCE_LOCK:
        budget.consume_index_units(0)
        return encoder.featurize_many(
            texts, batch_size=16, budget=budget, deduplicate=True, chunk_cache=chunk_cache
        )


async def sync_index(
    session: AsyncSession,
    *,
    scope: SessionScope,
    encoder,
    budget: BudgetState,
    exclude_id: str | None = None,
    batch_size: int = 16,
) -> IndexedSession:
    require(type(batch_size) is int and 1 <= batch_size <= 32, "index_batch")
    records = await read_records(session, scope=scope, budget=budget, exclude_id=exclude_id)
    index_id = digest_json([scope.key.owner_key, scope.key.thread_id, scope.fingerprint, encoder.model_id])
    basis = make_snapshot(scope, records, generation="basis").history_digest
    row = await session.get(SessionContextIndex, index_id, populate_existing=True)
    if row is None:
        row = SessionContextIndex(
            id=index_id,
            thread_id=scope.key.thread_id,
            owner_key=scope.key.owner_key,
            scope_fingerprint=scope.fingerprint,
            model_id=encoder.model_id,
            generation=uuid4().hex,
            snapshot_digest=basis,
            state="indexing",
            checkpoint={},
            features={},
        )
        session.add(row)
        await session.commit()
    elif row.snapshot_digest != basis or row.state == "stale":
        old_generation = row.generation
        changed = await session.execute(
            update(SessionContextIndex)
            .where(SessionContextIndex.id == index_id, SessionContextIndex.generation == old_generation)
            .values(generation=uuid4().hex, snapshot_digest=basis, state="indexing", checkpoint={})
        )
        require(changed.rowcount == 1, "index_generation_changed")
        await session.commit()
        row = await session.get(SessionContextIndex, index_id, populate_existing=True)
    generation = row.generation
    history = read_history(
        FixtureTraceStore(tuple(records.values()), scope=scope, generation=generation, budget=budget),
        scope=scope,
        page_size=128,
        budget=budget,
    )
    features = {}
    for message in history.messages:
        artifact = (row.features or {}).get(message.message_id)
        if artifact is None:
            continue
        try:
            feature = TextFeatures(
                artifact["content_hash"],
                artifact["model_id"],
                tuple(artifact["vector"]),
                tuple(EntityMention(**item) for item in artifact["mentions"]),
            )
            entity_keys(message.content, feature)
            if feature.content_hash == message.reference.content_hash and feature.model_id == encoder.model_id:
                features[message.message_id] = feature
        except (ContextContractError, KeyError, TypeError, ValueError):
            continue  # A damaged derived artifact is rebuilt from the authorized original.
    pending = [message for message in history.messages if message.message_id not in features]

    async def checkpoint(state):
        budget.consume_index_units(len(features))
        changed = await session.execute(
            update(SessionContextIndex)
            .where(
                SessionContextIndex.id == index_id,
                SessionContextIndex.generation == generation,
                SessionContextIndex.snapshot_digest == basis,
            )
            .values(
                state=state,
                checkpoint={"completed_ids": sorted(features)},
                features={key: asdict(value) for key, value in features.items()},
            )
        )
        require(changed.rowcount == 1, "index_generation_changed")
        await session.commit()

    chunk_cache = {}
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        encoded = await asyncio.to_thread(
            _encode, encoder, tuple(message.content for message in batch), budget, chunk_cache
        )
        for message, feature in zip(batch, encoded, strict=True):
            require(
                feature.model_id == encoder.model_id and feature.content_hash == message.reference.content_hash,
                "feature_content_changed",
            )
            features[message.message_id] = feature
        await checkpoint("indexing")
    current = await read_records(session, scope=scope, budget=budget, exclude_id=exclude_id)
    require(make_snapshot(scope, current, generation="basis").history_digest == basis, "index_snapshot_changed")
    await checkpoint("ready")
    return IndexedSession(
        index_id, generation, records, history, tuple(features[message.message_id] for message in history.messages)
    )
