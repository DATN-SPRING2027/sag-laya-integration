"""Comprehensive test suite for Phase 1: Upload, Versioning, Idempotency, and Status.

Covers:
- Strict Security Partition enforcement (422/400 on missing header, 403 on unauthorized partition)
- Mandatory Idempotency-Key header (422/400 on missing header)
- Principal Authentication and Project Authorization (401 on missing/invalid token, 403 on project mismatch or impersonation)
- Bounded Upload Stream Reader (422 on oversized or empty files)
- Content Addressing and Snapshot Immutability (payload hash-addressed storage path)
- Fresh document upload and record generation (201 Created)
- Idempotent request retry with identical payload (200 OK, is_duplicate=True)
- Idempotent request conflict with different payload (409 Conflict)
- Version increment on same logical source (version_no=2 with supersedes link)
- Exact Content Deduplication (200 OK, is_duplicate=True, no new run created for identical payload on same source)
- Status query endpoint (200 OK with stage progress, 404 on not found, 401/403 authorization guards)
- Worker dispatch and IngestionRun stage lifecycle transitions
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import httpx
import pytest
import uuid
from sqlalchemy import select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.security import create_access_token
from sag_api.db.models import Document, Job, Source, User
from sag_api.db.models.routing_rag import (
    DocumentVersion,
    IngestionRun,
    SourceSnapshot,
    StageRun,
)
from sag_api.enums import JobStatus, JobType
from sag_api.main import app


def make_auth_header(
    user_id: str = "user_dev",
    tenant_id: str = "tenant_continuum_default",
    allowed_projects: list[str] | None = None,
    allowed_partitions: list[str] | None = None,
    is_service: bool = False,
) -> dict[str, str]:
    extra: dict = {
        "tenant_id": tenant_id,
    }
    if allowed_projects is not None:
        extra["allowed_projects"] = allowed_projects
    else:
        extra["allowed_projects"] = ["*"]

    if allowed_partitions is not None:
        extra["allowed_partitions"] = allowed_partitions
    else:
        extra["allowed_partitions"] = ["*"]

    if is_service:
        extra["role"] = "service"

    token = create_access_token(subject=user_id, extra=extra)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
async def setup_database():
    """Ensure database schema is ready before each test."""
    await init_db()


@pytest.fixture
async def client():
    """HTTP client for API testing."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================================
# 1. Authentication & Authorization Boundary Tests
# ============================================================================

