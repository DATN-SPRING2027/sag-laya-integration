from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.services import search_unit_retrieval_service as retrieval


@pytest.mark.asyncio
@pytest.mark.parametrize("search_status", ["READY", "SEARCH_READY"])
async def test_verified_search_ready_unit_flows_through_acl_dense_sparse_and_citation(search_status):
    from sag_api.core.db import SessionLocal, init_db
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
    from sag_api.services.search_index_service import generate_search_unit_point_id

    await init_db()
    suffix = uuid.uuid4().hex
    project_id = f"project-{suffix}"
    source_id = f"source-{suffix}"
    source_config_id = f"config-{suffix}"
    tenant_id = f"tenant-{suffix}"
    partition_id = f"partition-{suffix}"
    version_id = f"version-{suffix}"
    document_id = f"document-{suffix}"
    block_id = f"block-{suffix}"
    unit_id = f"unit-{suffix}"
    run_id = f"run-{suffix}"
    content = "Release identifier XK-204 is approved."
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    checksum = hashlib.sha256(f"manifest-{suffix}".encode()).hexdigest()

    async with SessionLocal() as session:
        source = Source(
            id=source_id,
            name="verified source",
            sag_source_config_id=source_config_id,
            config={},
        )
        session.add(source)
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id=source_id,
                organization_id="org-checkpoint-a",
                project_id=project_id,
                state="CONFIRMED",
                confirmed_at=now,
                confirmed_by="test-owner",
                approval_ref="test-approved-mapping",
            )
        )
        await session.flush()
        session.add(
            Document(
                id=document_id,
                source_id=source_id,
                tenant_id=tenant_id,
                project_id=project_id,
                filename="release-notes.md",
                content_type="text/markdown",
                size_bytes=len(content),
                storage_path=f"/tmp/{document_id}",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=1,
                file_hash=hashlib.sha256(b"upload").hexdigest(),
                # The producer's verified index state is independent from the
                # document-version lifecycle status, which remains RECEIVED.
                status="RECEIVED",
                search_status=search_status,
                search_ready_at=now,
                metadata_json={"security_partition_id": partition_id},
            )
        )
        await session.flush()
        session.add(
            IngestionRun(
                id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                document_version_id=version_id,
                idempotency_key=suffix,
                payload_hash=hashlib.sha256(b"payload").hexdigest(),
                status="SUCCEEDED",
                started_at=now - timedelta(seconds=2),
                completed_at=now,
                created_at=now - timedelta(seconds=2),
            )
        )
        await session.flush()
        session.add(
            CanonicalBlock(
                id=block_id,
                document_version_id=version_id,
                ordinal=0,
                block_type="paragraph",
                page_from=4,
                page_to=4,
                section_path="Release > Approval",
                source_anchor="release-approval",
                normalized_text=content,
                content_hash=content_hash,
            )
        )
        session.add(
            SearchUnit(
                id=unit_id,
                document_version_id=version_id,
                block_from_id=block_id,
                block_to_id=block_id,
                security_partition_id=partition_id,
                content_hash=content_hash,
                token_count=7,
                page_from=4,
                page_to=4,
                section_path="Release > Approval",
            )
        )
        session.add(
            StageRun(
                id=f"stage-{suffix}",
                run_id=run_id,
                stage="INDEX_SEARCH",
                status="SUCCESS",
                duration_ms=1,
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": f"search_units_{project_id}",
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        await session.commit()
        source = await session.get(Source, source_id)

    principal = VerifiedPrincipal(
        subject="user-checkpoint-a",
        organization_id="org-checkpoint-a",
        allowed_project_ids=frozenset({project_id}),
        issuer="https://issuer.invalid",
        key_id="test-key",
        token_id=f"token-{suffix}",
        issued_at=1,
        expires_at=2,
        tenant_id=tenant_id,
        allowed_partition_ids=frozenset({partition_id}),
    )

    point_id = generate_search_unit_point_id(f"search_units_{project_id}", unit_id)
    requested_vectors: list[str] = []

    async def qdrant_reply(request: httpx.Request) -> httpx.Response:
        point_payload = {
            "search_unit_id": unit_id,
            "document_version_id": version_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "security_partition_id": partition_id,
            "content_hash": content_hash,
            "content": content,
        }
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "id": point_id,
                        "score": 0.0,
                        "payload": point_payload,
                    }
                },
            )
        body = json.loads(request.content)
        assert body["filter"]["must"] == [
            {"key": "project_id", "match": {"value": project_id}},
            {"key": "tenant_id", "match": {"value": tenant_id}},
            {"key": "security_partition_id", "match": {"value": partition_id}},
            {"key": "document_version_id", "match": {"any": [version_id]}},
        ]
        requested_vectors.append(body["using"])
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": point_id,
                            "score": 0.8 if body["using"] == "content_vector" else 1.1,
                            "payload": point_payload,
                        }
                    ]
                }
            },
        )

    qdrant_client = httpx.AsyncClient(
        transport=httpx.MockTransport(qdrant_reply),
        base_url="http://qdrant-test",
    )

    class Embedder:
        async def generate(self, _query: str):
            return [0.25, 0.75]

    class SearchEngines:
        async def get_sag_embedding(self, *_args):
            return Embedder()

        async def get_search_unit_qdrant_client(self):
            return qdrant_client

    search_engines = SearchEngines()

    outcome = await retrieval.retrieve_search_unit_sections(
        search_engines,
        [source],
        "XK-204",
        principal=principal,
        top_k=5,
    )

    assert requested_vectors == ["content_vector", "bm25_sparse"] or requested_vectors == [
        "bm25_sparse",
        "content_vector",
    ]
    assert outcome.stats["canonical_index"] is True
    assert outcome.stats["fusion_method"] == "rrf"
    assert outcome.stats["semantic_candidates"] == 1
    assert outcome.stats["lexical_candidates"] == 1
    assert len(outcome.sections) == 1
    evidence = outcome.sections[0]
    assert evidence.canonical_evidence_verified is True
    assert evidence.search_unit_id == unit_id
    assert evidence.document_id == document_id
    assert evidence.document_version_id == version_id
    assert evidence.page_from == 4 and evidence.page_to == 4
    assert evidence.section_path == "Release > Approval"
    assert evidence.anchor == "release-approval"

    from sag_api.services.evidence_service import resolve_traceable_evidence

    citations = await resolve_traceable_evidence(outcome.sections, [source])
    assert citations[0].document_version_id == version_id
    assert citations[0].block_from_id == block_id
    assert citations[0].anchor == "release-approval"

    from sag_api.core.errors import NotFoundError

    async with SessionLocal() as session:
        clicked = await retrieval.get_search_unit_citation(
            session,
            source=source,
            principal=principal,
            search_unit_id=unit_id,
            engine_manager=search_engines,
        )
        with pytest.raises(NotFoundError):
            await retrieval.get_search_unit_citation(
                session,
                source=source,
                principal=replace(principal, allowed_project_ids=frozenset({"other-project"})),
                search_unit_id=unit_id,
                engine_manager=search_engines,
            )

    from sag_api.api.v1.sources import get_chunk

    async with SessionLocal() as session:
        clicked_route = await get_chunk(
            source_id,
            unit_id,
            _user=None,
            source=source,
            principal=principal,
            session=session,
            engine_manager=search_engines,
        )

    assert clicked["content"] == content
    assert clicked["document_id"] == document_id
    assert clicked["document_version_id"] == version_id
    assert clicked["search_unit_id"] == unit_id
    assert clicked["block_from_id"] == block_id
    assert clicked["page_from"] == 4 and clicked["page_to"] == 4
    assert clicked["section_path"] == "Release > Approval"
    assert clicked["anchor"] == "release-approval"
    assert clicked_route["content"] == content
    assert clicked_route["search_unit_id"] == unit_id

    async with SessionLocal() as session:
        version = await session.get(DocumentVersion, version_id)
        version.search_status = "INDEX_FAILED"
        await session.commit()

    unavailable = await retrieval.retrieve_search_unit_sections(
        search_engines,
        [source],
        "XK-204",
        principal=principal,
        top_k=5,
    )
    assert unavailable.sections == []
    assert len(requested_vectors) == 2  # an unready version never reaches Qdrant
    async with SessionLocal() as session:
        with pytest.raises(NotFoundError):
            await retrieval.get_search_unit_citation(
                session,
                source=source,
                principal=principal,
                search_unit_id=unit_id,
                engine_manager=search_engines,
            )

    await qdrant_client.aclose()


