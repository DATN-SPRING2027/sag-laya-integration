"""Search Index Rebuild Service for Phase 2C.

Allows reconstructing Qdrant collections directly from PostgreSQL source of truth (search_units).
Guarantees disaster recovery, migration readiness, and idempotent re-indexing.
"""

from __future__ import annotations

from typing import Any
import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.logging import get_logger
from sag_api.db.models.document import Document
from sag_api.db.models.routing_rag import DocumentVersion, IngestionRun, SearchUnit
from sag_api.services.search_index_service import (
    ensure_qdrant_collection_and_indexes,
    run_search_indexing_stage,
)

log = get_logger("sag.rebuild_service")


async def rebuild_search_index_for_version(
    session: AsyncSession,
    *,
    document_version_id: str,
    project_id: str,
    security_partition_id: str,
    qdrant_client: httpx.AsyncClient,
    embedder: Any,
) -> int:
    """Rebuild Qdrant vector points for a specific document version from PostgreSQL."""
    doc_ver = await session.get(DocumentVersion, document_version_id)
    if not doc_ver:
        raise ValueError(f"DocumentVersion {document_version_id} not found")

    if not security_partition_id:
        raise ValueError(f"DocumentVersion {document_version_id} has no security_partition_id; fail closed")

    units = await run_search_indexing_stage(
        session,
        project_id=project_id,
        document_version=doc_ver,
        security_partition_id=security_partition_id,
        qdrant_client=qdrant_client,
        embedder=embedder,
    )
    return len(units)


async def rebuild_search_index_for_project(
    session: AsyncSession,
    *,
    project_id: str,
    qdrant_client: httpx.AsyncClient,
    embedder: Any,
) -> dict[str, Any]:
    """Rebuild all Qdrant vector points for an entire project from PostgreSQL.

    Guarantees strict tenant/project isolation: only versions associated with project_id
    via Document or IngestionRun mapping are re-indexed into search_units_{project_id}.
    Fails closed if any version lacks a confirmed security_partition_id.
    """
    collection_name = f"search_units_{project_id}"
    await ensure_qdrant_collection_and_indexes(qdrant_client, collection_name=collection_name)

    # Strictly scope query to DocumentVersions belonging to requested project_id
    stmt = (
        select(DocumentVersion)
        .join(SearchUnit, SearchUnit.document_version_id == DocumentVersion.id)
        .outerjoin(Document, Document.id == DocumentVersion.document_id)
        .outerjoin(IngestionRun, IngestionRun.document_version_id == DocumentVersion.id)
        .where(
            or_(
                Document.project_id == project_id,
                IngestionRun.project_id == project_id,
            )
        )
        .distinct()
    )
    versions = (await session.execute(stmt)).scalars().all()

    rebuilt_versions = 0
    total_units = 0
    for ver in versions:
        # Determine security partition strictly; fail closed if unmapped
        sec_partition = (ver.metadata_json or {}).get("security_partition_id")
        if not sec_partition:
            su_sec = (
                await session.execute(
                    select(SearchUnit.security_partition_id)
                    .where(SearchUnit.document_version_id == ver.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            sec_partition = su_sec

        if not sec_partition:
            log.error(
                "Rebuild failed closed: DocumentVersion %s has no confirmed security_partition_id",
                ver.id,
            )
            ver.search_status = "INDEX_FAILED"
            session.add(ver)
            await session.commit()
            raise ValueError(
                f"DocumentVersion {ver.id} has unmapped security_partition_id; rebuild failed closed"
            )

        units = await run_search_indexing_stage(
            session,
            project_id=project_id,
            document_version=ver,
            security_partition_id=sec_partition,
            qdrant_client=qdrant_client,
            embedder=embedder,
        )
        rebuilt_versions += 1
        total_units += len(units)

    log.info(
        "Rebuilt Qdrant search index for project=%s versions=%d units=%d",
        project_id,
        rebuilt_versions,
        total_units,
    )

    return {
        "project_id": project_id,
        "collection_name": collection_name,
        "rebuilt_versions_count": rebuilt_versions,
        "total_search_units_rebuilt": total_units,
    }
