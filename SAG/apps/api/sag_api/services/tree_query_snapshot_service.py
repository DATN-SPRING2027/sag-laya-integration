"""Request-scoped routing snapshot capture backed by durable slot leases."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import load_only

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.db.models.routing_rag import (
    ProjectSearchState,
    TreeManifest,
    TreeRoutingProfile,
    TreeSnapshotLease,
)
from sag_api.services.query_routing_service import (
    GroupRoutingSnapshot,
    scope_fingerprint,
)
from sag_api.services.query_routing_service import (
    NodeProfile as QueryNodeProfile,
)
from sag_api.services.query_routing_service import (
    RoutingSnapshot as QueryRoutingSnapshot,
)
from sag_api.services.tree_publish_service import _database_now, _scoped_profile_checksum

_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


def _query_signal_scores(query: str, profile: dict[str, Any]) -> dict[str, float]:
    query_terms = set(_TOKEN_PATTERN.findall(query.casefold()))
    sparse = profile.get("sparse")
    weights: list[tuple[str, float]] = []
    if isinstance(sparse, list):
        for item in sparse:
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], (int, float))
                and not isinstance(item[1], bool)
                and math.isfinite(float(item[1]))
                and float(item[1]) >= 0.0
            ):
                weights.append((item[0].casefold(), float(item[1])))
    total = math.fsum(weight for _term, weight in weights)
    sparse_score = (
        min(1.0, math.fsum(weight for term, weight in weights if term in query_terms) / total)
        if total > 0.0
        else 0.0
    )
    scores = {"sparse": sparse_score}
    entities = profile.get("entities")
    if isinstance(entities, list) and all(isinstance(entity, str) for entity in entities):
        folded_query = query.casefold()
        scores["entity"] = 1.0 if any(entity.casefold() in folded_query for entity in entities) else 0.0
    return scores


def _source_profile_payload(record: TreeRoutingProfile) -> dict[str, Any]:
    return {
        "project_id": record.project_id,
        "tenant_id": record.tenant_id,
        "source_id": record.source_id,
        "document_version_id": record.document_version_id,
        "partition_id": record.partition_id,
        "node_id": record.node_id,
        "parent_id": record.parent_id,
        "is_leaf": record.is_leaf,
        "accessible_unit_count": record.accessible_unit_count,
        "sparse": record.sparse_json,
        "entities": record.entities_json,
    }


def _aggregate_source_profiles(
    records: list[TreeRoutingProfile],
    *,
    project_id: str,
    tenant_id: str,
    tree_version: str,
    source_ids: tuple[str, ...],
    partition_id: str,
    document_version_ids: tuple[str, ...],
    fingerprint: str,
    query: str,
) -> tuple[QueryNodeProfile, ...] | None:
    by_node: dict[str, dict[str, Any]] = {}
    for record in records:
        profile = _source_profile_payload(record)
        if (
            record.project_id != project_id
            or record.tenant_id != tenant_id
            or record.tree_version != tree_version
            or record.source_id not in source_ids
            or record.document_version_id not in document_version_ids
            or record.partition_id != partition_id
            or record.profile_checksum != _scoped_profile_checksum(tree_version, profile)
            or not isinstance(record.node_id, str)
            or not record.node_id
            or (record.parent_id is not None and not isinstance(record.parent_id, str))
            or not isinstance(record.is_leaf, bool)
            or isinstance(record.accessible_unit_count, bool)
            or not isinstance(record.accessible_unit_count, int)
            or record.accessible_unit_count <= 0
            or not isinstance(record.sparse_json, list)
            or not isinstance(record.entities_json, list)
            or any(not isinstance(entity, str) for entity in record.entities_json)
        ):
            return None
        aggregate = by_node.setdefault(
            record.node_id,
            {
                "parent_id": record.parent_id,
                "is_leaf": record.is_leaf,
                "accessible_unit_count": 0,
                "sparse": defaultdict(list),
                "entities": set(),
            },
        )
        if aggregate["parent_id"] != record.parent_id or aggregate["is_leaf"] != record.is_leaf:
            return None
        aggregate["accessible_unit_count"] += record.accessible_unit_count
        for item in record.sparse_json:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], (int, float))
                or not math.isfinite(float(item[1]))
                or float(item[1]) < 0.0
            ):
                return None
            aggregate["sparse"][item[0]].append(float(item[1]))
        aggregate["entities"].update(record.entities_json)

    profiles: list[QueryNodeProfile] = []
    for node_id, aggregate in sorted(by_node.items()):
        sparse = sorted(
            ((term, math.fsum(weights)) for term, weights in aggregate["sparse"].items()),
            key=lambda item: (-item[1], item[0]),
        )
        profiles.append(
            QueryNodeProfile(
                node_id=node_id,
                parent_id=aggregate["parent_id"],
                is_leaf=aggregate["is_leaf"],
                accessible_unit_count=aggregate["accessible_unit_count"],
                project_id=project_id,
                source_ids=source_ids,
                document_version_ids=document_version_ids,
                tenant_id=tenant_id,
                partition_id=partition_id,
                tree_version=tree_version,
                scope_fingerprint=fingerprint,
                signal_scores=_query_signal_scores(
                    query,
                    {
                        "sparse": [[term, weight] for term, weight in sparse],
                        "entities": sorted(aggregate["entities"]),
                    },
                ),
            )
        )
    return tuple(profiles)


async def acquire_tree_query_snapshot(
    *,
    query: str,
    scopes: list[dict[str, object]],
    session_factory=SessionLocal,
) -> QueryRoutingSnapshot | None:
    """Capture project pointers and scope-indexed profiles with durable slot leases."""
    if not scopes or len(scopes) > 256:
        return None
    projects = sorted({str(scope.get("project_id") or "") for scope in scopes})
    if any(not project for project in projects):
        return None

    request_snapshot_id = str(uuid4())
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(ProjectSearchState, TreeManifest)
                    .options(
                        load_only(
                            TreeManifest.tree_version,
                            TreeManifest.project_id,
                            TreeManifest.status,
                            TreeManifest.checksum,
                        )
                    )
                    .join(
                        TreeManifest,
                        (TreeManifest.tree_version == ProjectSearchState.active_tree_version)
                        & (TreeManifest.project_id == ProjectSearchState.project_id),
                    )
                    .where(ProjectSearchState.project_id.in_(projects))
                    .order_by(ProjectSearchState.project_id)
                    .with_for_update(read=True, of=ProjectSearchState)
                )
            ).all()
            by_project = {state.project_id: (state, manifest_record) for state, manifest_record in rows}
            if set(by_project) != set(projects):
                return None

            scoped_requests: list[dict[str, Any]] = []
            leases: dict[str, tuple[str, str, int]] = {}
            for scope in scopes:
                project_id = str(scope["project_id"])
                state, record = by_project[project_id]
                if (
                    not state.active_tree_version
                    or state.active_routing_slot not in {"SLOT_A", "SLOT_B"}
                    or record.status != "ACTIVE"
                    or record.project_id != project_id
                    or record.tree_version != state.active_tree_version
                    or not isinstance(record.checksum, str)
                    or len(record.checksum) != 64
                    or any(character not in "0123456789abcdef" for character in record.checksum)
                    or state.active_tree_version != f"tree-{record.checksum[:24]}"
                    or isinstance(state.active_search_epoch, bool)
                    or state.active_search_epoch < 1
                ):
                    return None
                active_slot_version = (
                    state.slot_a_tree_version if state.active_routing_slot == "SLOT_A" else state.slot_b_tree_version
                )
                if active_slot_version != state.active_tree_version:
                    return None

                tenant_id = str(scope.get("tenant_id") or "")
                partition_id = str(scope.get("partition_id") or "")
                raw_source_ids = scope.get("source_ids")
                raw_version_ids = scope.get("document_version_ids")
                if (
                    not tenant_id
                    or not partition_id
                    or not isinstance(raw_source_ids, (list, tuple))
                    or not raw_source_ids
                    or any(not isinstance(value, str) or not value.strip() for value in raw_source_ids)
                    or not isinstance(raw_version_ids, (list, tuple))
                    or not raw_version_ids
                    or any(not isinstance(value, str) or not value.strip() for value in raw_version_ids)
                ):
                    return None
                fingerprint = scope_fingerprint(scope)
                source_ids = tuple(sorted(set(raw_source_ids)))
                version_ids = tuple(sorted(set(raw_version_ids)))
                scoped_requests.append(
                    {
                        "project_id": project_id,
                        "state": state,
                        "record": record,
                        "source_ids": source_ids,
                        "document_version_ids": version_ids,
                        "tenant_id": tenant_id,
                        "partition_id": partition_id,
                        "scope_fingerprint": fingerprint,
                    }
                )
                leases[project_id] = (
                    state.active_routing_slot,
                    state.active_tree_version,
                    state.active_search_epoch,
                )

            profiles_by_scope: dict[tuple[str, str, str, str, str], list[TreeRoutingProfile]] = defaultdict(list)
            total_profile_count = 0
            for project_id in projects:
                project_requests = [
                    request for request in scoped_requests if request["project_id"] == project_id
                ]
                state = by_project[project_id][0]
                scope_triples = sorted(
                    {
                        (source_id, version_id, request["partition_id"])
                        for request in project_requests
                        for source_id in request["source_ids"]
                        for version_id in request["document_version_ids"]
                    }
                )
                if not scope_triples or len(scope_triples) > settings.search_tree_profile_limit:
                    return None
                remaining_profile_count = settings.search_tree_profile_limit - total_profile_count
                if remaining_profile_count < 0:
                    return None
                scope_filter = tuple_(
                    TreeRoutingProfile.source_id,
                    TreeRoutingProfile.document_version_id,
                    TreeRoutingProfile.partition_id,
                ).in_(scope_triples)
                profile_rows = (
                    await session.scalars(
                        select(TreeRoutingProfile)
                        .where(
                            TreeRoutingProfile.project_id == project_id,
                            TreeRoutingProfile.tree_version == state.active_tree_version,
                            scope_filter,
                        )
                        .order_by(
                            TreeRoutingProfile.source_id,
                            TreeRoutingProfile.document_version_id,
                            TreeRoutingProfile.partition_id,
                            TreeRoutingProfile.node_id,
                        )
                        .limit(remaining_profile_count + 1)
                    )
                ).all()
                if len(profile_rows) > remaining_profile_count:
                    return None
                total_profile_count += len(profile_rows)
                for profile in profile_rows:
                    profiles_by_scope[
                        (
                            profile.project_id,
                            profile.tree_version,
                            profile.source_id,
                            profile.document_version_id,
                            profile.partition_id,
                        )
                    ].append(profile)

            captured_at = await _database_now(session)
            group_rows: list[GroupRoutingSnapshot] = []
            for request in scoped_requests:
                state = request["state"]
                record = request["record"]
                project_id = request["project_id"]
                source_ids = request["source_ids"]
                partition_id = request["partition_id"]
                matching_profiles = [
                    profile
                    for source_id in source_ids
                    for version_id in request["document_version_ids"]
                    for profile in profiles_by_scope.get(
                        (
                            project_id,
                            state.active_tree_version,
                            source_id,
                            version_id,
                            partition_id,
                        ),
                        [],
                    )
                ]
                if len(matching_profiles) > 1_024:
                    return None
                query_profiles = _aggregate_source_profiles(
                    matching_profiles,
                    project_id=project_id,
                    tenant_id=request["tenant_id"],
                    tree_version=state.active_tree_version,
                    source_ids=source_ids,
                    partition_id=partition_id,
                    document_version_ids=request["document_version_ids"],
                    fingerprint=request["scope_fingerprint"],
                    query=query,
                )
                if query_profiles is None:
                    return None
                group_rows.append(
                    GroupRoutingSnapshot(
                        snapshot_id=str(uuid4()),
                        captured_at=captured_at,
                        project_id=project_id,
                        source_ids=source_ids,
                        document_version_ids=request["document_version_ids"],
                        tenant_id=request["tenant_id"],
                        partition_id=partition_id,
                        scope_fingerprint=request["scope_fingerprint"],
                        tree_version=state.active_tree_version,
                        routing_slot=state.active_routing_slot,
                        search_epoch=state.active_search_epoch,
                        manifest_status=record.status,
                        manifest_checksum=record.checksum,
                        manifest_verified=True,
                        profiles=query_profiles,
                    )
                )

            database_now = await _database_now(session)
            expires_at = database_now + timedelta(
                seconds=max(30.0, float(settings.search_source_timeout) + 15.0)
            )
            for project_id, (slot, tree_version, epoch) in leases.items():
                session.add(
                    TreeSnapshotLease(
                        request_snapshot_id=request_snapshot_id,
                        project_id=project_id,
                        routing_slot=slot,
                        tree_version=tree_version,
                        search_epoch=epoch,
                        expires_at=expires_at,
                    )
                )
        return QueryRoutingSnapshot(
            snapshot_id=request_snapshot_id,
            captured_at=captured_at,
            groups=tuple(group_rows),
        )


async def release_tree_query_snapshot(request_snapshot_id: str, *, session_factory=SessionLocal) -> None:
    if not request_snapshot_id:
        return
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(TreeSnapshotLease).where(
                    TreeSnapshotLease.request_snapshot_id == request_snapshot_id
                )
            )
