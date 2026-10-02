"""Tests for Phase 2 worker execution, fail-closed security partition, error preservation, and safe resume handling."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import uuid
import httpx
import pytest
from sqlalchemy import select

from sag_api.core.db import SessionLocal, init_db
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.db.models import Document, Job, Source
from sag_api.db.models.routing_rag import DocumentVersion, IngestionRun, StageRun
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.tasks import _process_document_unlocked


class FakeEngineManager:
    async def get_sag_embedding(self, _config_id: str, _source=None):
        class FakeEmbedder:
            async def batch_generate(self, texts: list[str]) -> list[list[float]]:
                return [[0.1, 0.2, 0.3] for _ in texts]
        return FakeEmbedder()

    async def process_document(self, *args, **kwargs):
        return SimpleNamespace(
            paused=False,
            chunk_count=1,
            event_count=0,
            source_id="src_dummy",
            token_usage=100,
        )


@pytest.mark.asyncio
async def test_phase_2_fails_closed_when_security_partition_missing(tmp_path):
    """Kiểm tra nếu thiếu security_partition_id, worker bắt buộc fail-closed (ném lỗi, không mặc định public)."""
    await init_db()
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "test.md"
    md_file.write_text("# Tiêu đề\n\nNội dung văn bản.", encoding="utf-8")

    async with SessionLocal() as session:
        # Step 1: Parent Source
        source = Source(id=source_id, name="Test Source", sag_source_config_id="cfg_1")
        session.add(source)
        await session.commit()

        # Step 2: Parent Document
        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="test.md",
            storage_path=str(md_file),
            status=DocumentStatus.LOADING,
        )
        session.add(doc)
        await session.commit()

        # Step 3: DocumentVersion (THIẾU security_partition_id để test fail-closed)
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_p2",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={},  # Thiếu security_partition_id!
        )
        session.add(ver)
        await session.commit()

        # Step 4: IngestionRun & Job
        run = IngestionRun(
            id=run_id,
            tenant_id="tenant_1",
            project_id="proj_1",
            document_version_id=ver_id,
            idempotency_key="key_fc",
            payload_hash="hash_p2",
            status="RUNNING",
        )
        job = Job(
            id=job_id,
            type=JobType.PROCESS_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={"run_id": run_id, "storage_path": str(md_file)},
        )
        session.add_all([run, job])
        await session.commit()

        # Thực thi worker
        with pytest.raises(Exception) as exc_info:
            await _process_document_unlocked(
                session,
                job,
                engine_manager=FakeEngineManager(),
            )

        assert "Missing security_partition_id" in str(exc_info.value)
        assert "fail-closed" in str(exc_info.value)

    # Đọc lại từ DB bằng session mới để xác minh dữ liệu đã commit
    async with SessionLocal() as check_session:
        doc_updated = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_updated.status == DocumentStatus.FAILED
        assert doc_updated.error_layer == ErrorLayer.API.value
        assert doc_updated.error_stage == ErrorStage.EXTRACT.value

        run_updated = (await check_session.execute(select(IngestionRun).where(IngestionRun.id == run_id))).scalar_one()
        assert run_updated.status == "FAILED"
        assert run_updated.error_layer == ErrorLayer.API.value
        assert run_updated.error_stage == ErrorStage.EXTRACT.value


@pytest.mark.asyncio
async def test_phase_2_exception_propagates_and_marks_document_failed(tmp_path):
    """Kiểm tra exception trong Phase 2 không bị nuốt (swallowed), bảo toàn error_layer và error_stage."""
    await init_db()
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "valid.md"
    md_file.write_text("# Tiêu đề\n\nNội dung văn bản.", encoding="utf-8")

    class FailingEngineManager:
        async def get_sag_embedding(self, _config_id: str, _source=None):
            raise RuntimeError("Embedder connection refused (Qdrant/LLM down)")

        async def process_document(self, *args, **kwargs):
            return SimpleNamespace(paused=False)

    async with SessionLocal() as session:
        source = Source(id=source_id, name="Test Source", sag_source_config_id="cfg_2")
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="valid.md",
            storage_path=str(md_file),
            status=DocumentStatus.LOADING,
        )
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_valid",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "part_valid"},
        )
        session.add(ver)
        await session.commit()

        run = IngestionRun(
            id=run_id,
            tenant_id="tenant_1",
            project_id="proj_1",
            document_version_id=ver_id,
            idempotency_key="key_err_prop",
            payload_hash="hash_valid",
            status="RUNNING",
        )
        job = Job(
            id=job_id,
            type=JobType.PROCESS_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={"run_id": run_id, "storage_path": str(md_file)},
        )
        session.add_all([run, job])
        await session.commit()

        # Worker phải ném ngoại lệ khi Phase 2 fail (không nuốt lỗi)
        with pytest.raises(Exception) as exc_info:
            await _process_document_unlocked(
                session,
                job,
                engine_manager=FailingEngineManager(),
            )

        assert "Search indexing failed" in str(exc_info.value) or "Qdrant" in str(exc_info.value)

    # Đọc lại từ DB bằng session mới
    async with SessionLocal() as check_session:
        doc_updated = (await check_session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc_updated.status == DocumentStatus.FAILED
        assert doc_updated.error_layer == ErrorLayer.STORE.value
        assert doc_updated.error_stage == ErrorStage.PERSIST.value

        run_updated = (await check_session.execute(select(IngestionRun).where(IngestionRun.id == run_id))).scalar_one()
        assert run_updated.status == "FAILED"
        assert run_updated.error_layer == ErrorLayer.STORE.value
        assert run_updated.error_stage == ErrorStage.PERSIST.value


@pytest.mark.asyncio
async def test_prepared_none_resolution_does_not_decode_binary_as_utf8(tmp_path, monkeypatch):
    """Kiểm tra khi resume (checkpoint đã có chunk_ids, prepared ban đầu là None),

    worker không được đọc tệp nhị phân gốc (PDF/Word) bằng UTF-8 mà phải qua prepare_document để lấy markdown.
    """
    await init_db()
    from sag_api.parsing.service import PreparedDocument

    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    job_id = f"job_{uuid.uuid4().hex[:8]}"

    # Tạo tệp PDF giả định có chứa các byte nhị phân không thể giải mã utf-8 thuần
    raw_pdf = tmp_path / "document.pdf"
    raw_pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n")

    # Tạo tệp markdown đã phân tích (cache hoặc output của parser)
    parsed_md = tmp_path / "document.pdf.parsed.md"
    parsed_md.write_text("# Tài liệu PDF đã phân tích\n\nNội dung văn bản chuẩn.", encoding="utf-8")

    prepare_called = False

    async def mock_prepare_document(path, settings, **kwargs):
        nonlocal prepare_called
        prepare_called = True
        assert path == str(raw_pdf)
        return PreparedDocument(path=str(parsed_md), provider="markitdown")

    monkeypatch.setattr("sag_api.jobs.tasks.prepare_document", mock_prepare_document)

    # Mock Qdrant handler
    stored_points: list[dict[str, Any]] = []

    def mock_qdrant_handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": len(stored_points)}})
        if url_path.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": stored_points, "next_page_offset": None}})
        if request.method == "PUT" and "/points" in url_path:
            import json
            body = json.loads(request.content.decode("utf-8"))
            stored_points.extend(body.get("points", []))
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
        if request.method == "GET":
            return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"content_vector": {"size": 3}}}}}})
        return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_qdrant_handler),
        base_url="http://localhost:6333",
    )
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: mock_client)

    async with SessionLocal() as session:
        source = Source(id=source_id, name="Test Binary Source", sag_source_config_id="cfg_bin")
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="document.pdf",
            storage_path=str(raw_pdf),
            status=DocumentStatus.EXTRACTING,
        )
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_pdf",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "part_secure"},
        )
        session.add(ver)
        await session.commit()

        run = IngestionRun(
            id=run_id,
            tenant_id="tenant_1",
            project_id="proj_1",
            document_version_id=ver_id,
            idempotency_key="key_resume_bin",
            payload_hash="hash_pdf",
            status="RUNNING",
        )
        # Giả lập payload đã có chunk_ids (chạy tiếp từ checkpoint -> prepared ban đầu = None)
        job = Job(
            id=job_id,
            type=JobType.PROCESS_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={
                "run_id": run_id,
                "storage_path": str(raw_pdf),
                "chunks": ["chunk-1", "chunk-2"],  # checkpoint.chunk_ids is non-empty
            },
        )
        session.add_all([run, job])
        await session.commit()

        await _process_document_unlocked(
            session,
            job,
            engine_manager=FakeEngineManager(),
        )

    # Đảm bảo prepare_document được gọi để lấy parsed markdown an toàn thay vì đọc raw binary
    assert prepare_called is True

    # Xác minh Search readiness và canonical blocks được nạp thành công từ parsed markdown
    async with SessionLocal() as check_session:
        ver_updated = (await check_session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status in ("READY", "SEARCH_READY")
        assert ver_updated.search_ready_at is not None


@pytest.mark.asyncio
async def test_reprocess_document_task_creates_new_ingestion_run():
    """Kiểm tra reprocess document tạo IngestionRun mới và gán run_id vào process_job.payload (#7)."""
    await init_db()
    from sag_api.jobs.tasks import _reprocess_document_task_unlocked

    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    reprocess_job_id = f"job_rep_{uuid.uuid4().hex[:8]}"

    async with SessionLocal() as session:
        source = Source(id=source_id, name="Reprocess Source", sag_source_config_id="cfg_rep")
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            tenant_id="tenant_rep",
            project_id="proj_rep",
            filename="reprocess.md",
            storage_path="/tmp/rep.md",
            status=DocumentStatus.READY,
        )
        session.add(doc)

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_reprocess_123",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "sec_rep"},
        )
        session.add(ver)

        job = Job(
            id=reprocess_job_id,
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={},
        )
        session.add(job)
        await session.commit()

        class DummyEngine:
            async def delete_document_data(self, *args, **kwargs):
                pass

        await _reprocess_document_task_unlocked(session, job, engine_manager=DummyEngine())

    async with SessionLocal() as check_session:
        # Kiểm tra Job process mới được tạo
        process_job = (
            await check_session.execute(
                select(Job).where(Job.type == JobType.PROCESS_DOCUMENT, Job.document_id == doc_id)
            )
        ).scalar_one_or_none()
        assert process_job is not None
        assert "run_id" in process_job.payload
        new_run_id = process_job.payload["run_id"]

        # Kiểm tra IngestionRun tương ứng được tạo
        run = await check_session.get(IngestionRun, new_run_id)
        assert run is not None
        assert run.project_id == "proj_rep"
        assert run.tenant_id == "tenant_rep"
        assert run.document_version_id == ver_id
        assert run.status == "QUEUED"


