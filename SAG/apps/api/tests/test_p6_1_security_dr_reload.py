"""TDD regression tests for P6-1 Packet 3: Cross-tenant cache, DR rollback, advisory lock & hot-reload."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    SearchUnit,
    Source,
    SourceProjectMapping,
)
from sag_api.db.models.routing_rag import TreeManifest
from sag_api.enums import DocumentStatus
from sag_api.jobs.knowledge import KnowledgeWorker
from sag_api.runtime import KnowledgeRuntime
from sag_api.services import tree_publish_service, tree_rollback_service
from sag_api.services.session_context import IncrementalSessionIndex
from sag_api.services.session_context import extractor as ext
from sag_api.services.tree_publish_service import TreePublishResult, _checksum_manifest
from sag_api.services.tree_rollback_service import _prepared_candidate_from_manifest


@pytest_asyncio.fixture
async def p6_3_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Cross-Tenant _TURN_EXTRACTION_CACHE Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_cache_key_requires_tenant_id_and_isolates_tenants_and_threads():
    ext.clear_turn_extraction_cache()

    k_a1 = ext._turn_cache_key("OK chốt phương án 2", "user", "Qwen/Qwen3-8B", tenant_id="tenant-a", thread_id="th-1")
    k_b1 = ext._turn_cache_key("OK chốt phương án 2", "user", "Qwen/Qwen3-8B", tenant_id="tenant-b", thread_id="th-1")
    k_a2 = ext._turn_cache_key("OK chốt phương án 2", "user", "Qwen/Qwen3-8B", tenant_id="tenant-a", thread_id="th-2")
    assert k_a1 != k_b1
    assert k_a1 != k_a2

    with pytest.raises(ValueError, match="tenant_id"):
        ext._turn_cache_key("OK chốt phương án 2", "user", "Qwen/Qwen3-8B", tenant_id="", thread_id="th-1")

    call_idx = 0

    async def fake_complete(_messages, *, response_format=None):
        nonlocal call_idx
        call_idx += 1
        entity = "SecretProjectAlpha" if call_idx == 1 else "PublicProjectBeta"
        return json.dumps(
            {
                "turns": [
                    {
                        "turn_id": "t-same",
                        "epistemic_status": "CONFIRMED",
                        "topic_slot_key": f"slot:plan_{call_idx}",
                        "is_anaphoric_or_short": True,
                        "is_interrogative": False,
                        "entities": [{"surface": entity, "canonical_key": entity.lower(), "soft_aliases": []}],
                        "superseded_entities": [],
                    }
                ]
            }
        )

    llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_complete))
    turns = [{"id": "t-same", "role": "user", "content": "OK chốt phương án 2"}]

    res_a, deg_a, _ = await ext.extract_turns_structured_batch(
        turns, llm=llm, tenant_id="tenant-a", thread_id="th-1"
    )
    res_b, deg_b, _ = await ext.extract_turns_structured_batch(
        turns, llm=llm, tenant_id="tenant-b", thread_id="th-1"
    )
    assert deg_a is False and deg_b is False
    assert llm.complete.await_count == 2
    assert res_a[0].topic_slot_key == "slot:plan_1"
    assert res_b[0].topic_slot_key == "slot:plan_2"
    assert any("secretprojectalpha" in m.key for m in res_a[0].entities)
    assert not any("secretprojectalpha" in m.key for m in res_b[0].entities)

    # Same tenant + same thread DOES hit cache (no extra LLM call)
    res_a_cached, _, _ = await ext.extract_turns_structured_batch(
        turns, llm=llm, tenant_id="tenant-a", thread_id="th-1"
    )
    assert llm.complete.await_count == 2
    assert res_a_cached[0].topic_slot_key == "slot:plan_1"

    # Missing tenant_id MUST bypass cache (fail-closed)
    await ext.extract_turns_structured_batch(turns, llm=llm)
    await ext.extract_turns_structured_batch(turns, llm=llm)
    assert llm.complete.await_count == 4

    # Substrate with distinct scope_signatures never shares extraction cache across tenants
    idx_a = IncrementalSessionIndex("th-shared", scope_signature="sig-tenant-a")
    idx_b = IncrementalSessionIndex("th-shared", scope_signature="sig-tenant-b")
    await idx_a.append_messages([{"id": "m1", "role": "user", "content": "OK chốt phương án 2"}], llm=llm)
    before = llm.complete.await_count
    await idx_b.append_messages([{"id": "m1", "role": "user", "content": "OK chốt phương án 2"}], llm=llm)
    assert llm.complete.await_count == before + 1


# ---------------------------------------------------------------------------
# 2. Tree Rollback & sag_ops CLI Crash
# ---------------------------------------------------------------------------


def test_prepared_candidate_from_manifest_handles_knowledge_unit_ids_tuple():
    manifest_payload: dict = {
        "project_id": "proj-rb",
        "tenant_id": "t-1",
        "source_tree_version": "tv-0",
        "expected_point_count": 1,
        "quality_gates": {"recall": True},
        "routing_profiles": [
            {
                "node_id": "leaf-1",
                "parent_node_id": "root",
                "partition_id": "public",
                "level": 1,
                "is_leaf": True,
                "unit_ids": ["ku-1", "ku-2"],
                "child_node_ids": [],
            }
        ],
        "source_routing_profiles": [],
        "search_unit_assignments": [
            {
                "knowledge_unit_ids": ["ku-1", "ku-2"],
                "search_unit_id": "su-1",
                "source_id": "src-1",
                "document_version_id": "ver-1",
                "partition_id": "public",
            }
        ],
        "security_partition_search_unit_counts": {"public": 1},
    }
    chk = _checksum_manifest(manifest_payload)
    tv = f"tree-{chk[:24]}"
    manifest_payload["checksum"] = chk
    manifest_payload["tree_version"] = tv

    rec = TreeManifest(
        tree_version=tv,
        project_id="proj-rb",
        config_version="v1",
        node_count=1,
        leaf_count=1,
        max_leaf_size=2,
        giant_ratio=0.0,
        routing_recall_at_k=1.0,
        escape_win_rate=0.0,
        acl_blackhole_rate=0.0,
        status="INACTIVE",
        checksum=chk,
        manifest_json=json.dumps(manifest_payload),
    )

    candidate = _prepared_candidate_from_manifest(rec, expected_status="INACTIVE")
    assert candidate.tree_version == tv
    assert candidate.search_unit_assignments[0].knowledge_unit_ids == ("ku-1", "ku-2")
    assert isinstance(candidate.search_unit_assignments[0].knowledge_unit_ids, tuple)


@pytest.mark.asyncio
async def test_sag_ops_run_rollback_tree_matches_tree_publish_result_fields(monkeypatch, capsys):
    from sag_api.sag import engine_manager as em_mod
    from scripts import sag_ops

    fake_result = TreePublishResult(
        project_id="proj-ops",
        tree_version="tree-abc123",
        routing_slot="SLOT_B",
        search_epoch=7,
        checksum="c" * 64,
        point_count=42,
    )
    fake_client = SimpleNamespace(aclose=AsyncMock())
    fake_em = SimpleNamespace(
        get_search_unit_qdrant_client=AsyncMock(return_value=fake_client),
        aclose_all=AsyncMock(),
    )

    monkeypatch.setattr(em_mod, "EngineManager", lambda _s: fake_em)
    monkeypatch.setattr(tree_rollback_service, "rollback_tree_candidate", AsyncMock(return_value=fake_result))

    ok = await sag_ops.run_rollback_tree("proj-ops", "tree-abc123")
    assert ok is True
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "success"
    assert out["project_id"] == "proj-ops"
    assert out["routing_slot"] == "SLOT_B"
    assert out["tree_version"] == "tree-abc123"
    assert out["search_epoch"] == 7
    assert out["checksum"] == "c" * 64
    assert out["point_count"] == 42


# ---------------------------------------------------------------------------
# 3. PostgreSQL Advisory Lock Invalidates Connection on CancelledError / TimeoutError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("unlock_error", [asyncio.CancelledError(), TimeoutError("db timeout")])
async def test_project_publish_lock_invalidates_connection_on_unlock_base_exception(monkeypatch, unlock_error):
    invalidated = False
    closed = False
    acquire_called = False

    class FakeConn:
        async def start(self):
            return None

        @asynccontextmanager
        async def begin(self):
            yield

        async def scalar(self, stmt, _params=None):
            nonlocal acquire_called
            sql_text = str(stmt)
            if "pg_try_advisory_lock" in sql_text:
                acquire_called = True
                return True
            if "pg_advisory_unlock" in sql_text:
                raise unlock_error
            return None

        async def execute(self, stmt, _params=None):
            if "pg_advisory_unlock" in str(stmt):
                raise unlock_error
            return None

        async def invalidate(self):
            nonlocal invalidated
            invalidated = True

        async def close(self):
            nonlocal closed
            closed = True

    fake_pg_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    fake_lock_engine = SimpleNamespace(connect=lambda: FakeConn())
    monkeypatch.setattr(tree_publish_service, "engine", fake_pg_engine)
    monkeypatch.setattr(tree_publish_service, "publish_lock_engine", fake_lock_engine)

    if isinstance(unlock_error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            async with tree_publish_service._project_publish_lock("proj-lock-cancel"):
                pass
    else:
        async with tree_publish_service._project_publish_lock("proj-lock-timeout"):
            pass

    assert acquire_called is True
    assert invalidated is True, "Locked PG connection MUST be invalidated when unlock fails/cancels"
    assert closed is True


# ---------------------------------------------------------------------------
# 4. Hot-Reload KnowledgeWorker.embed & effective_embedding_api_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_worker_hot_reloads_embedder_after_apply_settings_reset(p6_3_db, tmp_path):
    now = datetime.now(UTC)
    async with p6_3_db() as session:
        session.add(Source(id="src-kw", name="KW", sag_source_config_id="cfg-kw"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="map-kw",
                source_id="src-kw",
                organization_id="t1",
                project_id="p-kw",
                state="CONFIRMED",
                mapping_version=1,
                confirmed_at=now,
                confirmed_by="u1",
                approval_ref="ref-kw",
            )
        )
        session.add(
            Document(
                id="doc-kw",
                source_id="src-kw",
                tenant_id="t1",
                project_id="p-kw",
                filename="kw.md",
                storage_path="/tmp/kw.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        ver = DocumentVersion(
            id="ver-kw",
            document_id="doc-kw",
            version_no=1,
            file_hash="fh-kw",
            valid_from=now,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            metadata_json={"security_partition_id": "p-kw"},
        )
        session.add(ver)
        await session.flush()
        txt = "Unique hot-reload quarterly cloud budget policy 2026-p6-1."
        session.add(
            CanonicalBlock(
                id="blk-kw",
                document_version_id="ver-kw",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Budget",
                source_anchor="p-0",
                normalized_text=txt,
                content_hash=hashlib.sha256(txt.encode()).hexdigest(),
            )
        )
        await session.commit()

    created_embedders: list[SimpleNamespace] = []

    class FakeEmbedder:
        def __init__(self, generation: int) -> None:
            self.generation = generation
            self.closed = False
            self.calls = 0

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            if self.closed:
                raise RuntimeError("Cannot send request on closed HTTP embedding client")
            self.calls += 1
            return [[0.5, 0.5, 0.5, 0.5] for _ in texts]

        async def close(self) -> None:
            self.closed = True

    class FakeEngineManager:
        def __init__(self) -> None:
            self._current: FakeEmbedder | None = None
            self._gen = 0

        async def get_sag_embedding(self, *_args, **_kwargs) -> FakeEmbedder:
            if self._current is None:
                self._gen += 1
                self._current = FakeEmbedder(self._gen)
                created_embedders.append(self._current)
            return self._current

        async def aclose_all(self) -> None:
            if self._current is not None:
                await self._current.close()
                self._current = None

    em = FakeEngineManager()
    base_settings = Settings(_env_file=None, embedding_api_key="old-key")
    runtime = KnowledgeRuntime(base_settings, p6_3_db, active_path=tmp_path)
    runtime._engine_manager = em

    worker = KnowledgeWorker(p6_3_db, base_settings, engine_manager=em)
    runtime.attach_knowledge_worker(worker)

    # Warm generation 1 embedder (simulating startup before hot-reload)
    emb1 = await em.get_sag_embedding()
    assert emb1.generation == 1 and emb1.closed is False

    # Hot-reload settings with reset_engines=True closes emb1
    new_settings = Settings(_env_file=None, embedding_api_key="new-key", embedding_model="Qwen/Qwen3-Embedding-0.6B")
    await runtime.apply_settings(new_settings, reset_engines=True)
    assert emb1.closed is True

    # Next FOUNDATION job must transparently obtain fresh open emb2 (not dead emb1!)
    await worker.discover()
    ran = await worker.run_one()
    assert ran is True
    assert len(created_embedders) == 2
    assert created_embedders[1].closed is False
    assert created_embedders[1].calls == 1

    # Also verify effective_embedding_api_key fallback reuses one managed EngineManager and closes on stop()
    fallback_managers: list[SimpleNamespace] = []

    class TrackingEM:
        def __init__(self, _s) -> None:
            self.closed = False
            self.embed = FakeEmbedder(99)
            fallback_managers.append(self)

        async def get_sag_embedding(self):
            return self.embed

        async def aclose_all(self):
            self.closed = True
            await self.embed.close()

    from sag_api.sag import engine_manager as em_mod

    orig_em_cls = em_mod.EngineManager
    em_mod.EngineManager = TrackingEM  # type: ignore[misc]
    try:
        # embedding_api_key is None, but llm_api_key supplies effective_embedding_api_key!
        shared_settings = Settings(
            _env_file=None,
            llm_provider="openai",
            llm_api_key="sk-shared-key",
            embedding_api_key=None,
        )
        assert shared_settings.embedding_api_key is None
        assert shared_settings.effective_embedding_api_key == "sk-shared-key"
        w2 = KnowledgeWorker(p6_3_db, shared_settings)
        resolved = await w2._resolve_embedder()
        resolved_again = await w2._resolve_embedder()
        assert resolved is resolved_again
        assert len(fallback_managers) == 1
        await w2.stop()
        assert fallback_managers[0].closed is True
    finally:
        em_mod.EngineManager = orig_em_cls  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. Live Integration Test SearchUnit Construction Has No qdrant_point_id Kwarg
# ---------------------------------------------------------------------------


def test_live_integration_search_unit_construction_has_no_invalid_qdrant_point_id():
    live_test_path = Path(__file__).with_name("test_live_integration.py")
    tree = ast.parse(live_test_path.read_text(encoding="utf-8"))
    search_unit_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SearchUnit"
    ]
    assert search_unit_calls, "Expected SearchUnit(...) construction in test_live_integration.py"
    for call in search_unit_calls:
        kw_names = {kw.arg for kw in call.keywords if kw.arg}
        assert "qdrant_point_id" not in kw_names, "SearchUnit ORM has no qdrant_point_id column"

    # Also verify direct instantiation with canonical fields succeeds
    unit = SearchUnit(
        id="su-live-check",
        document_version_id="ver-1",
        security_partition_id="public",
        block_from_id="blk-1",
        block_to_id="blk-1",
        char_start=0,
        char_end=10,
        text_snapshot="hello text",
        token_count=2,
        page_from=1,
        page_to=1,
        section_path="Section 1",
        content_hash="a" * 64,
    )
    assert unit.id == "su-live-check"
