from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from checkpoint_c_test_support import (
    _assignments,
    _qdrant_client,
    _seed_search_units,
    _snapshot,
    _stable_id,
)
from sqlalchemy import select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models.routing_rag import (
    ProjectSearchState,
    TreeManifest,
    TreeRoutingProfile,
    TreeSnapshotLease,
)
from sag_api.sag.engine_manager import EngineManager
from sag_api.services.query_routing_service import scope_fingerprint
from sag_api.services.tree_publish_service import (
    PublishVerificationError,
    TreePublishError,
    TreeSlotInUse,
    _scoped_profile_checksum,
    _verify_stored_source_profiles,
    build_inactive_slot_payloads,
    prepare_tree_candidate,
    publish_tree_candidate,
)


@pytest.mark.asyncio
async def test_candidate_manifest_checksum_covers_persisted_routing_profiles():
    snapshot = _snapshot(f"project-{uuid4().hex}")
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))

    assert candidate.manifest["checksum"] == candidate.checksum
    assert candidate.tree_version == candidate.manifest["tree_version"]
    assert candidate.tree_version != candidate.source_tree_version
    assert candidate.manifest["routing_profiles"]
    payload_batches = build_inactive_slot_payloads(candidate, "SLOT_B")
    assert len(payload_batches) >= 1
    assert sum(len(batch["points"]) for batch in payload_batches) == candidate.expected_point_count
    assert all("tree_version_b" in batch["payload"] for batch in payload_batches)
    assert candidate.expected_partition_counts == {"partition-public": 4, "partition-private": 4}


@pytest.mark.asyncio
async def test_stored_profile_verification_orders_document_versions_before_nodes():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    manifest = candidate.manifest
    profiles = manifest["source_routing_profiles"]
    source_id = profiles[0]["source_id"]
    source_profiles = [profile for profile in profiles if profile["source_id"] == source_id]
    template = source_profiles[0]
    expected_profiles = [profile for profile in profiles if profile["source_id"] != source_id]
    inserted_profiles = []
    for version_id in ("version-z", "version-a"):
        for node_id in ("node-2", "node-1"):
            profile = {
                **template,
                "document_version_id": version_id,
                "node_id": node_id,
            }
            expected_profiles.append(profile)
            inserted_profiles.append(profile)
    manifest["source_routing_profiles"] = sorted(
        expected_profiles,
        key=lambda profile: (
            profile["source_id"],
            profile["document_version_id"],
            profile["partition_id"],
            profile["node_id"],
        ),
    )
    candidate = replace(candidate, manifest_json=json.dumps(manifest))

    async with SessionLocal() as session:
        for profile in [item for item in profiles if item["source_id"] != source_id] + inserted_profiles:
            session.add(
                TreeRoutingProfile(
                    project_id=profile["project_id"],
                    tree_version=candidate.tree_version,
                    source_id=profile["source_id"],
                    document_version_id=profile["document_version_id"],
                    partition_id=profile["partition_id"],
                    node_id=profile["node_id"],
                    tenant_id=profile["tenant_id"],
                    parent_id=profile["parent_id"],
                    is_leaf=profile["is_leaf"],
                    accessible_unit_count=profile["accessible_unit_count"],
                    sparse_json=profile["sparse"],
                    entities_json=profile["entities"],
                    profile_checksum=_scoped_profile_checksum(candidate.tree_version, profile),
                )
            )
        await session.flush()
        await _verify_stored_source_profiles(session, candidate)