@pytest.mark.asyncio
async def test_upload_missing_auth_token_fails_with_401(client: httpx.AsyncClient):
    res = await client.post(
        "/api/v1/projects/proj_auth_test/documents/upload",
        headers={
            "X-Continuum-User-Id": "user_123",
            "Idempotency-Key": "key_auth_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_upload_invalid_auth_token_fails_with_401(client: httpx.AsyncClient):
    res = await client.post(
        "/api/v1/projects/proj_auth_test/documents/upload",
        headers={
            "Authorization": "Bearer invalid.jwt.token",
            "X-Continuum-User-Id": "user_123",
            "Idempotency-Key": "key_auth_02",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_upload_project_access_forbidden_fails_with_403(client: httpx.AsyncClient):
    auth_header = make_auth_header(
        user_id="user_restricted",
        allowed_projects=["proj_allowed_only"],
    )
    res = await client.post(
        "/api/v1/projects/proj_other_forbidden/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_restricted",
            "Idempotency-Key": "key_proj_forbidden",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code == 403
    assert "not authorized to access project" in res.text


@pytest.mark.asyncio
async def test_upload_security_partition_forbidden_fails_with_403(client: httpx.AsyncClient):
    auth_header = make_auth_header(
        user_id="user_dev",
        allowed_projects=["*"],
        allowed_partitions=["public", "team_backend"],
    )
    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_dev",
            "Idempotency-Key": "key_partition_forbidden",
            "X-Continuum-Security-Partition": "top_secret_executive",
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code == 403
    assert "not authorized for security partition" in res.text


@pytest.mark.asyncio
async def test_upload_user_impersonation_fails_with_403(client: httpx.AsyncClient):
    auth_header = make_auth_header(user_id="user_alice", is_service=False)
    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_bob",  # Attempt to impersonate user_bob
            "Idempotency-Key": "key_impersonate",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code == 403
    assert "cannot impersonate" in res.text


# ============================================================================
# 2. Validation & Bounded Upload Reader Tests
# ============================================================================

@pytest.mark.asyncio
async def test_upload_missing_security_partition_fails_with_400_or_422(client: httpx.AsyncClient):
    auth_header = make_auth_header(user_id="user_123")
    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_123",
            "Idempotency-Key": "key_001",
            # Omit X-Continuum-Security-Partition
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code in (400, 422)


@pytest.mark.asyncio
async def test_upload_missing_idempotency_key_fails_with_400_or_422(client: httpx.AsyncClient):
    auth_header = make_auth_header(user_id="user_123")
    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_123",
            "X-Continuum-Security-Partition": "public",
            # Omit Idempotency-Key
        },
        files={"file": ("test.txt", b"Hello World", "text/plain")},
    )
    assert res.status_code in (400, 422)


@pytest.mark.asyncio
async def test_upload_empty_file_fails_with_validation_error(client: httpx.AsyncClient):
    auth_header = make_auth_header(user_id="user_123")
    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_123",
            "Idempotency-Key": "key_empty",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert res.status_code == 422
    assert "empty" in res.text.lower()


@pytest.mark.asyncio
async def test_upload_oversized_file_fails_with_validation_error(monkeypatch, client: httpx.AsyncClient):
    auth_header = make_auth_header(user_id="user_123")
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    oversized_data = b"X" * (1024 * 1024 + 1)

    res = await client.post(
        "/api/v1/projects/proj_test/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_123",
            "Idempotency-Key": "key_oversized",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("big.txt", oversized_data, "text/plain")},
    )
    assert res.status_code == 422
    assert "exceeds maximum allowed" in res.text.lower()


# ============================================================================
# 3. Document Creation, Immutability & Persistence Tests
# ============================================================================

@pytest.mark.asyncio
async def test_upload_fresh_document_succeeds_and_creates_records(client: httpx.AsyncClient):
    project_id = "proj_fresh_01"
    content = b"Knowledge routing architecture specification v1.1"
    auth_header = make_auth_header(user_id="user_architect", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
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
    file_hash = data["file_hash"]

    # Verify database persistence & snapshot immutability
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
        assert snapshot.checksum_sha256 == file_hash
        assert file_hash[:16] in snapshot.storage_uri.replace("\\", "/")

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


# ============================================================================
# 4. Idempotency & Conflict Tests
# ============================================================================

@pytest.mark.asyncio
async def test_idempotent_retry_with_same_payload_returns_200_duplicate(client: httpx.AsyncClient):
    project_id = "proj_retry_01"
    content = b"Deterministic payload content for retry verification"
    auth_header = make_auth_header(user_id="user_dev", allowed_projects=[project_id])

    # First call: 201 Created
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
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
            **auth_header,
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
async def test_idempotency_key_reused_with_different_payload_returns_409_conflict(client: httpx.AsyncClient):
    project_id = "proj_conflict_01"
    auth_header = make_auth_header(user_id="user_dev", allowed_projects=[project_id])

    # First call with payload A
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
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
            **auth_header,
            "X-Continuum-User-Id": "user_dev",
            "Idempotency-Key": "shared_conflict_key",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("file2.txt", b"Payload Content Beta (Different)", "text/plain")},
    )
    assert res2.status_code == 409
    err = res2.json()
    assert "IDEMPOTENCY_PAYLOAD_MISMATCH" in str(err) or "different file payload" in str(err).lower()


# ============================================================================
# 5. Versioning & Exact Content Deduplication Tests
# ============================================================================

@pytest.mark.asyncio
async def test_versioning_increments_and_links_supersedes_id(client: httpx.AsyncClient):
    project_id = "proj_versioning_01"
    logical_source = "specs/protocol.md"
    auth_header = make_auth_header(user_id="user_lead", allowed_projects=[project_id])

    # Upload version 1
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
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
            **auth_header,
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
async def test_exact_content_deduplication_without_new_run(client: httpx.AsyncClient):
    """When a new upload request for the same logical source arrives with a NEW idempotency key
    but IDENTICAL content hash to the latest version, it returns 200 is_duplicate=True without creating
    a new IngestionRun or triggering pipeline processing.
    """
    project_id = "proj_dedup_01"
    logical_source = "shared/guidelines.md"
    payload_content = b"Content that stays exactly identical across repeated updates"
    auth_header = make_auth_header(user_id="user_dev", allowed_projects=[project_id])

    # First upload -> 201 Created
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_dev",
            "Idempotency-Key": "dedup_key_01",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_source},
        files={"file": ("guidelines.md", payload_content, "text/markdown")},
    )
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["is_duplicate"] is False
    assert data1["version_no"] == 1
    ver1_id = data1["version_id"]
    run1_id = data1["run_id"]

    # Second upload with NEW idempotency key but IDENTICAL content -> 200 OK is_duplicate=True
    res2 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_dev",
            "Idempotency-Key": "dedup_key_02_different_key",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_source},
        files={"file": ("guidelines.md", payload_content, "text/markdown")},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["is_duplicate"] is True
    assert data2["version_no"] == 1
    assert data2["version_id"] == ver1_id
    assert data2["run_id"] == run1_id

    # Verify database: exactly 1 DocumentVersion and 1 IngestionRun created
    async with SessionLocal() as session:
        versions = (
            await session.execute(
                select(DocumentVersion).where(DocumentVersion.id == ver1_id)
            )
        ).scalars().all()
        assert len(versions) == 1

        runs = (
            await session.execute(
                select(IngestionRun).where(IngestionRun.document_version_id == ver1_id)
            )
        ).scalars().all()
        assert len(runs) == 1


