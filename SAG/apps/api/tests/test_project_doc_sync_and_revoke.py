from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, Job, SourceProjectMapping
from sag_api.db.models.document import DocumentStatus
from sag_api.db.models.routing_rag import DocumentVersion
from sag_api.enums import JobType
from sag_api.jobs.control import JobPaused
from sag_api.jobs.tasks import _delete_document_task_unlocked
from sag_api.services.canonical_service import parse_and_persist_document_content
from sag_api.services.dedup_and_temporal_service import run_dedup_and_temporal_stage
from sag_api.services.document_service import (
    get_or_create_project_source,
    handle_document_upload,
    revoke_project_document,
)
from sag_api.services.search_index_service import run_search_indexing_stage
from sag_api.services.search_unit_retrieval_service import _load_current_ready_versions_checked
from sag_api.services.source_service import list_sources, search_source_candidates


def _fake_embedder(_text: str) -> list[float]:
    return [0.25, 0.5, 0.75, 1.0]


class InMemoryQdrantMock:
    """In-memory Qdrant HTTP mock supporting collections, upsert, count, scroll, and delete."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.delete_calls: list[tuple[str, list[str]]] = []
        self.corrupt_next_scroll_checksum: bool = False
        self.on_after_scroll_hook: Any = None

    def points_for_version(self, collection: str, version_id: str) -> list[dict[str, Any]]:
        col = self.collections.get(collection, {})
        return [
            p
            for p in col.values()
            if p.get("payload", {}).get("document_version_id") == version_id
        ]

    async def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        # GET /collections/{name}
        if path.startswith("/collections/") and method == "GET":
            col_name = path.split("/")[2]
            if col_name in self.collections:
                return httpx.Response(200, json={"result": {"status": "green"}})
            return httpx.Response(404, json={"status": {"error": "Not found"}})

        # PUT /collections/{name}
        if path.startswith("/collections/") and len(path.split("/")) == 3 and method == "PUT":
            col_name = path.split("/")[2]
            self.collections.setdefault(col_name, {})
            return httpx.Response(200, json={"result": True})

        # PUT /collections/{name}/index
        if path.endswith("/index") and method == "PUT":
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

        # PUT /collections/{name}/points (upsert)
        if path.endswith("/points") and method == "PUT":
            col_name = path.split("/")[2]
            col = self.collections.setdefault(col_name, {})
            for pt in body.get("points", []):
                col[str(pt["id"])] = pt
            return httpx.Response(200, json={"result": {"status": "completed"}})

        # POST /collections/{name}/points/count
        if path.endswith("/points/count") and method == "POST":
            col_name = path.split("/")[2]
            col = self.collections.get(col_name, {})
            ver_id = self._extract_version_ids(body.get("filter", {}))[0]
            matched = [
                p
                for p in col.values()
                if p.get("payload", {}).get("document_version_id") == ver_id
            ]
            return httpx.Response(200, json={"result": {"count": len(matched)}})

        # POST /collections/{name}/points/scroll
        if path.endswith("/points/scroll") and method == "POST":
            col_name = path.split("/")[2]
            col = self.collections.get(col_name, {})
            ver_id = self._extract_version_ids(body.get("filter", {}))[0]
            matched = []
            for p in col.values():
                if p.get("payload", {}).get("document_version_id") == ver_id:
                    pt_copy = {
                        "id": p["id"],
                        "payload": dict(p.get("payload", {})),
                    }
                    if self.corrupt_next_scroll_checksum:
                        pt_copy["payload"]["content_hash"] = "corrupted_hash"
                    matched.append(pt_copy)
            self.corrupt_next_scroll_checksum = False
            if self.on_after_scroll_hook is not None:
                hook = self.on_after_scroll_hook
                self.on_after_scroll_hook = None
                await hook()
            return httpx.Response(
                200,
                json={"result": {"points": matched, "next_page_offset": None}},
            )

        # POST /collections/{name}/points/delete
        if path.endswith("/points/delete") and method == "POST":
            col_name = path.split("/")[2]
            col = self.collections.get(col_name, {})
            ver_ids = self._extract_version_ids(body.get("filter", {}))
            self.delete_calls.append((col_name, ver_ids))
            to_delete = [
                pid
                for pid, p in col.items()
                if p.get("payload", {}).get("document_version_id") in set(ver_ids)
            ]
            for pid in to_delete:
                col.pop(pid, None)
            return httpx.Response(200, json={"result": {"status": "completed"}})

        return httpx.Response(200, json={"result": True})

    @staticmethod
    def _extract_version_ids(filter_obj: dict[str, Any]) -> list[str]:
        must = filter_obj.get("must", [])
        for cond in must:
            if cond.get("key") == "document_version_id":
                match = cond.get("match", {})
                if "value" in match:
                    return [str(match["value"])]
                if "any" in match:
                    return [str(v) for v in match["any"]]
        return []


@pytest_asyncio.fixture
async def sync_db(tmp_path, monkeypatch):
    from sag_api.core.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_principal(
    *,
    subject: str = "user-manager",
    organization_id: str = "6ad76c7d938f3bf53df7335e",
    tenant_id: str = "6ad76c7d938f3bf53df7335e",
    project_ids: set[str] | None = None,
    partition_ids: set[str] | None = None,
) -> VerifiedPrincipal:
    now_ts = int(datetime.now(UTC).timestamp())
    projs = frozenset(project_ids or {"6ad7759134bd88b3f2cc845b"})
    parts = frozenset(partition_ids or set(projs))
    return VerifiedPrincipal(
        subject=subject,
        organization_id=organization_id,
        allowed_project_ids=projs,
        issuer="continuum-be-bff",
        key_id="service-token",
        token_id="tok-1",
        issued_at=now_ts,
        expires_at=now_ts + 60,
        tenant_id=tenant_id,
        allowed_partition_ids=parts,
    )


@pytest.mark.asyncio
async def test_project_source_confirmed_mapping_and_retrieval_visibility(sync_db):
    """Verify Option B: get_or_create_project_source creates a CONFIRMED SourceProjectMapping,
    keeps the Project Source hidden in list_sources, but exposes it to
    search_source_candidates(include_project_sources=True) and _load_current_ready_versions_checked.
    """
    tenant_id = "6ad76c7d938f3bf53df7335e"
    project_id = "6ad7759134bd88b3f2cc845b"
    principal = _make_principal(
        organization_id=tenant_id,
        tenant_id=tenant_id,
        project_ids={project_id},
        partition_ids={project_id},
    )
    qdrant_mock = InMemoryQdrantMock()
    transport = httpx.MockTransport(qdrant_mock.handle_request)

    async with sync_db() as session:
        upload_res = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-sag-1:v1",
            file_bytes=b"# SAG Retrieval Architecture\n\nHybrid search combines dense and BM25 vectors.",
            original_filename="sag-architecture.md",
            content_type="text/markdown",
            logical_source_id="continuum:project_document:doc-sag-1",
            organization_id=principal.organization_id,
        )
        assert upload_res.version_no == 1

        # Verify Source & CONFIRMED SourceProjectMapping
        source = await get_or_create_project_source(session, tenant_id, project_id)
        assert source.config.get("is_project_source") is True
        mapping = (
            await session.execute(
                select(SourceProjectMapping).where(SourceProjectMapping.source_id == source.id)
            )
        ).scalar_one_or_none()
        assert mapping is not None
        assert mapping.state == "CONFIRMED"
        assert mapping.organization_id == tenant_id
        assert mapping.project_id == project_id
        assert mapping.confirmed_at is not None
        assert mapping.confirmed_by == principal.subject

        # Hidden from normal Notebook list_sources, visible when include_project_sources=True
        visible_notebooks = await list_sources(session, principal=principal)
        assert visible_notebooks == []

        candidate_sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        assert [s.id for s in candidate_sources] == [source.id]

        # Run Phase 2A -> 2B -> 2C indexing
        doc = await session.get(Document, upload_res.document_id)
        doc_ver = await session.get(DocumentVersion, upload_res.version_id)
        await parse_and_persist_document_content(
            session,
            doc_ver.id,
            "# SAG Retrieval Architecture\n\nHybrid search combines dense and BM25 vectors.",
            run_id=upload_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=doc_ver,
            document_id=doc.id,
            project_id=project_id,
            run_id=upload_res.run_id,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=doc_ver,
                security_partition_id=project_id,
                tenant_id=tenant_id,
                qdrant_client=qclient,
                embedder=_fake_embedder,
                run_id=upload_res.run_id,
            )
        doc.status = DocumentStatus.READY
        await session.commit()

        # Authorized principal resolves the version
        ready_versions = await _load_current_ready_versions_checked(
            session, candidate_sources, principal
        )
        assert list(ready_versions.keys()) == [upload_res.version_id]

        # Unauthorized project principal gets 0 versions
        other_principal = _make_principal(
            organization_id=tenant_id,
            tenant_id=tenant_id,
            project_ids={"other-project-999"},
            partition_ids={"other-project-999"},
        )
        other_ready = await _load_current_ready_versions_checked(
            session, candidate_sources, other_principal
        )
        assert other_ready == {}


@pytest.mark.asyncio
async def test_version_replacement_v1_to_v2_and_rollback_consistency(sync_db):
    """Verify safe v1 -> v2 replacement:
    1. Uploading v2 does NOT prematurely close v1.valid_to while v2 is PENDING.
    2. If v2 fails indexing/manifest check, v1 remains SEARCH_READY in both PostgreSQL and Qdrant,
       and v2's partial Qdrant points are cleaned up.
    3. When v2 succeeds and commits in PostgreSQL, v1.valid_to is closed, v1's Qdrant points are
       pruned via delete_document_points_from_qdrant, and only v2 is returned by retrieval.
    """
    tenant_id = "6ad76c7d938f3bf53df7335e"
    project_id = "6ad7759134bd88b3f2cc845b"
    logical_id = "continuum:project_document:doc-replace-test"
    principal = _make_principal(
        organization_id=tenant_id,
        tenant_id=tenant_id,
        project_ids={project_id},
        partition_ids={project_id},
    )
    qdrant_mock = InMemoryQdrantMock()
    transport = httpx.MockTransport(qdrant_mock.handle_request)
    col_name = f"search_units_{project_id}"

    async with sync_db() as session:
        # 1. Upload and index v1
        v1_res = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-replace-test:v1",
            file_bytes=b"# Version 1 Specification\n\nInitial baseline content for version 1.",
            original_filename="spec.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        doc = await session.get(Document, v1_res.document_id)
        ver1 = await session.get(DocumentVersion, v1_res.version_id)
        await parse_and_persist_document_content(
            session,
            ver1.id,
            "# Version 1 Specification\n\nInitial baseline content for version 1.",
            run_id=v1_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver1,
            document_id=doc.id,
            project_id=project_id,
            run_id=v1_res.run_id,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver1,
                security_partition_id=project_id,
                tenant_id=tenant_id,
                qdrant_client=qclient,
                embedder=_fake_embedder,
                run_id=v1_res.run_id,
            )
        doc.status = DocumentStatus.READY
        await session.commit()

        sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        assert len(qdrant_mock.points_for_version(col_name, ver1.id)) == 1

        # 2. Upload v2 -> v1 must STAY valid and searchable while v2 is still PENDING
        v2_res = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-replace-test:v2",
            file_bytes=b"# Version 2 Specification\n\nUpdated architecture content for version 2.",
            original_filename="spec.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        assert v2_res.version_no == 2
        await session.refresh(ver1)
        assert ver1.valid_to > datetime.now(UTC)
        ready_while_v2_pending = await _load_current_ready_versions_checked(
            session, sources, principal
        )
        assert list(ready_while_v2_pending.keys()) == [ver1.id]

        # 3. Simulate v2 indexing failure (corrupted Qdrant checksum) -> v1 must remain intact
        ver2 = await session.get(DocumentVersion, v2_res.version_id)
        await parse_and_persist_document_content(
            session,
            ver2.id,
            "# Version 2 Specification\n\nUpdated architecture content for version 2.",
            run_id=v2_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver2,
            document_id=doc.id,
            project_id=project_id,
            run_id=v2_res.run_id,
        )
        qdrant_mock.corrupt_next_scroll_checksum = True
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            with pytest.raises(RuntimeError, match="manifest verification mismatch"):
                await run_search_indexing_stage(
                    session,
                    project_id=project_id,
                    document_version=ver2,
                    security_partition_id=project_id,
                    tenant_id=tenant_id,
                    qdrant_client=qclient,
                    embedder=_fake_embedder,
                    run_id=v2_res.run_id,
                )
        await session.rollback()

        # Verify v1 is still intact in both PostgreSQL and Qdrant, and failed v2 points were cleaned up
        await session.refresh(ver1)
        await session.refresh(ver2)
        await session.refresh(doc)
        sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        assert ver1.search_status == "SEARCH_READY"
        assert ver1.valid_to > datetime.now(UTC)
        assert len(qdrant_mock.points_for_version(col_name, ver1.id)) == 1
        assert len(qdrant_mock.points_for_version(col_name, ver2.id)) == 0
        ready_after_v2_fail = await _load_current_ready_versions_checked(
            session, sources, principal
        )
        assert list(ready_after_v2_fail.keys()) == [ver1.id]

        # 4. Now run v2 indexing successfully -> v2 becomes SEARCH_READY, v1 is superseded & pruned
        await parse_and_persist_document_content(
            session,
            ver2.id,
            "# Version 2 Specification\n\nUpdated architecture content for version 2.",
            run_id=v2_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver2,
            document_id=doc.id,
            project_id=project_id,
            run_id=v2_res.run_id,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver2,
                security_partition_id=project_id,
                tenant_id=tenant_id,
                qdrant_client=qclient,
                embedder=_fake_embedder,
                run_id=v2_res.run_id,
            )

        await session.refresh(ver1)
        await session.refresh(ver2)
        assert ver2.search_status == "SEARCH_READY"
        assert ver2.supersedes_id == ver1.id
        assert ver1.valid_to == ver2.valid_from
        assert ver1.valid_to <= datetime.now(UTC)
        assert len(qdrant_mock.points_for_version(col_name, ver1.id)) == 0
        assert len(qdrant_mock.points_for_version(col_name, ver2.id)) == 1

        ready_after_v2_success = await _load_current_ready_versions_checked(
            session, sources, principal
        )
        assert list(ready_after_v2_success.keys()) == [ver2.id]


@pytest.mark.asyncio
async def test_project_document_revocation_post_ready_and_idempotency(sync_db, monkeypatch):
    """Verify project-scoped revocation immediately blocks retrieval, schedules Qdrant/DB deletion,
    and responds idempotently on repeated revocation calls.
    """
    tenant_id = "6ad76c7d938f3bf53df7335e"
    project_id = "6ad7759134bd88b3f2cc845b"
    logical_id = "continuum:project_document:doc-revoke-canary"
    principal = _make_principal(
        organization_id=tenant_id,
        tenant_id=tenant_id,
        project_ids={project_id},
        partition_ids={project_id},
    )
    qdrant_mock = InMemoryQdrantMock()
    transport = httpx.MockTransport(qdrant_mock.handle_request)
    col_name = f"search_units_{project_id}"

    async with sync_db() as session:
        up = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-revoke-canary:v1",
            file_bytes=b"# Canary Document\n\nTemporary canary document for revocation verification.",
            original_filename="canary.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        doc = await session.get(Document, up.document_id)
        ver = await session.get(DocumentVersion, up.version_id)
        await parse_and_persist_document_content(
            session,
            ver.id,
            "# Canary Document\n\nTemporary canary document for revocation verification.",
            run_id=up.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver,
            document_id=doc.id,
            project_id=project_id,
            run_id=up.run_id,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id=project_id,
                tenant_id=tenant_id,
                qdrant_client=qclient,
                embedder=_fake_embedder,
                run_id=up.run_id,
            )
        doc.status = DocumentStatus.READY
        await session.commit()

        sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        ready_before = await _load_current_ready_versions_checked(session, sources, principal)
        assert list(ready_before.keys()) == [ver.id]
        assert len(qdrant_mock.points_for_version(col_name, ver.id)) == 1

        # Revoke document
        rev_res = await revoke_project_document(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            security_partition_id=project_id,
            logical_source_id=logical_id,
            revoked_by=principal.subject,
            reason="Archived in Continuum",
        )
        assert rev_res.status == "REVOKED"
        assert rev_res.document_id == up.document_id
        assert rev_res.delete_job_id is not None
        assert rev_res.revoked_version_count == 1

        # Immediately excluded from retrieval even before DELETE_DOCUMENT worker runs
        ready_after_revoke = await _load_current_ready_versions_checked(
            session, sources, principal
        )
        assert ready_after_revoke == {}

        # Execute the queued DELETE_DOCUMENT job and verify Qdrant points + DB row are deleted
        del_job = await session.get(Job, rev_res.delete_job_id)
        assert del_job is not None
        assert del_job.type == JobType.DELETE_DOCUMENT

        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: real_async_client(transport=transport, base_url="http://qdrant.local"),
        )
        fake_engine_manager = SimpleNamespace(delete_document_data=AsyncMock())
        await _delete_document_task_unlocked(
            session,
            del_job,
            engine_manager=fake_engine_manager,
            job_queue=None,
        )

        assert len(qdrant_mock.points_for_version(col_name, ver.id)) == 0
        assert await session.get(Document, up.document_id) is None

        # Repeat revocation is idempotent (ALREADY_REVOKED)
        rev_again = await revoke_project_document(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            security_partition_id=project_id,
            logical_source_id=logical_id,
            revoked_by=principal.subject,
        )
        assert rev_again.status == "ALREADY_REVOKED"


@pytest.mark.asyncio
async def test_in_flight_worker_revocation_race_does_not_publish_or_leak_qdrant_points(sync_db):
    """Verify that if a document is revoked while run_search_indexing_stage is in flight
    (after Qdrant upsert and scroll, right before SEARCH_READY publication), the worker:
    1. Raises JobPaused instead of publishing SEARCH_READY,
    2. Rolls back uncommitted PostgreSQL changes,
    3. Cleans up the in-flight Qdrant points so zero vectors remain in Qdrant,
    4. Never makes the document visible to _load_current_ready_versions_checked.
    """
    tenant_id = "6ad76c7d938f3bf53df7335e"
    project_id = "6ad7759134bd88b3f2cc845b"
    logical_id = "continuum:project_document:doc-inflight-race"
    principal = _make_principal(
        organization_id=tenant_id,
        tenant_id=tenant_id,
        project_ids={project_id},
        partition_ids={project_id},
    )
    qdrant_mock = InMemoryQdrantMock()
    transport = httpx.MockTransport(qdrant_mock.handle_request)
    col_name = f"search_units_{project_id}"

    async with sync_db() as session:
        up = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-inflight-race:v1",
            file_bytes=b"# In-Flight Race Document\n\nContent indexed while revocation arrives.",
            original_filename="race.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        doc = await session.get(Document, up.document_id)
        doc.status = DocumentStatus.EXTRACTING
        await session.commit()

        ver = await session.get(DocumentVersion, up.version_id)
        await parse_and_persist_document_content(
            session,
            ver.id,
            "# In-Flight Race Document\n\nContent indexed while revocation arrives.",
            run_id=up.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver,
            document_id=doc.id,
            project_id=project_id,
            run_id=up.run_id,
        )

        # Hook executed right after Qdrant upsert & scroll checksum verification, BEFORE SEARCH_READY commit:
        # simulates concurrent HTTP revocation marking the document DELETING and inactive.
        async def concurrent_revoke_hook() -> None:
            doc.is_active = False
            doc.status = DocumentStatus.DELETING
            ver.valid_to = datetime.now(UTC)

        qdrant_mock.on_after_scroll_hook = concurrent_revoke_hook

        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            with pytest.raises(JobPaused):
                await run_search_indexing_stage(
                    session,
                    project_id=project_id,
                    document_version=ver,
                    security_partition_id=project_id,
                    tenant_id=tenant_id,
                    qdrant_client=qclient,
                    embedder=_fake_embedder,
                    run_id=up.run_id,
                )

        # Verify Qdrant points for the aborted in-flight version were cleaned up immediately
        assert len(qdrant_mock.points_for_version(col_name, up.version_id)) == 0

        # Verify version was NOT published as SEARCH_READY and is excluded from retrieval
        ver_after = await session.get(DocumentVersion, up.version_id, populate_existing=True)
        assert ver_after is not None
        assert ver_after.search_status != "SEARCH_READY"
        sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        ready = await _load_current_ready_versions_checked(session, sources, principal)
        assert ready == {}


@pytest.mark.asyncio
async def test_v2_db_commit_failure_rolls_back_without_deleting_v1_qdrant_points(
    sync_db, monkeypatch
):
    """Verify that if PostgreSQL session.commit() fails after v2 passes manifest verification,
    v1's Qdrant points are NEVER deleted (because Qdrant pruning of superseded_version_id happens
    strictly AFTER session.commit()), v2's Qdrant points are cleaned up, and v1 stays SEARCH_READY.
    """
    tenant_id = "6ad76c7d938f3bf53df7335e"
    project_id = "6ad7759134bd88b3f2cc845b"
    logical_id = "continuum:project_document:doc-commit-rollback"
    principal = _make_principal(
        organization_id=tenant_id,
        tenant_id=tenant_id,
        project_ids={project_id},
        partition_ids={project_id},
    )
    qdrant_mock = InMemoryQdrantMock()
    transport = httpx.MockTransport(qdrant_mock.handle_request)
    col_name = f"search_units_{project_id}"

    async with sync_db() as session:
        v1_res = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-commit-rollback:v1",
            file_bytes=b"# Version 1 Commit Guard\n\nBaseline content in v1.",
            original_filename="guard.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        doc = await session.get(Document, v1_res.document_id)
        ver1 = await session.get(DocumentVersion, v1_res.version_id)
        await parse_and_persist_document_content(
            session,
            ver1.id,
            "# Version 1 Commit Guard\n\nBaseline content in v1.",
            run_id=v1_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver1,
            document_id=doc.id,
            project_id=project_id,
            run_id=v1_res.run_id,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver1,
                security_partition_id=project_id,
                tenant_id=tenant_id,
                qdrant_client=qclient,
                embedder=_fake_embedder,
                run_id=v1_res.run_id,
            )
        doc.status = DocumentStatus.READY
        await session.commit()

        v2_res = await handle_document_upload(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            owner_id=principal.subject,
            security_partition_id=project_id,
            client_token="continuum:project_document:doc-commit-rollback:v2",
            file_bytes=b"# Version 2 Commit Guard\n\nReplacement content in v2.",
            original_filename="guard.md",
            content_type="text/markdown",
            logical_source_id=logical_id,
            organization_id=principal.organization_id,
        )
        ver2 = await session.get(DocumentVersion, v2_res.version_id)
        await parse_and_persist_document_content(
            session,
            ver2.id,
            "# Version 2 Commit Guard\n\nReplacement content in v2.",
            run_id=v2_res.run_id,
        )
        await run_dedup_and_temporal_stage(
            session,
            document_version=ver2,
            document_id=doc.id,
            project_id=project_id,
            run_id=v2_res.run_id,
        )
        await session.commit()

        real_commit = session.commit

        async def failing_commit() -> None:
            raise RuntimeError("Simulated PostgreSQL commit failure during v2 publication")

        monkeypatch.setattr(session, "commit", failing_commit)

        async with httpx.AsyncClient(transport=transport, base_url="http://qdrant.local") as qclient:
            with pytest.raises(RuntimeError, match="Simulated PostgreSQL commit failure"):
                await run_search_indexing_stage(
                    session,
                    project_id=project_id,
                    document_version=ver2,
                    security_partition_id=project_id,
                    tenant_id=tenant_id,
                    qdrant_client=qclient,
                    embedder=_fake_embedder,
                    run_id=v2_res.run_id,
                )

        monkeypatch.setattr(session, "commit", real_commit)

        # v1 Qdrant points must still be intact (1 point), and v2 Qdrant points cleaned up (0 points)
        assert len(qdrant_mock.points_for_version(col_name, v1_res.version_id)) == 1
        assert len(qdrant_mock.points_for_version(col_name, v2_res.version_id)) == 0

        # v1 in PostgreSQL must still be SEARCH_READY with valid_to open
        ver1_after = await session.get(DocumentVersion, v1_res.version_id, populate_existing=True)
        assert ver1_after is not None
        assert ver1_after.search_status == "SEARCH_READY"
        assert ver1_after.valid_to > datetime.now(UTC)

        sources = await search_source_candidates(
            session, principal=principal, include_project_sources=True
        )
        ready = await _load_current_ready_versions_checked(session, sources, principal)
        assert list(ready.keys()) == [v1_res.version_id]
