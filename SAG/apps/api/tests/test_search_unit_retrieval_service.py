from __future__ import annotations

import asyncio
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
from sag_api.sag.search_unit_store import SearchUnitHit
from sag_api.services import search_unit_retrieval_service as retrieval


def test_candidate_pool_is_bounded_and_round_robins_authorized_scopes():
    from types import SimpleNamespace

    hits = {}
    for source_id in ("source-a", "source-b", "source-c"):
        group = retrieval._SearchGroup(
            project_id="project-1",
            tenant_id="tenant-1",
            partition_id="partition-1",
            source=SimpleNamespace(id=source_id),
            versions=("version-1",),
        )
        for rank, unit_id in enumerate((f"{source_id}-top", f"{source_id}-next")):
            key = (
                "dense",
                group.project_id,
                source_id,
                group.tenant_id,
                group.partition_id,
                unit_id,
            )
            hits[key] = (group, SearchUnitHit(unit_id, 0.9 - rank * 0.1, {}), 1.0, rank, "global_only")

    result = retrieval._bounded_candidate_unit_ids(hits, limit=4)

    assert result == ["source-a-top", "source-b-top", "source-c-top", "source-a-next"]


@pytest.mark.parametrize(
    ("has_evidence", "required_anchors", "anchor_coverage", "expected"),
    [
        (False, 1, "missing_required_anchor", "empty_evidence"),
        (True, 0, "not_required", "unknown"),
        (True, 2, "missing_required_anchor", "weak"),
        (True, 2, "complete", "structurally_sufficient"),
    ],
)
def test_structural_coverage_states_do_not_claim_semantic_answerability(
    has_evidence,
    required_anchors,
    anchor_coverage,
    expected,
):
    actual = retrieval._structural_coverage_status(
        1 if has_evidence else 0,
        {"required_anchor_count": required_anchors, "anchor_coverage": anchor_coverage},
    )

    assert actual == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("search_status", ["READY", "SEARCH_READY"])