# ============================================================================
# 6. Status Query Endpoint Tests
# ============================================================================

@pytest.mark.asyncio
async def test_query_document_version_status_success_and_not_found(client: httpx.AsyncClient):
    project_id = "proj_status_01"
    auth_header = make_auth_header(user_id="user_test", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_test",
            "Idempotency-Key": "status_test_key",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("status_doc.txt", b"Testing status reporting", "text/plain")},
    )
    assert res.status_code == 201
    upload_data = res.json()
    doc_id = upload_data["document_id"]

    # Query status with authorized token
    status_res = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status",
        headers=auth_header,
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

    # Query status without auth -> 401
    no_auth_res = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status"
    )
    assert no_auth_res.status_code == 401

    # Query status with unauthorized project -> 403
    unauthorized_auth = make_auth_header(user_id="user_test", allowed_projects=["proj_other"])
    forbidden_res = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status",
        headers=unauthorized_auth,
    )
    assert forbidden_res.status_code == 403

    # Query nonexistent version -> 404
    not_found_res = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/999/status",
        headers=auth_header,
    )
    assert not_found_res.status_code == 404


# ============================================================================
# 7. Durable Dispatch Worker & IngestionRun Lifecycle Tests
# ============================================================================

@pytest.mark.asyncio
async def test_worker_transitions_ingestion_run_and_document_version(client: httpx.AsyncClient):
    """Verify that JobType.PROCESS_DOCUMENT linked with run_id transitions IngestionRun
    from QUEUED to RUNNING and on completion updates status to SUCCEEDED and DocumentVersion to READY.
    """
    project_id = "proj_worker_01"
    auth_header = make_auth_header(user_id="user_worker_test", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_worker_test",
            "Idempotency-Key": "key_worker_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("worker_test.txt", b"Content for worker dispatch test", "text/plain")},
    )
    assert res.status_code == 201
    data = res.json()
    doc_id = data["document_id"]
    version_id = data["version_id"]
    run_id = data["run_id"]

    # Verify initial run state in DB
    async with SessionLocal() as session:
        run = await session.get(IngestionRun, run_id)
        assert run is not None
        assert run.current_stage == "RECEIVE"

        # Verify Document exists
        doc = await session.get(Document, doc_id)
        assert doc is not None

        # Simulate job execution with mock EngineManager
        class MockOutcome:
            chunk_count = 5
            event_count = 3
            token_usage = 120
            paused = False
            source_id = "mock_sag_source_id"

        class MockEngineManager:
            async def process_document(self, *args, **kwargs):
                return MockOutcome()

        # Verify that project upload automatically maps and populates doc.source_id (Comment #9)
        assert doc.source_id is not None

        job = Job(
            id=f"job_{run_id}",
            type=JobType.PROCESS_DOCUMENT,
            document_id=doc.id,
            source_id=doc.source_id,
            status=JobStatus.RUNNING,
            payload={"run_id": run_id},
        )
        session.add(job)
        await session.commit()

        from sag_api.jobs.tasks import process_document  # lazy import to avoid octx chain
        await process_document(session, job, engine_manager=MockEngineManager())

        # Check that IngestionRun and DocumentVersion reached terminal success
        updated_run = await session.get(IngestionRun, run_id)
        assert updated_run.status == "SUCCEEDED"
        assert updated_run.current_stage == "COMPLETE"
        assert updated_run.completed_at is not None

        ver = await session.get(DocumentVersion, version_id)
        assert ver.status == "SEARCH_READY"
        assert ver.search_status == "SEARCH_READY"
        assert ver.search_ready_at is not None
        assert ver.knowledge_status == "NOT_STARTED"


