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

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.logging import get_logger
from sag_api.core.errors import ApiError, is_retryable_error
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.sanitizer import sanitize_error_message
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    SearchUnit,
    StageRun,
)

log = get_logger("sag.search_index")

# Bounded LRU cache mapping sha256([model_signature + NUL +] text) -> dense vector
_SHARED_TEXT_EMBEDDING_CACHE: dict[str, list[float]] = {}
_SHARED_TEXT_EMBEDDING_CACHE_MAX = 4096


def _embedding_cache_key(text: str, model_signature: str = "") -> str:
    sig = model_signature.strip()
    payload = f"{sig}\x00{text}" if sig else text
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached_text_embedding(text: str, *, model_signature: str = "") -> list[float] | None:
    key = _embedding_cache_key(text, model_signature)
    vec = _SHARED_TEXT_EMBEDDING_CACHE.get(key)
    return list(vec) if vec is not None else None


def put_cached_text_embedding(
    text: str,
    vector: Sequence[float],
    *,
    model_signature: str = "",
) -> None:
    key = _embedding_cache_key(text, model_signature)
    if (
        len(_SHARED_TEXT_EMBEDDING_CACHE) >= _SHARED_TEXT_EMBEDDING_CACHE_MAX
        and key not in _SHARED_TEXT_EMBEDDING_CACHE
    ):
        oldest_key = next(iter(_SHARED_TEXT_EMBEDDING_CACHE))
        _SHARED_TEXT_EMBEDDING_CACHE.pop(oldest_key, None)
    _SHARED_TEXT_EMBEDDING_CACHE[key] = [float(x) for x in vector]



def generate_search_unit_id(document_version_id: str, ordinal: int) -> str:
    """Generate deterministic UUIDv5 for SearchUnit based on document_version_id and ordinal."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:unit:{document_version_id}:{ordinal}"))


def generate_search_unit_point_id(collection_name: str, unit_id: str) -> str:
    """Generate deterministic UUIDv5 for Qdrant point ID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:qdrant:{collection_name}:{unit_id}"))


COMMON_STOPWORDS: frozenset[str] = frozenset(
    {
        # English function words & question particles
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
        "by", "from", "up", "about", "into", "over", "after", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did", "can", "could",
        "this", "that", "these", "those", "it", "its", "as",
        "what", "when", "where", "who", "whom", "whose", "which", "why", "how",
        # Vietnamese function words, prepositions, conjunctions & question particles
        "là", "và", "của", "cho", "với", "trong", "được", "có", "đã", "đang", "sẽ",
        "các", "những", "một", "này", "đó", "tại", "về", "từ", "theo", "khi", "như",
        "bởi", "vì", "nếu", "thì", "mà", "để", "đến", "tới", "ra", "vào", "lên", "xuống",
        "cũng", "vẫn", "đều", "chỉ", "rất", "quá", "lắm", "rồi", "hay", "hoặc", "nhưng",
        "gì", "nào", "ai", "đâu", "sao", "thế", "bao", "nhiêu", "không", "chưa", "phải",
    }
)


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
    import unicodedata
    from collections import Counter

    # Normalize Unicode to NFC first so decomposed Vietnamese marks never split tokens,
    # then normalize thousand-separated numbers (1,577 -> 1577) and ampersand initialisms (R&D -> rd)
    nfc_text = unicodedata.normalize("NFC", text or "")
    cleaned_text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", nfc_text.lower())
    cleaned_text = re.sub(r"\b([a-z]{1,2})\s*&\s*([a-z]{1,2})\b", r"\1\2", cleaned_text)
    tokens = re.findall(r"\w+", cleaned_text)
    if not tokens:
        return {"indices": [], "values": []}

    # Expand glued alphanumeric tokens (e.g. fy2018 -> fy2018, 2018) so years/codes match either form
    expanded_tokens: list[str] = list(tokens)
    for tok in tokens:
        if tok.isalnum() and not tok.isalpha() and not tok.isdigit() and len(tok) >= 3:
            for sub in re.findall(r"[a-z]+|\d{2,}", tok):
                if len(sub) >= 2:
                    expanded_tokens.append(sub)

    doc_len = len(tokens)
    counts = Counter(expanded_tokens)
    sparse_map: dict[int, float] = {}

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
        # Deduplicate indices and preserve highest weight on hash collision for Qdrant uniqueness
        sparse_map[token_idx] = max(sparse_map.get(token_idx, 0.0), round(bm25_weight, 4))
        if word.isascii() and word.isalpha():
            stem = (
                word[:-3]
                if len(word) >= 6 and word.endswith("ing")
                else word[:-2]
                if len(word) >= 6 and word.endswith("ed")
                else word[:-1]
                if len(word) >= 5 and word.endswith("s") and not word.endswith("ss")
                else None
            )
            if stem and len(stem) >= 3:
                stem_idx = int(hashlib.md5(stem.encode("utf-8")).hexdigest()[:8], 16) % 1000000
                sparse_map[stem_idx] = max(sparse_map.get(stem_idx, 0.0), round(bm25_weight * 0.55, 4))

    sorted_indices = sorted(sparse_map.keys())
    return {
        "indices": sorted_indices,
        "values": [sparse_map[idx] for idx in sorted_indices],
    }