@pytest.mark.asyncio
async def test_scope_database_errors_are_sanitized_before_tool_trace():
    from types import SimpleNamespace

    from sag_api.core.errors import ServiceUnavailableError

    source = SimpleNamespace(id="source-safe", name="Safe", sag_source_config_id="cfg-safe")
    principal = VerifiedPrincipal(
        subject="user-safe",
        organization_id="org-safe",
        allowed_project_ids=frozenset({"project-safe"}),
        issuer="https://issuer.invalid",
        key_id="test-key",
        token_id="token-safe",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-safe",
        allowed_partition_ids=frozenset({"partition-safe"}),
    )

    class BrokenSession:
        async def scalars(self, _statement):
            raise OperationalError(
                "SELECT secret_db_password FROM protected_table",
                {},
                RuntimeError("postgresql://user:secret-password@db.internal/private"),
            )

    with pytest.raises(ServiceUnavailableError) as error:
        await retrieval._load_current_ready_versions(BrokenSession(), [source], principal)

    assert "secret-password" not in str(error.value)
    assert "db.internal" not in str(error.value)


@pytest.mark.asyncio
async def test_citation_database_errors_are_sanitized():
    from types import SimpleNamespace

    from sag_api.core.errors import ServiceUnavailableError

    source = SimpleNamespace(id="source-safe", name="Safe", sag_source_config_id="cfg-safe")
    principal = VerifiedPrincipal(
        subject="user-safe",
        organization_id="org-safe",
        allowed_project_ids=frozenset({"project-safe"}),
        issuer="https://issuer.invalid",
        key_id="test-key",
        token_id="token-safe",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-safe",
        allowed_partition_ids=frozenset({"partition-safe"}),
    )

    class BrokenSession:
        async def scalar(self, _statement):
            raise OperationalError(
                "SELECT secret_db_password FROM protected_table",
                {},
                RuntimeError("postgresql://user:secret-password@db.internal/private"),
            )

    with pytest.raises(ServiceUnavailableError) as error:
        await retrieval.get_search_unit_citation(
            BrokenSession(),
            source=source,
            principal=principal,
            search_unit_id="unit-safe",
            engine_manager=object(),
        )

    assert "secret-password" not in str(error.value)
    assert "db.internal" not in str(error.value)