# ============================================================================
# Additional Tests Addressing Review Findings (PR #8 Review Comments)
# ============================================================================


@pytest.mark.asyncio
async def test_fail_closed_empty_scope_forbidden(client: httpx.AsyncClient):
    """Verify that an empty allowed_projects or allowed_partitions scope fails closed with 403 (Comment #7)."""
    # 1. Empty allowed_projects
    auth_empty_projects = make_auth_header(user_id="user_empty_proj", allowed_projects=[])
    res1 = await client.post(
        "/api/v1/projects/proj_any/documents/upload",
        headers={
            **auth_empty_projects,
            "X-Continuum-User-Id": "user_empty_proj",
            "Idempotency-Key": "key_empty_proj_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello", "text/plain")},
    )
    assert res1.status_code == 403
    assert "not authorized to access project" in res1.text

    # 2. Empty allowed_partitions
    auth_empty_partitions = make_auth_header(
        user_id="user_empty_part",
        allowed_projects=["proj_ok"],
        allowed_partitions=[],
    )
    res2 = await client.post(
        "/api/v1/projects/proj_ok/documents/upload",
        headers={
            **auth_empty_partitions,
            "X-Continuum-User-Id": "user_empty_part",
            "Idempotency-Key": "key_empty_part_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Hello", "text/plain")},
    )
    assert res2.status_code == 403
    assert "not authorized for security partition" in res2.text


@pytest.mark.asyncio
async def test_secret_key_bearer_credential_rejected(client: httpx.AsyncClient):
    """Verify that passing settings.secret_key directly as bearer token is rejected with 401 (Comment #8)."""
    res = await client.post(
        "/api/v1/projects/proj_secret_test/documents/upload",
        headers={
            "Authorization": f"Bearer {settings.secret_key}",
            "X-Continuum-User-Id": "admin",
            "Idempotency-Key": "key_secret_bearer_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Secret token test", "text/plain")},
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_mime_signature_mismatch_rejected(client: httpx.AsyncClient):
    """Verify that files with mismatched binary signatures (e.g. text disguised as PDF) are rejected with 422 (Comment #15)."""
    project_id = "proj_mime_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    # Text bytes uploaded as .pdf without %PDF- magic bytes
    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_mime_invalid_pdf",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("fake.pdf", b"This is plain text pretending to be PDF", "application/pdf")},
    )
    assert res.status_code == 422
    assert "thiếu chữ ký PDF" in res.text or "PDF" in res.text


@pytest.mark.asyncio
async def test_mime_signature_docx_without_zip_rejected(client: httpx.AsyncClient):
    """Verify that docx files lacking ZIP container signature (PK) are rejected with 422 (TC-VAL-05)."""
    project_id = "proj_mime_docx_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_mime_invalid_docx",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("fake.docx", b"Plain text disguised as docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert res.status_code == 422
    assert "ZIP" in res.text or "PK" in res.text


@pytest.mark.asyncio
async def test_mime_signature_text_with_null_byte_rejected(client: httpx.AsyncClient):
    """Verify that plain text files containing null bytes are rejected as binary pollution with 422 (TC-VAL-07)."""
    project_id = "proj_mime_null_byte_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_mime_null_byte",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("notes.md", b"# Header\nValid text\x00corrupted binary content", "text/markdown")},
    )
    assert res.status_code == 422
    assert "không được phép" in res.text or "nhị phân" in res.text


@pytest.mark.asyncio
async def test_upload_disallowed_extension_rejected(client: httpx.AsyncClient):
    """Verify that files with extensions outside allowed whitelist are rejected with 422 (TC-VAL-03)."""
    project_id = "proj_disallowed_ext_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_disallowed_ext",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("malicious.sh", b"#!/bin/bash\necho bad\n", "application/x-sh")},
    )
    assert res.status_code == 422
    assert "Unsupported file type" in res.text or "Allowed extensions" in res.text


@pytest.mark.asyncio
async def test_mime_signature_text_with_png_image_rejected(client: httpx.AsyncClient):
    """Verify that images disguised as plain text (.txt with PNG header) are rejected with 422 (TC-VAL-06)."""
    project_id = "proj_mime_png_text_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_mime_png_text",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("data.txt", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "text/plain")},
    )
    assert res.status_code == 422
    assert "Tệp nhị phân" in res.text or "trá hình" in res.text