@pytest.mark.asyncio
async def test_delete_document_task_deletes_all_versions_qdrant_points(monkeypatch):
    """Kiểm tra delete document thu thập tất cả DocumentVersion IDs và gọi xóa Qdrant points (#8)."""
    await init_db()
    from sag_api.jobs.tasks import _delete_document_task_unlocked

    doc_id = str(uuid.uuid4())
    ver_1_id = str(uuid.uuid4())
    ver_2_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    delete_job_id = f"job_del_{uuid.uuid4().hex[:8]}"

    deleted_versions: list[str] = []
    deleted_project: str | None = None

    async def mock_delete_document_points_from_qdrant(client, project_id, document_version_ids):
        nonlocal deleted_versions, deleted_project
        deleted_project = project_id
        deleted_versions.extend(document_version_ids)

    monkeypatch.setattr(
        "sag_api.services.search_index_service.delete_document_points_from_qdrant",
        mock_delete_document_points_from_qdrant,
    )

    async with SessionLocal() as session:
        source = Source(id=source_id, name="Delete Source", sag_source_config_id="cfg_del")
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            tenant_id="tenant_del",
            project_id="proj_del",
            filename="delete.md",
            storage_path="/tmp/del.md",
            status=DocumentStatus.READY,
        )
        session.add(doc)

        ver1 = DocumentVersion(
            id=ver_1_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_1",
            status="SEARCH_READY",
        )
        ver2 = DocumentVersion(
            id=ver_2_id,
            document_id=doc_id,
            version_no=2,
            file_hash="hash_2",
            status="SEARCH_READY",
        )
        session.add_all([ver1, ver2])

        job = Job(
            id=delete_job_id,
            type=JobType.DELETE_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={},
        )
        session.add(job)
        await session.commit()

        class DummyEngine:
            async def delete_document_data(self, *args, **kwargs):
                pass

        await _delete_document_task_unlocked(session, job, engine_manager=DummyEngine())

    assert deleted_project == "proj_del"
    assert set(deleted_versions) == {ver_1_id, ver_2_id}


