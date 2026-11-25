"""TDD regression tests for Batch P6-3 Packet 2:
- GLOBAL_SUMMARY -> GLOBAL_TOPIC typo fix (full global escape budget preserved)
- S4-01 boilerplate & weak 1-word OOD lexical floor bypass guard (rerank + latency fallback)
- Pure-semantic high-confidence hit survival in group_sections_by_document
- Removal of hidden 0.94 wall in select_diverse_sections (respects search_doc_admission_ratio)
- Leiden hierarchical ceiling + agglomerative merge eliminating N > 256 collapse
- RoutingPolicy threshold alignment so entropy-based broad routing is reachable
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source, SourceProjectMapping
from sag_api.db.models.routing_rag import CanonicalBlock, IngestionRun, SearchUnit, StageRun
from sag_api.enums import DocumentStatus
from sag_api.sag import RetrievedSection
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services.query_routing_service import (
    GroupRoutingSnapshot,
    NodeProfile,
    RouteDecision,
    RoutingPolicy,
    route_snapshot,
    scope_fingerprint,
)
from sag_api.services.query_strategy_planner import RetrievalMode, plan_query
from sag_api.services.retrieval_plan import group_sections_by_document
from sag_api.services.retrieval_service import rerank_sections, select_diverse_sections
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    TreeBuildConfig,
    _partition,
    build_routing_snapshot,
)
from sag_api.services.search_index_service import generate_search_unit_point_id


def _make_principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p63",
        organization_id="tenant-p63",
        issuer="https://issuer.test",
        key_id="k1",
        token_id="tok-p63",
        issued_at=1000,
        expires_at=9999999999,
        tenant_id="tenant-p63",
        allowed_project_ids=frozenset({"project-p63"}),
        allowed_partition_ids=frozenset({"partition-p63"}),
    )


async def _setup_canonical_db(monkeypatch, unit_specs: list[tuple[str, str, str]]):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(surs, "SessionLocal", factory)
    now = datetime.now(UTC)
    async with factory() as session:
        src = Source(id="source-p63", name="Source P63", sag_source_config_id="cfg-p63")
        session.add(src)
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="map-p63",
                source_id="source-p63",
                organization_id="tenant-p63",
                project_id="project-p63",
                state="CONFIRMED",
                mapping_version=1,
                confirmed_at=now,
                confirmed_by="u1",
                approval_ref="ref-p63",
            )
        )
        session.add(
            Document(
                id="doc-p63",
                source_id="source-p63",
                tenant_id="tenant-p63",
                project_id="project-p63",
                filename="guide.md",
                storage_path="/tmp/guide.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="version-p63",
                document_id="doc-p63",
                version_no=1,
                file_hash="fh-p63",
                valid_from=now,
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={"security_partition_id": "partition-p63"},
            )
        )
        await session.flush()
        for ordinal, (unit_id, heading, content) in enumerate(unit_specs):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            blk_id = f"blk-{unit_id}"
            session.add(
                CanonicalBlock(
                    id=blk_id,
                    document_version_id="version-p63",
                    ordinal=ordinal,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path=heading,
                    source_anchor=f"p63-b{ordinal}",
                    normalized_text=content,
                    content_hash=digest,
                )
            )
            await session.flush()
            session.add(
                SearchUnit(
                    id=unit_id,
                    document_version_id="version-p63",
                    block_from_id=blk_id,
                    block_to_id=blk_id,
                    security_partition_id="partition-p63",
                    content_hash=digest,
                    token_count=max(1, len(content.split())),
                    page_from=1,
                    page_to=1,
                    section_path=heading,
                )
            )
        run = IngestionRun(
            id="run-p63",
            document_version_id="version-p63",
            tenant_id="tenant-p63",
            project_id="project-p63",
            idempotency_key="idem-p63",
            payload_hash="ph-p63",
            status="COMPLETED",
            started_at=now,
        )
        session.add(run)
        await session.flush()
        checksum = "a" * 64
        session.add(
            StageRun(
                id="sr-p63",
                run_id="run-p63",
                stage="INDEX_SEARCH",
                status="SUCCESS",
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": "search_units_project-p63",
                    "search_unit_count": len(unit_specs),
                    "pg_count": len(unit_specs),
                    "qdrant_count": len(unit_specs),
                    "qdrant_indexed_count": len(unit_specs),
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        await session.commit()
    return engine, src


def _make_qdrant_point(unit_id: str, content: str, score: float) -> dict:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": generate_search_unit_point_id("search_units_project-p63", unit_id),
        "score": score,
        "payload": {
            "search_unit_id": unit_id,
            "document_version_id": "version-p63",
            "project_id": "project-p63",
            "tenant_id": "tenant-p63",
            "security_partition_id": "partition-p63",
            "content_hash": digest,
            "content": content,
        },
    }


@pytest.mark.asyncio
async def test_global_topic_preserves_full_escape_limit_not_halved_by_typo(monkeypatch):
    content = "Tổng quan kiến trúc hệ thống quản trị tri thức doanh nghiệp."
    db_engine, source = await _setup_canonical_db(
        monkeypatch, [("unit-gt", "Overview", content)]
    )
    captured_escape_limits: list[int] = []

    async def fake_routed_batch(
        group,
        *,
        query,
        limit,
        store,
        query_vector,
        route,
        escape_limit,
        sub_sparse_queries=(),
    ):
        del group, query, limit, store, query_vector, route, sub_sparse_queries
        captured_escape_limits.append(escape_limit)
        return [], [], [], []

    fp = scope_fingerprint(
        {
            "project_id": "project-p63",
            "source_ids": ("source-p63",),
            "document_version_ids": ("version-p63",),
            "tenant_id": "tenant-p63",
            "partition_id": "partition-p63",
        }
    )
    decisive_route = RouteDecision(
        snapshot_id="snap-gt",
        acl_scope_fingerprint=fp,
        project_id="project-p63",
        tree_version="tree-gt",
        routing_slot="SLOT_A",
        search_epoch=1,
        selected_nodes=("leaf-1",),
        membership_node_ids=("leaf-1",),
        entropy=0.25,
        margin=0.65,
        broad_route=False,
        reason_code="ROUTE_DESCENDED",
    )
    scope_key = ("project-p63", "source-p63", "tenant-p63", "partition-p63")
    monkeypatch.setattr(
        surs,
        "capture_routing_decisions",
        AsyncMock(return_value={scope_key: decisive_route}),
    )
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr(surs, "_query_routed_group_batch", fake_routed_batch)
    monkeypatch.setattr(surs, "_record_routed_query_demand", AsyncMock())

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": 1}})
        return httpx.Response(200, json={"result": []})

    strategy = plan_query(
        "Tổng quan toàn bộ chủ đề trong dự án",
        retrieval_mode_hint="global_topic",
        requested_top_k=5,
    )
    assert strategy.primary_strategy == RetrievalMode.GLOBAL_TOPIC
    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            await surs._retrieve_search_unit_sections(
                engine,
                [source],
                "Tổng quan toàn bộ chủ đề trong dự án",
                principal=_make_principal(),
                top_k=5,
                query_strategy_plan=strategy,
                candidate_limit=24,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
        assert captured_escape_limits == [24]
    finally:
        await db_engine.dispose()


def test_s4_01_rerank_blocks_boilerplate_and_weak_1word_ood_lexical_bypass():
    # 1. Boilerplate MUST be filtered even when present in lexical results
    bp_sec = RetrievedSection(
        chunk_id="c-bp",
        document_id="doc-bp",
        heading="Footer Disclaimer",
        content="All Rights Reserved. Confidential Disclaimer. System notice.",
        score=0.95,
        raw_dense_score=0.80,
        rank=0,
        source_id="src-1",
    )
    # 2. OOD chunk with low raw_dense_score=0.12 matching 1 generic word ("system") in BM25
    ood_sec = RetrievedSection(
        chunk_id="c-ood",
        document_id="doc-ood",
        heading="Kubernetes Notes",
        content="Internal cluster deployment uses Kubernetes monitoring system dashboards.",
        score=0.85,
        raw_dense_score=0.12,
        rank=1,
        source_id="src-1",
    )
    ood_res = rerank_sections(
        "chocolate cake oven baking temperature system recipe",
        [bp_sec, ood_sec],
        lexical=[bp_sec, ood_sec],
        limit=5,
    )
    assert ood_res.sections == []

    # 3. Exact identifier anchor (ERR-42) STILL survives even with low raw_dense_score
    exact_sec = RetrievedSection(
        chunk_id="c-err",
        document_id="doc-err",
        heading="Error Reference",
        content="Error code ERR-42 indicates an invalid token signature.",
        score=0.90,
        raw_dense_score=0.10,
        rank=0,
        source_id="src-1",
    )
    exact_res = rerank_sections(
        "ERR-42 invalid token",
        [exact_sec],
        lexical=[exact_sec],
        limit=5,
    )
    assert [s.chunk_id for s in exact_res.sections] == ["c-err"]


@pytest.mark.asyncio
async def test_s4_01_latency_fallback_blocks_lexical_boilerplate_and_weak_1word_ood(monkeypatch):
    specs = [
        ("unit-bp", "Footer", "All Rights Reserved. Confidential Disclaimer. System."),
        ("unit-ood", "Infra", "Cluster deployment uses Kubernetes monitoring system dashboards."),
        ("unit-exact", "Error Guide", "Error code ERR-901 requires manual rotation reset."),
        ("unit-good", "Baking Guide", "Chocolate cake oven baking temperature recipe is 180C."),
    ]
    db_engine, source = await _setup_canonical_db(monkeypatch, specs)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": 4}})
        if request.url.path.endswith("/points/query/batch"):
            dense_pts = [
                _make_qdrant_point("unit-bp", specs[0][2], 0.85),
                _make_qdrant_point("unit-ood", specs[1][2], 0.12),
                _make_qdrant_point("unit-exact", specs[2][2], 0.12),
                _make_qdrant_point("unit-good", specs[3][2], 0.78),
            ]
            # Put boilerplate & OOD into lexical too to verify in_lexical cannot bypass filters
            sparse_pts = [
                _make_qdrant_point("unit-bp", specs[0][2], 5.0),
                _make_qdrant_point("unit-ood", specs[1][2], 4.5),
                _make_qdrant_point("unit-exact", specs[2][2], 4.0),
            ]
            return httpx.Response(
                200,
                json={"result": [{"points": dense_pts}, {"points": sparse_pts}]},
            )
        raise AssertionError(f"Unexpected Qdrant call: {request.method} {request.url.path}")

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr(settings, "search_rerank_min_remaining_seconds", 999.0)
    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [source],
                "chocolate cake oven baking temperature system ERR-901",
                principal=_make_principal(),
                top_k=5,
                candidate_limit=8,
                deadline=asyncio.get_running_loop().time() + 10.0,
            )
        kept = [s.chunk_id for s in outcome.sections]
        assert "unit-good" in kept
        assert "unit-exact" in kept
        assert "unit-bp" not in kept
        assert "unit-ood" not in kept
    finally:
        await db_engine.dispose()


def test_group_sections_by_document_preserves_high_confidence_pure_semantic_hit():
    s_lex = RetrievedSection(
        chunk_id="c-lex",
        search_unit_id="c-lex",
        document_id="d-lex",
        document_version_id="v1",
        document_name="lexical_boosted.pdf",
        heading="H",
        content="lexical match",
        score=0.85,
        raw_dense_score=0.12,
        rank=0,
    )
    s_sem = RetrievedSection(
        chunk_id="c-sem",
        search_unit_id="c-sem",
        document_id="d-sem",
        document_version_id="v2",
        document_name="exact_semantic.pdf",
        heading="H",
        content="paraphrased semantic answer",
        score=0.40,
        raw_dense_score=0.85,
        rank=1,
    )
    s_pure = RetrievedSection(
        chunk_id="c-pure",
        search_unit_id="c-pure",
        document_id="d-pure",
        document_version_id="v3",
        document_name="pure_semantic.pdf",
        heading="H",
        content="strong dense match with zero lexical overlap",
        score=0.31,
        raw_dense_score=0.82,
        rank=2,
    )
    s_noise = RetrievedSection(
        chunk_id="c-noise",
        search_unit_id="c-noise",
        document_id="d-noise",
        document_version_id="v4",
        document_name="noise.pdf",
        heading="H",
        content="weak irrelevant noise",
        score=0.18,
        raw_dense_score=0.15,
        rank=3,
    )
    grouped = group_sections_by_document([s_lex, s_sem, s_pure, s_noise], limit=5)
    kept_docs = [s.document_id for s in grouped]
    assert "d-lex" in kept_docs
    assert "d-sem" in kept_docs
    assert "d-pure" in kept_docs
    assert "d-noise" not in kept_docs


def test_select_diverse_sections_respects_config_admission_ratio_no_094_wall():
    # d0 has top chunk 0.96 AND weak 2nd chunk 0.45; d1..d4 are distinct high-scoring docs (0.88..0.85)
    candidates = [
        RetrievedSection(
            chunk_id="d0-c1",
            document_id="d0",
            document_name="doc0.pdf",
            heading="Primary Overview",
            content="Unique alpha architecture details for enterprise cluster zero.",
            score=0.96,
            raw_dense_score=0.96,
            rank=0,
        ),
        RetrievedSection(
            chunk_id="d0-c2",
            document_id="d0",
            document_name="doc0.pdf",
            heading="Minor Appendix",
            content="Secondary footnote addendum with tangential remarks in appendix zero.",
            score=0.45,
            raw_dense_score=0.45,
            rank=5,
        ),
    ] + [
        RetrievedSection(
            chunk_id=f"d{i}-c1",
            document_id=f"d{i}",
            document_name=f"doc{i}.pdf",
            heading=f"Topic {i}",
            content=f"Distinct unique domain specification section {i} with independent facts delta {i}.",
            score=sc,
            raw_dense_score=sc,
            rank=i,
        )
        for i, sc in enumerate([0.88, 0.87, 0.86, 0.85], start=1)
    ] + [
        RetrievedSection(
            chunk_id="noise-1",
            document_id="d-noise",
            document_name="noise.pdf",
            heading="Noise",
            content="Unrelated cafeteria menu item for Friday lunch.",
            score=0.55,
            raw_dense_score=0.55,
            rank=6,
        )
    ]

    selected, _ = select_diverse_sections("domain specification architecture", candidates, limit=5)
    selected_ids = [s.chunk_id for s in selected]
    assert "d0-c2" not in selected_ids, f"Weak d0-c2 displaced distinct docs: {selected_ids}"
    assert "noise-1" not in selected_ids
    assert [s.document_id for s in selected] == ["d0", "d1", "d2", "d3", "d4"]

    # 7 distinct high-scoring docs (> limit+1) must still fill limit=5 without hidden 0.94/0.88 truncation
    seven_docs = [
        RetrievedSection(
            chunk_id=f"s{i}",
            document_id=f"sd{i}",
            document_name=f"spec{i}.pdf",
            heading=f"Spec {i}",
            content=f"Independent specification clause {i} covering module omega {i} parameters.",
            score=sc,
            raw_dense_score=sc,
            rank=i,
        )
        for i, sc in enumerate([0.96, 0.85, 0.84, 0.83, 0.82, 0.81, 0.80])
    ]
    sel7, _ = select_diverse_sections("specification module parameters", seven_docs, limit=5)
    assert len(sel7) == 5
    assert [s.document_id for s in sel7] == ["sd0", "sd1", "sd2", "sd3", "sd4"]


def test_routing_tree_leiden_preserves_cluster_purity_above_256_nodes():
    # N = 260 (> 32 * 8 = 256): 4 disjoint strongly-connected clusters of 65 SHA256-hashed nodes
    unit_ids_by_cluster: dict[int, list[str]] = {c: [] for c in range(4)}
    cluster_of: dict[str, int] = {}
    for c in range(4):
        for i in range(65):
            uid = hashlib.sha256(f"cluster-{c}-node-{i}".encode()).hexdigest()[:16]
            unit_ids_by_cluster[c].append(uid)
            cluster_of[uid] = c

    all_uids = tuple(sorted(cluster_of.keys()))
    edges: dict[tuple[str, str], float] = {}
    edge_inputs: list[KnowledgeEdgeInput] = []
    for members in unit_ids_by_cluster.values():
        for idx, u in enumerate(members):
            for step in range(1, 9):
                v = members[(idx + step) % len(members)]
                a, b = (u, v) if u < v else (v, u)
                if (a, b) not in edges:
                    edges[(a, b)] = 0.99
                    edge_inputs.append(KnowledgeEdgeInput(a, b, 0.99))

    cfg = TreeBuildConfig()
    parts = _partition(all_uids, edges, cfg)
    assert 2 <= len(parts) <= cfg.max_children
    assert sorted(uid for p in parts for uid in p) == list(all_uids)
    purities = []
    for p in parts:
        assert len(p) >= cfg.min_cluster_size
        counts: dict[int, int] = {}
        for u in p:
            counts[cluster_of[u]] = counts.get(cluster_of[u], 0) + 1
        purities.append(max(counts.values()) / len(p))
    assert min(purities) >= 0.90, f"Leiden purity collapsed: purities={purities}"

    # Full hierarchical build_routing_snapshot must satisfy all quality gates & keep cluster purity
    units = [
        KnowledgeUnitInput(
            unit_id=uid,
            tenant_id="t-leiden",
            project_id="p-leiden",
            security_partition_id="public",
            dense=tuple(1.0 if dim == cluster_of[uid] else 0.0 for dim in range(4)),
            sparse=((f"topic{cluster_of[uid]}", 1.0),),
        )
        for uid in all_uids
    ]
    snap = build_routing_snapshot(units, edge_inputs, config=cfg)
    assert snap.publishable is True
    assert snap.quality_gates["tree_integrity"] is True
    assert snap.quality_gates["cluster_constraints"] is True
    # Every depth-1 child and every leaf must preserve cluster purity (no SHA256 hex scrambling)
    assert snap.roots and snap.roots[0].children
    for child in snap.roots[0].children:
        child_counts: dict[int, int] = {}
        for u in child.unit_ids:
            child_counts[cluster_of[u]] = child_counts.get(cluster_of[u], 0) + 1
        assert max(child_counts.values()) / len(child.unit_ids) >= 0.90
    assert float(snap.metrics["cohesion"] or 0.0) >= 0.50

    # N = 320 across 10 disjoint clusters (> max_children=8): agglomerative merge must succeed cleanly
    ten_edges: dict[tuple[str, str], float] = {}
    ten_cluster_of: dict[str, int] = {}
    for c in range(10):
        members = [hashlib.sha256(f"ten-{c}-{i}".encode()).hexdigest()[:16] for i in range(32)]
        for u in members:
            ten_cluster_of[u] = c
        for idx, u in enumerate(members):
            for step in range(1, 5):
                v = members[(idx + step) % len(members)]
                a, b = (u, v) if u < v else (v, u)
                ten_edges[(a, b)] = 0.95
    ten_uids = tuple(sorted(ten_cluster_of.keys()))
    ten_parts = _partition(ten_uids, ten_edges, cfg)
    assert len(ten_parts) == cfg.max_children
    assert sorted(uid for p in ten_parts for uid in p) == list(ten_uids)
    # At least 6 of the 8 buckets remain pure single clusters (only 2 merges needed from 10 -> 8)
    pure_buckets = sum(
        len({ten_cluster_of[u] for u in p}) == 1 for p in ten_parts
    )
    assert pure_buckets >= 6


def test_routing_policy_entropy_broadening_reachable_and_margin_ordered():
    rp = RoutingPolicy()
    assert rp.broad_margin_threshold > rp.decisive_margin, (
        f"Entropy broadening dead: broad_margin_threshold={rp.broad_margin_threshold} "
        f"<= decisive_margin={rp.decisive_margin}"
    )

    fp = scope_fingerprint(
        {
            "project_id": "proj-r",
            "source_ids": ["src-1"],
            "document_version_ids": ["ver-1"],
            "tenant_id": "tenant-1",
            "partition_id": "public",
        }
    )
    common = dict(
        project_id="proj-r",
        source_ids=("src-1",),
        document_version_ids=("ver-1",),
        tenant_id="tenant-1",
        partition_id="public",
        tree_version="tree-r",
        scope_fingerprint=fp,
    )

    def _snap(child_scores: list[tuple[str, float]]) -> GroupRoutingSnapshot:
        return GroupRoutingSnapshot(
            snapshot_id="snap-r",
            captured_at=datetime.now(UTC),
            project_id="proj-r",
            source_ids=("src-1",),
            document_version_ids=("ver-1",),
            tenant_id="tenant-1",
            partition_id="public",
            scope_fingerprint=fp,
            tree_version="tree-r",
            tree_config_version="v1",
            routing_slot="SLOT_A",
            search_epoch=1,
            manifest_status="ACTIVE",
            manifest_checksum="c" * 64,
            manifest_verified=True,
            profiles=(
                NodeProfile(
                    node_id="root",
                    parent_id=None,
                    is_leaf=False,
                    accessible_unit_count=10,
                    signal_scores={"dense": 0.9},
                    **common,
                ),
                *[
                    NodeProfile(
                        node_id=nid,
                        parent_id="root",
                        is_leaf=True,
                        accessible_unit_count=2,
                        signal_scores={"dense": sc},
                        **common,
                    )
                    for nid, sc in child_scores
                ],
            ),
        )

    # High entropy (~0.98 >= 0.72) with intermediate margin (0.15 in [decisive_margin, broad_margin_threshold))
    # -> MUST trigger entropy broadening
    ambiguous_snap = _snap([("n1", 0.45), ("n2", 0.30), ("n3", 0.28), ("n4", 0.27)])
    dec_broad = route_snapshot(ambiguous_snap)
    assert dec_broad.broad_route is True
    assert dec_broad.reason_code == "ROUTE_UNCERTAIN_BROADENED"
    assert len(dec_broad.selected_nodes) == rp.beam_width

    # Low entropy (~0.59 < 0.72) with SAME intermediate margin (0.15 >= decisive_margin)
    # -> MUST descend decisively without broadening
    focused_snap = _snap([("n1", 0.18), ("n2", 0.03)])
    dec_descend = route_snapshot(focused_snap)
    assert dec_descend.broad_route is False
    assert dec_descend.reason_code == "ROUTE_DESCENDED"
    assert dec_descend.selected_nodes == ("n1",)
