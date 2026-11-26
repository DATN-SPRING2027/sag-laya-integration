"""TDD Phase 2 tests: 4-path ACL-aware unit supersession overlay, cache digest,
Call #3 _verify_selection_scope race, and ACL-safe citation badge.

Covers all 5 Phase 2 test groups from planner_2_handoff.md:783-799:
1. Cross-partition ACL isolation (Seam A — zero side-channel across all 4 surfaces).
2. Live winner lifecycle immunity (doc inactive, version revoked, mapping revoked,
   active DELETE_DOCUMENT / REPROCESS_DOCUMENT jobs) with zero Qdrant mutation.
3. Temporal accuracy (current_effective, past_point before/after effective_valid_to,
   change_lineage stamping + byte-immutable content).
4. All 4 surfaces + _SCOPE_KNOWLEDGE_INDEX_CACHE digest invalidation + non-mutating
   sibling/multi-hop hydration + grep exclusive vs sibling coverage & exact LIMIT +
   catalog backfill + Call #3 _verify_selection_scope mid-flight supersession race +
   get_search_unit_citation 200 with ACL-safe badge metadata.
5. Active unresolved conflict (PENDING_REVIEW) -> both units returned with
   conflict_status="ACTIVE_CONFLICT" and synchronized (Xung đột ...) across all 3 sites.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    KnowledgeConflictResolution,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
)
from sag_api.db.models.conflict_resolution import compute_conflict_pair_key
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
    DocumentStatus,
    JobStatus,
    JobType,
)
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services import agent_service, evidence_service
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services.query_strategy_planner import plan_query
from sag_api.services.search_index_service import generate_search_unit_point_id
from sag_api.services.temporal_metadata_format import format_evidence_metadata_suffix
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool, _format_sections

TENANT_ID = "tenant-p2b"
ORG_ID = "org-p2b"
PROJECT_ID = "proj-p2b"
P_PUBLIC = "part-public"
P_EXEC = "part-exec"
VALID_FROM_2025 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
VALID_TO_INF = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
CUTOFF_JUNE = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _principal(partitions: set[str] | frozenset[str]) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p2b",
        organization_id=ORG_ID,
        issuer="https://issuer.test",
        key_id="k-p2b",
        token_id="tok-p2b",
        issued_at=1000,
        expires_at=9999999999,
        tenant_id=TENANT_ID,
        allowed_project_ids=frozenset({PROJECT_ID}),
        allowed_partition_ids=frozenset(partitions),
    )


def _qdrant_point(
    *,
    version_id: str,
    unit_id: str,
    partition_id: str,
    content: str,
    score: float,
) -> dict:
    return {
        "id": generate_search_unit_point_id(f"search_units_{PROJECT_ID}", unit_id),
        "score": score,
        "payload": {
            "search_unit_id": unit_id,
            "document_version_id": version_id,
            "project_id": PROJECT_ID,
            "tenant_id": TENANT_ID,
            "security_partition_id": partition_id,
            "content_hash": _sha256(content),
            "content": content,
        },
    }


def _make_mock_qdrant_client(points_by_version: dict[str, list[dict]]) -> httpx.AsyncClient:
    points_by_point_id = {
        pt["id"]: pt for pts in points_by_version.values() for pt in pts
    }

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/points/count"):
            body = json.loads(request.content)
            must = (body.get("filter") or {}).get("must") or []
            allowed_vids: set[str] | None = None
            for cond in must:
                if cond.get("key") == "document_version_id":
                    match = cond.get("match") or {}
                    if "any" in match:
                        allowed_vids = set(match["any"])
                    elif "value" in match:
                        allowed_vids = {match["value"]}
            total = sum(
                len(pts)
                for vid, pts in points_by_version.items()
                if allowed_vids is None or vid in allowed_vids
            )
            return httpx.Response(200, json={"result": {"count": total}})
        if path.endswith("/points/query/batch"):
            body = json.loads(request.content)
            results = []
            for search_req in body.get("searches", []):
                must = (search_req.get("filter") or {}).get("must") or []
                allowed_vids = None
                allowed_part = None
                for cond in must:
                    key = cond.get("key")
                    match = cond.get("match") or {}
                    if key == "document_version_id":
                        if "any" in match:
                            allowed_vids = set(match["any"])
                        elif "value" in match:
                            allowed_vids = {match["value"]}
                    elif key == "security_partition_id" and "value" in match:
                        allowed_part = match["value"]
                matched_points = []
                for vid, pts in points_by_version.items():
                    if allowed_vids is not None and vid not in allowed_vids:
                        continue
                    for pt in pts:
                        if allowed_part is not None and pt["payload"]["security_partition_id"] != allowed_part:
                            continue
                        matched_points.append(pt)
                results.append({"points": matched_points})
            return httpx.Response(200, json={"result": results})
        if "/points/" in path and request.method == "GET":
            point_id = path.rsplit("/points/", 1)[-1]
            pt = points_by_point_id.get(point_id)
            if pt is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(
                200,
                json={"result": {"id": pt["id"], "payload": pt["payload"]}},
            )
        raise AssertionError(f"Unexpected Qdrant request: {request.method} {path}")

    return httpx.AsyncClient(
        base_url="https://qdrant.invalid",
        transport=httpx.MockTransport(respond),
    )


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


async def _make_db(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(surs, "SessionLocal", factory)
    surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
    return engine, factory


async def _seed_source(session: AsyncSession, *, source_id: str, name: str) -> Source:
    now = datetime.now(UTC)
    src = Source(id=source_id, name=name, sag_source_config_id=f"cfg-{source_id}")
    session.add(src)
    await session.flush()
    session.add(
        SourceProjectMapping(
            id=f"spm-{source_id}",
            source_id=source_id,
            organization_id=ORG_ID,
            project_id=PROJECT_ID,
            state="CONFIRMED",
            mapping_version=1,
            confirmed_at=now,
            confirmed_by="admin",
            approval_ref=f"appr-{source_id}",
        )
    )
    await session.flush()
    return src


async def _seed_doc_version(
    session: AsyncSession,
    *,
    source_id: str,
    doc_id: str,
    version_id: str,
    filename: str,
    partition_id: str,
    units: list[tuple[str, str, str]],
    valid_from: datetime = VALID_FROM_2025,
    valid_to: datetime = VALID_TO_INF,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    version_no: int = 1,
) -> dict[str, str]:
    now = datetime.now(UTC)
    existing_doc = await session.get(Document, doc_id)
    if existing_doc is None:
        session.add(
            Document(
                id=doc_id,
                source_id=source_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                filename=filename,
                storage_path=f"/tmp/{doc_id}.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

    session.add(
        DocumentVersion(
            id=version_id,
            document_id=doc_id,
            version_no=version_no,
            file_hash=_sha256(f"fh-{version_id}"),
            valid_from=valid_from,
            valid_to=valid_to,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            authority_tier=authority_tier,
            effective_from=effective_from,
            metadata_json={"security_partition_id": partition_id},
        )
    )
    await session.flush()

    for ordinal, (unit_id, heading, content) in enumerate(units):
        digest = _sha256(content)
        blk_id = f"blk-{unit_id}"
        session.add(
            CanonicalBlock(
                id=blk_id,
                document_version_id=version_id,
                ordinal=ordinal,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path=heading,
                source_anchor=f"{version_id}-b{ordinal}",
                normalized_text=content,
                content_hash=digest,
            )
        )
        await session.flush()
        session.add(
            SearchUnit(
                id=unit_id,
                document_version_id=version_id,
                block_from_id=blk_id,
                block_to_id=blk_id,
                security_partition_id=partition_id,
                content_hash=digest,
                token_count=max(1, len(content.split())),
                page_from=1,
                page_to=1,
                section_path=heading,
            )
        )

    run = IngestionRun(
        id=f"run-{version_id}",
        document_version_id=version_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        idempotency_key=f"idem-{version_id}",
        payload_hash=f"ph-{version_id}",
        status="COMPLETED",
        attempt_count=1,
        started_at=now,
    )
    session.add(run)
    await session.flush()
    checksum = "c" * 64
    session.add(
        StageRun(
            id=f"sr-{version_id}",
            run_id=run.id,
            stage="INDEX_SEARCH",
            status="SUCCESS",
            created_at=now,
            metrics_json={
                "manifest_verified": True,
                "collection_name": f"search_units_{PROJECT_ID}",
                "search_unit_count": len(units),
                "pg_count": len(units),
                "qdrant_count": len(units),
                "qdrant_indexed_count": len(units),
                "manifest_checksum": checksum,
                "qdrant_checksum": checksum,
                "attempt_count": 1,
            },
        )
    )
    await session.flush()
    return {
        "source_id": source_id,
        "document_id": doc_id,
        "version_id": version_id,
        "partition_id": partition_id,
    }


async def _seed_kcr(
    session: AsyncSession,
    *,
    res_id: str,
    winner_doc_id: str,
    winner_ver_id: str,
    winner_unit_id: str,
    winner_partition: str,
    loser_doc_id: str,
    loser_ver_id: str,
    loser_unit_id: str,
    loser_partition: str,
    status: str = ConflictResolutionStatus.ACTIVE.value,
    resolution_mode: str = ConflictResolutionMode.AUTO_POLICY.value,
    conflict_subtype: str = ConflictSubtype.DIRECT_SUPERSEDES.value,
    effective_valid_to: datetime | None = CUTOFF_JUNE,
) -> KnowledgeConflictResolution:
    row = KnowledgeConflictResolution(
        id=res_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        security_partition_id=loser_partition,
        winner_security_partition_id=winner_partition,
        pair_key=compute_conflict_pair_key(
            winner_ver_id,
            winner_unit_id,
            loser_ver_id,
            loser_unit_id,
        ),
        winner_document_id=winner_doc_id,
        winner_version_id=winner_ver_id,
        winner_unit_id=winner_unit_id,
        winner_content_hash=_sha256(f"w-{res_id}"),
        loser_document_id=loser_doc_id,
        loser_version_id=loser_ver_id,
        loser_unit_id=loser_unit_id,
        loser_content_hash=_sha256(f"l-{res_id}"),
        conflict_subtype=conflict_subtype,
        status=status,
        resolution_mode=resolution_mode,
        confidence=0.95,
        effective_valid_to=effective_valid_to,
        reason="Superseded by newer authoritative clause",
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Group 1: Cross-partition ACL isolation (Seam A) across ALL 4 surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_partition_acl_isolation_across_all_4_retrieval_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Winner unit_B (P_exec) supersedes loser unit_A (P_public).
    - Principal {P_public, P_exec}: sees unit_B and NOT unit_A across retrieve,
      siblings, grep, and catalog backfill.
    - Principal {P_public} ONLY: STILL sees unit_A across ALL 4 surfaces (zero side-channel!).
    """
    db_engine, factory = await _make_db(monkeypatch)
    c_a = "Remote work stipend policy grants 500 USD per year for home office setup."
    c_a2 = "Remote work stipend checklist covers ergonomic desk chair and external display."
    c_b = "Remote work stipend policy grants 1200 USD per year under updated executive directive."

    async with factory() as session:
        src_pub = await _seed_source(session, source_id="src-pub", name="Public HR")
        src_exec = await _seed_source(session, source_id="src-exec", name="Executive HR")
        await _seed_doc_version(
            session,
            source_id=src_pub.id,
            doc_id="doc-pub",
            version_id="ver-pub",
            filename="public_stipend.md",
            partition_id=P_PUBLIC,
            units=[
                ("unit-A", "Stipend Amount", c_a),
                ("unit-A2", "Equipment Checklist", c_a2),
            ],
            authority_tier=3,
            effective_from=VALID_FROM_2025,
        )
        await _seed_doc_version(
            session,
            source_id=src_exec.id,
            doc_id="doc-exec",
            version_id="ver-exec",
            filename="exec_stipend.md",
            partition_id=P_EXEC,
            units=[("unit-B", "Updated Stipend Amount", c_b)],
            authority_tier=1,
            effective_from=CUTOFF_JUNE,
        )
        await _seed_kcr(
            session,
            res_id="res-seam-a",
            winner_doc_id="doc-exec",
            winner_ver_id="ver-exec",
            winner_unit_id="unit-B",
            winner_partition=P_EXEC,
            loser_doc_id="doc-pub",
            loser_ver_id="ver-pub",
            loser_unit_id="unit-A",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await session.commit()

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    qdrant_points = {
        "ver-pub": [
            _qdrant_point(version_id="ver-pub", unit_id="unit-A", partition_id=P_PUBLIC, content=c_a, score=0.91),
            _qdrant_point(version_id="ver-pub", unit_id="unit-A2", partition_id=P_PUBLIC, content=c_a2, score=0.86),
        ],
        "ver-exec": [
            _qdrant_point(version_id="ver-exec", unit_id="unit-B", partition_id=P_EXEC, content=c_b, score=0.93),
        ],
    }

    p_exec = _principal({P_PUBLIC, P_EXEC})
    p_pub = _principal({P_PUBLIC})
    sources = [src_pub, src_exec]
    query = "What is the remote work stipend policy and checklist?"

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))

            # Surface 1: Main retrieval
            out_exec = await surs._retrieve_search_unit_sections(
                engine,
                sources,
                query,
                principal=p_exec,
                top_k=5,
                query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            exec_ids = {s.search_unit_id for s in out_exec.sections}
            assert "unit-B" in exec_ids
            assert "unit-A" not in exec_ids

            out_pub = await surs._retrieve_search_unit_sections(
                engine,
                sources,
                query,
                principal=p_pub,
                top_k=5,
                query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            pub_ids = {s.search_unit_id for s in out_pub.sections}
            assert "unit-A" in pub_ids
            assert "unit-B" not in pub_ids
            unit_a_sec = next(s for s in out_pub.sections if s.search_unit_id == "unit-A")
            assert unit_a_sec.is_superseded is False
            assert unit_a_sec.conflict_status is None

            # Surface 2: Sibling hydration (seeded with ONLY unit-A2; unit-A is its sibling)
            qdrant_a2_only = {
                "ver-pub": [
                    _qdrant_point(
                        version_id="ver-pub",
                        unit_id="unit-A2",
                        partition_id=P_PUBLIC,
                        content=c_a2,
                        score=0.92,
                    ),
                ],
                "ver-exec": [
                    _qdrant_point(
                        version_id="ver-exec",
                        unit_id="unit-B",
                        partition_id=P_EXEC,
                        content=c_b,
                        score=0.93,
                    ),
                ],
            }
            async with _make_mock_qdrant_client(qdrant_a2_only) as sib_client:
                sib_engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=sib_client))
                sib_exec = await surs._retrieve_search_unit_sections(
                    sib_engine,
                    sources,
                    query,
                    principal=p_exec,
                    top_k=5,
                    query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                    candidate_limit=10,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
                assert "unit-A" not in {s.search_unit_id for s in sib_exec.sections}

                sib_pub = await surs._retrieve_search_unit_sections(
                    sib_engine,
                    sources,
                    query,
                    principal=p_pub,
                    top_k=5,
                    query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                    candidate_limit=10,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
                assert "unit-A" in {s.search_unit_id for s in sib_pub.sections}

            # Surface 3: grep_search_unit_blocks
            async with factory() as session:
                grep_exec = await surs.grep_search_unit_blocks(
                    session,
                    sources,
                    "Remote work stipend",
                    principal=p_exec,
                    limit=10,
                )
                assert {b.id for b in grep_exec} == {"blk-unit-A2", "blk-unit-B"}

                grep_pub = await surs.grep_search_unit_blocks(
                    session,
                    sources,
                    "Remote work stipend",
                    principal=p_pub,
                    limit=10,
                )
                assert {b.id for b in grep_pub} == {"blk-unit-A", "blk-unit-A2"}

            # Surface 4: Catalog backfill (only unit-B returned by Qdrant so ver-pub is backfilled)
            catalog_plan = plan_query(
                "List all documents in this project",
                requested_top_k=5,
                route_decision={
                    "is_catalog_query": True,
                    "target_unit": "document",
                    "answer_shape": "document_list",
                    "temporal_mode": "current_effective",
                },
            )
            assert catalog_plan.retrieval_plan.is_unfiltered_catalog is True
            assert catalog_plan.retrieval_plan.per_document_limit == 1
            assert catalog_plan.retrieval_plan.output.value == "LIST"

            # For p_exec: Qdrant returns only unit-B; ver-pub is backfilled -> skips unit-A (#0), picks unit-A2 (#1)!
            cat_points_exec = {
                "ver-pub": [
                    _qdrant_point(
                        version_id="ver-pub", unit_id="unit-A", partition_id=P_PUBLIC, content=c_a, score=0.0
                    ),
                    _qdrant_point(
                        version_id="ver-pub", unit_id="unit-A2", partition_id=P_PUBLIC, content=c_a2, score=0.0
                    ),
                ],
                "ver-exec": [
                    _qdrant_point(
                        version_id="ver-exec", unit_id="unit-B", partition_id=P_EXEC, content=c_b, score=0.92
                    ),
                ],
            }

            def respond_catalog_exec(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/points/query/batch"):
                    body = json.loads(request.content)
                    # Return ONLY unit-B from vector search so ver-pub MUST come from catalog backfill
                    return httpx.Response(
                        200,
                        json={"result": [{"points": cat_points_exec["ver-exec"]} for _ in body.get("searches", [])]},
                    )
                if "/points/" in request.url.path and request.method == "GET":
                    pid = request.url.path.rsplit("/points/", 1)[-1]
                    for pts in cat_points_exec.values():
                        for pt in pts:
                            if pt["id"] == pid:
                                return httpx.Response(200, json={"result": {"id": pt["id"], "payload": pt["payload"]}})
                    return httpx.Response(404, json={"status": {"error": "not found"}})
                return httpx.Response(200, json={"result": {"count": 3}})

            async with httpx.AsyncClient(
                base_url="https://qdrant.invalid",
                transport=httpx.MockTransport(respond_catalog_exec),
            ) as cat_exec_client:
                cat_exec_eng = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=cat_exec_client))
                out_cat_exec = await surs._retrieve_search_unit_sections(
                    cat_exec_eng,
                    sources,
                    "List all documents in this project",
                    principal=p_exec,
                    top_k=5,
                    query_strategy_plan=catalog_plan,
                    candidate_limit=10,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
                assert {s.search_unit_id for s in out_cat_exec.sections} == {"unit-B", "unit-A2"}

            # For p_pub: add second public doc so ver-pub is backfilled -> picks unit-A (#0) since winner is invisible!
            async with factory() as session:
                c_pub2 = "Office parking guide for all employees at headquarters."
                await _seed_doc_version(
                    session,
                    source_id=src_pub.id,
                    doc_id="doc-pub2",
                    version_id="ver-pub2",
                    filename="parking.md",
                    partition_id=P_PUBLIC,
                    units=[("unit-P2", "Parking", c_pub2)],
                )
                await session.commit()

            pt_pub2 = _qdrant_point(
                version_id="ver-pub2", unit_id="unit-P2", partition_id=P_PUBLIC, content=c_pub2, score=0.92
            )
            cat_pub_lookup = {
                pt["id"]: pt
                for pt in [*cat_points_exec["ver-pub"], pt_pub2]
            }

            def respond_catalog_pub(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/points/query/batch"):
                    body = json.loads(request.content)
                    return httpx.Response(
                        200,
                        json={"result": [{"points": [pt_pub2]} for _ in body.get("searches", [])]},
                    )
                if "/points/" in request.url.path and request.method == "GET":
                    pid = request.url.path.rsplit("/points/", 1)[-1]
                    pt = cat_pub_lookup.get(pid)
                    if pt is not None:
                        return httpx.Response(200, json={"result": {"id": pt["id"], "payload": pt["payload"]}})
                    return httpx.Response(404, json={"status": {"error": "not found"}})
                return httpx.Response(200, json={"result": {"count": 3}})

            async with httpx.AsyncClient(
                base_url="https://qdrant.invalid",
                transport=httpx.MockTransport(respond_catalog_pub),
            ) as cat_pub_client:
                cat_pub_eng = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=cat_pub_client))
                out_cat_pub = await surs._retrieve_search_unit_sections(
                    cat_pub_eng,
                    sources,
                    "List all documents in this project",
                    principal=p_pub,
                    top_k=5,
                    query_strategy_plan=catalog_plan,
                    candidate_limit=10,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
                assert {s.search_unit_id for s in out_cat_pub.sections} == {"unit-P2", "unit-A"}
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 2: Live winner lifecycle immunity (zero Qdrant mutation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidation_kind",
    [
        "doc_inactive",
        "version_revoked",
        "mapping_revoked",
        "active_delete_job",
        "active_reprocess_job",
    ],
)
async def test_live_winner_lifecycle_immunity_revives_loser_with_zero_qdrant_mutation(
    monkeypatch: pytest.MonkeyPatch,
    invalidation_kind: str,
) -> None:
    """When winner doc/version/mapping is revoked/deleted/hidden by active control job,
    loser unit_A becomes immediately visible again in retrieve & grep with ZERO Qdrant change."""
    db_engine, factory = await _make_db(monkeypatch)
    c_old = "Incident escalation SLA requires paging on-call within 30 minutes."
    c_new = "Incident escalation SLA requires paging on-call within 10 minutes."

    async with factory() as session:
        src_old = await _seed_source(session, source_id="src-old", name="Legacy Ops")
        src_new = await _seed_source(session, source_id="src-new", name="Current Ops")
        await _seed_doc_version(
            session,
            source_id=src_old.id,
            doc_id="doc-old",
            version_id="ver-old",
            filename="sla_old.md",
            partition_id=P_PUBLIC,
            units=[("unit-A", "Incident SLA Old", c_old)],
        )
        await _seed_doc_version(
            session,
            source_id=src_new.id,
            doc_id="doc-new",
            version_id="ver-new",
            filename="sla_new.md",
            partition_id=P_PUBLIC,
            units=[("unit-B", "Incident SLA New", c_new)],
        )
        await _seed_kcr(
            session,
            res_id=f"res-{invalidation_kind}",
            winner_doc_id="doc-new",
            winner_ver_id="ver-new",
            winner_unit_id="unit-B",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-old",
            loser_ver_id="ver-old",
            loser_unit_id="unit-A",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await session.commit()

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    qdrant_points = {
        "ver-old": [
            _qdrant_point(version_id="ver-old", unit_id="unit-A", partition_id=P_PUBLIC, content=c_old, score=0.90)
        ],
        "ver-new": [
            _qdrant_point(version_id="ver-new", unit_id="unit-B", partition_id=P_PUBLIC, content=c_new, score=0.92)
        ],
    }
    principal = _principal({P_PUBLIC})
    sources = [src_old, src_new]
    query = "What is the incident escalation SLA for paging on-call?"

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))

            # Before invalidation: unit-A is suppressed
            before_out = await surs._retrieve_search_unit_sections(
                engine,
                sources,
                query,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.search_unit_id for s in before_out.sections] == ["unit-B"]

            # Mutate winner lifecycle in PG ONLY (zero Qdrant mutation!)
            async with factory() as session:
                if invalidation_kind == "doc_inactive":
                    doc_new = await session.get(Document, "doc-new")
                    assert doc_new is not None
                    doc_new.is_active = False
                elif invalidation_kind == "version_revoked":
                    ver_new = await session.get(DocumentVersion, "ver-new")
                    assert ver_new is not None
                    ver_new.status = "REVOKED"
                elif invalidation_kind == "mapping_revoked":
                    spm = await session.get(SourceProjectMapping, "spm-src-new")
                    assert spm is not None
                    spm.state = "REVOKED"
                    spm.revoked_at = datetime.now(UTC)
                    spm.revoked_by = "admin"
                    spm.revocation_ref = "rev-1"
                elif invalidation_kind == "active_delete_job":
                    session.add(
                        Job(
                            id="job-del-new",
                            source_id=src_new.id,
                            document_id="doc-new",
                            type=JobType.DELETE_DOCUMENT,
                            status=JobStatus.RUNNING,
                        )
                    )
                elif invalidation_kind == "active_reprocess_job":
                    session.add(
                        Job(
                            id="job-reproc-new",
                            source_id=src_new.id,
                            document_id="doc-new",
                            type=JobType.REPROCESS_DOCUMENT,
                            status=JobStatus.QUEUED,
                        )
                    )
                await session.commit()

            # Immediately revived in retrieve (using exact same untouched Qdrant client!)
            after_out = await surs._retrieve_search_unit_sections(
                engine,
                sources,
                query,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.search_unit_id for s in after_out.sections] == ["unit-A"]
            assert after_out.sections[0].is_superseded is False

            # Immediately revived in grep_search_unit_blocks too!
            async with factory() as session:
                grep_after = await surs.grep_search_unit_blocks(
                    session,
                    sources,
                    "Incident escalation SLA",
                    principal=principal,
                    limit=10,
                )
                assert [b.id for b in grep_after] == ["blk-unit-A"]
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 3: Temporal accuracy across current_effective / past_point / change_lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporal_accuracy_current_past_point_before_after_and_change_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unit_A superseded at effective_valid_to = 2025-06-01:
    - current_effective -> hides unit_A
    - past_point(2025-03-15) -> shows unit_A (not superseded!)
    - past_point(2025-07-01) -> hides unit_A
    - change_lineage -> shows unit_A with is_superseded=True, conflict_status='SUPERSEDED_BY_UNIT',
      valid_to=min_utc(version.valid_to, effective_valid_to), and byte-immutable content.
    """
    db_engine, factory = await _make_db(monkeypatch)
    c_a = "Travel reimbursement cap for domestic flights is 400 USD per trip under 2025 Q1 policy."
    c_b = "Travel reimbursement cap for domestic flights is 650 USD per trip effective June 2025."

    async with factory() as session:
        src = await _seed_source(session, source_id="src-fin", name="Finance")
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-a",
            version_id="ver-a",
            filename="travel_q1.md",
            partition_id=P_PUBLIC,
            units=[("unit-A", "Travel Cap Q1", c_a)],
            valid_from=VALID_FROM_2025,
            valid_to=VALID_TO_INF,
            authority_tier=2,
            effective_from=VALID_FROM_2025,
        )
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-b",
            version_id="ver-b",
            filename="travel_q2.md",
            partition_id=P_PUBLIC,
            units=[("unit-B", "Travel Cap Q2", c_b)],
            valid_from=VALID_FROM_2025,
            valid_to=VALID_TO_INF,
            authority_tier=1,
            effective_from=CUTOFF_JUNE,
        )
        await _seed_kcr(
            session,
            res_id="res-temporal",
            winner_doc_id="doc-b",
            winner_ver_id="ver-b",
            winner_unit_id="unit-B",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-a",
            loser_ver_id="ver-a",
            loser_unit_id="unit-A",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await session.commit()

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    qdrant_points = {
        "ver-a": [_qdrant_point(version_id="ver-a", unit_id="unit-A", partition_id=P_PUBLIC, content=c_a, score=0.90)],
        "ver-b": [_qdrant_point(version_id="ver-b", unit_id="unit-B", partition_id=P_PUBLIC, content=c_b, score=0.92)],
    }
    principal = _principal({P_PUBLIC})

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))

            # 1. current_effective -> hides unit-A
            q_curr = "What is the current travel reimbursement cap for domestic flights?"
            out_curr = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                q_curr,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(q_curr, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.search_unit_id for s in out_curr.sections] == ["unit-B"]

            # 2. past_point(2025-03-15) -> shows unit-A (not yet superseded!)
            q_mar = "What was the travel reimbursement cap on 2025-03-15?"
            out_mar = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                q_mar,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(
                    q_mar,
                    temporal_mode="past_point",
                    as_of=datetime(2025, 3, 15, 23, 59, 59, tzinfo=UTC),
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            mar_by_id = {s.search_unit_id: s for s in out_mar.sections}
            assert "unit-A" in mar_by_id
            assert mar_by_id["unit-A"].is_superseded is False
            assert mar_by_id["unit-A"].conflict_status is None

            # 3. past_point(2025-07-01) -> hides unit-A (already superseded at 2025-06-01!)
            q_jul = "What was the travel reimbursement cap on 2025-07-01?"
            out_jul = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                q_jul,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(
                    q_jul,
                    temporal_mode="past_point",
                    as_of=datetime(2025, 7, 1, 23, 59, 59, tzinfo=UTC),
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.search_unit_id for s in out_jul.sections] == ["unit-B"]

            # 4. change_lineage -> shows BOTH with unit-A stamped SUPERSEDED_BY_UNIT + min_utc valid_to
            q_lin = "How did the travel reimbursement cap change over time?"
            out_lin = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                q_lin,
                principal=principal,
                top_k=5,
                query_strategy_plan=plan_query(q_lin, temporal_mode="change_lineage"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            lin_by_id = {s.search_unit_id: s for s in out_lin.sections}
            assert set(lin_by_id.keys()) == {"unit-A", "unit-B"}
            sec_a = lin_by_id["unit-A"]
            assert sec_a.is_superseded is True
            assert sec_a.conflict_status == "SUPERSEDED_BY_UNIT"
            assert sec_a.valid_to == CUTOFF_JUNE
            assert sec_a.content == c_a
            assert _sha256(sec_a.content) == sec_a.content_hash
            assert lin_by_id["unit-B"].is_superseded is False
            assert lin_by_id["unit-B"].conflict_status is None
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 4a: Cache digest invalidation, ACL isolation, & non-mutating siblings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_knowledge_index_cache_digest_and_non_mutating_sibling_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_SCOPE_KNOWLEDGE_INDEX_CACHE produces distinct digests when KCR changes or
    between {P_public} vs {P_public, P_exec}, and sibling hydration NEVER mutates
    cached templates in k_index.hydrated_su_by_version."""
    db_engine, factory = await _make_db(monkeypatch)
    c_a = "Clause A old text about data backup schedules."
    c_a2 = "Clause A2 active sibling text about backup encryption."
    c_b = "Clause B executive superseding text about data backup schedules."

    async with factory() as session:
        src_pub = await _seed_source(session, source_id="src-c-pub", name="Pub")
        src_exec = await _seed_source(session, source_id="src-c-exec", name="Exec")
        await _seed_doc_version(
            session,
            source_id=src_pub.id,
            doc_id="doc-c-pub",
            version_id="ver-c-pub",
            filename="backup_pub.md",
            partition_id=P_PUBLIC,
            units=[
                ("unit-c-A", "Schedule", c_a),
                ("unit-c-A2", "Encryption", c_a2),
            ],
        )
        await _seed_doc_version(
            session,
            source_id=src_exec.id,
            doc_id="doc-c-exec",
            version_id="ver-c-exec",
            filename="backup_exec.md",
            partition_id=P_EXEC,
            units=[("unit-c-B", "Schedule Exec", c_b)],
        )
        await session.commit()

    try:
        async with factory() as session:
            pub_versions = await surs._load_current_ready_versions(
                session, [src_pub], _principal({P_PUBLIC})
            )
            assert set(pub_versions) == {"ver-c-pub"}

            # 1. Baseline index (no KCR yet)
            surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
            idx_baseline = await surs._get_or_build_scope_knowledge_index(
                session, pub_versions, [P_PUBLIC]
            )
            assert len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE) == 1
            key_baseline = next(iter(surs._SCOPE_KNOWLEDGE_INDEX_CACHE))

            # 2. Add ACTIVE cross-partition KCR (P_exec winner -> P_public loser)
            await _seed_kcr(
                session,
                res_id="res-cache-1",
                winner_doc_id="doc-c-exec",
                winner_ver_id="ver-c-exec",
                winner_unit_id="unit-c-B",
                winner_partition=P_EXEC,
                loser_doc_id="doc-c-pub",
                loser_ver_id="ver-c-pub",
                loser_unit_id="unit-c-A",
                loser_partition=P_PUBLIC,
                effective_valid_to=CUTOFF_JUNE,
            )
            await session.commit()

            # Same {P_public} principal sees NO visible KCR -> reuses baseline cache entry
            idx_pub_after = await surs._get_or_build_scope_knowledge_index(
                session, pub_versions, [P_PUBLIC]
            )
            assert idx_pub_after is idx_baseline
            assert len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE) == 1

            # {P_public, P_exec} principal MUST get a DISTINCT cache digest & entry!
            both_versions = await surs._load_current_ready_versions(
                session, [src_pub, src_exec], _principal({P_PUBLIC, P_EXEC})
            )
            idx_exec_active = await surs._get_or_build_scope_knowledge_index(
                session, both_versions, [P_PUBLIC, P_EXEC]
            )
            assert idx_exec_active is not idx_baseline
            assert len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE) == 2
            keys_after_active = set(surs._SCOPE_KNOWLEDGE_INDEX_CACHE)
            key_exec_active = next(k for k in keys_after_active if k != key_baseline)

            # 3. Revoke KCR -> {P_public, P_exec} MUST get a NEW distinct digest (cache invalidated!)
            kcr = await session.get(KnowledgeConflictResolution, "res-cache-1")
            assert kcr is not None
            kcr.status = ConflictResolutionStatus.REVOKED_WINNER.value
            await session.commit()

            idx_exec_revoked = await surs._get_or_build_scope_knowledge_index(
                session, both_versions, [P_PUBLIC, P_EXEC]
            )
            assert idx_exec_revoked is not idx_exec_active
            assert len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE) == 3
            assert key_exec_active in surs._SCOPE_KNOWLEDGE_INDEX_CACHE

            # Re-activate KCR for sibling non-mutation test
            kcr.status = ConflictResolutionStatus.ACTIVE.value
            await session.commit()

            sup_map = await surs.load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=sorted(both_versions),
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=datetime.now(UTC),
                organization_id=ORG_ID,
                allowed_source_ids=[src_pub.id, src_exec.id],
            )
            assert "unit-c-A" in sup_map

        # Seed section is unit-c-A2; test sibling hydration in BOTH modes and verify zero template mutation!
        seed_a2 = RetrievedSection(
            chunk_id="unit-c-A2",
            search_unit_id="unit-c-A2",
            document_id="doc-c-pub",
            document_version_id="ver-c-pub",
            heading="Encryption",
            content=c_a2,
            score=0.90,
            rank=0,
            source_id=src_pub.id,
        )
        holder: list[surs._ScopeKnowledgeIndex] = []
        sibs_hidden = await surs._hydrate_sibling_sections_for_top_versions(
            sections=[seed_a2],
            current_versions=both_versions,
            sources=[src_pub, src_exec],
            limit=5,
            index_holder=holder,
            superseded_units=sup_map,
            include_unit_superseded=False,
            allowed_partitions=[P_PUBLIC, P_EXEC],
        )
        assert sibs_hidden == []
        assert len(holder) == 1
        k_idx = holder[0]
        cached_unit_a = next(
            tpl
            for tpl, _s, _e in k_idx.hydrated_su_by_version["ver-c-pub"]
            if tpl.search_unit_id == "unit-c-A"
        )
        assert cached_unit_a.is_superseded is False
        assert cached_unit_a.conflict_status is None
        assert cached_unit_a.valid_to == VALID_TO_INF

        # In change_lineage (include_unit_superseded=True): returns stamped copy, cached template stays pristine!
        sibs_lineage = await surs._hydrate_sibling_sections_for_top_versions(
            sections=[seed_a2],
            current_versions=both_versions,
            sources=[src_pub, src_exec],
            limit=5,
            superseded_units=sup_map,
            include_unit_superseded=True,
            allowed_partitions=[P_PUBLIC, P_EXEC],
        )
        assert len(sibs_lineage) == 1
        assert sibs_lineage[0].search_unit_id == "unit-c-A"
        assert sibs_lineage[0].is_superseded is True
        assert sibs_lineage[0].conflict_status == "SUPERSEDED_BY_UNIT"
        assert sibs_lineage[0].valid_to == CUTOFF_JUNE
        # Cached template MUST STILL be untouched!
        assert cached_unit_a.is_superseded is False
        assert cached_unit_a.conflict_status is None
        assert cached_unit_a.valid_to == VALID_TO_INF
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 4b: grep_search_unit_blocks exclusive vs shared sibling & SQL LIMIT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_search_unit_blocks_excludes_exclusive_loser_keeps_shared_sibling_and_honors_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocks covered exclusively by superseded unit_A are excluded, blocks covered
    by active sibling unit_A2 (including overlapping blocks) are kept, and LIMIT is honored."""
    db_engine, factory = await _make_db(monkeypatch)
    now = datetime.now(UTC)

    async with factory() as session:
        src = await _seed_source(session, source_id="src-grep", name="Grep Docs")
        # Seed winner doc
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-gw",
            version_id="ver-gw",
            filename="winner.md",
            partition_id=P_PUBLIC,
            units=[("unit-gw", "Winner", "Updated policy unrelated text.")],
        )
        # Seed loser doc manually with 3 blocks and 2 overlapping SearchUnits:
        # unit-A covers ord 0..1 (SUPERSEDED)
        # unit-A2 covers ord 1..2 (ACTIVE SIBLING)
        session.add(
            Document(
                id="doc-gl",
                source_id=src.id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                filename="loser.md",
                storage_path="/tmp/loser.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-gl",
                document_id="doc-gl",
                version_no=1,
                file_hash=_sha256("fh-gl"),
                valid_from=VALID_FROM_2025,
                valid_to=VALID_TO_INF,
                status="READY",
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={"security_partition_id": P_PUBLIC},
            )
        )
        await session.flush()
        for ord_idx, (blk_id, txt) in enumerate(
            [
                ("blk-excl", "retention_limit is 30 days (old exclusive block)"),
                ("blk-shared", "retention_limit applies to archive logs (shared block)"),
                ("blk-active", "retention_limit audit procedures (active sibling only)"),
            ]
        ):
            session.add(
                CanonicalBlock(
                    id=blk_id,
                    document_version_id="ver-gl",
                    ordinal=ord_idx,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path=f"Sec {ord_idx}",
                    source_anchor=f"gl-b{ord_idx}",
                    normalized_text=txt,
                    content_hash=_sha256(txt),
                )
            )
        await session.flush()
        session.add(
            SearchUnit(
                id="unit-gl-A",
                document_version_id="ver-gl",
                block_from_id="blk-excl",
                block_to_id="blk-shared",
                security_partition_id=P_PUBLIC,
                content_hash=_sha256("u-a"),
                token_count=10,
                page_from=1,
                page_to=1,
                section_path="Sec 0-1",
            )
        )
        session.add(
            SearchUnit(
                id="unit-gl-A2",
                document_version_id="ver-gl",
                block_from_id="blk-shared",
                block_to_id="blk-active",
                security_partition_id=P_PUBLIC,
                content_hash=_sha256("u-a2"),
                token_count=10,
                page_from=1,
                page_to=1,
                section_path="Sec 1-2",
            )
        )
        run = IngestionRun(
            id="run-gl",
            document_version_id="ver-gl",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            idempotency_key="idem-gl",
            payload_hash="ph-gl",
            status="COMPLETED",
            attempt_count=1,
            started_at=now,
        )
        session.add(run)
        await session.flush()
        csum = "d" * 64
        session.add(
            StageRun(
                id="sr-gl",
                run_id=run.id,
                stage="INDEX_SEARCH",
                status="SUCCESS",
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": f"search_units_{PROJECT_ID}",
                    "search_unit_count": 2,
                    "pg_count": 2,
                    "qdrant_count": 2,
                    "qdrant_indexed_count": 2,
                    "manifest_checksum": csum,
                    "qdrant_checksum": csum,
                    "attempt_count": 1,
                },
            )
        )
        await _seed_kcr(
            session,
            res_id="res-grep",
            winner_doc_id="doc-gw",
            winner_ver_id="ver-gw",
            winner_unit_id="unit-gw",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-gl",
            loser_ver_id="ver-gl",
            loser_unit_id="unit-gl-A",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await session.commit()

    try:
        async with factory() as session:
            # Full match: blk-excl excluded, blk-shared + blk-active kept
            rows_all = await surs.grep_search_unit_blocks(
                session,
                [src],
                "retention_limit",
                principal=_principal({P_PUBLIC}),
                limit=10,
            )
            assert [b.id for b in rows_all] == ["blk-shared", "blk-active"]

            # Exact SQL LIMIT=1: must return [blk-shared], NOT [] (filter happens before LIMIT!)
            rows_lim1 = await surs.grep_search_unit_blocks(
                session,
                [src],
                "retention_limit",
                principal=_principal({P_PUBLIC}),
                limit=1,
            )
            assert [b.id for b in rows_lim1] == ["blk-shared"]
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 4c: Catalog backfill skips fully-superseded vid & Call #3 race -> 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_backfill_skips_fully_superseded_version_and_picks_non_superseded_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog backfill skips superseded unit #0 and picks unit #1 on partial version,
    and skips an entire version when all of its units are superseded."""
    db_engine, factory = await _make_db(monkeypatch)
    c_w = "Master compliance charter defining corporate governance."
    c_p0 = "Superseded section zero in partial document."
    c_p1 = "Active section one in partial document."
    c_s0 = "Only section in fully superseded document."

    async with factory() as session:
        src = await _seed_source(session, source_id="src-cat", name="Catalog Src")
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-w",
            version_id="ver-w",
            filename="z_winner.md",
            partition_id=P_PUBLIC,
            units=[("u-w0", "Charter", c_w)],
        )
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-all-sup",
            version_id="ver-all-sup",
            filename="a_all_superseded.md",
            partition_id=P_PUBLIC,
            units=[("u-s0", "Old Only", c_s0)],
        )
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-partial",
            version_id="ver-partial",
            filename="b_partial.md",
            partition_id=P_PUBLIC,
            units=[
                ("u-p0", "Old First", c_p0),
                ("u-p1", "Active Second", c_p1),
            ],
        )
        await _seed_kcr(
            session,
            res_id="res-cat-1",
            winner_doc_id="doc-w",
            winner_ver_id="ver-w",
            winner_unit_id="u-w0",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-all-sup",
            loser_ver_id="ver-all-sup",
            loser_unit_id="u-s0",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await _seed_kcr(
            session,
            res_id="res-cat-2",
            winner_doc_id="doc-w",
            winner_ver_id="ver-w",
            winner_unit_id="u-w0",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-partial",
            loser_ver_id="ver-partial",
            loser_unit_id="u-p0",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await session.commit()

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    all_pts = [
        _qdrant_point(version_id="ver-w", unit_id="u-w0", partition_id=P_PUBLIC, content=c_w, score=0.92),
        _qdrant_point(version_id="ver-all-sup", unit_id="u-s0", partition_id=P_PUBLIC, content=c_s0, score=0.0),
        _qdrant_point(version_id="ver-partial", unit_id="u-p0", partition_id=P_PUBLIC, content=c_p0, score=0.0),
        _qdrant_point(version_id="ver-partial", unit_id="u-p1", partition_id=P_PUBLIC, content=c_p1, score=0.0),
    ]
    by_pid = {pt["id"]: pt for pt in all_pts}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/query/batch"):
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={"result": [{"points": [all_pts[0]]} for _ in body.get("searches", [])]},
            )
        if "/points/" in request.url.path and request.method == "GET":
            pid = request.url.path.rsplit("/points/", 1)[-1]
            pt = by_pid.get(pid)
            if pt is not None:
                return httpx.Response(200, json={"result": {"id": pt["id"], "payload": pt["payload"]}})
            return httpx.Response(404, json={"status": {"error": "not found"}})
        return httpx.Response(200, json={"result": {"count": len(all_pts)}})

    catalog_plan = plan_query(
        "List all documents in the project",
        requested_top_k=2,
        route_decision={
            "is_catalog_query": True,
            "target_unit": "document",
            "answer_shape": "document_list",
            "temporal_mode": "current_effective",
        },
    )
    assert catalog_plan.retrieval_plan.is_unfiltered_catalog is True
    assert catalog_plan.retrieval_plan.per_document_limit == 1
    assert catalog_plan.retrieval_plan.output.value == "LIST"

    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid",
            transport=httpx.MockTransport(respond),
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "List all documents in the project",
                principal=_principal({P_PUBLIC}),
                top_k=2,
                query_strategy_plan=catalog_plan,
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            # Even with top_k=2 (only 1 backfill slot after u-w0) and a_all_superseded sorting
            # ahead of b_partial, catalog backfill skips ver-all-sup and picks u-p1 (skipping u-p0)!
            assert {s.search_unit_id for s in outcome.sections} == {"u-w0", "u-p1"}
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_verify_selection_scope_raises_503_when_selected_unit_superseded_between_call2_and_call3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call #3 linearizability: if a selected unit becomes superseded between Call #2
    and Call #3, _verify_selection_scope raises ServiceUnavailableError."""
    # Direct unit test of _verify_selection_scope
    orig = SimpleNamespace(
        source_id="s",
        project_id="p",
        tenant_id="t",
        partition_id="part",
        acl_mapping_version=1,
        ingestion_run_id="run",
        index_stage_run_id="idx",
        search_manifest_checksum="sum",
    )
    surs._verify_selection_scope(
        {"v"}, {"v": orig}, {"v": orig}, selected_unit_ids={"u1"}, superseded_unit_ids={}
    )
    with pytest.raises(ServiceUnavailableError, match="Search scope changed during evidence selection"):
        surs._verify_selection_scope(
            {"v"}, {"v": orig}, {"v": orig}, selected_unit_ids={"u1"}, superseded_unit_ids={"u1"}
        )

    # Full end-to-end race between Call #2 and Call #3
    db_engine, factory = await _make_db(monkeypatch)
    c_a = "Database backup retention window is 14 days for staging clusters."
    c_b = "Database backup retention window is 30 days for staging clusters."

    async with factory() as session:
        src = await _seed_source(session, source_id="src-race", name="Race Src")
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-ra",
            version_id="ver-ra",
            filename="ret_a.md",
            partition_id=P_PUBLIC,
            units=[("unit-rA", "Retention A", c_a)],
        )
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-rb",
            version_id="ver-rb",
            filename="ret_b.md",
            partition_id=P_PUBLIC,
            units=[("unit-rB", "Retention B", c_b)],
        )
        await session.commit()

    real_loader = surs._load_current_ready_versions
    call_count = 0

    async def racing_loader(
        session,
        sources,
        principal,
        *,
        version_ids=None,
        include_superseded=False,
        reference_time=None,
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            # Commit a brand-new ACTIVE supersession right before Call #3 executes!
            async with factory() as writer:
                await _seed_kcr(
                    writer,
                    res_id="res-midflight",
                    winner_doc_id="doc-rb",
                    winner_ver_id="ver-rb",
                    winner_unit_id="unit-rB",
                    winner_partition=P_PUBLIC,
                    loser_doc_id="doc-ra",
                    loser_ver_id="ver-ra",
                    loser_unit_id="unit-rA",
                    loser_partition=P_PUBLIC,
                    effective_valid_to=CUTOFF_JUNE,
                )
                await writer.commit()
        return await real_loader(
            session,
            sources,
            principal,
            version_ids=version_ids,
            include_superseded=include_superseded,
            reference_time=reference_time,
        )

    monkeypatch.setattr(surs, "_load_current_ready_versions", racing_loader)
    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    qdrant_points = {
        "ver-ra": [
            _qdrant_point(version_id="ver-ra", unit_id="unit-rA", partition_id=P_PUBLIC, content=c_a, score=0.92)
        ],
    }

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            with pytest.raises(ServiceUnavailableError, match="Search scope changed during evidence selection"):
                await surs._retrieve_search_unit_sections(
                    engine,
                    [src],
                    "What is the database backup retention window for staging clusters?",
                    principal=_principal({P_PUBLIC}),
                    top_k=5,
                    query_strategy_plan=plan_query(
                        "What is the database backup retention window for staging clusters?",
                        temporal_mode="current_effective",
                    ),
                    candidate_limit=10,
                    deadline=asyncio.get_running_loop().time() + 5.0,
                )
            assert call_count == 3
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 4d: get_search_unit_citation returns 200 with ACL-safe badge metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_search_unit_citation_returns_200_and_enforces_acl_safe_badge_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_search_unit_citation MUST keep returning 200 (dict) for superseded unit_A,
    attach superseded_by_unit_id/is_superseded/valid_to/conflict_status ONLY when
    winner is in principal.allowed_partition_ids, and hide winner details from P_public."""
    db_engine, factory = await _make_db(monkeypatch)
    c_a = "Legacy VPN onboarding steps using token version 1."
    c_b = "Updated executive VPN onboarding steps using hardware keys."
    c_c = "Conflicting VPN onboarding note pending review."

    async with factory() as session:
        src_pub = await _seed_source(session, source_id="src-cit-pub", name="Public IT")
        src_exec = await _seed_source(session, source_id="src-cit-exec", name="Exec IT")
        await _seed_doc_version(
            session,
            source_id=src_pub.id,
            doc_id="doc-cit-a",
            version_id="ver-cit-a",
            filename="vpn_v1.md",
            partition_id=P_PUBLIC,
            units=[("unit-cit-A", "VPN V1", c_a)],
            authority_tier=2,
            effective_from=VALID_FROM_2025,
        )
        await _seed_doc_version(
            session,
            source_id=src_exec.id,
            doc_id="doc-cit-b",
            version_id="ver-cit-b",
            filename="vpn_exec.md",
            partition_id=P_EXEC,
            units=[("unit-cit-B", "VPN Exec", c_b)],
            authority_tier=1,
            effective_from=CUTOFF_JUNE,
        )
        await _seed_doc_version(
            session,
            source_id=src_pub.id,
            doc_id="doc-cit-c",
            version_id="ver-cit-c",
            filename="vpn_conflict.md",
            partition_id=P_PUBLIC,
            units=[("unit-cit-C", "VPN Conflict", c_c)],
            authority_tier=2,
            effective_from=VALID_FROM_2025,
        )
        await _seed_kcr(
            session,
            res_id="res-cit-sup",
            winner_doc_id="doc-cit-b",
            winner_ver_id="ver-cit-b",
            winner_unit_id="unit-cit-B",
            winner_partition=P_EXEC,
            loser_doc_id="doc-cit-a",
            loser_ver_id="ver-cit-a",
            loser_unit_id="unit-cit-A",
            loser_partition=P_PUBLIC,
            effective_valid_to=CUTOFF_JUNE,
        )
        await _seed_kcr(
            session,
            res_id="res-cit-pending",
            winner_doc_id="doc-cit-b",
            winner_ver_id="ver-cit-b",
            winner_unit_id="unit-cit-B",
            winner_partition=P_EXEC,
            loser_doc_id="doc-cit-c",
            loser_ver_id="ver-cit-c",
            loser_unit_id="unit-cit-C",
            loser_partition=P_PUBLIC,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            effective_valid_to=None,
        )
        await session.commit()

    qdrant_points = {
        "ver-cit-a": [
            _qdrant_point(
                version_id="ver-cit-a", unit_id="unit-cit-A", partition_id=P_PUBLIC, content=c_a, score=1.0
            )
        ],
        "ver-cit-b": [
            _qdrant_point(
                version_id="ver-cit-b", unit_id="unit-cit-B", partition_id=P_EXEC, content=c_b, score=1.0
            )
        ],
        "ver-cit-c": [
            _qdrant_point(
                version_id="ver-cit-c", unit_id="unit-cit-C", partition_id=P_PUBLIC, content=c_c, score=1.0
            )
        ],
    }

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            async with factory() as session:
                # 1. {P_public, P_exec} caller gets HTTP 200 dict + full supersession badge
                cit_exec = await surs.get_search_unit_citation(
                    session,
                    source=src_pub,
                    principal=_principal({P_PUBLIC, P_EXEC}),
                    search_unit_id="unit-cit-A",
                    engine_manager=engine,
                )
                assert cit_exec is not None
                assert cit_exec["chunk_id"] == "unit-cit-A"
                assert cit_exec["content"] == c_a
                assert cit_exec["is_superseded"] is True
                assert cit_exec["conflict_status"] == "SUPERSEDED_BY_UNIT"
                assert cit_exec["superseded_by_unit_id"] == "unit-cit-B"
                assert cit_exec["valid_to"] == CUTOFF_JUNE.isoformat()
                assert cit_exec["authority_tier"] == 2
                assert cit_exec["effective_from"] == VALID_FROM_2025.isoformat()

                # 2. {P_public}-only caller STILL gets HTTP 200 dict, ZERO winner leakage!
                cit_pub = await surs.get_search_unit_citation(
                    session,
                    source=src_pub,
                    principal=_principal({P_PUBLIC}),
                    search_unit_id="unit-cit-A",
                    engine_manager=engine,
                )
                assert cit_pub is not None
                assert cit_pub["chunk_id"] == "unit-cit-A"
                assert cit_pub["content"] == c_a
                assert cit_pub["is_superseded"] is False
                assert cit_pub["conflict_status"] is None
                assert "superseded_by_unit_id" not in cit_pub
                assert "unit-cit-B" not in json.dumps(cit_pub)
                assert cit_pub["valid_to"] == VALID_TO_INF.isoformat()
                assert cit_pub["authority_tier"] == 2
                assert cit_pub["effective_from"] == VALID_FROM_2025.isoformat()

                # 3. PENDING_REVIEW badge: visible to {P_public, P_exec}, hidden from {P_public}
                cit_c_exec = await surs.get_search_unit_citation(
                    session,
                    source=src_pub,
                    principal=_principal({P_PUBLIC, P_EXEC}),
                    search_unit_id="unit-cit-C",
                    engine_manager=engine,
                )
                assert cit_c_exec is not None
                assert cit_c_exec["is_superseded"] is False
                assert cit_c_exec["conflict_status"] == "ACTIVE_CONFLICT"
                assert "superseded_by_unit_id" not in cit_c_exec

                cit_c_pub = await surs.get_search_unit_citation(
                    session,
                    source=src_pub,
                    principal=_principal({P_PUBLIC}),
                    search_unit_id="unit-cit-C",
                    engine_manager=engine,
                )
                assert cit_c_pub is not None
                assert cit_c_pub["is_superseded"] is False
                assert cit_c_pub["conflict_status"] is None
                assert "superseded_by_unit_id" not in cit_c_pub
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 5: Active unresolved conflict (PENDING_REVIEW) -> 3-site (Xung đột ...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_unresolved_conflict_returns_both_units_and_renders_xung_dot_across_all_3_sites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PENDING_REVIEW conflict returns BOTH units with conflict_status='ACTIVE_CONFLICT',
    renders identical (Xung đột ...) suffix across builtin, evidence_service, and
    agent_service verifier, and injects the conflict guardrail."""
    db_engine, factory = await _make_db(monkeypatch)
    c_1 = "Standard probation period for software engineers is 60 calendar days."
    c_2 = "Standard probation period for software engineers is 90 calendar days."

    async with factory() as session:
        src = await _seed_source(session, source_id="src-conf", name="HR Policies")
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-cf1",
            version_id="ver-cf1",
            filename="probation_alpha.md",
            partition_id=P_PUBLIC,
            units=[("unit-cf1", "Probation Alpha", c_1)],
            authority_tier=2,
            effective_from=VALID_FROM_2025,
        )
        await _seed_doc_version(
            session,
            source_id=src.id,
            doc_id="doc-cf2",
            version_id="ver-cf2",
            filename="probation_beta.md",
            partition_id=P_PUBLIC,
            units=[("unit-cf2", "Probation Beta", c_2)],
            authority_tier=2,
            effective_from=CUTOFF_JUNE,
        )
        await _seed_kcr(
            session,
            res_id="res-pending-cf",
            winner_doc_id="doc-cf2",
            winner_ver_id="ver-cf2",
            winner_unit_id="unit-cf2",
            winner_partition=P_PUBLIC,
            loser_doc_id="doc-cf1",
            loser_ver_id="ver-cf1",
            loser_unit_id="unit-cf1",
            loser_partition=P_PUBLIC,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            effective_valid_to=None,
        )
        await session.commit()

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    qdrant_points = {
        "ver-cf1": [
            _qdrant_point(version_id="ver-cf1", unit_id="unit-cf1", partition_id=P_PUBLIC, content=c_1, score=0.91)
        ],
        "ver-cf2": [
            _qdrant_point(version_id="ver-cf2", unit_id="unit-cf2", partition_id=P_PUBLIC, content=c_2, score=0.92)
        ],
    }
    query = "What is the standard probation period for software engineers?"

    try:
        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                query,
                principal=_principal({P_PUBLIC}),
                top_k=5,
                query_strategy_plan=plan_query(query, temporal_mode="current_effective"),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )

        assert {s.search_unit_id for s in outcome.sections} == {"unit-cf1", "unit-cf2"}
        for sec in outcome.sections:
            assert sec.is_superseded is False
            assert sec.conflict_status == "ACTIVE_CONFLICT"
            suffix = format_evidence_metadata_suffix(sec)
            assert suffix.startswith("(Xung đột")
            assert "[" not in suffix and "]" not in suffix
            assert len(suffix) <= 50

        expected_suffixes = [format_evidence_metadata_suffix(s) for s in outcome.sections]

        # Site 1: tools/builtin.py (_format_sections + SearchContextTool.invoke guardrail)
        formatted_tool = _format_sections(outcome.sections, 0)
        for sfx in expected_suffixes:
            assert sfx in formatted_tool

        monkeypatch.setattr(
            surs,
            "retrieve_search_unit_sections",
            AsyncMock(return_value=SearchOutcome(query=query, sections=outcome.sections, stats=outcome.stats)),
        )
        ctx = ToolContext(
            engine_manager=SimpleNamespace(),
            sources=[src],
            principal=_principal({P_PUBLIC}),
        )
        tool_res = await SearchContextTool().invoke({"query": query}, ctx)
        assert "(Xung đột" in tool_res.content
        assert "nếu còn (Xung đột) ngang bậc chưa phân giải" in tool_res.content

        # Site 2: evidence_service.py (_search_prompt_messages)
        ev_msgs = evidence_service._search_prompt_messages(query, outcome.sections)
        assert "equal-authority or (Xung đột) excerpts" in ev_msgs[0]["content"]
        for sfx in expected_suffixes:
            assert sfx in ev_msgs[1]["content"]

        # Site 3: agent_service.py (_verify_grounded_entailment)
        captured_prompts: list[str] = []

        class FakeVerifierLLM:
            configured = True

            async def complete(self, messages, **_kwargs):
                captured_prompts.append(messages[-1]["content"])
                return '{"answerability": "answered", "supported": true, "reason": "ok"}'

        cits = []
        meta_by_n = {}
        content_by_n = {}
        for idx, sec in enumerate(outcome.sections, start=1):
            cits.append(
                {
                    "n": idx,
                    "claim_level": "claim",
                    "document_name": sec.document_name,
                    "heading": sec.heading,
                }
            )
            meta_by_n[idx] = agent_service._extract_verifier_metadata(sec)
            content_by_n[idx] = sec.content

        verdict, _reason = await agent_service._verify_grounded_entailment(
            FakeVerifierLLM(),
            query=query,
            answer="Policy Alpha states 60 days [1] while Policy Beta states 90 days [2].",
            internal_citations=cits,
            session_citations=[],
            verifier_content_by_n=content_by_n,
            verifier_meta_by_n=meta_by_n,
        )
        assert verdict == "answered"
        assert len(captured_prompts) == 1
        for sfx in expected_suffixes:
            assert sfx in captured_prompts[0]
    finally:
        await db_engine.dispose()


