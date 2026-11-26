"""Phase 1 Wave 1.2 (P1-D) TDD tests for opt-in document metadata APIs,
PATCH /metadata, exact-duplicate metadata updates, reprocess_document Job.type guard,
search explicit as_of forwarding, and frozen Continuum BFF / SSE contracts.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1 import documents as documents_api
from sag_api.api.v1 import search as search_api
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import (
    get_current_user,
    get_current_user_or_connector,
    get_engine_manager,
    get_job_queue,
    get_llm,
    require_path_source_access,
)
from sag_api.core.errors import ApiError, ValidationError
from sag_api.core.principal_assertion import (
    VerifiedPrincipal,
    require_principal_assertion,
    require_project_bff_principal,
    require_project_read_principal,
)
from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    DocumentVersion,
    Job,
    OctxAsset,
    OctxRelease,
    OctxSourceBinding,
    Source,
    SourceProjectMapping,
)
from sag_api.enums import (
    DocumentStatus,
    JobStatus,
    JobType,
    OctxAssetOwnership,
    OctxReleaseOrigin,
)
from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.schemas.document import DocumentOut, IngestRequest
from sag_api.schemas.routing_rag import (
    DocumentMetadataPatchRequest,
    DocumentMetadataPatchResponse,
    DocumentUploadResponse,
    ProjectDocumentReadinessBatchRequest,
    ProjectDocumentReadinessBatchResponse,
    ProjectDocumentReadinessItem,
)
from sag_api.schemas.search import (
    GlobalSearchRequest,
    SearchCitationOut,
    SearchRequest,
    SectionOut,
)
from sag_api.services.document_service import (
    handle_document_upload,
    reprocess_document,
    revoke_project_document,
)


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:  # noqa: ANN001
        connection.execute("PRAGMA foreign_keys=ON")


def _make_principal(
    *,
    subject: str = "user-p1d",
    tenant_id: str = "tenant-p1d",
    project_ids: frozenset[str] = frozenset({"proj-p1d"}),
    partition_ids: frozenset[str] = frozenset({"part-a"}),
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject=subject,
        organization_id=tenant_id,
        allowed_project_ids=project_ids,
        issuer="continuum-bff",
        key_id="kid-p1d",
        token_id="jti-p1d",
        issued_at=1_700_000_000,
        expires_at=1_700_000_900,
        tenant_id=tenant_id,
        allowed_partition_ids=partition_ids,
    )


def _mock_qdrant_client(
    *,
    status_code: int = 200,
    calls: list[dict] | None = None,
) -> httpx.AsyncClient:
    recorded = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        recorded.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "body": body,
            }
        )
        if status_code >= 400:
            return httpx.Response(status_code, json={"status": {"error": "qdrant unavailable"}})
        return httpx.Response(200, json={"result": {"status": "completed"}, "status": "ok"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://qdrant.test",
    )


@pytest.fixture
async def p1d_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(documents_api, "SessionLocal", factory)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    yield factory
    await engine.dispose()


def _build_documents_app(
    session,
    *,
    principal: VerifiedPrincipal,
    job_queue=None,
    qdrant_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(documents_api.router)
    app.include_router(documents_api.project_router)

    @app.exception_handler(ApiError)
    async def handle_api_error(_request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    fake_user = SimpleNamespace(id="user-p1d", username="tester", is_active=True)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_project_bff_principal] = lambda: principal
    app.dependency_overrides[require_principal_assertion] = lambda: principal
    app.dependency_overrides[require_project_read_principal] = lambda: principal
    app.dependency_overrides[require_path_source_access] = lambda: None
    app.dependency_overrides[get_current_user_or_connector] = lambda: fake_user
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_job_queue] = lambda: job_queue
    if qdrant_client is not None:
        app.state.engine_manager = SimpleNamespace(
            get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_client)
        )
    return app


async def _seed_source(
    session,
    *,
    source_id: str = "src-p1d",
    tenant_id: str = "tenant-p1d",
    project_id: str = "proj-p1d",
) -> Source:
    src = Source(
        id=source_id,
        name="P1D Source",
        sag_source_config_id=f"cfg-{source_id}",
    )
    session.add(src)
    await session.flush()
    session.add(
        SourceProjectMapping(
            source_id=source_id,
            organization_id=tenant_id,
            project_id=project_id,
            state="CONFIRMED",
            confirmed_at=datetime.now(UTC),
            confirmed_by="user-p1d",
            approval_ref="ref-p1d",
        )
    )
    await session.commit()
    return src


# ---------------------------------------------------------------------------
# 1. Opt-in Upload Metadata (Project & Source) + Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_upload_opt_in_metadata_json_csv_omitted_and_validation(p1d_db):
    principal = _make_principal()
    async with p1d_db() as session:
        app = _build_documents_app(session, principal=principal)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 1a. JSON array scope_tags including Vietnamese + authority_tier + effective_from
            resp_json = await client.post(
                "/projects/proj-p1d/documents/upload",
                headers={
                    "Idempotency-Key": "tok-json-1",
                    "X-Continuum-Security-Partition": "part-a",
                },
                files={"file": ("policy.md", b"# Policy\n\nContent A.\n", "text/markdown")},
                data={
                    "logical_source_id": "doc-json-1",
                    "authority_tier": "1",
                    "effective_from": "2025-03-01T00:00:00Z",
                    "scope_tags": json.dumps(["Nhân Sự", "HR", "nhân-sự"], ensure_ascii=False),
                },
            )
            assert resp_json.status_code == 201, resp_json.text
            ver1 = await session.get(DocumentVersion, resp_json.json()["version_id"])
            assert ver1 is not None
            assert ver1.authority_tier == 1
            assert ver1.effective_from == datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
            assert ver1.scope_tags_json == ["hr", "nhân-sự"]
            assert ver1.metadata_json["security_partition_id"] == "part-a"

            # 1b. CSV scope_tags including Vietnamese
            resp_csv = await client.post(
                "/projects/proj-p1d/documents/upload",
                headers={
                    "Idempotency-Key": "tok-csv-1",
                    "X-Continuum-Security-Partition": "part-a",
                },
                files={"file": ("guide.md", b"# Guide\n\nContent B.\n", "text/markdown")},
                data={
                    "logical_source_id": "doc-csv-1",
                    "authority_tier": "3",
                    "scope_tags": "pháp-lý, nhân-sự, miền bắc",
                },
            )
            assert resp_csv.status_code == 201, resp_csv.text
            ver_csv = await session.get(DocumentVersion, resp_csv.json()["version_id"])
            assert ver_csv is not None
            assert ver_csv.authority_tier == 3
            assert ver_csv.scope_tags_json == ["miền-bắc", "nhân-sự", "pháp-lý"]

            # 1c. Omitted metadata -> preserves None (zero ranking tilt)
            resp_legacy = await client.post(
                "/projects/proj-p1d/documents/upload",
                headers={
                    "Idempotency-Key": "tok-legacy-1",
                    "X-Continuum-Security-Partition": "part-a",
                },
                files={"file": ("plain.md", b"# Plain\n\nLegacy.\n", "text/markdown")},
                data={"logical_source_id": "doc-legacy-1"},
            )
            assert resp_legacy.status_code == 201, resp_legacy.text
            ver_legacy = await session.get(DocumentVersion, resp_legacy.json()["version_id"])
            assert ver_legacy is not None
            assert ver_legacy.authority_tier is None
            assert ver_legacy.effective_from is None
            assert ver_legacy.scope_tags_json is None

            # 1d. Invalid authority_tier (0, 5, bool) -> 400/422
            for bad_tier in ("0", "5", "true", "false"):
                bad_resp = await client.post(
                    "/projects/proj-p1d/documents/upload",
                    headers={
                        "Idempotency-Key": f"tok-bad-tier-{bad_tier}",
                        "X-Continuum-Security-Partition": "part-a",
                    },
                    files={"file": ("bad.md", b"# Bad\n", "text/markdown")},
                    data={"authority_tier": bad_tier},
                )
                assert bad_resp.status_code in (400, 422), (bad_tier, bad_resp.status_code)

            # 1e. Invalid scope_tags (>16 tags, invalid chars) -> 400/422
            too_many = ",".join(f"tag-{i}" for i in range(17))
            for idx, bad_tags in enumerate((too_many, "valid,bad!tag", json.dumps(["bad@char"]))):
                bad_tag_resp = await client.post(
                    "/projects/proj-p1d/documents/upload",
                    headers={
                        "Idempotency-Key": f"tok-bad-tags-{idx}",
                        "X-Continuum-Security-Partition": "part-a",
                    },
                    files={"file": ("badtags.md", b"# BadTags\n", "text/markdown")},
                    data={"scope_tags": bad_tags},
                )
                assert bad_tag_resp.status_code in (400, 422), (bad_tags, bad_tag_resp.status_code)

            # 1f. Sentinel effective_from >= 9999-01-01Z -> 400/422
            sentinel_resp = await client.post(
                "/projects/proj-p1d/documents/upload",
                headers={
                    "Idempotency-Key": "tok-bad-eff",
                    "X-Continuum-Security-Partition": "part-a",
                },
                files={"file": ("sentinel.md", b"# Sentinel\n", "text/markdown")},
                data={"effective_from": "9999-01-01T00:00:00Z"},
            )
            assert sentinel_resp.status_code in (400, 422)

        # Direct service-layer bool rejection
        with pytest.raises(ValidationError):
            await handle_document_upload(
                session,
                tenant_id="tenant-p1d",
                project_id="proj-p1d",
                owner_id="user-p1d",
                security_partition_id="part-a",
                client_token="tok-direct-bool",
                file_bytes=b"# Bool\n",
                original_filename="bool.md",
                authority_tier=True,  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_source_upload_and_ingest_opt_in_metadata(p1d_db):
    principal = _make_principal()
    async with p1d_db() as session:
        src = await _seed_source(session)
        app = _build_documents_app(session, principal=principal)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Source multipart upload
            up_resp = await client.post(
                f"/sources/{src.id}/documents",
                headers={
                    "Idempotency-Key": "src-up-1",
                    "X-Continuum-Security-Partition": "part-a",
                },
                files={"file": ("sop.md", b"# SOP\n\nSteps.\n", "text/markdown")},
                data={
                    "project_id": "proj-p1d",
                    "source_published_at": "2025-02-01T08:00:00Z",
                    "authority_tier": "2",
                    "effective_from": "2025-02-15T00:00:00Z",
                    "scope_tags": "nhân-sự, vận-hành",
                },
            )
            assert up_resp.status_code == 201, up_resp.text
            doc_id = up_resp.json()["id"]
            ver = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.document_id == doc_id)
            )
            assert ver is not None
            assert ver.authority_tier == 2
            assert ver.source_published_at == datetime(2025, 2, 1, 8, 0, 0, tzinfo=UTC)
            assert ver.effective_from == datetime(2025, 2, 15, 0, 0, 0, tzinfo=UTC)
            assert ver.scope_tags_json == ["nhân-sự", "vận-hành"]

            # Source JSON ingest
            ing_resp = await client.post(
                f"/sources/{src.id}/documents/ingest",
                headers={
                    "Idempotency-Key": "src-ing-1",
                    "X-Continuum-Security-Partition": "part-a",
                },
                json={
                    "project_id": "proj-p1d",
                    "title": "Decision Memo",
                    "text": "Quarterly budget policy approved.",
                    "source_published_at": "2025-04-01T00:00:00Z",
                    "authority_tier": 4,
                    "effective_from": "2025-04-10T00:00:00Z",
                    "scope_tags": ["tài-chính", "nhân-sự"],
                },
            )
            assert ing_resp.status_code == 201, ing_resp.text
            ing_doc_id = ing_resp.json()["id"]
            ing_ver = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.document_id == ing_doc_id)
            )
            assert ing_ver is not None
            assert ing_ver.authority_tier == 4
            assert ing_ver.source_published_at == datetime(2025, 4, 1, 0, 0, 0, tzinfo=UTC)
            assert ing_ver.effective_from == datetime(2025, 4, 10, 0, 0, 0, tzinfo=UTC)
            assert ing_ver.scope_tags_json == ["nhân-sự", "tài-chính"]

            # Ingest rejects bool/0/5 authority_tier and >16 scope_tags
            for bad_payload in (
                {"project_id": "proj-p1d", "text": "x", "authority_tier": 0},
                {"project_id": "proj-p1d", "text": "x", "authority_tier": 5},
                {"project_id": "proj-p1d", "text": "x", "authority_tier": True},
                {
                    "project_id": "proj-p1d",
                    "text": "x",
                    "scope_tags": [f"t{i}" for i in range(17)],
                },
            ):
                bad_ing = await client.post(
                    f"/sources/{src.id}/documents/ingest",
                    headers={"X-Continuum-Security-Partition": "part-a"},
                    json=bad_payload,
                )
                assert bad_ing.status_code in (400, 422), bad_payload


# ---------------------------------------------------------------------------
# 2. Exact-Duplicate Re-Upload Metadata Update (No Re-Parse, Preserves DATA_READY)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_duplicate_reupload_updates_metadata_without_reparse_or_knowledge_reset(p1d_db):
    qdrant_calls: list[dict] = []
    qdrant = _mock_qdrant_client(status_code=200, calls=qdrant_calls)
    payload_bytes = b"# Stable Policy\n\nIdentical content bytes.\n"

    async with p1d_db() as session:
        v1 = await handle_document_upload(
            session,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            owner_id="user-p1d",
            security_partition_id="part-a",
            client_token="dup-tok-1",
            file_bytes=payload_bytes,
            original_filename="policy.md",
            content_type="text/markdown",
            logical_source_id="dup-doc-1",
            authority_tier=3,
            scope_tags=["old-tag"],
        )
        ver = await session.get(DocumentVersion, v1.version_id)
        doc = await session.get(Document, v1.document_id)
        assert ver is not None and doc is not None
        ver.status = "READY"
        ver.search_status = "SEARCH_READY"
        ver.knowledge_status = "DATA_READY"
        doc.status = DocumentStatus.READY
        await session.commit()

        jobs_before = list(
            (await session.scalars(select(Job).where(Job.document_id == doc.id))).all()
        )
        assert len(jobs_before) == 1

        # Exact-duplicate re-upload with new Idempotency-Key & changed authority_tier/scope_tags
        dup = await handle_document_upload(
            session,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            owner_id="user-p1d",
            security_partition_id="part-a",
            client_token="dup-tok-2",
            file_bytes=payload_bytes,
            original_filename="policy.md",
            content_type="text/markdown",
            logical_source_id="dup-doc-1",
            authority_tier=1,
            scope_tags=["nhân-sự", "toàn-công-ty"],
            qdrant_client=qdrant,
        )
        assert dup.is_duplicate is True
        assert dup.version_id == v1.version_id
        assert dup.knowledge_status == "DATA_READY"

        await session.refresh(ver)
        assert ver.authority_tier == 1
        assert ver.scope_tags_json == ["nhân-sự", "toàn-công-ty"]
        assert ver.metadata_json["security_partition_id"] == "part-a"
        assert ver.knowledge_status == "DATA_READY"

        jobs_after = list(
            (await session.scalars(select(Job).where(Job.document_id == doc.id))).all()
        )
        assert len(jobs_after) == 1  # ZERO re-parse job created!

        assert len(qdrant_calls) == 1
        assert "/points/payload" in qdrant_calls[0]["path"]
        assert qdrant_calls[0]["body"]["payload"]["authority_tier"] == 1
        assert qdrant_calls[0]["body"]["payload"]["scope_tags"] == ["nhân-sự", "toàn-công-ty"]
    await qdrant.aclose()


# ---------------------------------------------------------------------------
# 3. PATCH .../metadata (Project + Source Alias, Tri-State, ACL, REVOKED, B1, B10, 503 Degraded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_metadata_tristate_acl_revoked_and_source_alias(p1d_db):
    qdrant_calls: list[dict] = []
    qdrant = _mock_qdrant_client(status_code=200, calls=qdrant_calls)
    principal = _make_principal(partition_ids=frozenset({"part-a"}))

    async with p1d_db() as session:
        src = await _seed_source(session)
        up = await handle_document_upload(
            session,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            owner_id="user-p1d",
            security_partition_id="part-a",
            client_token="patch-up-1",
            file_bytes=b"# Governance\n\nRule 1.\n",
            original_filename="gov.md",
            content_type="text/markdown",
            logical_source_id="gov-doc-1",
            source_id=src.id,
            authority_tier=3,
            effective_from=datetime(2025, 1, 15, tzinfo=UTC),
            scope_tags=["old-scope"],
        )
        ver = await session.get(DocumentVersion, up.version_id)
        doc = await session.get(Document, up.document_id)
        assert ver is not None and doc is not None
        ver.status = "READY"
        ver.search_status = "SEARCH_READY"
        ver.knowledge_status = "DATA_READY"
        doc.status = DocumentStatus.READY
        await session.commit()

        app = _build_documents_app(session, principal=principal, qdrant_client=qdrant)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 3a. Empty patch or version_no-only -> 400/422
            for bad_body in ({}, {"version_no": 1}, {"authority_tier": 1, "extra_field": "nope"}):
                r_bad = await client.patch(
                    f"/projects/proj-p1d/documents/{doc.id}/metadata",
                    json=bad_body,
                )
                assert r_bad.status_code in (400, 422), (bad_body, r_bad.status_code)

            # 3b. Partial update ONLY authority_tier -> effective_from & scope_tags untouched, DATA_READY preserved (B1)
            r1 = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"authority_tier": 1},
            )
            assert r1.status_code == 200, r1.text
            body1 = r1.json()
            assert body1["authority_tier"] == 1
            assert body1["scope_tags"] == ["old-scope"]
            assert body1["effective_from"] is not None
            assert body1["temporal_recomputed"] is False
            assert body1["qdrant_payload_synced"] is True
            assert body1["degraded"] is False

            await session.refresh(ver)
            assert ver.knowledge_status == "DATA_READY"
            assert ver.metadata_json["security_partition_id"] == "part-a"

            # 3c. Explicit null clear via model_fields_set on source alias
            r2 = await client.patch(
                f"/sources/{src.id}/documents/{doc.id}/metadata",
                json={"authority_tier": None, "scope_tags": None},
            )
            assert r2.status_code == 200, r2.text
            body2 = r2.json()
            assert body2["authority_tier"] is None
            assert body2["scope_tags"] is None
            assert body2["effective_from"] is not None  # untouched!
            assert body2["temporal_recomputed"] is False
            await session.refresh(ver)
            assert ver.authority_tier is None
            assert ver.scope_tags_json is None
            assert ver.effective_from == datetime(2025, 1, 15, tzinfo=UTC)
            assert ver.knowledge_status == "DATA_READY"
            assert ver.metadata_json["security_partition_id"] == "part-a"

        # 3d. Cross-project 403
        other_proj_principal = _make_principal(
            project_ids=frozenset({"other-proj"}),
            partition_ids=frozenset({"part-a"}),
        )
        app_other_proj = _build_documents_app(
            session, principal=other_proj_principal, qdrant_client=qdrant
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_other_proj), base_url="http://test"
        ) as client:
            r_cross_proj = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"authority_tier": 2},
            )
            assert r_cross_proj.status_code == 403

        # 3e. Cross-partition 403 (same project, unauthorized partition!)
        other_part_principal = _make_principal(
            project_ids=frozenset({"proj-p1d"}),
            partition_ids=frozenset({"part-other"}),
        )
        app_other_part = _build_documents_app(
            session, principal=other_part_principal, qdrant_client=qdrant
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_other_part), base_url="http://test"
        ) as client:
            r_cross_part = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"authority_tier": 2},
            )
            assert r_cross_part.status_code == 403

        # 3f. REVOKED version -> 409 ConflictError
        await revoke_project_document(
            session,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            document_id=doc.id,
            reason="superseded_externally",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r_revoked = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"authority_tier": 2},
            )
            assert r_revoked.status_code == 409
    await qdrant.aclose()


@pytest.mark.asyncio
async def test_patch_effective_from_recomputes_temporal_resets_knowledge_and_bumps_octx(p1d_db):
    qdrant_calls: list[dict] = []
    qdrant = _mock_qdrant_client(status_code=200, calls=qdrant_calls)
    principal = _make_principal()

    async with p1d_db() as session:
        src = await _seed_source(session)
        # Seed OCTX binding to assert touch_source_revision bumps content_revision (B10)
        asset = OctxAsset(
            id=str(uuid.uuid4()),
            name="Asset P1D",
            ownership=OctxAssetOwnership.LOCAL,
            producer_source_id=src.id,
        )
        session.add(asset)
        await session.flush()
        release = OctxRelease(
            id=str(uuid.uuid4()),
            asset_id=asset.id,
            version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            manifest={"format": "0.1"},
            artifact_key="octx/test.octx",
            created_by=OctxReleaseOrigin.EXPORT,
        )
        session.add(release)
        await session.flush()
        binding = OctxSourceBinding(
            source_id=src.id,
            asset_id=asset.id,
            active_release_id=release.id,
            content_revision=4,
            released_revision=4,
        )
        session.add(binding)
        await session.commit()

        # Create doc with two SEARCH_READY versions v1 [2024-01-01, 2025-01-01) and v2 [2025-01-01, 9999)
        doc = Document(
            id="doc-temporal-patch",
            source_id=src.id,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            logical_source_id="temporal-patch-doc",
            filename="policy.md",
            storage_path="policy.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
        session.add(doc)
        await session.flush()
        v1 = DocumentVersion(
            id="ver-tp-1",
            document_id=doc.id,
            version_no=1,
            file_hash="h1",
            effective_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_from=datetime(2024, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 1, 1, tzinfo=UTC),
            status="READY",
            search_status="SEARCH_READY",
            knowledge_status="DATA_READY",
            metadata_json={"security_partition_id": "part-a"},
        )
        v2 = DocumentVersion(
            id="ver-tp-2",
            document_id=doc.id,
            version_no=2,
            file_hash="h2",
            supersedes_id=v1.id,
            effective_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            status="READY",
            search_status="SEARCH_READY",
            knowledge_status="DATA_READY",
            metadata_json={"security_partition_id": "part-a"},
        )
        session.add_all([v1, v2])
        await session.commit()

        app = _build_documents_app(session, principal=principal, qdrant_client=qdrant)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"version_no": 2, "effective_from": "2024-06-01T00:00:00Z"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["temporal_recomputed"] is True
            assert data["qdrant_payload_synced"] is True
            assert data["degraded"] is False

        await session.refresh(v1)
        await session.refresh(v2)
        await session.refresh(binding)

        # Contiguous intervals with zero gap and v2 still open-ended!
        assert v2.valid_from == datetime(2024, 6, 1, tzinfo=UTC)
        assert v2.valid_to.year == 9999
        assert v1.valid_to == datetime(2024, 6, 1, tzinfo=UTC)
        # B1: both temporally mutated versions reset to NOT_STARTED
        assert v1.knowledge_status == "NOT_STARTED"
        assert v2.knowledge_status == "NOT_STARTED"
        # B10: OCTX content_revision incremented
        assert binding.content_revision == 5
        # Both v1 and v2 synced to Qdrant
        synced_vids = {
            c["body"]["filter"]["must"][1]["match"]["value"] for c in qdrant_calls
        }
        assert synced_vids == {v1.id, v2.id}
    await qdrant.aclose()


@pytest.mark.asyncio
async def test_patch_metadata_qdrant_503_commits_pg_marks_degraded_and_persists_reconcile_job(
    p1d_db,
):
    qdrant_calls: list[dict] = []
    failing_qdrant = _mock_qdrant_client(status_code=503, calls=qdrant_calls)
    principal = _make_principal()
    fake_queue = SimpleNamespace(enqueue=AsyncMock())

    async with p1d_db() as session:
        src = await _seed_source(session)
        up = await handle_document_upload(
            session,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            owner_id="user-p1d",
            security_partition_id="part-a",
            client_token="q503-tok-1",
            file_bytes=b"# Q503\n\nContent.\n",
            original_filename="q503.md",
            content_type="text/markdown",
            logical_source_id="q503-doc",
            source_id=src.id,
        )
        ver = await session.get(DocumentVersion, up.version_id)
        doc = await session.get(Document, up.document_id)
        assert ver is not None and doc is not None
        ver.status = "READY"
        ver.search_status = "SEARCH_READY"
        ver.knowledge_status = "DATA_READY"
        doc.status = DocumentStatus.READY
        await session.commit()

        app = _build_documents_app(
            session,
            principal=principal,
            job_queue=fake_queue,
            qdrant_client=failing_qdrant,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.patch(
                f"/projects/proj-p1d/documents/{doc.id}/metadata",
                json={"authority_tier": 1, "scope_tags": ["nhân-sự"]},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["authority_tier"] == 1
            assert data["scope_tags"] == ["nhân-sự"]
            assert data["qdrant_payload_synced"] is False
            assert data["degraded"] is True
            assert data["degraded_reason"] == "temporal_payload_sync_failed"

        # PG commit succeeded & metadata_json merged safely
        await session.refresh(ver)
        assert ver.authority_tier == 1
        assert ver.scope_tags_json == ["nhân-sự"]
        assert ver.metadata_json["security_partition_id"] == "part-a"
        assert ver.metadata_json["qdrant_temporal_sync_degraded"] is True
        assert ver.id in ver.metadata_json["qdrant_temporal_sync_failed_version_ids"]

        # Reconcile job persisted & enqueued
        reconcile_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == doc.id,
                        Job.type == JobType.RECONCILE_SEARCH_INDEX,
                    )
                )
            ).all()
        )
        assert len(reconcile_jobs) == 1
        rjob = reconcile_jobs[0]
        assert rjob.status == JobStatus.QUEUED
        assert rjob.payload["reason"] == "temporal_payload_sync_failed"
        assert rjob.payload["project_id"] == "proj-p1d"
        assert ver.id in rjob.payload["sync_version_ids"]
        fake_queue.enqueue.assert_awaited_once_with(rjob.id)
    await failing_qdrant.aclose()


# ---------------------------------------------------------------------------
# 4. reprocess_document Job.type Filter Guard (document_service.py:243)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "non_lifecycle_type",
    [JobType.CONFLICT_RESOLUTION, JobType.RECONCILE_SEARCH_INDEX],
)
async def test_reprocess_document_ignores_non_lifecycle_jobs(p1d_db, non_lifecycle_type):
    fake_queue = SimpleNamespace(
        enqueue=AsyncMock(),
        begin_source_maintenance=lambda *_args, **_kwargs: None,
    )
    async with p1d_db() as session:
        src = await _seed_source(session)
        doc = Document(
            id=f"doc-reproc-{non_lifecycle_type.value}",
            source_id=src.id,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            filename="fail.md",
            storage_path="fail.md",
            status=DocumentStatus.FAILED,
            is_active=True,
        )
        session.add(doc)
        await session.flush()

        # Older FAILED process job with legitimate run_id payload
        old_proc = Job(
            id=f"job-old-proc-{non_lifecycle_type.value}",
            type=JobType.PROCESS_DOCUMENT,
            source_id=src.id,
            document_id=doc.id,
            status=JobStatus.FAILED,
            payload={"run_id": "run-orig-123"},
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        # Newer QUEUED non-lifecycle job (CONFLICT_RESOLUTION or RECONCILE_SEARCH_INDEX)
        side_job = Job(
            id=f"job-side-{non_lifecycle_type.value}",
            type=non_lifecycle_type,
            source_id=src.id,
            document_id=doc.id,
            status=JobStatus.QUEUED,
            payload={"reason": "temporal_payload_sync_failed", "sync_version_ids": ["v1"]},
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
        session.add_all([old_proc, side_job])
        await session.commit()

        new_job = await reprocess_document(session, src, doc.id, job_queue=fake_queue)
        assert new_job.id != side_job.id
        assert new_job.type == JobType.PROCESS_DOCUMENT
        assert new_job.status == JobStatus.QUEUED
        assert new_job.payload.get("run_id") == "run-orig-123"
        assert "sync_version_ids" not in new_job.payload
        fake_queue.enqueue.assert_awaited_once_with(new_job.id)


@pytest.mark.asyncio
async def test_reprocess_document_same_second_active_job_returns_existing_regardless_of_uuid(
    p1d_db,
):
    """Same-second SUCCEEDED PROCESS_DOCUMENT with larger UUID ('ffffffff-...') must NEVER shadow
    an active QUEUED REPROCESS_DOCUMENT with smaller UUID ('00000000-...')."""
    fake_queue = SimpleNamespace(
        enqueue=AsyncMock(),
        begin_source_maintenance=lambda *_args, **_kwargs: None,
    )
    same_ts = datetime(2026, 11, 26, 9, 31, 40, tzinfo=UTC)
    async with p1d_db() as session:
        src = await _seed_source(session)
        doc = Document(
            id="doc-reproc-same-sec",
            source_id=src.id,
            tenant_id="tenant-p1d",
            project_id="proj-p1d",
            filename="policy.md",
            storage_path="policy.md",
            status=DocumentStatus.PENDING,
            is_active=True,
        )
        session.add(doc)
        await session.flush()

        succeeded_job = Job(
            id="ffffffff-ffff-ffff-ffff-ffffffffffff",
            type=JobType.PROCESS_DOCUMENT,
            source_id=src.id,
            document_id=doc.id,
            status=JobStatus.SUCCEEDED,
            payload={"run_id": "run-old"},
            created_at=same_ts,
        )
        queued_reproc = Job(
            id="00000000-0000-0000-0000-000000000001",
            type=JobType.REPROCESS_DOCUMENT,
            source_id=src.id,
            document_id=doc.id,
            status=JobStatus.QUEUED,
            payload={"target_document_id": doc.id},
            created_at=same_ts,
        )
        session.add_all([succeeded_job, queued_reproc])
        await session.commit()

        returned_job = await reprocess_document(session, src, doc.id, job_queue=fake_queue)
        assert returned_job.id == queued_reproc.id
        assert returned_job.status == JobStatus.QUEUED
        fake_queue.enqueue.assert_not_awaited()

        all_jobs = list(
            (await session.scalars(select(Job).where(Job.document_id == doc.id))).all()
        )
        assert len(all_jobs) == 2


@pytest.mark.asyncio
async def test_patch_metadata_releases_source_mutation_lease_before_qdrant_http_sync(
    p1d_db, monkeypatch: pytest.MonkeyPatch
):
    """source_document_mutation lease protects PG write & commit, then releases BEFORE the
    post-commit Qdrant HTTP set_payload network call on both project and source routes."""
    from contextlib import asynccontextmanager

    lease_state = {"active": False, "entries": 0}
    qdrant_saw_lease_active: list[bool] = []
    real_mutation = documents_api.source_document_mutation

    @asynccontextmanager
    async def tracked_mutation(*args, **kwargs):
        async with real_mutation(*args, **kwargs):
            lease_state["entries"] += 1
            lease_state["active"] = True
            try:
                yield
            finally:
                lease_state["active"] = False

    monkeypatch.setattr(documents_api, "source_document_mutation", tracked_mutation)

    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        qdrant_saw_lease_active.append(lease_state["active"])
        return httpx.Response(200, json={"result": {"status": "completed"}, "status": "ok"})

    qdrant = httpx.AsyncClient(
        transport=httpx.MockTransport(qdrant_handler),
        base_url="http://qdrant.test",
    )
    principal = _make_principal()

    try:
        async with p1d_db() as session:
            src = await _seed_source(session)
            up = await handle_document_upload(
                session,
                tenant_id="tenant-p1d",
                project_id="proj-p1d",
                owner_id="user-p1d",
                security_partition_id="part-a",
                client_token="lease-tok-1",
                file_bytes=b"# Lease\n\nContent.\n",
                original_filename="lease.md",
                content_type="text/markdown",
                logical_source_id="lease-doc",
                source_id=src.id,
            )
            ver = await session.get(DocumentVersion, up.version_id)
            doc = await session.get(Document, up.document_id)
            assert ver is not None and doc is not None
            ver.status = "READY"
            ver.search_status = "SEARCH_READY"
            doc.status = DocumentStatus.READY
            await session.commit()

            app = _build_documents_app(session, principal=principal, qdrant_client=qdrant)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                r1 = await client.patch(
                    f"/projects/proj-p1d/documents/{doc.id}/metadata",
                    json={"authority_tier": 1},
                )
                assert r1.status_code == 200, r1.text
                assert r1.json()["qdrant_payload_synced"] is True

                r2 = await client.patch(
                    f"/sources/{src.id}/documents/{doc.id}/metadata",
                    json={"authority_tier": 2},
                )
                assert r2.status_code == 200, r2.text
                assert r2.json()["qdrant_payload_synced"] is True

            assert lease_state["entries"] == 2
            assert lease_state["active"] is False
            assert qdrant_saw_lease_active == [False, False]
    finally:
        await qdrant.aclose()


# ---------------------------------------------------------------------------
# 5. SearchRequest & GlobalSearchRequest Explicit as_of Normalization & Forwarding
# ---------------------------------------------------------------------------


def test_search_request_as_of_date_and_datetime_normalization():
    expected_eod = datetime(2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC)

    req_str = SearchRequest(query="policy?", as_of="2025-06-15")
    assert req_str.as_of == expected_eod

    req_date = GlobalSearchRequest(query="policy?", as_of=date(2025, 6, 15))
    assert req_date.as_of == expected_eod

    req_dt = GlobalSearchRequest(query="policy?", as_of="2025-06-15T10:30:00+07:00")
    assert req_dt.as_of == datetime(2025, 6, 15, 3, 30, 0, tzinfo=UTC)

    req_none = SearchRequest(query="policy?")
    assert req_none.as_of is None

    with pytest.raises(PydanticValidationError):
        SearchRequest(query="policy?", as_of=True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_routes_forward_explicit_as_of_to_plan_query_and_preserve_sse_events(
    p1d_db, monkeypatch: pytest.MonkeyPatch
):
    principal = _make_principal()
    expected_as_of = datetime(2025, 6, 15, 23, 59, 59, 999999, tzinfo=UTC)
    captured_plans: list[object] = []

    async def fake_retrieve_search_units(
        _engine_manager,
        _sources,
        query,
        *,
        principal=None,
        top_k=None,
        query_strategy_plan=None,
    ):
        captured_plans.append(query_strategy_plan)
        return SearchOutcome(
            query=query,
            sections=[
                RetrievedSection(
                    chunk_id="su-1",
                    search_unit_id="su-1",
                    block_from_id="b-1",
                    block_to_id="b-1",
                    section_path="Policy",
                    heading="Rule",
                    content="Leave quota was 12 days in 2025.",
                    score=0.9,
                    rank=0,
                    source_id="src-p1d",
                    source_config_id="cfg-src-p1d",
                    document_id="doc-1",
                    document_version_id="ver-1",
                    document_name="policy.md",
                    version_no=1,
                    page_from=1,
                    page_to=1,
                    anchor="p1-b1",
                    authority_tier=1,
                    effective_from=datetime(2025, 1, 1, tzinfo=UTC),
                    scope_tags=("nhân-sự",),
                    conflict_status=None,
                )
            ],
            stats={"sources": 1},
        )

    async def fake_stream_synth(*_args, **_kwargs):
        yield SimpleNamespace(kind="delta", text="Leave quota was 12 days [1].")
        yield SimpleNamespace(
            kind="completed",
            text="Leave quota was 12 days [1].",
            citations=[],
            status="answered",
            no_answer_reason=None,
            degraded=False,
            degraded_reason=None,
        )

    from sag_api.services import search_unit_retrieval_service

    monkeypatch.setattr(
        search_unit_retrieval_service,
        "retrieve_search_unit_sections",
        fake_retrieve_search_units,
    )
    monkeypatch.setattr(
        search_api,
        "synthesize_search_answer",
        AsyncMock(return_value=SimpleNamespace(
            text="Leave quota was 12 days [1].",
            citations=[],
            status="answered",
            no_answer_reason=None,
            degraded=False,
            degraded_reason=None,
        )),
    )
    monkeypatch.setattr(search_api, "stream_synthesize_search_answer", fake_stream_synth)
    monkeypatch.setattr(
        search_api,
        "versioned_source_ids",
        AsyncMock(return_value={"src-p1d"}),
    )

    async with p1d_db() as session:
        src = await _seed_source(session)
        app = FastAPI()
        app.include_router(search_api.router)
        app.include_router(search_api.global_router)

        fake_user = SimpleNamespace(id="user-p1d", is_active=True)

        async def _fake_user_or_connector(request: Request):
            request.state.auth_kind = "jwt"
            return fake_user

        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[require_project_read_principal] = lambda: principal
        app.dependency_overrides[get_current_user] = lambda: fake_user
        app.dependency_overrides[get_current_user_or_connector] = _fake_user_or_connector
        app.dependency_overrides[get_engine_manager] = lambda: SimpleNamespace()
        app.dependency_overrides[get_llm] = lambda: SimpleNamespace(configured=False)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # 5a. Source-scoped POST /sources/{sid}/search
            r_src = await client.post(
                f"/sources/{src.id}/search",
                json={"query": "leave quota", "as_of": "2025-06-15"},
            )
            assert r_src.status_code == 200, r_src.text
            assert captured_plans[-1].retrieval_plan.as_of == expected_as_of
            assert captured_plans[-1].retrieval_plan.temporal_mode == "past_point"
            # Frozen SectionOut must NOT leak internal metadata fields
            sec_json = r_src.json()["sections"][0]
            assert "authority_tier" not in sec_json
            assert "effective_from" not in sec_json
            assert "scope_tags" not in sec_json
            assert "conflict_status" not in sec_json

            # 5b. Global POST /search
            r_glob = await client.post(
                "/search",
                json={"query": "leave quota", "source_ids": [src.id], "as_of": "2025-06-15"},
            )
            assert r_glob.status_code == 200, r_glob.text
            assert captured_plans[-1].retrieval_plan.as_of == expected_as_of
            assert captured_plans[-1].retrieval_plan.temporal_mode == "past_point"

            # 5c. Streaming POST /search/stream preserves strict frozen SSE event set
            r_stream = await client.post(
                "/search/stream",
                json={"query": "leave quota", "source_ids": [src.id], "as_of": "2025-06-15"},
            )
            assert r_stream.status_code == 200, r_stream.text
            assert captured_plans[-1].retrieval_plan.as_of == expected_as_of
            assert captured_plans[-1].retrieval_plan.temporal_mode == "past_point"
            sse_events = [
                line.split(":", 1)[1].strip()
                for line in r_stream.text.splitlines()
                if line.startswith("event:")
            ]
            assert sse_events
            assert set(sse_events).issubset({"result", "summary.delta", "completed", "error"})
            assert "result" in sse_events and "completed" in sse_events


# ---------------------------------------------------------------------------
# 6. Frozen Continuum BFF Schema Surface Regression Check
# ---------------------------------------------------------------------------


def test_frozen_bff_schemas_remain_unchanged():
    assert set(DocumentOut.model_fields.keys()) == {
        "id",
        "source_id",
        "filename",
        "content_type",
        "size_bytes",
        "status",
        "chunk_count",
        "event_count",
        "progress",
        "token_usage",
        "token_usage_breakdown",
        "error",
        "error_layer",
        "error_stage",
        "parser_provider",
        "mineru_provider",
        "mineru_model",
        "parser_status",
        "fallback_from",
        "fallback_reason",
        "octx_installation_id",
        "created_at",
        "updated_at",
    }
    assert set(DocumentUploadResponse.model_fields.keys()) == {
        "document_id",
        "version_no",
        "version_id",
        "run_id",
        "file_hash",
        "status",
        "search_status",
        "knowledge_status",
        "is_duplicate",
    }
    assert set(SectionOut.model_fields.keys()) == {
        "chunk_id",
        "search_unit_id",
        "block_from_id",
        "block_to_id",
        "section_path",
        "document_id",
        "document_version_id",
        "document_name",
        "version_no",
        "page_from",
        "page_to",
        "anchor",
        "heading",
        "content",
        "score",
        "rank",
        "source_id",
        "source_name",
    }
    assert set(SearchCitationOut.model_fields.keys()) == {
        "kind",
        "n",
        "document_id",
        "document_version_id",
        "document_name",
        "version_no",
        "chunk_id",
        "search_unit_id",
        "block_from_id",
        "block_to_id",
        "section_path",
        "page_from",
        "page_to",
        "anchor",
        "heading",
        "snippet",
        "score",
        "source_id",
        "source_name",
    }
    assert ProjectDocumentReadinessBatchRequest.model_config.get("extra") == "forbid"
    assert ProjectDocumentReadinessItem.model_config.get("extra") == "forbid"
    assert ProjectDocumentReadinessBatchResponse.model_config.get("extra") == "forbid"
    assert DocumentMetadataPatchRequest.model_config.get("extra") == "forbid"
    assert DocumentMetadataPatchResponse.model_config.get("extra") == "forbid"
    assert set(IngestRequest.model_fields.keys()) >= {
        "project_id",
        "logical_source_id",
        "text",
        "title",
        "messages",
        "source_published_at",
        "authority_tier",
        "effective_from",
        "scope_tags",
    }
