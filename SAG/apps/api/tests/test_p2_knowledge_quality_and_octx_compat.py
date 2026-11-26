"""Phase 2 Wave 2.2 (P2-E): Cross-Subsystem Compatibility & Full E2E Regression Suite.

Covers all 4 cross-subsystem domains against real SQLite `:memory:` (`PRAGMA foreign_keys=ON`),
real SQLAlchemy ORM, and real production services (mocking ONLY external HTTP Qdrant/LLM
transports):

- Domain 1: KnowledgeQuality integration (`build_topic_inputs` / `_contradiction_signals` /
  `score_topic` / `assess_knowledge_tree`), `CONTRADICTS.metadata_json["is_candidate"]`
  sync across `PENDING_REVIEW` -> `WINNER_B` / `KEEP_BOTH_SCOPED` -> `reopen_conflict`,
  and `MAX_CONTRADICTION_CANDIDATES = 5000` cap preservation.
- Domain 2: Universe compatibility (strict 3-tuple edges, `_tree_manifest_checksum_is_valid`,
  zero mutation of `KnowledgeUnit` / `KnowledgeEvidence` / `TreeManifest`, plus frozen
  `UniverseManifestOut` & `UniverseKnowledgePageOut` schemas via `universe.manifest`,
  `get_verified_tree_manifest_projection`, `load_knowledge_universe_snapshot`, and
  `knowledge_universe_page`).
- Domain 3: OCTX `.octx 0.1` compatibility (`documents` table 100% untouched, Phase 1/2
  metadata isolated to `document_versions`, real `export_snapshot` -> `octx.create_octx` ->
  `octx.validate_octx` -> `octx.open_octx` -> `build_structured_plan` ->
  `import_structured_plan`, and clean FK cascade on document/source deletion under
  `PRAGMA foreign_keys=ON`).
- Domain 4: Full E2E business scenario ("Doc 1 `12 ngày` vs Doc 2 `15 ngày`") exercising
  `resolve_document_conflicts_service`, `current_effective`, `past_point`, `change_lineage`,
  `reopen_conflict`, `resolve_conflict`, `revoke_project_document`, and cross-partition
  Seam A isolation on both retrieval and `get_search_unit_citation`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import octx
import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from sag_api.api.v1 import universe as universe_api
from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    KnowledgeConflictResolution,
    KnowledgeEvidence,
    KnowledgeGraphEdge,
    KnowledgeTreeNode,
    KnowledgeUnit,
    ProjectSearchState,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
    TreeManifest,
    TreeRoutingProfile,
)
from sag_api.db.models.conflict_resolution import compute_conflict_pair_key
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
    DocumentStatus,
    JobStatus,
    JobType,
    SourceStatus,
)
from sag_api.sag.octx_importer import build_structured_plan, import_structured_plan
from sag_api.sag.octx_snapshot import export_snapshot
from sag_api.schemas.universe import UniverseKnowledgePageOut, UniverseManifestOut
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services import source_service
from sag_api.services import universe_knowledge_service as uks
from sag_api.services.document_service import revoke_project_document
from sag_api.services.knowledge_conflict_service import (
    ClassifiedConflictDecision,
    ConflictCandidate,
    persist_conflict_decisions,
    reopen_conflict,
    reset_conflict_classifier_degraded_metrics,
    resolve_conflict,
    resolve_document_conflicts_service,
)
from sag_api.services.knowledge_quality_repository import (
    MAX_CONTRADICTION_CANDIDATES,
    _tree_manifest_checksum_is_valid,
    assess_knowledge_tree,
    build_topic_inputs,
)
from sag_api.services.knowledge_quality_service import (
    KnowledgeQualityConfig,
    QualityTopicInput,
    score_topic,
)
from sag_api.services.knowledge_service import checksum, rebuild_knowledge_version, unit_checksum
from sag_api.services.query_strategy_planner import plan_query
from sag_api.services.search_index_service import generate_search_unit_point_id
from sag_api.services.tree_publish_service import _checksum_manifest, _scoped_profile_checksum
from sag_api.services.tree_query_snapshot_service import _source_profile_payload
from sag_api.services.unit_supersession_overlay import load_superseded_unit_ids_for_scope

TENANT_ID = "tenant-p2e"
PROJECT_ID = "proj-p2e"
P_PUBLIC = "part-public"
P_EXEC = "part-exec"
T_2024_01 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
T_2024_06 = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
T_2025_01 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
T_2025_06 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
VALID_TO_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _make_principal(
    partitions: set[str] | frozenset[str],
    *,
    tenant_id: str = TENANT_ID,
    organization_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p2e",
        organization_id=organization_id,
        issuer="https://issuer.test",
        key_id="k-p2e",
        token_id="tok-p2e",
        issued_at=1000,
        expires_at=9999999999,
        tenant_id=tenant_id,
        allowed_project_ids=frozenset({project_id}),
        allowed_partition_ids=frozenset(partitions),
    )


class _FakeLLM:
    def __init__(self, handler, *, configured: bool = True) -> None:
        self._handler = handler
        self._configured = configured
        self.calls: list[dict] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def complete(
        self,
        messages,
        *,
        response_format=None,
        temperature=None,
        max_tokens=None,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return str(self._handler(messages, response_format))


@pytest.fixture
async def p2e_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(surs, "SessionLocal", factory)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
    universe_api.clear_universe_caches(None)
    uks._VERIFIED_TREE_MANIFEST_CACHE.clear()
    reset_conflict_classifier_degraded_metrics()
    yield factory
    surs._SCOPE_KNOWLEDGE_INDEX_CACHE.clear()
    universe_api.clear_universe_caches(None)
    uks._VERIFIED_TREE_MANIFEST_CACHE.clear()
    reset_conflict_classifier_degraded_metrics()
    await engine.dispose()


async def _seed_ready_doc(
    session: AsyncSession,
    *,
    tag: str,
    filename: str,
    text_body: str,
    partition_id: str = P_PUBLIC,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    scope_tags: list[str] | None = None,
    valid_from: datetime = T_2024_01,
    valid_to: datetime = VALID_TO_FOREVER,
    source_published_at: datetime | None = None,
    section_path: str = "HR > Annual Leave",
    sag_source_id: str | None = None,
    build_knowledge: bool = False,
) -> dict[str, object]:
    now = datetime.now(UTC)
    src_id = f"src-{tag}"
    src = Source(
        id=src_id,
        name=f"Source {tag}",
        sag_source_config_id=f"cfg-{tag}",
        status=SourceStatus.ACTIVE,
    )
    session.add(src)
    await session.flush()

    spm = SourceProjectMapping(
        id=f"spm-{tag}",
        source_id=src.id,
        organization_id=TENANT_ID,
        project_id=PROJECT_ID,
        state="CONFIRMED",
        mapping_version=1,
        confirmed_at=now - timedelta(days=1),
        confirmed_by="admin",
        approval_ref=f"appr-{tag}",
    )
    session.add(spm)
    await session.flush()

    doc = Document(
        id=f"doc-{tag}",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        source_id=src.id,
        logical_source_id=f"logical-{tag}",
        filename=filename,
        storage_path=f"/tmp/{tag}.md",
        status=DocumentStatus.READY,
        is_active=True,
        sag_source_id=sag_source_id,
    )
    session.add(doc)
    await session.flush()

    ver = DocumentVersion(
        id=f"ver-{tag}",
        document_id=doc.id,
        version_no=1,
        file_hash=_sha256(f"file-{tag}"),
        authority_tier=authority_tier,
        effective_from=effective_from,
        source_published_at=source_published_at or effective_from or valid_from,
        observed_at=now - timedelta(days=2),
        ingested_at=now - timedelta(days=2),
        valid_from=valid_from,
        valid_to=valid_to,
        scope_tags_json=scope_tags,
        status="READY",
        search_status="SEARCH_READY",
        search_ready_at=now - timedelta(hours=1),
        metadata_json={"security_partition_id": partition_id},
    )
    session.add(ver)
    await session.flush()

    digest = _sha256(text_body)
    blk = CanonicalBlock(
        id=f"blk-{tag}",
        document_version_id=ver.id,
        ordinal=0,
        block_type="paragraph",
        page_from=1,
        page_to=1,
        section_path=section_path,
        source_anchor=f"anchor-{tag}-0",
        normalized_text=text_body,
        content_hash=digest,
        is_boilerplate=False,
    )
    session.add(blk)
    await session.flush()

    su = SearchUnit(
        id=f"unit-{tag}",
        document_version_id=ver.id,
        block_from_id=blk.id,
        block_to_id=blk.id,
        security_partition_id=partition_id,
        content_hash=digest,
        token_count=max(1, len(text_body.split())),
        page_from=1,
        page_to=1,
        section_path=section_path,
        text_snapshot=text_body,
    )
    session.add(su)
    await session.flush()

    run = IngestionRun(
        id=f"run-{tag}",
        document_version_id=ver.id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        idempotency_key=f"idem-{tag}",
        payload_hash=ver.file_hash,
        status="COMPLETED",
        attempt_count=1,
        started_at=now - timedelta(minutes=5),
        completed_at=now - timedelta(minutes=1),
    )
    session.add(run)
    await session.flush()

    manifest_cs = _sha256(f"idx-{tag}")
    session.add(
        StageRun(
            id=f"sr-{tag}",
            run_id=run.id,
            stage="INDEX_SEARCH",
            status="SUCCESS",
            created_at=now - timedelta(minutes=1),
            metrics_json={
                "manifest_verified": True,
                "collection_name": f"search_units_{PROJECT_ID}",
                "search_unit_count": 1,
                "pg_count": 1,
                "qdrant_count": 1,
                "qdrant_indexed_count": 1,
                "manifest_checksum": manifest_cs,
                "qdrant_checksum": manifest_cs,
                "attempt_count": 1,
            },
        )
    )
    await session.flush()

    ku_list: list[KnowledgeUnit] = []
    if build_knowledge:
        ku_list = await rebuild_knowledge_version(session, ver.id)
        await session.flush()

    return {
        "source": src,
        "doc": doc,
        "ver": ver,
        "block": blk,
        "unit": su,
        "knowledge_units": ku_list,
    }


def _make_conflict_candidate(
    *,
    pair_id: str,
    new_rec: dict[str, object],
    ex_rec: dict[str, object],
    partition_id: str = P_PUBLIC,
    cosine: float = 0.91,
) -> ConflictCandidate:
    new_doc: Document = new_rec["doc"]  # type: ignore[assignment]
    new_ver: DocumentVersion = new_rec["ver"]  # type: ignore[assignment]
    new_unit: SearchUnit = new_rec["unit"]  # type: ignore[assignment]
    ex_doc: Document = ex_rec["doc"]  # type: ignore[assignment]
    ex_ver: DocumentVersion = ex_rec["ver"]  # type: ignore[assignment]
    ex_unit: SearchUnit = ex_rec["unit"]  # type: ignore[assignment]
    return ConflictCandidate(
        pair_id=pair_id,
        pair_key=compute_conflict_pair_key(new_ver.id, new_unit.id, ex_ver.id, ex_unit.id),
        cosine_similarity=cosine,
        new_document_id=new_doc.id,
        new_document_name=new_doc.filename,
        new_version_id=new_ver.id,
        new_version=new_ver,
        new_unit_id=new_unit.id,
        new_content_hash=new_unit.content_hash,
        new_section_path=new_unit.section_path or "",
        new_text=new_unit.text_snapshot or "",
        new_security_partition_id=partition_id,
        existing_document_id=ex_doc.id,
        existing_document_name=ex_doc.filename,
        existing_version_id=ex_ver.id,
        existing_version=ex_ver,
        existing_unit_id=ex_unit.id,
        existing_content_hash=ex_unit.content_hash,
        existing_section_path=ex_unit.section_path or "",
        existing_text=ex_unit.text_snapshot or "",
        existing_security_partition_id=partition_id,
    )


async def _seed_verified_tree_manifest(
    session: AsyncSession,
    *,
    records: list[dict[str, object]],
    node_id: str = "leaf-policy",
    partition_id: str = P_PUBLIC,
    edges: list[list[object]] | None = None,
    with_universe_routing: bool = False,
) -> tuple[TreeManifest, KnowledgeTreeNode, list[KnowledgeUnit]]:
    kunits: list[KnowledgeUnit] = []
    source_profiles: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    for rec in records:
        rec_kus: list[KnowledgeUnit] = rec["knowledge_units"]  # type: ignore[assignment]
        assert len(rec_kus) >= 1, "rebuild_knowledge_version must produce at least 1 KnowledgeUnit"
        kunits.extend(rec_kus)
        src: Source = rec["source"]  # type: ignore[assignment]
        ver: DocumentVersion = rec["ver"]  # type: ignore[assignment]
        su: SearchUnit = rec["unit"]  # type: ignore[assignment]
        source_profiles.append(
            {
                "project_id": PROJECT_ID,
                "source_id": src.id,
                "document_version_id": ver.id,
                "partition_id": partition_id,
                "tenant_id": TENANT_ID,
                "node_id": node_id,
            }
        )
        assignments.append(
            {
                "source_id": src.id,
                "document_version_id": ver.id,
                "partition_id": partition_id,
                "search_unit_id": su.id,
                "knowledge_unit_ids": [ku.id for ku in rec_kus],
            }
        )

    unit_ids = sorted(ku.id for ku in kunits)
    manifest_edges = (
        edges
        if edges is not None
        else ([[unit_ids[0], unit_ids[1], 0.82]] if len(unit_ids) >= 2 else [])
    )
    node_payload = {
        "node_id": node_id,
        "tenant_id": TENANT_ID,
        "project_id": PROJECT_ID,
        "partition_id": partition_id,
        "unit_ids": unit_ids,
    }
    raw_manifest = {
        "tenant_id": TENANT_ID,
        "project_id": PROJECT_ID,
        "routing_profiles": [node_payload],
        "source_routing_profiles": source_profiles,
        "search_unit_assignments": assignments,
        "edges": manifest_edges,
    }
    digest = _checksum_manifest(raw_manifest)
    tree_ver = f"tree-{digest[:24]}"
    # Include tree_version & checksum so BOTH `_tree_manifest_checksum_is_valid` AND
    # `get_verified_tree_manifest_projection` validate this exact TreeManifest!
    stored_manifest_json = {
        **raw_manifest,
        "tree_version": tree_ver,
        "checksum": digest,
    }
    manifest = TreeManifest(
        tree_version=tree_ver,
        project_id=PROJECT_ID,
        config_version="v1",
        node_count=1,
        leaf_count=1,
        max_leaf_size=len(unit_ids),
        giant_ratio=0.0,
        routing_recall_at_k=1.0,
        escape_win_rate=0.0,
        acl_blackhole_rate=0.0,
        status="ACTIVE",
        checksum=digest,
        manifest_json=stored_manifest_json,
    )
    session.add(manifest)
    await session.flush()

    tree_node = KnowledgeTreeNode(
        tree_version=tree_ver,
        node_id=node_id,
        parent_id=None,
        security_partition_id=partition_id,
        payload_json=node_payload,
        checksum=checksum(node_payload),
    )
    session.add(tree_node)

    if with_universe_routing:
        session.add(
            ProjectSearchState(
                project_id=PROJECT_ID,
                active_tree_version=tree_ver,
                active_routing_slot="SLOT_A",
                slot_a_tree_version=tree_ver,
                active_search_epoch=1,
            )
        )
        for sp in source_profiles:
            rec_count = sum(
                len(a["knowledge_unit_ids"])
                for a in assignments
                if a["source_id"] == sp["source_id"]
                and a["document_version_id"] == sp["document_version_id"]
            )
            rp = TreeRoutingProfile(
                project_id=str(sp["project_id"]),
                tree_version=tree_ver,
                source_id=str(sp["source_id"]),
                document_version_id=str(sp["document_version_id"]),
                partition_id=str(sp["partition_id"]),
                tenant_id=str(sp["tenant_id"]),
                node_id=str(sp["node_id"]),
                is_leaf=True,
                accessible_unit_count=rec_count,
                sparse_json=[["policy", 1.0]],
                entities_json=[],
            )
            rp.profile_checksum = _scoped_profile_checksum(tree_ver, _source_profile_payload(rp))
            session.add(rp)

    await session.flush()
    return manifest, tree_node, kunits


# ---------------------------------------------------------------------------
# Domain 1: KnowledgeQuality Integration & `is_candidate` Lifecycle Sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain1_knowledge_quality_is_candidate_lifecycle_sync(p2e_db) -> None:
    """Verify full KnowledgeQuality repository + scoring pipeline reacts deterministically
    to `HARD_CONTRADICTION` -> `WINNER_B` / `KEEP_BOTH_SCOPED` -> `reopen_conflict`."""
    as_of = datetime.now(UTC)
    async with p2e_db() as session:
        rec1 = await _seed_ready_doc(
            session,
            tag="kq1",
            filename="leave_policy_v1.md",
            text_body="Policy Alpha effective 2024-01-01 grants 12% bonus and 12 annual leave days under SLA-01.",
            authority_tier=2,
            effective_from=T_2024_01,
            build_knowledge=True,
        )
        rec2 = await _seed_ready_doc(
            session,
            tag="kq2",
            filename="leave_policy_v2.md",
            text_body="Policy Alpha effective 2025-01-01 grants 15% bonus and 15 annual leave days under SLA-02.",
            authority_tier=2,
            effective_from=T_2025_01,
            build_knowledge=True,
        )
        rec3 = await _seed_ready_doc(
            session,
            tag="kq3",
            filename="leave_policy_regional.md",
            text_body="Policy Beta effective 2025-01-01 grants 20% bonus and 20 annual leave days under SLA-03.",
            authority_tier=2,
            effective_from=T_2025_01,
            build_knowledge=True,
        )
        manifest, _node, kunits = await _seed_verified_tree_manifest(
            session,
            records=[rec1, rec2, rec3],
            node_id="node-leave-policy",
        )
        assert _tree_manifest_checksum_is_valid(manifest) is True
        # Ensure every KnowledgeUnit has anchored evidence with quote + source_anchor
        for ku in kunits:
            evs = (
                await session.scalars(select(KnowledgeEvidence).where(KnowledgeEvidence.unit_id == ku.id))
            ).all()
            assert len(evs) >= 1, f"Expected anchored evidence for {ku.id}"
        await session.commit()

        # Baseline: zero contradictions -> contradiction_candidate_rate == 0.0
        base_topics, _ = await build_topic_inputs(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        assert len(base_topics) == 1
        assert isinstance(base_topics[0], QualityTopicInput)
        base_score = score_topic(base_topics[0], KnowledgeQualityConfig())
        assert base_score.metrics.contradiction_candidate_rate == 0.0
        assert "CONTRADICTION_CANDIDATE" not in base_score.reason_codes

        # Step 1: Persist two HARD_CONTRADICTION pairs (PENDING_REVIEW) -> CONTRADICTS edges with is_candidate=True
        cand_12 = _make_conflict_candidate(pair_id="p-12", new_rec=rec2, ex_rec=rec1)
        cand_23 = _make_conflict_candidate(pair_id="p-23", new_rec=rec3, ex_rec=rec2)
        summary = await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            decisions=[
                ClassifiedConflictDecision(
                    candidate=cand_12,
                    conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
                    confidence=0.92,
                    winner="NONE",
                    scope_difference_summary=None,
                    reason="Incompatible annual leave quotas 12 vs 15",
                ),
                ClassifiedConflictDecision(
                    candidate=cand_23,
                    conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
                    confidence=0.90,
                    winner="NONE",
                    scope_difference_summary=None,
                    reason="Incompatible annual leave quotas 15 vs 20",
                ),
            ],
            now_utc=as_of,
        )
        assert summary.pending_review == 2
        await session.commit()

        res_12 = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.project_id == PROJECT_ID,
                KnowledgeConflictResolution.pair_key == cand_12.pair_key,
            )
        )
        res_23 = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.project_id == PROJECT_ID,
                KnowledgeConflictResolution.pair_key == cand_23.pair_key,
            )
        )
        assert res_12 is not None and res_23 is not None
        assert res_12.graph_edge_id is not None and res_23.graph_edge_id is not None
        edge_12 = await session.get(KnowledgeGraphEdge, res_12.graph_edge_id)
        edge_23 = await session.get(KnowledgeGraphEdge, res_23.graph_edge_id)
        assert edge_12 is not None and edge_12.edge_type == "CONTRADICTS"
        assert edge_12.metadata_json.get("is_candidate") is True
        assert edge_23 is not None and edge_23.edge_type == "CONTRADICTS"
        assert edge_23.metadata_json.get("is_candidate") is True

        # Real KnowledgeQuality collector + full assess_knowledge_tree -> contradiction_candidate_rate > 0
        assessment, results = await assess_knowledge_tree(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        assert assessment is not None
        assert len(results) == 1
        # 3 units -> 3 possible pairs, 2 contradicting pairs -> 2/3
        assert results[0].metrics.contradiction_candidate_rate == pytest.approx(2.0 / 3.0)
        assert "CONTRADICTION_CANDIDATE" in results[0].reason_codes

        # Step 2: Resolve pair 1 via WINNER_B -> edge_12.is_candidate becomes False, rate drops to 1/3
        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=res_12.id,
            action="WINNER_B",
            reason="2025 policy supersedes 2024 policy",
            actor="reviewer-1",
        )
        await session.refresh(edge_12)
        assert edge_12.metadata_json.get("is_candidate") is False

        topics_mid, _ = await build_topic_inputs(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        score_mid = score_topic(topics_mid[0])
        assert score_mid.metrics.contradiction_candidate_rate == pytest.approx(1.0 / 3.0)

        # Step 3: Resolve pair 2 via KEEP_BOTH_SCOPED -> edge_23.is_candidate becomes False, rate drops to 0.0
        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=res_23.id,
            action="KEEP_BOTH_SCOPED",
            reason="Regional branch exemption applies to different office",
            actor="reviewer-1",
        )
        await session.refresh(edge_23)
        assert edge_23.metadata_json.get("is_candidate") is False

        topics_resolved, _ = await build_topic_inputs(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        assert len(topics_resolved[0].contradictions) == 0
        score_resolved = score_topic(topics_resolved[0])
        assert score_resolved.metrics.contradiction_candidate_rate == 0.0
        assert "CONTRADICTION_CANDIDATE" not in score_resolved.reason_codes

        # Step 4: Reopen pair 1 -> edge_12.is_candidate flips back to True, rate rises > 0.0 again
        await reopen_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=res_12.id,
            reason="Legal requested re-review",
            actor="reviewer-2",
        )
        await session.refresh(edge_12)
        assert edge_12.metadata_json.get("is_candidate") is True

        topics_reopened, _ = await build_topic_inputs(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        assert len(topics_reopened[0].contradictions) == 1
        score_reopened = score_topic(topics_reopened[0])
        assert score_reopened.metrics.contradiction_candidate_rate == pytest.approx(1.0 / 3.0)
        assert score_reopened.metrics.contradiction_candidate_rate > 0.0
        assert "CONTRADICTION_CANDIDATE" in score_reopened.reason_codes


@pytest.mark.asyncio
async def test_domain1_max_contradiction_candidates_5000_cap_preserves_quality_scoring(
    p2e_db, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify the 5,000 CONTRADICTS edge cap guard persists conflict #5001 cleanly without
    crashing `build_topic_inputs` / `score_topic` or exceeding `MAX_CONTRADICTION_CANDIDATES`."""
    as_of = datetime.now(UTC)
    async with p2e_db() as session:
        rec1 = await _seed_ready_doc(
            session,
            tag="cap1",
            filename="sla_a.md",
            text_body="Service Alpha SLA-10 guarantees 99.9% uptime effective 2024-01-01.",
            authority_tier=2,
            effective_from=T_2024_01,
            build_knowledge=True,
        )
        rec2 = await _seed_ready_doc(
            session,
            tag="cap2",
            filename="sla_b.md",
            text_body="Service Alpha SLA-11 guarantees 99.5% uptime effective 2025-01-01.",
            authority_tier=2,
            effective_from=T_2025_01,
            build_knowledge=True,
        )
        rec3 = await _seed_ready_doc(
            session,
            tag="cap3",
            filename="sla_c.md",
            text_body="Service Alpha SLA-12 guarantees 98.0% uptime effective 2025-01-01.",
            authority_tier=2,
            effective_from=T_2025_01,
            build_knowledge=True,
        )
        manifest, _node, _kunits = await _seed_verified_tree_manifest(
            session,
            records=[rec1, rec2, rec3],
            node_id="node-sla",
        )

        # Persist 1 real contradiction edge on (rec1, rec2)
        cand_12 = _make_conflict_candidate(pair_id="cap-12", new_rec=rec2, ex_rec=rec1)
        await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            decisions=[
                ClassifiedConflictDecision(
                    candidate=cand_12,
                    conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
                    confidence=0.91,
                    winner="NONE",
                    scope_difference_summary=None,
                    reason="SLA conflict 99.9% vs 99.5%",
                )
            ],
            now_utc=as_of,
        )
        await session.flush()

        # Bulk-seed 72 dummy SearchUnits in a separate inactive/unlinked version to generate
        # exactly MAX_CONTRADICTION_CANDIDATES - 1 (4,999) unique (source, target) CONTRADICTS edges.
        rec_fill = await _seed_ready_doc(
            session,
            tag="capfill",
            filename="filler.md",
            text_body="Filler reference block for cap verification.",
            authority_tier=4,
            effective_from=T_2024_01,
            build_knowledge=False,
        )
        fill_ver: DocumentVersion = rec_fill["ver"]  # type: ignore[assignment]
        fill_blk: CanonicalBlock = rec_fill["block"]  # type: ignore[assignment]
        dummy_unit_ids: list[str] = []
        dummy_units: list[dict[str, object]] = []
        for idx in range(72):
            uid = f"u-cap-{idx:03d}"
            dummy_unit_ids.append(uid)
            dummy_units.append(
                {
                    "id": uid,
                    "document_version_id": fill_ver.id,
                    "block_from_id": fill_blk.id,
                    "block_to_id": fill_blk.id,
                    "security_partition_id": P_PUBLIC,
                    "content_hash": _sha256(f"cap-unit-{idx}"),
                    "token_count": 4,
                    "page_from": 1,
                    "page_to": 1,
                    "section_path": "Filler",
                }
            )
        await session.execute(insert(SearchUnit), dummy_units)

        needed = MAX_CONTRADICTION_CANDIDATES - 1
        filler_edges: list[dict[str, object]] = []
        for i in range(len(dummy_unit_ids)):
            for j in range(len(dummy_unit_ids)):
                if i == j:
                    continue
                filler_edges.append(
                    {
                        "id": str(uuid.uuid4()),
                        "project_id": PROJECT_ID,
                        "source_unit_id": dummy_unit_ids[i],
                        "target_unit_id": dummy_unit_ids[j],
                        "edge_type": "CONTRADICTS",
                        "weight": 0.5,
                        "calibrated_weight": 0.5,
                        "metadata_json": {"is_candidate": False},
                    }
                )
                if len(filler_edges) >= needed:
                    break
            if len(filler_edges) >= needed:
                break
        assert len(filler_edges) == needed
        await session.execute(insert(KnowledgeGraphEdge), filler_edges)
        await session.commit()

        # Persist conflict #5001 -> resolution saved, edge skipped with cap warning, total edges == 5000
        cand_13 = _make_conflict_candidate(pair_id="cap-5001", new_rec=rec3, ex_rec=rec1)
        with caplog.at_level(logging.WARNING):
            cap_summary = await persist_conflict_decisions(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=P_PUBLIC,
                decisions=[
                    ClassifiedConflictDecision(
                        candidate=cand_13,
                        conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
                        confidence=0.90,
                        winner="NONE",
                        scope_difference_summary=None,
                        reason="Conflict #5001 must honor MAX_CONTRADICTION_CANDIDATES cap",
                    )
                ],
                now_utc=as_of,
            )
        await session.commit()

        assert cap_summary.pending_review == 1
        assert cap_summary.contradiction_edge_cap_hits == 1
        assert any("contradiction_edge_cap_reached=True" in rec.getMessage() for rec in caplog.records)

        row_5001 = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.project_id == PROJECT_ID,
                KnowledgeConflictResolution.pair_key == cand_13.pair_key,
            )
        )
        assert row_5001 is not None
        assert row_5001.status == ConflictResolutionStatus.PENDING_REVIEW.value
        assert row_5001.graph_edge_id is None

        total_edges = await session.scalar(
            select(func.count(KnowledgeGraphEdge.id)).where(
                KnowledgeGraphEdge.project_id == PROJECT_ID,
                KnowledgeGraphEdge.edge_type == "CONTRADICTS",
            )
        )
        assert int(total_edges or 0) == MAX_CONTRADICTION_CANDIDATES

        # Real KnowledgeQuality collector MUST NOT crash and MUST still score the live candidate!
        topics, _ = await build_topic_inputs(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            tree_version=manifest.tree_version,
            as_of=as_of,
            demand_window_days=7,
        )
        result = score_topic(topics[0])
        assert result.metrics.contradiction_candidate_rate > 0.0
        assert "CONTRADICTION_CANDIDATE" in result.reason_codes