@pytest.mark.parametrize(
    "route_mode",
    [
        "global_only",
        "correct_route",
        "wrong_route",
        "wrong_route_nonempty",
        "local_timeout",
        "rerank_budget_exhausted",
    ],
)
async def test_verified_search_ready_unit_flows_through_acl_dense_sparse_and_citation(
    search_status,
    route_mode,
    monkeypatch,
):
    from sag_api.core.config import settings

    if route_mode == "local_timeout":
        monkeypatch.setattr(settings, "search_source_timeout", 1.0)
        monkeypatch.setattr(settings, "search_tree_escape_reserve_seconds", 0.4)
    elif route_mode == "rerank_budget_exhausted":
        monkeypatch.setattr(settings, "search_rerank_min_remaining_seconds", 999.0)
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
    decoy_block_id = f"block-decoy-{suffix}"
    decoy_unit_id = f"unit-decoy-{suffix}"
    run_id = f"run-{suffix}"
    content = "Release identifier XK-204 is approved."
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    decoy_content = "A different branch contains an outdated approval note."
    decoy_content_hash = hashlib.sha256(decoy_content.encode("utf-8")).hexdigest()
    has_decoy = route_mode == "wrong_route_nonempty"
    unit_count = 2 if has_decoy else 1
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
        if has_decoy:
            session.add(
                CanonicalBlock(
                    id=decoy_block_id,
                    document_version_id=version_id,
                    ordinal=1,
                    block_type="paragraph",
                    page_from=5,
                    page_to=5,
                    section_path="Release > Superseded approval",
                    source_anchor="superseded-approval",
                    normalized_text=decoy_content,
                    content_hash=decoy_content_hash,
                )
            )
            session.add(
                SearchUnit(
                    id=decoy_unit_id,
                    document_version_id=version_id,
                    block_from_id=decoy_block_id,
                    block_to_id=decoy_block_id,
                    security_partition_id=partition_id,
                    content_hash=decoy_content_hash,
                    token_count=9,
                    page_from=5,
                    page_to=5,
                    section_path="Release > Superseded approval",
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
                    "search_unit_count": unit_count,
                    "pg_count": unit_count,
                    "qdrant_count": unit_count,
                    "qdrant_indexed_count": unit_count,
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
        decoy_point_payload = {
            "search_unit_id": decoy_unit_id,
            "document_version_id": version_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "security_partition_id": partition_id,
            "content_hash": decoy_content_hash,
            "content": decoy_content,
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
        expected_must = [
            {"key": "project_id", "match": {"value": project_id}},
            {"key": "tenant_id", "match": {"value": tenant_id}},
            {"key": "security_partition_id", "match": {"value": partition_id}},
            {"key": "document_version_id", "match": {"any": [version_id]}},
        ]
        if route_mode != "global_only" and "should" in body["filter"]:
            selected_leaf = (
                "leaf-wrong" if route_mode in {"wrong_route", "wrong_route_nonempty"} else "leaf-1"
            )
            expected_must.append({"key": "tree_version_a", "match": {"value": "tree-v1"}})
            assert body["filter"]["should"] == [
                {"key": "primary_node_a", "match": {"any": [selected_leaf]}},
                {"key": "secondary_node_ids_a", "match": {"any": [selected_leaf]}},
            ]
        assert body["filter"]["must"] == expected_must
        requested_vectors.append(body["using"])
        if route_mode == "wrong_route" and "should" in body["filter"]:
            return httpx.Response(200, json={"result": {"points": []}})
        if route_mode == "wrong_route_nonempty" and "should" in body["filter"]:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": generate_search_unit_point_id(
                                    f"search_units_{project_id}", decoy_unit_id
                                ),
                                "score": 0.8 if body["using"] == "content_vector" else 1.1,
                                "payload": decoy_point_payload,
                            }
                        ]
                    }
                },
            )
        if route_mode == "local_timeout" and "should" in body["filter"]:
            await asyncio.sleep(0.8)
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

        async def get_routing_snapshot(self, *, query, scopes, planner):
            from sag_api.services.query_routing_service import (
                GroupRoutingSnapshot,
                NodeProfile,
                RoutingSnapshot,
                scope_fingerprint,
            )

            del query, planner
            if route_mode == "global_only":
                return None
            scope = scopes[0]
            fingerprint = scope_fingerprint(scope)
            captured = datetime.now(UTC)
            profile = NodeProfile(
                node_id="leaf-wrong" if route_mode in {"wrong_route", "wrong_route_nonempty"} else "leaf-1",
                parent_id=None,
                is_leaf=True,
                accessible_unit_count=1,
                project_id=scope["project_id"],
                source_ids=tuple(scope["source_ids"]),
                document_version_ids=tuple(scope["document_version_ids"]),
                tenant_id=scope["tenant_id"],
                partition_id=scope["partition_id"],
                tree_version="tree-v1",
                scope_fingerprint=fingerprint,
                signal_scores={"dense": 0.9},
            )
            group_snapshot = GroupRoutingSnapshot(
                snapshot_id="tree-snapshot-1",
                captured_at=captured,
                project_id=scope["project_id"],
                source_ids=tuple(scope["source_ids"]),
                document_version_ids=tuple(scope["document_version_ids"]),
                tenant_id=scope["tenant_id"],
                partition_id=scope["partition_id"],
                scope_fingerprint=fingerprint,
                tree_version="tree-v1",
                routing_slot="SLOT_A",
                search_epoch=1,
                manifest_status="ACTIVE",
                manifest_checksum="a" * 64,
                manifest_verified=True,
                profiles=(profile,),
            )
            return RoutingSnapshot(snapshot_id="request-snapshot-1", captured_at=captured, groups=(group_snapshot,))

    search_engines = SearchEngines()

    query = "summarize release" if route_mode == "wrong_route_nonempty" else "XK-204"
    outcome = await retrieval.retrieve_search_unit_sections(
        search_engines,
        [source],
        query,
        principal=principal,
        top_k=5,
    )
    has_route = route_mode != "global_only"
    assert sorted(requested_vectors) == sorted(["content_vector", "bm25_sparse"] * (2 if has_route else 1))
    assert outcome.stats["canonical_index"] is True
    assert outcome.stats["fusion_method"] == (
        "rank_interleave_fallback" if route_mode == "rerank_budget_exhausted" else "rrf"
    )
    assert outcome.stats["routing"]["planner"]["planner_version"] == "qsp-v1"
    route_trace = outcome.stats["routing"]["groups"][0]
    assert route_trace["branch_local_candidates"] == (
        1 if route_mode in {"correct_route", "wrong_route_nonempty", "rerank_budget_exhausted"} else 0
    )
    assert route_trace["global_candidates"] == 1
    assert route_trace["escape_recovered_candidates"] == (
        1 if route_mode in {"wrong_route", "wrong_route_nonempty", "local_timeout"} else 0
    )
    assert route_trace["blackhole_detected"] is (route_mode == "wrong_route")
    assert route_trace["tree_version"] == ("tree-v1" if has_route else None)
    if route_mode == "local_timeout":
        assert route_trace["local_failure"] == "QUERY_TIMEOUT"
        assert outcome.stats["routing"]["fallback_used"] is True
    if route_mode == "rerank_budget_exhausted":
        assert outcome.stats["selection_fallback_reason"] == "rerank_budget_exhausted"
        assert outcome.stats["selection_method"] == "latency_fallback_rank_interleave"
        assert outcome.stats["routing"]["fallback_used"] is True
    expected_candidate_count = 2 if has_decoy else 1
    assert outcome.stats["semantic_candidates"] == expected_candidate_count
    assert outcome.stats["lexical_candidates"] == expected_candidate_count
    indexed_request_count = len(requested_vectors)
    assert len(outcome.sections) == expected_candidate_count
    if route_mode == "wrong_route_nonempty":
        assert [section.search_unit_id for section in outcome.sections] == [decoy_unit_id, unit_id]
    evidence = next(section for section in outcome.sections if section.search_unit_id == unit_id)
    assert evidence.canonical_evidence_verified is True
    assert evidence.search_unit_id == unit_id
    assert evidence.document_id == document_id
    assert evidence.document_version_id == version_id
    assert evidence.page_from == 4 and evidence.page_to == 4
    assert evidence.section_path == "Release > Approval"
    assert evidence.anchor == "release-approval"

    from sag_api.services.evidence_service import resolve_traceable_evidence

    citations = await resolve_traceable_evidence(outcome.sections, [source])
    citation = next(item for item in citations if item.search_unit_id == unit_id)
    assert citation.document_version_id == version_id
    assert citation.block_from_id == block_id
    assert citation.anchor == "release-approval"

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
    assert len(requested_vectors) == indexed_request_count  # an unready version never reaches Qdrant
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