@pytest.mark.asyncio
async def test_query_snapshot_honors_configured_profile_limit_above_1024(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "search_tree_profile_limit", 2048)
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate)
    await publish_tree_candidate(
        snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
    )
    await client.aclose()

    scope = {
        "project_id": snapshot.project_id,
        "source_ids": [_stable_id(snapshot.project_id, "source", "0")],
        "document_version_ids": [_stable_id(snapshot.project_id, "version", "0")],
        "tenant_id": "tenant-c",
        "partition_id": "partition-public",
    }
    async with SessionLocal() as session:
        existing_profiles = (
            await session.scalars(
                select(TreeRoutingProfile).where(
                    TreeRoutingProfile.project_id == snapshot.project_id,
                    TreeRoutingProfile.tree_version == candidate.tree_version,
                    TreeRoutingProfile.source_id == scope["source_ids"][0],
                    TreeRoutingProfile.document_version_id == scope["document_version_ids"][0],
                    TreeRoutingProfile.partition_id == scope["partition_id"],
                )
            )
        ).all()
        assert existing_profiles
        template = existing_profiles[0]
        extras = []
        for index in range(1025 - len(existing_profiles)):
            profile = {
                "project_id": template.project_id,
                "tenant_id": template.tenant_id,
                "source_id": template.source_id,
                "document_version_id": template.document_version_id,
                "partition_id": template.partition_id,
                "node_id": f"limit-profile-{index:04d}",
                "parent_id": template.parent_id,
                "is_leaf": template.is_leaf,
                "accessible_unit_count": template.accessible_unit_count,
                "sparse": template.sparse_json,
                "entities": template.entities_json,
            }
            extras.append(
                TreeRoutingProfile(
                    project_id=profile["project_id"],
                    tree_version=candidate.tree_version,
                    source_id=profile["source_id"],
                    document_version_id=profile["document_version_id"],
                    partition_id=profile["partition_id"],
                    node_id=profile["node_id"],
                    tenant_id=profile["tenant_id"],
                    parent_id=profile["parent_id"],
                    is_leaf=profile["is_leaf"],
                    accessible_unit_count=profile["accessible_unit_count"],
                    sparse_json=profile["sparse"],
                    entities_json=profile["entities"],
                    profile_checksum=_scoped_profile_checksum(candidate.tree_version, profile),
                )
            )
        session.add_all(extras)
        await session.commit()

    pinned = await EngineManager(settings).get_routing_snapshot(query="alpha", scopes=[scope], planner={})
    assert pinned is not None and pinned.groups
    assert len(pinned.groups[0].profiles) == 1025


@pytest.mark.asyncio
async def test_verification_failure_keeps_active_pointer_and_epoch_unchanged():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    first = _snapshot(project_id, variant=0)
    second = _snapshot(project_id, variant=1)
    await _seed_search_units(first)
    first_candidate = prepare_tree_candidate(first, _assignments(first))
    good_client = _qdrant_client(first_candidate)
    await publish_tree_candidate(first, search_unit_assignments=_assignments(first), qdrant_client=good_client)
    await good_client.aclose()

    async with SessionLocal() as session:
        before = await session.get(ProjectSearchState, project_id)
        assert before is not None
        active_before = (before.active_tree_version, before.active_routing_slot, before.active_search_epoch)

    failed_candidate = prepare_tree_candidate(second, _assignments(second))
    bad_client = _qdrant_client(failed_candidate, wrong_count=True)
    with pytest.raises(PublishVerificationError):
        await publish_tree_candidate(second, search_unit_assignments=_assignments(second), qdrant_client=bad_client)
    await bad_client.aclose()

    async with SessionLocal() as session:
        after = await session.get(ProjectSearchState, project_id)
        assert after is not None
        assert (after.active_tree_version, after.active_routing_slot, after.active_search_epoch) == active_before
        rejected = await session.get(TreeManifest, failed_candidate.tree_version)
        assert rejected is not None and rejected.status == "REJECTED"


@pytest.mark.asyncio
async def test_acknowledged_only_payload_write_cannot_switch_active_pointer():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate, acknowledged_only=True)
    with pytest.raises(PublishVerificationError, match="completed"):
        await publish_tree_candidate(
            snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
        )
    await client.aclose()

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, snapshot.project_id)
        manifest = await session.get(TreeManifest, candidate.tree_version)
        assert state is not None and state.active_tree_version is None
        assert state.active_search_epoch == 1
        assert manifest is not None and manifest.status == "REJECTED"


@pytest.mark.asyncio
async def test_idempotent_publish_rejects_active_slot_manifest_mismatch():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate)
    await publish_tree_candidate(
        snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
    )

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, snapshot.project_id)
        assert state is not None
        state.slot_a_tree_version = "tree-from-another-publish"
        await session.commit()

    with pytest.raises(TreePublishError, match="slot and tree version disagree"):
        await publish_tree_candidate(
            snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_acl_smoke_rejects_wrong_point_source_with_correct_counts():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate, wrong_source_payload=True)
    with pytest.raises(PublishVerificationError, match="ACL or tree payload"):
        await publish_tree_candidate(
            snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
        )
    await client.aclose()

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, snapshot.project_id)
        assert state is not None and state.active_tree_version is None