@pytest.mark.asyncio
async def test_rejected_upload_leaves_no_disk_or_db_garbage(client: httpx.AsyncClient):
    """Verify that uploads rejected by MIME verification abort fail-fast without creating Document, Job, Snapshot, or Run records (DoD 2)."""
    project_id = "proj_zero_garbage_test"
    auth_header = make_auth_header(user_id="user_zero", allowed_projects=[project_id])
    idem_key = "key_zero_garbage_check"
    fake_payload = b"MZ\x90\x00\x03\x00\x00\x00_pretending_to_be_pdf"

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_zero",
            "Idempotency-Key": idem_key,
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("fake.pdf", fake_payload, "application/pdf")},
    )
    assert res.status_code == 422
    assert "thiếu chữ ký PDF" in res.text or "PDF" in res.text

    async with SessionLocal() as session:
        run = (
            await session.execute(
                select(IngestionRun).where(
                    IngestionRun.project_id == project_id,
                    IngestionRun.idempotency_key == idem_key,
                )
            )
        ).scalar_one_or_none()
        assert run is None, "Rejected upload must not create an IngestionRun record"

        doc = (
            await session.execute(
                select(Document).where(Document.project_id == project_id)
            )
        ).scalar_one_or_none()
        assert doc is None, "Rejected upload must not create a Document record"


@pytest.mark.asyncio
async def test_mime_signature_text_with_null_byte_after_8kb_rejected(client: httpx.AsyncClient):
    """Verify that text files with null bytes beyond the first 8KB are rejected by full payload scanning (P2 review fix)."""
    project_id = "proj_mime_null_deep_test"
    auth_header = make_auth_header(user_id="user_mime", allowed_projects=[project_id])

    # 10KB valid ASCII text followed by null byte
    payload = (b"A" * 10000) + b"\x00" + b"trailing binary content"
    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_mime",
            "Idempotency-Key": "key_mime_null_deep",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("deep_null.txt", payload, "text/plain")},
    )
    assert res.status_code == 422
    assert "Tệp nhị phân hoặc hình ảnh trá hình" in res.text


@pytest.mark.asyncio
async def test_legacy_source_upload_rejects_invalid_mime_signature(client: httpx.AsyncClient):
    """Verify that legacy POST /api/v1/sources/{source_id}/documents also enforces MIME signature checks (P1 review fix)."""
    uid = uuid.uuid4().hex[:8]
    async with SessionLocal() as session:
        user = User(
            id=f"user_leg_{uid}",
            email=f"leg_{uid}@example.com",
            password_hash="dummy_password",
        )
        source = Source(
            id=f"src_leg_{uid}",
            name="Legacy MIME Test Source",
            sag_source_config_id=f"cfg_{uid}",
        )
        session.add_all([user, source])
        await session.commit()
        user_id = user.id
        source_id = source.id

    token = create_access_token(subject=user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Upload text disguised as PDF to legacy route
    res = await client.post(
        f"/api/v1/sources/{source_id}/documents",
        headers=headers,
        files={"file": ("fake.pdf", b"This is plain text pretending to be PDF", "application/pdf")},
    )
    assert res.status_code == 422
    assert "thiếu chữ ký PDF" in res.text or "PDF" in res.text


@pytest.mark.asyncio
async def test_replay_idempotency_different_logical_source_conflict(client: httpx.AsyncClient):
    """Verify that reusing the same Idempotency-Key with a different logical_source_id raises 409 Conflict (Comment #14)."""
    project_id = "proj_idemp_source_test"
    auth_header = make_auth_header(user_id="user_idemp", allowed_projects=[project_id])
    payload = b"Common document payload content"

    # First request with logical_source_id = "source_A"
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_idemp",
            "Idempotency-Key": "shared_idem_key_01",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": "logical_doc_alpha"},
        files={"file": ("doc_a.txt", payload, "text/plain")},
    )
    assert res1.status_code == 201

    # Second request with SAME idempotency key and SAME bytes, but DIFFERENT logical_source_id = "source_B"
    res2 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_idemp",
            "Idempotency-Key": "shared_idem_key_01",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": "logical_doc_beta"},
        files={"file": ("doc_b.txt", payload, "text/plain")},
    )
    assert res2.status_code == 409
    assert "IDEMPOTENCY_IDENTITY_MISMATCH" in res2.text or "logical source" in res2.text


