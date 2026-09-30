"""信源领域逻辑（单用户，扁平）。"""

from __future__ import annotations

import os
import shutil

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.connectors import registry
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import (
    ApiError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import new_id
from sag_api.db.models import AgentBinding, Job, Source, SourceProjectMapping
from sag_api.enums import CONNECTOR_SOURCE_TYPE, BindingTargetType, JobStatus, JobType, SourceType
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.schemas.source import SourceCreate, SourceUpdate

log = get_logger("services.source")
SOURCE_ID_LOOKUP_BATCH_SIZE = 500


def _is_project_source(source: Source) -> bool:
    return isinstance(source.config, dict) and bool(source.config.get("is_project_source"))


def _authorized_source_statement(principal: VerifiedPrincipal):
    return (
        select(Source)
        .join(SourceProjectMapping, SourceProjectMapping.source_id == Source.id)
        .where(
            SourceProjectMapping.state == "CONFIRMED",
            SourceProjectMapping.organization_id == principal.organization_id,
            SourceProjectMapping.project_id.in_(principal.allowed_project_ids),
        )
    )


async def list_sources(session: AsyncSession, *, principal: VerifiedPrincipal) -> list[Source]:
    if not principal.allowed_project_ids:
        return []
    try:
        rows = await session.execute(
            _authorized_source_statement(principal).order_by(Source.created_at.desc(), Source.id)
        )
    except SQLAlchemyError as error:
        log.exception("source scope resolution failed operation=list")
        raise ServiceUnavailableError("Source authorization mapping is unavailable") from error
    sources = list(rows.scalars().all())
    log.info("source scope resolved operation=list authorized_source_count=%d", len(sources))
    return [source for source in sources if not _is_project_source(source)]


async def search_source_candidates(
    session: AsyncSession,
    *,
    principal: VerifiedPrincipal,
    requested_source_ids: list[str] | None = None,
) -> list[Source]:
    """Select candidates only from confirmed Source mappings in the principal scope."""
    limit = settings.search_source_candidate_limit
    if not principal.allowed_project_ids:
        log.info("source scope resolved authorized_project_count=0 effective_source_count=0")
        return []
    if requested_source_ids is not None:
        ordered_ids = list(dict.fromkeys(source_id.strip() for source_id in requested_source_ids if source_id.strip()))
        if len(ordered_ids) > limit:
            raise ValidationError(
                f"单次最多检索 {limit} 个信息源，请通过 @ 缩小范围",
                code=ErrorCode.TOO_MANY_SEARCH_SOURCES,
            )
        if not ordered_ids:
            return []
        statement = _authorized_source_statement(principal).where(Source.id.in_(ordered_ids))
        try:
            rows = await session.execute(statement)
        except SQLAlchemyError as error:
            log.exception("source scope resolution failed operation=search requested_count=%d", len(ordered_ids))
            raise ServiceUnavailableError("Source authorization mapping is unavailable") from error
        by_id = {source.id: source for source in rows.scalars().all()}
        sources = [
            by_id[source_id]
            for source_id in ordered_ids
            if source_id in by_id and not _is_project_source(by_id[source_id])
        ]
    else:
        statement = _authorized_source_statement(principal).order_by(
            Source.chunk_count.desc(),
            Source.event_count.desc(),
            Source.updated_at.desc(),
            Source.id,
        ).limit(limit)
        try:
            rows = await session.execute(statement)
        except SQLAlchemyError as error:
            log.exception("source scope resolution failed operation=search requested_count=0")
            raise ServiceUnavailableError("Source authorization mapping is unavailable") from error
        sources = [source for source in rows.scalars().all() if not _is_project_source(source)]
    log.info(
        "source scope resolved authorized_project_count=%d requested_source_count=%d effective_source_count=%d",
        len(principal.allowed_project_ids),
        len(requested_source_ids or []),
        len(sources),
    )
    return sources


async def get_authorized_source(
    session: AsyncSession,
    *,
    principal: VerifiedPrincipal,
    source_id: str,
) -> Source | None:
    """Resolve a single Source only when its active mapping is inside this principal's scope."""
    if not principal.allowed_project_ids:
        log.info("authorization denied reason=empty_project_scope operation=read")
        return None
    try:
        result = await session.execute(
            _authorized_source_statement(principal).where(Source.id == source_id).limit(1)
        )
    except SQLAlchemyError as error:
        log.exception("source scope resolution failed operation=read")
        raise ServiceUnavailableError("Source authorization mapping is unavailable") from error
    source = result.scalar_one_or_none()
    if source is None:
        log.info("authorization denied reason=source_mapping_not_visible operation=read")
    else:
        log.info("source scope resolved operation=read effective_source_count=1")
    return source


async def get_authorized_source_ids(
    session: AsyncSession,
    *,
    principal: VerifiedPrincipal,
    requested_source_ids: list[str],
) -> set[str]:
    ordered_ids = list(dict.fromkeys(source_id.strip() for source_id in requested_source_ids if source_id.strip()))
    if not ordered_ids or not principal.allowed_project_ids:
        return set()
    authorized: set[str] = set()
    for start in range(0, len(ordered_ids), SOURCE_ID_LOOKUP_BATCH_SIZE):
        batch = ordered_ids[start : start + SOURCE_ID_LOOKUP_BATCH_SIZE]
        try:
            rows = await session.execute(
                _authorized_source_statement(principal)
                .where(Source.id.in_(batch))
                .with_only_columns(Source.id)
            )
        except SQLAlchemyError as error:
            log.exception("source scope resolution failed operation=validate requested_count=%d", len(ordered_ids))
            raise ServiceUnavailableError("Source authorization mapping is unavailable") from error
        authorized.update(rows.scalars().all())
    log.info(
        "source scope validated requested_source_count=%d effective_source_count=%d",
        len(ordered_ids),
        len(authorized),
    )
    return authorized


async def get_source(
    session: AsyncSession,
    source_id: str,
    *,
    allow_project_source: bool = False,
) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise NotFoundError("信源不存在")
    if not allow_project_source and isinstance(source.config, dict) and source.config.get("is_project_source"):
        raise ForbiddenError(
            "Nguồn dữ liệu dự án được bảo vệ và không thể truy cập qua route không có scope. Hãy sử dụng API /projects/{project_id}/documents."
        )
    return source


async def create_source(
    session: AsyncSession,
    data: SourceCreate,
    *,
    engine_manager: EngineManager,
    principal: VerifiedPrincipal | None = None,
) -> Source:
    project_id: str | None = None
    if principal is not None:
        if not principal.allowed_project_ids:
            raise ForbiddenError("A readable Project is required to request Source assignment")
        project_id = data.project_id
        if project_id is None:
            if len(principal.allowed_project_ids) != 1:
                raise ValidationError("project_id is required when the principal can access multiple Projects")
            project_id = next(iter(principal.allowed_project_ids))
        if project_id != project_id.strip():
            raise ValidationError("project_id must not contain leading or trailing whitespace")
        if project_id not in principal.allowed_project_ids:
            raise ForbiddenError("The requested Project is outside the principal scope")

    connector = registry.get(data.connector_kind)
    connector.validate_config(data.config)
    source_type = CONNECTOR_SOURCE_TYPE.get(data.connector_kind, SourceType.DOCUMENT)

    source = Source(
        name=data.name,
        description=data.description,
        source_type=source_type,
        connector_kind=data.connector_kind,
        sag_source_config_id=f"src_{new_id()[:16]}",
        config=data.config or {},
    )
    session.add(source)
    if principal is not None:
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id=source.id,
                organization_id=principal.organization_id,
                project_id=project_id,
                state="PENDING",
                mapping_version=1,
            )
        )
    await session.commit()
    await session.refresh(source)

    # Source 与知识引擎父记录必须同时可用。预建失败时补偿删除主库记录，
    # 避免接口成功却留下无法上传、检索的半成品信源。
    try:
        await engine_manager.provision(source.sag_source_config_id, source)
    except ApiError as e:
        log.warning("信源引擎预建失败，回滚信源 %s：%s", source.sag_source_config_id, e.message)
        await session.execute(delete(SourceProjectMapping).where(SourceProjectMapping.source_id == source.id))
        await session.delete(source)
        await session.commit()
        raise
    return source


