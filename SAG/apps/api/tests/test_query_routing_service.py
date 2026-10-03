from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sag_api.services.query_routing_service import (
    GroupRoutingSnapshot,
    NodeProfile,
    RoutingPolicy,
    RoutingSnapshot,
    capture_routing_decisions,
    route_snapshot,
    scope_fingerprint,
)


def _snapshot(*, accessible: bool = True, scores: tuple[float, float] = (0.9, 0.1)):
    scope = {
        "project_id": "p1",
        "source_ids": ["s1"],
        "document_version_ids": ["v1"],
        "tenant_id": "t1",
        "partition_id": "part1",
    }
    fingerprint = scope_fingerprint(scope)
    scope_fields = {
        **scope,
        "source_ids": ("s1",),
        "document_version_ids": ("v1",),
    }
    profiles = (
        NodeProfile(
            node_id="root",
            parent_id=None,
            is_leaf=False,
            accessible_unit_count=2,
            **scope_fields,
            tree_version="tree-v1",
            scope_fingerprint=fingerprint,
            signal_scores={"dense": 0.8},
        ),
        NodeProfile(
            node_id="leaf-a",
            parent_id="root",
            is_leaf=True,
            accessible_unit_count=1 if accessible else 0,
            **scope_fields,
            tree_version="tree-v1",
            scope_fingerprint=fingerprint,
            signal_scores={"dense": scores[0]},
        ),
        NodeProfile(
            node_id="leaf-b",
            parent_id="root",
            is_leaf=True,
            accessible_unit_count=1,
            **scope_fields,
            tree_version="tree-v1",
            scope_fingerprint=fingerprint,
            signal_scores={"dense": scores[1]},
        ),
    )
    return GroupRoutingSnapshot(
        snapshot_id="snap-1",
        captured_at=datetime(2026, 10, 3, tzinfo=UTC),
        project_id="p1",
        source_ids=("s1",),
        document_version_ids=("v1",),
        tenant_id="t1",
        partition_id="part1",
        scope_fingerprint=fingerprint,
        tree_version="tree-v1",
        routing_slot="SLOT_A",
        search_epoch=2,
        manifest_status="ACTIVE",
        manifest_checksum="a" * 64,
        manifest_verified=True,
        profiles=profiles,
    )


def test_snapshot_routes_to_decisive_leaf_with_versioned_membership():
    result = route_snapshot(_snapshot(), policy=RoutingPolicy())

    assert "leaf-a" in result.membership_node_ids
    assert result.membership_node_ids == ("leaf-a",)
    assert result.tree_version == "tree-v1"
    assert not result.broad_route


def test_undecidable_scores_keep_broad_accessible_branches():
    result = route_snapshot(_snapshot(scores=(0.5, 0.49)), policy=RoutingPolicy())

    assert set(result.selected_nodes) == {"leaf-a", "leaf-b"}
    assert result.broad_route
    assert result.reason_code == "ROUTE_UNCERTAIN_BROADENED"


def test_membership_limit_falls_back_to_global_instead_of_failing_search():
    result = route_snapshot(
        _snapshot(scores=(0.5, 0.49)),
        policy=RoutingPolicy(membership_limit=1),
    )

    assert not result.routed
    assert result.reason_code == "TREE_MEMBERSHIP_LIMIT"


def test_profiles_with_wrong_scope_or_zero_access_are_pruned_before_beam():
    snapshot = _snapshot(accessible=False)
    wrong_scope = snapshot.profiles[1].model_copy(update={"source_ids": ("other",)})
    snapshot = snapshot.model_copy(update={"profiles": (snapshot.profiles[0], wrong_scope, snapshot.profiles[2])})

    result = route_snapshot(snapshot)

    assert result.selected_nodes == ("leaf-b",)
    assert "leaf-a" not in result.selected_nodes
    assert "leaf-a" not in result.membership_node_ids


def test_invalid_slot_tree_or_membership_fails_closed():
    snapshot = _snapshot().model_copy(update={"routing_slot": "SLOT_C"})

    result = route_snapshot(snapshot)

    assert result.selected_nodes == ()
    assert result.reason_code == "ROUTING_SNAPSHOT_INVALID"


@pytest.mark.asyncio
async def test_snapshot_provider_timeout_falls_back_to_global_search():
    group = SimpleNamespace(
        project_id="p1",
        source=SimpleNamespace(id="s1"),
        versions=("v1",),
        tenant_id="t1",
        partition_id="part1",
    )

    class DelayedProvider:
        async def get_routing_snapshot(self, **_kwargs):
            await asyncio.sleep(1)

    decisions = await capture_routing_decisions(
        DelayedProvider(),
        [group],
        query="question",
        planner_trace={"planner_version": "qsp-v1"},
        timeout_seconds=0.001,
    )

    route = decisions[group.project_id, "s1", "t1", "part1"]
    assert not route.routed
    assert route.reason_code == "TREE_PROVIDER_ERROR"
    assert route.fallback_reason == "routing_snapshot_invalid_or_unavailable"


@pytest.mark.asyncio
async def test_oversized_request_snapshot_falls_back_before_profile_routing(monkeypatch):
    from sag_api.core.config import settings

    group_snapshot = _snapshot()
    request_snapshot = RoutingSnapshot(
        snapshot_id="request-snapshot",
        captured_at=group_snapshot.captured_at,
        groups=(group_snapshot,),
    )
    monkeypatch.setattr(settings, "search_tree_profile_limit", 2)
    group = SimpleNamespace(
        project_id="p1",
        source=SimpleNamespace(id="s1"),
        versions=("v1",),
        tenant_id="t1",
        partition_id="part1",
    )

    class OversizedProvider:
        async def get_routing_snapshot(self, **_kwargs):
            return request_snapshot

    decisions = await capture_routing_decisions(
        OversizedProvider(),
        [group],
        query="question",
        planner_trace={"planner_version": "qsp-v1"},
    )

    route = decisions["p1", "s1", "t1", "part1"]
    assert not route.routed
    assert route.reason_code == "TREE_PROVIDER_ERROR"
