from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    RoutingBenchmarkCase,
    TreeBuildConfig,
    _walk,
    build_routing_snapshot,
    fixture_snapshot,
)


def _units() -> list[KnowledgeUnitInput]:
    return [
        KnowledgeUnitInput(
            f"ku-{index}",
            "tenant-1",
            "project-1",
            "partition-a" if index < 4 else "partition-b",
            (1.0, float(index < 4)),
            (("shared", 1.0), ("group-a" if index < 4 else "group-b", 2.0)),
            ("shared-entity", "entity-a" if index < 4 else "entity-b"),
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 12, 31, tzinfo=UTC),
        )
        for index in range(8)
    ]


def test_build_is_deterministic_and_manifest_tracks_profile_inputs():
    units = _units()
    edges = [KnowledgeEdgeInput(f"ku-{i}", f"ku-{i + 1}", 0.8) for i in range(7)]
    benchmark = [RoutingBenchmarkCase("q-1", "ku-0", ("ku-0", "ku-1"))]
    config = TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_children=4)

    first = build_routing_snapshot(units, edges, config=config, benchmark=benchmark)
    second = build_routing_snapshot(reversed(units), reversed(edges), config=config, benchmark=benchmark)

    assert first.tree_version == second.tree_version
    assert first.lineage == second.lineage
    assert first.publishable
    assert first.metrics["routing_recall_at_k"] == 1.0
    units[0] = replace(units[0], entities=("changed",))
    changed = build_routing_snapshot(units, edges, config=config, benchmark=benchmark)
    assert changed.tree_version != first.tree_version


def test_profiles_and_edges_never_cross_security_partition():
    units = _units()
    # Explicit cross-partition edge must be excluded from clustering and lineage.
    edges = [KnowledgeEdgeInput("ku-0", "ku-4", 1.0)]
    snapshot = build_routing_snapshot(
        units,
        edges,
        config=TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_children=4),
        benchmark=[RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",))],
    )

    assert snapshot.quality_gates["partition_profiles"]
    for node in _walk(snapshot.roots):
        assert {units[int(unit_id.split("-")[1])].security_partition_id for unit_id in node.unit_ids} == {
            node.security_partition_id
        }
        assert node.profile.accessible_unit_count == len(node.unit_ids)
        assert not ({"entity-a", "entity-b"} <= set(node.profile.entities))


