"""TDD regression tests for Milestone P3 Packet 5: S3-03 & S3-04.

Covers:
- S3-03: `delete_source` Qdrant cleanup & fail-closed behavior; `qdrant_prune_degraded` +
  `RECONCILE_SEARCH_INDEX` enqueue on superseded prune failure; `reconcile_orphan_search_units`
  skipping `_collection_meta` and pruning inactive/non-ready/superseded/missing units;
  `JobType.RECONCILE_SEARCH_INDEX` handler wiring.
- S3-04: Removing `DELETE /collections` from `session_context/persistence.py`, skipping Qdrant
  upsert for `deterministic-ngram-v1`, setting `embedding_degraded` on fallback, and
  collision-safe `session_qdrant_collection_name`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    Job,
    SearchUnit,
    Source,
    SourceProjectMapping,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.services import search_index_service, source_service
from sag_api.services.session_context.persistence import (
    ensure_session_qdrant_collection,
    session_qdrant_collection_name,
    upsert_session_units_to_qdrant,
)
from sag_api.services.session_context.substrate import IncrementalSessionIndex


@pytest_asyncio.fixture
async def p3_lifecycle_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# S3-03: delete_source Qdrant cleanup & fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_source_cleans_qdrant_points_and_fails_closed_on_error(p3_lifecycle_db, tmp_path):
    deleted_calls: list[tuple[str, dict]] = []
    fail_qdrant = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/delete"):
            if fail_qdrant:
                return httpx.Response(503, text="qdrant unavailable")
            deleted_calls.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    qdrant_client = httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    )
    mgr = SimpleNamespace(
        release=AsyncMock(),
        get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_client),
    )

    try:
        async with p3_lifecycle_db() as session:
            session.add(Source(id="src-s3", name="S3 Source", sag_source_config_id="cfg-s3"))
            await session.flush()
            session.add(
                SourceProjectMapping(
                    source_id="src-s3",
                    organization_id="tenant-s3",
                    project_id="proj-s3",
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="admin",
                    approval_ref="ref-s3",
                )
            )
            session.add(
                Document(
                    id="doc-s3",
                    source_id="src-s3",
                    tenant_id="tenant-s3",
                    project_id="proj-s3",
                    filename="doc.md",
                    storage_path=str(tmp_path / "doc.md"),
                    status=DocumentStatus.READY,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id="ver-s3-1",
                    document_id="doc-s3",
                    version_no=1,
                    file_hash="fh-s3-1",
                    status="READY",
                    search_status="SEARCH_READY",
                )
            )
            await session.commit()

            # 1. Fail-closed when Qdrant errors: source MUST remain in DB for retry
            fail_qdrant = True
            with pytest.raises(RuntimeError):
                await source_service.delete_source(
                    session,
                    "src-s3",
                    engine_manager=mgr,
                    upload_dir=str(tmp_path),
                    job_queue=None,
                )
            await session.rollback()
            assert await session.get(Source, "src-s3") is not None

            # 2. When Qdrant succeeds: deletes version points BEFORE removing source
            fail_qdrant = False
            await source_service.delete_source(
                session,
                "src-s3",
                engine_manager=mgr,
                upload_dir=str(tmp_path),
                job_queue=None,
            )
            assert await session.get(Source, "src-s3") is None
            assert deleted_calls == [
                (
                    "/collections/search_units_proj-s3/points/delete",
                    {"filter": {"must": [{"key": "document_version_id", "match": {"any": ["ver-s3-1"]}}]}},
                )
            ]
    finally:
        await qdrant_client.aclose()


# ---------------------------------------------------------------------------
# S3-03: reconcile_orphan_search_units, marker preservation & JobType wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_orphan_search_units_prunes_inactive_superseded_and_preserves_meta(p3_lifecycle_db):
    now = datetime.now(UTC)
    async with p3_lifecycle_db() as session:
        session.add(Source(id="src-rec", name="Rec", sag_source_config_id="cfg-rec"))
        session.add_all(
            [
                Document(
                    id="doc-active",
                    source_id="src-rec",
                    tenant_id="t1",
                    project_id="proj-rec",
                    filename="a.md",
                    storage_path="a.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc-inactive",
                    source_id="src-rec",
                    tenant_id="t1",
                    project_id="proj-rec",
                    filename="inactive.md",
                    storage_path="inactive.md",
                    status=DocumentStatus.READY,
                    is_active=False,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DocumentVersion(
                    id="ver-ready",
                    document_id="doc-active",
                    version_no=2,
                    file_hash="fh-2",
                    valid_from=now - timedelta(hours=1),
                    valid_to=now + timedelta(days=1),
                    status="READY",
                    search_status="SEARCH_READY",
                ),
                DocumentVersion(
                    id="ver-superseded",
                    document_id="doc-active",
                    version_no=1,
                    file_hash="fh-1",
                    valid_from=now - timedelta(days=2),
                    valid_to=now - timedelta(minutes=1),  # closed / superseded
                    status="READY",
                    search_status="SEARCH_READY",
                ),
                DocumentVersion(
                    id="ver-failed",
                    document_id="doc-active",
                    version_no=3,
                    file_hash="fh-3",
                    valid_from=now,
                    valid_to=now + timedelta(days=1),
                    status="FAILED",
                    search_status="INDEX_FAILED",
                ),
                DocumentVersion(
                    id="ver-inactive",
                    document_id="doc-inactive",
                    version_no=1,
                    file_hash="fh-inact",
                    valid_from=now - timedelta(hours=1),
                    valid_to=now + timedelta(days=1),
                    status="READY",
                    search_status="SEARCH_READY",
                ),
            ]
        )
        await session.flush()

        for suffix, vid in (
            ("ready", "ver-ready"),
            ("sup", "ver-superseded"),
            ("fail", "ver-failed"),
            ("inact", "ver-inactive"),
        ):
            digest = hashlib.sha256(suffix.encode()).hexdigest()
            session.add(
                CanonicalBlock(
                    id=f"blk-{suffix}",
                    document_version_id=vid,
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="S",
                    normalized_text=suffix,
                    content_hash=digest,
                )
            )
            session.add(
                SearchUnit(
                    id=f"su-{suffix}",
                    document_version_id=vid,
                    block_from_id=f"blk-{suffix}",
                    block_to_id=f"blk-{suffix}",
                    security_partition_id="proj-rec",
                    content_hash=digest,
                    token_count=1,
                    page_from=1,
                    page_to=1,
                    section_path="S",
                )
            )
        await session.commit()

        meta_id = search_index_service.collection_meta_point_id("search_units_proj-rec")
        qdrant_points = [
            {"id": meta_id, "payload": {"_collection_meta": True, "payload_only": True}},
            {"id": "pt-ready", "payload": {"search_unit_id": "su-ready", "document_version_id": "ver-ready"}},
            {"id": "pt-sup", "payload": {"search_unit_id": "su-sup", "document_version_id": "ver-superseded"}},
            {"id": "pt-fail", "payload": {"search_unit_id": "su-fail", "document_version_id": "ver-failed"}},
            {"id": "pt-inact", "payload": {"search_unit_id": "su-inact", "document_version_id": "ver-inactive"}},
            {"id": "pt-missing", "payload": {"search_unit_id": "su-gone", "document_version_id": "ver-gone"}},
        ]
        deleted_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/points/scroll"):
                return httpx.Response(200, json={"result": {"points": qdrant_points, "next_page_offset": None}})
            if request.url.path.endswith("/points/delete"):
                body = json.loads(request.content.decode("utf-8"))
                deleted_ids.extend(body.get("points", []))
                return httpx.Response(200, json={"result": {"status": "completed"}})
            return httpx.Response(200, json={"result": {}})

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            stats = await search_index_service.reconcile_orphan_search_units(
                session, q_client, project_id="proj-rec"
            )

        assert set(deleted_ids) == {"pt-sup", "pt-fail", "pt-inact", "pt-missing"}
        assert meta_id not in deleted_ids
        assert "pt-ready" not in deleted_ids
        assert stats["orphans_deleted"] == 4


@pytest.mark.asyncio
async def test_reconcile_search_index_job_type_and_handler_wired(p3_lifecycle_db, monkeypatch):
    from sag_api.jobs import tasks

    assert hasattr(JobType, "RECONCILE_SEARCH_INDEX")
    assert JobType.RECONCILE_SEARCH_INDEX in tasks.TASK_HANDLERS

    called_projects: list[str] = []

    async def fake_reconcile(session, qdrant_client, *, project_id: str):
        called_projects.append(project_id)
        return {"scanned": 2, "orphans_deleted": 1}

    monkeypatch.setattr(search_index_service, "reconcile_orphan_search_units", fake_reconcile)

    async with p3_lifecycle_db() as session:
        session.add(Source(id="src-j", name="J", sag_source_config_id="cfg-j"))
        session.add(
            Document(
                id="doc-j",
                source_id="src-j",
                tenant_id="t-j",
                project_id="proj-j1",
                filename="j.md",
                storage_path="j.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        job = Job(
            id="job-reconcile-1",
            type=JobType.RECONCILE_SEARCH_INDEX,
            status=JobStatus.RUNNING,
            payload={},
        )
        session.add(job)
        await session.commit()

        q_client = httpx.AsyncClient(
            base_url="http://qdrant.local",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"result": {}})),
        )
        try:
            mgr = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=q_client))
            await tasks.TASK_HANDLERS[JobType.RECONCILE_SEARCH_INDEX](
                session, job, engine_manager=mgr
            )
        finally:
            await q_client.aclose()

        assert called_projects == ["proj-j1"]
        assert job.progress == 1.0
        assert (job.payload or {}).get("orphans_deleted") == 1


# ---------------------------------------------------------------------------
# S3-04: Session Collection Safety & Fallback Degraded Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_qdrant_never_deletes_collection_on_dim_mismatch():
    delete_collection_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            delete_collection_calls.append(request.url.path)
            return httpx.Response(200, json={"result": True})
        if request.method == "GET" and "/collections/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 64}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={"result": {}})

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="mismatch"):
            await ensure_session_qdrant_collection(client, project_id="proj-safe", vector_dim=32)

    assert delete_collection_calls == []


@pytest.mark.asyncio
async def test_session_qdrant_skips_deterministic_ngram_fallback_and_marks_degraded():
    put_points_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/points"):
            put_points_calls.append(request.url.path)
        return httpx.Response(200, json={"result": {}})

    class FailingEmbedder:
        model = "BAAI/bge-m3"
        dimensions = 1024

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("503 upstream")

    idx = IncrementalSessionIndex("thread-deg-1")
    await idx.append_messages(
        [{"id": "m1", "role": "user", "content": "Deploy API on cluster A"}],
        embedder=FailingEmbedder(),
    )
    assert idx.embedding_mode == "deterministic-ngram-v1"
    assert idx.embedding_degraded is True
    assert idx.embedding_degraded_reason

    from sag_api.db.models.session_context import SessionUnit

    fake_unit = SessionUnit(
        id="su-deg-1",
        tenant_id="t1",
        project_id="proj-deg",
        thread_id="thread-deg-1",
        owner_key="owner-1",
        unit_kind="TURN",
        ordinal=0,
        role="user",
        message_from_id="m1",
        message_to_id="m1",
        source_message_ids_json=["m1"],
        security_partition_ids_json=["proj-deg"],
        content_hash="a" * 64,
        token_count=5,
        embedding_model_signature="deterministic-ngram-v1",
        valid_from=datetime.now(UTC),
    )

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
    ) as client:
        upserted = await upsert_session_units_to_qdrant(
            client,
            project_id="proj-deg",
            items=[(fake_unit, [0.1] * 256, {"indices": [1], "values": [1.0]})],
        )
    assert upserted == 0
    assert put_points_calls == []


def test_session_qdrant_collection_name_prevents_unicode_and_punctuation_collisions():
    assert session_qdrant_collection_name("dự án 1") != session_qdrant_collection_name("đề án 1")
    assert session_qdrant_collection_name("proj:a") != session_qdrant_collection_name("proj/a")
    assert session_qdrant_collection_name("proj-alpha") == session_qdrant_collection_name("proj-alpha")
