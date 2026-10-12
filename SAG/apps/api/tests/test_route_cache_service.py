from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sag_api.services.query_routing_service import (
    GroupRoutingSnapshot,
    NodeProfile,
    RoutingPolicy,
    scope_fingerprint,
)
from sag_api.services.route_cache_service import (
    RouteDecisionCache,
    build_route_decision_cache_key,
    effective_acl_epoch,
    principal_acl_fingerprint,
)


def test_route_decision_cache_setting_defaults_off():
    from sag_api.core.config import Settings

    assert Settings.model_fields["search_route_decision_cache_enabled"].default is False


def _snapshot() -> GroupRoutingSnapshot:
    scope = {
        "project_id": "project-1",
        "source_ids": ["source-1"],
        "document_version_ids": ["version-1"],
        "tenant_id": "tenant-1",
        "partition_id": "partition-1",
    }
    fingerprint = scope_fingerprint(scope)
    profile = NodeProfile(
        node_id="node-1",
        parent_id=None,
        is_leaf=True,
        accessible_unit_count=1,
        project_id="project-1",
        source_ids=("source-1",),
        document_version_ids=("version-1",),
        tenant_id="tenant-1",
        partition_id="partition-1",
        tree_version="tree-1",
        scope_fingerprint=fingerprint,
        signal_scores={"sparse": 1.0},
    )
    return GroupRoutingSnapshot(
        snapshot_id="snapshot-1",
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        project_id="project-1",
        source_ids=("source-1",),
        document_version_ids=("version-1",),
        tenant_id="tenant-1",
        partition_id="partition-1",
        scope_fingerprint=fingerprint,
        tree_version="tree-1",
        tree_config_version="routing-snapshot.v1",
        routing_slot="SLOT_A",
        search_epoch=3,
        manifest_status="ACTIVE",
        manifest_checksum="a" * 64,
        manifest_verified=True,
        profiles=(profile,),
    )


def _key(snapshot: GroupRoutingSnapshot | None = None, **overrides: object) -> str | None:
    query = overrides.pop("query", "Where is the current policy?")
    planner_trace = overrides.pop(
        "planner_trace",
        {"planner_version": "qsp-v1", "effective_strategy": "LOCAL_FACTUAL"},
    )
    scope = {
        "project_id": "project-1",
        "source_ids": ["source-1"],
        "document_version_ids": ["version-1"],
        "tenant_id": "tenant-1",
        "partition_id": "partition-1",
        "acl_mapping_version": 1,
        "search_index_versions": [
            {
                "document_version_id": "version-1",
                "ingestion_run_id": "run-1",
                "index_stage_run_id": "stage-1",
                "manifest_checksum": "b" * 64,
            }
        ],
    }
    values: dict[str, object] = {
        "acl_fingerprint": "acl-fingerprint-1",
        "acl_epoch": "effective-acl-epoch-1",
        "model_version": "profile-scorer-v1",
        "filters": {"authorized_scopes": [scope], "top_k": 8},
    }
    values.update(overrides)
    return build_route_decision_cache_key(
        snapshot or _snapshot(),
        query=query,
        planner_trace=planner_trace,
        **values,
    )


def test_cache_is_disabled_by_default():
    cache = RouteDecisionCache()

    cache.put("key", "decision")

    assert cache.get("key") is None


def test_exact_route_key_is_stable_and_does_not_retain_raw_query():
    key = _key()

    assert key == _key()
    assert key is not None
    assert "Where is the current policy?" not in key


@pytest.mark.parametrize(
    "overrides",
    [
        {"acl_fingerprint": "other-principal"},
        {"acl_epoch": "after-revoke"},
        {"model_version": "profile-scorer-v2"},
        {
            "filters": {
                "authorized_scopes": [
                    {
                        "project_id": "project-1",
                        "source_ids": ["source-1"],
                        "document_version_ids": ["version-1"],
                        "tenant_id": "tenant-1",
                        "partition_id": "partition-1",
                        "acl_mapping_version": 1,
                        "search_index_versions": [
                            {
                                "document_version_id": "version-1",
                                "ingestion_run_id": "run-1",
                                "index_stage_run_id": "stage-1",
                                "manifest_checksum": "b" * 64,
                            }
                        ],
                    }
                ],
                "top_k": 9,
            }
        },
    ],
)
def test_route_key_changes_for_acl_model_and_filter_boundaries(overrides):
    assert _key(**overrides) != _key()


def test_route_key_changes_for_exact_query_planner_and_profile_inputs():
    assert _key(query="Where is the previous policy?") != _key()
    assert _key(planner_trace={"planner_version": "qsp-v2", "effective_strategy": "LOCAL_FACTUAL"}) != _key()

    snapshot = _snapshot()
    changed_profile = snapshot.profiles[0].model_copy(update={"signal_scores": {"sparse": 0.5}})
    assert _key(snapshot.model_copy(update={"profiles": (changed_profile,)})) != _key(snapshot)
    assert _key(snapshot.model_copy(update={"manifest_checksum": "b" * 64})) != _key(snapshot)