@pytest.mark.asyncio
async def test_query_snapshot_fails_closed_on_corrupt_indexed_profile():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate)
    await publish_tree_candidate(
        snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
    )
    await client.aclose()

    scope = {
        "project_id": snapshot.project_id,
        "source_ids": [_stable_id(snapshot.project_id, "source", "0")],
        "document_version_ids": [_stable_id(snapshot.project_id, "version", "0")],
        "tenant_id": "tenant-c",
        "partition_id": "partition-public",
    }
    async with SessionLocal() as session:
        profile = await session.scalar(
            select(TreeRoutingProfile).where(
                TreeRoutingProfile.project_id == snapshot.project_id,
                TreeRoutingProfile.tree_version == candidate.tree_version,
                TreeRoutingProfile.source_id == scope["source_ids"][0],
                TreeRoutingProfile.partition_id == "partition-public",
            )
        )
        assert profile is not None
        profile.sparse_json = [["tampered", 1.0]]
        await session.commit()

    manager = EngineManager(settings)
    assert await manager.get_routing_snapshot(query="tampered", scopes=[scope], planner={}) is None


@pytest.mark.asyncio
async def test_query_snapshot_does_not_mix_profile_signals_across_document_versions():
    await init_db()
    snapshot = _snapshot(f"project-{uuid4().hex}")
    await _seed_search_units(snapshot)
    candidate = prepare_tree_candidate(snapshot, _assignments(snapshot))
    client = _qdrant_client(candidate)
    await publish_tree_candidate(
        snapshot, search_unit_assignments=_assignments(snapshot), qdrant_client=client
    )
    await client.aclose()

    source_id = _stable_id(snapshot.project_id, "source", "0")
    current_version_id = _stable_id(snapshot.project_id, "version", "0")
    old_version_id = _stable_id(snapshot.project_id, "source", "0", "older-version")
    async with SessionLocal() as session:
        current_profiles = (
            await session.scalars(
                select(TreeRoutingProfile).where(
                    TreeRoutingProfile.project_id == snapshot.project_id,
                    TreeRoutingProfile.tree_version == candidate.tree_version,
                    TreeRoutingProfile.source_id == source_id,
                    TreeRoutingProfile.document_version_id == current_version_id,
                    TreeRoutingProfile.partition_id == "partition-public",
                )
            )
        ).all()
        assert current_profiles
        for current in current_profiles:
            stale_profile = {
                "project_id": current.project_id,
                "tenant_id": current.tenant_id,
                "source_id": current.source_id,
                "document_version_id": old_version_id,
                "partition_id": current.partition_id,
                "node_id": current.node_id,
                "parent_id": current.parent_id,
                "is_leaf": current.is_leaf,
                "accessible_unit_count": current.accessible_unit_count,
                "sparse": [["beta", 1_000_000.0]],
                "entities": ["Beta"],
            }
            session.add(
                TreeRoutingProfile(
                    project_id=stale_profile["project_id"],
                    tree_version=candidate.tree_version,
                    source_id=stale_profile["source_id"],
                    document_version_id=stale_profile["document_version_id"],
                    partition_id=stale_profile["partition_id"],
                    node_id=stale_profile["node_id"],
                    tenant_id=stale_profile["tenant_id"],
                    parent_id=stale_profile["parent_id"],
                    is_leaf=stale_profile["is_leaf"],
                    accessible_unit_count=stale_profile["accessible_unit_count"],
                    sparse_json=stale_profile["sparse"],
                    entities_json=stale_profile["entities"],
                    profile_checksum=_scoped_profile_checksum(candidate.tree_version, stale_profile),
                )
            )
        await session.commit()

    scope = {
        "project_id": snapshot.project_id,
        "source_ids": [source_id],
        "document_version_ids": [current_version_id],
        "tenant_id": "tenant-c",
        "partition_id": "partition-public",
    }
    pinned = await EngineManager(settings).get_routing_snapshot(query="alpha", scopes=[scope], planner={})
    assert pinned is not None and pinned.groups
    assert max(profile.signal_scores["sparse"] for profile in pinned.groups[0].profiles) == 1.0