# ---------------------------------------------------------------------------
# Group 6: Multi-hop expansion respects unit supersession overlay without mutating cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_hop_expansion_filters_superseded_unit_in_past_point_after_and_stamps_in_lineage() -> None:
    """_expand_multi_hop_sections drops unit_A when include_unit_superseded=False
    (even when allows_superseded=True, as in past_point(after)), and stamps unit_A
    when include_unit_superseded=True without mutating cached templates."""
    src = SimpleNamespace(id="src-mh", name="MH", sag_source_config_id="cfg-mh")
    ver_a = surs._ReadyVersion(
        document_version_id="ver-a",
        document_id="doc-a",
        source_id=src.id,
        project_id=PROJECT_ID,
        tenant_id=TENANT_ID,
        partition_id=P_PUBLIC,
        source=src,
        valid_to=VALID_TO_INF,
        is_superseded=False,
    )
    ver_b = surs._ReadyVersion(
        document_version_id="ver-b",
        document_id="doc-b",
        source_id=src.id,
        project_id=PROJECT_ID,
        tenant_id=TENANT_ID,
        partition_id=P_PUBLIC,
        source=src,
        valid_to=VALID_TO_INF,
        is_superseded=False,
    )
    sec_a = RetrievedSection(
        chunk_id="u-a",
        search_unit_id="u-a",
        document_id="doc-a",
        document_version_id="ver-a",
        document_name="a.md",
        heading="Old Clause",
        content="Old clause value 100.",
        score=0.0,
        rank=0,
        source_id=src.id,
        source_config_id=src.sag_source_config_id,
        valid_to=VALID_TO_INF,
        is_superseded=False,
        canonical_evidence_verified=True,
        page_from=1,
        page_to=1,
        anchor="a-0",
    )
    sec_b = RetrievedSection(
        chunk_id="u-b",
        search_unit_id="u-b",
        document_id="doc-b",
        document_version_id="ver-b",
        document_name="b.md",
        heading="New Clause",
        content="New clause value 200.",
        score=0.92,
        rank=0,
        source_id=src.id,
        source_config_id=src.sag_source_config_id,
        valid_to=VALID_TO_INF,
        is_superseded=False,
        canonical_evidence_verified=True,
        page_from=1,
        page_to=1,
        anchor="b-0",
    )
    k_index = surs._ScopeKnowledgeIndex(
        ku_by_version={},
        ku_by_id={},
        edges_by_ku={},
        ent_exact_versions={},
        hydrated_su_by_version={"ver-a": [(sec_a, 0, 0)], "ver-b": [(sec_b, 0, 0)]},
        lineage_by_version_ordinal={("ver-b", 0): [("ver-a", 0, 0, 0.88, "SUPERSEDES")]},
    )
    sup_info = surs.SupersededUnitInfo(
        loser_unit_id="u-a",
        winner_unit_id="u-b",
        winner_version_id="ver-b",
        winner_document_id="doc-b",
        effective_valid_to=CUTOFF_JUNE,
        resolution_id="res-mh",
    )

    # past_point(after): allows_superseded=True, include_unit_superseded=False -> MUST drop u-a!
    exp_past_after = await surs._expand_multi_hop_sections(
        seed_sections=[sec_b],
        unit_ordinals={"u-b": (0, 0)},
        current_versions={"ver-a": ver_a, "ver-b": ver_b},
        existing_unit_ids={"u-b"},
        sources=[src],
        limit=8,
        lineage_only=True,
        preloaded_k_index=k_index,
        allows_superseded=True,
        include_unit_superseded=False,
        superseded_units={"u-a": sup_info},
    )
    assert exp_past_after == []

    # change_lineage: allows_superseded=True, include_unit_superseded=True -> returns stamped copy, cache untouched!
    exp_lineage = await surs._expand_multi_hop_sections(
        seed_sections=[sec_b],
        unit_ordinals={"u-b": (0, 0)},
        current_versions={"ver-a": ver_a, "ver-b": ver_b},
        existing_unit_ids={"u-b"},
        sources=[src],
        limit=8,
        lineage_only=True,
        preloaded_k_index=k_index,
        allows_superseded=True,
        include_unit_superseded=True,
        superseded_units={"u-a": sup_info},
    )
    assert len(exp_lineage) == 1
    assert exp_lineage[0].search_unit_id == "u-a"
    assert exp_lineage[0].is_superseded is True
    assert exp_lineage[0].conflict_status == "SUPERSEDED_BY_UNIT"
    assert exp_lineage[0].valid_to == CUTOFF_JUNE
    # Cached template MUST remain unmutated!
    assert sec_a.is_superseded is False
    assert sec_a.conflict_status is None
    assert sec_a.valid_to == VALID_TO_INF


