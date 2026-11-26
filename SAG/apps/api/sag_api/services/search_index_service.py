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
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.bm25_tokenizer import (
    BM25_STEM_WEIGHT,
    english_stem,
    token_hash_bucket,
    tokenize_bm25_tokens,
)
from sag_api.core.bm25_tokenizer import (
    COMMON_STOPWORDS as COMMON_STOPWORDS,
)
from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError, is_retryable_error
from sag_api.core.logging import get_logger
from sag_api.core.sanitizer import sanitize_error_message
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    SearchUnit,
    StageRun,
)
from sag_api.enums import normalize_scope_tags

log = get_logger("sag.search_index")

_MAX_VALID_TO = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _as_utc(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        parsed = datetime.fromisoformat(dt)
    else:
        parsed = dt
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

# Bounded LRU cache mapping sha256(model_signature + NUL + text) -> dense vector
_SHARED_TEXT_EMBEDDING_CACHE: dict[str, list[float]] = {}
_SHARED_TEXT_EMBEDDING_CACHE_MAX = 16384

_KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "bge-large-en-v1.5": 1024,
    "baai/bge-large-en-v1.5": 1024,
    "baai/bge-m3": 1024,
    "bge-m3": 1024,
    "qwen/qwen3-embedding-4b": 1024,
    "qwen3-embedding-4b": 1024,
    "qwen/qwen3-embedding-0.6b": 1024,
    "qwen3-embedding-0.6b": 1024,
    "qwen/qwen3-embedding-8b": 4096,
    "qwen3-embedding-8b": 4096,
    "text-embedding-3-small": 1536,
    "openai/text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "openai/text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "openai/text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
}


def resolve_embedding_dimensions(
    model_name: str | None = None,
    configured_dimensions: int | None = None,
) -> int:
    """Resolve embedding vector dimensions from explicit config or model registry (default 1024)."""
    if configured_dimensions is not None and configured_dimensions != "":
        if isinstance(configured_dimensions, bool):
            raise ValueError(f"Invalid embedding dimensions: {configured_dimensions!r}")
        dim = int(configured_dimensions)
        if dim <= 0:
            raise ValueError(f"Embedding dimensions must be positive, got {dim}")
        return dim
    normalized = str(model_name or "").strip().lower()
    if normalized in _KNOWN_EMBEDDING_DIMENSIONS:
        return _KNOWN_EMBEDDING_DIMENSIONS[normalized]
    return 1024


def _attr_from_embedder_or_delegates(embedder: Any | None, *names: str) -> Any:
    if embedder is None:
        return None
    candidates: list[Any] = [embedder]
    cfg = getattr(embedder, "config", None)
    if cfg is not None:
        candidates.append(cfg)
    for delegate_attr in ("inner", "_inner", "client", "_client", "_delegate"):
        delegate = getattr(embedder, delegate_attr, None)
        if delegate is not None:
            candidates.append(delegate)
            d_cfg = getattr(delegate, "config", None)
            if d_cfg is not None:
                candidates.append(d_cfg)
    for obj in candidates:
        for name in names:
            val = getattr(obj, name, None)
            if val is not None and val != "":
                return val
    return None


def build_embedding_model_signature(
    embedder: Any | None = None,
    *,
    settings_obj: Any | None = None,
    model: str | None = None,
    base_url: str | None = None,
    dimensions: int | None = None,
) -> str:
    """Build canonical cache/index signature capturing base_url, model, and resolved dimensions."""
    s = settings_obj if settings_obj is not None else settings
    emb_model = (
        model
        if isinstance(model, str) and model.strip()
        else _attr_from_embedder_or_delegates(embedder, "model", "_model", "model_name")
    )
    settings_model = str(getattr(s, "embedding_model", "") or "").strip()
    resolved_model = str(emb_model or settings_model).strip()

    emb_base_url = (
        base_url
        if isinstance(base_url, str) and base_url.strip()
        else _attr_from_embedder_or_delegates(embedder, "base_url", "_base_url")
    )
    settings_base_url = getattr(s, "effective_embedding_base_url", None) or getattr(s, "embedding_base_url", None)
    resolved_base_url = str(emb_base_url or settings_base_url or "").strip().rstrip("/").lower()

    raw_dim: Any = dimensions
    if raw_dim is None:
        raw_dim = _attr_from_embedder_or_delegates(embedder, "schema_dimensions", "dimensions", "dim")
    if raw_dim is None and (not emb_model or str(emb_model).strip() == settings_model):
        raw_dim = getattr(s, "embedding_dimensions", None)
    resolved_dim = resolve_embedding_dimensions(resolved_model, raw_dim)
    return f"emb:{resolved_base_url}|{resolved_model}|{resolved_dim}"


