"""TDD regression tests for Milestone P3 Packet 6: S3-05, S3-07, S3-08.

Covers:
- S3-05: `event_vectors_wide` in `octx_vector_protocol.ROLE_STORAGE`, 404 vs non-404 handling in
  `write_existing_vector_payload`, SiliconFlow `BAAI/bge-m3` `schema_dimensions=1024`,
  `configured_embedding_identity`, and `octx_vector_identity` persistence in `Source.config`.
- S3-07: `QdrantVectorStore.upsert` `payload_only: True` on zero vectors, `must_not` filter on
  vector `query`, and metric-aware score normalization (`Cosine`, `Dot`, `Euclid`).
- S3-08: `chunk_heading_vectors.py` deletion and paginated/404-safe `octx_gc_service`.
"""

from __future__ import annotations

import importlib.util
import json
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag.core.adapters.models import Filter, VectorQuery, VectorRecord
from zleap.sag.exceptions import StorageError

from sag_api.core.config import Settings
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, IngestionRun, Job, Source
from sag_api.enums import ConnectorKind, DocumentStatus, JobStatus, JobType, SourceStatus, SourceType
from sag_api.sag import octx_vector_protocol
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.qdrant_store import QdrantVectorConfig, QdrantVectorStore
from sag_api.services import octx_gc_service


@pytest_asyncio.fixture
async def p3_octx_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# S3-05: OCTX Vector Protocol, BGE-M3 schema_dimensions, Source.config identity
# ---------------------------------------------------------------------------


def test_octx_role_storage_uses_event_vectors_wide_and_bge_m3_identity():
    assert octx_vector_protocol.ROLE_STORAGE["event.title"] == ("event_vectors_wide", "title_vector")
    assert octx_vector_protocol.ROLE_STORAGE["event.content"] == ("event_vectors_wide", "content_vector")

    s = Settings(
        embedding_model="BAAI/bge-m3",
        embedding_base_url="https://api.siliconflow.com/v1",
        embedding_dimensions=None,
    )
    cfg_id = octx_vector_protocol.configured_embedding_identity(s)
    assert cfg_id is not None
    assert cfg_id["model"] == "BAAI/bge-m3"
    assert cfg_id["dimensions"] == 1024

    class FakeEmb:
        model = "BAAI/bge-m3"
        base_url = "https://api.siliconflow.com/v1"
        dimensions = 1024
        max_tokens = 8192

        async def generate(self, text: str):
            return [0.1] * 1024

        async def batch_generate(self, texts: list[str]):
            return [[0.1] * 1024 for _ in texts]

    emb = FakeEmb()
    EngineManager(s)._configure_embedding_client(emb)
    assert emb.dimensions is None
    assert getattr(emb, "schema_dimensions", None) == 1024

    emb_id = octx_vector_protocol.embedding_identity(emb)
    assert emb_id is not None
    assert emb_id["dimensions"] == 1024
    assert emb_id["model"] == "BAAI/bge-m3"


@pytest.mark.asyncio
async def test_write_existing_vector_payload_skips_404_but_raises_on_503(tmp_path):
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "relations").mkdir(parents=True)
    (ws / "data/chunks.jsonl").write_text(
        json.dumps({"id": "c1", "heading": "H", "text": "Body"}) + "\n",
        encoding="utf-8",
    )
    (ws / "data/events.jsonl").write_text("", encoding="utf-8")
    (ws / "data/entities.jsonl").write_text("", encoding="utf-8")
    (ws / "relations/event-entities.jsonl").write_text("", encoding="utf-8")

    source_ids = {
        "chunk.heading": {"c1": "local-c1"},
        "chunk.content": {"c1": "local-c1"},
    }
    emb = SimpleNamespace(model="BAAI/bge-m3", base_url="https://api.siliconflow.com/v1", dimensions=4)

    class MissingStore:
        async def fetch_vector_fields(self, index, ids, fields):
            raise StorageError("Qdrant request failed (404): Not found: Collection `source_chunks` doesn't exist")

    ws_404 = tmp_path / "ws404"
    (ws_404 / "data").mkdir(parents=True)
    (ws_404 / "relations").mkdir(parents=True)
    for fn in ("chunks.jsonl", "events.jsonl", "entities.jsonl"):
        (ws_404 / "data" / fn).write_text((ws / "data" / fn).read_text(encoding="utf-8"), encoding="utf-8")
    (ws_404 / "relations/event-entities.jsonl").write_text("", encoding="utf-8")

    written = await octx_vector_protocol.write_existing_vector_payload(
        ws_404, MissingStore(), emb, source_ids=source_ids
    )
    assert written == set()

    class FailingStore:
        async def fetch_vector_fields(self, index, ids, fields):
            raise StorageError("Qdrant request failed (503): service unavailable")

    with pytest.raises(StorageError, match="503"):
        await octx_vector_protocol.write_existing_vector_payload(
            ws, FailingStore(), emb, source_ids=source_ids
        )


