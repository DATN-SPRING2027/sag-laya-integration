"""Durable feature checkpoints and mutation fences, without provider calls."""

import asyncio
import threading

import pytest
import test_session_context_storage as storage_fixtures
from sqlalchemy import delete, select, update
from test_session_context_storage import raw, work

from sag_api.db.models import Message, SessionContextIndex
from sag_api.services.session_context.graph import TextFeatures, text_hash
from sag_api.services.session_context.index import sync_index
from sag_api.services.session_context.storage import invalidate, read_records
from sag_api.services.session_context.types import ContextContractError

storage_db = storage_fixtures.storage_db


class Encoder:
    model_id = "test-model"

    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after

    def featurize_many(self, texts, **kwargs):
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("worker interrupted")
        self.calls += 1
        return tuple(TextFeatures(text_hash(text), self.model_id, (1.0, 0.1)) for text in texts)


@pytest.mark.asyncio
async def test_restart_resumes_only_unfinished_features_and_incrementally_adds(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add_all([raw("a", ordinal=0), raw("b", ordinal=1)])
        await session.commit()
        with pytest.raises(RuntimeError, match="interrupted"):
            await sync_index(session, scope=scope, encoder=Encoder(1), budget=work(), batch_size=1)
    encoder = Encoder()
    async with factory() as session:
        indexed = await sync_index(session, scope=scope, encoder=encoder, budget=work(), batch_size=1)
        assert len(indexed.history.messages) == 2 and encoder.calls == 1
        row = (await session.execute(select(SessionContextIndex))).scalar_one()
        assert row.state == "ready" and len(row.features) == 2
        session.add(raw("c", ordinal=2))
        await session.commit()
        indexed = await sync_index(session, scope=scope, encoder=encoder, budget=work(), batch_size=1)
        assert len(indexed.history.messages) == 3 and encoder.calls == 2


@pytest.mark.asyncio
async def test_interrupted_build_then_delete_restart_discards_old_features(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add(raw("a"))
        await session.commit()

        class MutatingEncoder(Encoder):
            def featurize_many(self, texts, **kwargs):
                raise RuntimeError("simulated pause")

        with pytest.raises(RuntimeError):
            await sync_index(session, scope=scope, encoder=MutatingEncoder(), budget=work())
        message = await session.get(Message, "a")
        await session.delete(message)
        await invalidate(session, "thread")
        await session.commit()
        indexed = await sync_index(session, scope=scope, encoder=Encoder(), budget=work())
        assert indexed.history.messages == ()
        assert not (await read_records(session, scope=scope, budget=work()))
        row = (await session.execute(select(SessionContextIndex))).scalar_one()
        assert row.features == {}


class PausedEncoder(Encoder):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def featurize_many(self, texts, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=8):
            raise TimeoutError("test did not release feature encoding")
        return super().featurize_many(texts, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,reason", [("content", "index_snapshot_changed"),
                                            ("delete_and_invalidate", "index_generation_changed")])
async def test_mutation_during_encoding_blocks_ready_publication(storage_db, mutation, reason):
    factory, scope = storage_db
    async with factory() as writer:
        writer.add(raw("a", content="old text"))
        await writer.commit()
    encoder = PausedEncoder()
    async with factory() as reader:
        task = asyncio.create_task(sync_index(reader, scope=scope, encoder=encoder, budget=work()))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(encoder.entered.wait, 5), timeout=6)
            async with factory() as writer:
                if mutation == "content":
                    # Bypass the invalidation hook deliberately: the fresh raw
                    # snapshot comparison must independently catch this change.
                    await writer.execute(update(Message).where(Message.id == "a").values(content="changed text"))
                else:
                    await writer.execute(delete(Message).where(Message.id == "a"))
                    await invalidate(writer, scope.key.thread_id)
                await writer.commit()
            encoder.release.set()
            with pytest.raises(ContextContractError, match=reason):
                await asyncio.wait_for(task, timeout=8)
        finally:
            encoder.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        row = (await session.scalars(select(SessionContextIndex))).one()
        assert row.state != "ready"
        if mutation == "delete_and_invalidate":
            assert row.state == "stale" and row.features == {}
        current = await sync_index(session, scope=scope, encoder=Encoder(), budget=work())
        assert [message.content for message in current.history.messages] == (
            ["changed text"] if mutation == "content" else [])


@pytest.mark.asyncio
async def test_duplicate_jobs_for_same_snapshot_publish_one_consistent_ready_index(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add(raw("a"))
        await session.commit()
    encoder = PausedEncoder()
    second_started = asyncio.Event()

    async def another_job():
        async with factory() as session:
            second_started.set()
            return await sync_index(session, scope=scope, encoder=Encoder(), budget=work())

    async with factory() as session:
        first = asyncio.create_task(sync_index(session, scope=scope, encoder=encoder, budget=work()))
        second = None
        try:
            assert await asyncio.wait_for(asyncio.to_thread(encoder.entered.wait, 5), timeout=6)
            second = asyncio.create_task(another_job())
            await asyncio.wait_for(second_started.wait(), timeout=5)
            encoder.release.set()
            one, two = await asyncio.wait_for(asyncio.gather(first, second), timeout=8)
            assert one.index_id == two.index_id and one.generation == two.generation
            assert one.features == two.features
        finally:
            encoder.release.set()
            pending = [task for task in (first, second) if task is not None and not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    async with factory() as session:
        row = (await session.scalars(select(SessionContextIndex))).one()
        assert row.state == "ready" and set(row.features) == {"a"}


@pytest.mark.asyncio
async def test_invalid_cached_entity_offsets_are_rebuilt_before_ready_publication(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add(raw("a", content="Paris"))
        await session.commit()
        await sync_index(session, scope=scope, encoder=Encoder(), budget=work())
        row = (await session.scalars(select(SessionContextIndex))).one()
        corrupted = {key: dict(value) for key, value in row.features.items()}
        corrupted["a"]["mentions"] = [{"start": 0, "end": 100, "label": "LOC"}]
        row.features = corrupted
        await session.commit()
    encoder = Encoder()
    async with factory() as session:
        indexed = await sync_index(session, scope=scope, encoder=encoder, budget=work())
        assert encoder.calls == 1
        indexed.views(scope=scope, budget=work())
        row = (await session.scalars(select(SessionContextIndex))).one()
        assert row.features["a"]["mentions"] == [] and row.state == "ready"