def test_route_key_changes_for_acl_mapping_and_current_index_attempt():
    scope = _key_filters()["authorized_scopes"][0]
    changed_mapping = {**scope, "acl_mapping_version": 2}
    assert _key(filters={"authorized_scopes": [changed_mapping], "top_k": 8}) != _key()

    changed_attempt = {
        **scope,
        "search_index_versions": [
            {
                "document_version_id": "version-1",
                "ingestion_run_id": "run-2",
                "index_stage_run_id": "stage-2",
                "manifest_checksum": "b" * 64,
            }
        ],
    }
    assert _key(filters={"authorized_scopes": [changed_attempt], "top_k": 8}) != _key()


def _key_filters() -> dict[str, object]:
    # Keep test mutations focused on the authorization and current-index tokens.
    return {
        "authorized_scopes": [
            {
                "project_id": "project-1",
                "source_ids": ["source-1"],
                "document_version_ids": ["version-1"],
                "tenant_id": "tenant-1",
                "partition_id": "partition-1",
                "acl_mapping_version": 1,
                "search_index_versions": [
                    {
                        "document_version_id": "version-1",
                        "ingestion_run_id": "run-1",
                        "index_stage_run_id": "stage-1",
                        "manifest_checksum": "b" * 64,
                    }
                ],
            }
        ],
        "top_k": 8,
    }


def test_route_key_changes_after_tree_search_publish_or_rollback():
    snapshot = _snapshot()
    changed = snapshot.model_copy(update={"search_epoch": snapshot.search_epoch + 1})
    assert _key(changed) != _key(snapshot)

    changed = snapshot.model_copy(update={"tree_version": "tree-2"})
    assert _key(changed) != _key(snapshot)

    changed = snapshot.model_copy(update={"tree_config_version": "routing-snapshot.v2"})
    assert _key(changed) != _key(snapshot)

    changed = snapshot.model_copy(update={"routing_slot": "SLOT_B"})
    assert _key(changed) != _key(snapshot)

    changed = snapshot.model_copy(update={"document_version_ids": ("version-2",)})
    assert _key(changed) != _key(snapshot)


def test_route_key_changes_for_project_tenant_and_partition_boundaries():
    snapshot = _snapshot()
    for field, value in (
        ("project_id", "project-2"),
        ("tenant_id", "tenant-2"),
        ("partition_id", "partition-2"),
        ("source_ids", ("source-2",)),
    ):
        assert _key(snapshot.model_copy(update={field: value})) != _key(snapshot)


def test_route_key_requires_current_acl_and_complete_snapshot_versions():
    assert _key(acl_fingerprint="") is None
    assert _key(acl_epoch="") is None
    assert _key(model_version="") is None
    assert _key(_snapshot().model_copy(update={"manifest_verified": False})) is None
    assert _key(_snapshot().model_copy(update={"search_epoch": 0})) is None
    assert _key(_snapshot().model_copy(update={"tree_config_version": ""})) is None
    incomplete_scope = _key_filters()["authorized_scopes"][0]
    del incomplete_scope["search_index_versions"]
    assert _key(filters={"authorized_scopes": [incomplete_scope], "top_k": 8}) is None


def test_principal_fingerprint_and_effective_acl_epoch_change_with_authorization_scope():
    from types import SimpleNamespace

    principal = SimpleNamespace(
        subject="user-1",
        organization_id="org-1",
        tenant_id="tenant-1",
        allowed_project_ids=frozenset({"project-1"}),
        allowed_partition_ids=frozenset({"partition-1"}),
    )
    same_principal = SimpleNamespace(
        subject="user-1",
        organization_id="org-1",
        tenant_id="tenant-1",
        allowed_project_ids=frozenset({"project-1"}),
        allowed_partition_ids=frozenset({"partition-1"}),
    )

    assert principal_acl_fingerprint(principal) == principal_acl_fingerprint(same_principal)
    changed_principal = SimpleNamespace(
        subject="user-1",
        organization_id="org-1",
        tenant_id="tenant-1",
        allowed_project_ids=frozenset({"project-1"}),
        allowed_partition_ids=frozenset({"partition-2"}),
    )
    assert principal_acl_fingerprint(principal) != principal_acl_fingerprint(changed_principal)
    scope = {
        "project_id": "project-1",
        "source_ids": ["source-1"],
        "document_version_ids": ["version-1"],
        "tenant_id": "tenant-1",
        "partition_id": "partition-1",
        "acl_mapping_version": 1,
    }
    assert effective_acl_epoch([scope]) == effective_acl_epoch([dict(scope)])
    assert effective_acl_epoch([scope]) != effective_acl_epoch([{**scope, "source_ids": ["source-2"]}])
    assert effective_acl_epoch([scope]) != effective_acl_epoch([{**scope, "acl_mapping_version": 2}])
    assert effective_acl_epoch([{key: value for key, value in scope.items() if key != "acl_mapping_version"}]) is None


def test_cache_is_bounded_and_only_reuses_exact_key():
    cache = RouteDecisionCache(enabled=True, max_entries=1)
    cache.put("one", "route-one")
    assert cache.get("one") == "route-one"

    cache.put("two", "route-two")

    assert cache.get("one") is None
    assert cache.get("two") == "route-two"
    assert cache.get("two:semantic-neighbor") is None


def test_policy_change_changes_route_key():
    default = _key()
    changed = _key(policy=RoutingPolicy(beam_width=2))

    assert changed != default