def collection_meta_point_id(collection_name: str) -> str:
    """Deterministic UUIDv5 point ID for Qdrant collection embedding identity metadata."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:qdrant:collection_meta:{collection_name}"))


def contextualize_block_text(section_path: str | None, text: str) -> str:
    """Prepend hierarchical section_path once when absent from raw block text."""
    raw_t = (text or "").strip()
    sec_path = (section_path or "").strip()
    if sec_path and sec_path not in raw_t:
        return f"{sec_path}\n\n{raw_t}"
    return raw_t


def _embedding_cache_key(text: str, model_signature: str) -> str:
    if not isinstance(model_signature, str) or not model_signature.strip():
        raise ValueError("model_signature must be a non-empty string for _SHARED_TEXT_EMBEDDING_CACHE")
    sig = model_signature.strip()
    payload = f"{sig}\x00{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached_text_embedding(text: str, *, model_signature: str) -> list[float] | None:
    key = _embedding_cache_key(text, model_signature)
    vec = _SHARED_TEXT_EMBEDDING_CACHE.get(key)
    return list(vec) if vec is not None else None


def put_cached_text_embedding(
    text: str,
    vector: Sequence[float],
    *,
    model_signature: str,
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


def compute_sparse_bm25_vector(
    text: str,
    *,
    avg_doc_len: float = 256.0,
    k1: float = 1.5,
    b: float = 0.75,
    corpus_doc_freqs: dict[str, int] | None = None,
    total_corpus_docs: int = 1000,
) -> dict[str, list]:
    """Compute BM25 saturated term-frequency sparse vector using the unified Unicode tokenizer."""
    import math
    from collections import Counter

    tokenized = tokenize_bm25_tokens(text, filter_stopwords=True)
    if not tokenized.tokens:
        return {"indices": [], "values": []}

    doc_len = len(tokenized.tokens)
    counts = Counter(tokenized.expanded_tokens)
    sparse_map: dict[int, float] = {}

    for word, tf in counts.items():
        if corpus_doc_freqs is not None:
            if word in corpus_doc_freqs:
                df = corpus_doc_freqs[word]
                idf = math.log(1.0 + (total_corpus_docs - df + 0.5) / (df + 0.5))
            else:
                idf = math.log(1.0 + (total_corpus_docs - 1 + 0.5) / (1 + 0.5))
        else:
            idf = 1.0

        len_norm = 1.0 - b + b * (doc_len / avg_doc_len)
        tf_weight = (tf * (k1 + 1.0)) / (tf + k1 * len_norm)
        bm25_weight = idf * tf_weight

        token_idx = token_hash_bucket(word)
        sparse_map[token_idx] = max(sparse_map.get(token_idx, 0.0), round(bm25_weight, 4))
        stem = english_stem(word)
        if stem:
            stem_idx = token_hash_bucket(stem)
            sparse_map[stem_idx] = max(
                sparse_map.get(stem_idx, 0.0),
                round(bm25_weight * BM25_STEM_WEIGHT, 4),
            )

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
    ("authority_tier", "integer"),
    ("effective_from_ts", "integer"),
    ("scope_tags", "keyword"),
]

_VERIFIED_CLIENT_COLLECTIONS: WeakKeyDictionary[Any, set[tuple[str, int, str]]] = WeakKeyDictionary()
_CLIENT_COLLECTION_LOCKS: WeakKeyDictionary[Any, dict[str, asyncio.Lock]] = WeakKeyDictionary()


async def ensure_qdrant_collection_and_indexes(
    qdrant_client: httpx.AsyncClient,
    collection_name: str,
    vector_dim: int | None = None,
    *,
    embedding_signature: str | None = None,
    embedding_model: str | None = None,
    embedding_base_url: str | None = None,
) -> None:
    """Ensure per-project collection exists with dense+sparse config, model metadata, and payload indexes."""
    if vector_dim is None or isinstance(vector_dim, bool) or int(vector_dim) <= 0:
        raise ValueError(
            f"Qdrant collection {collection_name} requires a positive vector_dim, got {vector_dim!r}"
        )
    vector_dim = int(vector_dim)
    resolved_model = (embedding_model or getattr(settings, "embedding_model", "") or "").strip()
    resolved_base_url = (
        embedding_base_url
        or getattr(settings, "effective_embedding_base_url", None)
        or getattr(settings, "embedding_base_url", None)
        or ""
    ).strip().rstrip("/").lower()
    resolved_sig = (
        embedding_signature.strip()
        if isinstance(embedding_signature, str) and embedding_signature.strip()
        else build_embedding_model_signature(
            model=resolved_model,
            base_url=resolved_base_url,
            dimensions=vector_dim,
        )
    )

    verified_set = _VERIFIED_CLIENT_COLLECTIONS.get(qdrant_client)
    if verified_set is not None and (collection_name, vector_dim, resolved_sig) in verified_set:
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
        if verified_set is not None and (collection_name, vector_dim, resolved_sig) in verified_set:
            return

        # 0. Check if collection already exists to verify vector dimension compatibility and existing indexes
        collection_ready = False
        collection_verified = False
        existing_indexed_fields: set[str] = set()
        info_res: httpx.Response | None = None
        try:
            info_res = await qdrant_client.get(f"/collections/{collection_name}")
        except (httpx.HTTPError, TimeoutError, OSError) as check_exc:
            log.warning("Collection check skipped or non-fatal: %s", sanitize_error_message(check_exc))
            info_res = None

        if info_res is not None and info_res.is_success:
            try:
                raw_info = info_res.json()
                result_data = raw_info.get("result", {}) if isinstance(raw_info, dict) else {}
            except (ValueError, TypeError) as parse_exc:
                log.warning(
                    "Invalid JSON from Qdrant collection check on %s: %s",
                    collection_name,
                    sanitize_error_message(parse_exc),
                )
                result_data = {}
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
                        sparse_cfg = existing_sparse.get("bm25_sparse")
                        if isinstance(sparse_cfg, dict) and sparse_cfg.get("modifier") != "idf":
                            try:
                                patch_res = await qdrant_client.patch(
                                    f"/collections/{collection_name}",
                                    json={"sparse_vectors": {"bm25_sparse": {"modifier": "idf"}}},
                                )
                                if patch_res.status_code >= 400:
                                    log.warning(
                                        "Could not enable Qdrant IDF modifier on %s: HTTP %s",
                                        collection_name,
                                        patch_res.status_code,
                                    )
                            except (httpx.HTTPError, TimeoutError, OSError) as patch_exc:
                                log.warning(
                                    "Skipping Qdrant IDF modifier patch on %s: %s",
                                    collection_name,
                                    sanitize_error_message(patch_exc),
                                )
                        collection_ready = True
                        collection_verified = True
                payload_schema = result_data.get("payload_schema")
                if isinstance(payload_schema, dict):
                    existing_indexed_fields = set(payload_schema.keys())

        # 1. Create collection with dense and sparse vectors when not already verified
        if not collection_ready:
            col_res: httpx.Response | None = None
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
            except (httpx.HTTPError, TimeoutError, OSError) as exc:
                log.warning(
                    "Could not ensure Qdrant collection %s: %s",
                    collection_name,
                    sanitize_error_message(exc),
                )
            if col_res is not None:
                if col_res.status_code >= 400 and col_res.status_code != 409:
                    err_text = col_res.text.lower()
                    if "already exists" not in err_text:
                        raise RuntimeError(
                            f"Failed to create Qdrant collection {collection_name}: HTTP {col_res.status_code} - {col_res.text}"
                        )
                collection_verified = True

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

        # 3. Validate or record deterministic collection embedding metadata marker point
        meta_id = collection_meta_point_id(collection_name)
        meta_exists_matching = False
        meta_res = await qdrant_client.get(
            f"/collections/{collection_name}/points/{meta_id}",
            params={"with_payload": "true", "with_vector": "false"},
        )
        if meta_res.is_success:
            meta_result = meta_res.json().get("result")
            meta_payload = meta_result.get("payload") if isinstance(meta_result, dict) else None
            if isinstance(meta_payload, dict) and meta_payload.get("_collection_meta") is True:
                existing_sig = str(meta_payload.get("embedding_signature") or "").strip()
                if existing_sig and existing_sig != resolved_sig:
                    raise ValueError(
                        f"Qdrant collection {collection_name} embedding model signature mismatch: "
                        f"existing {existing_sig!r} vs required {resolved_sig!r}"
                    )
                if existing_sig == resolved_sig:
                    meta_exists_matching = True
        elif meta_res.status_code != 404:
            raise RuntimeError(
                f"Failed to inspect collection metadata marker for {collection_name}: "
                f"HTTP {meta_res.status_code} - {meta_res.text}"
            )

        if not meta_exists_matching:
            meta_put = await qdrant_client.put(
                f"/collections/{collection_name}/points?wait=true",
                json={
                    "points": [
                        {
                            "id": meta_id,
                            "payload": {
                                "_collection_meta": True,
                                "payload_only": True,
                                "embedding_model": resolved_model,
                                "embedding_base_url": resolved_base_url,
                                "embedding_dimensions": vector_dim,
                                "embedding_signature": resolved_sig,
                            },
                            "vector": {
                                "content_vector": [0.0] * vector_dim,
                            },
                        }
                    ]
                },
            )
            if meta_put.status_code >= 400:
                raise RuntimeError(
                    f"Failed to write collection metadata marker for {collection_name}: "
                    f"HTTP {meta_put.status_code} - {meta_put.text}"
                )

        if collection_verified:
            try:
                vset = _VERIFIED_CLIENT_COLLECTIONS.get(qdrant_client)
                if vset is None:
                    vset = set()
                    _VERIFIED_CLIENT_COLLECTIONS[qdrant_client] = vset
                vset.add((collection_name, vector_dim, resolved_sig))
            except TypeError:
                pass


def _unit_normalize_vec(vec: Sequence[float]) -> list[float]:
    import math

    norm = math.sqrt(math.fsum(float(x) * float(x) for x in vec))
    if norm <= 1e-12:
        return [float(x) for x in vec]
    inv = 1.0 / norm
    return [float(x) * inv for x in vec]


def _pool_weighted_vectors(vectors: Sequence[Sequence[float]], weights: Sequence[float]) -> list[float]:
    import math

    if not vectors:
        return []
    if len(vectors) == 1:
        return [float(x) for x in vectors[0]]
    dim = len(vectors[0])
    acc = [0.0] * dim
    for vec, w in zip(vectors, weights, strict=True):
        uvec = _unit_normalize_vec(vec)
        scale = math.sqrt(max(1.0, float(w)))
        for idx, val in enumerate(uvec):
            acc[idx] += scale * val
    return _unit_normalize_vec(acc)


def _segment_section_body_blocks_dp(
    body_blocks: Sequence[CanonicalBlock],
    body_tokens: Sequence[int],
    *,
    block_vectors: dict[str, list[float]] | None,
    lead_heading_tokens: int,
    max_tokens_per_unit: int,
) -> list[tuple[int, int]]:
    """Optimal semantic vector partitioning of contiguous body blocks within a single section.

    When dense block embeddings are available, uses Dynamic Programming over unit-normalized
    block vectors with an adaptive local-cohesion threshold to merge cohesive sub-topic blocks
    (and micro lead-in lines) while splitting cleanly at semantic topic valleys.
    """
    import math

    m = len(body_blocks)
    if m == 0:
        return []

    has_all_vecs = bool(
        m >= 2
        and block_vectors
        and all(b.id in block_vectors and len(block_vectors[b.id]) > 0 for b in body_blocks)
    )
    if not has_all_vecs:
        spans: list[tuple[int, int]] = []
        start = 0
        cur_toks = lead_heading_tokens if (lead_heading_tokens + body_tokens[0]) <= max_tokens_per_unit else 0
        for idx, b_toks in enumerate(body_tokens):
            if idx > start and (cur_toks + b_toks) > max_tokens_per_unit:
                spans.append((start, idx))
                start = idx
                cur_toks = b_toks
            else:
                cur_toks += b_toks
        spans.append((start, m))
        return spans

    assert block_vectors is not None
    uvecs = [_unit_normalize_vec(block_vectors[b.id]) for b in body_blocks]
    dim = len(uvecs[0])
    adj_cos = [
        math.fsum(uvecs[i][d] * uvecs[i + 1][d] for d in range(dim))
        for i in range(m - 1)
    ]
    mean_adj = sum(adj_cos) / len(adj_cos)
    var_adj = sum((c - mean_adj) ** 2 for c in adj_cos) / len(adj_cos)
    std_adj = math.sqrt(max(0.0, var_adj))
    tau = max(0.62, min(0.78, mean_adj - 0.35 * std_adj))

    prefix_toks = [0] * (m + 1)
    for i, t_cnt in enumerate(body_tokens):
        prefix_toks[i + 1] = prefix_toks[i] + t_cnt

    lead_fits_first = (lead_heading_tokens + body_tokens[0]) <= max_tokens_per_unit
    neg_inf = -1e18
    dp = [neg_inf] * (m + 1)
    prev_cut = [0] * (m + 1)
    dp[0] = 0.0

    for b in range(1, m + 1):
        best_val = neg_inf
        best_a = b - 1
        for a in range(b - 1, -1, -1):
            extra_lead = lead_heading_tokens if (a == 0 and lead_fits_first) else 0
            seg_toks = (prefix_toks[b] - prefix_toks[a]) + extra_lead
            if b - a > 1 and seg_toks > max_tokens_per_unit:
                break

            if b - a == 1:
                micro_pen = 0.35 * (40 - body_tokens[a]) if body_tokens[a] < 40 else 0.0
                cand = dp[a] - micro_pen
            else:
                min_adj = min(adj_cos[a : b - 1])
                has_micro = any(body_tokens[k] < 40 for k in range(a, b))
                if min_adj < 0.52 or (not has_micro and min_adj < tau):
                    continue

                centroid = [0.0] * dim
                for k in range(a, b):
                    w_k = math.sqrt(max(1.0, float(body_tokens[k])))
                    vk = uvecs[k]
                    for d in range(dim):
                        centroid[d] += w_k * vk[d]
                c_norm = math.sqrt(math.fsum(x * x for x in centroid)) or 1.0
                inv_c = 1.0 / c_norm
                centroid = [x * inv_c for x in centroid]

                mem_sims = [
                    math.fsum(uvecs[k][d] * centroid[d] for d in range(dim))
                    for k in range(a, b)
                ]
                min_mem = min(mem_sims)
                if not ((min_adj >= tau and min_mem >= tau) or (has_micro and min_adj >= 0.52 and seg_toks <= 384)):
                    continue

                cohesion_gain = sum(
                    body_tokens[a + idx] * max(0.0, sim_val - tau)
                    for idx, sim_val in enumerate(mem_sims)
                )
                size_over_sweet = max(0, seg_toks - 384) * 0.08
                cand = dp[a] + cohesion_gain + 4.0 * (b - a - 1) - size_over_sweet

            if cand > best_val:
                best_val = cand
                best_a = a
        dp[b] = best_val
        prev_cut[b] = best_a

    cuts: list[tuple[int, int]] = []
    cur = m
    while cur > 0:
        a = prev_cut[cur]
        cuts.append((a, cur))
        cur = a
    cuts.reverse()
    return cuts


def resolve_search_unit_text(unit: Any, unit_blocks: Sequence[Any]) -> str:
    """Resolve exact SearchUnit text, preferring text_snapshot or single-block [char_start:char_end] slice."""
    snapshot = getattr(unit, "text_snapshot", None)
    if snapshot is not None and snapshot != "":
        return str(snapshot)

    block_from_id = getattr(unit, "block_from_id", None)
    block_to_id = getattr(unit, "block_to_id", None)
    char_start = getattr(unit, "char_start", None)
    char_end = getattr(unit, "char_end", None)
    if (
        unit_blocks
        and block_from_id is not None
        and block_from_id == block_to_id
        and char_start is not None
        and char_end is not None
    ):
        base_text = str(getattr(unit_blocks[0], "normalized_text", "") or "")
        start_i = int(char_start)
        end_i = int(char_end)
        if 0 <= start_i <= end_i <= len(base_text):
            return base_text[start_i:end_i]

    return "\n\n".join(
        str(getattr(b, "normalized_text", "") or "")
        for b in unit_blocks
        if getattr(b, "normalized_text", None)
        and not bool(getattr(b, "is_boilerplate", False))
    )


def _locate_monotonic_span(norm_text: str, body_text: str, cursor: int) -> tuple[int, int]:
    """Locate monotonic (char_start, char_end) bounds in norm_text starting at cursor."""
    n = len(norm_text)
    safe_cursor = max(0, min(cursor, n))
    if not body_text:
        return safe_cursor, safe_cursor

    exact_pos = norm_text.find(body_text, safe_cursor)
    if exact_pos != -1:
        return exact_pos, exact_pos + len(body_text)

    tokens = body_text.split()
    if not tokens:
        return safe_cursor, safe_cursor

    start_pos = norm_text.find(tokens[0], safe_cursor)
    if start_pos == -1:
        return safe_cursor, safe_cursor

    scan_pos = start_pos + len(tokens[0])
    for tok in tokens[1:]:
        found = norm_text.find(tok, scan_pos)
        if found != -1:
            scan_pos = found + len(tok)
    return start_pos, max(start_pos, min(scan_pos, n))


def build_search_units_from_blocks(
    blocks: Sequence[CanonicalBlock],
    *,
    document_version_id: str,
    security_partition_id: str,
    max_tokens_per_unit: int = 512,
    block_vectors: dict[str, list[float]] | None = None,
) -> list[SearchUnit]:
    """Group canonical blocks into search units using section boundaries and semantic vector cohesion."""
    from sag_api.parsing.canonical import _split_atomic_spans, estimate_unicode_tokens

    units: list[SearchUnit] = []
    filtered_blocks = [b for b in blocks if not bool(getattr(b, "is_boilerplate", False))]
    if not filtered_blocks:
        return units
    has_body_blocks = any(b.block_type != "heading" for b in filtered_blocks)

    def _emit_group(group: Sequence[CanonicalBlock], *, is_subblock_split: bool = False) -> None:
        if not group:
            return
        first = group[0]
        last = group[-1]
        combined_text = "\n\n".join(b.normalized_text for b in group)
        content_hash = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()
        ordinal = len(units)
        tok_count = sum(max(1, estimate_unicode_tokens(b.normalized_text)) for b in group)
        unit = SearchUnit(
            id=generate_search_unit_id(document_version_id, ordinal),
            document_version_id=document_version_id,
            block_from_id=first.id,
            block_to_id=last.id,
            security_partition_id=security_partition_id,
            content_hash=content_hash,
            token_count=tok_count,
            page_from=first.page_from,
            page_to=last.page_to,
            section_path=last.section_path or first.section_path,
            char_start=None,
            char_end=None,
            text_snapshot=None,
        )
        unit._text_content = combined_text
        unit._body_block_ids = [b.id for b in group if b.block_type != "heading"]
        unit._is_subblock_split = is_subblock_split
        units.append(unit)

    def _emit_oversized_block(block: CanonicalBlock) -> None:
        norm_text = block.normalized_text or ""
        lines = norm_text.splitlines()
        sub_entries: list[tuple[str, str]] = []
        if len(lines) > 1:
            table_header_lines: list[str] = []
            body_start = 0
            if block.block_type == "table" or (
                lines[0].strip().startswith("|") and lines[0].strip().endswith("|")
            ):
                table_header_lines = [lines[0]]
                body_start = 1
                if len(lines) > 1 and set(lines[1].strip()) <= {"|", "-", ":", " "}:
                    table_header_lines.append(lines[1])
                    body_start = 2
            header_tokens = sum(estimate_unicode_tokens(hl) for hl in table_header_lines)
            effective_budget = max(64, max_tokens_per_unit - header_tokens)
            cur_lines: list[str] = []
            cur_toks = 0
            for ln in lines[body_start:]:
                ln_toks = max(1, estimate_unicode_tokens(ln))
                if cur_lines and (cur_toks + ln_toks) > effective_budget:
                    prefix = table_header_lines if table_header_lines else []
                    body_text = "\n".join(cur_lines)
                    sub_entries.append(("\n".join([*prefix, *cur_lines]), body_text))
                    cur_lines = [ln]
                    cur_toks = ln_toks
                else:
                    cur_lines.append(ln)
                    cur_toks += ln_toks
            if cur_lines:
                prefix = table_header_lines if table_header_lines else []
                body_text = "\n".join(cur_lines)
                sub_entries.append(("\n".join([*prefix, *cur_lines]), body_text))
            if not sub_entries and table_header_lines:
                header_text = "\n".join(table_header_lines)
                sub_entries.append((header_text, header_text))
            elif not sub_entries and norm_text.strip():
                sub_entries.append((norm_text, norm_text))
        else:
            atomic_spans = _split_atomic_spans(norm_text, max_span_tokens=max_tokens_per_unit)
            cur_spans: list[str] = []
            cur_toks = 0
            for sp in atomic_spans:
                sp_toks = max(1, estimate_unicode_tokens(sp))
                if sp_toks > max_tokens_per_unit:
                    if cur_spans:
                        chunk_t = " ".join(cur_spans)
                        sub_entries.append((chunk_t, chunk_t))
                        cur_spans = []
                        cur_toks = 0
                    words = sp.split()
                    if len(words) > max_tokens_per_unit:
                        for i in range(0, len(words), max_tokens_per_unit):
                            chunk_t = " ".join(words[i : i + max_tokens_per_unit])
                            sub_entries.append((chunk_t, chunk_t))
                    else:
                        for i in range(0, len(sp), max_tokens_per_unit):
                            chunk_t = sp[i : i + max_tokens_per_unit]
                            sub_entries.append((chunk_t, chunk_t))
                elif cur_spans and (cur_toks + sp_toks) > max_tokens_per_unit:
                    chunk_t = " ".join(cur_spans)
                    sub_entries.append((chunk_t, chunk_t))
                    cur_spans = [sp]
                    cur_toks = sp_toks
                else:
                    cur_spans.append(sp)
                    cur_toks += sp_toks
            if cur_spans:
                chunk_t = " ".join(cur_spans)
                sub_entries.append((chunk_t, chunk_t))

        is_split = len(sub_entries) > 1
        cursor = 0
        for chunk_text, body_anchor in sub_entries:
            pos = norm_text.find(chunk_text, cursor)
            if pos != -1:
                char_start = pos
                char_end = pos + len(chunk_text)
                text_snapshot: str | None = None
                cursor = char_end
            else:
                char_start, char_end = _locate_monotonic_span(norm_text, body_anchor, cursor)
                text_snapshot = chunk_text
                cursor = char_end

            chunk_tok_count = max(1, estimate_unicode_tokens(chunk_text))
            chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            ordinal = len(units)
            unit = SearchUnit(
                id=generate_search_unit_id(document_version_id, ordinal),
                document_version_id=document_version_id,
                block_from_id=block.id,
                block_to_id=block.id,
                security_partition_id=security_partition_id,
                content_hash=chunk_hash,
                token_count=chunk_tok_count,
                page_from=block.page_from,
                page_to=block.page_to,
                section_path=block.section_path,
                char_start=char_start,
                char_end=char_end,
                text_snapshot=text_snapshot,
            )
            unit._text_content = chunk_text
            unit._body_block_ids = [block.id] if block.block_type != "heading" else []
            unit._is_subblock_split = is_split
            units.append(unit)

    lead_headings: list[CanonicalBlock] = []
    section_body: list[CanonicalBlock] = []

    def _flush_section() -> None:
        nonlocal lead_headings, section_body
        if not lead_headings and not section_body:
            return
        if not section_body:
            if not has_body_blocks:
                _emit_group(lead_headings)
            lead_headings = []
            return

        lead_toks = sum(max(1, estimate_unicode_tokens(h.normalized_text)) for h in lead_headings)
        body_toks = [max(1, estimate_unicode_tokens(b.normalized_text)) for b in section_body]
        cuts = _segment_section_body_blocks_dp(
            section_body,
            body_toks,
            block_vectors=block_vectors,
            lead_heading_tokens=lead_toks,
            max_tokens_per_unit=max_tokens_per_unit,
        )
        for seg_idx, (a, b) in enumerate(cuts):
            seg_blocks = list(section_body[a:b])
            if seg_idx == 0 and lead_headings:
                seg_tok_sum = sum(body_toks[a:b])
                if (lead_toks + seg_tok_sum) <= max_tokens_per_unit:
                    seg_blocks = [*lead_headings, *seg_blocks]
            _emit_group(seg_blocks)
        lead_headings = []
        section_body = []

    for block in filtered_blocks:
        block_tokens = max(1, estimate_unicode_tokens(block.normalized_text))
        if block.block_type == "heading":
            if section_body:
                _flush_section()
            if block_tokens > max_tokens_per_unit:
                if not has_body_blocks:
                    if lead_headings:
                        _emit_group(lead_headings)
                        lead_headings = []
                    _emit_oversized_block(block)
            else:
                lead_headings.append(block)
            continue

        if block_tokens > max_tokens_per_unit:
            if section_body:
                _flush_section()
            else:
                # Section heading is already attached via block.section_path; never emit a heading-only unit.
                lead_headings = []
            _emit_oversized_block(block)
            continue

        section_body.append(block)

    _flush_section()
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
    embedding_model_signature: str | None = None,
) -> dict[str, Any]:
    """Build Qdrant point payload including pre-provisioned tree routing fields and retrieval locators."""
    valid_from_utc = _as_utc(version.valid_from)
    valid_to_utc = _as_utc(version.valid_to)
    effective_from_utc = _as_utc(getattr(version, "effective_from", None))
    valid_from_iso = valid_from_utc.isoformat() if valid_from_utc else None
    valid_to_iso = valid_to_utc.isoformat() if valid_to_utc else None
    valid_from_ts = int(valid_from_utc.timestamp()) if valid_from_utc else None
    valid_to_ts = int(valid_to_utc.timestamp()) if valid_to_utc else None
    effective_from_iso = effective_from_utc.isoformat() if effective_from_utc else None
    effective_from_ts = int(effective_from_utc.timestamp()) if effective_from_utc else None
    authority_tier = getattr(version, "authority_tier", None)
    scope_tags = normalize_scope_tags(getattr(version, "scope_tags_json", None))

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
        "valid_from": valid_from_iso,
        "valid_to": valid_to_iso,
        "valid_from_ts": valid_from_ts,
        "valid_to_ts": valid_to_ts,
        "valid_from_iso": valid_from_iso,
        "valid_to_iso": valid_to_iso,
        "authority_tier": authority_tier,
        "effective_from": effective_from_iso,
        "effective_from_ts": effective_from_ts,
        "scope_tags": scope_tags,
        "token_count": unit.token_count,
        "section_path": unit.section_path,
        "page_from": unit.page_from,
        "page_to": unit.page_to,
        "content_hash": unit.content_hash,
        "content": content or getattr(unit, "_text_content", ""),
        "embedding_model_signature": embedding_model_signature,
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


async def precompute_canonical_block_embeddings(
    blocks: Sequence[CanonicalBlock],
    *,
    embedder: Any,
    project_id: str,
    tenant_id: str,
    document_id: str | None = None,
    document_version_id: str | None = None,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Pre-embed non-heading CanonicalBlocks once (with section_path context) so SearchUnit DP
    segmentation and KnowledgeWorker both reuse exact dense vectors at zero duplicate AI cost.
    """
    from sag_api.core.token_usage import (
        TokenLedger,
        estimate_text_tokens,
        record_embedding_cache_hit,
        token_usage_scope,
    )

    non_heading = [
        b
        for b in blocks
        if b.block_type != "heading"
        and not bool(getattr(b, "is_boilerplate", False))
        and (b.normalized_text or "").strip()
    ]
    if not non_heading or embedder is None:
        return {}, TokenLedger().to_summary()

    use_cache = hasattr(embedder, "batch_generate")
    emb_model = getattr(embedder, "model", None) or getattr(settings, "embedding_model", None)
    emb_sig = build_embedding_model_signature(embedder, settings_obj=settings)
    block_ctx_by_id: dict[str, str] = {
        b.id: contextualize_block_text(b.section_path, b.normalized_text)
        for b in non_heading
    }
    unique_texts = list(dict.fromkeys(block_ctx_by_id.values()))
    resolved_by_text: dict[str, list[float]] = {}
    missing_texts: list[str] = []
    cache_hits = 0

    if use_cache:
        for txt in unique_texts:
            cached = get_cached_text_embedding(txt, model_signature=emb_sig)
            if cached is not None:
                resolved_by_text[txt] = cached
                cache_hits += 1
            else:
                missing_texts.append(txt)
    else:
        missing_texts = unique_texts

    with token_usage_scope(
        component="search_index_embedding",
        project_id=project_id,
        tenant_id=tenant_id,
        document_id=document_id,
        document_version_id=document_version_id,
    ) as pre_ledger:
        if cache_hits > 0:
            record_embedding_cache_hit(model=emb_model, count=cache_hits)
        if missing_texts:
            fetched_vecs = await compute_unit_embeddings(embedder, missing_texts)
            for txt, vec in zip(missing_texts, fetched_vecs, strict=True):
                vec_f = [float(x) for x in vec]
                if use_cache:
                    put_cached_text_embedding(txt, vec_f, model_signature=emb_sig)
                resolved_by_text[txt] = vec_f
        pre_ledger.set_metadata(
            canonical_block_texts_count=len(unique_texts),
            canonical_block_estimated_tokens=estimate_text_tokens(unique_texts),
        )

    block_vectors = {
        bid: list(resolved_by_text[ctx_t])
        for bid, ctx_t in block_ctx_by_id.items()
        if ctx_t in resolved_by_text
    }
    return block_vectors, pre_ledger.to_summary()


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
    block_vectors: dict[str, list[float]] | None = None,
    precomputed_ledger_summary: dict[str, Any] | None = None,
) -> int:
    """Upsert search units with real dense + sparse BM25 embeddings into per-project collection."""
    from sag_api.core.token_usage import (
        TokenLedger,
        estimate_text_tokens,
        record_embedding_cache_hit,
        token_usage_scope,
    )
    from sag_api.parsing.canonical import estimate_unicode_tokens

    collection_name = f"search_units_{project_id}"
    if not units:
        return 0

    ordered_blocks = (
        sorted(blocks_by_id.values(), key=lambda blk: blk.ordinal)
        if blocks_by_id
        else []
    )
    texts: list[str] = []
    contextual_texts: list[str] = []
    for u in units:
        t = getattr(u, "_text_content", None)
        if not t:
            if getattr(u, "text_snapshot", None):
                t = resolve_search_unit_text(u, [])
            elif blocks_by_id:
                b1 = blocks_by_id.get(u.block_from_id)
                b2 = blocks_by_id.get(u.block_to_id)
                if b1 and b2:
                    if b1.id == b2.id:
                        unit_blocks = [b1]
                    else:
                        lo, hi = min(b1.ordinal, b2.ordinal), max(b1.ordinal, b2.ordinal)
                        unit_blocks = [
                            b
                            for b in ordered_blocks
                            if lo <= b.ordinal <= hi and not bool(getattr(b, "is_boilerplate", False))
                        ] or [b1, b2]
                    t = resolve_search_unit_text(u, unit_blocks)
        raw_t = t or u.section_path or u.content_hash
        texts.append(raw_t)
        contextual_texts.append(contextualize_block_text(u.section_path, raw_t))

    combined_ledger = TokenLedger()
    if precomputed_ledger_summary:
        combined_ledger.merge_summary(precomputed_ledger_summary)

    _su_est_tokens = estimate_text_tokens(contextual_texts)
    _preload_block_count = 0
    _preload_est_tokens = 0
    use_cache = hasattr(embedder, "batch_generate")
    emb_model = getattr(embedder, "model", None) or getattr(settings, "embedding_model", None)
    emb_sig = build_embedding_model_signature(embedder, settings_obj=settings)

    if block_vectors:
        resolved_vectors: dict[int, list[float]] = {}
        fallback_indices: list[int] = []
        fallback_texts: list[str] = []
        cache_hits = 0

        for idx, (u, ctx_t) in enumerate(zip(units, contextual_texts, strict=True)):
            is_subblock = bool(getattr(u, "_is_subblock_split", False))
            body_ids = getattr(u, "_body_block_ids", None)
            if body_ids is None and blocks_by_id and not is_subblock:
                b1 = blocks_by_id.get(u.block_from_id)
                b2 = blocks_by_id.get(u.block_to_id)
                if b1 and b2:
                    lo, hi = min(b1.ordinal, b2.ordinal), max(b1.ordinal, b2.ordinal)
                    body_ids = [
                        b.id
                        for b in ordered_blocks
                        if lo <= b.ordinal <= hi
                        and b.block_type != "heading"
                        and not bool(getattr(b, "is_boilerplate", False))
                    ]
            if (
                not is_subblock
                and body_ids
                and all(bid in block_vectors for bid in body_ids)
            ):
                if len(body_ids) == 1:
                    resolved_vectors[idx] = list(block_vectors[body_ids[0]])
                else:
                    weights = [
                        float(
                            estimate_unicode_tokens(blocks_by_id[bid].normalized_text)
                            if blocks_by_id and bid in blocks_by_id
                            else 1
                        )
                        for bid in body_ids
                    ]
                    resolved_vectors[idx] = _pool_weighted_vectors(
                        [block_vectors[bid] for bid in body_ids],
                        weights,
                    )
            elif use_cache and (cached_v := get_cached_text_embedding(ctx_t, model_signature=emb_sig)) is not None:
                resolved_vectors[idx] = cached_v
                cache_hits += 1
            else:
                fallback_indices.append(idx)
                fallback_texts.append(ctx_t)

        if fallback_texts or cache_hits > 0:
            with token_usage_scope(
                component="search_index_embedding",
                project_id=project_id,
                tenant_id=tenant_id,
                document_id=document_id or getattr(version, "document_id", None),
                document_version_id=getattr(version, "id", None),
            ) as extra_ledger:
                if cache_hits > 0:
                    record_embedding_cache_hit(model=emb_model, count=cache_hits)
                if fallback_texts:
                    fetched = await compute_unit_embeddings(embedder, fallback_texts)
                    for idx, txt, vec in zip(fallback_indices, fallback_texts, fetched, strict=True):
                        vec_f = [float(x) for x in vec]
                        if use_cache:
                            put_cached_text_embedding(txt, vec_f, model_signature=emb_sig)
                        resolved_vectors[idx] = vec_f
            combined_ledger.merge_summary(extra_ledger.to_summary())

        vectors = [resolved_vectors[i] for i in range(len(units))]
    else:
        with token_usage_scope(
            component="search_index_embedding",
            project_id=project_id,
            tenant_id=tenant_id,
            document_id=document_id or getattr(version, "document_id", None),
            document_version_id=getattr(version, "id", None),
        ) as direct_ledger:
            if use_cache and blocks_by_id:
                extra_block_texts: list[str] = []
                seen_texts = set(contextual_texts)
                for blk in blocks_by_id.values():
                    if (
                        blk.block_type == "heading"
                        or bool(getattr(blk, "is_boilerplate", False))
                        or not (blk.normalized_text or "").strip()
                    ):
                        continue
                    bt = contextualize_block_text(blk.section_path, blk.normalized_text)
                    if (
                        bt
                        and bt not in seen_texts
                        and get_cached_text_embedding(bt, model_signature=emb_sig) is None
                    ):
                        seen_texts.add(bt)
                        extra_block_texts.append(bt)
                _preload_block_count = len(extra_block_texts)
                _preload_est_tokens = estimate_text_tokens(extra_block_texts)
                combined_batch = list(contextual_texts) + extra_block_texts
                all_vectors = await compute_unit_embeddings(embedder, combined_batch)
                for txt, vec in zip(combined_batch, all_vectors, strict=True):
                    put_cached_text_embedding(txt, vec, model_signature=emb_sig)
                vectors = all_vectors[: len(contextual_texts)]
            else:
                vectors = await compute_unit_embeddings(embedder, contextual_texts)
                if use_cache:
                    for txt, vec in zip(contextual_texts, vectors, strict=True):
                        put_cached_text_embedding(txt, vec, model_signature=emb_sig)
        combined_ledger.merge_summary(direct_ledger.to_summary())

    combined_ledger.set_metadata(
        search_unit_texts_count=len(contextual_texts),
        knowledge_block_preload_texts_count=_preload_block_count,
        search_unit_estimated_tokens=_su_est_tokens,
        knowledge_block_preload_estimated_tokens=_preload_est_tokens,
    )
    try:
        version._last_search_index_token_usage = combined_ledger.to_summary()
    except AttributeError:
        pass
    if not vectors or not vectors[0] or len(vectors[0]) <= 0:
        raise ValueError(
            "Cannot index SearchUnits to Qdrant: embedding vectors are empty or zero-dimensional"
        )
    vector_dim = len(vectors[0])
    if any(len(vec) != vector_dim for vec in vectors):
        raise ValueError(
            f"Cannot index SearchUnits to Qdrant: inconsistent vector dimensions (expected {vector_dim})"
        )

    # 1. Ensure collection exists, model metadata matches, and payload indexes are created
    emb_base_url = _attr_from_embedder_or_delegates(embedder, "base_url", "_base_url")
    emb_sig_for_collection = build_embedding_model_signature(
        embedder,
        settings_obj=settings,
        dimensions=vector_dim,
    )
    await ensure_qdrant_collection_and_indexes(
        qdrant_client,
        collection_name,
        vector_dim=vector_dim,
        embedding_signature=emb_sig_for_collection,
        embedding_model=emb_model,
        embedding_base_url=emb_base_url,
    )

    # 2. Build points with both dense and sparse representations (Qdrant native IDF modifier handles IDF)
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
            embedding_model_signature=emb_sig_for_collection,
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

    # 3. Upsert points with wait=true for strong consistency
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


