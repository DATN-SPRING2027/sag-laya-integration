from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models.document import Document
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    ProjectSearchState,
    SearchUnit,
    TreeManifest,
    TreeRoutingProfile,
    TreeSnapshotLease,
)
from sag_api.db.models.source import Source
from sag_api.sag.engine_manager import EngineManager
from sag_api.services.query_routing_service import scope_fingerprint
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    RoutingBenchmarkCase,
    TreeBuildConfig,
    build_routing_snapshot,
)
from sag_api.services.tree_publish_service import (
    PublishVerificationError,
    SearchUnitAssignment,
    TreePublishError,
    TreeSlotInUse,
    _scoped_profile_checksum,
    build_inactive_slot_payloads,
    prepare_tree_candidate,
    publish_tree_candidate,
)


def _snapshot(project_id: str, variant: int = 0):
    units = [
        KnowledgeUnitInput(
            unit_id=f"ku-{index:02d}",
            tenant_id="tenant-c",
            project_id=project_id,
            security_partition_id="partition-public" if index < 4 else "partition-private",
            dense=(1.0, float(index < 4), float(variant if index == 0 else 0)),
            sparse=((("alpha", "alpha", "gamma", "gamma", "beta", "beta", "delta", "delta")[index],
                     1.0 + variant if index == 0 else 1.0),),
            entities=(("Alpha thông tin", "Alpha", "Gamma", "Gamma", "Beta", "Beta", "Delta", "Delta")[index],),
        )
        for index in range(8)
    ]
    edges = [KnowledgeEdgeInput(f"ku-{i:02d}", f"ku-{i + 1:02d}", 0.8) for i in range(7)]
    benchmark = [RoutingBenchmarkCase("query-1", "ku-00", ("ku-00", "ku-01"))]
    return build_routing_snapshot(
        units,
        edges,
        config=TreeBuildConfig(target_cluster_size=4, max_cluster_size=4, max_children=4),
        benchmark=benchmark,
    )


def _stable_id(*parts: str) -> str:
    return sha256(":".join(parts).encode()).hexdigest()[:32]


def _assignments(snapshot) -> tuple[SearchUnitAssignment, ...]:
    result = []
    for unit in snapshot.manifest["units"]:
        index = int(unit["unit_id"][-2:])
        source_number = index % 4 // 2
        source_id = _stable_id(snapshot.project_id, "source", str(source_number))
        result.append(
            SearchUnitAssignment(
                knowledge_unit_id=unit["unit_id"],
                search_unit_id=_stable_id(snapshot.project_id, "search-unit", str(index)),
                source_id=source_id,
                document_version_id=_stable_id(snapshot.project_id, "version", str(source_number)),
                partition_id=unit["partition"],
            )
        )
    return tuple(result)


async def _seed_search_units(snapshot) -> None:
    project_id = snapshot.project_id
    assignments = _assignments(snapshot)
    async with SessionLocal() as session:
        for source_number in range(2):
            source_id = _stable_id(project_id, "source", str(source_number))
            document_id = _stable_id(project_id, "document", str(source_number))
            version_id = _stable_id(project_id, "version", str(source_number))
            block_id = _stable_id(project_id, "block", str(source_number))
            session.add(Source(id=source_id, name="Checkpoint C fixture", sag_source_config_id=source_id))
            await session.flush()
            session.add(
                Document(
                    id=document_id,
                    source_id=source_id,
                    tenant_id="tenant-c",
                    project_id=project_id,
                    filename="fixture.txt",
                    storage_path="fixture.txt",
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    version_no=1,
                    file_hash="0" * 64,
                )
            )
            await session.flush()
            session.add(
                CanonicalBlock(
                    id=block_id,
                    document_version_id=version_id,
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="fixture",
                    normalized_text="fixture",
                    content_hash="0" * 64,
                )
            )
            await session.flush()
        for item in assignments:
            source_number = 0 if item.source_id == _stable_id(project_id, "source", "0") else 1
            block_id = _stable_id(project_id, "block", str(source_number))
            session.add(
                SearchUnit(
                    id=item.search_unit_id,
                    document_version_id=item.document_version_id,
                    block_from_id=block_id,
                    block_to_id=block_id,
                    security_partition_id=item.partition_id,
                    content_hash="0" * 64,
                    token_count=1,
                    page_from=1,
                    page_to=1,
                    section_path="fixture",
                )
            )
        await session.commit()


def _qdrant_client(
    candidate,
    *,
    wrong_count: bool = False,
    acknowledged_only: bool = False,
    wrong_source_payload: bool = False,
    seen_writes: list[dict] | None = None,
    shared_points: dict[str, dict] | None = None,
):
    from sag_api.services.search_index_service import generate_search_unit_point_id

    collection = f"search_units_{candidate.project_id}"
    initial_points = {
        generate_search_unit_point_id(collection, item.search_unit_id): {
            "search_unit_id": item.search_unit_id,
            "source_id": item.source_id,
            "document_version_id": item.document_version_id,
            "project_id": candidate.project_id,
            "tenant_id": candidate.tenant_id,
            "security_partition_id": item.partition_id,
        }
        for item in candidate.search_unit_assignments
    }
    points = shared_points if shared_points is not None else {}
    for point_id, payload in initial_points.items():
        points.setdefault(point_id, payload)
    if wrong_source_payload:
        next(iter(points.values()))["source_id"] = "wrong-source"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/payload/delete"):
            if request.url.params.get("wait") != "true":
                return httpx.Response(400, json={"status": "error"})
            body = json.loads(request.content)
            filters = body["filter"]["must"]
            for payload in points.values():
                if all(payload.get(item["key"]) == item["match"]["value"] for item in filters):
                    for key in body["keys"]:
                        payload.pop(key, None)
            return httpx.Response(
                200,
                json={"status": "ok", "result": {"status": "completed", "operation_id": 1}},
            )
        if request.url.path.endswith("/points/payload"):
            if request.url.params.get("wait") != "true":
                return httpx.Response(400, json={"status": "error"})
            body = json.loads(request.content)
            if seen_writes is not None:
                seen_writes.append(body)
            for point_id in body["points"]:
                points[point_id].update(body["payload"])
            status = "acknowledged" if acknowledged_only else "completed"
            return httpx.Response(200, json={"status": "ok", "result": {"status": status, "operation_id": 1}})
        if request.url.path.endswith("/points/count"):
            body = json.loads(request.content)
            filters = body["filter"]["must"]
            count = sum(
                all(payload.get(item["key"]) == item["match"]["value"] for item in filters)
                for payload in points.values()
            )
            if wrong_count:
                count -= 1
            return httpx.Response(200, json={"status": "ok", "result": {"count": count}})
        if request.url.path.endswith("/points"):
            body = json.loads(request.content)
            result = [
                {"id": point_id, "payload": points[point_id]}
                for point_id in body["ids"]
                if point_id in points
            ]
            return httpx.Response(200, json={"status": "ok", "result": result})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")


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
