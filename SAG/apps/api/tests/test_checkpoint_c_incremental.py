"""Comprehensive Test Suite for Phase 8 / Checkpoint C (INCREMENTAL_READY).

Covers all 7 mandatory acceptance test categories:
- TEST-C1: Delta assignment during normal ingest without full rebuild.
- TEST-C2: Drift monitoring with hysteresis and targeted subtree rebuild with stable lineage.
- TEST-C3: Dual-slot inactive build & Qdrant payload batch updates.
- TEST-C4: Manifest, checksum, and quality gates verification (fail-closed).
- TEST-C5: Atomic pointer switch in a single PostgreSQL transaction.
- TEST-C6: Query-time snapshot consistency (no mixed slot reads during publish).
- TEST-C7: Fault injection resilience & instant zero-downtime rollback.
- TEST-C8: End-to-end integration with EngineManager.get_routing_snapshot().
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from sag_api.core.config import Settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models.routing_rag import ProjectSearchState, TreeManifest
from sag_api.sag.engine_manager import EngineManager
from sag_api.services import incremental_tree_service as incremental_service
from sag_api.services.incremental_tree_service import (
    T_HIGH,
    DriftReport,
    assign_delta_units,
    build_inactive_slot_payloads,
    build_query_routing_snapshot,
    compute_tree_drift,
    coordinate_ingest_delta,
    execute_atomic_tree_publish,
    execute_tree_rollback,
    get_or_create_project_state,
    rebuild_drifted_subtree,
    update_inactive_slot_qdrant_payloads,
    verify_inactive_slot_manifest,
)
from sag_api.services.query_routing_service import (
    route_snapshot,
)
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    RoutingBenchmarkCase,
    TreeBuildConfig,
    build_routing_snapshot,
)
from sag_api.services.routing_tree_service import (
    RoutingSnapshot as TreeRoutingSnapshot,
)
from sag_api.services.tree_publish_service import TreePublishError


def _base_units() -> list[KnowledgeUnitInput]:
    """Create 8 deterministic units across 2 partitions."""
    return [
        KnowledgeUnitInput(
            unit_id=f"ku-{i}",
            tenant_id="tenant-alpha",
            project_id="proj-delta",
            security_partition_id="part-tech" if i < 4 else "part-hr",
            dense=(1.0 if i < 4 else 0.0, 0.0 if i < 4 else 1.0, 0.5),
            sparse=(("common", 1.0), ("tech" if i < 4 else "hr", 2.0)),
            entities=("Company", "TechCorp" if i < 4 else "HRCorp"),
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2026, 12, 31, tzinfo=UTC),
        )
        for i in range(8)
    ]


def _base_edges() -> list[KnowledgeEdgeInput]:
    return [
        KnowledgeEdgeInput(f"ku-{i}", f"ku-{i + 1}", 0.8)
        for i in range(3)  # Tech cluster
    ] + [
        KnowledgeEdgeInput(f"ku-{i}", f"ku-{i + 1}", 0.8)
        for i in range(4, 7)  # HR cluster
    ]


def _base_benchmark() -> list[RoutingBenchmarkCase]:
    return [
        RoutingBenchmarkCase("q-tech", "ku-0", ("ku-0", "ku-1")),
        RoutingBenchmarkCase("q-hr", "ku-4", ("ku-4", "ku-5")),
    ]


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


# ---------------------------------------------------------------------------
# TEST-C1: Delta Assignment without Full Rebuild
# ---------------------------------------------------------------------------


def test_incremental_normal_ingest_updates_delta_without_full_rebuild():
    """Verify that normal ingest assigns new units to existing leaf nodes without changing tree structure."""
    units = _base_units()
    edges = _base_edges()
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    base_snapshot = build_routing_snapshot(units, edges, config=config)

    # 1. New unit highly similar to part-tech cluster
    similar_unit = KnowledgeUnitInput(
        unit_id="ku-new-tech",
        tenant_id="tenant-alpha",
        project_id="proj-delta",
        security_partition_id="part-tech",
        dense=(1.0, 0.0, 0.5),  # Identical direction to tech medoid
        sparse=(("common", 1.0), ("python", 2.0)),
        entities=("Company", "Python"),
        valid_from=datetime(2026, 5, 1, tzinfo=UTC),
        valid_to=datetime(2026, 12, 31, tzinfo=UTC),
    )

    # 2. Borderline unit
    borderline_unit = KnowledgeUnitInput(
        unit_id="ku-borderline",
        tenant_id="tenant-alpha",
        project_id="proj-delta",
        security_partition_id="part-tech",
        dense=(0.6, 0.5, 0.2),  # Intermediate similarity
        sparse=(("common", 0.5),),
        entities=("General",),
    )

    # 3. Outlier unit (different direction, below T_LOW)
    outlier_unit = KnowledgeUnitInput(
        unit_id="ku-outlier",
        tenant_id="tenant-alpha",
        project_id="proj-delta",
        security_partition_id="part-tech",
        dense=(-1.0, -1.0, 0.0),  # Negative correlation
        sparse=(),
        entities=(),
    )

    delta_result = assign_delta_units(base_snapshot, [similar_unit, borderline_unit, outlier_unit], config)

    # Verify classification thresholds
    assert delta_result.direct_count >= 1
    tech_assignment = next(a for a in delta_result.assignments if a.unit_id == "ku-new-tech")
    assert tech_assignment.assignment_type == "DIRECT"
    assert tech_assignment.score >= T_HIGH
    assert tech_assignment.target_node_id is not None

    border_assignment = next(a for a in delta_result.assignments if a.unit_id == "ku-borderline")
    assert border_assignment.assignment_type in {"BORDERLINE", "DIRECT"}

    outlier_assignment = next(a for a in delta_result.assignments if a.unit_id == "ku-outlier")
    assert outlier_assignment.assignment_type == "OUTLIER"

    # Verify ancestor statistics update without full tree rebuild
    base_leaves = [n for n in base_snapshot.roots if not n.children]
    updated_leaves = [n for n in delta_result.updated_roots if not n.children]

    # Node count and IDs remain unchanged
    assert len(base_snapshot.roots) == len(delta_result.updated_roots)
    assert {n.node_id for n in base_leaves} == {n.node_id for n in updated_leaves}

    # Leaf receiving the direct unit increased unit count
    target_leaf = next(n for n in updated_leaves if n.node_id == tech_assignment.target_node_id)
    assert "ku-new-tech" in target_leaf.unit_ids
    assert target_leaf.profile.accessible_unit_count > len(units) // 2


# ---------------------------------------------------------------------------
# TEST-C2: Drift Detection, Hysteresis & Subtree Rebuild with Lineage
# ---------------------------------------------------------------------------


def test_drift_detection_and_hysteresis_triggers_subtree_rebuild():
    """Verify that drift metrics require hysteresis (>= 2 violations) to trigger rebuild."""
    units = _base_units()
    edges = _base_edges()
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=6)
    base_snapshot = build_routing_snapshot(units, edges, config=config)

    # Introduce drifting/overflow units
    drifting_units = [
        KnowledgeUnitInput(
            unit_id=f"ku-drift-{i}",
            tenant_id="tenant-alpha",
            project_id="proj-delta",
            security_partition_id="part-tech",
            dense=(1.0, 0.0, 0.5),
            sparse=(("ai", 1.0),),
            entities=("AI",),
        )
        for i in range(5)
    ]
    delta_result = assign_delta_units(base_snapshot, drifting_units, config)

    # First violation window: should NOT trigger rebuild (hysteresis = 1 < 2)
    report_win1 = compute_tree_drift(base_snapshot, delta_result, config, history_window_violations=0)
    assert report_win1.capacity_overflow or report_win1.centroid_drift > 0
    assert report_win1.violation_count == 1
    assert not report_win1.trigger_rebuild

    # Second consecutive violation window: triggers rebuild!
    report_win2 = compute_tree_drift(base_snapshot, delta_result, config, history_window_violations=1)
    assert report_win2.violation_count == 2
    assert report_win2.trigger_rebuild
    assert len(report_win2.affected_node_ids) > 0

    # Specifically test centroid drift via angular shift without capacity overflow
    config_large = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=20)
    shifted_units = [
        KnowledgeUnitInput(
            unit_id=f"ku-shift-{i}",
            tenant_id="tenant-alpha",
            project_id="proj-delta",
            security_partition_id="part-tech",
            dense=(0.85, 0.55, 0.5),  # Shifts centroid
            sparse=(("common", 1.0), ("tech", 2.0)),
            entities=("Company", "TechCorp"),
        )
        for i in range(4)
    ]
    shift_result = assign_delta_units(base_snapshot, shifted_units, config_large)
    shift_report = compute_tree_drift(base_snapshot, shift_result, config_large, history_window_violations=0)
    assert not shift_report.capacity_overflow
    assert shift_report.centroid_drift > 0.0


def test_targeted_subtree_rebuild_and_stable_node_lineage():
    """Verify that rebuilding drifted subtree inherits node_id when overlap >= 0.70."""
    units = _base_units()
    edges = _base_edges()
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    base_snapshot = build_routing_snapshot(units, edges, config=config, benchmark=_base_benchmark())

    # Rebuild with 1 additional unit (high overlap)
    additional_unit = KnowledgeUnitInput(
        unit_id="ku-add-1",
        tenant_id="tenant-alpha",
        project_id="proj-delta",
        security_partition_id="part-tech",
        dense=(1.0, 0.0, 0.5),
        sparse=(("tech", 2.0),),
        entities=("Company", "TechCorp"),
    )
    all_units = units + [additional_unit]
    all_edges = edges + [KnowledgeEdgeInput("ku-0", "ku-add-1", 0.9)]

    tech_root = next(r for r in base_snapshot.roots if r.security_partition_id == "part-tech")
    hr_root_before = next(r for r in base_snapshot.roots if r.security_partition_id == "part-hr")

    rebuilt_snapshot = rebuild_drifted_subtree(
        base_snapshot,
        affected_node_id=tech_root.node_id,
        all_units=all_units,
        all_edges=all_edges,
        config=config,
    )

    assert rebuilt_snapshot.publishable
    assert rebuilt_snapshot.tree_version != base_snapshot.tree_version

    # Verify that unaffected partition (part-hr) remains 100% untouched
    hr_root_after = next(r for r in rebuilt_snapshot.roots if r.security_partition_id == "part-hr")
    assert hr_root_before == hr_root_after
    assert hr_root_before.node_id == hr_root_after.node_id
    assert hr_root_before.unit_ids == hr_root_after.unit_ids

    # Check that stable node ID was preserved via lineage mapping
    old_ids = {n.node_id for n in base_snapshot.roots}
    new_ids = {n.node_id for n in rebuilt_snapshot.roots}
    assert len(old_ids & new_ids) > 0  # At least one node ID was stably inherited!

    manifest = rebuilt_snapshot.manifest
    assert "lineage_events" in manifest
    assert any(ev.get("type") == "SUPERSEDES_TREE_NODE" for ev in manifest["lineage_events"])


# ---------------------------------------------------------------------------
# TEST-C3: Dual-Slot Inactive Build & Qdrant Payload Batch Update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_slot_inactive_build_and_qdrant_payload_update():
    """Verify building payloads for inactive Slot B without modifying active Slot A."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges)

    collection = "search_units_proj_delta"
    batches = build_inactive_slot_payloads(snapshot, target_slot="SLOT_B", collection_name=collection)

    assert len(batches) > 0
    for batch in batches:
        payload = batch["payload"]
        assert "primary_node_b" in payload
        assert "secondary_node_ids_b" in payload
        assert payload["tree_version_b"] == snapshot.tree_version
        assert "primary_node_a" not in payload  # Does not touch Slot A!

    # Test update with mock Qdrant client
    received_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        return httpx.Response(200, json={"result": {"status": "completed"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock-qdrant:6333")
    success = await update_inactive_slot_qdrant_payloads(client, collection, batches)
    await client.aclose()

    assert success
    assert len(received_requests) == len(batches)


# ---------------------------------------------------------------------------
# TEST-C4: Manifest Verification Gate (Fail-Closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_verification_gate_rejects_corrupted_or_regressed_tree():
    """Verify that corrupt checksum or mismatched point count fails verification before publish."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges, benchmark=_base_benchmark())

    # 1. Valid snapshot passes
    valid, reason = await verify_inactive_slot_manifest(snapshot, target_slot="SLOT_B")
    assert valid
    assert reason == "VERIFIED"

    # 2. Corrupted checksum fails
    corrupted_manifest = dict(snapshot.manifest)
    corrupted_manifest["checksum"] = "bad" * 21 + "a"
    corrupted_snapshot = TreeRoutingSnapshot(
        contract_version=snapshot.contract_version,
        tree_version=snapshot.tree_version,
        tenant_id=snapshot.tenant_id,
        project_id=snapshot.project_id,
        config_version=snapshot.config_version,
        roots=snapshot.roots,
        metrics=snapshot.metrics,
        quality_gates=snapshot.quality_gates,
        publishable=snapshot.publishable,
        manifest_json=json.dumps(corrupted_manifest),
        lineage=snapshot.lineage,
        partition_algorithm=snapshot.partition_algorithm,
    )
    valid_corrupt, reason_corrupt = await verify_inactive_slot_manifest(
        corrupted_snapshot, target_slot="SLOT_B"
    )
    assert not valid_corrupt
    assert reason_corrupt == "checksum_mismatch"

    # 3. Point count mismatch in Qdrant fails
    def count_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"count": 2}})  # Expected 8, got 2

    client = httpx.AsyncClient(transport=httpx.MockTransport(count_handler), base_url="http://mock-qdrant:6333")
    valid_count, reason_count = await verify_inactive_slot_manifest(
        snapshot, target_slot="SLOT_B", qdrant_client=client, collection_name="test_col"
    )
    await client.aclose()

    assert not valid_count
    assert "point_count_mismatch" in reason_count

    # 4. Missing checksum fails (strict fail-closed)
    missing_cs_manifest = dict(snapshot.manifest)
    missing_cs_manifest.pop("checksum", None)
    missing_cs_snapshot = TreeRoutingSnapshot(
        contract_version=snapshot.contract_version,
        tree_version=snapshot.tree_version,
        tenant_id=snapshot.tenant_id,
        project_id=snapshot.project_id,
        config_version=snapshot.config_version,
        roots=snapshot.roots,
        metrics=snapshot.metrics,
        quality_gates=snapshot.quality_gates,
        publishable=snapshot.publishable,
        manifest_json=json.dumps(missing_cs_manifest),
        lineage=snapshot.lineage,
        partition_algorithm=snapshot.partition_algorithm,
    )
    valid_missing, reason_missing = await verify_inactive_slot_manifest(
        missing_cs_snapshot, target_slot="SLOT_B"
    )
    assert not valid_missing
    assert reason_missing == "checksum_mismatch"


# ---------------------------------------------------------------------------
# TEST-C5: Atomic Pointer Switch in Single PostgreSQL Transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_pointer_switch_in_single_postgres_transaction():
    """Verify atomic pointer switch and epoch increment in one transaction."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges)
    project_id = "proj-c5"

    async with SessionLocal() as session:
        # Initial state setup
        state = await get_or_create_project_state(session, project_id)
        assert state.active_routing_slot == "SLOT_A"
        assert state.active_search_epoch == 1

        # Perform atomic publish to SLOT_B
        updated_state = await execute_atomic_tree_publish(session, project_id, snapshot, "SLOT_B")

        assert updated_state.active_routing_slot == "SLOT_B"
        assert updated_state.active_tree_version == snapshot.tree_version
        assert updated_state.active_search_epoch == 2
        assert updated_state.slot_b_tree_version == snapshot.tree_version

        # Verify Manifest table recorded status ACTIVE
        m_stmt = select(TreeManifest).where(TreeManifest.tree_version == snapshot.tree_version)
        m_res = await session.execute(m_stmt)
        manifest_rec = m_res.scalar_one_or_none()
        assert manifest_rec is not None
        assert manifest_rec.status == "ACTIVE"
        assert manifest_rec.node_count == len(snapshot.lineage)


# ---------------------------------------------------------------------------
# TEST-C6: Concurrent Query Snapshot Consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_queries_read_isolated_consistent_snapshot():
    """Verify that queries pin snapshot at request start and never read mixed slots during publish."""
    units = _base_units()
    edges = _base_edges()
    snapshot_a = build_routing_snapshot(units[:4], edges[:2])
    snapshot_b = build_routing_snapshot(units, edges)
    project_id = "proj-c6"

    async with SessionLocal() as session:
        # Initially active: Snapshot A in Slot A
        await execute_atomic_tree_publish(session, project_id, snapshot_a, "SLOT_A")

        state_res = await session.execute(
            select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
        )
        state_before = state_res.scalar_one()
        m_res = await session.execute(
            select(TreeManifest).where(TreeManifest.tree_version == snapshot_a.tree_version)
        )
        manifest_before = m_res.scalar_one()

        scopes = [{
            "project_id": project_id,
            "source_ids": ["src-1"],
            "document_version_ids": ["ver-1"],
            "tenant_id": "tenant-alpha",
            "partition_id": "part-tech",
        }]

        # Query 1 captures snapshot A
        query_snap_1 = build_query_routing_snapshot(state_before, manifest_before, scopes)
        decision_1 = route_snapshot(query_snap_1.groups[0])
        assert decision_1.routing_slot == "SLOT_A"
        assert decision_1.tree_version == snapshot_a.tree_version

        # Now, Publish process switches active state to Snapshot B in Slot B
        await execute_atomic_tree_publish(session, project_id, snapshot_b, "SLOT_B")

        # In-flight Query 1 STILL evaluates to Slot A without contamination
        decision_1_continued = route_snapshot(query_snap_1.groups[0])
        assert decision_1_continued.routing_slot == "SLOT_A"
        assert decision_1_continued.tree_version == snapshot_a.tree_version

        # Query 2 started AFTER publish reads new active state (Slot B)
        state_res2 = await session.execute(
            select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
        )
        state_after = state_res2.scalar_one()
        m_res2 = await session.execute(
            select(TreeManifest).where(TreeManifest.tree_version == snapshot_b.tree_version)
        )
        manifest_after = m_res2.scalar_one()

        query_snap_2 = build_query_routing_snapshot(state_after, manifest_after, scopes)
        decision_2 = route_snapshot(query_snap_2.groups[0])
        assert decision_2.routing_slot == "SLOT_B"
        assert decision_2.tree_version == snapshot_b.tree_version


# ---------------------------------------------------------------------------
# TEST-C7: Fault Injection & Rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_pg_only_rollback_flips_previous_pointer():
    """Exercise the legacy DB-only helper; cross-store rollback is tested elsewhere."""
    units = _base_units()
    edges = _base_edges()
    snapshot_v1 = build_routing_snapshot(units[:4], edges[:2])
    snapshot_v2 = build_routing_snapshot(units, edges)
    project_id = "proj-c7"

    async with SessionLocal() as session:
        # Establish stable active V1 in SLOT_A
        await execute_atomic_tree_publish(session, project_id, snapshot_v1, "SLOT_A")

        # Fault Injection: Network failure writing V2 to Qdrant inactive slot
        def failing_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Qdrant connection timeout")

        failing_client = httpx.AsyncClient(
            transport=httpx.MockTransport(failing_handler), base_url="http://mock-qdrant:6333"
        )
        batches = build_inactive_slot_payloads(snapshot_v2, "SLOT_B", "test_col")
        update_ok = await update_inactive_slot_qdrant_payloads(failing_client, "test_col", batches)
        await failing_client.aclose()

        assert not update_ok  # Write failed!

        # Because write failed, we do NOT call execute_atomic_tree_publish
        # Verify active tree in DB is still untouched V1 on SLOT_A
        state_res = await session.execute(
            select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
        )
        state = state_res.scalar_one()
        assert state.active_tree_version == snapshot_v1.tree_version
        assert state.active_routing_slot == "SLOT_A"

        # Now test Rollback flow after successful publish
        # 1. Publish V2
        await execute_atomic_tree_publish(session, project_id, snapshot_v2, "SLOT_B")
        state_v2 = (
            await session.execute(
                select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
            )
        ).scalar_one()
        assert state_v2.active_tree_version == snapshot_v2.tree_version
        assert state_v2.previous_tree_version == snapshot_v1.tree_version

        # 2. Trigger Rollback
        rolled_back_state = await execute_tree_rollback(session, project_id)
        assert rolled_back_state.active_tree_version == snapshot_v1.tree_version
        assert rolled_back_state.active_routing_slot == "SLOT_A"
        assert rolled_back_state.active_search_epoch == 4
        assert rolled_back_state.slot_a_tree_version == snapshot_v1.tree_version
        assert rolled_back_state.previous_tree_version is None

        # Check manifest statuses
        m1 = (
            await session.execute(
                select(TreeManifest).where(TreeManifest.tree_version == snapshot_v1.tree_version)
            )
        ).scalar_one()
        m2 = (
            await session.execute(
                select(TreeManifest).where(TreeManifest.tree_version == snapshot_v2.tree_version)
            )
        ).scalar_one()
        assert m1.status == "ACTIVE"
        assert m2.status == "INACTIVE"

        # 3. Attempting double rollback fails because previous_tree_version was consumed
        with pytest.raises(ValueError, match="Cannot rollback"):
            await execute_tree_rollback(session, project_id)


# ---------------------------------------------------------------------------
# TEST-C8: EngineManager Integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_manager_get_routing_snapshot_integration():
    """Verify that EngineManager.get_routing_snapshot() integrates with capture_routing_decisions()."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges)
    project_id = "proj-c8"

    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, project_id, snapshot, "SLOT_A")

    settings = Settings()
    engine_manager = EngineManager(settings)

    scopes = [{
        "project_id": project_id,
        "source_ids": ["src-1"],
        "document_version_ids": ["ver-1"],
        "tenant_id": "tenant-alpha",
        "partition_id": "part-tech",
    }]

    # Call EngineManager.get_routing_snapshot()
    captured_snapshot = await engine_manager.get_routing_snapshot(
        query="Tìm kiếm thông tin tech",
        scopes=scopes,
        planner={"strategy": "EXACT"},
    )

    assert captured_snapshot is not None
    assert len(captured_snapshot.groups) == 1
    group = captured_snapshot.groups[0]
    assert group.project_id == project_id
    assert group.routing_slot == "SLOT_A"
    assert group.tree_version == snapshot.tree_version
    assert group.manifest_status == "ACTIVE"
    assert group.manifest_verified

    # Pass to query routing
    decision = route_snapshot(group)
    assert decision.routed
    assert decision.routing_slot == "SLOT_A"
    assert decision.tree_version == snapshot.tree_version
    assert len(decision.membership_node_ids) > 0


@pytest.mark.asyncio
async def test_hierarchical_multi_depth_tree_routing_snapshot_integration():
    """Verify that multi-depth trees with root and child nodes correctly compute is_leaf and route without error."""
    units = _base_units()
    edges = _base_edges()
    # Configure tight cluster sizes to force Leiden to create multi-depth hierarchy (roots have children)
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=2, max_cluster_size=4)
    snapshot = build_routing_snapshot(units, edges, config=config)
    assert any(len(root.children) > 0 for root in snapshot.roots)

    project_id = "proj-c9-hierarchical"
    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, project_id, snapshot, "SLOT_A")

    settings = Settings()
    engine_manager = EngineManager(settings)
    scopes = [{
        "project_id": project_id,
        "source_ids": ["src-1"],
        "document_version_ids": ["ver-1"],
        "tenant_id": "tenant-alpha",
        "partition_id": "part-tech",
    }]

    captured_snapshot = await engine_manager.get_routing_snapshot(
        query="Tìm kiếm đa tầng",
        scopes=scopes,
        planner={"strategy": "LOCAL_FACTUAL"},
    )
    assert captured_snapshot is not None
    group = captured_snapshot.groups[0]

    # Verify is_leaf is False for parent roots, True for child leaves
    profiles_by_id = {p.node_id: p for p in group.profiles}
    parent_ids = {p.parent_id for p in group.profiles if p.parent_id}
    for p_id in parent_ids:
        if p_id in profiles_by_id:
            assert not profiles_by_id[p_id].is_leaf

    # Routing must succeed without "Leaf routing profile has children" or "invalid_profile_topology" error
    decision = route_snapshot(group)
    assert decision.routed
    assert decision.fallback_reason is None
    assert len(decision.membership_node_ids) > 0


# ---------------------------------------------------------------------------
# TEST-C10: Fail-Closed Verification & Unverified Publish Rejection (Point 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_closed_qdrant_verification_and_unverified_publish_rejected():
    """Verify fail-closed invariant: Qdrant verification is required and unverified publish is rejected."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges, benchmark=_base_benchmark())

    # 1. Verification with require_qdrant=True fails if client/collection missing
    valid, reason = await verify_inactive_slot_manifest(
        snapshot, target_slot="SLOT_B", require_qdrant=True
    )
    assert not valid
    assert reason == "missing_qdrant_verification_inputs"

    # 2. execute_atomic_tree_publish() strictly rejects unverified publish
    async with SessionLocal() as session:
        with pytest.raises(TreePublishError, match="Cannot publish unverified tree snapshot"):
            await execute_atomic_tree_publish(
                session, "proj-fail-closed", snapshot, "SLOT_A", verified=False
            )


# ---------------------------------------------------------------------------
# TEST-C11: Runtime Ingestion Pipeline Coordinator (Point 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinate_ingest_delta_runtime_pipeline():
    """Verify runtime ingestion coordinator links delta, drift check, Qdrant update, verification, and publish."""
    project_id = "proj-runtime-delta"
    units = [replace(unit, project_id=project_id) for unit in _base_units()]
    edges = _base_edges()
    base_snapshot = build_routing_snapshot(units, edges, benchmark=_base_benchmark())
    collection = "search_units_runtime_test"

    # Initial publish of base tree
    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, project_id, base_snapshot, "SLOT_A")

    # Ingest a delta unit into tech cluster
    delta_unit = KnowledgeUnitInput(
        unit_id="ku-delta-runtime-1",
        tenant_id="tenant-alpha",
        project_id=project_id,
        security_partition_id="part-tech",
        dense=(1.0, 0.0, 0.5),
        sparse=(("tech", 2.0), ("runtime", 1.5)),
        entities=("TechCorp", "RuntimeService"),
    )

    # Mock Qdrant responding to payload update and point count
    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/payload"):
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": 9}})
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(qdrant_handler), base_url="http://mock-qdrant:6333"
    )

    async with SessionLocal() as session:
        result = await coordinate_ingest_delta(
            session,
            project_id=project_id,
            base_snapshot=base_snapshot,
            new_units=[delta_unit],
            qdrant_client=client,
            collection_name=collection,
        )

    await client.aclose()

    assert result.project_id == project_id
    assert result.target_slot == "SLOT_B"  # Switched from initial SLOT_A to SLOT_B!
    assert result.verified is True
    assert result.action_taken in {"DELTA_ASSIGNED", "SUBTREE_REBUILT"}
    assert result.new_search_epoch == 3

    # Verify PostgreSQL state was atomically updated
    async with SessionLocal() as session:
        state = await get_or_create_project_state(session, project_id)
        assert state.active_routing_slot == "SLOT_B"
        assert state.active_tree_version == result.tree_version
        assert state.active_search_epoch == 3


# ---------------------------------------------------------------------------
# TEST-C12: Query-Dependent Signal Scores & Branch Routing (Point 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_dependent_routing_signal_scores():
    """Verify that query routing dynamically computes signal scores reflecting query tokens."""
    units = _base_units()
    edges = _base_edges()
    snapshot = build_routing_snapshot(units, edges, benchmark=_base_benchmark())
    project_id = "proj-c12-query-signals"

    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, project_id, snapshot, "SLOT_A")

    settings = Settings()
    engine_manager = EngineManager(settings)

    tech_scope = [{
        "project_id": project_id,
        "source_ids": ["src-1"],
        "document_version_ids": ["ver-1"],
        "tenant_id": "tenant-alpha",
        "partition_id": "part-tech",
    }]
    hr_scope = [{
        "project_id": project_id,
        "source_ids": ["src-1"],
        "document_version_ids": ["ver-1"],
        "tenant_id": "tenant-alpha",
        "partition_id": "part-hr",
    }]

    # Query targeting Tech
    tech_snap = await engine_manager.get_routing_snapshot(
        query="Tìm kiếm thông tin tech công nghệ",
        scopes=tech_scope,
        planner={"strategy": "EXACT"},
    )
    assert tech_snap is not None
    tech_group = tech_snap.groups[0]
    tech_decision = route_snapshot(tech_group)
    assert tech_decision.routed
    assert len(tech_decision.membership_node_ids) > 0

    # Verify tech node has non-zero sparse score for query 'tech'
    tech_profile = next(p for p in tech_group.profiles if p.partition_id == "part-tech")
    assert tech_profile.signal_scores.get("sparse", 0.0) > 0.0

    # Query targeting HR
    hr_snap = await engine_manager.get_routing_snapshot(
        query="Tìm kiếm nhân sự hr tuyển dụng",
        scopes=hr_scope,
        planner={"strategy": "EXACT"},
    )
    assert hr_snap is not None
    hr_group = hr_snap.groups[0]
    hr_decision = route_snapshot(hr_group)
    assert hr_decision.routed
    assert len(hr_decision.membership_node_ids) > 0

    hr_profile = next(p for p in hr_group.profiles if p.partition_id == "part-hr")
    assert hr_profile.signal_scores.get("sparse", 0.0) > 0.0