class TemporalPayloadSyncError(RuntimeError):
    """Structured error raised when one or more Qdrant set_payload temporal sync calls fail."""

    def __init__(
        self,
        message: str,
        *,
        failed_version_ids: Sequence[str],
        synced_version_ids: Sequence[str] = (),
        errors_by_version: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_version_ids: list[str] = [str(v) for v in failed_version_ids]
        self.synced_version_ids: list[str] = [str(v) for v in synced_version_ids]
        self.errors_by_version: dict[str, str] = dict(errors_by_version or {})


def build_mutated_version_entries(
    versions: Sequence[DocumentVersion],
) -> list[dict[str, Any]]:
    """Build canonical mutated-version payload sync entries from ORM DocumentVersion rows."""
    entries: list[dict[str, Any]] = []
    for v in versions:
        cur_from = _as_utc(v.valid_from)
        cur_to = _as_utc(v.valid_to)
        eff_from = _as_utc(getattr(v, "effective_from", None))
        entries.append(
            {
                "version_id": str(v.id),
                "valid_from": cur_from,
                "valid_to": cur_to,
                "is_superseded": bool(cur_to is not None and cur_to < _MAX_VALID_TO),
                "authority_tier": getattr(v, "authority_tier", None),
                "effective_from": eff_from,
                "scope_tags": normalize_scope_tags(getattr(v, "scope_tags_json", None)),
            }
        )
    return entries


def _build_temporal_payload_patch(entry: dict[str, Any]) -> dict[str, Any]:
    """Build the 10-key Qdrant set_payload patch for temporal validity & authority metadata."""
    valid_from_utc = _as_utc(entry.get("valid_from"))
    valid_to_utc = _as_utc(entry.get("valid_to"))
    effective_from_utc = _as_utc(entry.get("effective_from"))

    valid_from_iso = valid_from_utc.isoformat() if valid_from_utc else None
    valid_to_iso = valid_to_utc.isoformat() if valid_to_utc else None
    valid_from_ts = int(valid_from_utc.timestamp()) if valid_from_utc else None
    valid_to_ts = int(valid_to_utc.timestamp()) if valid_to_utc else None
    effective_from_iso = effective_from_utc.isoformat() if effective_from_utc else None
    effective_from_ts = int(effective_from_utc.timestamp()) if effective_from_utc else None

    return {
        "valid_from": valid_from_iso,
        "valid_to": valid_to_iso,
        "valid_from_ts": valid_from_ts,
        "valid_to_ts": valid_to_ts,
        "valid_from_iso": valid_from_iso,
        "valid_to_iso": valid_to_iso,
        "authority_tier": entry.get("authority_tier"),
        "effective_from": effective_from_iso,
        "effective_from_ts": effective_from_ts,
        "scope_tags": normalize_scope_tags(entry.get("scope_tags")),
    }


async def sync_version_temporal_payloads_to_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    mutated_versions: Sequence[dict[str, Any]],
    request_timeout: float | None = None,
    allow_missing_collection: bool = False,
) -> list[str]:
    """Sync temporal & authority payload fields in Qdrant via POST /points/payload?wait=true."""
    deduped: dict[str, dict[str, Any]] = {}
    for entry in mutated_versions:
        vid = str(entry.get("version_id") or "").strip()
        if not vid:
            continue
        deduped[vid] = entry

    if not deduped:
        return []

    encoded_col = quote(f"search_units_{project_id}", safe="")
    payload_url = f"/collections/{encoded_col}/points/payload?wait=true"
    index_url = f"/collections/{encoded_col}/index?wait=true"

    req_kwargs: dict[str, Any] = {}
    if isinstance(request_timeout, httpx.Timeout):
        req_kwargs["timeout"] = request_timeout
    elif request_timeout is not None:
        timeout_sec = max(1.0, float(request_timeout))
        req_kwargs["timeout"] = httpx.Timeout(timeout_sec, connect=min(10.0, timeout_sec))

    synced_ids: list[str] = []
    failed_ids: list[str] = []
    errors_by_version: dict[str, str] = {}

    for vid, entry in deduped.items():
        patch = _build_temporal_payload_patch(entry)
        body = {
            "payload": patch,
            "filter": {
                "must": [
                    {"key": "project_id", "match": {"value": str(project_id)}},
                    {"key": "document_version_id", "match": {"value": vid}},
                ]
            },
        }
        try:
            res = await qdrant_client.post(payload_url, json=body, **req_kwargs)
            if res.status_code == 400 and "index required but not found" in res.text.lower():
                lower_text = res.text.lower()
                missing_fields = [
                    field_name
                    for field_name in ("project_id", "document_version_id")
                    if field_name in lower_text
                ]
                if not missing_fields:
                    missing_fields = ["project_id", "document_version_id"]
                for field_name in missing_fields:
                    idx_res = await qdrant_client.put(
                        index_url,
                        json={"field_name": field_name, "field_schema": "keyword"},
                        **req_kwargs,
                    )
                    if (
                        idx_res.status_code >= 400
                        and idx_res.status_code != 409
                        and "already exists" not in idx_res.text.lower()
                    ):
                        idx_res.raise_for_status()
                res = await qdrant_client.post(payload_url, json=body, **req_kwargs)

            if res.status_code == 404 and allow_missing_collection:
                synced_ids.append(vid)
                continue
            if res.status_code >= 400:
                failed_ids.append(vid)
                errors_by_version[vid] = sanitize_error_message(
                    f"HTTP {res.status_code} - {res.text}"
                )
                continue
            synced_ids.append(vid)
        except Exception as exc:
            failed_ids.append(vid)
            errors_by_version[vid] = sanitize_error_message(exc)

    if failed_ids:
        raise TemporalPayloadSyncError(
            f"Failed to sync temporal payloads in Qdrant for versions {failed_ids}",
            failed_version_ids=failed_ids,
            synced_version_ids=synced_ids,
            errors_by_version=errors_by_version,
        )
    return synced_ids


