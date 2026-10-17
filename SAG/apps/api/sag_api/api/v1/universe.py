from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager, get_job_queue
from sag_api.core.errors import NotFoundError, ServiceUnavailableError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import (
    ProjectSearchState,
    Source,
    SourceProjectMapping,
    TreeManifest,
    TreeRoutingProfile,
    User,
)
from sag_api.sag import EngineManager
from sag_api.schemas.job import JobOut
from sag_api.schemas.universe import (
    ExplorationDetailOut,
    ExplorationSessionOut,
    ExplorationStepOut,
    UniverseExpandIn,
    UniverseGraphPatchOut,
    UniverseManifestOut,
    UniverseNodeDetailOut,
    UniverseTimelineIn,
    UniverseTimelineSliceOut,
)
from sag_api.services.search_unit_retrieval_service import _load_current_ready_versions
from sag_api.services.source_service import get_authorized_source, get_authorized_source_ids, search_source_candidates
from sag_api.services.tree_query_snapshot_service import _aggregate_source_profiles
from sag_api.services.universe_service import (
    _universe_policy,
    get_exploration,
    list_explorations,
    universe_expand,
    universe_node_detail,
    universe_timeline,
)
from sag_api.services.universe_tree_service import enqueue_project_tree_rebuild

router = APIRouter(prefix="/universe", tags=["universe"])


def _stale_manifest() -> UniverseManifestOut:
    return UniverseManifestOut(
        status="stale",
        stale=True,
        as_of=datetime.now(UTC),
        counts={"trees": 0, "sources": 0, "nodes": 0, "units": 0, "partitions": 0},
        policy=_universe_policy(),
    )


def _failed_manifest() -> UniverseManifestOut:
    return UniverseManifestOut(
        status="failed",
        as_of=datetime.now(UTC),
        counts={"trees": 0, "sources": 0, "nodes": 0, "units": 0, "partitions": 0},
        policy=_universe_policy(),
    )


