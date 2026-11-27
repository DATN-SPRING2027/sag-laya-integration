from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager, require_path_source_access
from sag_api.core.errors import ForbiddenError, NotFoundError, ValidationError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_project_read_principal
from sag_api.core.text_utils import normalize_text
from sag_api.db.models import User
from sag_api.sag import EngineManager
from sag_api.schemas.chunk import (
    EntityContextOut,
    GrepMatchOut,
    GrepResponse,
    OutlineOut,
    ReadResponse,
)
from sag_api.services.document_service import get_public_document
from sag_api.services.source_service import get_source, versioned_source_ids

router = APIRouter(
    prefix="/sources/{source_id}",
    tags=["knowledge"],
    dependencies=[Depends(require_path_source_access)],
)


@router.get("/outline", response_model=OutlineOut)
async def outline(
    source_id: str,
    document_id: str = Query(min_length=1),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> OutlineOut:
    """文档大纲：标题 + chunk_id，按阅读顺序排列。"""
    source = await get_source(session, source_id)
    document = await get_public_document(session, source, document_id, principal=principal)
    if source.id in await versioned_source_ids(session, [source.id]):
        from datetime import UTC, datetime

        from sqlalchemy import select

        from sag_api.db.models import CanonicalBlock, DocumentVersion

        if not principal.allowed_partition_ids:
            raise NotFoundError("文档尚无大纲，可能仍在处理中")
        now = datetime.now(UTC)
        partition_expr = DocumentVersion.metadata_json["security_partition_id"].as_string()
        version_id = (
            await session.execute(
                select(DocumentVersion.id)
                .where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
                    DocumentVersion.search_ready_at.is_not(None),
                    DocumentVersion.valid_from <= now,
                    DocumentVersion.valid_to > now,
                    partition_expr.in_(sorted(principal.allowed_partition_ids)),
                )
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if not version_id:
            raise NotFoundError("文档尚无大纲，可能仍在处理中")
        blocks = (
            await session.scalars(
                select(CanonicalBlock)
                .where(CanonicalBlock.document_version_id == version_id)
                .order_by(CanonicalBlock.ordinal.asc())
            )
        ).all()
        heading_blocks = [b for b in blocks if b.block_type == "heading"]
        outline_rows: list[dict[str, object]] = []
        if heading_blocks:
            for rank, blk in enumerate(heading_blocks):
                heading_text = (blk.normalized_text or blk.section_path or "").strip()
                if heading_text:
                    outline_rows.append(
                        {"rank": rank, "heading": heading_text, "chunk_id": blk.id}
                    )
        else:
            seen_paths: set[str] = set()
            for blk in blocks:
                sp = (blk.section_path or "").strip()
                if sp and sp not in seen_paths:
                    seen_paths.add(sp)
                    outline_rows.append(
                        {"rank": len(outline_rows), "heading": sp, "chunk_id": blk.id}
                    )
        if not outline_rows:
            raise NotFoundError("文档尚无大纲，可能仍在处理中")
        return OutlineOut(
            document_id=document.id,
            filename=document.filename,
            outline=outline_rows,
        )

    if not document.sag_source_id:
        raise NotFoundError("文档尚无大纲，可能仍在处理中")
    rows = await engine_manager.list_chunk_headings(
        source.sag_source_config_id,
        source=source,
        doc_sag_id=document.sag_source_id,
    )
    if not rows:
        raise NotFoundError("文档尚无大纲，可能仍在处理中")
    return OutlineOut(
        document_id=document.id,
        filename=document.filename,
        outline=[
            {"rank": row["rank"], "heading": row["heading"], "chunk_id": row["chunk_id"]}
            for row in rows
        ],
    )


@router.get("/grep", response_model=GrepResponse)
async def grep(
    source_id: str,
    pattern: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> GrepResponse:
    """精确文本匹配：按原文字面内容查找，大小写不敏感。"""
    pattern = normalize_text(pattern)
    source = await get_source(session, source_id)
    if source.id in await versioned_source_ids(session, [source.id]):
        from sag_api.services.search_unit_retrieval_service import grep_search_unit_blocks

        blocks = await grep_search_unit_blocks(
            session,
            [source],
            pattern,
            principal=principal,
            limit=limit,
        )
        matches = [
            GrepMatchOut(
                chunk_id=block.id,
                heading=block.section_path or "片段",
                snippet=block.normalized_text,
                source_id=source.id,
                source_name=source.name,
            )
            for block in blocks
        ]
        return GrepResponse(pattern=pattern, matches=matches, count=len(matches))
    rows = await engine_manager.grep_chunks(
        source.sag_source_config_id,
        pattern,
        source=source,
        limit=limit,
    )
    matches = [
        GrepMatchOut(
            chunk_id=row["chunk_id"],
            heading=row["heading"],
            snippet=row["snippet"],
        )
        for row in rows
    ]
    return GrepResponse(pattern=pattern, matches=matches, count=len(matches))


@router.get("/documents/{document_id}/read", response_model=ReadResponse)
async def read(
    source_id: str,
    document_id: str,
    offset: int = Query(default=1, ge=1),
    limit: int = Query(default=120, ge=1, le=500),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
) -> ReadResponse:
    """按行分页读取原始文件。"""
    source = await get_source(session, source_id)
    document = await get_public_document(session, source, document_id, principal=principal)
    import asyncio

    if not document.storage_path or not await asyncio.to_thread(os.path.isfile, document.storage_path):
        raise NotFoundError("原始文件不存在或已清理")

    def _read_lines(path: str) -> list[str]:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()

    try:
        all_lines = await asyncio.to_thread(_read_lines, document.storage_path)
    except OSError as exc:
        raise NotFoundError("文件读取失败") from exc
    total = len(all_lines)
    start = max(0, offset - 1)
    page = all_lines[start : start + limit]
    if not page:
        raise ValidationError(f"超出范围：全文共 {total} 行")
    return ReadResponse(
        document_id=document.id,
        filename=document.filename,
        total_lines=total,
        offset=start + 1,
        limit=len(page),
        lines=page,
    )


@router.get("/entities/{name}/context", response_model=EntityContextOut)
async def entity_context(
    source_id: str,
    name: str,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> EntityContextOut:
    """查询实体的相关事件上下文。先精确名称匹配、再子串匹配。"""
    source = await get_source(session, source_id)
    if source.id in await versioned_source_ids(session, [source.id]):
        raise ForbiddenError("Versioned Sources require ACL-filtered search; legacy entity context is unavailable")
    target = name.strip()
    if not target:
        raise ValidationError("实体名称不能为空")
    entities = await engine_manager.list_entities(
        source.sag_source_config_id, source=source, limit=200
    )
    lowered = target.lower()
    match = next(
        (entity for entity in entities if (entity.name or "").lower() == lowered),
        None,
    )
    if match is None:
        match = next(
            (entity for entity in entities if lowered in (entity.name or "").lower()),
            None,
        )
    if match is None:
        raise NotFoundError(f"未找到实体「{target}」")
    snippets = await engine_manager.entity_context(
        source.sag_source_config_id, match.id, source=source, limit=6
    )
    context = "\n\n".join(snippets) if snippets else (match.description or "")
    return EntityContextOut(
        entity_id=match.id,
        name=match.name,
        type=match.type,
        description=match.description,
        context=context,
        source_id=source.id,
        source_name=source.name,
    )
