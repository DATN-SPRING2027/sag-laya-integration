"""Regression Test Suite for Checkpoint A Ingestion Lane.

Verifies end-to-end ingestion from upload through canonical extraction, deduplication,
SearchUnit indexing, Qdrant payload contract, manifest verification, search lane isolation,
empty index handling, zero secret leakage, idempotent retries, and disaster recovery rebuild.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError
from sag_api.core.sanitizer import sanitize_error_message
from sag_api.core.security import create_access_token
from sag_api.db.models import Document, Job, Source
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    IngestionRun,
    SearchUnit,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.tasks import _process_document_unlocked
from sag_api.main import app
from sag_api.sag import RetrievedSection
from sag_api.services.document_service import get_document_version_status
from sag_api.services.evidence_service import has_traceable_locator, resolve_traceable_evidence
from sag_api.services.rebuild_service import rebuild_search_index_for_project
from sag_api.services.search_index_service import (
    build_search_units_from_blocks,
    generate_search_unit_point_id,
    run_search_indexing_stage,
)

# Reference to unpatched real AsyncClient to avoid infinite recursion when mocking
_RealAsyncClient = httpx.AsyncClient


class MockQdrantStorage:
    """In-memory mock server for Qdrant simulating collection creation, index, points, scroll, count, and delete."""

    def __init__(self, vector_dim: int = 3) -> None:
        self.vector_dim = vector_dim
        self.collections: dict[str, dict[str, Any]] = {}
        self.points: dict[str, dict[str, dict[str, Any]]] = {}  # col -> {point_id: point_dict}
        self.corrupt_checksum_on_scroll: bool = False
        self.fail_on_upsert: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        method = request.method

        # 1. Collection check / creation
        if method == "GET" and url_path.startswith("/collections/"):
            col = url_path.split("/")[2]
            if col in self.collections:
                return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"content_vector": {"size": self.vector_dim}}}}}})
            return httpx.Response(404, json={"status": "error", "message": "Not found"})

        if method == "PUT" and url_path.startswith("/collections/") and "/index" not in url_path and "/points" not in url_path:
            col = url_path.split("/")[2]
            self.collections[col] = {}
            if col not in self.points:
                self.points[col] = {}
            return httpx.Response(200, json={"result": True, "status": "ok"})

        # 2. Payload Index creation
        if method == "PUT" and "/index" in url_path:
            return httpx.Response(200, json={"result": True, "status": "ok"})

        # 3. Points count
        if method == "POST" and url_path.endswith("/points/count"):
            col = url_path.split("/")[2]
            col_points = self.points.get(col, {})
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            doc_ver_id = None
            for f in body.get("filter", {}).get("must", []):
                if f.get("key") == "document_version_id":
                    doc_ver_id = f.get("match", {}).get("value")
            if doc_ver_id:
                count = sum(1 for p in col_points.values() if p.get("payload", {}).get("document_version_id") == doc_ver_id)
            else:
                count = len(col_points)
            return httpx.Response(200, json={"result": {"count": count}})

        # 4. Points scroll
        if method == "POST" and url_path.endswith("/points/scroll"):
            col = url_path.split("/")[2]
            col_points = self.points.get(col, {})
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            doc_ver_id = None
            for f in body.get("filter", {}).get("must", []):
                if f.get("key") == "document_version_id":
                    doc_ver_id = f.get("match", {}).get("value")
            matched = [p for p in col_points.values() if not doc_ver_id or p.get("payload", {}).get("document_version_id") == doc_ver_id]

            if self.corrupt_checksum_on_scroll:
                matched = [
                    {
                        **p,
                        "payload": {**p.get("payload", {}), "content_hash": "tampered_hash_value"},
                    }
                    for p in matched
                ]

            return httpx.Response(200, json={"result": {"points": matched, "next_page_offset": None}})

        # 5. Points upsert
        if method == "PUT" and "/points" in url_path:
            if self.fail_on_upsert:
                return httpx.Response(500, json={"status": "error", "message": "Qdrant internal storage error"})
            col = url_path.split("/")[2]
            if col not in self.points:
                self.points[col] = {}
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            new_points = body.get("points", [])
            for p in new_points:
                self.points[col][p["id"]] = p
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

        # 6. Points delete
        if method == "POST" and "/points/delete" in url_path:
            col = url_path.split("/")[2]
            col_points = self.points.get(col, {})
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            doc_ver_id = None
            for f in body.get("filter", {}).get("must", []):
                if f.get("key") == "document_version_id":
                    doc_ver_id = f.get("match", {}).get("value")
            if doc_ver_id:
                to_del = [pid for pid, p in col_points.items() if p.get("payload", {}).get("document_version_id") == doc_ver_id]
                for pid in to_del:
                    del col_points[pid]
            else:
                col_points.clear()
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

        return httpx.Response(200, json={"result": True, "status": "ok"})

    def make_client(self) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://mock-qdrant:6333")


class FakeIngestionEngine:
    """Mock engine simulating Phase 1 extraction with dual-representation vector embedding."""

    def __init__(self, vector_dim: int = 3) -> None:
        self.vector_dim = vector_dim

    async def get_sag_embedding(self, _config_id: str, _source=None):
        dim = self.vector_dim

        class FakeEmbedder:
            async def batch_generate(self, texts: list[str]) -> list[list[float]]:
                return [[0.1 * (i + 1) for i in range(dim)] for _ in texts]

        return FakeEmbedder()

    async def process_document(self, *args, **kwargs):
        return SimpleNamespace(
            paused=False,
            chunk_count=1,
            event_count=0,
            source_id="src_e2e",
            token_usage=150,
        )


@pytest.fixture(autouse=True)
def setup_qdrant_env(monkeypatch):
    """Ensure tests run against a dummy Qdrant URL without real network calls."""
    monkeypatch.setattr(settings, "sag_qdrant_url", "http://mock-qdrant:6333")
    monkeypatch.setattr(settings, "sag_qdrant_api_key", "mock-key")


async def seed_ingestion_pipeline(
    session: Any,
    *,
    source_id: str,
    doc_id: str,
    ver_id: str,
    run_id: str,
    job_id: str,
    project_id: str,
    file_path: Path,
    tenant_id: str = "tenant_default",
    security_partition_id: str = "sec_default",
    source_config_id: str | None = None,
) -> tuple[Source, Document, DocumentVersion, IngestionRun, Job]:
    """Helper committing parent-child entities in sequential order to satisfy SQLite foreign keys."""
    source = Source(
        id=source_id,
        name=f"Source {source_id}",
        sag_source_config_id=source_config_id or f"cfg_{source_id}",
    )
    session.add(source)
    await session.commit()

    doc = Document(
        id=doc_id,
        source_id=source_id,
        project_id=project_id,
        tenant_id=tenant_id,
        filename=file_path.name,
        storage_path=str(file_path),
        status=DocumentStatus.LOADING,
    )
    session.add(doc)
    await session.commit()

    ver = DocumentVersion(
        id=ver_id,
        document_id=doc_id,
        version_no=1,
        file_hash=f"hash_{ver_id[:8]}",
        valid_from=datetime.now(UTC),
        valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        metadata_json={"security_partition_id": security_partition_id},
    )
    session.add(ver)
    await session.commit()

    run = IngestionRun(
        id=run_id,
        tenant_id=tenant_id,
        project_id=project_id,
        document_version_id=ver_id,
        idempotency_key=f"run_{run_id}",
        payload_hash=f"hash_{ver_id[:8]}",
        status="RUNNING",
    )
    job = Job(
        id=job_id,
        type=JobType.PROCESS_DOCUMENT,
        source_id=source_id,
        document_id=doc_id,
        status=JobStatus.RUNNING,
        payload={"run_id": run_id, "storage_path": str(file_path)},
    )
    session.add_all([run, job])
    await session.commit()
    return source, doc, ver, run, job


# ==============================================================================
# Gate 1: E2E Upload to Manifest Verified & Payload Contract
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_e2e_upload_to_manifest_verified(tmp_path):
    """Kịch bản 1: Luồng thành công đầy đủ từ upload, parse, dedup, indexing đến manifest verified."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "spec.md"
    md_file.write_text("# Chương 1: Kiến Trúc Hệ Thống\n\nNội dung chi tiết về Checkpoint A.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                tenant_id="tenant_alpha",
                security_partition_id="sec_alpha",
            )
            job = await session.get(Job, job_id)
            await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.READY
        assert doc_db.error is None

        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.status == "SEARCH_READY"
        assert ver_db.search_status == "SEARCH_READY"
        assert ver_db.search_ready_at is not None

        run_db = (await check_session.execute(select(IngestionRun).where(IngestionRun.id == run_id))).scalar_one()
        assert run_db.status == "SUCCEEDED"
        assert run_db.current_stage == "COMPLETE"

        # Kiểm tra Qdrant payload contract
        col_name = f"search_units_{project_id}"
        assert col_name in q_mock.points
        points = list(q_mock.points[col_name].values())
        assert len(points) >= 1

        payload = points[0]["payload"]
        assert payload["project_id"] == project_id
        assert payload["source_id"] == source_id
        assert payload["document_id"] == doc_id
        assert payload["document_version_id"] == ver_id
        assert payload["version_no"] == 1
        assert payload["security_partition_id"] == "sec_alpha"
        assert payload["block_from_id"] is not None
        assert payload["block_to_id"] is not None
        assert payload.get("source_anchor") is not None and len(payload["source_anchor"].strip()) > 0
        assert payload.get("page_from", 0) >= 1
        assert payload.get("page_to", 0) >= payload.get("page_from", 1)
        assert "section_path" in payload and len(payload["section_path"]) > 0
        assert "valid_from_ts" in payload and payload["valid_from_ts"] is not None
        assert "bm25_sparse" in points[0]["vector"]