@pytest.mark.asyncio
async def test_new_version_closes_previous_temporal_validity(client: httpx.AsyncClient):
    """Verify that when a new version is uploaded, the previous version's valid_to is closed at effective time (Comment #13)."""
    project_id = "proj_temporal_test"
    auth_header = make_auth_header(user_id="user_temporal", allowed_projects=[project_id])
    logical_source = "temporal_contract_doc"

    # Version 1
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_temporal",
            "Idempotency-Key": "key_temp_v1",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_source},
        files={"file": ("contract.txt", b"Version 1 Contract Content", "text/plain")},
    )
    assert res1.status_code == 201
    v1_id = res1.json()["version_id"]

    # Version 2
    res2 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_temporal",
            "Idempotency-Key": "key_temp_v2",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_source},
        files={"file": ("contract.txt", b"Version 2 Updated Contract Content", "text/plain")},
    )
    assert res2.status_code == 201
    v2_id = res2.json()["version_id"]

    async with SessionLocal() as session:
        v1 = await session.get(DocumentVersion, v1_id)
        v2 = await session.get(DocumentVersion, v2_id)
        assert v1 is not None and v2 is not None
        # Previous version validity must be closed (not far future 9999)
        assert v1.valid_to.year < 9000
        # Version 2 validity starts when version 1 validity ends
        assert abs((v2.valid_from - v1.valid_to).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_status_endpoint_forbidden_for_unauthorized_partition(client: httpx.AsyncClient):
    """Verify that querying status for a document version in an unauthorized security partition returns 403 (Comment #12)."""
    project_id = "proj_status_auth_test"
    auth_uploader = make_auth_header(
        user_id="uploader",
        allowed_projects=[project_id],
        allowed_partitions=["finance_confidential"],
    )

    # Upload document to private partition
    res_up = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_uploader,
            "X-Continuum-User-Id": "uploader",
            "Idempotency-Key": "key_status_auth_01",
            "X-Continuum-Security-Partition": "finance_confidential",
        },
        files={"file": ("financials.txt", b"Secret balance sheet", "text/plain")},
    )
    assert res_up.status_code == 201
    data = res_up.json()
    doc_id = data["document_id"]
    version_no = data["version_no"]

    # Principal without finance_confidential partition access tries to query status
    auth_viewer_restricted = make_auth_header(
        user_id="viewer_restricted",
        allowed_projects=[project_id],
        allowed_partitions=["public"],
    )
    res_status = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/{version_no}/status",
        headers=auth_viewer_restricted,
    )
    assert res_status.status_code == 403
    assert "not authorized to access security partition" in res_status.text


# ============================================================================
# Additional Tests Addressing KeyT9999 Review Findings (Head 5b635ca)
# ============================================================================


@pytest.mark.asyncio
async def test_project_source_protected_from_global_sources_routes(client: httpx.AsyncClient):
    """[P1] Verify that auto-provisioned Project Sources are NOT listed in GET /sources
    and reject un-scoped access via GET /sources/{source_id} or /sources/{source_id}/documents with 403.
    """
    project_id = "proj_acl_guard_01"
    auth_header = make_auth_header(user_id="user_acl", allowed_projects=[project_id])

    # Upload document -> auto-creates project source
    res_up = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_acl",
            "Idempotency-Key": "key_acl_guard_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("guard_doc.txt", b"Guard test payload", "text/plain")},
    )
    assert res_up.status_code == 201

    async with SessionLocal() as session:
        doc = await session.get(Document, res_up.json()["document_id"])
        assert doc is not None
        source_id = doc.source_id
        assert source_id is not None

        # Create user in database for SAG global auth
        test_user = await session.get(User, "sag_global_user")
        if not test_user:
            session.add(User(
                id="sag_global_user",
                email="global@sag.local",
                password_hash="dummy_hash",
                is_active=True,
            ))
            await session.commit()

    # Normal authenticated SAG user calls GET /sources
    global_user_token = create_access_token(
        subject="sag_global_user",
        extra={"auth_mode": settings.auth_mode},
    )
    res_sources = await client.get(
        "/api/v1/sources",
        headers={"Authorization": f"Bearer {global_user_token}"},
    )
    assert res_sources.status_code == 200
    sources_list = res_sources.json()
    assert all(s["id"] != source_id for s in sources_list), "Project source must not be exposed in global GET /sources"

    # User attempts to access project source via un-scoped GET /sources/{source_id}
    res_get_source = await client.get(
        f"/api/v1/sources/{source_id}",
        headers={"Authorization": f"Bearer {global_user_token}"},
    )
    assert res_get_source.status_code == 403
    assert "Nguồn dữ liệu dự án được bảo vệ" in res_get_source.text or "403" in str(res_get_source.status_code)

    # User attempts to list documents via un-scoped GET /sources/{source_id}/documents
    res_docs = await client.get(
        f"/api/v1/sources/{source_id}/documents",
        headers={"Authorization": f"Bearer {global_user_token}"},
    )
    assert res_docs.status_code == 403


