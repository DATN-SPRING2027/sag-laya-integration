"""Search Index Rebuild Service for Phase 2C.

Allows reconstructing Qdrant collections directly from PostgreSQL source of truth (search_units).
Guarantees disaster recovery, migration readiness, and idempotent re-indexing.
"""

from __future__ import annotations

from typing import Any
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.logging import get_logger
from sag_api.db.models.routing_rag import DocumentVersion, SearchUnit
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
    """Rebuild all Qdrant vector points for an entire project from PostgreSQL."""
    collection_name = f"search_units_{project_id}"
    await ensure_qdrant_collection_and_indexes(qdrant_client, collection_name=collection_name)

    # Find all DocumentVersions that have SearchUnits or CanonicalBlocks
    versions = (
        await session.execute(
            select(DocumentVersion)
            .join(SearchUnit, SearchUnit.document_version_id == DocumentVersion.id)
            .distinct()
        )
    ).scalars().all()

    rebuilt_versions = 0
    total_units = 0
    for ver in versions:
        sec_partition = (ver.metadata_json or {}).get("security_partition_id", "public")
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