@pytest.mark.asyncio
async def test_prepare_document_parse_paused_handled_safely(tmp_path, monkeypatch):
    """Kiểm tra khi prepare_document ném ParsePaused, worker xử lý pause/yield an toàn (#16)."""
    await init_db()
    from sag_api.jobs.control import JobPaused, JobYielded
    from sag_api.parsing import ParsePaused

    doc_id = str(uuid.uuid4())
    source_id = f"src_{uuid.uuid4().hex[:8]}"
    job_id = f"job_pause_{uuid.uuid4().hex[:8]}"

    md_file = tmp_path / "pause.md"
    md_file.write_text("# Test Pause\n\nNội dung chờ parse.", encoding="utf-8")

    async def mock_prepare_document_raises_paused(*args, **kwargs):
        raise ParsePaused()

    monkeypatch.setattr("sag_api.jobs.tasks.prepare_document", mock_prepare_document_raises_paused)

    async with SessionLocal() as session:
        source = Source(id=source_id, name="Pause Source", sag_source_config_id="cfg_pause")
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="pause.md",
            storage_path=str(md_file),
            status=DocumentStatus.LOADING,
        )
        session.add(doc)

        job = Job(
            id=job_id,
            type=JobType.PROCESS_DOCUMENT,
            source_id=source_id,
            document_id=doc_id,
            status=JobStatus.RUNNING,
            payload={"storage_path": str(md_file)},
        )
        session.add(job)
        await session.commit()

        with pytest.raises((JobPaused, JobYielded)):
            await _process_document_unlocked(session, job, engine_manager=FakeEngineManager())
