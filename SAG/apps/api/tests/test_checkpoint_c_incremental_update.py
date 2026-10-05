from __future__ import annotations

from sag_api.services import incremental_tree_service
from sag_api.services.incremental_tree_service import update_tree_incrementally
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    TreeBuildConfig,
    build_routing_snapshot,
)


def _tree(config: TreeBuildConfig):
    units = [
        KnowledgeUnitInput(
            unit_id=f"ku-{index}",
            tenant_id="tenant-1",
            project_id="project-1",
            security_partition_id="partition-1",
            dense=(1.0, 0.0) if index < 4 else (0.0, 1.0),
            sparse=(("alpha" if index < 4 else "beta", 1.0),),
            entities=("Alpha" if index < 4 else "Beta",),
        )
        for index in range(8)
    ]
    edges = [
        KnowledgeEdgeInput(f"ku-{index}", f"ku-{index + 1}", 0.9)
        for index in (0, 1, 2, 4, 5, 6)
    ]
    return build_routing_snapshot(units, edges, config=config), edges


def _new_unit() -> KnowledgeUnitInput:
    return KnowledgeUnitInput(
        unit_id="ku-new",
        tenant_id="tenant-1",
        project_id="project-1",
        security_partition_id="partition-1",
        dense=(1.0, 0.0),
        sparse=(("alpha", 2.0), ("new-term", 1.0)),
        entities=("Alpha", "NewEntity"),
    )


def test_normal_ingest_updates_base_and_delta_without_tree_builder(monkeypatch):
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    base, _ = _tree(config)
    unit = _new_unit()
    untouched_sibling = base.roots[0].children[1]

    def forbidden_full_or_subtree_build(*_args, **_kwargs):
        raise AssertionError("normal delta ingest must not call the tree builder")

    monkeypatch.setattr(incremental_tree_service, "build_routing_snapshot", forbidden_full_or_subtree_build)
    result = update_tree_incrementally(base, [unit], config=config)

    updated_root = result.snapshot.roots[0]
    updated_leaf = next(child for child in updated_root.children if unit.unit_id in child.unit_ids)
    assert result.rebuilt_node_ids == ()
    assert result.drift.trigger_rebuild is False
    assert unit.unit_id in updated_root.unit_ids
    assert updated_root.profile.accessible_unit_count == 9
    assert "new-term" in dict(updated_root.profile.sparse)
    assert updated_leaf.profile.accessible_unit_count == 5
    assert updated_root.children[1] is untouched_sibling
    assert result.snapshot.manifest["unit_count"] == 9
    assert result.snapshot.manifest["delta_unit_ids"] == []

    retry = update_tree_incrementally(result.snapshot, [unit], config=config)
    assert retry.snapshot is result.snapshot
    assert retry.snapshot.tree_version == result.snapshot.tree_version
    assert len(retry.snapshot.manifest["units"]) == 9


def test_drift_rebuilds_only_selected_subtree_and_is_deterministic(monkeypatch):
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=4)
    base, _ = _tree(config)
    unit = _new_unit()
    new_edge = KnowledgeEdgeInput("ku-0", unit.unit_id, 0.95)
    target_before = base.roots[0].children[0]
    sibling_before = base.roots[0].children[1]
    built_unit_sets: list[set[str]] = []
    builder = incremental_tree_service.build_routing_snapshot

    def record_subtree_build(units, *args, **kwargs):
        rows = list(units)
        built_unit_sets.append({row.unit_id for row in rows})
        return builder(rows, *args, **kwargs)

    monkeypatch.setattr(incremental_tree_service, "build_routing_snapshot", record_subtree_build)
    first = update_tree_incrementally(
        base,
        [unit],
        [new_edge],
        config=config,
        history_window_violations=1,
    )
    second = update_tree_incrementally(
        base,
        [unit],
        [new_edge],
        config=config,
        history_window_violations=1,
    )

    assert first.drift.capacity_overflow
    assert first.drift.trigger_rebuild
    assert first.rebuilt_node_ids == (target_before.node_id,)
    assert built_unit_sets == [
        {"ku-0", "ku-1", "ku-2", "ku-3", "ku-new"},
        {"ku-0", "ku-1", "ku-2", "ku-3", "ku-new"},
    ]
    updated_root = first.snapshot.roots[0]
    rebuilt_target = next(child for child in updated_root.children if child.node_id == target_before.node_id)
    assert rebuilt_target.profile.accessible_unit_count == 5
    assert next(child for child in updated_root.children if child.node_id == sibling_before.node_id) is sibling_before
    assert updated_root.profile.accessible_unit_count == 9
    assert first.snapshot.tree_version == second.snapshot.tree_version
    assert first.snapshot.lineage == second.snapshot.lineage
    assert first.snapshot.manifest["lineage_events"] == second.snapshot.manifest["lineage_events"]
    assert first.snapshot.manifest["incremental_update"]["drift"]["trigger_rebuild"] is True

    retry = update_tree_incrementally(first.snapshot, [unit], [new_edge], config=config)
    assert retry.snapshot is first.snapshot
    assert retry.rebuilt_node_ids == ()