# ---------------------------------------------------------------------------
# TEST-C13: Multi-Project Scope Isolation (Point 5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_project_routing_snapshots_isolation():
    """Verify that multi-project requests capture isolated snapshots per project."""
    units_a = _base_units()
    edges_a = _base_edges()
    snapshot_a = build_routing_snapshot(units_a, edges_a, benchmark=_base_benchmark())

    # Create distinct units for project B
    units_b = [
        KnowledgeUnitInput(
            unit_id=f"ku-b-{i}",
            tenant_id="tenant-beta",
            project_id="proj-multi-b",
            security_partition_id="part-finance",
            dense=(0.5, 0.5, float(i) * 0.1),
            sparse=(("finance", 2.0), ("audit", 1.0)),
            entities=("FinanceCorp", "AuditUnit"),
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2026, 12, 31, tzinfo=UTC),
        )
        for i in range(4)
    ]
    edges_b = [KnowledgeEdgeInput(f"ku-b-{i}", f"ku-b-{i + 1}", 0.75) for i in range(3)]
    snapshot_b = build_routing_snapshot(units_b, edges_b)

    proj_a = "proj-multi-a"
    proj_b = "proj-multi-b"

    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, proj_a, snapshot_a, "SLOT_A")
        await execute_atomic_tree_publish(session, proj_b, snapshot_b, "SLOT_B")

    settings = Settings()
    engine_manager = EngineManager(settings)

    multi_scopes = [
        {
            "project_id": proj_a,
            "source_ids": ["src-1"],
            "document_version_ids": ["ver-1"],
            "tenant_id": "tenant-alpha",
            "partition_id": "part-tech",
        },
        {
            "project_id": proj_b,
            "source_ids": ["src-1"],
            "document_version_ids": ["ver-1"],
            "tenant_id": "tenant-beta",
            "partition_id": "part-finance",
        },
    ]

    captured = await engine_manager.get_routing_snapshot(
        query="Kiểm toán tài chính và công nghệ tech",
        scopes=multi_scopes,
        planner={"strategy": "EXACT"},
    )

    assert captured is not None
    assert len(captured.groups) == 2

    # Group 1 belongs to proj_a in SLOT_A
    group_a = next(g for g in captured.groups if g.project_id == proj_a)
    assert group_a.routing_slot == "SLOT_A"
    assert group_a.tree_version == snapshot_a.tree_version
    assert group_a.tenant_id == "tenant-alpha"
    assert group_a.partition_id == "part-tech"

    # Group 2 belongs to proj_b in SLOT_B
    group_b = next(g for g in captured.groups if g.project_id == proj_b)
    assert group_b.routing_slot == "SLOT_B"
    assert group_b.tree_version == snapshot_b.tree_version
    assert group_b.tenant_id == "tenant-beta"
    assert group_b.partition_id == "part-finance"


