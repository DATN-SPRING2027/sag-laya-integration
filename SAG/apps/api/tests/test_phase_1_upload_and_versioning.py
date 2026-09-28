"""Comprehensive test suite for Phase 1: Upload, Versioning, Idempotency, and Status.

Covers:
- Strict Security Partition enforcement (400 on missing header)
- Mandatory Idempotency-Key header (400 on missing header)
- Fresh document upload and record generation (201 Created)
- Idempotent request retry with identical payload (200 OK, is_duplicate=True)
- Idempotent request conflict with different payload (409 Conflict)
- Version increment on same logical source (version_no=2 with supersedes link)
- Status query endpoint (200 OK with stage progress, 404 on not found)
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document
from sag_api.db.models.routing_rag import (
    DocumentVersion,
    IngestionRun,
    SourceSnapshot,
    StageRun,
)
from sag_api.main import app


@pytest.fixture(autouse=True)
async def setup_database():
    """Ensure database schema is ready before each test."""
    await init_db()


@pytest.mark.asyncio
async def test_upload_missing_security_partition_fails_with_400():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/projects/proj_test/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_123",
                "Idempotency-Key": "key_001",
                # Omit X-Continuum-Security-Partition
            },
            files={"file": ("test.txt", b"Hello World", "text/plain")},
        )
        assert res.status_code == 422 or res.status_code == 400


@pytest.mark.asyncio
async def test_upload_missing_idempotency_key_fails_with_400():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/projects/proj_test/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_123",
                "X-Continuum-Security-Partition": "public",
                # Omit Idempotency-Key
            },
            files={"file": ("test.txt", b"Hello World", "text/plain")},
        )
        assert res.status_code == 422 or res.status_code == 400


@pytest.mark.asyncio
async def test_upload_empty_file_fails_with_validation_error():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/projects/proj_test/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_123",
                "Idempotency-Key": "key_empty",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert res.status_code == 422
        assert "empty" in res.text.lower()


@pytest.mark.asyncio
async def test_upload_fresh_document_succeeds_and_creates_records():
    transport = httpx.ASGITransport(app=app)
    project_id = "proj_fresh_01"
    content = b"Knowledge routing architecture specification v1.1"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_architect",
                "Idempotency-Key": "idemp_fresh_01",
                "X-Continuum-Security-Partition": "team_backend",
            },
            data={"logical_source_id": "docs/architecture.md"},
            files={"file": ("architecture.md", content, "text/markdown")},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["is_duplicate"] is False
        assert data["version_no"] == 1
        assert data["status"] == "RECEIVED"
        assert data["search_status"] == "PENDING"
        assert data["knowledge_status"] == "NOT_STARTED"
        assert len(data["file_hash"]) == 64
        doc_id = data["document_id"]
        version_id = data["version_id"]
        run_id = data["run_id"]

    # Verify database persistence
    async with SessionLocal() as session:
        doc = await session.get(Document, doc_id)
        assert doc is not None
        assert doc.project_id == project_id
        assert doc.logical_source_id == "docs/architecture.md"

        ver = await session.get(DocumentVersion, version_id)
        assert ver is not None
        assert ver.document_id == doc_id
        assert ver.version_no == 1
        assert ver.status == "RECEIVED"
        assert ver.search_status == "PENDING"
        assert ver.knowledge_status == "NOT_STARTED"

        snapshot = (
            await session.execute(
                select(SourceSnapshot).where(SourceSnapshot.document_version_id == version_id)
            )
        ).scalar_one_or_none()
        assert snapshot is not None
        assert snapshot.byte_size == len(content)

        run = await session.get(IngestionRun, run_id)
        assert run is not None
        assert run.idempotency_key == "idemp_fresh_01"
        assert run.current_stage == "RECEIVE"

        stage_runs = (
            await session.execute(
                select(StageRun).where(StageRun.run_id == run_id)
            )
        ).scalars().all()
        assert len(stage_runs) >= 1
        assert stage_runs[0].stage == "receive"
        assert stage_runs[0].status == "SUCCESS"


@pytest.mark.asyncio
async def test_idempotent_retry_with_same_payload_returns_200_duplicate():
    transport = httpx.ASGITransport(app=app)
    project_id = "proj_retry_01"
    content = b"Deterministic payload content for retry verification"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First call: 201 Created
        res1 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_dev",
                "Idempotency-Key": "idemp_retry_key",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("guide.txt", content, "text/plain")},
        )
        assert res1.status_code == 201
        data1 = res1.json()
        assert data1["is_duplicate"] is False

        # Second call with identical payload: 200 OK duplicate
        res2 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_dev",
                "Idempotency-Key": "idemp_retry_key",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("guide.txt", content, "text/plain")},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["is_duplicate"] is True
        assert data2["document_id"] == data1["document_id"]
        assert data2["version_id"] == data1["version_id"]
        assert data2["run_id"] == data1["run_id"]
        assert data2["file_hash"] == data1["file_hash"]


@pytest.mark.asyncio
async def test_idempotency_key_reused_with_different_payload_returns_409_conflict():
    transport = httpx.ASGITransport(app=app)
    project_id = "proj_conflict_01"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # First call with payload A
        res1 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_dev",
                "Idempotency-Key": "shared_conflict_key",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("file1.txt", b"Payload Content Alpha", "text/plain")},
        )
        assert res1.status_code == 201

        # Second call with payload B using same key -> Conflict!
        res2 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_dev",
                "Idempotency-Key": "shared_conflict_key",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("file2.txt", b"Payload Content Beta (Different)", "text/plain")},
        )
        assert res2.status_code == 409
        err = res2.json()
        assert "IDEMPOTENCY_PAYLOAD_MISMATCH" in str(err) or "different file payload" in str(err).lower()


@pytest.mark.asyncio
async def test_versioning_increments_and_links_supersedes_id():
    transport = httpx.ASGITransport(app=app)
    project_id = "proj_versioning_01"
    logical_source = "specs/protocol.md"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Upload version 1
        res1 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_lead",
                "Idempotency-Key": "ver1_key",
                "X-Continuum-Security-Partition": "team_backend",
            },
            data={"logical_source_id": logical_source},
            files={"file": ("protocol.md", b"Protocol specification version 1.0", "text/markdown")},
        )
        assert res1.status_code == 201
        data1 = res1.json()
        assert data1["version_no"] == 1
        ver1_id = data1["version_id"]
        doc1_id = data1["document_id"]

        # Upload version 2 (different content, same logical_source_id)
        res2 = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_lead",
                "Idempotency-Key": "ver2_key",
                "X-Continuum-Security-Partition": "team_backend",
            },
            data={"logical_source_id": logical_source},
            files={"file": ("protocol.md", b"Protocol specification version 2.0 with breaking changes", "text/markdown")},
        )
        assert res2.status_code == 201
        data2 = res2.json()
        assert data2["document_id"] == doc1_id
        assert data2["version_no"] == 2
        ver2_id = data2["version_id"]

    # Verify supersedes linkage in database
    async with SessionLocal() as session:
        v2 = await session.get(DocumentVersion, ver2_id)
        assert v2 is not None
        assert v2.supersedes_id == ver1_id


@pytest.mark.asyncio
async def test_query_document_version_status_success_and_not_found():
    transport = httpx.ASGITransport(app=app)
    project_id = "proj_status_01"

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/projects/{project_id}/documents/upload",
            headers={
                "X-Continuum-User-Id": "user_test",
                "Idempotency-Key": "status_test_key",
                "X-Continuum-Security-Partition": "public",
            },
            files={"file": ("status_doc.txt", b"Testing status reporting", "text/plain")},
        )
        assert res.status_code == 201
        upload_data = res.json()
        doc_id = upload_data["document_id"]

        # Query status
        status_res = await client.get(
            f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status"
        )
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["document_id"] == doc_id
        assert status_data["version_no"] == 1
        assert status_data["status"] == "RECEIVED"
        assert status_data["search_status"] == "PENDING"
        assert status_data["knowledge_status"] == "NOT_STARTED"
        assert status_data["search_ready"] is False
        assert status_data["knowledge_ready"] is False
        assert status_data["current_stage"] == "RECEIVE"
        assert "receive" in status_data["stage_progress"]
        assert status_data["stage_progress"]["receive"]["status"] == "SUCCESS"

        # Query nonexistent version -> 404
        not_found_res = await client.get(
            f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/999/status"
        )
        assert not_found_res.status_code == 404