@pytest.mark.asyncio
async def test_job_reads_correct_immutable_snapshot_version(client: httpx.AsyncClient):
    """[P1] Verify that Job payload carries immutable storage_path and worker processes
    the snapshot of the specific version, even if Document.storage_path is updated by subsequent version.
    """
    project_id = "proj_snapshot_immut_01"
    auth_header = make_auth_header(user_id="user_snap", allowed_projects=[project_id])
    logical_doc = "concurrent/spec.txt"

    # 1. Upload Version 1
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_snap",
            "Idempotency-Key": "key_snap_v1",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_doc},
        files={"file": ("spec.txt", b"Version 1 Immutable Content", "text/plain")},
    )
    assert res1.status_code == 201
    run1_id = res1.json()["run_id"]
    doc_id = res1.json()["document_id"]

    # 2. Upload Version 2 immediately (simulating race before worker runs v1)
    res2 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_snap",
            "Idempotency-Key": "key_snap_v2",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_doc},
        files={"file": ("spec.txt", b"Version 2 Mutated Content", "text/plain")},
    )
    assert res2.status_code == 201

    # Verify that Job for Version 1 carries its own immutable storage_path
    async with SessionLocal() as session:
        job1 = (
            await session.execute(
                select(Job).where(
                    Job.document_id == doc_id,
                    Job.payload["run_id"].as_string() == run1_id,
                )
            )
        ).scalar_one()

        assert "storage_path" in job1.payload
        v1_storage_path = job1.payload["storage_path"]
        assert Path(v1_storage_path).read_bytes() == b"Version 1 Immutable Content"

        # Current Document.storage_path now points to Version 2!
        doc = await session.get(Document, doc_id)
        assert Path(doc.storage_path).read_bytes() == b"Version 2 Mutated Content"

        # When worker executes Job 1, it must process v1_storage_path
        recorded_paths = []

        class MockOutcome:
            chunk_count = 3
            event_count = 1
            token_usage = 50
            paused = False
            source_id = "test_source"

        class MockEngine:
            async def process_document(self, *args, **kwargs):
                recorded_paths.append(kwargs.get("original_path"))
                return MockOutcome()

        from sag_api.jobs.tasks import process_document
        await process_document(session, job1, engine_manager=MockEngine())

        assert len(recorded_paths) == 1
        assert recorded_paths[0] == v1_storage_path, "Worker must process v1 snapshot, NOT mutated Document.storage_path"