async def update_source(
    session: AsyncSession,
    source_id: str,
    data: SourceUpdate,
    *,
    job_queue: JobQueue | None = None,
) -> Source:
    source = await get_source(session, source_id)
    if data.name is not None:
        source.name = data.name
    if data.description is not None:
        source.description = data.description
    if data.status is not None:
        source.status = data.status
    await session.commit()
    await session.refresh(source)
    from sag_api.services.universe_service import schedule_universe_refresh

    await schedule_universe_refresh(
        session,
        job_queue,
        source_id=source.id,
        reason="source_updated",
    )
    return source


async def delete_source(
    session: AsyncSession,
    source_id: str,
    *,
    engine_manager: EngineManager,
    upload_dir: str,
    job_queue: JobQueue | None = None,
) -> None:
    """删除信源并收尾：移除悬挂绑定、关闭引擎槽、清理上传文件。"""
    source = await get_source(session, source_id)
    sag_id = source.sag_source_config_id

    # 悬挂绑定清理（target_id 为普通字符串，无 FK 级联）
    await session.execute(
        AgentBinding.__table__.delete().where(
            AgentBinding.target_type == BindingTargetType.SOURCE,
            AgentBinding.target_id == source.id,
        )
    )
    await session.delete(source)
    await session.commit()

    # 引擎槽关闭 + 上传目录清理（尽力而为，不阻断删除）
    await engine_manager.release(sag_id)
    shutil.rmtree(os.path.join(upload_dir, source_id), ignore_errors=True)
    from sag_api.services.universe_service import schedule_universe_refresh

    await schedule_universe_refresh(
        session,
        job_queue,
        source_id=None,
        reason="source_deleted",
    )


async def sync_source(session: AsyncSession, source_id: str, *, job_queue: JobQueue) -> Job:
    """触发一次动态连接器同步（如网页抓取）。"""
    source = await get_source(session, source_id)
    connector = registry.get(source.connector_kind)
    if not connector.meta.supports_sync:
        raise ValidationError("该连接器不支持同步")
    job = Job(type=JobType.SYNC_SOURCE, source_id=source.id, status=JobStatus.QUEUED)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    from sag_api.services.document_service import _enqueue_persisted_job

    await _enqueue_persisted_job(job_queue, job.id)
    return job