# ---------------------------------------------------------------------------
# Group 7: Triad Round 2 Hardening Regression (Fix 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_knowledge_index_cache_key_isolated_from_other_partition_kg_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 10: Inserting P_exec-only KnowledgeGraphEdge rows MUST NOT change or bust
    {P_public}'s _SCOPE_KNOWLEDGE_INDEX_CACHE key (zero cross-partition side-channel)."""
    from sag_api.db.models import KnowledgeGraphEdge

    db_engine, factory = await _make_db(monkeypatch)
    surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
    try:
        async with factory() as session:
            src_pub = await _seed_source(session, source_id="src-ck-pub", name="Public")
            src_exec = await _seed_source(session, source_id="src-ck-exec", name="Exec")
            await _seed_doc_version(
                session,
                source_id=src_pub.id,
                doc_id="doc-ck-pub",
                version_id="ver-ck-pub",
                filename="pub.md",
                partition_id=P_PUBLIC,
                units=[("unit-ck-pub-1", "Public Sec", "Public SLA response is 24 hours.")],
            )
            await _seed_doc_version(
                session,
                source_id=src_exec.id,
                doc_id="doc-ck-exec-1",
                version_id="ver-ck-exec-1",
                filename="exec1.md",
                partition_id=P_EXEC,
                units=[("unit-ck-exec-1", "Exec Sec 1", "Executive policy A.")],
            )
            await _seed_doc_version(
                session,
                source_id=src_exec.id,
                doc_id="doc-ck-exec-2",
                version_id="ver-ck-exec-2",
                filename="exec2.md",
                partition_id=P_EXEC,
                units=[("unit-ck-exec-2", "Exec Sec 2", "Executive policy B.")],
            )
            await session.commit()

            pub_versions = {
                "ver-ck-pub": surs._ReadyVersion(
                    document_version_id="ver-ck-pub",
                    document_id="doc-ck-pub",
                    source_id=src_pub.id,
                    project_id=PROJECT_ID,
                    tenant_id=TENANT_ID,
                    partition_id=P_PUBLIC,
                    source=src_pub,
                    valid_to=VALID_TO_INF,
                    is_superseded=False,
                )
            }

            k1 = await surs._get_or_build_scope_knowledge_index(
                session,
                pub_versions,
                allowed_partitions={P_PUBLIC},
                reference_time=datetime(2025, 8, 1, tzinfo=UTC),
                organization_id=ORG_ID,
            )
            keys_before = len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE)
            assert keys_before == 1

            # Insert P_exec-only CONTRADICTS and SUPERSEDES edges + a cross-partition edge
            session.add_all(
                [
                    KnowledgeGraphEdge(
                        id="edge-ck-exec-con",
                        project_id=PROJECT_ID,
                        source_unit_id="unit-ck-exec-1",
                        target_unit_id="unit-ck-exec-2",
                        edge_type="CONTRADICTS",
                        metadata_json={"is_candidate": True},
                    ),
                    KnowledgeGraphEdge(
                        id="edge-ck-exec-sup",
                        project_id=PROJECT_ID,
                        source_unit_id="unit-ck-exec-2",
                        target_unit_id="unit-ck-exec-1",
                        edge_type="SUPERSEDES",
                        metadata_json={"is_candidate": False},
                    ),
                    KnowledgeGraphEdge(
                        id="edge-ck-cross-con",
                        project_id=PROJECT_ID,
                        source_unit_id="unit-ck-exec-1",
                        target_unit_id="unit-ck-pub-1",
                        edge_type="CONTRADICTS",
                        metadata_json={"is_candidate": True},
                    ),
                ]
            )
            await session.commit()

            k2 = await surs._get_or_build_scope_knowledge_index(
                session,
                pub_versions,
                allowed_partitions={P_PUBLIC},
                reference_time=datetime(2025, 8, 1, tzinfo=UTC),
                organization_id=ORG_ID,
            )
            keys_after = len(surs._SCOPE_KNOWLEDGE_INDEX_CACHE)
            assert k1 is k2
            assert keys_after == keys_before == 1
    finally:
        surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
        await db_engine.dispose()
