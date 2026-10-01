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
        unit._text_content = combined_text
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
    content: str | None = None,
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
        "content": content or getattr(unit, "_text_content", ""),
        # Pre-provisioned fields for Tree Routing (Phases 6/8)
        "primary_node_a": None,
        "primary_node_b": None,
        "tree_version_a": None,
        "tree_version_b": None,
    }


async def compute_unit_embeddings(embedder: Any, texts: Sequence[str]) -> list[list[float]]:
    """Compute dense embeddings for search units using the provided embedder."""
    if not texts:
        return []
    if embedder is None:
        raise ValueError("Embedder instance is required to compute real vectors for Qdrant indexing")

    import inspect
    if hasattr(embedder, "batch_generate"):
        vectors = await embedder.batch_generate(list(texts))
    elif hasattr(embedder, "generate"):
        vectors = [await embedder.generate(t) for t in texts]
    elif callable(embedder):
        if inspect.iscoroutinefunction(embedder):
            vectors = [await embedder(t) for t in texts]
        else:
            vectors = [embedder(t) for t in texts]
    else:
        raise TypeError(f"Embedder of type {type(embedder)} has no generate/batch_generate interface")

    if len(vectors) != len(texts):
        raise RuntimeError(f"Embedding count mismatch: expected {len(texts)}, got {len(vectors)}")
    return vectors


async def index_search_units_to_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    units: Sequence[SearchUnit],
    version: DocumentVersion,
    embedder: Any | None = None,
    blocks_by_id: dict[str, CanonicalBlock] | None = None,
) -> int:
    """Upsert search units with real dense embeddings into per-project collection `search_units_{project_id}`."""
    collection_name = f"search_units_{project_id}"
    if not units:
        return 0

    texts = []
    for u in units:
        t = getattr(u, "_text_content", None)
        if not t and blocks_by_id:
            b1 = blocks_by_id.get(u.block_from_id)
            b2 = blocks_by_id.get(u.block_to_id)
            if b1 and b2:
                t = b1.normalized_text if b1.id == b2.id else f"{b1.normalized_text}\n\n{b2.normalized_text}"
        texts.append(t or u.section_path or u.content_hash)

    vectors = await compute_unit_embeddings(embedder, texts)
    vector_dim = len(vectors[0]) if vectors and len(vectors[0]) > 0 else 1536

    points = []
    for unit, vec, text in zip(units, vectors, texts):
        point_id = generate_search_unit_point_id(collection_name, unit.id)
        payload = build_qdrant_payload(unit, version=version, content=text)
        points.append({
            "id": point_id,
            "payload": payload,
            "vector": {"content_vector": [float(x) for x in vec]},
        })

    # Ensure collection exists (lazy idempotency)
    await qdrant_client.put(
        f"/collections/{collection_name}",
        json={
            "vectors": {
                "content_vector": {
                    "size": vector_dim,
                    "distance": "Cosine",
                }
            }
        },
    )

    # Upsert points
    res = await qdrant_client.put(
        f"/collections/{collection_name}/points?wait=true",
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
    embedder: Any | None = None,
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
    blocks_by_id = {b.id: b for b in blocks}
    indexed_count = 0
    indexing_error: str | None = None
    if qdrant_client is not None and units:
        try:
            indexed_count = await index_search_units_to_qdrant(
                qdrant_client,
                project_id=project_id,
                units=units,
                version=document_version,
                embedder=embedder,
                blocks_by_id=blocks_by_id,
            )
        except Exception as exc:
            indexing_error = str(exc)
            indexed_count = 0

    # 6. Verify Manifest and update Search Readiness
    manifest_verified = False
    if not units:
        manifest_verified = True
    elif qdrant_client is not None and indexing_error is None and indexed_count == len(units):
        manifest_verified = True

    if manifest_verified:
        document_version.search_status = "READY"
        document_version.search_ready_at = datetime.now(UTC)
    else:
        document_version.search_status = "INDEX_FAILED"
        document_version.search_ready_at = None
    session.add(document_version)

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    # 7. Record StageRun with Manifest verification
    if run_id:
        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="INDEX_SEARCH",
            status="SUCCESS" if manifest_verified else "FAILED",
            error_message=indexing_error if not manifest_verified and indexing_error else (
                "Qdrant client missing or manifest count mismatch" if not manifest_verified else None
            ),
            duration_ms=duration_ms,
            metrics_json={
                "search_unit_count": len(units),
                "qdrant_indexed_count": indexed_count,
                "manifest_verified": manifest_verified,
                "collection_name": f"search_units_{project_id}",
                "error": indexing_error,
            },
        )
        session.add(stage_run)

    if not manifest_verified and units:
        err_msg = indexing_error or (
            f"Search indexing failed: Qdrant client missing or count mismatch (units={len(units)}, indexed={indexed_count})"
        )
        raise RuntimeError(err_msg)

    return units