# ---------------------------------------------------------------------------
# Domain 2: Universe Compatibility (3-Tuple Edges, Manifest Checksum, Frozen Schemas)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain2_universe_3tuple_edges_manifest_checksum_and_frozen_schemas(p2e_db) -> None:
    """Verify conflict detection, HITL resolve/reopen, and document revocation NEVER mutate
    `KnowledgeUnit`, `KnowledgeEvidence`, or `TreeManifest`, and keep Universe 3-tuple edges
    and frozen `UniverseManifestOut` / `UniverseKnowledgePageOut` schemas 100% intact."""
    as_of = datetime.now(UTC)
    principal = _make_principal({P_PUBLIC})
    user = SimpleNamespace(id="reader-p2e")

    async with p2e_db() as session:
        rec1 = await _seed_ready_doc(
            session,
            tag="uni1",
            filename="policy_2024.md",
            text_body="Project Orion SLA-2024 requires 12 annual leave days starting 2024-01-01.",
            authority_tier=2,
            effective_from=T_2024_01,
            build_knowledge=True,
        )
        rec2 = await _seed_ready_doc(
            session,
            tag="uni2",
            filename="policy_2025.md",
            text_body="Project Orion SLA-2025 requires 15 annual leave days starting 2025-01-01.",
            authority_tier=1,
            effective_from=T_2025_01,
            build_knowledge=True,
        )
        manifest, _node, kunits = await _seed_verified_tree_manifest(
            session,
            records=[rec1, rec2],
            node_id="leaf-orion",
            with_universe_routing=True,
        )
        await session.commit()

        # Baseline integrity snapshot of TreeManifest, KnowledgeUnits, and KnowledgeEvidence
        baseline_manifest_json = json.dumps(manifest.manifest_json, sort_keys=True)
        baseline_manifest_checksum = manifest.checksum
        baseline_units = {
            ku.id: (ku.text, ku.checksum, json.dumps(ku.provenance_json, sort_keys=True))
            for ku in kunits
        }
        baseline_evidence = {
            ev.id: (ev.tier, ev.kind, ev.confidence, json.dumps(ev.payload_json, sort_keys=True))
            for ev in (
                await session.scalars(
                    select(KnowledgeEvidence).where(KnowledgeEvidence.unit_id.in_(list(baseline_units)))
                )
            ).all()
        }

        async def _assert_immutable_integrity(stage: str) -> None:
            fresh_manifest = await session.get(TreeManifest, manifest.tree_version, populate_existing=True)
            assert fresh_manifest is not None, stage
            assert fresh_manifest.checksum == baseline_manifest_checksum, stage
            assert json.dumps(fresh_manifest.manifest_json, sort_keys=True) == baseline_manifest_json, stage
            assert _tree_manifest_checksum_is_valid(fresh_manifest) is True, stage

            for ku_id, (orig_text, orig_cs, orig_prov) in baseline_units.items():
                ku = await session.get(KnowledgeUnit, ku_id, populate_existing=True)
                assert ku is not None, f"{stage}:{ku_id}"
                assert ku.text == orig_text, f"{stage}:{ku_id}"
                assert ku.checksum == orig_cs, f"{stage}:{ku_id}"
                assert await unit_checksum(session, ku) == orig_cs, f"{stage}:{ku_id}"
                assert json.dumps(ku.provenance_json, sort_keys=True) == orig_prov, f"{stage}:{ku_id}"

            for ev_id, orig_tuple in baseline_evidence.items():
                ev = await session.get(KnowledgeEvidence, ev_id, populate_existing=True)
                assert ev is not None, f"{stage}:{ev_id}"
                assert (
                    ev.tier,
                    ev.kind,
                    ev.confidence,
                    json.dumps(ev.payload_json, sort_keys=True),
                ) == orig_tuple, f"{stage}:{ev_id}"

        await _assert_immutable_integrity("before_conflict")

        # 1. Conflict detection -> creates CONTRADICTS edge
        cand = _make_conflict_candidate(pair_id="uni-pair", new_rec=rec2, ex_rec=rec1)
        await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=P_PUBLIC,
            decisions=[
                ClassifiedConflictDecision(
                    candidate=cand,
                    conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
                    confidence=0.93,
                    winner="NONE",
                    scope_difference_summary=None,
                    reason="Conflict under review",
                )
            ],
            now_utc=as_of,
        )
        await session.commit()
        await _assert_immutable_integrity("after_persist_contradicts")

        kcr = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.project_id == PROJECT_ID,
                KnowledgeConflictResolution.pair_key == cand.pair_key,
            )
        )
        assert kcr is not None

        # 2. Resolve conflict -> creates SUPERSEDES edge
        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=kcr.id,
            action="WINNER_B",
            reason="2025 supersedes 2024",
            actor="admin",
        )
        sup_edge = await session.scalar(
            select(KnowledgeGraphEdge).where(
                KnowledgeGraphEdge.project_id == PROJECT_ID,
                KnowledgeGraphEdge.edge_type == "SUPERSEDES",
            )
        )
        assert sup_edge is not None
        await _assert_immutable_integrity("after_resolve_supersedes")

        # 3. Reopen conflict -> removes SUPERSEDES, restores CONTRADICTS is_candidate=True
        await reopen_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=kcr.id,
            reason="Re-check scope",
            actor="admin",
        )
        await _assert_immutable_integrity("after_reopen")

        # 4. Exercise REAL Universe projections with both CONTRADICTS and SUPERSEDES edges present
        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={P_PUBLIC},
            resolution_id=kcr.id,
            action="WINNER_B",
            reason="Confirm winner",
            actor="admin",
        )

        # 4a. get_verified_tree_manifest_projection -> strict 3-tuple edges, intact SHA-256
        projection = await uks.get_verified_tree_manifest_projection(
            session,
            PROJECT_ID,
            manifest.tree_version,
            manifest.checksum,
        )
        assert projection["project_id"] == PROJECT_ID
        assert len(projection["edges"]) == 1
        assert len(projection["edges"][0]) == 3
        assert isinstance(projection["edges"][0][0], str)
        assert isinstance(projection["edges"][0][1], str)
        assert isinstance(projection["edges"][0][2], (int, float))

        # 4b. universe.manifest -> frozen UniverseManifestOut schema
        manifest_out = await universe_api.manifest(
            session=session,
            user=user,
            principal=principal,
            project_id=PROJECT_ID,
        )
        assert isinstance(manifest_out, UniverseManifestOut)
        assert manifest_out.status == "ready"
        assert len(manifest_out.trees) == 1
        assert manifest_out.trees[0].tree_version == manifest.tree_version
        validated_manifest = UniverseManifestOut.model_validate(manifest_out.model_dump())
        assert validated_manifest.status == "ready"

        # 4c. load_knowledge_universe_snapshot + knowledge_universe_page -> frozen UniverseKnowledgePageOut
        snapshot = await uks.load_knowledge_universe_snapshot(
            session,
            principal,
            manifest_out.trees[0],
        )
        assert len(snapshot.edges) == 1
        assert all(
            isinstance(edge, tuple)
            and len(edge) == 3
            and isinstance(edge[0], str)
            and isinstance(edge[1], str)
            and isinstance(edge[2], float)
            for edge in snapshot.edges
        )

        page_out = await uks.knowledge_universe_page(
            session,
            snapshot,
            node_id="leaf-orion",
            limit=24,
        )
        assert isinstance(page_out, UniverseKnowledgePageOut)
        assert page_out.total == 2
        assert {u.id for u in page_out.units} == set(baseline_units.keys())
        assert len(page_out.edges) == 1
        validated_page = UniverseKnowledgePageOut.model_validate(page_out.model_dump())
        assert validated_page.total == 2
        assert not session.new and not session.dirty and not session.deleted

        # 5. Revoke Doc 2 -> TreeManifest checksum & KnowledgeUnit/KnowledgeEvidence stay 100% immutable!
        doc2: Document = rec2["doc"]  # type: ignore[assignment]
        revoke_res = await revoke_project_document(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            document_id=doc2.id,
            reason="POLICY_WITHDRAWN",
        )
        assert revoke_res.status == "REVOKED"
        await _assert_immutable_integrity("after_revoke_document")


