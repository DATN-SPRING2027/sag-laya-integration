from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from checkpoint_c_test_support import (
    _assignments,
    _public_scope,
    _qdrant_client,
    _seed_search_units,
    _snapshot,
)
from sqlalchemy import event, select
from sqlalchemy.orm import Session as SyncSession

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models.routing_rag import ProjectSearchState, TreeManifest, TreeRoutingProfile, TreeSnapshotLease
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.search_unit_store import SearchUnitQdrantStore
from sag_api.services import tree_publish_service as publish_service
from sag_api.services.query_routing_service import route_snapshot
from sag_api.services.search_unit_retrieval_service import _query_group, _SearchGroup
from sag_api.services.tree_publish_service import (
    PublishVerificationError,
    TreePublishError,
    TreeSlotInUse,
    build_inactive_slot_payloads,
    prepare_tree_candidate,
    publish_tree_candidate,
)
from sag_api.services.tree_rollback_service import rollback_tree_candidate


async def _assert_pinned_read(snapshot, client):
    for group in snapshot.groups:
        decision = route_snapshot(group)
        assert decision.routed
        _, semantic, lexical = await _query_group(
            _SearchGroup(
                project_id=group.project_id,
                tenant_id=group.tenant_id,
                partition_id=group.partition_id,
                source=SimpleNamespace(id=group.source_ids[0]),
                versions=group.document_version_ids,
            ),
            query="alpha",
            limit=10,
            store=SearchUnitQdrantStore(client),
            query_vector=[1.0, 1.0, 0.0],
            route=decision,
        )
        assert semantic and lexical
        slot = "a" if group.routing_slot == "SLOT_A" else "b"
        for hit in [*semantic, *lexical]:
            assert hit.payload[f"tree_version_{slot}"] == group.tree_version
            assert hit.payload["source_id"] in group.source_ids
            assert hit.payload["document_version_id"] in group.document_version_ids
            assert hit.payload["security_partition_id"] == group.partition_id


@pytest.mark.asyncio
async def test_quality_gate_failure_keeps_previous_query_snapshot():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    active = _snapshot(project_id, variant=0)
    rejected = _snapshot(project_id, variant=1)
    await _seed_search_units(active)
    shared_points: dict[str, dict] = {}
    active_client = _qdrant_client(prepare_tree_candidate(active, _assignments(active)), shared_points=shared_points)
    await publish_tree_candidate(active, search_unit_assignments=_assignments(active), qdrant_client=active_client)

    manager = EngineManager(settings)
    pinned = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(active)], planner={})
    assert pinned is not None
    active_group = pinned.groups[0]
    invalid_quality = replace(rejected, publishable=False, quality_gates={"injected": False})
    rejected_client = _qdrant_client(
        prepare_tree_candidate(rejected, _assignments(rejected)), shared_points=shared_points
    )
    with pytest.raises(PublishVerificationError, match="quality gate"):
        await publish_tree_candidate(
            invalid_quality,
            search_unit_assignments=_assignments(invalid_quality),
            qdrant_client=rejected_client,
        )

    still_active = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(active)], planner={})
    assert still_active is not None
    assert (still_active.groups[0].tree_version, still_active.groups[0].routing_slot) == (
        active_group.tree_version,
        active_group.routing_slot,
    )
    await _assert_pinned_read(pinned, active_client)
    await _assert_pinned_read(still_active, active_client)
    await manager.release_routing_snapshot(pinned.snapshot_id)
    await manager.release_routing_snapshot(still_active.snapshot_id)
    await rejected_client.aclose()
    await active_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["manifest", "checksum", "count"])
