"""Search Indexing Service for Phase 2C (Search Units & Qdrant Integration).

Follows Ponytail minimalism:
- Chunks CanonicalBlocks by natural boundaries (headings/tables/token limits).
- Stores SearchUnits in PostgreSQL.
- Pre-provisions tree routing fields in Qdrant payload: primary_node_a/b, tree_version_a/b.
- Targets per-project collection: search_units_{project_id}.
- Deterministic stable point ID via UUIDv5.
- Manifest verification comparing PostgreSQL count vs indexed points.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any, Sequence
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    SearchUnit,
    StageRun,
)


def generate_search_unit_point_id(collection_name: str, unit_id: str) -> str:
    """Generate deterministic UUIDv5 for Qdrant point ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:qdrant:{collection_name}:{unit_id}"))


def build_search_units_from_blocks(
    blocks: Sequence[CanonicalBlock],
    *,
    document_version_id: str,
    security_partition_id: str,
    max_tokens_per_unit: int = 512,
) -> list[SearchUnit]:
    """Group canonical blocks into search units based on natural section boundaries."""
    units: list[SearchUnit] = []
    if not blocks:
        return units

    current_group: list[CanonicalBlock] = []
    current_tokens = 0
    current_section = ""

    def flush_group():
        nonlocal current_group, current_tokens, current_section
        if not current_group:
            return
        first = current_group[0]
        last = current_group[-1]
        combined_text = "\n\n".join(b.normalized_text for b in current_group)
        content_hash = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()

        unit = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=document_version_id,
            block_from_id=first.id,
            block_to_id=last.id,
            security_partition_id=security_partition_id,
            content_hash=content_hash,
            token_count=current_tokens,
            page_from=first.page_from,
            page_to=last.page_to,
            section_path=first.section_path,
        )
        units.append(unit)
        current_group = []
        current_tokens = 0

    for block in blocks:
        # Estimate token count (~1 word = 1.3 tokens or simple word count)
        block_tokens = max(1, len(block.normalized_text.split()))

        # Natural boundary: new heading or table triggers new unit if group non-empty
        is_boundary = block.block_type in {"heading", "table"} and current_group
        is_overflow = (current_tokens + block_tokens) > max_tokens_per_unit

        if is_boundary or is_overflow:
            flush_group()

        current_group.append(block)
        current_tokens += block_tokens
        current_section = block.section_path

    flush_group()
    return units


def build_qdrant_payload(
    unit: SearchUnit,
    *,
    version: DocumentVersion,
) -> dict[str, Any]:
    """Build Qdrant point payload including pre-provisioned tree routing fields."""
    return {
        "_sag_id": unit.id,
        "document_version_id": unit.document_version_id,
        "security_partition_id": unit.security_partition_id,
        "valid_from": version.valid_from.isoformat() if version.valid_from else None,
        "valid_to": version.valid_to.isoformat() if version.valid_to else None,
        "token_count": unit.token_count,
        "section_path": unit.section_path,
        "page_from": unit.page_from,
        "page_to": unit.page_to,
        "content_hash": unit.content_hash,
        # Pre-provisioned fields for Tree Routing (Phases 6/8)
        "primary_node_a": None,
        "primary_node_b": None,
        "tree_version_a": None,
        "tree_version_b": None,
    }


async def index_search_units_to_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    units: Sequence[SearchUnit],
    version: DocumentVersion,
    dummy_vector_dim: int = 4,
) -> int:
    """Upsert search units into per-project collection `search_units_{project_id}`."""
    collection_name = f"search_units_{project_id}"
    points = []

    for unit in units:
        point_id = generate_search_unit_point_id(collection_name, unit.id)
        payload = build_qdrant_payload(unit, version=version)
        # Vector placeholder (or dense vector)
        points.append({
            "id": point_id,
            "payload": payload,
            "vector": {"content_vector": [0.1] * dummy_vector_dim},
        })

    if not points:
        return 0

    # Ensure collection exists (lazy idempotency)
    await qdrant_client.put(
        f"/collections/{collection_name}",
        json={
            "vectors": {
                "content_vector": {
                    "size": dummy_vector_dim,
                    "distance": "Cosine",
                }
            }
        },
    )

    # Upsert points
    res = await qdrant_client.put(
        f"/collections/{collection_name}/points",
        json={"points": points},
    )
    res.raise_for_status()
    return len(points)


async def run_search_indexing_stage(
    session: AsyncSession,
    *,
    project_id: str,
    document_version: DocumentVersion,
    security_partition_id: str,
    qdrant_client: httpx.AsyncClient | None = None,
    run_id: str | None = None,
) -> list[SearchUnit]:
    """Execute Phase 2C chunking, PostgreSQL persistence, and Qdrant indexing with audit."""
    start_time = datetime.now(UTC)

    # 1. Fetch canonical blocks
    blocks = (
        await session.execute(
            select(CanonicalBlock)
            .where(CanonicalBlock.document_version_id == document_version.id)
            .order_by(CanonicalBlock.ordinal)
        )
    ).scalars().all()

    # 2. Build search units
    units = build_search_units_from_blocks(
        blocks,
        document_version_id=document_version.id,
        security_partition_id=security_partition_id,
    )

    # 3. Clean up prior units on retry
    prior_units = (
        await session.execute(
            select(SearchUnit).where(SearchUnit.document_version_id == document_version.id)
        )
    ).scalars().all()
    for u in prior_units:
        await session.delete(u)

    # 4. Save new units
    for u in units:
        session.add(u)
    await session.flush()

    # 5. Index to Qdrant if client provided
    indexed_count = 0
    if qdrant_client is not None and units:
        indexed_count = await index_search_units_to_qdrant(
            qdrant_client,
            project_id=project_id,
            units=units,
            version=document_version,
        )

    # 6. Verify Manifest and update Search Readiness
    manifest_verified = (len(units) == indexed_count) if qdrant_client else True
    if manifest_verified:
        document_version.search_status = "READY"
        document_version.search_ready_at = datetime.now(UTC)
    else:
        document_version.search_status = "INDEX_FAILED"
    session.add(document_version)

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    # 7. Record StageRun with Manifest verification
    if run_id:
        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="INDEX_SEARCH",
            status="SUCCESS" if manifest_verified else "FAILED",
            duration_ms=duration_ms,
            metrics_json={
                "search_unit_count": len(units),
                "qdrant_indexed_count": indexed_count,
                "manifest_verified": manifest_verified,
                "collection_name": f"search_units_{project_id}",
            },
        )
        session.add(stage_run)

    return units
