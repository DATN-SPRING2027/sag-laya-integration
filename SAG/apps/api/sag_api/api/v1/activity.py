"""近期动态 —— 知识库时间线：最近文档（会话不属于知识库动态）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import Document, Source, User
from sag_api.services.source_service import list_sources

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
async def list_activity(
    limit: int = Query(default=20, ge=1, le=50),
    source_ids: list[str] | None = Query(default=None, max_length=100),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    authorized_source_ids = {source.id for source in await list_sources(session, principal=principal)}
    if source_ids is not None:
        requested_source_ids = {
            source_id.strip() for source_id in source_ids if source_id.strip()
        }
        authorized_source_ids.intersection_update(requested_source_ids)
    if not authorized_source_ids:
        return []
    statement = (
        select(Document, Source.name)
        .join(Source, Source.id == Document.source_id)
        .where(Document.source_id.in_(authorized_source_ids))
    )
    docs = (
        await session.execute(
            statement.order_by(Document.created_at.desc()).limit(limit)
        )
    ).all()
    items: list[dict] = [
        {
            "type": "document",
            "id": d.id,
            "source_id": d.source_id,
            "title": d.filename,
            "subtitle": source_name,
            "status": d.status.value,
            "at": d.created_at.isoformat(),
        }
        for d, source_name in docs
    ]
    items.sort(key=lambda x: x["at"], reverse=True)
    return items[:limit]