async def test_verification_failure_keeps_previous_tree(monkeypatch, corruption):
    await init_db()
    project_id = f"project-{uuid4().hex}"
    active = _snapshot(project_id, variant=0)
    candidate_snapshot = _snapshot(project_id, variant=1)
    await _seed_search_units(active)
    shared_points: dict[str, dict] = {}
    active_candidate = prepare_tree_candidate(active, _assignments(active))
    active_client = _qdrant_client(active_candidate, shared_points=shared_points)
    await publish_tree_candidate(active, search_unit_assignments=_assignments(active), qdrant_client=active_client)
    before_candidate = prepare_tree_candidate(candidate_snapshot, _assignments(candidate_snapshot))
    before_client = _qdrant_client(before_candidate, shared_points=shared_points, wrong_count=corruption == "count")
    original_verify = publish_service._verify_staged_candidate

    async def corrupt_staged(candidate):
        async with SessionLocal() as session:
            record = await session.get(TreeManifest, candidate.tree_version)
            assert record is not None
            if corruption == "manifest":
                record.manifest_json = {
                    **record.manifest_json,
                    "expected_point_count": record.manifest_json["expected_point_count"] + 1,
                }
            else:
                record.checksum = "f" * 64
            await session.commit()
        await original_verify(candidate)

    if corruption != "count":
        monkeypatch.setattr(publish_service, "_verify_staged_candidate", corrupt_staged)
    with pytest.raises(PublishVerificationError):
        await publish_tree_candidate(
            candidate_snapshot,
            search_unit_assignments=_assignments(candidate_snapshot),
            qdrant_client=before_client,
        )

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        failed_record = await session.get(TreeManifest, before_candidate.tree_version)
        active_record = await session.get(TreeManifest, active_candidate.tree_version)
        assert state is not None
        assert state.active_tree_version == active_candidate.tree_version
        assert state.active_routing_slot == "SLOT_A"
        assert state.active_search_epoch == 2
        assert state.slot_b_tree_version is None
        assert failed_record is not None and failed_record.status == "REJECTED"
        assert active_record is not None and active_record.status == "ACTIVE"

    manager = EngineManager(settings)
    pinned = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(active)], planner={})
    assert pinned is not None
    assert all(group.tree_version == active_candidate.tree_version for group in pinned.groups)
    await _assert_pinned_read(pinned, active_client)
    await manager.release_routing_snapshot(pinned.snapshot_id)
    await before_client.aclose()
    await active_client.aclose()


@pytest.mark.asyncio
async def test_partial_qdrant_batch_write_keeps_active_slot_queryable():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    active = _snapshot(project_id, variant=0)
    current = _snapshot(project_id, variant=1)
    candidate_snapshot = _snapshot(project_id, variant=2)
    await _seed_search_units(active)
    shared_points: dict[str, dict] = {}
    active_candidate = prepare_tree_candidate(active, _assignments(active))
    active_client = _qdrant_client(active_candidate, shared_points=shared_points)
    await publish_tree_candidate(active, search_unit_assignments=_assignments(active), qdrant_client=active_client)

    current_candidate = prepare_tree_candidate(current, _assignments(current))
    current_client = _qdrant_client(current_candidate, shared_points=shared_points)
    await publish_tree_candidate(current, search_unit_assignments=_assignments(current), qdrant_client=current_client)
    candidate = prepare_tree_candidate(candidate_snapshot, _assignments(candidate_snapshot))
    assert len(build_inactive_slot_payloads(candidate, "SLOT_A")) > 1
    partial_client = _qdrant_client(candidate, shared_points=shared_points, fail_write_number=2)
    with pytest.raises(PublishVerificationError, match="error status"):
        await publish_tree_candidate(
            candidate_snapshot,
            search_unit_assignments=_assignments(candidate_snapshot),
            qdrant_client=partial_client,
        )

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        failed_record = await session.get(TreeManifest, candidate.tree_version)
        assert state is not None
        assert state.active_tree_version == current_candidate.tree_version
        assert state.active_routing_slot == "SLOT_B"
        assert state.active_search_epoch == 3
        assert state.previous_tree_version is None
        assert state.slot_a_tree_version is None
        assert state.slot_b_tree_version == current_candidate.tree_version
        assert failed_record is not None and failed_record.status == "REJECTED"
    assert all(payload.get("tree_version_b") == current_candidate.tree_version for payload in shared_points.values())

    manager = EngineManager(settings)
    pinned = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(current)], planner={})
    assert pinned is not None
    assert all(group.tree_version == current_candidate.tree_version for group in pinned.groups)
    assert all(group.routing_slot == "SLOT_B" for group in pinned.groups)
    await _assert_pinned_read(pinned, current_client)
    with pytest.raises(TreePublishError, match="No retained previous tree"):
        await rollback_tree_candidate(project_id, target_tree_version="unavailable", qdrant_client=partial_client)
    await manager.release_routing_snapshot(pinned.snapshot_id)
    await partial_client.aclose()
    await current_client.aclose()
    await active_client.aclose()


