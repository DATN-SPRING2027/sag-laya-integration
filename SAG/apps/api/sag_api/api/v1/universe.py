from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager
from sag_api.core.errors import NotFoundError, ServiceUnavailableError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import User
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
from sag_api.services.source_service import get_authorized_source, search_source_candidates
from sag_api.services.universe_service import (
    get_exploration,
    list_explorations,
    universe_expand,
    universe_node_detail,
    universe_timeline,
)

router = APIRouter(prefix="/universe", tags=["universe"])


@router.get("/manifest", response_model=UniverseManifestOut)
async def manifest(
    user: User = Depends(get_current_user),
    _principal: VerifiedPrincipal = Depends(require_principal_assertion),
) -> UniverseManifestOut:
    del user
    raise ServiceUnavailableError("Universe manifest is disabled until Project-scoped graph builds are available")


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
    user: User = Depends(get_current_user),
    _principal: VerifiedPrincipal = Depends(require_principal_assertion),
) -> JobOut:
    del user
    raise ServiceUnavailableError("Universe rebuild is disabled until it can build per Project authorization scopes")


@router.get("/explorations", response_model=list[ExplorationSessionOut])
async def explorations(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
) -> list[ExplorationSessionOut]:
    rows = await list_explorations(session, user.id, limit=limit)
    visible: list[tuple[object, int]] = []
    for item, count in rows:
        source_ids = list(dict.fromkeys(item.source_ids or []))
        if not source_ids:
            continue
        authorized = await search_source_candidates(
            session,
            principal=principal,
            requested_source_ids=source_ids,
        ) if source_ids else []
        if len(authorized) == len(source_ids):
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