# ==============================================================================
# Gate 2: Secondary Queue Universe Refresh Failure Does Not Block SEARCH_READY
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_universe_refresh_failure_does_not_downgrade_search_ready(tmp_path):
    """Kịch bản 2: Lỗi tại schedule_universe_refresh (Redis die, timeout) không làm hỏng SEARCH_READY."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "notes.md"
    md_file.write_text("# Ghi chú\n\nNội dung tài liệu thử nghiệm.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()), \
         patch("sag_api.services.universe_service.schedule_universe_refresh", AsyncMock(side_effect=RuntimeError("Redis connection refused"))):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_notes",
            )
            job = await session.get(Job, job_id)
            fake_queue = SimpleNamespace(enqueue=AsyncMock())
            await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3), job_queue=fake_queue)

    async with SessionLocal() as check_session:
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.status == "SEARCH_READY"
        assert ver_db.search_status == "SEARCH_READY"
        assert ver_db.search_ready_at is not None

        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.READY


# ==============================================================================
# Gate 3: Enrichment Disabled or Lag Does Not Block Search Lane
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_enrichment_disabled_or_lag_does_not_block_search(tmp_path):
    """Kịch bản 3: Tắt hoàn toàn queue làm giàu tri thức (job_queue=None) -> SEARCH_READY vẫn đạt được ngay."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "pure_search.md"
    md_file.write_text("# Pure Search\n\nKhông phụ thuộc vào enrichment worker.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_pure",
            )
            job = await session.get(Job, job_id)
            await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3), job_queue=None)

    async with SessionLocal() as check_session:
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status == "SEARCH_READY"
        assert ver_db.knowledge_status == "NOT_STARTED"

    # Trường hợp 2: Enrichment queue bị delayed / lag -> SEARCH_READY đã commit và sẵn sàng từ trước
    project_id_lag = f"proj_lag_{uuid.uuid4().hex[:8]}"
    source_id_lag = f"src_lag_{uuid.uuid4().hex[:8]}"
    doc_id_lag = str(uuid.uuid4())
    ver_id_lag = str(uuid.uuid4())
    run_id_lag = str(uuid.uuid4())
    job_id_lag = f"job_lag_{uuid.uuid4().hex[:8]}"

    md_file_lag = tmp_path / "lag_search.md"
    md_file_lag.write_text("# Lag Search\n\nKiểm tra enrichment bị lag không ảnh hưởng search readiness.", encoding="utf-8")

    lag_event = asyncio.Event()
    readiness_verified_during_lag = False

    async def _mock_delayed_schedule(*args, **kwargs):
        nonlocal readiness_verified_during_lag
        # Đọc từ session hoàn toàn độc lập trong lúc enrichment scheduler đang bị block
        async with SessionLocal() as indep_session:
            v_check = (await indep_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id_lag))).scalar_one()
            assert v_check.search_status == "SEARCH_READY"
            assert v_check.search_ready_at is not None
            readiness_verified_during_lag = True
        await lag_event.wait()

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()), \
         patch("sag_api.services.universe_service.schedule_universe_refresh", side_effect=_mock_delayed_schedule):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id_lag,
                doc_id=doc_id_lag,
                ver_id=ver_id_lag,
                run_id=run_id_lag,
                job_id=job_id_lag,
                project_id=project_id_lag,
                file_path=md_file_lag,
                security_partition_id="sec_lag",
            )
            job_lag = await session.get(Job, job_id_lag)
            worker_task = asyncio.create_task(
                _process_document_unlocked(session, job_lag, engine_manager=FakeIngestionEngine(vector_dim=3), job_queue=SimpleNamespace())
            )
            for _ in range(50):
                if readiness_verified_during_lag:
                    break
                await asyncio.sleep(0.05)

            assert readiness_verified_during_lag is True
            lag_event.set()
            await worker_task


