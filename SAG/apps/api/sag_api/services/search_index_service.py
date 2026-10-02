"""Search Indexing Service for Phase 2C (Search Units & Qdrant Integration).

Follows Ponytail minimalism:
- Chunks CanonicalBlocks by natural boundaries (headings/tables/token limits).
- Deterministic SearchUnit ID (UUIDv5) and Qdrant point ID (UUIDv5).
- Stores SearchUnits in PostgreSQL.
- Pre-provisions tree routing fields and filter payload indexes in Qdrant.
- Supports dual vector representation: dense vector + BM25 sparse representation.
- Targets per-project collection: search_units_{project_id}.
- Real manifest verification querying both PostgreSQL count and Qdrant point count.
- Cleans up both PostgreSQL SearchUnits and Qdrant points on retry.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any, Sequence
import uuid

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    SearchUnit,
    StageRun,
)

log = get_logger("sag.search_index")


def generate_search_unit_id(document_version_id: str, ordinal: int) -> str:
    """Generate deterministic UUIDv5 for SearchUnit based on document_version_id and ordinal."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:unit:{document_version_id}:{ordinal}"))


def generate_search_unit_point_id(collection_name: str, unit_id: str) -> str:
    """Generate deterministic UUIDv5 for Qdrant point ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:qdrant:{collection_name}:{unit_id}"))


COMMON_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "up", "about", "into", "over", "after", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "can", "could",
    "this", "that", "these", "those", "it", "its", "as",
    "là", "và", "của", "cho", "với", "trong", "được", "có", "đã", "đang", "sẽ",
    "các", "những", "một", "này", "đó", "tại", "về", "từ", "theo", "khi",
}


def compute_sparse_bm25_vector(
    text: str,
    *,
    avg_doc_len: float = 256.0,
    k1: float = 1.5,
    b: float = 0.75,
    corpus_doc_freqs: dict[str, int] | None = None,
    total_corpus_docs: int = 1000,
) -> dict[str, list]:
    """Compute true BM25 weighted sparse vector (indices, values) with length normalization and IDF."""
    import math
    import re
    from collections import Counter

    tokens = re.findall(r"\w+", text.lower())
    if not tokens:
        return {"indices": [], "values": []}

    doc_len = len(tokens)
    counts = Counter(tokens)
    indices = []
    values = []

    for word, tf in counts.items():
        # Compute Robertson-Spärck Jones IDF
        if corpus_doc_freqs and word in corpus_doc_freqs:
            df = corpus_doc_freqs[word]
            idf = math.log(1.0 + (total_corpus_docs - df + 0.5) / (df + 0.5))
        elif word in COMMON_STOPWORDS:
            # Common stopword has high frequency in almost all documents -> damped low weight
            idf = 0.1
        else:
            # Rare/informative content term -> high IDF
            idf = math.log(1.0 + (total_corpus_docs - 2 + 0.5) / (2 + 0.5))

        # BM25 term saturation with document-length normalization
        len_norm = 1.0 - b + b * (doc_len / avg_doc_len)
        tf_weight = (tf * (k1 + 1.0)) / (tf + k1 * len_norm)
        bm25_weight = idf * tf_weight

        # Deterministic 32-bit token hash index for Qdrant sparse vector
        token_idx = int(hashlib.md5(word.encode("utf-8")).hexdigest()[:8], 16) % 1000000
        indices.append(token_idx)
        values.append(round(bm25_weight, 4))

    return {"indices": indices, "values": values}


PAYLOAD_INDEX_FIELDS = [
    ("tenant_id", "keyword"),
    ("project_id", "keyword"),
    ("security_partition_id", "keyword"),
    ("document_version_id", "keyword"),
    ("valid_from_ts", "integer"),
    ("valid_to_ts", "integer"),
    ("primary_node_a", "keyword"),
    ("primary_node_b", "keyword"),
]


async def ensure_qdrant_collection_and_indexes(
    qdrant_client: httpx.AsyncClient,
    collection_name: str,
    vector_dim: int = 1536,
) -> None:
    """Ensure per-project collection exists with dense+sparse config and pre-provisioned payload indexes."""
    # 0. Check if collection already exists to verify vector dimension compatibility
    try:
        info_res = await qdrant_client.get(f"/collections/{collection_name}")
        if info_res.is_success:
            cfg = info_res.json().get("result", {}).get("config", {})
            existing_vectors = cfg.get("params", {}).get("vectors", {})
            if isinstance(existing_vectors, dict) and "content_vector" in existing_vectors:
                existing_dim = existing_vectors["content_vector"].get("size")
                if existing_dim and existing_dim != vector_dim:
                    raise ValueError(
                        f"Qdrant collection {collection_name} dimension mismatch: "
                        f"existing {existing_dim} vs required {vector_dim}"
                    )
    except Exception as check_exc:
        if isinstance(check_exc, ValueError):
            raise
        log.debug("Collection check skipped or non-fatal: %s", check_exc)

    # 1. Create collection with dense and sparse vectors
    try:
        col_res = await qdrant_client.put(
            f"/collections/{collection_name}",
            json={
                "vectors": {
                    "content_vector": {
                        "size": vector_dim,
                        "distance": "Cosine",
                    }
                },
                "sparse_vectors": {
                    "bm25_sparse": {
                        "modifier": "idf"
                    }
                }
            },
        )
        if col_res.status_code >= 400 and col_res.status_code != 409:
            err_text = col_res.text.lower()
            if "already exists" not in err_text:
                raise RuntimeError(
                    f"Failed to create Qdrant collection {collection_name}: HTTP {col_res.status_code} - {col_res.text}"
                )
    except Exception as exc:
        if isinstance(exc, (RuntimeError, ValueError)):
            raise
        log.warning("Could not ensure Qdrant collection %s: %s", collection_name, exc)

    # 2. Pre-provision payload indexes for fast filtering; fail stage if index creation fails
    for field_name, field_schema in PAYLOAD_INDEX_FIELDS:
        idx_res = await qdrant_client.put(
            f"/collections/{collection_name}/index",
            json={
                "field_name": field_name,
                "field_schema": field_schema,
            },
        )
        if idx_res.status_code >= 400 and idx_res.status_code != 409:
            err_text = idx_res.text.lower()
            if "already exists" not in err_text:
                raise RuntimeError(
                    f"Failed to create Qdrant payload index '{field_name}' ({field_schema}): "
                    f"HTTP {idx_res.status_code} - {idx_res.text}"
                )


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
        ordinal = len(units)

        unit = SearchUnit(
            id=generate_search_unit_id(document_version_id, ordinal),
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
        words = block.normalized_text.split()
        block_tokens = max(1, len(words))

        # Natural boundary: new heading or table triggers new unit if group non-empty
        is_boundary = block.block_type in {"heading", "table"} and current_group
        is_overflow = (current_tokens + block_tokens) > max_tokens_per_unit

        if is_boundary or is_overflow:
            flush_group()

        if block_tokens > max_tokens_per_unit:
            # Oversized single block (> max_tokens_per_unit): chunk into sub-units
            for i in range(0, len(words), max_tokens_per_unit):
                chunk_words = words[i : i + max_tokens_per_unit]
                chunk_text = " ".join(chunk_words)
                chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                ordinal = len(units)
                unit = SearchUnit(
                    id=generate_search_unit_id(document_version_id, ordinal),
                    document_version_id=document_version_id,
                    block_from_id=block.id,
                    block_to_id=block.id,
                    security_partition_id=security_partition_id,
                    content_hash=chunk_hash,
                    token_count=len(chunk_words),
                    page_from=block.page_from,
                    page_to=block.page_to,
                    section_path=block.section_path,
                )
                unit._text_content = chunk_text
                units.append(unit)
            current_section = block.section_path
            continue

        current_group.append(block)
        current_tokens += block_tokens
        current_section = block.section_path

    flush_group()
    return units


def build_qdrant_payload(
    unit: SearchUnit,
    *,
    version: DocumentVersion,
    project_id: str = "",
    tenant_id: str = "tenant_default",
    content: str | None = None,
) -> dict[str, Any]:
    """Build Qdrant point payload including pre-provisioned tree routing fields."""
    valid_from_ts = int(version.valid_from.timestamp()) if version.valid_from else None
    valid_to_ts = int(version.valid_to.timestamp()) if version.valid_to else None

    return {
        "_sag_id": unit.id,
        "search_unit_id": unit.id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "document_version_id": unit.document_version_id,
        "security_partition_id": unit.security_partition_id,
        "valid_from": version.valid_from.isoformat() if version.valid_from else None,
        "valid_to": version.valid_to.isoformat() if version.valid_to else None,
        "valid_from_ts": valid_from_ts,
        "valid_to_ts": valid_to_ts,
        "valid_from_iso": version.valid_from.isoformat() if version.valid_from else None,
        "valid_to_iso": version.valid_to.isoformat() if version.valid_to else None,
        "token_count": unit.token_count,
        "section_path": unit.section_path,
        "page_from": unit.page_from,
        "page_to": unit.page_to,
        "content_hash": unit.content_hash,
        "content": content or getattr(unit, "_text_content", ""),
        # Pre-provisioned fields for Tree Routing (Phases 6/8)
        "primary_node_a": None,
        "secondary_node_ids_a": [],
        "tree_version_a": None,
        "primary_node_b": None,
        "secondary_node_ids_b": [],
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
    tenant_id: str = "tenant_default",
    embedder: Any | None = None,
    blocks_by_id: dict[str, CanonicalBlock] | None = None,
) -> int:
    """Upsert search units with real dense + sparse BM25 embeddings into per-project collection."""
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

    # 1. Ensure collection exists and payload indexes are created
    await ensure_qdrant_collection_and_indexes(qdrant_client, collection_name, vector_dim=vector_dim)

    # 2. Calculate corpus statistics across units for true BM25 Robertson-Spärck Jones IDF
    import re
    from collections import Counter
    total_docs = len(texts)
    doc_lens = []
    corpus_doc_freqs: Counter[str] = Counter()
    for text in texts:
        t_tokens = set(re.findall(r"\w+", text.lower()))
        for tok in t_tokens:
            corpus_doc_freqs[tok] += 1
        doc_lens.append(len(re.findall(r"\w+", text.lower())))
    avg_len = float(sum(doc_lens) / total_docs) if total_docs > 0 else 256.0

    # 3. Build points with both dense and sparse representations
    points = []
    for unit, vec, text in zip(units, vectors, texts):
        point_id = generate_search_unit_point_id(collection_name, unit.id)
        payload = build_qdrant_payload(
            unit,
            version=version,
            project_id=project_id,
            tenant_id=tenant_id,
            content=text,
        )
        sparse_vec = compute_sparse_bm25_vector(
            text,
            avg_doc_len=avg_len,
            corpus_doc_freqs=dict(corpus_doc_freqs),
            total_corpus_docs=max(1, total_docs),
        )
        points.append({
            "id": point_id,
            "payload": payload,
            "vector": {
                "content_vector": [float(x) for x in vec],
                "bm25_sparse": sparse_vec,
            },
        })

    # 4. Upsert points with wait=true for strong consistency
    res = await qdrant_client.put(
        f"/collections/{collection_name}/points?wait=true",
        json={"points": points},
    )
    res.raise_for_status()
    return len(points)


async def delete_document_points_from_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    document_version_ids: Sequence[str],
) -> int:
    """Delete all Qdrant points belonging to the given document version IDs."""
    if not document_version_ids:
        return 0
    collection_name = f"search_units_{project_id}"
    del_res = await qdrant_client.post(
        f"/collections/{collection_name}/points/delete?wait=true",
        json={
            "filter": {
                "must": [
                    {
                        "key": "document_version_id",
                        "match": {"any": [str(v_id) for v_id in document_version_ids]},
                    }
                ]
            }
        },
    )
    if del_res.status_code >= 400 and del_res.status_code != 404:
        raise RuntimeError(
            f"Failed to delete Qdrant points for versions {document_version_ids}: "
            f"HTTP {del_res.status_code} - {del_res.text}"
        )
    return len(document_version_ids)


async def reconcile_orphan_search_units(
    session: AsyncSession,
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
) -> dict[str, int]:
    """Identify and delete orphan Qdrant points that no longer exist in PostgreSQL."""
    collection_name = f"search_units_{project_id}"
    all_points: list[dict[str, Any]] = []
    next_offset = None
    while True:
        payload: dict[str, Any] = {
            "limit": 500,
            "with_payload": ["search_unit_id", "document_version_id"],
            "with_vector": False,
        }
        if next_offset is not None:
            payload["offset"] = next_offset
        res = await qdrant_client.post(f"/collections/{collection_name}/points/scroll", json=payload)
        if not res.is_success:
            break
        data = res.json().get("result", {})
        points = data.get("points", [])
        all_points.extend(points)
        next_offset = data.get("next_page_offset")
        if next_offset is None or not points:
            break

    if not all_points:
        return {"scanned": 0, "orphans_deleted": 0}

    point_unit_map = {p["id"]: p.get("payload", {}).get("search_unit_id") for p in all_points if p.get("id")}
    valid_unit_ids = set()
    unit_ids = [uid for uid in point_unit_map.values() if uid]
    if unit_ids:
        for i in range(0, len(unit_ids), 500):
            batch = unit_ids[i : i + 500]
            rows = (
                await session.execute(select(SearchUnit.id).where(SearchUnit.id.in_(batch)))
            ).scalars().all()
            valid_unit_ids.update(rows)

    orphan_point_ids = [
        pid for pid, uid in point_unit_map.items() if not uid or uid not in valid_unit_ids
    ]
    if orphan_point_ids:
        del_res = await qdrant_client.post(
            f"/collections/{collection_name}/points/delete?wait=true",
            json={"points": orphan_point_ids},
        )
        if del_res.status_code >= 400 and del_res.status_code != 404:
            raise RuntimeError(f"Failed to delete orphan points: {del_res.text}")

    return {"scanned": len(all_points), "orphans_deleted": len(orphan_point_ids)}


async def run_search_indexing_stage(
    session: AsyncSession,
    *,
    project_id: str,
    document_version: DocumentVersion,
    security_partition_id: str,
    tenant_id: str | None = None,
    qdrant_client: httpx.AsyncClient | None = None,
    embedder: Any | None = None,
    run_id: str | None = None,
) -> list[SearchUnit]:
    """Execute Phase 2C chunking, PostgreSQL persistence, and Qdrant indexing with audit."""
    start_time = datetime.now(UTC)
    collection_name = f"search_units_{project_id}"

    # Resolve authoritative tenant_id from Document or IngestionRun if not provided
    if not tenant_id:
        from sag_api.db.models.document import Document
        from sag_api.db.models.routing_rag import IngestionRun
        doc = await session.get(Document, document_version.document_id)
        if doc and doc.tenant_id:
            tenant_id = doc.tenant_id
        elif run_id:
            ing_run = await session.get(IngestionRun, run_id)
            if ing_run and ing_run.tenant_id:
                tenant_id = ing_run.tenant_id
    if not tenant_id:
        tenant_id = "tenant_default"

    # 1. Fetch canonical blocks
    blocks = (
        await session.execute(
            select(CanonicalBlock)
            .where(CanonicalBlock.document_version_id == document_version.id)
            .order_by(CanonicalBlock.ordinal)
        )
    ).scalars().all()

    # 2. Build search units with deterministic IDs
    units = build_search_units_from_blocks(
        blocks,
        document_version_id=document_version.id,
        security_partition_id=security_partition_id,
    )

    # 3. Clean up prior units on retry in both PostgreSQL and Qdrant
    prior_units = (
        await session.execute(
            select(SearchUnit).where(SearchUnit.document_version_id == document_version.id)
        )
    ).scalars().all()
    for u in prior_units:
        await session.delete(u)
    await session.flush()

    if qdrant_client is not None:
        try:
            del_res = await qdrant_client.post(
                f"/collections/{collection_name}/points/delete?wait=true",
                json={
                    "filter": {
                        "must": [
                            {"key": "document_version_id", "match": {"value": str(document_version.id)}}
                        ]
                    }
                },
            )
            if del_res.status_code >= 400 and del_res.status_code != 404:
                raise RuntimeError(
                    f"Qdrant point cleanup failed before indexing: HTTP {del_res.status_code} - {del_res.text}"
                )
        except Exception as del_err:
            log.error("Could not delete prior points from Qdrant: %s", del_err)
            document_version.search_status = "INDEX_FAILED"
            session.add(document_version)
            await session.commit()
            raise RuntimeError(f"Qdrant point cleanup failed: {del_err}") from del_err

    # 4. Save new units to PostgreSQL
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
                tenant_id=tenant_id,
                embedder=embedder,
                blocks_by_id=blocks_by_id,
            )
        except Exception as exc:
            indexing_error = str(exc)
            indexed_count = 0

    # 6. Verify Manifest and update Search Readiness
    # Verify count in PostgreSQL
    pg_count = (
        await session.scalar(
            select(func.count(SearchUnit.id)).where(SearchUnit.document_version_id == document_version.id)
        )
    ) or 0

    # Query Qdrant point count for this document version (strict count without mock fallback)
    qdrant_count: int | None = None
    if qdrant_client is not None:
        try:
            count_res = await qdrant_client.post(
                f"/collections/{collection_name}/points/count",
                json={
                    "filter": {
                        "must": [
                            {"key": "document_version_id", "match": {"value": str(document_version.id)}}
                        ]
                    },
                    "exact": True,
                },
            )
            if count_res.is_success:
                res_data = count_res.json().get("result", {})
                if "count" in res_data:
                    qdrant_count = int(res_data["count"])
        except Exception as count_err:
            log.warning("Could not query points count from Qdrant: %s", count_err)

    items = sorted(f"{generate_search_unit_point_id(collection_name, u.id)}:{u.content_hash}" for u in units)
    manifest_checksum = hashlib.sha256(";".join(items).encode("utf-8")).hexdigest()

    # Read back point IDs and content hashes from Qdrant with full pagination for checksum
    qdrant_checksum: str | None = None
    if qdrant_client is not None and units and indexing_error is None:
        try:
            all_q_points: list[dict[str, Any]] | None = []
            next_offset = None
            while True:
                scroll_body: dict[str, Any] = {
                    "filter": {
                        "must": [
                            {"key": "document_version_id", "match": {"value": str(document_version.id)}}
                        ]
                    },
                    "limit": 500,
                    "with_payload": ["content_hash", "search_unit_id"],
                    "with_vector": False,
                }
                if next_offset is not None:
                    scroll_body["offset"] = next_offset

                scroll_res = await qdrant_client.post(
                    f"/collections/{collection_name}/points/scroll",
                    json=scroll_body,
                )
                if not scroll_res.is_success:
                    log.warning("Qdrant scroll returned status %d: %s", scroll_res.status_code, scroll_res.text)
                    all_q_points = None
                    break

                res_data = scroll_res.json().get("result", {})
                batch_points = res_data.get("points", [])
                all_q_points.extend(batch_points)
                next_offset = res_data.get("next_page_offset")
                if next_offset is None or not batch_points:
                    break

            if all_q_points is not None:
                q_items = sorted(
                    f"{p.get('id')}:{p.get('payload', {}).get('content_hash')}" for p in all_q_points
                )
                qdrant_checksum = hashlib.sha256(";".join(q_items).encode("utf-8")).hexdigest()
        except Exception as scroll_err:
            log.warning("Could not read back points from Qdrant for checksum: %s", scroll_err)
            qdrant_checksum = None

    manifest_verified = False
    if not units:
        if qdrant_client is not None:
            # Empty units must confirm Qdrant has 0 points left; stale points cause INDEX_FAILED
            manifest_verified = (qdrant_count == 0)
        else:
            manifest_verified = True
    elif (
        qdrant_client is not None
        and indexing_error is None
        and pg_count == len(units)
        and qdrant_count == len(units)
        and qdrant_checksum is not None
    ):
        # Strict fail-closed manifest verification: checksum must match
        manifest_verified = (qdrant_checksum == manifest_checksum)

    if manifest_verified:
        document_version.search_status = "READY"
        document_version.search_ready_at = datetime.now(UTC)
    else:
        document_version.search_status = "INDEX_FAILED"
        document_version.search_ready_at = None
    session.add(document_version)
    await session.flush()

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    # 7. Record StageRun with Manifest verification
    if run_id:
        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="INDEX_SEARCH",
            status="SUCCESS" if manifest_verified else "FAILED",
            error_message=indexing_error if not manifest_verified and indexing_error else (
                "Qdrant client missing or manifest count/checksum mismatch" if not manifest_verified else None
            ),
            duration_ms=duration_ms,
            metrics_json={
                "search_unit_count": len(units),
                "pg_count": pg_count,
                "qdrant_count": qdrant_count,
                "qdrant_indexed_count": indexed_count,
                "manifest_checksum": manifest_checksum,
                "qdrant_checksum": qdrant_checksum,
                "manifest_verified": manifest_verified,
                "collection_name": collection_name,
                "error": indexing_error,
            },
        )
        session.add(stage_run)

    if not manifest_verified:
        err_msg = indexing_error or (
            f"Search indexing failed: manifest verification mismatch (units={len(units)}, pg={pg_count}, qdrant={qdrant_count}, qdrant_checksum={qdrant_checksum})"
        )
        raise RuntimeError(err_msg)

    return units