@pytest.mark.asyncio
async def test_live_request_lease_prevents_reusing_its_inactive_slot():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    versions = [_snapshot(project_id, variant=index) for index in range(3)]
    await _seed_search_units(versions[0])
    shared_points: dict[str, dict] = {}
    first_client = _qdrant_client(
        prepare_tree_candidate(versions[0], _assignments(versions[0])), shared_points=shared_points
    )
    await publish_tree_candidate(
        versions[0], search_unit_assignments=_assignments(versions[0]), qdrant_client=first_client
    )

    scope = {
        "project_id": project_id,
        "source_ids": [_stable_id(project_id, "source", "0")],
        "document_version_ids": [_stable_id(project_id, "version", "0")],
        "tenant_id": "tenant-c",
        "partition_id": "partition-public",
    }
    second_scope = {
        **scope,
        "source_ids": [_stable_id(project_id, "source", "1")],
        "document_version_ids": [_stable_id(project_id, "version", "1")],
    }
    manager = EngineManager(settings)
    pinned_ready = asyncio.Event()
    finish_request = asyncio.Event()

    async def in_flight_request():
        snapshot = await manager.get_routing_snapshot(
            query="alpha", scopes=[scope, second_scope], planner={}
        )
        pinned_ready.set()
        await finish_request.wait()
        return snapshot

    request_task = asyncio.create_task(in_flight_request())
    await pinned_ready.wait()
    pinned = await manager.get_routing_snapshot(query="alpha", scopes=[scope, second_scope], planner={})
    assert pinned is not None
    assert len(pinned.groups) == 2
    assert {group.scope_fingerprint for group in pinned.groups} == {
        scope_fingerprint(scope),
        scope_fingerprint(second_scope),
    }
    assert {group.routing_slot for group in pinned.groups} == {"SLOT_A"}
    assert {group.tree_version for group in pinned.groups} == {
        prepare_tree_candidate(versions[0], _assignments(versions[0])).tree_version
    }
    assert {group.search_epoch for group in pinned.groups} == {2}
    assert {group.captured_at for group in pinned.groups} == {pinned.captured_at}
    assert all(group.profiles for group in pinned.groups)
    assert all(max(profile.accessible_unit_count for profile in group.profiles) == 2 for group in pinned.groups)
    by_scope = {group.scope_fingerprint: group for group in pinned.groups}
    assert max(profile.signal_scores["sparse"] for profile in by_scope[scope_fingerprint(scope)].profiles) == 1.0
    assert max(profile.signal_scores["sparse"] for profile in by_scope[scope_fingerprint(second_scope)].profiles) == 0.0

    second_client = _qdrant_client(
        prepare_tree_candidate(versions[1], _assignments(versions[1])), shared_points=shared_points
    )
    await publish_tree_candidate(
        versions[1], search_unit_assignments=_assignments(versions[1]), qdrant_client=second_client
    )
    assert all(payload.get("tree_version_a") == pinned.groups[0].tree_version for payload in shared_points.values())

    third_candidate = prepare_tree_candidate(versions[2], _assignments(versions[2]))
    blocked_client = _qdrant_client(third_candidate, shared_points=shared_points)
    with pytest.raises(TreeSlotInUse):
        await publish_tree_candidate(
            versions[2], search_unit_assignments=_assignments(versions[2]), qdrant_client=blocked_client
        )
    await blocked_client.aclose()

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        assert state is not None
        assert state.active_routing_slot == "SLOT_B"
        assert state.active_tree_version == prepare_tree_candidate(versions[1], _assignments(versions[1])).tree_version
        lease = await session.scalar(
            select(TreeSnapshotLease).where(TreeSnapshotLease.request_snapshot_id == pinned.snapshot_id)
        )
        assert lease is not None

    await manager.release_routing_snapshot(pinned.snapshot_id)
    finish_request.set()
    in_flight_snapshot = await request_task
    assert in_flight_snapshot is not None
    assert all(group.tree_version == pinned.groups[0].tree_version for group in in_flight_snapshot.groups)
    await manager.release_routing_snapshot(in_flight_snapshot.snapshot_id)
    stale_point_id = "orphaned-search-unit-point"
    shared_points[stale_point_id] = {
        "project_id": project_id,
        "tenant_id": "tenant-c",
        "tree_version_a": in_flight_snapshot.groups[0].tree_version,
        "primary_node_a": "stale-leaf",
        "secondary_node_ids_a": [],
        "tree_version_b": prepare_tree_candidate(versions[1], _assignments(versions[1])).tree_version,
        "primary_node_b": "previous-slot-b-leaf",
    }
    retry_client = _qdrant_client(third_candidate, shared_points=shared_points)
    await publish_tree_candidate(
        versions[2], search_unit_assignments=_assignments(versions[2]), qdrant_client=retry_client
    )
    await retry_client.aclose()
    await first_client.aclose()
    await second_client.aclose()

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        assert state is not None
        assert state.active_routing_slot == "SLOT_A"
        assert state.active_tree_version == third_candidate.tree_version
        assert state.active_search_epoch == 4
    assert all(
        payload.get("tree_version_a") == third_candidate.tree_version
        for point_id, payload in shared_points.items()
        if point_id != stale_point_id
    )
    assert "tree_version_a" not in shared_points[stale_point_id]
    assert shared_points[stale_point_id]["tree_version_b"] == prepare_tree_candidate(
        versions[1], _assignments(versions[1])
    ).tree_version
    assert all(
        payload.get("tree_version_b") == prepare_tree_candidate(versions[1], _assignments(versions[1])).tree_version
        for payload in shared_points.values()
    )