# ==============================================================================
# Gate 4: Parse Failure Fails Closed
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_parse_failure_fails_closed(tmp_path):
    """Kịch bản 4: Lỗi phân tích cú pháp tệp -> Đánh dấu FAILED, stage=PARSE, 0 vector trong Qdrant."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    bad_file = tmp_path / "broken.pdf"
    bad_file.write_bytes(b"%PDF-INVALID-DATA")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()), \
         patch("sag_api.jobs.tasks.prepare_document", side_effect=ValueError("Corrupt PDF / bad syntax")):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=bad_file,
                security_partition_id="sec_bad",
            )
            job = await session.get(Job, job_id)
            with pytest.raises(Exception):
                await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.FAILED
        assert doc_db.error_stage == ErrorStage.PARSE.value

        run_db = (await check_session.execute(select(IngestionRun).where(IngestionRun.id == run_id))).scalar_one()
        assert run_db.status == "FAILED"
        assert run_db.error_stage == ErrorStage.PARSE.value

        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status != "SEARCH_READY"

        col_name = f"search_units_{project_id}"
        assert len(q_mock.points.get(col_name, {})) == 0


# ==============================================================================
# Gate 5: Indexing Failure Fails Closed
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_indexing_failure_fails_closed(tmp_path):
    """Kịch bản 5: Lỗi khi nạp điểm vào Qdrant (500 Internal Storage Error) -> INDEX_FAILED, rollback."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "idx_fail.md"
    md_file.write_text("# Lỗi Index\n\nVăn bản nạp thất bại do Qdrant sập.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)
    q_mock.fail_on_upsert = True

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_f",
            )
            job = await session.get(Job, job_id)
            with pytest.raises(Exception):
                await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status == "INDEX_FAILED"
        assert ver_db.search_ready_at is None

        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.FAILED

        # Kiểm tra StageRun không bị mất sau khi rollback
        stage_run = (
            await check_session.execute(
                select(StageRun).where(StageRun.run_id == run_id).where(StageRun.stage == "INDEX_SEARCH")
            )
        ).scalar_one_or_none()
        assert stage_run is not None
        assert stage_run.status == "FAILED"
        assert stage_run.error_message is not None


