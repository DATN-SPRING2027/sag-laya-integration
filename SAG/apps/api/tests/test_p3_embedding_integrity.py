"""TDD regression tests for Milestone P3 Packet 4: S3-01 & S3-02.

Covers:
- S3-01 Layer 1: `_reject_incompatible_embedding_change` checks Phase 2C SearchUnit & DocumentVersion,
  plus `embedding_base_url`.
- S3-01 Layer 2: Universal `model_signature` on `_SHARED_TEXT_EMBEDDING_CACHE` across all 7 callers.
- S3-01 Layer 3: Qdrant collection marker metadata (`_collection_meta`), `build_qdrant_payload` signature,
  retrieval mismatch fail-closed.
- S3-02: Default `vector_dim=1024` / `resolve_embedding_dimensions` registry, refuse `vector_dim <= 0` /
  empty / mismatched vectors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings, settings
from sag_api.core.errors import ConflictError, ServiceUnavailableError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
)
from sag_api.enums import DocumentStatus
from sag_api.services import settings_service


@pytest_asyncio.fixture
async def p3_emb_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _reset_shared_cache():
    from sag_api.services import search_index_service

    search_index_service._SHARED_TEXT_EMBEDDING_CACHE.clear()


# ---------------------------------------------------------------------------
# S3-01 Layer 1: Embedding Change Guard in settings_service.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_guard_blocks_model_and_baseurl_change_for_phase2c_docs(p3_emb_db, monkeypatch):
    monkeypatch.setattr(settings_service._settings, "embedding_model", "bge-large-en-v1.5")
    monkeypatch.setattr(settings_service._settings, "embedding_base_url", "https://api.302ai.cn/v1")
    monkeypatch.setattr(settings_service._settings, "embedding_dimensions", 1024)

    now = datetime.now(UTC)
    async with p3_emb_db() as session:
        session.add(Source(id="src-p4", name="P4 Source", sag_source_config_id="cfg-p4"))
        session.add(
            Document(
                id="doc-p4",
                source_id="src-p4",
                tenant_id="tenant-1",
                project_id="proj-1",
                filename="policy.md",
                storage_path="policy.md",
                sag_source_id=None,  # Phase 2C canonical doc has sag_source_id=None
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        session.add(
            DocumentVersion(
                id="ver-p4",
                document_id="doc-p4",
                version_no=1,
                file_hash="fh-p4",
                valid_from=now - timedelta(hours=1),
                valid_to=now + timedelta(days=1),
                status="READY",
                search_status="SEARCH_READY",
                search_ready_at=now,
            )
        )
        session.add(
            CanonicalBlock(
                id="blk-p4",
                document_version_id="ver-p4",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Policy",
                normalized_text="Policy text",
                content_hash=hashlib.sha256(b"Policy text").hexdigest(),
            )
        )
        session.add(
            SearchUnit(
                id="su-p4",
                document_version_id="ver-p4",
                block_from_id="blk-p4",
                block_to_id="blk-p4",
                security_partition_id="proj-1",
                content_hash=hashlib.sha256(b"Policy text").hexdigest(),
                token_count=3,
                page_from=1,
                page_to=1,
                section_path="Policy",
            )
        )
        await session.commit()

        # Same 1024-d, different model -> MUST raise ConflictError
        with pytest.raises(ConflictError):
            await settings_service._reject_incompatible_embedding_change(
                session,
                {"embedding_model": "Qwen/Qwen3-Embedding-4B", "embedding_dimensions": 1024},
            )

        # Same model & dim, different base_url -> MUST raise ConflictError
        with pytest.raises(ConflictError):
            await settings_service._reject_incompatible_embedding_change(
                session,
                {"embedding_base_url": "https://api.siliconflow.com/v1"},
            )

        # Unrelated LLM patch -> MUST succeed
        await settings_service._reject_incompatible_embedding_change(
            session,
            {"llm_model": "gpt-4o-mini"},
        )


# ---------------------------------------------------------------------------
# S3-01 Layer 2: Universal model_signature on _SHARED_TEXT_EMBEDDING_CACHE
# ---------------------------------------------------------------------------


def test_shared_embedding_cache_requires_non_empty_model_signature():
    from sag_api.services.search_index_service import (
        get_cached_text_embedding,
        put_cached_text_embedding,
    )

    _reset_shared_cache()
    with pytest.raises((TypeError, ValueError)):
        put_cached_text_embedding("hello", [0.1, 0.2], model_signature="")  # type: ignore[call-arg]
    with pytest.raises((TypeError, ValueError)):
        get_cached_text_embedding("hello", model_signature="   ")


@pytest.mark.asyncio
async def test_all_seven_cache_callers_isolate_same_dim_models(p3_emb_db, monkeypatch):
    from sag_api.core.token_usage import wrap_embedding_client_with_cache_and_metering
    from sag_api.jobs.knowledge import _embed_blocks
    from sag_api.sag.engine_manager import EngineManager
    from sag_api.services import dedup_and_temporal_service, search_index_service
    from sag_api.services.search_index_service import (
        build_embedding_model_signature,
        put_cached_text_embedding,
    )
    from sag_api.services.search_unit_retrieval_service import _query_vector, _SearchGroup
    from sag_api.services.session_context.substrate import embed_texts_with_provenance

    _reset_shared_cache()

    vec_a = [0.1] * 4
    vec_b = [0.9] * 4

    class ModelAEmbedder:
        model = "bge-large-en-v1.5"
        base_url = "https://provider-a.invalid/v1"
        dimensions = 4

    class ModelBEmbedder:
        model = "Qwen/Qwen3-Embedding-4B"
        base_url = "https://provider-b.invalid/v1"
        dimensions = 4

        def __init__(self):
            self.calls: list[list[str]] = []

        async def generate(self, text: str) -> list[float]:
            self.calls.append([text])
            return list(vec_b)

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [list(vec_b) for _ in texts]

    sig_a = build_embedding_model_signature(ModelAEmbedder())
    sig_b = build_embedding_model_signature(ModelBEmbedder())
    assert sig_a != sig_b

    probe_text = "Section\n\nShared embedding cache isolation test"
    put_cached_text_embedding(probe_text, vec_a, model_signature=sig_a)

    # 1. token_usage.wrap_embedding_client_with_cache_and_metering
    emb1 = ModelBEmbedder()
    wrap_embedding_client_with_cache_and_metering(emb1, default_model=emb1.model)
    out1 = await emb1.batch_generate([probe_text])
    assert out1 == [vec_b]
    assert emb1.calls == [[probe_text]]

    # 2. EngineManager._configure_embedding_client (clear cache for sig_b first)
    _reset_shared_cache()
    put_cached_text_embedding(probe_text, vec_a, model_signature=sig_a)
    emb2 = ModelBEmbedder()
    mgr_settings = Settings(
        embedding_model=emb2.model,
        embedding_base_url=emb2.base_url,
        embedding_dimensions=4,
    )
    mgr = EngineManager(mgr_settings)
    mgr._configure_embedding_client(emb2)
    out2 = await emb2.generate(probe_text)
    assert out2 == vec_b
    assert emb2.calls == [[probe_text]]

    # 3. search_index_service.precompute_canonical_block_embeddings
    _reset_shared_cache()
    put_cached_text_embedding(probe_text, vec_a, model_signature=sig_a)
    emb3 = ModelBEmbedder()
    blk = CanonicalBlock(
        id="blk-cache-1",
        document_version_id="ver-cache-1",
        ordinal=0,
        block_type="paragraph",
        page_from=1,
        page_to=1,
        section_path="Section",
        normalized_text="Shared embedding cache isolation test",
        content_hash=hashlib.sha256(b"Shared embedding cache isolation test").hexdigest(),
    )
    bvecs, _ = await search_index_service.precompute_canonical_block_embeddings(
        [blk],
        embedder=emb3,
        project_id="proj-1",
        tenant_id="tenant-1",
    )
    assert bvecs["blk-cache-1"] == vec_b
    assert emb3.calls == [[probe_text]]

    # 4. search_index_service.index_search_units_to_qdrant
    _reset_shared_cache()
    put_cached_text_embedding(probe_text, vec_a, model_signature=sig_a)
    emb4 = ModelBEmbedder()
    upserted_vectors: list[list[float]] = []

    def q_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/points"):
            body = json.loads(request.content.decode("utf-8"))
            for pt in body.get("points", []):
                if not pt.get("payload", {}).get("_collection_meta"):
                    upserted_vectors.append(pt["vector"]["content_vector"])
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if request.method == "GET" and "/points/" in request.url.path:
            return httpx.Response(404, json={"status": {"error": "not found"}})
        if request.method == "GET" and "/collections/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 4}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={"result": {}})

    ver = DocumentVersion(
        id="ver-cache-1",
        document_id="doc-cache-1",
        version_no=1,
        file_hash="fh-c",
        valid_from=datetime.now(UTC),
    )
    su = SearchUnit(
        id="su-cache-1",
        document_version_id="ver-cache-1",
        block_from_id="blk-cache-1",
        block_to_id="blk-cache-1",
        security_partition_id="proj-1",
        content_hash=blk.content_hash,
        token_count=5,
        section_path="Section",
    )
    su._text_content = "Shared embedding cache isolation test"
    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(q_handler)
    ) as q_client:
        await search_index_service.index_search_units_to_qdrant(
            q_client,
            project_id="proj-cache",
            units=[su],
            version=ver,
            embedder=emb4,
            blocks_by_id={"blk-cache-1": blk},
        )
    assert upserted_vectors == [vec_b]
    assert emb4.calls == [[probe_text]]

    # 5. search_unit_retrieval_service._query_vector
    _reset_shared_cache()
    from sag_api.services.search_unit_retrieval_service import _QWEN3_QUERY_INSTRUCT

    q_text = "Shared embedding cache isolation test"
    instructed_q_text = f"{_QWEN3_QUERY_INSTRUCT}{q_text}"
    put_cached_text_embedding(instructed_q_text, vec_a, model_signature=sig_a)
    emb5 = ModelBEmbedder()
    monkeypatch.setattr(settings, "embedding_model", emb5.model)
    monkeypatch.setattr(settings, "embedding_base_url", emb5.base_url)
    monkeypatch.setattr(settings, "embedding_dimensions", 4)
    fake_mgr = SimpleNamespace(
        _settings=mgr_settings,
        get_sag_embedding=AsyncMock(return_value=emb5),
    )
    grp = _SearchGroup(
        project_id="proj-1",
        tenant_id="tenant-1",
        partition_id="part-1",
        source=SimpleNamespace(id="src-1", sag_source_config_id="cfg-1"),
        versions={"ver-1"},
    )
    q_vec = await _query_vector(fake_mgr, grp, q_text)
    assert q_vec == vec_b
    assert emb5.calls == [[instructed_q_text]]

    # 6. dedup_and_temporal_service.run_dedup_and_temporal_stage
    _reset_shared_cache()
    put_cached_text_embedding("Shared embedding cache isolation test", vec_a, model_signature=sig_a)
    emb6 = ModelBEmbedder()
    async with p3_emb_db() as session:
        session.add(Source(id="src-d", name="D", sag_source_config_id="cfg-d"))
        session.add(
            Document(
                id="doc-d",
                source_id="src-d",
                tenant_id="tenant-1",
                project_id="proj-d",
                filename="d.md",
                storage_path="d.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        now = datetime.now(UTC)
        ver_prior = DocumentVersion(
            id="ver-d-1",
            document_id="doc-d",
            version_no=1,
            file_hash="fh-d-1",
            valid_from=now - timedelta(hours=2),
            valid_to=now + timedelta(days=1),
            status="READY",
            search_status="SEARCH_READY",
        )
        ver_curr = DocumentVersion(
            id="ver-d-2",
            document_id="doc-d",
            version_no=2,
            file_hash="fh-d-2",
            valid_from=now,
            valid_to=now + timedelta(days=1),
        )
        session.add_all([ver_prior, ver_curr])
        session.add(
            CanonicalBlock(
                id="blk-d-1",
                document_version_id="ver-d-1",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Section",
                normalized_text="Shared embedding cache isolation test",
                content_hash=hashlib.sha256(b"Shared embedding cache isolation test").hexdigest(),
            )
        )
        session.add(
            CanonicalBlock(
                id="blk-d-2",
                document_version_id="ver-d-2",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Section",
                normalized_text="Shared embedding cache isolation test",
                content_hash=hashlib.sha256(b"Shared embedding cache isolation test").hexdigest(),
            )
        )
        await session.commit()
        await dedup_and_temporal_service.run_dedup_and_temporal_stage(
            session,
            document_version=ver_curr,
            document_id="doc-d",
            project_id="proj-d",
            embedder=emb6,
        )
    assert emb6.calls

    # 7. jobs/knowledge.py::_embed_blocks
    _reset_shared_cache()
    put_cached_text_embedding(q_text, vec_a, model_signature=sig_a)
    emb7 = ModelBEmbedder()
    kb_vecs = await _embed_blocks(emb7, [("blk-k1", q_text)])
    assert kb_vecs["blk-k1"] == vec_b
    assert emb7.calls == [[q_text]]

    # 8. session_context.substrate.embed_texts_with_provenance (same model, different base_url)
    _reset_shared_cache()
    same_model_other_url = SimpleNamespace(
        model=emb7.model,
        base_url="https://other-provider.invalid/v1",
        dimensions=4,
    )
    sig_other_url = build_embedding_model_signature(same_model_other_url)
    put_cached_text_embedding(q_text, [1.0, 0.0, 0.0, 0.0], model_signature=sig_other_url)
    emb8 = ModelBEmbedder()
    sess_vecs, _ = await embed_texts_with_provenance([q_text], embedder=emb8)
    assert emb8.calls == [[q_text]]
    assert len(sess_vecs) == 1


# ---------------------------------------------------------------------------
# S3-01 Layer 3: Qdrant Collection Marker & Retrieval Signature Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_collection_metadata_marker_rejects_same_dim_model_swap():
    from sag_api.services.search_index_service import (
        build_embedding_model_signature,
        collection_meta_point_id,
        ensure_qdrant_collection_and_indexes,
    )

    stored_points: dict[str, dict] = {}
    collection_name = "search_units_proj_meta"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/collections/{collection_name}":
            if not stored_points and "created" not in stored_points:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 1024}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        },
                        "payload_schema": {},
                    }
                },
            )
        if request.method == "PUT" and request.url.path == f"/collections/{collection_name}":
            stored_points["created"] = {}
            return httpx.Response(200, json={"result": True})
        if request.method == "GET" and f"/collections/{collection_name}/points/" in request.url.path:
            pid = request.url.path.rsplit("/", 1)[-1]
            pt = stored_points.get(pid)
            if pt is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(200, json={"result": pt})
        if request.method == "PUT" and request.url.path == f"/collections/{collection_name}/points":
            body = json.loads(request.content.decode("utf-8"))
            for pt in body.get("points", []):
                stored_points[pt["id"]] = pt
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    sig_a = build_embedding_model_signature(
        model="bge-large-en-v1.5",
        base_url="https://api.302ai.cn/v1",
        dimensions=1024,
    )
    sig_b = build_embedding_model_signature(
        model="Qwen/Qwen3-Embedding-4B",
        base_url="https://api.302ai.cn/v1",
        dimensions=1024,
    )

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    ) as client_a:
        await ensure_qdrant_collection_and_indexes(
            client_a,
            collection_name,
            vector_dim=1024,
            embedding_signature=sig_a,
            embedding_model="bge-large-en-v1.5",
            embedding_base_url="https://api.302ai.cn/v1",
        )

    meta_id = collection_meta_point_id(collection_name)
    assert meta_id in stored_points
    meta_payload = stored_points[meta_id]["payload"]
    assert meta_payload["_collection_meta"] is True
    assert meta_payload["payload_only"] is True
    assert meta_payload["embedding_signature"] == sig_a
    assert "document_version_id" not in meta_payload
    assert len(stored_points[meta_id]["vector"]["content_vector"]) == 1024

    # Second client with same dim=1024 but different model signature -> MUST raise ValueError
    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    ) as client_b:
        with pytest.raises(ValueError, match="mismatch"):
            await ensure_qdrant_collection_and_indexes(
                client_b,
                collection_name,
                vector_dim=1024,
                embedding_signature=sig_b,
                embedding_model="Qwen/Qwen3-Embedding-4B",
                embedding_base_url="https://api.302ai.cn/v1",
            )


@pytest.mark.asyncio
async def test_retrieval_fails_closed_on_hit_embedding_signature_mismatch(monkeypatch):
    from sag_api.services import search_unit_retrieval_service as retrieval
    from sag_api.services.search_index_service import build_embedding_model_signature

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    content = "Kubernetes deployment guide."
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    async with factory() as session:
        session.add(Source(id="source-a", name="Source A", sag_source_config_id="cfg-a"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id="source-a",
                organization_id="tenant-a",
                project_id="project-a",
                state="CONFIRMED",
                confirmed_at=now,
                confirmed_by="user",
                approval_ref="test",
            )
        )
        session.add(
            Document(
                id="doc-a",
                source_id="source-a",
                tenant_id="tenant-a",
                project_id="project-a",
                filename="k8s.md",
                storage_path="k8s.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        session.add(
            DocumentVersion(
                id="version-a",
                document_id="doc-a",
                version_no=1,
                file_hash="fh-a",
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={"security_partition_id": "partition-a"},
            )
        )
        session.add(
            IngestionRun(
                id="run-a",
                document_version_id="version-a",
                project_id="project-a",
                tenant_id="tenant-a",
                idempotency_key="idem-a",
                payload_hash="ph-a",
                status="COMPLETED",
                started_at=now,
            )
        )
        checksum = "a" * 64
        session.add(
            StageRun(
                id="sr-a",
                run_id="run-a",
                stage="INDEX_SEARCH",
                status="SUCCESS",
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": "search_units_project-a",
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        session.add(
            CanonicalBlock(
                id="blk-a",
                document_version_id="version-a",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="K8s",
                normalized_text=content,
                content_hash=digest,
            )
        )
        session.add(
            SearchUnit(
                id="unit-a",
                document_version_id="version-a",
                block_from_id="blk-a",
                block_to_id="blk-a",
                security_partition_id="partition-a",
                content_hash=digest,
                token_count=4,
                page_from=1,
                page_to=1,
                section_path="K8s",
            )
        )
        await session.commit()

    @asynccontextmanager
    async def session_ctx():
        async with factory() as sess:
            yield sess

    stale_sig = build_embedding_model_signature(
        model="old-embedding-model",
        base_url="https://api.302ai.cn/v1",
        dimensions=1024,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/query/batch"):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "points": [
                                {
                                    "id": "pt-1",
                                    "score": 0.9,
                                    "payload": {
                                        "search_unit_id": "unit-a",
                                        "document_version_id": "version-a",
                                        "project_id": "project-a",
                                        "tenant_id": "tenant-a",
                                        "security_partition_id": "partition-a",
                                        "content_hash": digest,
                                        "content": content,
                                        "embedding_model_signature": stale_sig,
                                    },
                                }
                            ]
                        },
                        {"points": []},
                    ]
                },
            )
        raise AssertionError(f"Unexpected Qdrant call: {request.method} {request.url.path}")

    monkeypatch.setattr(retrieval, "SessionLocal", session_ctx)
    monkeypatch.setattr(retrieval, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr(settings, "embedding_model", "Qwen/Qwen3-Embedding-4B")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)

    principal = VerifiedPrincipal(
        subject="u1",
        organization_id="tenant-a",
        allowed_project_ids=frozenset({"project-a"}),
        issuer="test",
        key_id="k1",
        token_id="t1",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-a",
        allowed_partition_ids=frozenset({"partition-a"}),
    )
    source = SimpleNamespace(id="source-a", name="Source A", sag_source_config_id="cfg-a")

    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            mgr = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            with pytest.raises(ServiceUnavailableError):
                await retrieval._retrieve_search_unit_sections(
                    mgr,
                    [source],
                    "kubernetes deployment",
                    principal=principal,
                    top_k=5,
                    candidate_limit=8,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# S3-02: dim=1024 registry & refuse unknown/invalid vector_dim <= 0
# ---------------------------------------------------------------------------


def test_resolve_embedding_dimensions_and_config_builder():
    from sag_api.sag.config_builder import build_engine_config
    from sag_api.services.search_index_service import resolve_embedding_dimensions

    assert resolve_embedding_dimensions("bge-large-en-v1.5", None) == 1024
    assert resolve_embedding_dimensions("BAAI/bge-m3", None) == 1024
    assert resolve_embedding_dimensions("Qwen/Qwen3-Embedding-4B", None) == 1024
    assert resolve_embedding_dimensions("text-embedding-3-small", None) == 1536
    assert resolve_embedding_dimensions("text-embedding-3-large", None) == 3072
    assert resolve_embedding_dimensions("custom-unknown-model", None) == 1024
    assert resolve_embedding_dimensions("custom-unknown-model", 768) == 768

    with pytest.raises(ValueError):
        resolve_embedding_dimensions("bge-large-en-v1.5", 0)
    with pytest.raises(ValueError):
        resolve_embedding_dimensions("bge-large-en-v1.5", -128)

    cfg_default = build_engine_config(Settings(embedding_model="bge-large-en-v1.5", embedding_dimensions=None))
    assert cfg_default.embedding.dimensions == 1024

    cfg_small = build_engine_config(Settings(embedding_model="text-embedding-3-small", embedding_dimensions=None))
    assert cfg_small.embedding.dimensions == 1536


@pytest.mark.asyncio
async def test_qdrant_refuses_unknown_or_invalid_vector_dimensions():
    from sag_api.services.search_index_service import (
        ensure_qdrant_collection_and_indexes,
        index_search_units_to_qdrant,
    )
    from sag_api.services.session_context.persistence import ensure_session_qdrant_collection

    called = False

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"result": {}})

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(fail_if_called)
    ) as client:
        for bad_dim in (None, 0, -1, -1024):
            with pytest.raises(ValueError):
                await ensure_qdrant_collection_and_indexes(client, "search_units_bad", vector_dim=bad_dim)

        with pytest.raises(ValueError):
            await ensure_session_qdrant_collection(client, project_id="bad", vector_dim=0)

        # Empty vector returned from embedder must fail closed, never fallback to 1536
        class EmptyEmbedder:
            model = "bge-large-en-v1.5"

            async def batch_generate(self, texts: list[str]) -> list[list[float]]:
                return [[] for _ in texts]

        ver = DocumentVersion(
            id="ver-bad",
            document_id="doc-bad",
            version_no=1,
            file_hash="fh-bad",
            valid_from=datetime.now(UTC),
        )
        su = SearchUnit(
            id="su-bad",
            document_version_id="ver-bad",
            block_from_id="blk-bad",
            block_to_id="blk-bad",
            security_partition_id="proj-bad",
            content_hash="ch-bad",
            token_count=2,
            section_path="S",
        )
        su._text_content = "hello"
        with pytest.raises(ValueError):
            await index_search_units_to_qdrant(
                client,
                project_id="proj-bad",
                units=[su],
                version=ver,
                embedder=EmptyEmbedder(),
            )
    assert called is False


@pytest.mark.asyncio
async def test_retrieval_unlisted_custom_512d_model_with_none_dimensions_matches_index(
    p3_emb_db, monkeypatch
):
    from sag_api.services import search_index_service
    from sag_api.services import search_unit_retrieval_service as retrieval

    _reset_shared_cache()

    custom_model = "custom/unlisted-embedder-v1"
    custom_base_url = "https://custom-emb.invalid/v1"
    vec_512 = [0.5] * 512

    class Custom512DEmbedder:
        model = custom_model
        base_url = custom_base_url
        dimensions = None

        async def generate(self, _text: str) -> list[float]:
            return list(vec_512)

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            return [list(vec_512) for _ in texts]

    monkeypatch.setattr(settings, "embedding_model", custom_model)
    monkeypatch.setattr(settings, "embedding_base_url", custom_base_url)
    monkeypatch.setattr(settings, "embedding_dimensions", None)

    now = datetime.now(UTC)
    content = "Custom 512-d unlisted embedding model integrity content."
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    collection_name = "search_units_proj-512"

    stored_points: dict[str, dict] = {}
    collection_created = False

    def q_handler(request: httpx.Request) -> httpx.Response:
        nonlocal collection_created
        if request.method == "GET" and request.url.path == f"/collections/{collection_name}":
            if not collection_created:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 512}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        },
                        "payload_schema": {},
                    }
                },
            )
        if request.method == "PUT" and request.url.path == f"/collections/{collection_name}":
            collection_created = True
            return httpx.Response(200, json={"result": True})
        if request.method == "GET" and f"/collections/{collection_name}/points/" in request.url.path:
            pid = request.url.path.rsplit("/", 1)[-1]
            pt = stored_points.get(pid)
            if pt is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(200, json={"result": pt})
        if request.method == "PUT" and request.url.path == f"/collections/{collection_name}/points":
            body = json.loads(request.content.decode("utf-8"))
            for pt in body.get("points", []):
                stored_points[pt["id"]] = pt
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if request.method == "POST" and request.url.path == f"/collections/{collection_name}/points/query/batch":
            data_points = [
                {
                    "id": pid,
                    "score": 0.92,
                    "payload": pt["payload"],
                }
                for pid, pt in stored_points.items()
                if not pt.get("payload", {}).get("_collection_meta")
            ]
            return httpx.Response(200, json={"result": [{"points": data_points}, {"points": []}]})
        return httpx.Response(200, json={"result": {}})

    async with p3_emb_db() as session:
        session.add(Source(id="src-512", name="Source 512", sag_source_config_id="cfg-512"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id="src-512",
                organization_id="tenant-512",
                project_id="proj-512",
                state="CONFIRMED",
                confirmed_at=now,
                confirmed_by="user",
                approval_ref="test",
            )
        )
        session.add(
            Document(
                id="doc-512",
                source_id="src-512",
                tenant_id="tenant-512",
                project_id="proj-512",
                filename="custom512.md",
                storage_path="custom512.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        session.add(
            DocumentVersion(
                id="ver-512",
                document_id="doc-512",
                version_no=1,
                file_hash="fh-512",
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={"security_partition_id": "part-512"},
            )
        )
        session.add(
            IngestionRun(
                id="run-512",
                document_version_id="ver-512",
                project_id="proj-512",
                tenant_id="tenant-512",
                idempotency_key="idem-512",
                payload_hash="ph-512",
                status="COMPLETED",
                started_at=now,
            )
        )
        checksum = "b" * 64
        session.add(
            StageRun(
                id="sr-512",
                run_id="run-512",
                stage="INDEX_SEARCH",
                status="SUCCESS",
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": collection_name,
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        blk = CanonicalBlock(
            id="blk-512",
            document_version_id="ver-512",
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Custom",
            normalized_text=content,
            content_hash=digest,
            source_anchor="anchor-512",
        )
        su = SearchUnit(
            id="unit-512",
            document_version_id="ver-512",
            block_from_id="blk-512",
            block_to_id="blk-512",
            security_partition_id="part-512",
            content_hash=digest,
            token_count=8,
            page_from=1,
            page_to=1,
            section_path="Custom",
        )
        su._text_content = content
        session.add_all([blk, su])
        await session.commit()

        embedder = Custom512DEmbedder()
        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(q_handler)
        ) as client:
            ver = await session.get(DocumentVersion, "ver-512")
            await search_index_service.index_search_units_to_qdrant(
                client,
                project_id="proj-512",
                units=[su],
                version=ver,
                tenant_id="tenant-512",
                embedder=embedder,
                blocks_by_id={"blk-512": blk},
            )
            unit_pt_id = search_index_service.generate_search_unit_point_id(collection_name, "unit-512")
            assert stored_points[unit_pt_id]["payload"]["embedding_model_signature"].endswith("|512")

            @asynccontextmanager
            async def session_ctx():
                async with p3_emb_db() as sess:
                    yield sess

            monkeypatch.setattr(retrieval, "SessionLocal", session_ctx)
            monkeypatch.setattr(retrieval, "capture_routing_decisions", AsyncMock(return_value={}))
            mgr = SimpleNamespace(
                _settings=settings,
                get_search_unit_qdrant_client=AsyncMock(return_value=client),
                get_sag_embedding=AsyncMock(return_value=embedder),
            )
            principal = VerifiedPrincipal(
                subject="u1",
                organization_id="tenant-512",
                allowed_project_ids=frozenset({"proj-512"}),
                issuer="test",
                key_id="k1",
                token_id="t1",
                issued_at=1,
                expires_at=2,
                tenant_id="tenant-512",
                allowed_partition_ids=frozenset({"part-512"}),
            )
            source = SimpleNamespace(id="src-512", name="Source 512", sag_source_config_id="cfg-512")
            outcome = await retrieval._retrieve_search_unit_sections(
                mgr,
                [source],
                "custom 512-d unlisted embedding",
                principal=principal,
                top_k=5,
                candidate_limit=8,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert len(outcome.sections) == 1
            assert outcome.sections[0].chunk_id == "unit-512"