@pytest.mark.asyncio
async def test_atomic_switch_failure_rolls_back_and_rejects_candidate():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    active = _snapshot(project_id, variant=0)
    candidate_snapshot = _snapshot(project_id, variant=1)
    await _seed_search_units(active)
    shared_points: dict[str, dict] = {}
    active_candidate = prepare_tree_candidate(active, _assignments(active))
    active_client = _qdrant_client(active_candidate, shared_points=shared_points)
    await publish_tree_candidate(active, search_unit_assignments=_assignments(active), qdrant_client=active_client)

    candidate = prepare_tree_candidate(candidate_snapshot, _assignments(candidate_snapshot))
    candidate_client = _qdrant_client(candidate, shared_points=shared_points)
    injection = {"armed": True}

    def fail_atomic_switch_commit(sync_session):
        if injection["armed"] and any(
            isinstance(row, ProjectSearchState) and row.active_tree_version == candidate.tree_version
            for row in sync_session.dirty
        ):
            injection["armed"] = False
            raise RuntimeError("injected atomic pointer commit failure")

    event.listen(SyncSession, "before_commit", fail_atomic_switch_commit)
    try:
        with pytest.raises(RuntimeError, match="injected atomic pointer commit failure"):
            await publish_tree_candidate(
                candidate_snapshot,
                search_unit_assignments=_assignments(candidate_snapshot),
                qdrant_client=candidate_client,
            )
    finally:
        event.remove(SyncSession, "before_commit", fail_atomic_switch_commit)

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        failed_record = await session.get(TreeManifest, candidate.tree_version)
        active_record = await session.get(TreeManifest, active_candidate.tree_version)
        assert state is not None
        assert state.active_tree_version == active_candidate.tree_version
        assert state.active_routing_slot == "SLOT_A"
        assert state.active_search_epoch == 2
        assert state.slot_b_tree_version is None
        assert failed_record is not None and failed_record.status == "REJECTED"
        assert active_record is not None and active_record.status == "ACTIVE"

    manager = EngineManager(settings)
    pinned = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(active)], planner={})
    assert pinned is not None
    assert all(group.tree_version == active_candidate.tree_version for group in pinned.groups)
    await _assert_pinned_read(pinned, active_client)
    await manager.release_routing_snapshot(pinned.snapshot_id)
    await candidate_client.aclose()
    await active_client.aclose()


@pytest.mark.asyncio
async def test_rollback_restores_matching_postgres_manifest_and_qdrant_slot():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    previous = _snapshot(project_id, variant=0)
    current = _snapshot(project_id, variant=1)
    await _seed_search_units(previous)
    shared_points: dict[str, dict] = {}
    previous_candidate = prepare_tree_candidate(previous, _assignments(previous))
    previous_client = _qdrant_client(previous_candidate, shared_points=shared_points)
    await publish_tree_candidate(
        previous, search_unit_assignments=_assignments(previous), qdrant_client=previous_client
    )
    current_candidate = prepare_tree_candidate(current, _assignments(current))
    current_client = _qdrant_client(current_candidate, shared_points=shared_points)
    await publish_tree_candidate(current, search_unit_assignments=_assignments(current), qdrant_client=current_client)

    result = await rollback_tree_candidate(
        project_id, target_tree_version=previous_candidate.tree_version, qdrant_client=current_client
    )
    retry_result = await rollback_tree_candidate(
        project_id, target_tree_version=previous_candidate.tree_version, qdrant_client=current_client
    )
    assert retry_result == result
    assert (result.tree_version, result.routing_slot, result.search_epoch) == (
        previous_candidate.tree_version,
        "SLOT_A",
        4,
    )
    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        previous_record = await session.get(TreeManifest, previous_candidate.tree_version)
        current_record = await session.get(TreeManifest, current_candidate.tree_version)
        assert state is not None
        assert state.active_tree_version == previous_candidate.tree_version
        assert state.active_routing_slot == "SLOT_A"
        assert state.slot_a_tree_version == previous_candidate.tree_version
        assert state.slot_b_tree_version == current_candidate.tree_version
        assert state.previous_tree_version == current_candidate.tree_version
        assert state.active_search_epoch == 4
        assert previous_record is not None and previous_record.status == "ACTIVE"
        assert previous_record.checksum == previous_candidate.checksum
        assert current_record is not None and current_record.status == "INACTIVE"
    assert all(
        payload.get("tree_version_a") == previous_candidate.tree_version
        and payload.get("tree_version_b") == current_candidate.tree_version
        for payload in shared_points.values()
    )

    manager = EngineManager(settings)
    restored = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(previous)], planner={})
    assert restored is not None
    assert all(group.tree_version == previous_candidate.tree_version for group in restored.groups)
    assert all(group.routing_slot == "SLOT_A" and group.search_epoch == 4 for group in restored.groups)
    await _assert_pinned_read(restored, current_client)
    await manager.release_routing_snapshot(restored.snapshot_id)
    await previous_client.aclose()
    await current_client.aclose()