async def reconcile_version_temporal_payloads(
    session: AsyncSession,
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    version_ids: Sequence[str] | None = None,
    request_timeout: float = 30.0,
) -> dict[str, int]:
    """Repair Qdrant temporal & authority payloads from PostgreSQL truth and clear degraded flags."""
    from sag_api.db.models.document import Document

    if version_ids is None:
        proj_versions = (
            await session.execute(
                select(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.project_id == project_id)
            )
        ).scalars().all()
        discovered_ids: list[str] = []
        for ver in proj_versions:
            meta = ver.metadata_json or {}
            if meta.get("qdrant_temporal_sync_degraded") is not True:
                continue
            raw_failed = meta.get("qdrant_temporal_sync_failed_version_ids")
            failed_list = (
                [str(x).strip() for x in raw_failed if str(x).strip()]
                if isinstance(raw_failed, list)
                else []
            )
            if failed_list:
                discovered_ids.extend(failed_list)
            else:
                discovered_ids.append(str(ver.id))
        unique_ids = list(dict.fromkeys(discovered_ids))
    else:
        unique_ids = list(dict.fromkeys(str(v).strip() for v in version_ids if str(v).strip()))

    if not unique_ids:
        return {"synced_versions": 0, "requested_versions": 0}

    versions = (
        await session.execute(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                Document.project_id == project_id,
                Document.is_active.is_(True),
                DocumentVersion.id.in_(unique_ids),
                DocumentVersion.status.notin_(("FAILED", "REVOKED")),
                DocumentVersion.search_status.in_(("SEARCH_READY", "READY")),
            )
        )
    ).scalars().all()

    synced_ids: list[str] = []
    if versions:
        entries = build_mutated_version_entries(versions)
        synced_ids = await sync_version_temporal_payloads_to_qdrant(
            qdrant_client,
            project_id=project_id,
            mutated_versions=entries,
            request_timeout=request_timeout,
            allow_missing_collection=True,
        )

    synced_set = set(synced_ids)
    proj_rows = (
        await session.execute(
            select(DocumentVersion, Document.is_active)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.project_id == project_id)
        )
    ).all()
    active_ready_ids = {
        str(ver.id)
        for ver, doc_is_active in proj_rows
        if bool(doc_is_active)
        and ver.status not in ("FAILED", "REVOKED")
        and ver.search_status in ("SEARCH_READY", "READY")
    }

    mutated_any = False
    for ver, _doc_is_active in proj_rows:
        meta = dict(ver.metadata_json or {})
        has_degraded_marker = (
            meta.get("qdrant_temporal_sync_degraded") is True
            or "qdrant_temporal_sync_failed_version_ids" in meta
            or "qdrant_temporal_sync_degraded_reason" in meta
            or "qdrant_temporal_sync_error" in meta
        )
        if not has_degraded_marker:
            continue
        raw_failed = meta.get("qdrant_temporal_sync_failed_version_ids")
        failed_list = (
            [str(x).strip() for x in raw_failed if str(x).strip()]
            if isinstance(raw_failed, list)
            else []
        )
        ver_id = str(ver.id)
        if failed_list:
            resolved_ids = {
                fid for fid in failed_list if fid in synced_set or fid not in active_ready_ids
            }
            if not resolved_ids and ver_id not in synced_set:
                continue
            remaining_failed = [fid for fid in failed_list if fid not in resolved_ids]
        else:
            if ver_id not in synced_set and ver_id in active_ready_ids:
                continue
            remaining_failed = []
        if not remaining_failed:
            meta["qdrant_temporal_sync_degraded"] = False
            meta.pop("qdrant_temporal_sync_degraded_reason", None)
            meta.pop("qdrant_temporal_sync_error", None)
            meta.pop("qdrant_temporal_sync_failed_version_ids", None)
        else:
            meta["qdrant_temporal_sync_degraded"] = True
            meta["qdrant_temporal_sync_failed_version_ids"] = remaining_failed
            meta["qdrant_temporal_sync_degraded_reason"] = (
                f"temporal_payload_sync_failed:{','.join(remaining_failed)}"
            )
        ver.metadata_json = meta
        session.add(ver)
        mutated_any = True
    if mutated_any:
        await session.flush()

    return {"synced_versions": len(synced_ids), "requested_versions": len(unique_ids)}