# ==============================================================================
# Gate 6: Manifest Checksum Mismatch Fails Closed
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_manifest_checksum_mismatch_fails_closed():
    """Kịch bản 6: Sai lệch Checksum giữa Qdrant và PostgreSQL -> Bắt buộc ném ngoại lệ và ghi INDEX_FAILED."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    q_mock = MockQdrantStorage(vector_dim=3)
    q_mock.corrupt_checksum_on_scroll = True

    client = q_mock.make_client()

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="mismatch.md", storage_path="/tmp/m.md", project_id=project_id)
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(id=ver_id, document_id=doc_id, version_no=1, file_hash="hash_m", valid_from=datetime.now(UTC), metadata_json={"security_partition_id": "sec_m"})
        session.add(ver)
        await session.commit()

        b = CanonicalBlock(id=str(uuid.uuid4()), document_version_id=ver_id, ordinal=0, block_type="paragraph", page_from=1, page_to=1, section_path="Sec", normalized_text="Test mismatch checksum", content_hash="hm1")
        run = IngestionRun(id=run_id, tenant_id="tenant_default", project_id=project_id, document_version_id=ver_id, idempotency_key="key_m", payload_hash="hm", status="RUNNING")
        session.add_all([b, run])
        await session.commit()

        class SimpleEmbedder:
            async def batch_generate(self, texts):
                return [[0.1, 0.2, 0.3] for _ in texts]

        with pytest.raises(RuntimeError) as exc_info:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id="sec_m",
                qdrant_client=client,
                embedder=SimpleEmbedder(),
                run_id=run_id,
            )

        assert "manifest verification mismatch" in str(exc_info.value)
        await session.refresh(ver)
        assert ver.search_status == "INDEX_FAILED"
        assert ver.search_ready_at is None


# ==============================================================================
# Gate 7: Empty Index Fails Gracefully with EMPTY_INDEX Code
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_empty_index_fails_gracefully(tmp_path):
    """Kịch bản 7: Tệp chỉ có khoảng trắng / không có khối văn bản -> Đánh dấu FAILED, mã EMPTY_INDEX, 0 points."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    empty_file = tmp_path / "whitespace.md"
    empty_file.write_text("   \n\n\t   \n  ", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=empty_file,
                security_partition_id="sec_empty",
            )
            job = await session.get(Job, job_id)
            with pytest.raises(Exception) as exc_info:
                await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

            assert "EMPTY_INDEX" in str(exc_info.value)

    async with SessionLocal() as check_session:
        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.FAILED

        run_db = (await check_session.execute(select(IngestionRun).where(IngestionRun.id == run_id))).scalar_one()
        assert run_db.status == "FAILED"
        assert run_db.error_code == "EMPTY_INDEX"

        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status == "INDEX_FAILED"
        assert ver_db.search_ready_at is None

        # Kiểm tra API status trả về error code EMPTY_INDEX rõ ràng (không null)
        status_res = await get_document_version_status(check_session, project_id=project_id, document_id=doc_id, version_no=1)
        assert status_res.search_ready is False
        assert status_res.error is not None
        assert status_res.error["code"] == "EMPTY_INDEX"


