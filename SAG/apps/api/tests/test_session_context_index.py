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
    embedding_dimension = 2

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


def test_encode_propagates_internal_typeerror_without_second_attempt():
    from sag_api.services.session_context.index import _encode

    class FailingTypeErrorEncoder:
        model_id = "typeerror-model"
        calls = 0

        def featurize_many(self, texts, *, batch_size, budget, deduplicate=False, chunk_cache=None):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("internal inference TypeError")
            return ()

    encoder = FailingTypeErrorEncoder()
    with pytest.raises(TypeError, match="internal inference TypeError"):
        _encode(encoder, ("alpha",), work(), chunk_cache={})
    assert encoder.calls == 1


@pytest.mark.asyncio
async def test_sync_index_reuses_exact_chunks_across_slices_and_isolates_cache_across_jobs_and_models(storage_db):
    from dataclasses import replace

    from test_session_context_graph import stub_encoder

    factory, scope = storage_db
    async with factory() as session:
        session.add_all([raw("m1", content="🙂abcDEF", ordinal=0), raw("m2", content="🙂abcXYZ", ordinal=1)])
        await session.commit()

    encoder = stub_encoder(4)
    expected_m1 = encoder.featurize("🙂abcDEF", budget=work())
    expected_m2 = encoder.featurize("🙂abcXYZ", budget=work())

    def make_batch_stub(model_id="explicit-stub-model"):
        inst = stub_encoder(4)
        inst.model_id = model_id

        class BatchEncoder(type(inst.encoder)):
            @staticmethod
            def encode(texts, **kwargs):
                return [(1.0, 0.0) for _ in texts]

        class BatchNer(type(inst.ner)):
            def __call__(self, texts, **kwargs):
                return [super(BatchNer, self).__call__(text) for text in texts]

        inst.encoder, inst.ner = BatchEncoder(), BatchNer()
        return inst

    sliced_encoder = make_batch_stub()
    seen_caches = []
    orig_featurize_many = sliced_encoder.featurize_many

    def tracked_featurize_many(texts, **kwargs):
        seen_caches.append(id(kwargs.get("chunk_cache")))
        return orig_featurize_many(texts, **kwargs)

    sliced_encoder.featurize_many = tracked_featurize_many
    async with factory() as session:
        indexed = await sync_index(session, scope=scope, encoder=sliced_encoder, budget=work(), batch_size=1)
    assert indexed.features == (expected_m1, expected_m2)
    assert len(seen_caches) == 2 and seen_caches[0] == seen_caches[1]
    assert sliced_encoder.encoder_calls == 2 and sliced_encoder.ner_calls == 2
    assert sliced_encoder.encoder_tokens == 16 and sliced_encoder.ner_tokens == 16

    other_model_encoder = make_batch_stub("other-stub-model")
    other_caches = []
    orig_other = other_model_encoder.featurize_many

    def tracked_other(texts, **kwargs):
        other_caches.append(id(kwargs.get("chunk_cache")))
        return orig_other(texts, **kwargs)

    other_model_encoder.featurize_many = tracked_other
    async with factory() as session:
        indexed_other = await sync_index(
            session,
            scope=replace(scope, source_ids=frozenset()),
            encoder=other_model_encoder,
            budget=work(),
            batch_size=1,
        )
    assert len(other_caches) == 2 and other_caches[0] == other_caches[1]
    assert other_caches[0] != seen_caches[0]
    assert other_model_encoder.encoder_calls == 2
    assert other_model_encoder.encoder_tokens == 16
    assert all(f.model_id == "other-stub-model" for f in indexed_other.features)

    # Even if a caller passes the same dict across two different model_ids, entries are keyed by model_id.
    shared_dict = {}
    model_a = make_batch_stub("model-a")
    model_b = make_batch_stub("model-b")
    model_a.featurize_many(("🙂abcDEF",), batch_size=2, budget=work(), deduplicate=True, chunk_cache=shared_dict)
    model_b.featurize_many(("🙂abcDEF",), batch_size=2, budget=work(), deduplicate=True, chunk_cache=shared_dict)
    assert model_a.encoder_calls == 1 and model_b.encoder_calls == 1


