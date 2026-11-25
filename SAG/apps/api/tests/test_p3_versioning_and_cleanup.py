"""TDD tests for Milestone P3 Packet 3: S1-09 versioning/retry/valid_to and S1-11 delete/reprocess cleanup."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.errors import ConflictError
from sag_api.core.identity import generate_version_id
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.services.dedup_and_temporal_service import resolve_temporal_supersedes
from sag_api.services.document_service import handle_document_upload, reprocess_document


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:  # noqa: ANN001
        connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
async def p3_ver_db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_mode", ["FAILED", "INDEX_FAILED"])
async def test_reupload_same_bytes_after_failed_or_index_failed_allocates_v2(p3_ver_db, fail_mode):
    payload = b"# Spec\n\nRetryable upload bytes.\n"
    async with p3_ver_db() as session:
        v1 = await handle_document_upload(
            session,
            tenant_id="tenant-r",
            project_id="proj-r",
            owner_id="user-1",
            security_partition_id="proj-r:default",
            client_token="tok-v1",
            file_bytes=payload,
            original_filename="spec.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:retry-doc",
        )
        assert v1.version_no == 1
        assert v1.is_duplicate is False

        doc = await session.get(Document, v1.document_id)
        ver1 = await session.get(DocumentVersion, v1.version_id)
        assert doc is not None and ver1 is not None
        if fail_mode == "FAILED":
            ver1.status = "FAILED"
            ver1.search_status = "FAILED"
            doc.status = DocumentStatus.FAILED
        else:
            ver1.status = "FAILED"
            ver1.search_status = "INDEX_FAILED"
        await session.commit()

        v2 = await handle_document_upload(
            session,
            tenant_id="tenant-r",
            project_id="proj-r",
            owner_id="user-1",
            security_partition_id="proj-r:default",
            client_token="tok-v2",
            file_bytes=payload,
            original_filename="spec.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:retry-doc",
        )
        assert v2.is_duplicate is False
        assert v2.version_no == 2
        assert v2.version_id != v1.version_id
        assert v2.run_id != v1.run_id


@pytest.mark.asyncio
async def test_v1_valid_to_stays_open_while_v2_pending_or_failed_and_closes_on_v3_ready(
    p3_ver_db,
    monkeypatch,
):
    from sag_api.services import search_unit_retrieval_service

    monkeypatch.setattr(tasks.settings, "document_event_extraction_enabled", False)

    async with p3_ver_db() as session:
        v1_res = await handle_document_upload(
            session,
            tenant_id="tenant-t",
            project_id="proj-t",
            owner_id="user-1",
            security_partition_id="proj-t:default",
            client_token="tok-t-v1",
            file_bytes=b"# V1\n\nFirst ready version.\n",
            original_filename="doc.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:temporal-doc",
        )
        doc = await session.get(Document, v1_res.document_id)
        ver1 = await session.get(DocumentVersion, v1_res.version_id)
        run1 = await session.get(IngestionRun, v1_res.run_id)
        source = await session.get(Source, doc.source_id)
        assert doc is not None and ver1 is not None and run1 is not None and source is not None

        now_ready = datetime.now(UTC)
        ver1.status = "READY"
        ver1.search_status = "SEARCH_READY"
        ver1.search_ready_at = now_ready
        doc.status = DocumentStatus.READY
        run1.status = "SUCCEEDED"
        run1.started_at = now_ready
        mapping = await session.scalar(
            select(SourceProjectMapping).where(SourceProjectMapping.source_id == source.id)
        )
        if mapping is None:
            session.add(
                SourceProjectMapping(
                    source_id=source.id,
                    organization_id="tenant-t",
                    project_id="proj-t",
                    state="CONFIRMED",
                    confirmed_at=now_ready,
                    confirmed_by="user-1",
                    approval_ref="test-ref",
                )
            )
        else:
            mapping.state = "CONFIRMED"
            mapping.organization_id = "tenant-t"
            mapping.project_id = "proj-t"
            mapping.confirmed_at = now_ready
        blk1 = CanonicalBlock(
            id="blk-t-v1",
            document_version_id=ver1.id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="V1",
            source_anchor="a-1",
            normalized_text="First ready version.",
            content_hash=hashlib.sha256(b"First ready version.").hexdigest(),
        )
        session.add(blk1)
        await session.flush()
        session.add(
            SearchUnit(
                id="su-t-v1",
                document_version_id=ver1.id,
                block_from_id=blk1.id,
                block_to_id=blk1.id,
                security_partition_id="proj-t:default",
                content_hash=blk1.content_hash,
                token_count=3,
                page_from=1,
                page_to=1,
                section_path="V1",
            )
        )
        session.add(
            StageRun(
                id="sr-t-v1",
                run_id=run1.id,
                stage="INDEX_SEARCH",
                status="SUCCESS",
                duration_ms=1.0,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": "search_units_proj-t",
                    "manifest_checksum": "a" * 64,
                    "qdrant_checksum": "a" * 64,
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                },
                created_at=now_ready,
            )
        )
        await session.commit()
        orig_v1_valid_to = ver1.valid_to
        assert orig_v1_valid_to.year == 9999

        # Upload v2 -> v1.valid_to MUST stay open while v2 is still PENDING
        v2_res = await handle_document_upload(
            session,
            tenant_id="tenant-t",
            project_id="proj-t",
            owner_id="user-1",
            security_partition_id="proj-t:default",
            client_token="tok-t-v2",
            file_bytes=b"# V2\n\nSecond version that will fail.\n",
            original_filename="doc.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:temporal-doc",
        )
        await session.refresh(ver1)
        assert ver1.valid_to == orig_v1_valid_to

        # Fail v2 inside _process_document_unlocked
        all_jobs = list((await session.scalars(select(Job).where(Job.document_id == doc.id))).all())
        v2_job = next(j for j in all_jobs if (j.payload or {}).get("run_id") == v2_res.run_id)
        v2_job.status = JobStatus.RUNNING
        await session.commit()

        async def fail_parse(*_args, **_kwargs):
            raise RuntimeError("Simulated v2 parse failure")

        monkeypatch.setattr(tasks, "prepare_document", fail_parse)
        manager = SimpleNamespace(get_sag_embedding=AsyncMock(return_value=None))
        with pytest.raises(RuntimeError, match="Simulated v2 parse failure"):
            await tasks._process_document_unlocked(session, v2_job, engine_manager=manager)

        await session.refresh(doc)
        await session.refresh(ver1)
        ver2 = await session.get(DocumentVersion, v2_res.version_id)
        v2_run = await session.get(IngestionRun, v2_res.run_id)
        assert ver2 is not None and v2_run is not None
        assert ver2.status == "FAILED"
        assert ver2.search_status == "FAILED"
        assert v2_run.status == "FAILED"
        # Critical: Document.status stays READY and v1.valid_to stays open so v1 remains retrievable!
        assert doc.status == DocumentStatus.READY
        assert ver1.valid_to > datetime.now(UTC)

        # Verify scope version query still sees v1 as active ready version
        principal = VerifiedPrincipal(
            subject="user-1",
            organization_id="tenant-t",
            allowed_project_ids=frozenset({"proj-t"}),
            issuer="test",
            key_id="k1",
            token_id="t1",
            issued_at=1,
            expires_at=9999999999,
            tenant_id="tenant-t",
            allowed_partition_ids=frozenset({"proj-t:default"}),
        )
        ready_versions = await search_unit_retrieval_service._load_current_ready_versions(
            session,
            [source],
            principal,
        )
        assert ver1.id in ready_versions
        assert ver2.id not in ready_versions

        # Now upload v3 and commit SEARCH_READY via resolve_temporal_supersedes
        v3_res = await handle_document_upload(
            session,
            tenant_id="tenant-t",
            project_id="proj-t",
            owner_id="user-1",
            security_partition_id="proj-t:default",
            client_token="tok-t-v3",
            file_bytes=b"# V3\n\nThird ready version.\n",
            original_filename="doc.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:temporal-doc",
        )
        ver3 = await session.get(DocumentVersion, v3_res.version_id)
        assert ver3 is not None
        await resolve_temporal_supersedes(session, current_version=ver3, document_id=doc.id)
        ver3.search_status = "SEARCH_READY"
        ver3.status = "READY"
        await session.commit()

        await session.refresh(ver1)
        await session.refresh(ver3)
        v1_to = ver1.valid_to.replace(tzinfo=UTC) if ver1.valid_to.tzinfo is None else ver1.valid_to
        v3_from = ver3.valid_from.replace(tzinfo=UTC) if ver3.valid_from.tzinfo is None else ver3.valid_from
        assert v1_to <= v3_from
        assert ver3.supersedes_id == ver1.id


@pytest.mark.asyncio
async def test_existing_version_hash_mismatch_raises_conflict_error(p3_ver_db):
    async with p3_ver_db() as session:
        v1 = await handle_document_upload(
            session,
            tenant_id="tenant-h",
            project_id="proj-h",
            owner_id="user-1",
            security_partition_id="proj-h:default",
            client_token="tok-h-1",
            file_bytes=b"v1 content",
            original_filename="h.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:hash-conflict",
        )
        # Pre-seed v2 id with a mismatched file_hash
        v2_id = generate_version_id(v1.document_id, 2)
        session.add(
            DocumentVersion(
                id=v2_id,
                document_id=v1.document_id,
                version_no=2,
                file_hash="0" * 64,
                valid_from=datetime.now(UTC),
                status="RECEIVED",
                search_status="PENDING",
                knowledge_status="NOT_STARTED",
                metadata_json={"security_partition_id": "proj-h:default"},
            )
        )
        # Set v1 latest_ver? Wait: v2_id is version_no=2; to force existing_ver collision at version_no=2,
        # simulate latest_ver at version_no=1? No, query orders by version_no.desc(), so delete version_no=2's
        # ordering? Wait: existing_ver triggers when version_id=generate_version_id(doc.id, latest_ver.version_no + 1)
        # exists (e.g. if version_no=1 exists AND version_no=3 exists? No, version_no=1 + 1 = 2, wait if v2 exists
        # latest_ver would be v2!). Let's check how existing_ver can exist if latest_ver is v1?
        # It happens if an orphan/out-of-order version row with same deterministic UUID exists, OR if version_no=1
        # already existed when is_new_doc=True (fresh doc whose deterministic doc_id already had version 1).
        await session.commit()

    # Test deterministic version_id collision on existing_ver when doc has version_no=1 deleted or pre-seeded:
    # Seed a document where version_no=1 has mismatched hash on another branch? Actually simplest:
    # Pre-seed an out-of-order row at deterministic v2 UUID (version_no=0) while v1 is latest_ver
    # so uploading v2 collides on existing_ver and hits the file_hash mismatch check.
    async with p3_ver_db() as session:
        v1 = await handle_document_upload(
            session,
            tenant_id="tenant-h2",
            project_id="proj-h2",
            owner_id="user-1",
            security_partition_id="proj-h2:default",
            client_token="tok-h2-1",
            file_bytes=b"v1 content",
            original_filename="h2.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:hash-conflict-2",
        )
        v2_id = generate_version_id(v1.document_id, 2)
        session.add(
            DocumentVersion(
                id=v2_id,
                document_id=v1.document_id,
                version_no=0,
                file_hash="f" * 64,
                valid_from=datetime.now(UTC),
                status="RECEIVED",
                search_status="PENDING",
                knowledge_status="NOT_STARTED",
                metadata_json={"security_partition_id": "proj-h2:default"},
            )
        )
        await session.commit()

        with pytest.raises(ConflictError) as exc_info:
            await handle_document_upload(
                session,
                tenant_id="tenant-h2",
                project_id="proj-h2",
                owner_id="user-1",
                security_partition_id="proj-h2:default",
                client_token="tok-h2-2",
                file_bytes=b"v2 different content",
                original_filename="h2.md",
                content_type="text/markdown",
                logical_source_id="continuum:project_document:hash-conflict-2",
            )
        assert exc_info.value.code == "VERSION_HASH_MISMATCH"


@pytest.mark.asyncio
async def test_delete_document_task_cleans_qdrant_when_project_id_is_none(p3_ver_db, tmp_path):
    deleted_calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/delete"):
            deleted_calls.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    qdrant_client = httpx.AsyncClient(base_url="http://qdrant.local", transport=httpx.MockTransport(handler))
    manager = SimpleNamespace(
        delete_document_data=AsyncMock(),
        get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_client),
    )

    f = tmp_path / "legacy.md"
    f.write_text("legacy content", encoding="utf-8")

    async with p3_ver_db() as session:
        session.add(Source(id="src-del", name="Del Source", sag_source_config_id="cfg-del"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id="src-del",
                organization_id="tenant-d",
                project_id="proj-mapped",
                state="CONFIRMED",
                confirmed_at=datetime.now(UTC),
                confirmed_by="admin",
                approval_ref="ref-1",
            )
        )
        session.add(
            Document(
                id="doc-del",
                source_id="src-del",
                tenant_id="tenant-d",
                project_id=None,
                filename="legacy.md",
                storage_path=str(f),
                status=DocumentStatus.DELETING,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-del-1",
                document_id="doc-del",
                version_no=1,
                file_hash="fh-del-1",
                valid_from=datetime.now(UTC),
                status="READY",
                search_status="SEARCH_READY",
            )
        )
        await session.flush()
        session.add(
            IngestionRun(
                id="run-del-1",
                tenant_id="tenant-d",
                project_id="proj-run",
                document_version_id="ver-del-1",
                idempotency_key="idem-del-1",
                payload_hash="fh-del-1",
                status="SUCCEEDED",
            )
        )
        job = Job(
            id="job-del-1",
            type=JobType.DELETE_DOCUMENT,
            source_id="src-del",
            document_id="doc-del",
            status=JobStatus.RUNNING,
            payload={},
        )
        session.add(job)
        await session.commit()

        await tasks.delete_document_task(session, job, engine_manager=manager)

    await qdrant_client.aclose()
    paths = {p for p, _ in deleted_calls}
    assert "/collections/search_units_proj-run/points/delete" in paths
    assert "/collections/search_units_proj-mapped/points/delete" in paths
    for _p, body in deleted_calls:
        assert body["filter"]["must"][0]["match"]["any"] == ["ver-del-1"]


@pytest.mark.asyncio
async def test_reprocess_document_cleans_stale_search_units_qdrant_and_resets_knowledge_status(
    p3_ver_db,
    monkeypatch,
):
    deleted_calls: list[tuple[str, dict]] = []
    qdrant_fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/delete"):
            if qdrant_fail:
                return httpx.Response(503, text="qdrant unavailable")
            deleted_calls.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    qdrant_client = httpx.AsyncClient(base_url="http://qdrant.local", transport=httpx.MockTransport(handler))
    manager = SimpleNamespace(
        delete_document_data=AsyncMock(),
        get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_client),
    )
    fake_queue = SimpleNamespace(
        enqueue=AsyncMock(),
        begin_source_maintenance=lambda *_args, **_kwargs: None,
    )

    async with p3_ver_db() as session:
        uploaded = await handle_document_upload(
            session,
            tenant_id="tenant-rep",
            project_id="proj-rep",
            owner_id="user-1",
            security_partition_id="proj-rep:default",
            client_token="tok-rep-1",
            file_bytes=b"# Reprocess\n\nStale units must be purged.\n",
            original_filename="rep.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:rep-doc",
        )
        doc = await session.get(Document, uploaded.document_id)
        source = await session.get(Source, doc.source_id)
        ver = await session.get(DocumentVersion, uploaded.version_id)
        assert doc is not None and source is not None and ver is not None
        doc.status = DocumentStatus.READY
        ver.status = "READY"
        ver.search_status = "SEARCH_READY"
        ver.search_ready_at = datetime.now(UTC)
        ver.knowledge_status = "DATA_READY"

        blk = CanonicalBlock(
            id="blk-rep-1",
            document_version_id=ver.id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Reprocess",
            normalized_text="Stale units must be purged.",
            content_hash=hashlib.sha256(b"Stale units must be purged.").hexdigest(),
        )
        session.add(blk)
        await session.flush()
        session.add(
            SearchUnit(
                id="su-rep-1",
                document_version_id=ver.id,
                block_from_id=blk.id,
                block_to_id=blk.id,
                security_partition_id="proj-rep:default",
                content_hash=blk.content_hash,
                token_count=5,
                page_from=1,
                page_to=1,
                section_path="Reprocess",
            )
        )
        await session.commit()

        reproc_job = await reprocess_document(session, source, doc.id, job_queue=fake_queue)
        reproc_job.status = JobStatus.RUNNING
        await session.commit()

        # 1. Fail-closed if Qdrant deletion fails
        qdrant_fail = True
        with pytest.raises(RuntimeError):
            await tasks._reprocess_document_task_unlocked(
                session, reproc_job, engine_manager=manager, job_queue=fake_queue
            )
        await session.refresh(reproc_job)
        assert not (reproc_job.payload or {}).get("cleanup_completed")
        assert await session.get(SearchUnit, "su-rep-1") is not None

        # 2. Succeeds when Qdrant deletion succeeds
        qdrant_fail = False
        await tasks._reprocess_document_task_unlocked(
            session, reproc_job, engine_manager=manager, job_queue=fake_queue
        )

        await session.refresh(ver)
        await session.refresh(reproc_job)
        assert (reproc_job.payload or {}).get("cleanup_completed") is True
        assert await session.get(SearchUnit, "su-rep-1") is None
        assert await session.get(CanonicalBlock, "blk-rep-1") is None
        assert ver.status == "RECEIVED"
        assert ver.search_status == "PENDING"
        assert ver.search_ready_at is None
        assert ver.knowledge_status == "NOT_STARTED"
        assert len(deleted_calls) == 1
        assert deleted_calls[0][0] == "/collections/search_units_proj-rep/points/delete"
        assert deleted_calls[0][1]["filter"]["must"][0]["match"]["any"] == [ver.id]

    await qdrant_client.aclose()


@pytest.mark.asyncio
async def test_delete_document_task_falls_back_to_httpx_client_when_manager_lacks_helper(
    p3_ver_db,
    tmp_path,
    monkeypatch,
):
    deleted_calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/delete"):
            deleted_calls.append((request.url.path, json.loads(request.content.decode("utf-8"))))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(tasks.httpx, "AsyncClient", fake_async_client)
    manager = SimpleNamespace(delete_document_data=AsyncMock())

    f = tmp_path / "fallback_del.md"
    f.write_text("fallback delete", encoding="utf-8")

    async with p3_ver_db() as session:
        session.add(Source(id="src-fb", name="FB", sag_source_config_id="cfg-fb"))
        await session.flush()
        session.add(
            Document(
                id="doc-fb",
                source_id="src-fb",
                tenant_id="tenant-fb",
                project_id=None,
                filename="fallback_del.md",
                storage_path=str(f),
                status=DocumentStatus.DELETING,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-fb-1",
                document_id="doc-fb",
                version_no=1,
                file_hash="fh-fb-1",
                valid_from=datetime.now(UTC),
                status="READY",
                search_status="SEARCH_READY",
            )
        )
        await session.flush()
        session.add(
            IngestionRun(
                id="run-fb-1",
                tenant_id="tenant-fb",
                project_id="proj-fb",
                document_version_id="ver-fb-1",
                idempotency_key="idem-fb-1",
                payload_hash="fh-fb-1",
                status="SUCCEEDED",
            )
        )
        job = Job(
            id="job-fb-1",
            type=JobType.DELETE_DOCUMENT,
            source_id="src-fb",
            document_id="doc-fb",
            status=JobStatus.RUNNING,
            payload={},
        )
        session.add(job)
        await session.commit()

        await tasks.delete_document_task(session, job, engine_manager=manager)

    assert len(deleted_calls) == 1
    assert deleted_calls[0][0] == "/collections/search_units_proj-fb/points/delete"
    assert deleted_calls[0][1]["filter"]["must"][0]["match"]["any"] == ["ver-fb-1"]