# ==============================================================================
# Gate 8: Idempotent Retry and Reprocess
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_idempotent_retry_and_reprocess(tmp_path):
    """Kịch bản 8: Chạy lại tiến trình nạp (retry/reprocess) bảo đảm tính lũy đẳng:
    - Xóa sạch điểm cũ trong Qdrant.
    - Tạo lại các SearchUnit có ID UUIDv5 tất định.
    - Không nhân đôi bản ghi trong PostgreSQL và Qdrant.
    """
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_1_id = str(uuid.uuid4())
    run_2_id = str(uuid.uuid4())
    job_1_id = f"job_1_{uuid.uuid4().hex[:8]}"
    job_2_id = f"job_2_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "idempotent.md"
    md_file.write_text("# Lũy Đẳng\n\nNội dung kiểm tra idempotent retry.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        # Lần 1: Chạy hoàn tất
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_1_id,
                job_id=job_1_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_idem",
            )
            job1 = await session.get(Job, job_1_id)
            await _process_document_unlocked(session, job1, engine_manager=FakeIngestionEngine(vector_dim=3))

        col_name = f"search_units_{project_id}"
        points_run_1 = list(q_mock.points[col_name].keys())
        assert len(points_run_1) >= 1

        # Lần 2: Reprocess cùng tài liệu và phiên bản
        async with SessionLocal() as session:
            doc = await session.get(Document, doc_id)
            doc.status = DocumentStatus.LOADING
            run2 = IngestionRun(id=run_2_id, tenant_id="tenant_default", project_id=project_id, document_version_id=ver_id, idempotency_key=f"run_{run_2_id}", payload_hash="hidem", status="RUNNING")
            job2 = Job(id=job_2_id, type=JobType.PROCESS_DOCUMENT, source_id=source_id, document_id=doc_id, status=JobStatus.RUNNING, payload={"run_id": run_2_id, "storage_path": str(md_file)})
            session.add_all([doc, run2, job2])
            await session.commit()

            await _process_document_unlocked(session, job2, engine_manager=FakeIngestionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        # Số lượng SearchUnit trong PostgreSQL không bị nhân đôi
        units_count = (await check_session.execute(select(func.count(SearchUnit.id)).where(SearchUnit.document_version_id == ver_id))).scalar()
        assert units_count == len(points_run_1)

        # Qdrant điểm không bị nhân đôi và giữ nguyên ID tất định
        points_run_2 = list(q_mock.points[col_name].keys())
        assert points_run_2 == points_run_1

        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status == "SEARCH_READY"


# ==============================================================================
# Gate 9: Zero Secret Leakage in DB and Logs
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_zero_secret_leakage(caplog: pytest.LogCaptureFixture):
    """Kịch bản 9: Không rò rỉ secret trong error_message, log và database."""
    raw_leak = (
        "Failed to request https://admin_user:super_secret_password_123@qdrant.internal:6333/points"
        "?api-key=secret_query_key_xyz&token=secret_query_token_abc"
        "&api_key=secret_snake_key&password=secret_password_val"
        "&access_token=secret_access_tok&client_secret=secret_client_sec"
        "&refresh_token=secret_refresh_tok&id_token=secret_id_tok "
        "using Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sensitive_payload.signature "
        "and OpenAI key sk-proj-1234567890abcdef1234567890"
    )
    cleaned = sanitize_error_message(raw_leak)

    assert "super_secret_password_123" not in cleaned
    assert "admin_user" not in cleaned
    assert "secret_query_key_xyz" not in cleaned
    assert "secret_query_token_abc" not in cleaned
    assert "secret_snake_key" not in cleaned
    assert "secret_password_val" not in cleaned
    assert "secret_access_tok" not in cleaned
    assert "secret_client_sec" not in cleaned
    assert "secret_refresh_tok" not in cleaned
    assert "secret_id_tok" not in cleaned
    assert "sk-proj-1234567890abcdef1234567890" not in cleaned
    assert "sensitive_payload" not in cleaned

    assert "[REDACTED]" in cleaned
    assert "Bearer [REDACTED]" in cleaned
    assert "https://[REDACTED]@qdrant.internal:6333" in cleaned

    # Kiểm tra scrub credentials trên các URI DSN (PostgreSQL, Redis)
    dsn_leak = (
        "Database error: postgresql+asyncpg://admin_user:super_secret_db_pass@db.internal:5432/rag "
        "and cache error: redis://default:super_secret_redis_pass@redis.internal:6379/0"
    )
    cleaned_dsn = sanitize_error_message(dsn_leak)
    assert "super_secret_db_pass" not in cleaned_dsn
    assert "super_secret_redis_pass" not in cleaned_dsn
    assert "postgresql+asyncpg://[REDACTED]@db.internal:5432/rag" in cleaned_dsn
    assert "redis://[REDACTED]@redis.internal:6379/0" in cleaned_dsn

    # Kiểm tra log sink trực tiếp với logger
    test_logger = logging.getLogger("sag_api.test_sanitizer")
    test_logger.warning("Sanitized sink test: %s | %s", sanitize_error_message(raw_leak), cleaned_dsn)
    assert "super_secret_password_123" not in caplog.text
    assert "super_secret_db_pass" not in caplog.text
    assert "super_secret_redis_pass" not in caplog.text
    assert "secret_refresh_tok" not in caplog.text
    assert "secret_id_tok" not in caplog.text
    assert "sk-proj-1234567890abcdef1234567890" not in caplog.text
    assert "[REDACTED]" in caplog.text

    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="leak_test.md", storage_path="/tmp/leak.md", project_id=project_id)
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(id=ver_id, document_id=doc_id, version_no=1, file_hash="hash_l", valid_from=datetime.now(UTC), metadata_json={"security_partition_id": "sec_leak"})
        session.add(ver)
        await session.commit()

        b = CanonicalBlock(id=str(uuid.uuid4()), document_version_id=ver_id, ordinal=0, block_type="paragraph", page_from=1, page_to=1, section_path="Sec", normalized_text="Leak block", content_hash="hl1")
        run = IngestionRun(id=run_id, tenant_id="tenant_default", project_id=project_id, document_version_id=ver_id, idempotency_key="key_l", payload_hash="hl", status="RUNNING")
        session.add_all([b, run])
        await session.commit()

        class LeakingEmbedder:
            async def batch_generate(self, _texts):
                raise RuntimeError(raw_leak)

        client = _RealAsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)), base_url="http://mock-qdrant:6333")

        with pytest.raises(RuntimeError):
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id="sec_leak",
                qdrant_client=client,
                embedder=LeakingEmbedder(),
                run_id=run_id,
            )

        sr = (await session.execute(select(StageRun).where(StageRun.run_id == run_id))).scalar_one()
        assert "super_secret_password_123" not in sr.error_message
        assert "secret_refresh_tok" not in sr.error_message
        assert "secret_id_tok" not in sr.error_message
        assert "sk-proj-1234567890abcdef1234567890" not in sr.error_message
        assert "[REDACTED]" in sr.error_message

        # Khẳng định log sink của search_index_service cũng không chứa secret trong caplog
        assert "super_secret_password_123" not in caplog.text
        assert "secret_refresh_tok" not in caplog.text
        assert "secret_id_tok" not in caplog.text
        assert "sk-proj-1234567890abcdef1234567890" not in caplog.text