@pytest.mark.asyncio
async def test_corrupted_cached_vector_dimension_is_rebuilt_from_raw_message(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add_all(
            [
                raw("m1", content="First decision ORBIT-1", ordinal=1),
                raw("m2", content="Second decision ORBIT-2", ordinal=2),
            ]
        )
        await session.commit()
        first_encoder = Encoder()
        first = await sync_index(session, scope=scope, encoder=first_encoder, budget=work(), batch_size=2)
        assert first_encoder.calls == 1
        assert all(len(feature.vector) == 2 for feature in first.features)

        row = await session.get(SessionContextIndex, first.index_id)
        corrupted_features = dict(row.features)
        corrupted_m2 = dict(corrupted_features["m2"])
        corrupted_m2["vector"] = [1.0, 0.1, 0.2]
        corrupted_features["m2"] = corrupted_m2
        row.features = corrupted_features
        await session.commit()

    async with factory() as session:
        healed_encoder = Encoder()
        healed = await sync_index(session, scope=scope, encoder=healed_encoder, budget=work(), batch_size=2)
        assert healed_encoder.calls == 1
        assert all(len(feature.vector) == 2 for feature in healed.features)
        graph, _ = healed.views(scope=scope, budget=work())
        assert len(graph.features) == 2
        row = await session.get(SessionContextIndex, healed.index_id)
        assert len(row.features["m1"]["vector"]) == 2
        assert len(row.features["m2"]["vector"]) == 2

    async with factory() as session:
        reuse_encoder = Encoder()
        reused = await sync_index(session, scope=scope, encoder=reuse_encoder, budget=work(), batch_size=2)
        assert reuse_encoder.calls == 0
        assert all(len(feature.vector) == 2 for feature in reused.features)


@pytest.mark.asyncio
async def test_all_cached_vectors_wrong_dimension_and_dimensionless_encoder_are_rebuilt(storage_db):
    import hashlib

    from sag_api.services.session_context.graph import TextFeatures

    factory, scope = storage_db
    async with factory() as session:
        session.add_all(
            [
                raw("m1", content="First decision ORBIT-1", ordinal=1),
                raw("m2", content="Second decision ORBIT-2", ordinal=2),
            ]
        )
        await session.commit()
        first = await sync_index(session, scope=scope, encoder=Encoder(), budget=work(), batch_size=2)
        row = await session.get(SessionContextIndex, first.index_id)
        row.features = {
            mid: {**dict(feat), "vector": [1.0, 0.2, 0.3, 0.4]}
            for mid, feat in row.features.items()
        }
        await session.commit()

    async with factory() as session:
        healed_encoder = Encoder()
        healed = await sync_index(session, scope=scope, encoder=healed_encoder, budget=work(), batch_size=2)
        assert healed_encoder.calls == 1
        assert all(len(f.vector) == 2 for f in healed.features)
        healed.views(scope=scope, budget=work())

    class DimensionlessEncoder:
        model_id = "dimensionless-test-model"
        calls = 0

        def featurize(self, text, *, budget):
            self.calls += 1
            return TextFeatures(
                hashlib.sha256(text.encode()).hexdigest(),
                self.model_id,
                (1.0, 0.5),
                (),
            )

        def featurize_many(self, texts, *, batch_size, budget, deduplicate=False, chunk_cache=None):
            return tuple(self.featurize(t, budget=budget) for t in texts)

    async with factory() as session:
        dimless = DimensionlessEncoder()
        idx1 = await sync_index(session, scope=scope, encoder=dimless, budget=work(), batch_size=2)
        row = await session.get(SessionContextIndex, idx1.index_id)
        corrupted = dict(row.features)
        corrupted["m1"] = {**dict(corrupted["m1"]), "vector": [1.0, 0.5, 0.9]}
        row.features = corrupted
        session.add(raw("m3", content="Third decision ORBIT-3", ordinal=3))
        await session.commit()

    async with factory() as session:
        dimless2 = DimensionlessEncoder()
        idx2 = await sync_index(session, scope=scope, encoder=dimless2, budget=work(), batch_size=2)
        assert all(len(f.vector) == 2 for f in idx2.features)
        idx2.views(scope=scope, budget=work())