def test_centroid_drift_rebuilds_only_the_shifted_leaf(monkeypatch):
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=20)
    base, edges = _tree(config)
    target_before, sibling_before = base.roots[0].children
    shifted_units = [
        KnowledgeUnitInput(
            unit_id=f"ku-shift-{index}",
            tenant_id="tenant-1",
            project_id="project-1",
            security_partition_id="partition-1",
            dense=(0.75, 0.66),
            sparse=(("alpha", 1.0),),
            entities=("Alpha",),
        )
        for index in range(6)
    ]
    new_edges = [KnowledgeEdgeInput("ku-0", unit.unit_id, 0.9) for unit in shifted_units]
    built_unit_sets: list[set[str]] = []
    builder = incremental_tree_service.build_routing_snapshot

    def record_subtree_build(units, *args, **kwargs):
        rows = list(units)
        built_unit_sets.append({row.unit_id for row in rows})
        return builder(rows, *args, **kwargs)

    monkeypatch.setattr(incremental_tree_service, "build_routing_snapshot", record_subtree_build)
    result = update_tree_incrementally(
        base,
        shifted_units,
        [*edges, *new_edges],
        config=config,
        history_window_violations=1,
    )

    assert result.drift.centroid_drift > 0.15
    assert not result.drift.capacity_overflow
    assert result.rebuilt_node_ids == (target_before.node_id,)
    assert built_unit_sets == [set(target_before.unit_ids) | {unit.unit_id for unit in shifted_units}]
    assert result.snapshot.roots[0].children[1] is sibling_before


def test_new_partition_quality_gate_builds_only_that_partition():
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    base, _ = _tree(config)
    existing_root = base.roots[0]
    new_units = [
        KnowledgeUnitInput(
            unit_id=f"ku-new-partition-{index}",
            tenant_id="tenant-1",
            project_id="project-1",
            security_partition_id="partition-2",
            dense=(1.0, float(index)),
            sparse=(("gamma", 1.0),),
            entities=("Gamma",),
        )
        for index in range(2)
    ]

    result = update_tree_incrementally(
        base,
        new_units,
        config=config,
        history_window_violations=1,
    )

    assert result.drift.new_partition_ids == ("partition-2",)
    assert result.rebuilt_partition_ids == ("partition-2",)
    assert [root.security_partition_id for root in result.snapshot.roots] == [
        "partition-1",
        "partition-2",
    ]
    assert result.snapshot.roots[0] is existing_root
    assert set(result.snapshot.roots[1].unit_ids) == {unit.unit_id for unit in new_units}


def test_lineage_matching_is_independent_of_input_order():
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    base, _ = _tree(config)
    left = base.roots[0].children
    result_forward = incremental_tree_service.match_node_lineage(list(left), list(left))
    result_reversed = incremental_tree_service.match_node_lineage(list(reversed(left)), list(reversed(left)))

    assert result_forward.node_id_map == result_reversed.node_id_map
    assert result_forward.relationships == result_reversed.relationships

    split = incremental_tree_service.match_node_lineage([base.roots[0]], list(left))
    merged = incremental_tree_service.match_node_lineage(list(left), [base.roots[0]])
    assert any(event["type"] == "SPLIT_FROM" for event in split.relationships)
    assert any(event["type"] == "MERGED_FROM" for event in merged.relationships)