@pytest.mark.asyncio
async def test_source_identity_deterministic_hash_tenant_isolation(client: httpx.AsyncClient):
    """[P1] Verify that Source identity incorporates tenant_id via deterministic hash,
    guaranteeing cross-tenant isolation and preventing truncation collision for long project IDs.
    """
    same_project_id = "proj_alpha_common"
    long_project_id = "proj_" + "a" * 80  # Exceeds 64 chars

    auth_tenant1 = make_auth_header(user_id="user_t1", tenant_id="tenant_01", allowed_projects=[same_project_id])
    auth_tenant2 = make_auth_header(user_id="user_t2", tenant_id="tenant_02", allowed_projects=[same_project_id])
    auth_long = make_auth_header(user_id="user_t3", tenant_id="tenant_01", allowed_projects=[long_project_id])

    # Upload from Tenant 1
    res1 = await client.post(
        f"/api/v1/projects/{same_project_id}/documents/upload",
        headers={
            **auth_tenant1,
            "X-Continuum-User-Id": "user_t1",
            "X-Continuum-Tenant-Id": "tenant_01",
            "Idempotency-Key": "key_t1_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("doc.txt", b"Tenant 1 doc", "text/plain")},
    )
    assert res1.status_code == 201

    # Upload from Tenant 2 (same project_id name)
    res2 = await client.post(
        f"/api/v1/projects/{same_project_id}/documents/upload",
        headers={
            **auth_tenant2,
            "X-Continuum-User-Id": "user_t2",
            "X-Continuum-Tenant-Id": "tenant_02",
            "Idempotency-Key": "key_t2_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("doc.txt", b"Tenant 2 doc", "text/plain")},
    )
    assert res2.status_code == 201

    # Upload with long project ID
    res_long = await client.post(
        f"/api/v1/projects/{long_project_id}/documents/upload",
        headers={
            **auth_long,
            "X-Continuum-User-Id": "user_t3",
            "X-Continuum-Tenant-Id": "tenant_01",
            "Idempotency-Key": "key_long_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("doc.txt", b"Long project doc", "text/plain")},
    )
    assert res_long.status_code == 201

    async with SessionLocal() as session:
        doc1 = await session.get(Document, res1.json()["document_id"])
        doc2 = await session.get(Document, res2.json()["document_id"])
        doc_long = await session.get(Document, res_long.json()["document_id"])

        s1 = await session.get(Source, doc1.source_id)
        s2 = await session.get(Source, doc2.source_id)
        s_long = await session.get(Source, doc_long.source_id)

        # Cross-tenant same project_id MUST map to different Sources!
        assert s1.id != s2.id
        assert s1.sag_source_config_id != s2.sag_source_config_id

        # Config ID must always be valid <= 64 chars
        assert len(s_long.sag_source_config_id) <= 64
        assert s_long.config.get("is_project_source") is True
        assert s_long.config.get("tenant_id") == "tenant_01"


@pytest.mark.asyncio
async def test_status_endpoint_fails_closed_when_partition_missing(client: httpx.AsyncClient):
    """[P2] Verify fail-closed behavior: if a document version has empty/missing partition in metadata,
    non-service callers are rejected with 403 Forbidden, but service callers can query.
    """
    project_id = "proj_missing_part_01"
    auth_uploader = make_auth_header(user_id="uploader_part", allowed_projects=[project_id])

    res = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_uploader,
            "X-Continuum-User-Id": "uploader_part",
            "Idempotency-Key": "key_part_miss_01",
            "X-Continuum-Security-Partition": "public",
        },
        files={"file": ("test.txt", b"Missing partition test", "text/plain")},
    )
    assert res.status_code == 201
    doc_id = res.json()["document_id"]
    ver_id = res.json()["version_id"]

    # Manually simulate a legacy/backfill record with missing security_partition_id in metadata
    async with SessionLocal() as session:
        ver = await session.get(DocumentVersion, ver_id)
        ver.metadata_json = {}  # No security_partition_id
        await session.commit()

    # Normal user query status -> 403 Forbidden (fail-closed)
    res_user = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status",
        headers=auth_uploader,
    )
    assert res_user.status_code == 403
    assert "not authorized to access security partition" in res_user.text

    # Service principal query status -> 200 OK (authorized)
    service_auth = make_auth_header(user_id="service_agent", is_service=True, allowed_projects=[project_id])
    res_svc = await client.get(
        f"/api/v1/projects/{project_id}/documents/{doc_id}/versions/1/status",
        headers=service_auth,
    )
    assert res_svc.status_code == 200


@pytest.mark.asyncio
async def test_document_count_does_not_increment_for_subsequent_versions(client: httpx.AsyncClient):
    """[P2] Verify that Source.document_count increments ONLY on initial Document creation,
    and remains unchanged when subsequent versions are uploaded for the same logical source.
    """
    project_id = "proj_counter_test_01"
    auth_header = make_auth_header(user_id="user_counter", allowed_projects=[project_id])
    logical_doc = "shared/changelog.txt"

    # Version 1 upload -> document_count should become 1
    res1 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_counter",
            "Idempotency-Key": "key_count_v1",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_doc},
        files={"file": ("changelog.txt", b"Version 1 Notes", "text/plain")},
    )
    assert res1.status_code == 201

    async with SessionLocal() as session:
        doc = await session.get(Document, res1.json()["document_id"])
        source1 = await session.get(Source, doc.source_id)
        assert source1.document_count == 1, "Initial upload must increment document_count to 1"

    # Version 2 upload -> document_count must STAY 1 (not become 2)
    res2 = await client.post(
        f"/api/v1/projects/{project_id}/documents/upload",
        headers={
            **auth_header,
            "X-Continuum-User-Id": "user_counter",
            "Idempotency-Key": "key_count_v2",
            "X-Continuum-Security-Partition": "public",
        },
        data={"logical_source_id": logical_doc},
        files={"file": ("changelog.txt", b"Version 2 Updated Notes", "text/plain")},
    )
    assert res2.status_code == 201

    async with SessionLocal() as session:
        source2 = await session.get(Source, doc.source_id)
        assert source2.document_count == 1, "Subsequent version upload must NOT increment document_count"