async def reconcile_orphan_search_units(
    session: AsyncSession,
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    preserve_superseded: bool = True,
    prune_version_ids: Sequence[str] | None = None,
    inflight_grace_minutes: int = 15,
    request_timeout: float = 30.0,
) -> dict[str, int]:
    """Identify and delete orphan Qdrant points while protecting in-flight indexing and active history."""
    from sag_api.db.models.document import Document

    collection_name = f"search_units_{project_id}"
    timeout_sec = max(30.0, float(request_timeout))
    reconcile_timeout = httpx.Timeout(timeout_sec, connect=min(10.0, timeout_sec))

    all_points: list[dict[str, Any]] = []
    next_offset = None
    while True:
        payload: dict[str, Any] = {
            "limit": 500,
            "with_payload": ["search_unit_id", "document_version_id", "_collection_meta"],
            "with_vector": False,
        }
        if next_offset is not None:
            payload["offset"] = next_offset
        res = await qdrant_client.post(
            f"/collections/{collection_name}/points/scroll",
            json=payload,
            timeout=reconcile_timeout,
        )
        if not res.is_success:
            if res.status_code == 404 and next_offset is None and not all_points:
                return {"scanned": 0, "orphans_deleted": 0}
            raise RuntimeError(
                f"Failed to scroll Qdrant collection {collection_name}: HTTP {res.status_code} - {res.text}"
            )
        data = res.json().get("result", {})
        points = data.get("points", [])
        all_points.extend(points)
        prev_offset = next_offset
        next_offset = data.get("next_page_offset")
        if next_offset is None or not points or next_offset == prev_offset:
            break

    if not all_points:
        return {"scanned": 0, "orphans_deleted": 0}

    point_entries: list[tuple[Any, Any, Any]] = []
    for p in all_points:
        pid = p.get("id")
        if not pid:
            continue
        pt_payload = p.get("payload") or {}
        if pt_payload.get("_collection_meta") is True:
            continue
        point_entries.append(
            (pid, pt_payload.get("search_unit_id"), pt_payload.get("document_version_id"))
        )

    now = datetime.now(UTC)
    grace_cutoff = now - timedelta(minutes=max(0, int(inflight_grace_minutes)))

    targeted_prune_ids = {
        str(vid).strip() for vid in (prune_version_ids or ()) if str(vid).strip()
    }
    valid_unit_ids: set[str] = set()
    unit_ids = sorted({str(uid) for _pid, uid, _vid in point_entries if uid})
    if unit_ids:
        if preserve_superseded:
            if targeted_prune_ids:
                ready_condition = DocumentVersion.search_status.in_(("SEARCH_READY", "READY")) & (
                    DocumentVersion.id.notin_(targeted_prune_ids) | (DocumentVersion.valid_to > now)
                )
            else:
                ready_condition = DocumentVersion.search_status.in_(("SEARCH_READY", "READY"))
        else:
            ready_condition = DocumentVersion.search_status.in_(("SEARCH_READY", "READY")) & (
                DocumentVersion.valid_to > now
            )
        inflight_or_grace = or_(
            DocumentVersion.search_status.in_(("INDEXING", "PENDING")),
            ready_condition,
            (
                DocumentVersion.search_status.notin_(("INDEX_FAILED", "SEARCH_READY", "READY"))
                & (DocumentVersion.created_at >= grace_cutoff)
            ),
        )
        for i in range(0, len(unit_ids), 500):
            batch = unit_ids[i : i + 500]
            rows = (
                await session.execute(
                    select(SearchUnit.id)
                    .join(DocumentVersion, DocumentVersion.id == SearchUnit.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        SearchUnit.id.in_(batch),
                        Document.is_active.is_(True),
                        DocumentVersion.status.notin_(("FAILED", "REVOKED")),
                        inflight_or_grace,
                    )
                )
            ).scalars().all()
            valid_unit_ids.update(str(row) for row in rows)

    # Also protect uncommitted SearchUnit points whose DocumentVersion is actively INDEXING/PENDING
    inflight_version_ids: set[str] = set()
    candidate_vids = sorted(
        {
            str(vid)
            for _pid, uid, vid in point_entries
            if vid and (not uid or str(uid) not in valid_unit_ids)
        }
    )
    if candidate_vids:
        for i in range(0, len(candidate_vids), 500):
            v_batch = candidate_vids[i : i + 500]
            v_rows = (
                await session.execute(
                    select(DocumentVersion.id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        DocumentVersion.id.in_(v_batch),
                        Document.is_active.is_(True),
                        DocumentVersion.status.notin_(("FAILED", "REVOKED")),
                        DocumentVersion.search_status.in_(("INDEXING", "PENDING")),
                    )
                )
            ).scalars().all()
            inflight_version_ids.update(str(row) for row in v_rows)

    orphan_point_ids = [
        pid
        for pid, uid, vid in point_entries
        if not uid
        or (str(uid) not in valid_unit_ids and (not vid or str(vid) not in inflight_version_ids))
    ]
    if orphan_point_ids:
        for i in range(0, len(orphan_point_ids), 500):
            batch_ids = orphan_point_ids[i : i + 500]
            del_res = await qdrant_client.post(
                f"/collections/{collection_name}/points/delete?wait=true",
                json={"points": batch_ids},
                timeout=reconcile_timeout,
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
    try:
        document_version._last_reconcile_job_id = None
    except AttributeError:
        pass

    # Resolve authoritative tenant_id and doc from Document or IngestionRun
    from sag_api.db.models.document import Document
    from sag_api.db.models.routing_rag import IngestionRun

    doc = await session.get(Document, document_version.document_id)
    ing_run = await session.get(IngestionRun, run_id) if run_id else None
    if not tenant_id:
        if doc and doc.tenant_id:
            tenant_id = doc.tenant_id
        elif ing_run and ing_run.tenant_id:
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

    # 1b. Pre-embed CanonicalBlocks once (when embedder is provided) so SearchUnit DP segmentation
    # and KnowledgeWorker share 100% of block vectors with zero duplicate AI calls.
    block_vectors: dict[str, list[float]] | None = None
    pre_embed_summary: dict[str, Any] | None = None
    indexing_error: str | None = None
    indexing_exception: Exception | None = None
    if qdrant_client is not None and embedder is not None and blocks:
        try:
            block_vectors, pre_embed_summary = await precompute_canonical_block_embeddings(
                blocks,
                embedder=embedder,
                project_id=project_id,
                tenant_id=tenant_id,
                document_id=document_version.document_id,
                document_version_id=document_version.id,
            )
            if pre_embed_summary is not None:
                try:
                    document_version._last_search_index_token_usage = pre_embed_summary
                except AttributeError:
                    pass
        except Exception as exc:
            indexing_error = str(exc)
            indexing_exception = exc
            block_vectors = None

    # 2. Build search units with deterministic IDs (using semantic vector DP when block_vectors exist)
    units = build_search_units_from_blocks(
        blocks,
        document_version_id=document_version.id,
        security_partition_id=security_partition_id,
        block_vectors=block_vectors,
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
    if qdrant_client is not None and units and indexing_error is None:
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
                block_vectors=block_vectors,
                precomputed_ledger_summary=pre_embed_summary,
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

    sync_degraded = False
    sync_degraded_reason: str | None = None
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
        versions_to_sync: list[dict[str, Any]] = []
        if isinstance(temporal_info, dict) and temporal_info.get("mutated_versions"):
            versions_to_sync = list(temporal_info["mutated_versions"])
        else:
            versions_to_sync = build_mutated_version_entries([document_version])
            superseded_version_id = (
                temporal_info.get("supersedes_id") if isinstance(temporal_info, dict) else None
            )
            if (
                isinstance(temporal_info, dict)
                and temporal_info.get("action") == "SUPERSEDED"
                and superseded_version_id
            ):
                superseded_ver = await session.get(DocumentVersion, str(superseded_version_id))
                if superseded_ver is not None:
                    versions_to_sync.extend(build_mutated_version_entries([superseded_ver]))
                else:
                    versions_to_sync.append(
                        {
                            "version_id": str(superseded_version_id),
                            "valid_from": None,
                            "valid_to": _as_utc(document_version.valid_from),
                            "is_superseded": True,
                            "authority_tier": None,
                            "effective_from": None,
                            "scope_tags": [],
                        }
                    )

        if qdrant_client is not None and versions_to_sync:
            try:
                await sync_version_temporal_payloads_to_qdrant(
                    qdrant_client,
                    project_id=project_id,
                    mutated_versions=versions_to_sync,
                )
            except Exception as exc:
                failed_ids = (
                    list(exc.failed_version_ids)
                    if isinstance(exc, TemporalPayloadSyncError)
                    else [
                        str(entry["version_id"])
                        for entry in versions_to_sync
                        if entry.get("version_id")
                    ]
                )
                sync_degraded = True
                sync_degraded_reason = f"temporal_payload_sync_failed:{','.join(failed_ids)}"
                err_safe = sanitize_error_message(exc)
                log.warning(
                    "Failed to sync temporal payloads in Qdrant for versions %s "
                    "(marking degraded & scheduling reconcile): %s",
                    failed_ids,
                    err_safe,
                )
                document_version.metadata_json = {
                    **(document_version.metadata_json or {}),
                    "qdrant_temporal_sync_degraded": True,
                    "qdrant_temporal_sync_degraded_reason": sync_degraded_reason,
                    "qdrant_temporal_sync_error": err_safe,
                    "qdrant_temporal_sync_failed_version_ids": failed_ids,
                }
                from sag_api.db.models import Job
                from sag_api.enums import JobStatus, JobType

                reconcile_job = Job(
                    id=str(uuid.uuid4()),
                    type=JobType.RECONCILE_SEARCH_INDEX,
                    status=JobStatus.QUEUED,
                    source_id=doc.source_id if doc else None,
                    document_id=document_version.document_id,
                    payload={
                        "project_id": project_id,
                        "reason": "temporal_payload_sync_failed",
                        "sync_version_ids": failed_ids,
                    },
                )
                session.add(reconcile_job)
                try:
                    document_version._last_reconcile_job_id = reconcile_job.id
                except AttributeError:
                    pass
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

        stage_created_at = datetime.now(UTC)
        if ing_run is not None and ing_run.started_at is not None:
            run_started = _as_utc(ing_run.started_at)
            if run_started is not None and stage_created_at < run_started:
                stage_created_at = run_started

        stage_metrics: dict[str, Any] = {
            "search_unit_count": len(units),
            "pg_count": pg_count,
            "qdrant_count": qdrant_count,
            "qdrant_indexed_count": indexed_count,
            "manifest_checksum": manifest_checksum,
            "qdrant_checksum": qdrant_checksum,
            "manifest_verified": manifest_verified,
            "degraded": sync_degraded,
            "degraded_reason": sync_degraded_reason,
            "collection_name": collection_name,
            "token_usage": _idx_token_usage,
            "error": (
                sanitize_error_message(indexing_error)
                if indexing_error
                else (err_text if not is_success else None)
            ),
        }
        if ing_run is not None and getattr(ing_run, "attempt_count", None) is not None:
            stage_metrics["attempt_count"] = ing_run.attempt_count

        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="INDEX_SEARCH",
            status="SUCCESS" if is_success else "FAILED",
            error_message=err_text,
            duration_ms=duration_ms,
            metrics_json=stage_metrics,
            created_at=stage_created_at,
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