@pytest.mark.asyncio
async def test_legacy_process_document_persists_octx_vector_identity_in_source_config(
    p3_octx_db, tmp_path, monkeypatch
):
    from sag_api.jobs import tasks
    from sag_api.sag.dto import ProcessOutcome
    from sag_api.services import search_index_service

    async def fake_index(session, *, document_version, **_kwargs):
        document_version.search_status = "SEARCH_READY"
        document_version.search_ready_at = None
        return [SimpleNamespace(id="su-leg")]

    monkeypatch.setattr(search_index_service, "run_search_indexing_stage", fake_index)
    monkeypatch.setattr(tasks, "SessionLocal", p3_octx_db)
    monkeypatch.setattr(tasks.settings, "document_event_extraction_enabled", True)
    monkeypatch.setattr(tasks.settings, "embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(tasks.settings, "embedding_dimensions", None)

    doc_file = tmp_path / "legacy_octx.md"
    doc_file.write_text("# Legacy OCTX\n\nContent.", encoding="utf-8")

    async with p3_octx_db() as session:
        session.add(
            Source(
                id="src-leg",
                name="Legacy Src",
                source_type=SourceType.DOCUMENT,
                connector_kind=ConnectorKind.FILE_UPLOAD,
                sag_source_config_id="cfg-leg",
                config={},
                status=SourceStatus.ACTIVE,
            )
        )
        session.add(
            Document(
                id="doc-leg",
                source_id="src-leg",
                tenant_id="t1",
                project_id=None,
                filename="legacy_octx.md",
                storage_path=str(doc_file),
                status=DocumentStatus.PENDING,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-leg",
                document_id="doc-leg",
                version_no=1,
                file_hash="fh-leg",
                status="RECEIVED",
                metadata_json={"security_partition_id": "default"},
            )
        )
        session.add(
            IngestionRun(
                id="run-leg",
                tenant_id="t1",
                project_id="default",
                document_version_id="ver-leg",
                idempotency_key="idem-leg",
                payload_hash="fh-leg",
                status="QUEUED",
            )
        )
        job = Job(
            id="job-leg",
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id="src-leg",
            document_id="doc-leg",
            payload={"run_id": "run-leg"},
        )
        session.add(job)
        await session.commit()

        async def fake_process_doc(*_args, **_kwargs):
            return ProcessOutcome(source_id="sag-src-1", chunk_count=2, event_count=1)

        emb = SimpleNamespace(
            model="BAAI/bge-m3",
            base_url="https://api.siliconflow.com/v1",
            dimensions=None,
            schema_dimensions=1024,
        )
        mgr = SimpleNamespace(
            process_document=AsyncMock(side_effect=fake_process_doc),
            get_sag_embedding=AsyncMock(return_value=emb),
        )
        await tasks._process_document_unlocked(session, job, engine_manager=mgr)

        src = await session.get(Source, "src-leg")
        assert src is not None
        ident = (src.config or {}).get("octx_vector_identity")
        assert isinstance(ident, dict)
        assert ident["model"] == "BAAI/bge-m3"
        assert ident["dimensions"] == 1024


# ---------------------------------------------------------------------------
# S3-07: QdrantVectorStore payload_only & metric-aware score normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_store_tags_payload_only_filters_on_query_and_normalizes_by_metric():
    requests_seen: list[tuple[str, str, dict | None]] = []
    active_distance = "Cosine"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        requests_seen.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/collections/test_col":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {
                                    "content_vector": {"size": 3, "distance": active_distance},
                                }
                            }
                        },
                        "payload_schema": {"data_source_id": {"data_type": "keyword"}},
                    }
                },
            )
        if request.method == "PUT" and request.url.path.endswith("/points"):
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if request.method == "POST" and request.url.path.endswith("/points/query"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {"id": "p1", "score": 2.0, "payload": {"_sag_id": "rec-1"}},
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"result": {}})

    store = QdrantVectorStore(QdrantVectorConfig(url="http://qdrant.local"))
    store._client = httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    )
    try:
        await store.upsert(
            "test_col",
            [
                VectorRecord(id="r-empty", payload={"k": "v"}, vectors={}),
                VectorRecord(id="r-real", payload={"k": "v2"}, vectors={"content_vector": [1.0, 0.0, 0.0]}),
            ],
        )
        put_calls = [body for m, p, body in requests_seen if m == "PUT" and p.endswith("/points")]
        assert len(put_calls) == 1
        pts_by_sag = {pt["payload"]["_sag_id"]: pt for pt in put_calls[0]["points"]}
        assert pts_by_sag["r-empty"]["payload"]["payload_only"] is True
        assert pts_by_sag["r-empty"]["vector"]["content_vector"] == [0.0, 0.0, 0.0]
        assert "payload_only" not in pts_by_sag["r-real"]["payload"]

        # Query under Dot metric -> sigmoid(2.0), NOT (2.0 + 1.0) / 2.0 = 1.5
        active_distance = "Dot"
        dot_hits = await store.query(
            "test_col",
            VectorQuery(
                vector=[1.0, 0.0, 0.0],
                vector_field="content_vector",
                filters=Filter.eq("data_source_id", "s1"),
                limit=5,
            ),
        )
        assert math.isclose(dot_hits[0].score, 1.0 / (1.0 + math.exp(-2.0)), rel_tol=1e-6)
        query_body = [body for m, p, body in requests_seen if m == "POST" and p.endswith("/points/query")][-1]
        assert {"key": "payload_only", "match": {"value": True}} in query_body["filter"]["must_not"]

        # Query under Euclid metric on a fresh store -> 1 / (1 + 2.0) = 1/3
        active_distance = "Euclid"
        store_euclid = QdrantVectorStore(QdrantVectorConfig(url="http://qdrant.local"))
        store_euclid._client = store._client
        euclid_hits = await store_euclid.query(
            "test_col",
            VectorQuery(vector=[1.0, 0.0, 0.0], vector_field="content_vector", limit=5),
        )
        assert math.isclose(euclid_hits[0].score, 1.0 / 3.0, rel_tol=1e-6)
        query_body2 = [body for m, p, body in requests_seen if m == "POST" and p.endswith("/points/query")][-1]
        assert {"key": "payload_only", "match": {"value": True}} in query_body2["filter"]["must_not"]
    finally:
        await store._client.aclose()


