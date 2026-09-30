from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.core.errors import NotFoundError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import Document, Job, User
from sag_api.schemas.job import JobOut
from sag_api.services.source_service import get_authorized_source

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
) -> JobOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise NotFoundError("任务不存在")
    source_id = job.source_id
    if source_id is None and job.document_id is not None:
        source_id = await session.scalar(
            select(Document.source_id).where(Document.id == job.document_id)
        )
    if source_id is None or await get_authorized_source(
        session,
        principal=principal,
        source_id=source_id,
    ) is None:
        raise NotFoundError("任务不存在")
    return JobOut.model_validate(job)