@pytest.mark.asyncio
async def test_subtree_build_failure_keeps_previous_tree_serving(monkeypatch):
    project_id = "proj-subtree-build-failure"
    units = [replace(unit, project_id=project_id) for unit in _base_units()]
    base_snapshot = build_routing_snapshot(units, _base_edges(), benchmark=_base_benchmark())
    affected_node = base_snapshot.roots[0].node_id

    async with SessionLocal() as session:
        await execute_atomic_tree_publish(session, project_id, base_snapshot, "SLOT_A")

    async with SessionLocal() as session:
        state_before = await session.get(ProjectSearchState, project_id)
        manifest_before = await session.get(TreeManifest, base_snapshot.tree_version)
        assert state_before is not None and manifest_before is not None
        epoch_before = state_before.active_search_epoch
        pinned_before = build_query_routing_snapshot(
            state_before,
            manifest_before,
            [
                {
                    "project_id": project_id,
                    "source_ids": ["src-1"],
                    "document_version_ids": ["ver-1"],
                    "tenant_id": "tenant-alpha",
                    "partition_id": "part-tech",
                }
            ],
        )

    monkeypatch.setattr(
        incremental_service,
        "compute_tree_drift",
        lambda *_args, **_kwargs: DriftReport(1.0, 1.0, False, (), 1, True, (affected_node,)),
    )

    def fail_subtree_build(*_args, **_kwargs):
        raise RuntimeError("injected subtree build failure")

    monkeypatch.setattr(incremental_service, "rebuild_drifted_subtree", fail_subtree_build)
    qdrant_requests = []

    def record_qdrant_request(request):
        qdrant_requests.append(request)
        return httpx.Response(500)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(record_qdrant_request),
        base_url="http://mock-qdrant:6333",
    )
    delta = replace(units[0], unit_id="ku-subtree-failure-delta")
    with pytest.raises(RuntimeError, match="injected subtree build failure"):
        async with SessionLocal() as session:
            await coordinate_ingest_delta(
                session,
                project_id=project_id,
                base_snapshot=base_snapshot,
                new_units=[delta],
                qdrant_client=client,
                collection_name="search_units_subtree_build_failure",
            )
    await client.aclose()

    async with SessionLocal() as session:
        state_after = await session.get(ProjectSearchState, project_id)
        manifest_after = await session.get(TreeManifest, base_snapshot.tree_version)
        assert state_after is not None and manifest_after is not None
        assert state_after.active_tree_version == base_snapshot.tree_version
        assert state_after.active_routing_slot == "SLOT_A"
        assert state_after.active_search_epoch == epoch_before
        assert manifest_after.status == "ACTIVE"
        pinned_after = build_query_routing_snapshot(
            state_after,
            manifest_after,
            [
                {
                    "project_id": project_id,
                    "source_ids": ["src-1"],
                    "document_version_ids": ["ver-1"],
                    "tenant_id": "tenant-alpha",
                    "partition_id": "part-tech",
                }
            ],
        )
    assert not qdrant_requests
    assert pinned_before.groups[0].tree_version == pinned_after.groups[0].tree_version
    assert pinned_before.groups[0].routing_slot == pinned_after.groups[0].routing_slot == "SLOT_A"