import asyncio
from weakref import WeakKeyDictionary

PAYLOAD_INDEX_FIELDS = [
    ("tenant_id", "keyword"),
    ("project_id", "keyword"),
    ("security_partition_id", "keyword"),
    ("document_version_id", "keyword"),
    ("source_id", "keyword"),
    ("document_id", "keyword"),
    ("valid_from_ts", "integer"),
    ("valid_to_ts", "integer"),
    ("primary_node_a", "keyword"),
    ("tree_version_a", "keyword"),
    ("primary_node_b", "keyword"),
    ("tree_version_b", "keyword"),
    ("secondary_node_ids_a", "keyword"),
    ("secondary_node_ids_b", "keyword"),
]

_VERIFIED_CLIENT_COLLECTIONS: WeakKeyDictionary[Any, set[tuple[str, int]]] = WeakKeyDictionary()
_CLIENT_COLLECTION_LOCKS: WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = WeakKeyDictionary()


async def ensure_qdrant_collection_and_indexes(
    qdrant_client: httpx.AsyncClient,
    collection_name: str,
    vector_dim: int = 1536,
) -> None:
    """Ensure per-project collection exists with dense+sparse config and pre-provisioned payload indexes."""
    verified_set = _VERIFIED_CLIENT_COLLECTIONS.get(qdrant_client)
    if verified_set is not None and (collection_name, vector_dim) in verified_set:
        return

    locks_by_col = _CLIENT_COLLECTION_LOCKS.get(qdrant_client)
    if locks_by_col is None:
        locks_by_col = {}
        try:
            _CLIENT_COLLECTION_LOCKS[qdrant_client] = locks_by_col
        except TypeError:
            locks_by_col = {}
    col_lock = locks_by_col.setdefault(collection_name, asyncio.Lock())

    async with col_lock:
        verified_set = _VERIFIED_CLIENT_COLLECTIONS.get(qdrant_client)
        if verified_set is not None and (collection_name, vector_dim) in verified_set:
            return

        # 0. Check if collection already exists to verify vector dimension compatibility and existing indexes
        collection_ready = False
        collection_verified = False
        existing_indexed_fields: set[str] = set()
        try:
            info_res = await qdrant_client.get(f"/collections/{collection_name}")
            if info_res.is_success:
                result_data = info_res.json().get("result", {})
                if isinstance(result_data, dict):
                    cfg = result_data.get("config", {})
                    existing_vectors = cfg.get("params", {}).get("vectors", {})
                    existing_sparse = cfg.get("params", {}).get("sparse_vectors", {})
                    if isinstance(existing_vectors, dict) and "content_vector" in existing_vectors:
                        existing_dim = existing_vectors["content_vector"].get("size")
                        if existing_dim and existing_dim != vector_dim:
                            raise ValueError(
                                f"Qdrant collection {collection_name} dimension mismatch: "
                                f"existing {existing_dim} vs required {vector_dim}"
                            )
                        if isinstance(existing_sparse, dict) and "bm25_sparse" in existing_sparse:
                            collection_ready = True
                            collection_verified = True
                    payload_schema = result_data.get("payload_schema")
                    if isinstance(payload_schema, dict):
                        existing_indexed_fields = set(payload_schema.keys())
        except Exception as check_exc:
            if isinstance(check_exc, ValueError):
                raise
            log.debug("Collection check skipped or non-fatal: %s", sanitize_error_message(check_exc))

        # 1. Create collection with dense and sparse vectors when not already verified
        if not collection_ready:
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
                collection_verified = True
            except Exception as exc:
                if isinstance(exc, (RuntimeError, ValueError)):
                    raise
                log.warning("Could not ensure Qdrant collection %s: %s", collection_name, sanitize_error_message(exc))

        # 2. Pre-provision missing payload indexes for fast filtering; fail stage if index creation fails
        missing_index_fields = [
            (fname, fschema)
            for fname, fschema in PAYLOAD_INDEX_FIELDS
            if fname not in existing_indexed_fields
        ]
        if missing_index_fields:
            async def _create_payload_index(field_name: str, field_schema: str) -> None:
                idx_res = await qdrant_client.put(
                    f"/collections/{collection_name}/index?wait=true",
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

            await asyncio.gather(
                *(_create_payload_index(fname, fschema) for fname, fschema in missing_index_fields)
            )

        if collection_verified:
            try:
                vset = _VERIFIED_CLIENT_COLLECTIONS.get(qdrant_client)
                if vset is None:
                    vset = set()
                    _VERIFIED_CLIENT_COLLECTIONS[qdrant_client] = vset
                vset.add((collection_name, vector_dim))
            except TypeError:
                pass


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
            section_path=last.section_path or first.section_path,
        )
        unit._text_content = combined_text
        units.append(unit)
        current_group = []
        current_tokens = 0

    for block in blocks:
        words = block.normalized_text.split()
        block_tokens = max(1, len(words))

        # Natural boundary: new heading triggers a new unit once the current group has body content
        has_body_in_group = any(b.block_type != "heading" for b in current_group)
        is_boundary = block.block_type == "heading" and has_body_in_group
        is_overflow = (current_tokens + block_tokens) > max_tokens_per_unit

        if is_boundary or is_overflow:
            flush_group()

        if block_tokens > max_tokens_per_unit:
            # Oversized single block (> max_tokens_per_unit): preserve line/table structure and propagate table headers
            lines = block.normalized_text.splitlines()
            if len(lines) > 1:
                table_header_lines: list[str] = []
                body_start = 0
                if block.block_type == "table" or (lines[0].strip().startswith("|") and lines[0].strip().endswith("|")):
                    table_header_lines = [lines[0]]
                    body_start = 1
                    if len(lines) > 1 and set(lines[1].strip()) <= {"|", "-", ":", " "}:
                        table_header_lines.append(lines[1])
                        body_start = 2
                header_tokens = sum(len(hl.split()) for hl in table_header_lines)
                effective_budget = max(64, max_tokens_per_unit - header_tokens)
                sub_chunks: list[str] = []
                cur_lines: list[str] = []
                cur_toks = 0
                for ln in lines[body_start:]:
                    ln_toks = max(1, len(ln.split()))
                    if cur_lines and (cur_toks + ln_toks) > effective_budget:
                        prefix = (table_header_lines if table_header_lines else [])
                        sub_chunks.append("\n".join([*prefix, *cur_lines]))
                        cur_lines = [ln]
                        cur_toks = ln_toks
                    else:
                        cur_lines.append(ln)
                        cur_toks += ln_toks
                if cur_lines:
                    prefix = (table_header_lines if table_header_lines else [])
                    sub_chunks.append("\n".join([*prefix, *cur_lines]))
                for chunk_text in sub_chunks:
                    chunk_words_count = max(1, len(chunk_text.split()))
                    chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
                    ordinal = len(units)
                    unit = SearchUnit(
                        id=generate_search_unit_id(document_version_id, ordinal),
                        document_version_id=document_version_id,
                        block_from_id=block.id,
                        block_to_id=block.id,
                        security_partition_id=security_partition_id,
                        content_hash=chunk_hash,
                        token_count=chunk_words_count,
                        page_from=block.page_from,
                        page_to=block.page_to,
                        section_path=block.section_path,
                    )
                    unit._text_content = chunk_text
                    units.append(unit)
            else:
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
    source_id: str | None = None,
    document_id: str | None = None,
    version_no: int | None = None,
    block_from_id: str | None = None,
    block_to_id: str | None = None,
    source_anchor: str | None = None,
) -> dict[str, Any]:
    """Build Qdrant point payload including pre-provisioned tree routing fields and retrieval locators."""
    valid_from_ts = int(version.valid_from.timestamp()) if version.valid_from else None
    valid_to_ts = int(version.valid_to.timestamp()) if version.valid_to else None

    return {
        "_sag_id": unit.id,
        "search_unit_id": unit.id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_id": source_id,
        "document_id": document_id or getattr(version, "document_id", None),
        "document_version_id": unit.document_version_id,
        "version_no": version_no if version_no is not None else getattr(version, "version_no", 1),
        "security_partition_id": unit.security_partition_id,
        "block_from_id": block_from_id or getattr(unit, "block_from_id", None),
        "block_to_id": block_to_id or getattr(unit, "block_to_id", None),
        "source_anchor": source_anchor,
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
    if not getattr(embedder, "_sag_token_metered_cache_wrapped", False):
        from sag_api.core.token_usage import extract_embedding_usage, record_embedding_usage
        record_embedding_usage(
            model=getattr(embedder, "model", None) or getattr(settings, "embedding_model", None),
            usage=extract_embedding_usage(None, list(texts)),
        )
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
    source_id: str | None = None,
    document_id: str | None = None,
) -> int:
    """Upsert search units with real dense + sparse BM25 embeddings into per-project collection."""
    collection_name = f"search_units_{project_id}"
    if not units:
        return 0

    texts = []
    contextual_texts = []
    for u in units:
        t = getattr(u, "_text_content", None)
        if not t and blocks_by_id:
            b1 = blocks_by_id.get(u.block_from_id)
            b2 = blocks_by_id.get(u.block_to_id)
            if b1 and b2:
                t = b1.normalized_text if b1.id == b2.id else f"{b1.normalized_text}\n\n{b2.normalized_text}"
        raw_t = t or u.section_path or u.content_hash
        texts.append(raw_t)
        sec_path = (u.section_path or "").strip()
        ctx_t = f"{sec_path}\n\n{raw_t}" if sec_path and sec_path not in raw_t else raw_t
        contextual_texts.append(ctx_t)

    # Single-Brain Unified Batch Embedding: when using a real batch embedder, pre-batch
    # any individual CanonicalBlock texts alongside SearchUnit contextual texts in 1 HTTP call
    # so KnowledgeWorker gets a 0ms cache hit with 0 extra network calls.
    from sag_api.core.token_usage import estimate_text_tokens, token_usage_scope
    _preload_block_count = 0
    _preload_est_tokens = 0
    _su_est_tokens = estimate_text_tokens(contextual_texts)
    _embed_scope = token_usage_scope(
        component="search_index_embedding",
        project_id=project_id,
        tenant_id=tenant_id,
        document_id=document_id or getattr(version, "document_id", None),
        document_version_id=getattr(version, "id", None),
    )
    _index_ledger = _embed_scope.__enter__()
    if hasattr(embedder, "batch_generate") and blocks_by_id:
        extra_block_texts: list[str] = []
        seen_texts = set(contextual_texts)
        for blk in blocks_by_id.values():
            bt = (blk.normalized_text or "").strip()
            if blk.block_type != "heading" and bt and bt not in seen_texts and get_cached_text_embedding(bt) is None:
                seen_texts.add(bt)
                extra_block_texts.append(blk.normalized_text)
        _preload_block_count = len(extra_block_texts)
        _preload_est_tokens = estimate_text_tokens(extra_block_texts)
        combined_batch = list(contextual_texts) + extra_block_texts
        try:
            all_vectors = await compute_unit_embeddings(embedder, combined_batch)
        except BaseException:
            _embed_scope.__exit__(None, None, None)
            raise
        for txt, vec in zip(combined_batch, all_vectors, strict=True):
            put_cached_text_embedding(txt, vec)
        vectors = all_vectors[: len(contextual_texts)]
    else:
        try:
            vectors = await compute_unit_embeddings(embedder, contextual_texts)
        except BaseException:
            _embed_scope.__exit__(None, None, None)
            raise
        if hasattr(embedder, "batch_generate"):
            for txt, vec in zip(contextual_texts, vectors, strict=True):
                put_cached_text_embedding(txt, vec)
    _embed_scope.__exit__(None, None, None)
    _index_ledger.set_metadata(
        search_unit_texts_count=len(contextual_texts),
        knowledge_block_preload_texts_count=_preload_block_count,
        search_unit_estimated_tokens=_su_est_tokens,
        knowledge_block_preload_estimated_tokens=_preload_est_tokens,
    )
    try:
        setattr(version, "_last_search_index_token_usage", _index_ledger.to_summary())
    except Exception:
        pass
    vector_dim = len(vectors[0]) if vectors and len(vectors[0]) > 0 else 1536

    # 1. Ensure collection exists and payload indexes are created
    await ensure_qdrant_collection_and_indexes(qdrant_client, collection_name, vector_dim=vector_dim)

    # 2. Calculate corpus statistics across units for true BM25 Robertson-Spärck Jones IDF
    import re
    from collections import Counter
    total_docs = len(contextual_texts)
    doc_lens = []
    corpus_doc_freqs: Counter[str] = Counter()
    for ctx_text in contextual_texts:
        t_tokens = set(re.findall(r"\w+", ctx_text.lower()))
        for tok in t_tokens:
            corpus_doc_freqs[tok] += 1
        doc_lens.append(len(re.findall(r"\w+", ctx_text.lower())))
    avg_len = float(sum(doc_lens) / total_docs) if total_docs > 0 else 256.0

    # 3. Build points with both dense and sparse representations
    points = []
    for unit, vec, text, ctx_text in zip(units, vectors, texts, contextual_texts, strict=True):
        point_id = generate_search_unit_point_id(collection_name, unit.id)
        b_start = blocks_by_id.get(unit.block_from_id) if blocks_by_id else None
        raw_anchor = b_start.source_anchor if b_start and b_start.source_anchor else None
        s_anchor = raw_anchor.strip() if raw_anchor and raw_anchor.strip() else f"block-{unit.block_from_id[:8]}"
        if b_start and (not b_start.source_anchor or not b_start.source_anchor.strip()):
            b_start.source_anchor = s_anchor
        payload = build_qdrant_payload(
            unit,
            version=version,
            project_id=project_id,
            tenant_id=tenant_id,
            content=text,
            source_id=source_id,
            document_id=document_id or getattr(version, "document_id", None),
            version_no=getattr(version, "version_no", 1),
            block_from_id=unit.block_from_id,
            block_to_id=unit.block_to_id,
            source_anchor=s_anchor,
        )
        sparse_vec = compute_sparse_bm25_vector(
            ctx_text,
            avg_doc_len=256.0,
            corpus_doc_freqs=None,
            total_corpus_docs=1000,
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

    # Resolve authoritative tenant_id and doc from Document or IngestionRun
    from sag_api.db.models.document import Document
    from sag_api.db.models.routing_rag import IngestionRun
    doc = await session.get(Document, document_version.document_id)
    if not tenant_id:
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
    # Demote readiness immediately before deleting old points to prevent false-ready state during reindexing
    if document_version.search_status == "SEARCH_READY":
        document_version.search_status = "INDEXING"
        document_version.search_ready_at = None
        session.add(document_version)
        await session.flush()

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
            delete_body = {
                    "filter": {
                        "must": [
                            {"key": "document_version_id", "match": {"value": str(document_version.id)}}
                        ]
                    }
                }
            delete_url = f"/collections/{collection_name}/points/delete?wait=true"
            del_res = await qdrant_client.post(delete_url, json=delete_body)
            if (del_res.status_code == 400 and "index required but not found" in del_res.text.lower()
                    and "document_version_id" in del_res.text):
                index_res = await qdrant_client.put(
                    f"/collections/{collection_name}/index?wait=true",
                    json={"field_name": "document_version_id", "field_schema": "keyword"},
                )
                if index_res.status_code >= 400 and index_res.status_code != 409 and "already exists" not in index_res.text.lower():
                    index_res.raise_for_status()
                del_res = await qdrant_client.post(delete_url, json=delete_body)
            if del_res.status_code >= 400 and del_res.status_code != 404:
                del_res.raise_for_status()
        except Exception as del_err:
            log.error("Qdrant cleanup failed error_type=%s retryable=%s",
                      type(del_err).__name__, is_retryable_error(del_err))
            document_version.search_status = "INDEX_FAILED"
            session.add(document_version)
            await session.commit()
            raise ApiError(f"Qdrant point cleanup failed ({type(del_err).__name__})",
                           layer=ErrorLayer.STORE, stage=ErrorStage.PERSIST,
                           retryable=is_retryable_error(del_err)) from del_err

    # 4. Save new units to PostgreSQL
    for u in units:
        session.add(u)
    await session.flush()

    # 5. Index to Qdrant if client provided
    blocks_by_id = {b.id: b for b in blocks}
    indexed_count = 0
    indexing_error: str | None = None
    indexing_exception: Exception | None = None
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
                source_id=doc.source_id if doc else None,
                document_id=document_version.document_id,
            )
        except Exception as exc:
            indexing_error = str(exc)
            indexing_exception = exc
            indexed_count = 0

    for b in blocks:
        session.add(b)
    await session.flush()

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
            else:
                count_res.raise_for_status()
        except Exception as count_err:
            indexing_exception = indexing_exception or count_err
            log.warning("Could not query points count from Qdrant: %s", sanitize_error_message(count_err))

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
                    scroll_res.raise_for_status()
                    log.warning(
                        "Qdrant scroll returned status %d: %s",
                        scroll_res.status_code,
                        sanitize_error_message(scroll_res.text),
                    )
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
            indexing_exception = indexing_exception or scroll_err
            log.warning("Could not read back points from Qdrant for checksum: %s", sanitize_error_message(scroll_err))
            qdrant_checksum = None

    manifest_verified = False
    if not units:
        if qdrant_client is not None:
            # Empty units must confirm Qdrant has 0 points left; stale points cause INDEX_FAILED
            manifest_verified = (qdrant_count == 0)
        else:
            # Fail closed: cannot verify 0 points in Qdrant without client
            manifest_verified = False
    elif (
        qdrant_client is not None
        and indexing_error is None
        and pg_count == len(units)
        and qdrant_count == len(units)
        and qdrant_checksum is not None
    ):
        # Strict fail-closed manifest verification: checksum must match
        manifest_verified = (qdrant_checksum == manifest_checksum)

    if manifest_verified and units:
        from sag_api.services.dedup_and_temporal_service import resolve_temporal_supersedes

        # Serialize publication, while retaining the previous version if indexing fails.
        await session.execute(
            select(Document.id).where(Document.id == document_version.document_id).with_for_update()
        )
        temporal_info = await resolve_temporal_supersedes(
            session,
            current_version=document_version,
            document_id=document_version.document_id,
        )
        superseded_version_id = temporal_info.get("supersedes_id") if isinstance(temporal_info, dict) else None
        if (
            qdrant_client is not None
            and isinstance(temporal_info, dict)
            and temporal_info.get("action") == "SUPERSEDED"
            and superseded_version_id
        ):
            try:
                await delete_document_points_from_qdrant(
                    qdrant_client,
                    project_id=project_id,
                    document_version_ids=[str(superseded_version_id)],
                )
            except Exception as exc:
                log.warning(
                    "Failed to prune superseded version %s from Qdrant: %s",
                    superseded_version_id,
                    exc,
                )
        document_version.search_status = "SEARCH_READY"
        document_version.search_ready_at = datetime.now(UTC)
    else:
        document_version.search_status = "INDEX_FAILED"
        document_version.search_ready_at = None
    session.add(document_version)
    await session.flush()

    _idx_token_usage = getattr(document_version, "_last_search_index_token_usage", None)
    if _idx_token_usage is not None:
        document_version.metadata_json = {
            **(document_version.metadata_json or {}),
            "search_index_token_usage": _idx_token_usage,
        }
        session.add(document_version)
        await session.flush()
        from sag_api.core.token_usage import sync_document_token_usage
        await sync_document_token_usage(session, document_version.id)
    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    # 7. Record StageRun with Manifest verification
    if run_id:
        is_success = manifest_verified and bool(units)
        err_text: str | None = None
        if not units:
            err_text = "EMPTY_INDEX: Document has no text content to index"
        elif not manifest_verified:
            err_text = (
                sanitize_error_message(indexing_error)
                if indexing_error
                else "Qdrant client missing or manifest count/checksum mismatch"
            )

        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="INDEX_SEARCH",
            status="SUCCESS" if is_success else "FAILED",
            error_message=err_text,
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
                "token_usage": _idx_token_usage,
                "error": (
                    sanitize_error_message(indexing_error)
                    if indexing_error
                    else (err_text if not is_success else None)
                ),
            },
        )
        session.add(stage_run)

    if not manifest_verified:
        err_msg = indexing_error or (
            "Search indexing failed: manifest verification mismatch "
            f"(units={len(units)}, pg={pg_count}, qdrant={qdrant_count}, "
            f"qdrant_checksum={qdrant_checksum})"
        )
        raise ApiError(err_msg, layer=ErrorLayer.STORE, stage=ErrorStage.PERSIST,
                       retryable=is_retryable_error(indexing_exception) if indexing_exception else False) from indexing_exception

    if not units:
        raise RuntimeError("EMPTY_INDEX: Document has no text content to index")

    return units