def test_quality_failure_is_rejected_and_benchmark_is_required():
    snapshot = build_routing_snapshot(
        _units(),
        config=TreeBuildConfig(min_routing_recall_at_k=0.8),
    )
    assert not snapshot.publishable
    assert snapshot.manifest["status"] == "REJECTED"
    assert not snapshot.quality_gates["routing_recall"]

    measured = build_routing_snapshot(
        _units(),
        config=TreeBuildConfig(min_routing_recall_at_k=0.8),
        benchmark=[RoutingBenchmarkCase("q-1", "ku-0", ())],
    )
    assert not measured.publishable
    assert measured.metrics["routing_recall_at_k"] == 0.0

    edge_cut_rejected = build_routing_snapshot(
        _units(),
        [KnowledgeEdgeInput(f"ku-{i}", f"ku-{i + 1}", 1.0) for i in range(7)],
        config=TreeBuildConfig(
            target_cluster_size=2,
            max_cluster_size=4,
            max_children=4,
            max_edge_cut=0.0,
        ),
        benchmark=[RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",))],
    )
    assert edge_cut_rejected.metrics["edge_cut"] > 0.0
    assert not edge_cut_rejected.quality_gates["edge_cut"]
    assert not edge_cut_rejected.publishable


def test_builder_rejects_mixed_topology_and_duplicate_unit_ids():
    with pytest.raises(ValueError, match="one tenant/project"):
        build_routing_snapshot([_units()[0], KnowledgeUnitInput("other", "tenant-2", "project-1", "p")])
    with pytest.raises(ValueError, match="unique"):
        build_routing_snapshot([_units()[0], _units()[0]])


def test_b2_snapshot_fixture_has_stable_contract():
    snapshot = fixture_snapshot()
    assert snapshot.contract_version == "routing-snapshot.v1"
    assert snapshot.tenant_id == "tenant-fixture"
    assert snapshot.project_id == "project-fixture"
    assert len(snapshot.roots) == 2
    assert {root.security_partition_id for root in snapshot.roots} == {
        "partition-public",
        "partition-private",
    }
    assert snapshot.manifest["partition_algorithm"] == snapshot.partition_algorithm
    assert snapshot.quality_gates["acl_partition_isolation"]
    assert snapshot.quality_gates["acl_blackhole"]
    manifest = snapshot.manifest
    manifest["status"] = "TAMPERED"
    assert snapshot.manifest["status"] == "QUALITY_PASSED"
    with pytest.raises(FrozenInstanceError):
        snapshot.roots[0].children = ()
    with pytest.raises(TypeError):
        snapshot.metrics["depth"] = 99


def test_leiden_uses_sparse_graph_edges_instead_of_complete_feature_graph(monkeypatch):
    import leidenalg

    actual_find_partition = leidenalg.find_partition
    observed_edge_counts = []

    def record_graph(graph, *args, **kwargs):
        observed_edge_counts.append(graph.ecount())
        return actual_find_partition(graph, *args, **kwargs)

    monkeypatch.setattr(leidenalg, "find_partition", record_graph)
    units = [replace(unit, dense=(1.0, 1.0)) for unit in _units()]
    snapshot = build_routing_snapshot(
        units,
        [KnowledgeEdgeInput("ku-0", "ku-1", 0.8)],
        config=TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_children=4),
        benchmark=[RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",))],
    )

    assert snapshot.publishable
    assert observed_edge_counts
    assert 1 in observed_edge_counts
    assert max(observed_edge_counts) == 1


def test_giant_single_community_uses_balanced_guard_and_obeys_bounds():
    units = [
        KnowledgeUnitInput(f"unit-{i:03}", "tenant-1", "project-1", "partition-a", (1.0,))
        for i in range(20)
    ]
    edges = [KnowledgeEdgeInput(f"unit-{i:03}", f"unit-{i + 1:03}", 1.0) for i in range(19)]
    snapshot = build_routing_snapshot(
        units,
        edges,
        config=TreeBuildConfig(target_cluster_size=2, max_cluster_size=20, max_children=4),
        benchmark=[RoutingBenchmarkCase("q-1", "unit-000", ("unit-000",))],
    )

    assert snapshot.publishable
    assert len(snapshot.roots[0].children) > 1
    assert snapshot.metrics["giant_ratio"] > 0
    assert snapshot.quality_gates["cluster_constraints"]
    assert snapshot.manifest["partition_algorithm"] == "constrained-hierarchical-leiden-cpm-v1"


def test_benchmark_applies_k_and_rejects_cross_partition_route():
    cases = [RoutingBenchmarkCase("q-1", "ku-0", ("ku-4", "ku-0"), k=1)]
    snapshot = build_routing_snapshot(
        _units(),
        config=TreeBuildConfig(min_routing_recall_at_k=1.0),
        benchmark=cases,
    )

    assert snapshot.metrics["routing_recall_at_k"] == 0.0
    assert snapshot.metrics["acl_routing_leakage_rate"] == 1.0
    assert not snapshot.quality_gates["acl_partition_isolation"]
    assert not snapshot.publishable


def test_reordered_duplicate_edges_preserve_checksum_and_metrics():
    units = _units()
    edges = [
        KnowledgeEdgeInput("ku-0", "ku-1", 0.8),
        KnowledgeEdgeInput("ku-0", "ku-1", 0.1),
        KnowledgeEdgeInput("ku-1", "ku-2", 0.5),
    ]
    config = TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_children=4)
    benchmark = [RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",))]
    first = build_routing_snapshot(units, edges, config=config, benchmark=benchmark)
    second = build_routing_snapshot(units, reversed(edges), config=config, benchmark=benchmark)

    assert first.tree_version == second.tree_version
    assert first.metrics == second.metrics


def test_reordered_sparse_and_entity_features_preserve_checksum():
    units = _units()
    reordered = [
        replace(unit, sparse=tuple(reversed(unit.sparse)), entities=tuple(reversed(unit.entities)))
        for unit in units
    ]
    edges = [KnowledgeEdgeInput(f"ku-{i}", f"ku-{i + 1}", 0.5) for i in range(7)]
    benchmark = [RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",))]

    original = build_routing_snapshot(units, edges, benchmark=benchmark)
    reordered_snapshot = build_routing_snapshot(reordered, edges, benchmark=benchmark)

    assert original.tree_version == reordered_snapshot.tree_version
    assert original.lineage == reordered_snapshot.lineage


def test_malformed_unit_edge_config_and_benchmark_inputs_fail_early():
    with pytest.raises(ValueError, match="finite"):
        build_routing_snapshot([replace(_units()[0], dense=(float("nan"),))])
    with pytest.raises(ValueError, match="aggregate must be finite"):
        build_routing_snapshot(
            [
                replace(_units()[0], sparse=(("overflow", 1e308),)),
                replace(_units()[1], sparse=(("overflow", 1e308),)),
            ]
        )
    with pytest.raises(ValueError, match="validity range"):
        build_routing_snapshot(
            [
                replace(
                    _units()[0],
                    valid_from=datetime(2026, 12, 31, tzinfo=UTC),
                    valid_to=datetime(2026, 1, 1, tzinfo=UTC),
                )
            ]
        )
    with pytest.raises(ValueError, match="missing Knowledge Unit"):
        build_routing_snapshot([_units()[0]], [KnowledgeEdgeInput("ku-0", "missing", 1.0)])
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        build_routing_snapshot([_units()[0]], [KnowledgeEdgeInput("ku-0", "ku-0", 1.1)])
    with pytest.raises(ValueError, match="positive k"):
        build_routing_snapshot(
            _units(), benchmark=[RoutingBenchmarkCase("q-1", "ku-0", (), k=0)]
        )
    with pytest.raises(ValueError, match="build constraints"):
        TreeBuildConfig(target_cluster_size=2.5)
    with pytest.raises(ValueError, match="build constraints"):
        TreeBuildConfig(max_edge_cut=1.1)
    with pytest.raises(ValueError, match="feature collections"):
        build_routing_snapshot([replace(_units()[0], dense=[1.0])])
    with pytest.raises(ValueError, match="endpoints"):
        build_routing_snapshot(
            [_units()[0]], [KnowledgeEdgeInput([], "ku-0", 0.5)]  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="benchmark fields"):
        build_routing_snapshot(
            _units(), benchmark=[RoutingBenchmarkCase("q-1", "ku-0", ("ku-0",), k=True)]
        )
    with pytest.raises(ValueError, match="benchmark fields"):
        build_routing_snapshot(
            _units(), benchmark=[RoutingBenchmarkCase(None, "ku-0", ())]  # type: ignore[arg-type]
        )


def test_dense_medoid_search_has_a_deterministic_candidate_bound():
    units = [
        KnowledgeUnitInput(f"unit-{i:03}", "tenant-1", "project-1", "partition-a", (float(i), 1.0))
        for i in range(130)
    ]
    snapshot = build_routing_snapshot(
        units,
        config=TreeBuildConfig(target_cluster_size=8, max_cluster_size=32, max_children=8),
        benchmark=[RoutingBenchmarkCase("q-1", "unit-000", ("unit-000",))],
    )

    assert snapshot.publishable
    assert snapshot.roots[0].profile.dense_medoid_candidate_count == 64
    assert snapshot.roots[0].profile.dense_medoid in {unit.dense for unit in units}


def test_dense_medoid_handles_large_finite_vectors_without_overflow():
    units = [
        KnowledgeUnitInput(f"unit-{i}", "tenant-1", "project-1", "partition-a", (1e308, 1e308))
        for i in range(2)
    ]
    snapshot = build_routing_snapshot(
        units,
        config=TreeBuildConfig(min_cluster_size=1, target_cluster_size=1, max_cluster_size=1),
        benchmark=[RoutingBenchmarkCase("q-1", "unit-0", ("unit-0",))],
    )

    assert snapshot.publishable
    assert snapshot.roots[0].profile.dense_medoid == (1e308, 1e308)


def test_max_depth_violation_rejects_candidate_instead_of_returning_oversized_leaf():
    units = [
        KnowledgeUnitInput(f"unit-{i:02}", "tenant-1", "project-1", "partition-a")
        for i in range(12)
    ]
    snapshot = build_routing_snapshot(
        units,
        config=TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_depth=0),
        benchmark=[RoutingBenchmarkCase("q-1", "unit-00", ("unit-00",))],
    )

    assert not snapshot.publishable
    assert snapshot.manifest["status"] == "REJECTED"
    assert not snapshot.quality_gates["cluster_constraints"]


def test_small_cluster_repair_does_not_create_oversized_child():
    units = [
        KnowledgeUnitInput(f"unit-{i:02}", "tenant-1", "project-1", "partition-a")
        for i in range(33)
    ]
    snapshot = build_routing_snapshot(
        units,
        config=TreeBuildConfig(
            min_cluster_size=2,
            target_cluster_size=2,
            max_cluster_size=8,
            max_children=8,
        ),
        benchmark=[RoutingBenchmarkCase("q-1", "unit-00", ("unit-00",))],
    )

    assert snapshot.publishable
    assert all(2 <= len(child.unit_ids) <= 8 for child in snapshot.roots[0].children)