# ---------------------------------------------------------------------------
# S3-08: dead chunk_heading_vectors deleted & octx_gc_service pagination / 404
# ---------------------------------------------------------------------------


def test_dead_chunk_heading_vectors_module_removed():
    assert importlib.util.find_spec("sag_api.sag.chunk_heading_vectors") is None


@pytest.mark.asyncio
async def test_octx_gc_delete_vector_partition_paginates_and_ignores_404():
    # 1. Generic VectorStore adapter with >1000 points paginates across batches
    remaining = [f"id-{i}" for i in range(1500)]
    query_limits: list[int] = []

    class PaginatedStore:
        async def query(self, index: str, q: VectorQuery):
            query_limits.append(q.limit)
            return [SimpleNamespace(id=rid) for rid in remaining[: q.limit]]

        async def delete(self, index: str, ids: tuple[str, ...]):
            id_set = set(ids)
            remaining[:] = [rid for rid in remaining if rid not in id_set]

    await octx_gc_service._delete_vector_partition(PaginatedStore(), "source_chunks", "src-1")
    assert remaining == []
    assert len(query_limits) == 2
    assert all(lim == 1000 for lim in query_limits)

    # 2. Missing collection (404) is cleanly ignored
    class MissingQdrantAdapter:
        async def query(self, index: str, q: VectorQuery):
            raise StorageError("Qdrant request failed (404): Not found")

    await octx_gc_service._delete_vector_partition(MissingQdrantAdapter(), "event_vectors_wide", "src-1")

    # 3. QdrantVectorStore fast-path uses filter delete & ignores 404
    delete_filters: list[dict] = []

    def q_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/collections/event_vectors_wide/points/delete":
            return httpx.Response(404, text="Collection `event_vectors_wide` doesn't exist")
        if request.url.path.endswith("/points/delete"):
            delete_filters.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    qstore = QdrantVectorStore(QdrantVectorConfig(url="http://qdrant.local"))
    qstore._client = httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(q_handler)
    )
    try:
        await octx_gc_service._delete_vector_partition(qstore, "source_chunks", "src-q1")
        await octx_gc_service._delete_vector_partition(qstore, "event_vectors_wide", "src-q1")
    finally:
        await qstore._client.aclose()
    assert delete_filters == [
        {"filter": {"must": [{"key": "data_source_id", "match": {"value": "src-q1"}}]}}
    ]