# ==============================================================================
# Gate 10: Disaster Recovery Rebuild from PostgreSQL SSOT
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_disaster_recovery_rebuild(tmp_path):
    """Kịch bản 10: Xóa trắng collection Qdrant (thảm họa mất vector) -> Rebuild phục hồi 100% từ PostgreSQL."""
    await init_db()
    project_id = f"proj_dr_{uuid.uuid4().hex[:8]}"
    source_id = f"src_dr_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_dr_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "dr.md"
    md_file.write_text("# Khôi Phục Thảm Họa\n\nToàn bộ dữ liệu được lưu bền vững trong PostgreSQL.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_dr",
                source_config_id="cfg_dr",
            )
            job = await session.get(Job, job_id)
            await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

        col_name = f"search_units_{project_id}"
        initial_points = dict(q_mock.points[col_name])
        assert len(initial_points) >= 1

        # Giả lập thảm họa: Xóa sạch toàn bộ điểm trong Qdrant
        q_mock.points[col_name].clear()
        assert len(q_mock.points[col_name]) == 0

        embedder = (await FakeIngestionEngine(vector_dim=3).get_sag_embedding("cfg_dr"))
        client = q_mock.make_client()

        async with SessionLocal() as session:
            result = await rebuild_search_index_for_project(
                session,
                project_id=project_id,
                qdrant_client=client,
                embedder=embedder,
            )

            assert result["rebuilt_versions_count"] == 1
            assert result["total_search_units_rebuilt"] == len(initial_points)

    # Khẳng định điểm trong Qdrant đã được phục hồi đầy đủ
    restored_points = q_mock.points[col_name]
    assert len(restored_points) == len(initial_points)
    assert set(restored_points.keys()) == set(initial_points.keys())