# ---------------------------------------------------------------------------
# Domain 3: OCTX `.octx 0.1` Compatibility & Untouched `documents` Table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain3_octx_01_compat_untouched_documents_and_fk_cascades(
    p2e_db, tmp_path: Path
) -> None:
    """Verify:
    1. `documents` table has ZERO Phase 1/2 temporal/authority columns (`authority_tier`,
       `effective_from`, `scope_tags_json` live strictly on `document_versions`).
    2. Real `.octx 0.1` export -> validate -> open -> build_structured_plan ->
       import_structured_plan succeeds cleanly alongside Phase 2 metadata & conflict rows
       with manifest version `"0.1"` and zero unexpected extra fields.
    3. Deleting/replacing documents or sources cleanly cascades `KnowledgeConflictResolution`
       rows with zero FK errors under `PRAGMA foreign_keys=ON`.
    """
    from zleap.sag.db.models import (
        Article,
        DataSource,
        Entity,
        EntityType,
        EventEntity,
        SourceChunk,
        SourceEvent,
    )
    from zleap.sag.db.models import Base as SagBase

    # 1. Column placement contract: `documents` untouched, `document_versions` owns Phase 1/2 fields
    doc_columns = set(Document.__table__.columns.keys())
    ver_columns = set(DocumentVersion.__table__.columns.keys())
    for forbidden_on_doc in ("authority_tier", "effective_from", "scope_tags_json"):
        assert forbidden_on_doc not in doc_columns, f"{forbidden_on_doc} must NOT be on documents"
        assert forbidden_on_doc in ver_columns, f"{forbidden_on_doc} must live on document_versions"

    # Build an isolated in-memory SQLite SAG engine DB for real OCTX snapshot export & import
    sag_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with sag_engine.begin() as conn:
        for table in SagBase.metadata.sorted_tables:
            await conn.execute(CreateTable(table, if_not_exists=True))
    sag_factory = async_sessionmaker(sag_engine, expire_on_commit=False)

    try:
        async with p2e_db() as session:
            # Verify PRAGMA foreign_keys=ON is truly enforced in this session
            fk_on = await session.scalar(text("PRAGMA foreign_keys"))
            assert fk_on == 1

            rec1 = await _seed_ready_doc(
                session,
                tag="octx1",
                filename="leave_2024.md",
                text_body="Annual leave policy 2024 specifies 12 days per calendar year.",
                authority_tier=2,
                effective_from=T_2024_01,
                scope_tags=["vn", "hr-2024"],
                sag_source_id="art-octx-1",
            )
            rec2 = await _seed_ready_doc(
                session,
                tag="octx2",
                filename="leave_2025.md",
                text_body="Annual leave policy 2025 specifies 15 days per calendar year.",
                authority_tier=1,
                effective_from=T_2025_01,
                scope_tags=["vn", "hr-2025"],
                sag_source_id="art-octx-2",
            )
            src1: Source = rec1["source"]  # type: ignore[assignment]
            doc1: Document = rec1["doc"]  # type: ignore[assignment]
            ver1: DocumentVersion = rec1["ver"]  # type: ignore[assignment]
            unit1: SearchUnit = rec1["unit"]  # type: ignore[assignment]
            doc2: Document = rec2["doc"]  # type: ignore[assignment]
            ver2: DocumentVersion = rec2["ver"]  # type: ignore[assignment]
            unit2: SearchUnit = rec2["unit"]  # type: ignore[assignment]
            doc2_id, ver2_id, unit2_id, unit2_hash = doc2.id, ver2.id, unit2.id, unit2.content_hash

            # Seed an ACTIVE KnowledgeConflictResolution alongside the OCTX-backed docs
            kcr = KnowledgeConflictResolution(
                id="kcr-octx-1",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=P_PUBLIC,
                winner_security_partition_id=P_PUBLIC,
                pair_key=compute_conflict_pair_key(ver2.id, unit2.id, ver1.id, unit1.id),
                winner_document_id=doc2.id,
                winner_version_id=ver2.id,
                winner_unit_id=unit2.id,
                winner_content_hash=unit2.content_hash,
                loser_document_id=doc1.id,
                loser_version_id=ver1.id,
                loser_unit_id=unit1.id,
                loser_content_hash=unit1.content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
                confidence=0.95,
                effective_valid_to=T_2025_01,
                reason="2025 policy replaces 2024 policy",
            )
            session.add(kcr)
            await session.commit()

            # 2. Populate matching SAG partition for src1 and run REAL `.octx 0.1` export/validate/import
            async with sag_factory() as sag_session:
                sag_session.add(DataSource(id=src1.sag_source_config_id, name=src1.name))
                sag_session.add(
                    Article(
                        id="art-octx-1",
                        data_source_id=src1.sag_source_config_id,
                        title="Leave Policy 2024",
                        content="Annual leave policy 2024 specifies 12 days per calendar year.",
                    )
                )
                sag_session.add(
                    SourceChunk(
                        id="chk-octx-1",
                        data_source_id=src1.sag_source_config_id,
                        source_type="ARTICLE",
                        source_id="art-octx-1",
                        article_id="art-octx-1",
                        content="Annual leave policy 2024 specifies 12 days per calendar year.",
                        raw_content="Annual leave policy 2024 specifies 12 days per calendar year.",
                        heading="Annual Leave",
                        rank=0,
                        chunk_length=60,
                    )
                )
                sag_session.add(
                    EntityType(
                        id="et-octx-1",
                        scope="source",
                        data_source_id=src1.sag_source_config_id,
                        type="policy",
                        name="policy",
                        weight=Decimal("1.00"),
                        similarity_threshold=Decimal("0.800"),
                    )
                )
                sag_session.add(
                    Entity(
                        id="ent-octx-1",
                        data_source_id=src1.sag_source_config_id,
                        entity_type_id="et-octx-1",
                        type="policy",
                        name="Annual Leave Policy",
                        normalized_name="annual leave policy",
                    )
                )
                sag_session.add(
                    SourceEvent(
                        id="evt-octx-1",
                        data_source_id=src1.sag_source_config_id,
                        source_type="ARTICLE",
                        source_id="art-octx-1",
                        article_id="art-octx-1",
                        chunk_id="chk-octx-1",
                        title="Leave Policy 2024",
                        summary="12 annual leave days",
                        content="Annual leave policy 2024 specifies 12 days per calendar year.",
                    )
                )
                sag_session.add(
                    EventEntity(
                        id="ee-octx-1",
                        event_id="evt-octx-1",
                        entity_id="ent-octx-1",
                        weight=Decimal("1.00"),
                    )
                )
                await sag_session.commit()

            ws_dir = tmp_path / "octx_export_ws"
            snap_stats = await export_snapshot(
                src1,
                [doc1],
                ws_dir,
                producer_state_path=tmp_path / "producer_ids.json",
                session_factory=sag_factory,
            )
            assert snap_stats.counts == {
                "documents": 1,
                "chunks": 1,
                "events": 1,
                "entities": 1,
                "chunk_events": 1,
                "event_entities": 1,
            }

            archive_path = tmp_path / "leave_2024.octx"
            octx.create_octx(
                ws_dir,
                output=archive_path,
                name="Leave Policy 2024",
                version="1.0.0",
                capabilities={"sag-structured": "0.1"},
            )
            report = octx.validate_octx(archive_path).to_dict()
            assert report["valid"] is True
            assert report["fully_validated"] is True

            with octx.open_octx(archive_path, validate=True) as pkg:
                manifest_dict = dict(pkg.manifest)
                # Strict `.octx 0.1` format & sag-structured capability version check
                format_ver = (
                    manifest_dict.get("octx_version")
                    or manifest_dict.get("format_version")
                    or (manifest_dict.get("format") or {}).get("version")
                    if isinstance(manifest_dict.get("format"), dict)
                    else manifest_dict.get("octx_version") or manifest_dict.get("format_version")
                )
                assert format_ver == "0.1", f"Unexpected OCTX manifest version: {manifest_dict}"
                sag_cap = (manifest_dict.get("capabilities") or {}).get("sag-structured")
                sag_cap_ver = sag_cap.get("version") if isinstance(sag_cap, dict) else sag_cap
                assert sag_cap_ver == "0.1"

                # Zero Phase 1/2 field leakage into manifest or document frontmatter
                serialized_manifest = json.dumps(manifest_dict)
                for forbidden_key in (
                    "authority_tier",
                    "effective_from",
                    "scope_tags_json",
                    "knowledge_conflict_resolutions",
                    "conflict_status",
                ):
                    assert forbidden_key not in serialized_manifest

                exported_docs = list(pkg.iter_documents())
                assert len(exported_docs) == 1
                doc_meta_json = json.dumps(dict(exported_docs[0].metadata))
                for forbidden_key in ("authority_tier", "effective_from", "scope_tags_json"):
                    assert forbidden_key not in doc_meta_json

            # Round-trip real OCTX import plan -> SAG materialization -> OCTX `Document(...)` constructor
            plan_path = tmp_path / "import_plan.sqlite3"
            id_namespace = str(uuid.uuid4())
            plan_stats = build_structured_plan(archive_path, plan_path, id_namespace)
            assert plan_stats.counts["chunks"] == 1
            assert plan_stats.counts["events"] == 1

            imp_stats = await import_structured_plan(
                plan_path,
                id_namespace,
                source_config_id="cfg-octx-imported",
                source_name="Imported Leave 2024",
                session_factory=sag_factory,
            )
            assert imp_stats.counts == {
                "documents": 1,
                "chunks": 1,
                "events": 1,
                "entities": 1,
                "event_entities": 1,
            }

            # Exact OCTX `Document(...)` constructor kwargs from `octx_transfer_service.py:732`
            imp_source = Source(
                id="src-octx-imported",
                name="Imported Leave 2024",
                sag_source_config_id="cfg-octx-imported",
                status=SourceStatus.ACTIVE,
            )
            session.add(imp_source)
            await session.flush()
            octx_doc = Document(
                source_id=imp_source.id,
                filename="leave_2024.md",
                content_type="text/markdown",
                size_bytes=60,
                storage_path=str(tmp_path / "imported_leave_2024.md"),
                status=DocumentStatus.READY,
                chunk_count=1,
                event_count=1,
                progress=100,
                sag_source_id="art-imp-1",
                octx_installation_id=None,
                octx_document_id="doc-octx-imported-1",
                is_active=True,
            )
            session.add(octx_doc)
            await session.commit()
            assert (await session.get(Document, octx_doc.id)) is not None

            # 3a. OCTX source replacement (deactivating prior docs + adding replacement doc)
            # immediately stops live supersession with zero FK errors under PRAGMA foreign_keys=ON.
            src2: Source = rec2["source"]  # type: ignore[assignment]
            sup_before_replace = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[ver1.id],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions={P_PUBLIC},
                reference_time=T_2025_06,
                organization_id=TENANT_ID,
            )
            assert unit1.id in sup_before_replace

            await session.execute(
                update(Document)
                .where(Document.source_id == src2.id, Document.is_active.is_(True))
                .values(is_active=False)
            )
            replacement_doc = Document(
                source_id=src2.id,
                filename="leave_2025_replaced.md",
                content_type="text/markdown",
                size_bytes=64,
                storage_path=str(tmp_path / "leave_2025_replaced.md"),
                status=DocumentStatus.READY,
                chunk_count=1,
                event_count=1,
                progress=100,
                sag_source_id="art-octx-2-replaced",
                octx_installation_id=None,
                octx_document_id="doc-octx-replaced-2",
                is_active=True,
            )
            session.add(replacement_doc)
            await session.commit()

            sup_after_replace = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[ver1.id],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions={P_PUBLIC},
                reference_time=T_2025_06,
                organization_id=TENANT_ID,
            )
            assert unit1.id not in sup_after_replace

            # 3b. Direct Document delete under PRAGMA foreign_keys=ON cascades KnowledgeConflictResolution
            await session.delete(doc1)
            await session.commit()
            session.expire_all()
            assert (
                await session.scalar(
                    select(KnowledgeConflictResolution).where(
                        KnowledgeConflictResolution.id == "kcr-octx-1"
                    )
                )
                is None
            )

            # 3c. Real `source_service.delete_source` cascades both winner & loser conflict rows cleanly
            rec_a = await _seed_ready_doc(
                session,
                tag="delA",
                filename="del_a.md",
                text_body="Source delete test document A with 10 days policy.",
                authority_tier=1,
                effective_from=T_2025_01,
                scope_tags=["global"],
            )
            rec_b = await _seed_ready_doc(
                session,
                tag="delB",
                filename="del_b.md",
                text_body="Source delete test document B with 12 days policy.",
                authority_tier=2,
                effective_from=T_2024_01,
                scope_tags=["global"],
            )
            src_a: Source = rec_a["source"]  # type: ignore[assignment]
            doc_a: Document = rec_a["doc"]  # type: ignore[assignment]
            ver_a: DocumentVersion = rec_a["ver"]  # type: ignore[assignment]
            unit_a: SearchUnit = rec_a["unit"]  # type: ignore[assignment]
            doc_b: Document = rec_b["doc"]  # type: ignore[assignment]
            ver_b: DocumentVersion = rec_b["ver"]  # type: ignore[assignment]
            unit_b: SearchUnit = rec_b["unit"]  # type: ignore[assignment]

            session.add_all(
                [
                    KnowledgeConflictResolution(
                        id="kcr-src-winner",
                        tenant_id=TENANT_ID,
                        project_id=PROJECT_ID,
                        security_partition_id=P_PUBLIC,
                        winner_security_partition_id=P_PUBLIC,
                        pair_key=compute_conflict_pair_key(ver_a.id, unit_a.id, ver_b.id, unit_b.id),
                        winner_document_id=doc_a.id,
                        winner_version_id=ver_a.id,
                        winner_unit_id=unit_a.id,
                        winner_content_hash=unit_a.content_hash,
                        loser_document_id=doc_b.id,
                        loser_version_id=ver_b.id,
                        loser_unit_id=unit_b.id,
                        loser_content_hash=unit_b.content_hash,
                        conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                        status=ConflictResolutionStatus.ACTIVE.value,
                        resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
                        confidence=0.92,
                        effective_valid_to=T_2025_01,
                        reason="A supersedes B",
                    ),
                    KnowledgeConflictResolution(
                        id="kcr-src-loser",
                        tenant_id=TENANT_ID,
                        project_id=PROJECT_ID,
                        security_partition_id=P_PUBLIC,
                        winner_security_partition_id=P_PUBLIC,
                        pair_key=compute_conflict_pair_key(ver2_id, unit2_id, ver_a.id, unit_a.id),
                        winner_document_id=doc2_id,
                        winner_version_id=ver2_id,
                        winner_unit_id=unit2_id,
                        winner_content_hash=unit2_hash,
                        loser_document_id=doc_a.id,
                        loser_version_id=ver_a.id,
                        loser_unit_id=unit_a.id,
                        loser_content_hash=unit_a.content_hash,
                        conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
                        status=ConflictResolutionStatus.PENDING_REVIEW.value,
                        resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
                        confidence=0.88,
                        reason="Doc 2 conflicts with A",
                    ),
                ]
            )
            await session.commit()

            src_a_id = src_a.id
            doc_a_id = doc_a.id
            qdrant_ok = httpx.AsyncClient(
                base_url="http://qdrant.test",
                transport=httpx.MockTransport(
                    lambda _req: httpx.Response(200, json={"status": "ok", "result": {"status": "completed"}})
                ),
            )
            try:
                fake_mgr = SimpleNamespace(
                    get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_ok),
                    release=AsyncMock(return_value=None),
                )
                await source_service.delete_source(
                    session,
                    src_a_id,
                    engine_manager=fake_mgr,
                    upload_dir=str(tmp_path / "uploads"),
                    job_queue=None,
                    qdrant_client=qdrant_ok,
                )
            finally:
                await qdrant_ok.aclose()

            session.expire_all()
            assert await session.get(Source, src_a_id) is None
            assert await session.get(Document, doc_a_id) is None
            assert await session.get(KnowledgeConflictResolution, "kcr-src-winner") is None
            assert await session.get(KnowledgeConflictResolution, "kcr-src-loser") is None
    finally:
        await sag_engine.dispose()