@router.get("/manifest", response_model=UniverseManifestOut)
async def manifest(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
) -> UniverseManifestOut:
    del user
    if not principal.allowed_project_ids or not principal.tenant_id or not principal.allowed_partition_ids:
        return UniverseManifestOut(status="empty", as_of=datetime.now(UTC), policy=_universe_policy())
    authorized_source_ids = select(SourceProjectMapping.source_id).where(
        SourceProjectMapping.state == "CONFIRMED",
        SourceProjectMapping.organization_id == principal.organization_id,
        SourceProjectMapping.project_id.in_(principal.allowed_project_ids),
    )
    sources = list(
        (
            await session.scalars(
                select(Source)
                .where(Source.id.in_(authorized_source_ids))
                .order_by(Source.id)
                .limit(settings.universe_manifest_source_limit + 1)
            )
        ).all()
    )
    if len(sources) > settings.universe_manifest_source_limit:
        raise ServiceUnavailableError("Universe exceeds the configured authorized source limit")
    ready_versions = await _load_current_ready_versions(session, sources, principal)
    if not ready_versions:
        return UniverseManifestOut(
            status="empty",
            as_of=datetime.now(UTC),
            policy=_universe_policy(),
        )

    project_ids = sorted({version.project_id for version in ready_versions.values()})
    states = (
        await session.scalars(
            select(ProjectSearchState)
            .where(ProjectSearchState.project_id.in_(project_ids))
            .order_by(ProjectSearchState.project_id)
            .with_for_update(read=True)
        )
    ).all()
    states_by_project = {state.project_id: state for state in states}
    stale = set(project_ids) != set(states_by_project)
    integrity_failed = False
    active_versions = [
        (state.project_id, state.active_tree_version)
        for state in states
        if state.active_tree_version
    ]
    manifest_rows = (
        await session.execute(
            select(
                TreeManifest,
                TreeManifest.manifest_json["source_routing_profiles"].label("source_profile_inventory"),
            )
            .options(
                load_only(
                    TreeManifest.tree_version,
                    TreeManifest.project_id,
                    TreeManifest.status,
                    TreeManifest.checksum,
                    TreeManifest.node_count,
                )
            )
            .where(tuple_(TreeManifest.project_id, TreeManifest.tree_version).in_(active_versions))
        )
    ).all()
    manifests_by_key = {(row.project_id, row.tree_version): row for row, _inventory in manifest_rows}
    profile_inventories = {
        (row.project_id, row.tree_version): inventory
        for row, inventory in manifest_rows
    }
    active: dict[str, tuple[ProjectSearchState, TreeManifest]] = {}
    for state in states:
        version = state.active_tree_version
        if not version:
            stale = True
            continue
        record = manifests_by_key.get((state.project_id, version))
        if record is None:
            stale = True
            continue
        checksum = record.checksum
        active_slot_version = (
            state.slot_a_tree_version if state.active_routing_slot == "SLOT_A"
            else state.slot_b_tree_version if state.active_routing_slot == "SLOT_B"
            else None
        )
        if (
            record.status != "ACTIVE"
            or record.project_id != state.project_id
            or record.tree_version != version
            or active_slot_version != version
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
            or version != f"tree-{checksum[:24]}"
            or isinstance(state.active_search_epoch, bool)
            or state.active_search_epoch < 1
        ):
            integrity_failed = True
            continue
        active[state.project_id] = (state, record)

    if integrity_failed:
        return _failed_manifest()
    if stale:
        return _stale_manifest()

    scopes = sorted(
        {
            (
                version.project_id,
                version.source_id,
                version.document_version_id,
                version.partition_id,
            )
            for version in ready_versions.values()
            if version.project_id in active
        }
    )
    expected_scopes: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for version in ready_versions.values():
        if version.project_id in active:
            expected_scopes[version.project_id].add(
                (version.source_id, version.document_version_id, version.partition_id)
            )
    expected_profile_nodes: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for project_id, scopes_for_project in expected_scopes.items():
        state = active[project_id][0]
        inventory = profile_inventories.get((project_id, state.active_tree_version or ""))
        if not isinstance(inventory, list):
            stale = True
            continue
        scope_set = {
            (project_id, source_id, version_id, partition_id)
            for source_id, version_id, partition_id in scopes_for_project
        }
        for item in inventory:
            if not isinstance(item, dict):
                integrity_failed = True
                continue
            scope = (
                item.get("project_id"),
                item.get("source_id"),
                item.get("document_version_id"),
                item.get("partition_id"),
            )
            if scope not in scope_set:
                continue
            node_id = item.get("node_id")
            if (
                item.get("tenant_id") != principal.tenant_id
                or not isinstance(node_id, str)
                or not node_id.strip()
                or node_id in expected_profile_nodes[scope]
            ):
                integrity_failed = True
                continue
            expected_profile_nodes[scope].add(node_id)
        if any(not expected_profile_nodes[scope] for scope in scope_set):
            stale = True
    if integrity_failed:
        return _failed_manifest()
    if stale:
        return _stale_manifest()
    profile_rows: list[TreeRoutingProfile] = []
    if scopes:
        profile_rows = list(
            (
                await session.scalars(
                    select(TreeRoutingProfile)
                    .where(
                        tuple_(
                            TreeRoutingProfile.project_id,
                            TreeRoutingProfile.source_id,
                            TreeRoutingProfile.document_version_id,
                            TreeRoutingProfile.partition_id,
                        ).in_(scopes),
                        tuple_(TreeRoutingProfile.project_id, TreeRoutingProfile.tree_version).in_(
                            [
                                (project_id, state.active_tree_version)
                                for project_id, (state, _record) in active.items()
                            ]
                        ),
                        TreeRoutingProfile.tenant_id == principal.tenant_id,
                        TreeRoutingProfile.partition_id.in_(principal.allowed_partition_ids),
                    )
                    .order_by(
                        TreeRoutingProfile.project_id,
                        TreeRoutingProfile.node_id,
                        TreeRoutingProfile.source_id,
                    )
                    .limit(settings.search_tree_profile_limit + 1)
                )
            ).all()
        )
    if len(profile_rows) > settings.search_tree_profile_limit:
        raise ServiceUnavailableError("Active Universe tree exceeds the configured profile limit")
    observed_profile_nodes: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in profile_rows:
        scope = (row.project_id, row.source_id, row.document_version_id, row.partition_id)
        observed_profile_nodes[scope].add(row.node_id)
    incomplete_projects = {
        project_id
        for project_id, project_scopes in expected_scopes.items()
        if any(
            observed_profile_nodes[(project_id, source_id, version_id, partition_id)]
            != expected_profile_nodes[(project_id, source_id, version_id, partition_id)]
            for source_id, version_id, partition_id in project_scopes
        )
    }
    stale = stale or bool(incomplete_projects)
    if integrity_failed:
        return _failed_manifest()
    if stale:
        return _stale_manifest()

    profile_groups: dict[tuple[str, str], list[TreeRoutingProfile]] = defaultdict(list)
    for row in profile_rows:
        profile_groups[(row.project_id, row.partition_id)].append(row)
    aggregates: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    source_ids_by_project: dict[str, set[str]] = defaultdict(set)
    for (project_id, partition_id), records in profile_groups.items():
        state = active[project_id][0]
        verified_profiles = _aggregate_source_profiles(
            records,
            project_id=project_id,
            tenant_id=principal.tenant_id,
            tree_version=state.active_tree_version or "",
            source_ids=tuple(sorted({row.source_id for row in records})),
            partition_id=partition_id,
            document_version_ids=tuple(sorted({row.document_version_id for row in records})),
            fingerprint="universe-manifest",
            query="",
        )
        if verified_profiles is None:
            raise ServiceUnavailableError("Active Universe tree profile failed integrity validation")
        key = (project_id, state.active_tree_version or "")
        nodes = aggregates[key]
        for profile in verified_profiles:
            node = nodes.setdefault(
                profile.node_id,
                {
                    "parent_id": profile.parent_id,
                    "is_leaf": profile.is_leaf,
                    "unit_count": 0,
                    "sources": set(),
                    "entities": Counter(),
                    "terms": defaultdict(float),
                },
            )
            if node["parent_id"] != profile.parent_id or node["is_leaf"] != profile.is_leaf:
                raise ServiceUnavailableError("Active Universe tree has conflicting node structure")
            node["unit_count"] += profile.accessible_unit_count
        for row in records:
            node = nodes[row.node_id]
            node["sources"].add(row.source_id)
            source_ids_by_project[project_id].add(row.source_id)
            # The legacy incremental-tree writer uses this pair for empty profiles.
            is_empty_profile_placeholder = (
                row.entities_json == ["TechCorp"] and row.sparse_json == [["tech", 1.0]]
            )
            if not is_empty_profile_placeholder:
                for entity in row.entities_json:
                    if entity.strip():
                        node["entities"][entity] += 1
                for term, weight in row.sparse_json:
                    if term.strip():
                        node["terms"][term] += float(weight)

    trees = []
    for (project_id, tree_version), nodes in sorted(aggregates.items()):
        if not nodes:
            continue
        if len(nodes) > active[project_id][1].node_count:
            raise ServiceUnavailableError("Active Universe tree exceeds its manifest node count")
        for node_id, node in nodes.items():
            parent_id = node["parent_id"]
            if parent_id is not None and parent_id not in nodes:
                raise ServiceUnavailableError("Active Universe tree has an incomplete parent chain")
            seen = {node_id}
            depth = 0
            current = parent_id
            while current is not None:
                if current in seen:
                    raise ServiceUnavailableError("Active Universe tree contains a parent cycle")
                seen.add(current)
                depth += 1
                current = nodes[current]["parent_id"]
            node["depth"] = depth

        output_nodes = []
        for node_id, node in sorted(nodes.items(), key=lambda item: (int(item[1]["depth"]), item[0])):
            entities = node["entities"]
            terms = node["terms"]
            label = (
                min(entities, key=lambda value: (-entities[value], value.casefold(), value))
                if entities
                else min(terms, key=lambda value: (-terms[value], value.casefold(), value))
                if terms
                else node_id
            )
            output_nodes.append(
                {
                    "id": node_id,
                    "parent_id": node["parent_id"],
                    "depth": node["depth"],
                    "label": label,
                    "is_leaf": node["is_leaf"],
                    "unit_count": node["unit_count"],
                    "source_count": len(node["sources"]),
                }
            )
        state = active[project_id][0]
        trees.append(
            {
                "project_id": project_id,
                "tree_version": tree_version,
                "search_epoch": state.active_search_epoch,
                "node_count": len(output_nodes),
                "unit_count": sum(node["unit_count"] for node in output_nodes if node["is_leaf"]),
                "source_count": len(source_ids_by_project[project_id]),
                "nodes": output_nodes,
            }
        )

    version = sha256(
        "|".join(
            f"{tree['project_id']}:{tree['tree_version']}:{tree['search_epoch']}"
            for tree in trees
        ).encode()
    ).hexdigest() if trees else None
    return UniverseManifestOut(
        version=version,
        status="failed" if integrity_failed else "ready" if trees else "empty",
        as_of=datetime.now(UTC),
        trees=trees,
        counts={
            "trees": len(trees),
            "sources": sum(tree["source_count"] for tree in trees),
            "nodes": sum(tree["node_count"] for tree in trees),
            "units": sum(tree["unit_count"] for tree in trees),
            "partitions": 0,
            "events": 0,
            "entities": 0,
            "relations": 0,
        },
        policy=_universe_policy(),
    )