# ==============================================================================
# Gate 11: Extraction Failure After Indexing Does Not Downgrade SEARCH_READY (Finding 1)
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_extraction_failure_after_indexing_does_not_downgrade_search_ready(tmp_path):
    """Lỗi tại engine_manager.process_document (LLM extraction) sau khi Phase 2C đã commit không được hạ SEARCH_READY."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "post_idx_fail.md"
    md_file.write_text("# Tài liệu\n\nNội dung hoàn tất index nhưng fail extraction sau.", encoding="utf-8")

    q_mock = MockQdrantStorage(vector_dim=3)

    class FailingExtractionEngine(FakeIngestionEngine):
        async def process_document(self, *args, **kwargs):
            raise RuntimeError("LLM service unavailable / quota exhausted")

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=md_file,
                security_partition_id="sec_post",
            )
            job = await session.get(Job, job_id)
            with pytest.raises(Exception):
                await _process_document_unlocked(session, job, engine_manager=FailingExtractionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        # Document vẫn giữ READY để không làm mất tính khả truy/truy xuất evidence của version SEARCH_READY
        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.READY

        # Tuy nhiên DocumentVersion vẫn giữ nguyên SEARCH_READY và search_ready_at vì Qdrant manifest đã verified
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.search_status == "SEARCH_READY"
        assert ver_db.search_ready_at is not None
        assert ver_db.knowledge_status == "FAILED"

        # Khẳng định evidence locator thực sự resolve thành công qua resolve_traceable_evidence
        src_db = (await check_session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        unit_db = (await check_session.execute(select(SearchUnit).where(SearchUnit.document_version_id == ver_id))).scalars().first()
        assert unit_db is not None

        # Locator verification requires the exact indexed text, not an unrelated placeholder.
        collection = f"search_units_{project_id}"
        indexed_text = q_mock.points[collection][generate_search_unit_point_id(collection, unit_db.id)]["payload"]["content"]
        section = RetrievedSection(
            chunk_id=unit_db.id,
            heading="Section",
            content=indexed_text,
            source_config_id=src_db.sag_source_config_id,
        )
        resolved = await resolve_traceable_evidence([section], [src_db])
        assert len(resolved) == 1
        assert has_traceable_locator(resolved[0])
        assert resolved[0].document_id == doc_id
        assert resolved[0].document_version_id == ver_id
        assert resolved[0].version_no == 1
        assert resolved[0].anchor is not None and resolved[0].anchor.strip() != ""
        mismatched = await resolve_traceable_evidence([section.model_copy(update={"content": "Unrelated text"})], [src_db])
        assert not has_traceable_locator(mismatched[0])


# ==============================================================================
# Gate 12: True E2E Upload API to Manifest Verified (Finding 5)
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_e2e_real_upload_api_to_manifest_verified():
    """Luồng E2E hoàn chỉnh từ HTTP Upload API thực tế đến worker xử lý và verify Qdrant manifest."""
    await init_db()
    project_id = f"proj_real_{uuid.uuid4().hex[:8]}"
    token = create_access_token(
        subject="user_tester",
        extra={"tenant_id": "tenant_real", "allowed_projects": [project_id], "allowed_partitions": ["sec_real"]},
    )
    auth_header = {"Authorization": f"Bearer {token}"}

    md_content = b"# Architecture Overview\n\nThis is a complete end-to-end integration test from upload API to verified Qdrant manifest."
    transport = httpx.ASGITransport(app=app)

    async with _RealAsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                **auth_header,
                "X-Continuum-User-Id": "user_tester",
                "Idempotency-Key": f"idemp_{uuid.uuid4().hex[:12]}",
                "X-Continuum-Security-Partition": "sec_real",
            },
            files={"file": ("architecture.md", md_content, "text/markdown")},
        )
        assert res.status_code == 201, res.text
        upload_data = res.json()
        doc_id = upload_data["document_id"]
        ver_id = upload_data["version_id"]

    q_mock = MockQdrantStorage(vector_dim=3)

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: q_mock.make_client()):
        async with SessionLocal() as session:
            job = (await session.execute(select(Job).where(Job.document_id == doc_id))).scalars().first()
            assert job is not None
            await _process_document_unlocked(session, job, engine_manager=FakeIngestionEngine(vector_dim=3))

    async with SessionLocal() as check_session:
        doc_db = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_db.status == DocumentStatus.READY

        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_db.status == "SEARCH_READY"
        assert ver_db.search_status == "SEARCH_READY"
        assert ver_db.search_ready_at is not None

        col_name = f"search_units_{project_id}"
        assert col_name in q_mock.points
        points = list(q_mock.points[col_name].values())
        assert len(points) >= 1
        payload = points[0]["payload"]
        assert payload["project_id"] == project_id
        assert payload["document_id"] == doc_id
        assert payload["document_version_id"] == ver_id
        assert payload.get("source_anchor") is not None
        assert payload.get("page_from", 0) >= 1
        assert payload.get("page_to", 0) >= payload.get("page_from", 1)


# ==============================================================================
# Gate 13: Whitespace Source Anchor Fallback and Locator Resolution (Finding 3)
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_whitespace_source_anchor_fallback_and_locator_resolution():
    """Kiểm tra anchor chỉ chứa khoảng trắng ('   ') tự động fallback sang block-<id[:8]>
    và resolve thành công trong evidence_service mà không bị loại bỏ.
    """
    await init_db()
    project_id = f"proj_ws_{uuid.uuid4().hex[:8]}"
    source_id = f"src_ws_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    block_id = str(uuid.uuid4())

    q_mock = MockQdrantStorage(vector_dim=3)

    async with SessionLocal() as session:
        src = Source(
            id=source_id,
            name="Source WS",
            sag_source_config_id=f"cfg_{project_id}",
        )
        session.add(src)
        await session.commit()

        doc = Document(
            id=doc_id,
            filename="ws_anchor.md",
            storage_path="/tmp/ws_anchor.md",
            project_id=project_id,
            tenant_id="tenant_default",
            source_id=source_id,
            status=DocumentStatus.READY,
            is_active=True,
        )
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_ws",
            valid_from=datetime.now(UTC),
            metadata_json={"security_partition_id": "sec_ws"},
        )
        session.add(ver)
        await session.commit()

        block = CanonicalBlock(
            id=block_id,
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Section WS",
            source_anchor="   ",  # Chỉ chứa khoảng trắng
            normalized_text="Content with whitespace anchor.",
            content_hash="hash_blk_ws",
        )
        run = IngestionRun(
            id=run_id,
            tenant_id="tenant_default",
            project_id=project_id,
            document_version_id=ver_id,
            idempotency_key=f"ws_{run_id}",
            payload_hash="hash_run_ws",
            status="RUNNING",
        )
        session.add_all([block, run])
        await session.commit()

        client = q_mock.make_client()
        embedder = (await FakeIngestionEngine(vector_dim=3).get_sag_embedding("dummy_cfg"))

        await run_search_indexing_stage(
            session,
            project_id=project_id,
            document_version=ver,
            security_partition_id="sec_ws",
            qdrant_client=client,
            embedder=embedder,
            run_id=run_id,
        )
        await session.commit()

    col_name = f"search_units_{project_id}"
    points = list(q_mock.points[col_name].values())
    assert len(points) == 1
    expected_fallback_anchor = f"block-{block_id[:8]}"
    assert points[0]["payload"]["source_anchor"] == expected_fallback_anchor
    assert points[0]["payload"]["source_anchor"].strip() != ""

    async with SessionLocal() as check_session:
        # DB block source_anchor cũng được chuẩn hóa thành fallback
        b_db = (await check_session.execute(select(CanonicalBlock).where(CanonicalBlock.id == block_id))).scalar_one()
        assert b_db.source_anchor == expected_fallback_anchor

        u_db = (await check_session.execute(select(SearchUnit).where(SearchUnit.document_version_id == ver_id))).scalar_one()

        # Evidence resolution thành công và có traceable locator
        src_db = (await check_session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        section = RetrievedSection(
            chunk_id=u_db.id,
            heading="Section WS",
            content="Content with whitespace anchor.",
            source_config_id=src_db.sag_source_config_id,
        )
        resolved = await resolve_traceable_evidence([section], [src_db])
        assert len(resolved) == 1
        assert has_traceable_locator(resolved[0])
        assert resolved[0].anchor == expected_fallback_anchor


# ==============================================================================
# Gate 14: Re-index Demotes Readiness Before Deleting Points
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_reindex_demotes_readiness_before_deleting_points():
    """Khẳng định khi reindex tài liệu đang SEARCH_READY:
    - Trạng thái lập tức hạ xuống INDEXING trước khi xóa các điểm cũ.
    - Nếu có lỗi xảy ra trong quá trình xóa/index, trạng thái không bị kẹt ở SEARCH_READY.
    """
    await init_db()
    project_id = f"proj_reidx_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    block_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="reindex.md", storage_path="/tmp/reindex.md", project_id=project_id, tenant_id="tenant_default", status=DocumentStatus.READY)
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_reidx",
            valid_from=datetime.now(UTC),
            status="SEARCH_READY",
            search_status="SEARCH_READY",
            search_ready_at=datetime.now(UTC),
            metadata_json={"security_partition_id": "sec_reidx"},
        )
        block = CanonicalBlock(
            id=block_id,
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Section",
            normalized_text="Existing block",
            content_hash="h1",
        )
        unit = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            block_from_id=block_id,
            block_to_id=block_id,
            security_partition_id="sec_reidx",
            content_hash="h1",
            page_from=1,
            page_to=1,
            token_count=10,
            section_path="Section",
        )
        session.add(doc)
        await session.commit()
        session.add(ver)
        await session.commit()
        session.add(block)
        await session.commit()
        session.add(unit)
        await session.commit()

        # Giả lập client Qdrant bị lỗi khi xóa điểm cũ
        class FailingDeleteQdrantClient:
            async def post(self, url, **kwargs):
                if "/points/delete" in url:
                    return httpx.Response(500, text="Internal Qdrant Error on Delete")
                return httpx.Response(200, json={"result": {}})

        with pytest.raises(RuntimeError):
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id="sec_reidx",
                qdrant_client=FailingDeleteQdrantClient(),
                embedder=FakeIngestionEngine(vector_dim=3),
                run_id=run_id,
            )

    async with SessionLocal() as check_session:
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        # Trạng thái bắt buộc không còn SEARCH_READY, không có search_ready_at
        assert ver_db.search_status == "INDEX_FAILED"
        assert ver_db.search_ready_at is None


# ==============================================================================
# Gate 15: Legacy Chunk Count Does Not Promote SEARCH_READY
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_chunk_count_without_manifest_does_not_promote_search_ready():
    """Khẳng định nếu Phase 2C thất bại manifest, dù legacy engine trả về chunk_count > 0,
    worker tuyệt đối không được ghi đè hoặc thăng hạng search_status thành SEARCH_READY.
    """
    await init_db()
    project_id = f"proj_chunk_{uuid.uuid4().hex[:8]}"
    source_id = f"src_chunk_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_id = f"job_chunk_{uuid.uuid4().hex[:8]}"

    q_mock = MockQdrantStorage(vector_dim=3)

    class IngestionEngineWithChunksAndFailStage(FakeIngestionEngine):
        async def process_document(self, *args, **kwargs):
            # Giả lập trả về outcome có chunk_count > 0
            return SimpleNamespace(chunk_count=15, event_count=2, token_usage=100, paused=False, source_id="src_dummy")

    # Giả lập Qdrant client trả về lỗi khi index Phase 2C
    class FailingQdrantIndexingClient:
        async def post(self, url, **kwargs):
            if "/points" in url:
                return httpx.Response(500, text="Qdrant node disk full")
            return httpx.Response(200, json={"result": {}})

        async def put(self, url, **kwargs):
            return httpx.Response(200, json={"result": True})

        async def get(self, url, **kwargs):
            return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"content_vector": {"size": 3}}}}}})

    with patch("sag_api.jobs.tasks.httpx.AsyncClient", lambda **kw: FailingQdrantIndexingClient()):
        async with SessionLocal() as session:
            await seed_ingestion_pipeline(
                session,
                source_id=source_id,
                doc_id=doc_id,
                ver_id=ver_id,
                run_id=run_id,
                job_id=job_id,
                project_id=project_id,
                file_path=Path("dummy.md"),
                security_partition_id="sec_chk",
            )
            job = await session.get(Job, job_id)
            with pytest.raises(Exception):
                await _process_document_unlocked(session, job, engine_manager=IngestionEngineWithChunksAndFailStage(vector_dim=3))

    async with SessionLocal() as check_session:
        ver_db = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        # Không được phép là SEARCH_READY
        assert ver_db.search_status == "INDEX_FAILED"
        assert ver_db.search_ready_at is None
        assert ver_db.status == "FAILED"


# ==============================================================================
# Gate 16: Legacy READY Alias Rejected Without Manifest
# ==============================================================================
@pytest.mark.asyncio
async def test_checkpoint_a_legacy_ready_alias_rejected_without_manifest():
    """Khẳng định bản ghi legacy có search_status='READY' nhưng chưa qua manifest
    (search_ready_at is None) bị từ chối search_ready=False tại status API.
    """
    await init_db()
    project_id = f"proj_leg_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="legacy.md", storage_path="/tmp/legacy.md", project_id=project_id, tenant_id="tenant_default")
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_leg",
            valid_from=datetime.now(UTC),
            status="READY",
            search_status="READY",  # Legacy alias chưa verified
            search_ready_at=None,
            metadata_json={"security_partition_id": "sec_leg"},
        )
        session.add(doc)
        await session.commit()
        session.add(ver)
        await session.commit()

        status_res = await get_document_version_status(session, project_id=project_id, document_id=doc_id, version_no=1)
        assert status_res.search_ready is False