@pytest.mark.asyncio
async def test_rollback_rejects_target_that_is_not_the_retained_version():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    previous = _snapshot(project_id, variant=0)
    current = _snapshot(project_id, variant=1)
    await _seed_search_units(previous)
    shared_points: dict[str, dict] = {}
    previous_candidate = prepare_tree_candidate(previous, _assignments(previous))
    previous_client = _qdrant_client(previous_candidate, shared_points=shared_points)
    await publish_tree_candidate(
        previous, search_unit_assignments=_assignments(previous), qdrant_client=previous_client
    )
    current_candidate = prepare_tree_candidate(current, _assignments(current))
    current_client = _qdrant_client(current_candidate, shared_points=shared_points)
    await publish_tree_candidate(current, search_unit_assignments=_assignments(current), qdrant_client=current_client)

    with pytest.raises(TreePublishError, match="not the retained previous tree"):
        await rollback_tree_candidate(
            project_id, target_tree_version="tree-not-retained", qdrant_client=current_client
        )

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        assert state is not None
        assert state.active_tree_version == current_candidate.tree_version
        assert state.active_routing_slot == "SLOT_B"
        assert state.previous_tree_version == previous_candidate.tree_version
        assert state.active_search_epoch == 3
    await previous_client.aclose()
    await current_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["qdrant", "checksum", "profile"])
async def test_rollback_verification_failure_keeps_current_tree_active(corruption):
    await init_db()
    project_id = f"project-{uuid4().hex}"
    previous = _snapshot(project_id, variant=0)
    current = _snapshot(project_id, variant=1)
    await _seed_search_units(previous)
    shared_points: dict[str, dict] = {}
    previous_candidate = prepare_tree_candidate(previous, _assignments(previous))
    previous_client = _qdrant_client(previous_candidate, shared_points=shared_points)
    await publish_tree_candidate(
        previous, search_unit_assignments=_assignments(previous), qdrant_client=previous_client
    )
    current_candidate = prepare_tree_candidate(current, _assignments(current))
    current_client = _qdrant_client(current_candidate, shared_points=shared_points)
    await publish_tree_candidate(current, search_unit_assignments=_assignments(current), qdrant_client=current_client)
    if corruption == "qdrant":
        next(iter(shared_points.values()))["tree_version_a"] = "corrupted-retained-slot"
    else:
        async with SessionLocal() as session:
            if corruption == "checksum":
                record = await session.get(TreeManifest, previous_candidate.tree_version)
                record.checksum = "f" * 64
            else:
                record = await session.scalar(
                    select(TreeRoutingProfile).where(TreeRoutingProfile.tree_version == previous_candidate.tree_version)
                )
                record.sparse_json = [["tampered", 1.0]]
            await session.commit()

    with pytest.raises(PublishVerificationError):
        await rollback_tree_candidate(
            project_id, target_tree_version=previous_candidate.tree_version, qdrant_client=current_client
        )

    async with SessionLocal() as session:
        state = await session.get(ProjectSearchState, project_id)
        previous_record = await session.get(TreeManifest, previous_candidate.tree_version)
        current_record = await session.get(TreeManifest, current_candidate.tree_version)
        assert state is not None
        assert state.active_tree_version == current_candidate.tree_version
        assert state.active_routing_slot == "SLOT_B"
        assert state.active_search_epoch == 3
        assert state.previous_tree_version == previous_candidate.tree_version
        assert previous_record is not None and previous_record.status == "INACTIVE"
        assert current_record is not None and current_record.status == "ACTIVE"

    manager = EngineManager(settings)
    still_active = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(current)], planner={})
    assert still_active is not None
    assert all(group.tree_version == current_candidate.tree_version for group in still_active.groups)
    assert all(group.routing_slot == "SLOT_B" for group in still_active.groups)
    await _assert_pinned_read(still_active, current_client)
    await manager.release_routing_snapshot(still_active.snapshot_id)
    await previous_client.aclose()
    await current_client.aclose()