# ---------------------------------------------------------------------------
# Domain 4: Full E2E Business Scenario ("Doc 1 `12 ngày` vs Doc 2 `15 ngày`")
# ---------------------------------------------------------------------------


def _build_e2e_qdrant_client(
    *,
    collection: str,
    points: list[dict],
) -> httpx.AsyncClient:
    """Mock ONLY the external Qdrant HTTP transport for both conflict discovery and retrieval."""
    by_point_id = {pt["id"]: pt for pt in points}

    def _matches_filter(pt: dict, flt: dict | None) -> bool:
        if not isinstance(flt, dict):
            return True
        payload = pt.get("payload") or {}
        for cond in flt.get("must") or []:
            key = cond.get("key")
            match = cond.get("match") or {}
            val = payload.get(key)
            if "value" in match and val != match["value"]:
                return False
            if "any" in match and val not in set(match["any"]):
                return False
        for cond in flt.get("must_not") or []:
            key = cond.get("key")
            match = cond.get("match") or {}
            val = payload.get(key)
            if "value" in match and val == match["value"]:
                return False
            if "any" in match and val in set(match["any"]):
                return False
        return True

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        # Stage 2A vector fetch: POST /collections/{col}/points
        if request.method == "POST" and path == f"/collections/{collection}/points":
            ids = body.get("ids") or []
            found = [by_point_id[pid] for pid in ids if pid in by_point_id]
            return httpx.Response(200, json={"result": found})

        # Retrieval / Stage 2A batch search: POST /collections/{col}/points/query/batch
        if request.method == "POST" and path == f"/collections/{collection}/points/query/batch":
            batches = []
            for spec in body.get("searches") or []:
                flt = spec.get("filter")
                matched = [
                    {
                        "id": pt["id"],
                        "score": pt["score"],
                        "payload": pt["payload"],
                    }
                    for pt in points
                    if _matches_filter(pt, flt)
                ]
                batches.append({"points": matched})
            return httpx.Response(200, json={"result": batches})

        # Count check: POST /collections/{col}/points/count
        if request.method == "POST" and path == f"/collections/{collection}/points/count":
            flt = body.get("filter")
            count = sum(1 for pt in points if _matches_filter(pt, flt))
            return httpx.Response(200, json={"result": {"count": count}})

        # Single-point read (citations / hydration): GET /collections/{col}/points/{id}
        if request.method == "GET" and f"/collections/{collection}/points/" in path:
            pid = path.rsplit("/points/", 1)[-1]
            pt = by_point_id.get(pid)
            if pt is None:
                return httpx.Response(404, json={"status": {"error": "not found"}})
            return httpx.Response(200, json={"result": {"id": pt["id"], "payload": pt["payload"]}})

        raise AssertionError(f"Unexpected Qdrant HTTP request: {request.method} {path}")

    return httpx.AsyncClient(
        base_url="http://qdrant.test",
        transport=httpx.MockTransport(_handler),
    )