@router.post("/expand", response_model=UniverseGraphPatchOut)
async def expand(
    body: UniverseExpandIn,
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> UniverseGraphPatchOut:
    if await get_authorized_source(session, principal=principal, source_id=body.source_id) is None:
        raise NotFoundError("信源不存在")
    value = await universe_expand(
        session,
        engine_manager,
        user_id=user.id,
        source_id=body.source_id,
        node_kind=body.node_kind,
        node_id=body.node_id,
        limit=body.limit,
        cursor=body.cursor,
        snapshot_id=body.snapshot_id,
        after=body.after,
        before=body.before,
    )
    return UniverseGraphPatchOut(epoch=body.epoch, **value)


@router.post("/timeline", response_model=UniverseTimelineSliceOut)
async def timeline(
    body: UniverseTimelineIn,
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> UniverseTimelineSliceOut:
    if await get_authorized_source(session, principal=principal, source_id=body.source_id) is None:
        raise NotFoundError("信源不存在")
    value = await universe_timeline(
        session,
        engine_manager,
        user_id=user.id,
        source_id=body.source_id,
        limit=body.limit,
        direction=body.direction,
        cursor=body.cursor,
        snapshot_id=body.snapshot_id,
    )
    return UniverseTimelineSliceOut(epoch=body.epoch, **value)


@router.get("/nodes/{node_kind}/{node_id}", response_model=UniverseNodeDetailOut)
async def node_detail(
    node_kind: Literal["event", "entity"],
    node_id: str,
    source_id: str = Query(min_length=1, max_length=64),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> UniverseNodeDetailOut:
    if await get_authorized_source(session, principal=principal, source_id=source_id) is None:
        raise NotFoundError("信源不存在")
    value = await universe_node_detail(
        session,
        engine_manager,
        node_kind,
        node_id,
        source_id=source_id,
    )
    return UniverseNodeDetailOut(**value)

@router.post("/rebuild", response_model=JobOut, status_code=202)
async def rebuild(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    job_queue=Depends(get_job_queue),
) -> JobOut:
    del user
    if not principal.tenant_id or not principal.allowed_project_ids or not principal.allowed_partition_ids:
        raise NotFoundError("Universe scope is unavailable")
    job = await enqueue_project_tree_rebuild(
        session,
        job_queue,
        tenant_id=principal.tenant_id,
        project_ids=sorted(principal.allowed_project_ids),
        reason="manual_rebuild",
    )
    return JobOut.model_validate(job)


@router.get("/explorations", response_model=list[ExplorationSessionOut])
async def explorations(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
) -> list[ExplorationSessionOut]:
    rows = await list_explorations(session, user.id, limit=limit)
    requested_source_ids = list(
        dict.fromkeys(source_id for item, _count in rows for source_id in (item.source_ids or []))
    )
    authorized_source_ids = (
        await get_authorized_source_ids(
            session,
            principal=principal,
            requested_source_ids=requested_source_ids,
        )
        if requested_source_ids
        else set()
    )
    visible: list[tuple[object, int]] = []
    for item, count in rows:
        source_ids = set(item.source_ids or [])
        if not source_ids or source_ids.issubset(authorized_source_ids):
            visible.append((item, count))
    return [
        ExplorationSessionOut(
            id=item.id,
            title=item.title,
            source_ids=item.source_ids or [],
            created_at=item.created_at,
            updated_at=item.updated_at,
            step_count=count,
        )
        for item, count in visible
    ]


@router.get("/explorations/{exploration_id}", response_model=ExplorationDetailOut)
async def exploration_detail(
    exploration_id: str,
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
) -> ExplorationDetailOut:
    item, steps = await get_exploration(session, user.id, exploration_id)
    source_ids = set(item.source_ids or [])
    for step in steps:
        source_ids.update(step.source_ids or [])
    if not source_ids:
        raise NotFoundError("探索记录不存在")
    authorized = await search_source_candidates(
        session,
        principal=principal,
        requested_source_ids=sorted(source_ids),
    ) if source_ids else []
    if len(authorized) != len(source_ids):
        raise NotFoundError("探索记录不存在")
    return ExplorationDetailOut(
        session=ExplorationSessionOut(
            id=item.id,
            title=item.title,
            source_ids=item.source_ids or [],
            created_at=item.created_at,
            updated_at=item.updated_at,
            step_count=len(steps),
        ),
        steps=[
            ExplorationStepOut(
                id=step.id,
                session_id=step.session_id,
                query=step.query,
                summary=step.summary,
                source_ids=step.source_ids or [],
                event_refs=step.event_refs or [],
                entity_refs=step.entity_refs or [],
                relation_refs=step.relation_refs or [],
                evidence_refs=step.evidence_refs or [],
                camera=step.camera or {},
                created_at=step.created_at,
            )
            for step in steps
        ],
    )