@pytest.mark.asyncio
async def test_concurrent_requests_keep_one_snapshot_across_publish_and_rollback():
    await init_db()
    project_id = f"project-{uuid4().hex}"
    previous = _snapshot(project_id, variant=0)
    current = _snapshot(project_id, variant=1)
    await _seed_search_units(previous)
    shared_points: dict[str, dict] = {}
    previous_candidate = prepare_tree_candidate(previous, _assignments(previous))
    resume_reads = asyncio.Event()
    old_query_started = asyncio.Event()
    previous_client = _qdrant_client(
        previous_candidate, shared_points=shared_points, query_pause=resume_reads, query_started=old_query_started
    )
    await publish_tree_candidate(
        previous, search_unit_assignments=_assignments(previous), qdrant_client=previous_client
    )
    manager = EngineManager(settings)
    before_publish = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(previous)], planner={})
    assert before_publish is not None
    old_read = asyncio.create_task(_assert_pinned_read(before_publish, previous_client))
    await asyncio.wait_for(old_query_started.wait(), timeout=5)

    current_candidate = prepare_tree_candidate(current, _assignments(current))
    current_query_started = asyncio.Event()
    current_client = _qdrant_client(
        current_candidate, shared_points=shared_points, query_pause=resume_reads, query_started=current_query_started
    )
    await publish_tree_candidate(current, search_unit_assignments=_assignments(current), qdrant_client=current_client)
    before_rollback = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(previous)], planner={})
    assert before_rollback is not None
    new_read = asyncio.create_task(_assert_pinned_read(before_rollback, current_client))
    await asyncio.wait_for(current_query_started.wait(), timeout=5)
    await rollback_tree_candidate(
        project_id, target_tree_version=previous_candidate.tree_version, qdrant_client=current_client
    )
    after_rollback = await manager.get_routing_snapshot(query="alpha", scopes=[_public_scope(previous)], planner={})
    assert after_rollback is not None

    assert {(group.tree_version, group.routing_slot, group.search_epoch) for group in before_publish.groups} == {
        (previous_candidate.tree_version, "SLOT_A", 2)
    }
    assert {(group.tree_version, group.routing_slot, group.search_epoch) for group in before_rollback.groups} == {
        (current_candidate.tree_version, "SLOT_B", 3)
    }
    assert {(group.tree_version, group.routing_slot, group.search_epoch) for group in after_rollback.groups} == {
        (previous_candidate.tree_version, "SLOT_A", 4)
    }

    # Both in-flight leases remain valid while the active pointer has moved twice.
    assert all(group.tree_version == previous_candidate.tree_version for group in before_publish.groups)
    assert all(group.tree_version == current_candidate.tree_version for group in before_rollback.groups)
    async with SessionLocal() as session:
        leases = (
            await session.scalars(
                select(TreeSnapshotLease).where(
                    TreeSnapshotLease.request_snapshot_id.in_([before_publish.snapshot_id, before_rollback.snapshot_id])
                )
            )
        ).all()
        assert {(lease.routing_slot, lease.tree_version, lease.search_epoch) for lease in leases} == {
            ("SLOT_A", previous_candidate.tree_version, 2),
            ("SLOT_B", current_candidate.tree_version, 3),
        }

    # After rollback, the SLOT_B lease still protects the newer in-flight query.
    third = _snapshot(project_id, variant=2)
    with pytest.raises(TreeSlotInUse):
        await publish_tree_candidate(third, search_unit_assignments=_assignments(third), qdrant_client=current_client)
    resumed_read = asyncio.create_task(_assert_pinned_read(after_rollback, previous_client))
    resume_reads.set()
    await asyncio.gather(old_read, new_read, resumed_read)
    await manager.release_routing_snapshot(before_publish.snapshot_id)
    await manager.release_routing_snapshot(before_rollback.snapshot_id)
    await manager.release_routing_snapshot(after_rollback.snapshot_id)
    await previous_client.aclose()
    await current_client.aclose()