@pytest.mark.asyncio
async def test_candidate_hydration_database_errors_are_sanitized(monkeypatch):
    from types import SimpleNamespace

    from sag_api.core.errors import ServiceUnavailableError

    source = SimpleNamespace(id="source-safe", name="Safe", sag_source_config_id="cfg-safe")
    principal = VerifiedPrincipal(
        subject="user-safe",
        organization_id="org-safe",
        allowed_project_ids=frozenset({"project-safe"}),
        issuer="https://issuer.invalid",
        key_id="test-key",
        token_id="token-safe",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-safe",
        allowed_partition_ids=frozenset({"partition-safe"}),
    )
    ready = retrieval._ReadyVersion(
        document_version_id="version-safe",
        source_id=source.id,
        project_id="project-safe",
        tenant_id="tenant-safe",
        partition_id="partition-safe",
        source=source,
    )
    monkeypatch.setattr(
        retrieval,
        "_load_current_ready_versions",
        AsyncMock(return_value={ready.document_version_id: ready}),
    )

    class BrokenSession:
        async def execute(self, _statement):
            raise OperationalError(
                "SELECT secret_db_password FROM protected_table",
                {},
                RuntimeError("postgresql://user:secret-password@db.internal/private"),
            )

    class SessionContext:
        async def __aenter__(self):
            return BrokenSession()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(retrieval, "SessionLocal", SessionContext)

    async def qdrant_reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": "candidate-point",
                            "score": 0.9,
                            "payload": {
                                "search_unit_id": "unit-safe",
                                "project_id": "project-safe",
                                "tenant_id": "tenant-safe",
                                "security_partition_id": "partition-safe",
                                "document_version_id": "version-safe",
                            },
                        }
                    ]
                }
            },
        )

    qdrant_client = httpx.AsyncClient(
        transport=httpx.MockTransport(qdrant_reply),
        base_url="http://qdrant-test",
    )

    class Embedder:
        async def generate(self, _query: str):
            return [0.25, 0.75]

    class SearchEngines:
        async def get_sag_embedding(self, *_args):
            return Embedder()

        async def get_search_unit_qdrant_client(self):
            return qdrant_client

    with pytest.raises(ServiceUnavailableError) as error:
        await retrieval.retrieve_search_unit_sections(
            SearchEngines(),
            [source],
            "XK-204",
            principal=principal,
            top_k=5,
        )

    assert "secret-password" not in str(error.value)
    assert "db.internal" not in str(error.value)
    await qdrant_client.aclose()