@pytest.mark.asyncio
async def test_domain4_e2e_vietnamese_leave_policy_12_vs_15_days_and_seam_a(
    p2e_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full end-to-end business lifecycle for Vietnamese HR annual leave policy:
    Doc 1 (`12 ngày`, 2024, T2) vs Doc 2 (`15 ngày`, 2025, T1), plus Seam A cross-partition
    isolation with Doc 3 (`20 ngày`, P_exec).
    """
    text_doc1 = "Nhân viên chính thức được hưởng 12 ngày phép năm."
    text_doc2 = "Nhân viên chính thức được hưởng 15 ngày phép năm."
    text_doc3 = "Nhân viên chính thức được hưởng 20 ngày phép năm theo nghị quyết điều hành."
    collection = f"search_units_{PROJECT_ID}"

    async with p2e_db() as session:
        rec1 = await _seed_ready_doc(
            session,
            tag="leave2024",
            filename="Chính sách nghỉ phép 2024.md",
            text_body=text_doc1,
            partition_id=P_PUBLIC,
            authority_tier=2,
            effective_from=T_2024_01,
            valid_from=T_2024_01,
            valid_to=VALID_TO_FOREVER,
        )
        rec2 = await _seed_ready_doc(
            session,
            tag="leave2025",
            filename="Chính sách nghỉ phép 2025.md",
            text_body=text_doc2,
            partition_id=P_PUBLIC,
            authority_tier=1,
            effective_from=T_2025_01,
            valid_from=T_2025_01,
            valid_to=VALID_TO_FOREVER,
        )
        src1: Source = rec1["source"]  # type: ignore[assignment]
        doc1: Document = rec1["doc"]  # type: ignore[assignment]
        ver1: DocumentVersion = rec1["ver"]  # type: ignore[assignment]
        unit1: SearchUnit = rec1["unit"]  # type: ignore[assignment]

        src2: Source = rec2["source"]  # type: ignore[assignment]
        doc2: Document = rec2["doc"]  # type: ignore[assignment]
        ver2: DocumentVersion = rec2["ver"]  # type: ignore[assignment]
        unit2: SearchUnit = rec2["unit"]  # type: ignore[assignment]

        conflict_job = Job(
            id="job-e2e-leave-2025",
            type=JobType.CONFLICT_RESOLUTION,
            source_id=src2.id,
            document_id=doc2.id,
            status=JobStatus.RUNNING,
            payload={
                "tenant_id": TENANT_ID,
                "project_id": PROJECT_ID,
                "document_id": doc2.id,
                "document_version_id": ver2.id,
                "security_partition_id": P_PUBLIC,
            },
        )
        session.add(conflict_job)
        await session.commit()

    def _make_pt(
        *,
        doc: Document,
        ver: DocumentVersion,
        unit: SearchUnit,
        partition_id: str,
        content: str,
        score: float,
        vec: list[float],
    ) -> dict:
        return {
            "id": generate_search_unit_point_id(collection, unit.id),
            "score": score,
            "vector": {"content_vector": vec},
            "payload": {
                "search_unit_id": unit.id,
                "document_id": doc.id,
                "document_version_id": ver.id,
                "project_id": PROJECT_ID,
                "tenant_id": TENANT_ID,
                "security_partition_id": partition_id,
                "content_hash": unit.content_hash,
                "content": content,
            },
        }

    qdrant_points = [
        _make_pt(
            doc=doc1,
            ver=ver1,
            unit=unit1,
            partition_id=P_PUBLIC,
            content=text_doc1,
            score=0.90,
            vec=[0.8, 0.2, 0.0],
        ),
        _make_pt(
            doc=doc2,
            ver=ver2,
            unit=unit2,
            partition_id=P_PUBLIC,
            content=text_doc2,
            score=0.92,
            vec=[0.82, 0.18, 0.0],
        ),
    ]

    def _llm_direct_supersedes(messages, _rf):
        user_msg = messages[-1]["content"]
        # Extract the exact pair_id embedded in PAIRS_JSON
        start = user_msg.index("PAIRS_JSON:\n") + len("PAIRS_JSON:\n")
        end = user_msg.index("\nEND_PAIRS_JSON")
        pairs = json.loads(user_msg[start:end])
        return json.dumps(
            {
                "items": [
                    {
                        "pair_id": item["pair_id"],
                        "classification": "DIRECT_SUPERSEDES",
                        "confidence": 0.94,
                        "winner": "NEW",
                        "scope_difference_summary": None,
                        "reason": "Chính sách nghỉ phép 2025 (15 ngày, T1) thay thế bản 2024 (12 ngày, T2).",
                    }
                    for item in pairs
                ]
            }
        )

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[0.8, 0.2, 0.0]))
    principal_pub = _make_principal({P_PUBLIC})
    sources_12 = [src1, src2]
    query_vi = "Nhân viên chính thức được hưởng bao nhiêu ngày phép năm?"

    async with _build_e2e_qdrant_client(collection=collection, points=qdrant_points) as qdrant:
        engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=qdrant))

        # Step 2: Run real `resolve_document_conflicts_service` for Doc 2 v1
        async with p2e_db() as session:
            job_row = await session.get(Job, conflict_job.id)
            assert job_row is not None
            conflict_res = await resolve_document_conflicts_service(
                session,
                job=job_row,
                qdrant_client=qdrant,
                llm=_FakeLLM(_llm_direct_supersedes),
                lease_session_factory=p2e_db,
                now_utc=T_2025_06,
            )
            await session.commit()

        assert conflict_res.degraded is False
        assert conflict_res.candidates_discovered == 1
        assert conflict_res.auto_superseded == 1

        async with p2e_db() as session:
            kcr = await session.scalar(
                select(KnowledgeConflictResolution).where(
                    KnowledgeConflictResolution.project_id == PROJECT_ID,
                    KnowledgeConflictResolution.winner_unit_id == unit2.id,
                    KnowledgeConflictResolution.loser_unit_id == unit1.id,
                )
            )
            assert kcr is not None
            assert kcr.status == ConflictResolutionStatus.ACTIVE.value
            assert kcr.resolution_mode == ConflictResolutionMode.AUTO_POLICY.value
            assert kcr.effective_valid_to == T_2025_01
            kcr_id = kcr.id

        # Step 3: `current_effective` (reference_time=2025-06-01) -> ONLY Doc 2 (`15 ngày`)
        out_curr = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            query_vi,
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_curr.sections] == [unit2.id]
        assert "15 ngày" in out_curr.sections[0].content
        assert out_curr.sections[0].is_superseded is False
        assert out_curr.sections[0].conflict_status is None

        # Step 4: `past_point` (as_of=2024-06-01) -> ONLY Doc 1 (`12 ngày`)
        out_past = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            "Năm 2024 nhân viên chính thức được hưởng bao nhiêu ngày phép năm?",
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                "Năm 2024 nhân viên chính thức được hưởng bao nhiêu ngày phép năm?",
                temporal_mode="past_point",
                as_of=T_2024_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_past.sections] == [unit1.id]
        assert "12 ngày" in out_past.sections[0].content
        assert out_past.sections[0].is_superseded is False
        assert out_past.sections[0].conflict_status is None

        # Step 5: `change_lineage` -> BOTH Doc 2 (`15 ngày`) and Doc 1 (`12 ngày`, byte-identical)
        out_lineage = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            "Chính sách ngày phép năm đã thay đổi như thế nào từ 2024 đến 2025?",
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                "Chính sách ngày phép năm đã thay đổi như thế nào từ 2024 đến 2025?",
                temporal_mode="change_lineage",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        lin_by_id = {s.search_unit_id: s for s in out_lineage.sections}
        assert set(lin_by_id.keys()) == {unit1.id, unit2.id}
        assert lin_by_id[unit2.id].content == text_doc2
        assert lin_by_id[unit2.id].is_superseded is False
        assert lin_by_id[unit1.id].is_superseded is True
        assert lin_by_id[unit1.id].conflict_status == "SUPERSEDED_BY_UNIT"
        assert lin_by_id[unit1.id].content == text_doc1
        assert _sha256(lin_by_id[unit1.id].content) == lin_by_id[unit1.id].content_hash

        # Step 6: `reopen_conflict` -> Doc 1 (`12 ngày`) IMMEDIATELY revives with ACTIVE_CONFLICT
        async with p2e_db() as session:
            reopened = await reopen_conflict(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                allowed_partition_ids={P_PUBLIC},
                resolution_id=kcr_id,
                reason="HR director reviewing transition rules",
                actor="hr-director",
            )
            assert reopened.status == ConflictResolutionStatus.PENDING_REVIEW.value

        out_reopened = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            query_vi,
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        reop_by_id = {s.search_unit_id: s for s in out_reopened.sections}
        assert set(reop_by_id.keys()) == {unit1.id, unit2.id}
        assert reop_by_id[unit1.id].conflict_status == "ACTIVE_CONFLICT"
        assert reop_by_id[unit1.id].is_superseded is False
        assert reop_by_id[unit2.id].conflict_status == "ACTIVE_CONFLICT"

        # Step 7: `resolve_conflict(WINNER_B)` (MANUAL_HITL) -> Doc 1 suppressed again
        async with p2e_db() as session:
            resolved = await resolve_conflict(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                allowed_partition_ids={P_PUBLIC},
                resolution_id=kcr_id,
                action="WINNER_B",
                reason="Confirmed 15 days applies to all permanent staff",
                actor="hr-director",
            )
            assert resolved.status == ConflictResolutionStatus.ACTIVE.value
            assert resolved.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value

        out_hitl = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            query_vi,
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_hitl.sections] == [unit2.id]

        # Step 8: `revoke_project_document` on Doc 2 -> Doc 1 (`12 ngày`) IMMEDIATELY revives
        # with zero ghost suppression and `conflict_status is None`
        async with p2e_db() as session:
            rev = await revoke_project_document(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                document_id=doc2.id,
                reason="WITHDRAWN_BY_LEGAL",
            )
            assert rev.status == "REVOKED"
            kcr_after_rev = await session.get(KnowledgeConflictResolution, kcr_id)
            assert kcr_after_rev is not None
            assert kcr_after_rev.status == ConflictResolutionStatus.REVOKED_WINNER.value

        out_after_revoke = await surs._retrieve_search_unit_sections(
            engine,
            sources_12,
            query_vi,
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_after_revoke.sections] == [unit1.id]
        assert out_after_revoke.sections[0].content == text_doc1
        assert out_after_revoke.sections[0].is_superseded is False
        assert out_after_revoke.sections[0].conflict_status is None

    # Step 9: Cross-partition Seam A E2E: Doc 3 (P_exec) supersedes Doc 1 (P_public)
    async with p2e_db() as session:
        rec3 = await _seed_ready_doc(
            session,
            tag="leaveexec",
            filename="Chính sách nghỉ phép điều hành 2025.md",
            text_body=text_doc3,
            partition_id=P_EXEC,
            authority_tier=1,
            effective_from=T_2025_01,
            valid_from=T_2025_01,
            valid_to=VALID_TO_FOREVER,
        )
        src3: Source = rec3["source"]  # type: ignore[assignment]
        doc3: Document = rec3["doc"]  # type: ignore[assignment]
        ver3: DocumentVersion = rec3["ver"]  # type: ignore[assignment]
        unit3: SearchUnit = rec3["unit"]  # type: ignore[assignment]

        session.add(
            KnowledgeConflictResolution(
                id="kcr-seam-a-e2e",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=P_PUBLIC,
                winner_security_partition_id=P_EXEC,
                pair_key=compute_conflict_pair_key(ver3.id, unit3.id, ver1.id, unit1.id),
                winner_document_id=doc3.id,
                winner_version_id=ver3.id,
                winner_unit_id=unit3.id,
                winner_content_hash=unit3.content_hash,
                loser_document_id=doc1.id,
                loser_version_id=ver1.id,
                loser_unit_id=unit1.id,
                loser_content_hash=unit1.content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
                confidence=0.96,
                effective_valid_to=T_2025_01,
                reason="Executive resolution overrides public leave policy",
            )
        )
        await session.commit()

    qdrant_points_seam_a = [
        *qdrant_points,
        _make_pt(
            doc=doc3,
            ver=ver3,
            unit=unit3,
            partition_id=P_EXEC,
            content=text_doc3,
            score=0.95,
            vec=[0.85, 0.15, 0.0],
        ),
    ]
    principal_exec = _make_principal({P_PUBLIC, P_EXEC})
    sources_123 = [src1, src2, src3]

    async with _build_e2e_qdrant_client(collection=collection, points=qdrant_points_seam_a) as qdrant_sa:
        engine_sa = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_sa))

        # {P_public}-only principal: STILL sees Doc 1 (`12 ngày`) with ZERO conflict/superseded leak!
        out_pub_only = await surs._retrieve_search_unit_sections(
            engine_sa,
            sources_123,
            query_vi,
            principal=principal_pub,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_pub_only.sections] == [unit1.id]
        assert out_pub_only.sections[0].content == text_doc1
        assert out_pub_only.sections[0].is_superseded is False
        assert out_pub_only.sections[0].conflict_status is None

        # Citation check for {P_public}-only: ZERO conflict_status or superseded_by_unit_id leak!
        async with p2e_db() as session:
            cite_pub = await surs.get_search_unit_citation(
                session,
                source=src1,
                principal=principal_pub,
                search_unit_id=unit1.id,
                engine_manager=engine_sa,
            )
            assert cite_pub is not None
            assert cite_pub["is_superseded"] is False
            assert cite_pub["conflict_status"] is None
            assert "superseded_by_unit_id" not in cite_pub

        # {P_public, P_exec} principal: sees Doc 3 (`20 ngày`) superseding Doc 1 (`12 ngày` suppressed)!
        out_with_exec = await surs._retrieve_search_unit_sections(
            engine_sa,
            sources_123,
            query_vi,
            principal=principal_exec,
            top_k=5,
            query_strategy_plan=plan_query(
                query_vi,
                temporal_mode="current_effective",
                reference_time=T_2025_06,
            ),
            candidate_limit=10,
            deadline=asyncio.get_running_loop().time() + 5.0,
        )
        assert [s.search_unit_id for s in out_with_exec.sections] == [unit3.id]
        assert "20 ngày" in out_with_exec.sections[0].content

        # Citation check for {P_public, P_exec}: sees unit1 marked SUPERSEDED_BY_UNIT -> unit3
        async with p2e_db() as session:
            cite_exec = await surs.get_search_unit_citation(
                session,
                source=src1,
                principal=principal_exec,
                search_unit_id=unit1.id,
                engine_manager=engine_sa,
            )
            assert cite_exec is not None
            assert cite_exec["is_superseded"] is True
            assert cite_exec["conflict_status"] == "SUPERSEDED_BY_UNIT"
            assert cite_exec["superseded_by_unit_id"] == unit3.id
