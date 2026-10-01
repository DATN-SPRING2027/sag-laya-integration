"""Dedup & Temporal Service for SAG Routing RAG (Phase 2B).

Follows Ponytail minimalism:
- 4-tier deduplication (File exact hash, block exact hash, MinHash/LSH near-dedup, semantic candidate guard).
- Block-type specific similarity thresholds: 0.85 for narrative text, 0.95 for code and tables.
- 5 semantic relations: EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED.
- Strict non-auto-merge for CONTRADICTS / SUPERSEDES (candidates only with evidence mapping).
- Non-overlapping bi-temporal validity tracking: selects active predecessor by temporal validity interval
  (valid_from <= cur_start < valid_to) rather than naive version_no, preventing validity overlap on out-of-order ingest.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import re
from typing import Any, Sequence
import uuid

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    KnowledgeGraphEdge,
    SearchUnit,
    StageRun,
)

# Standard edge relation taxonomy for Phase 2B
RELATION_EQUIVALENT = "EQUIVALENT"
RELATION_SUPPORTS = "SUPPORTS"
RELATION_CONTRADICTS = "CONTRADICTS"
RELATION_SUPERSEDES = "SUPERSEDES"
RELATION_RELATED = "RELATED"

VALID_RELATIONS = {
    RELATION_EQUIVALENT,
    RELATION_SUPPORTS,
    RELATION_CONTRADICTS,
    RELATION_SUPERSEDES,
    RELATION_RELATED,
}


def compute_minhash_signature(tokens: Sequence[str], num_perm: int = 128) -> list[int]:
    """Compute 128-permutation MinHash signature for token k-shingles."""
    if not tokens:
        return [0] * num_perm
    k = min(3, len(tokens))
    shingles = set()
    for i in range(len(tokens) - k + 1):
        shingles.add(" ".join(tokens[i:i+k]))
    if not shingles:
        shingles = set(tokens)

    signature = []
    for i in range(num_perm):
        min_val = 0x7FFFFFFF
        for s in shingles:
            h = int(hashlib.md5(f"{i}:{s}".encode("utf-8")).hexdigest()[:8], 16)
            if h < min_val:
                min_val = h
        signature.append(min_val)
    return signature


def estimate_minhash_similarity(sig1: Sequence[int], sig2: Sequence[int]) -> float:
    """Estimate Jaccard similarity between two MinHash signatures."""
    if not sig1 or not sig2 or len(sig1) != len(sig2):
        return 0.0
    matches = sum(1 for a, b in zip(sig1, sig2) if a == b)
    return matches / len(sig1)


def compute_text_similarity(text1: str, text2: str) -> float:
    """Compute lexical similarity using tokenization and MinHash signature estimator."""
    t1 = re.findall(r"\w+", text1.lower())
    t2 = re.findall(r"\w+", text2.lower())
    if not t1 or not t2:
        return 0.0
    sig1 = compute_minhash_signature(t1, num_perm=128)
    sig2 = compute_minhash_signature(t2, num_perm=128)
    return estimate_minhash_similarity(sig1, sig2)


NEGATION_WORDS = {
    "not", "no", "never", "none", "neither", "nor", "cannot", "can't", "without",
    "không", "chẳng", "chưa", "không phải", "đối lập", "ngược lại", "phủ nhận",
    "contrary", "contradicts", "false", "deprecated", "prohibited", "bị cấm",
}

SUPPORT_MARKERS = {
    "đồng thuận", "chứng minh", "xác nhận", "cụ thể", "minh chứng", "phù hợp",
    "supports", "confirms", "verifies", "proves", "endorses", "consistent with",
    "furthermore", "moreover", "in addition", "đồng thời",
}

SUPERSEDE_MARKERS = {
    "thay thế", "thay bằng", "bãi bỏ", "supersedes", "replaces", "deprecated by",
}


def detect_semantic_signals(text1: str, text2: str) -> tuple[bool, bool, bool]:
    """Detect contradiction, support, or supersede signals between two texts.

    Returns:
        (is_contradiction, is_support, is_supersede)
    """
    t1_lower = text1.lower()
    t2_lower = text2.lower()
    words1 = set(re.findall(r"\w+", t1_lower))
    words2 = set(re.findall(r"\w+", t2_lower))

    # Detect polarity / negation flip: one text negates a claim present in the other
    neg1 = words1 & NEGATION_WORDS
    neg2 = words2 & NEGATION_WORDS

    is_contradiction = False
    if (neg1 and not neg2) or (neg2 and not neg1):
        is_contradiction = True
    elif any(kw in t1_lower or kw in t2_lower for kw in ["contradicts", "trái ngược", "đối lập"]):
        is_contradiction = True

    is_support = False
    if any(m in t1_lower or m in t2_lower for m in SUPPORT_MARKERS):
        is_support = True

    is_supersede = False
    if any(kw in t1_lower or kw in t2_lower for kw in SUPERSEDE_MARKERS):
        is_supersede = True

    return is_contradiction, is_support, is_supersede


def build_lsh_band_index(
    signatures_by_id: dict[str, list[int]],
    *,
    num_bands: int = 16,
    rows_per_band: int = 8,
) -> dict[tuple[int, tuple], list[str]]:
    """Index MinHash signatures into LSH band hash buckets for sub-quadratic candidate search."""
    buckets: dict[tuple[int, tuple], list[str]] = {}
    for item_id, sig in signatures_by_id.items():
        if len(sig) < num_bands * rows_per_band:
            continue
        for b in range(num_bands):
            band_key = (b, tuple(sig[b * rows_per_band : (b + 1) * rows_per_band]))
            if band_key not in buckets:
                buckets[band_key] = []
            buckets[band_key].append(item_id)
    return buckets


def query_lsh_candidates(
    query_sig: list[int],
    lsh_buckets: dict[tuple[int, tuple], list[str]],
    *,
    num_bands: int = 16,
    rows_per_band: int = 8,
) -> set[str]:
    """Retrieve candidate IDs sharing at least one LSH band collision with query signature."""
    candidates = set()
    if len(query_sig) < num_bands * rows_per_band:
        return candidates
    for b in range(num_bands):
        band_key = (b, tuple(query_sig[b * rows_per_band : (b + 1) * rows_per_band]))
        if band_key in lsh_buckets:
            candidates.update(lsh_buckets[band_key])
    return candidates


def get_block_type_threshold(block_type: str) -> float:
    """Return near-dedup similarity threshold by data type according to Phase 2B plan.

    Narrative text (heading, paragraph, list, caption): 0.85
    Structured data (code, table): 0.95 to avoid false merges on common boilerplates.
    """
    if block_type in {"code", "table"}:
        return 0.95
    return 0.85


def classify_relation_candidate(
    sim_score: float,
    is_contradiction: bool = False,
    is_supersede: bool = False,
    is_support: bool = False,
) -> str:
    """Map similarity and semantic signals to one of the 5 canonical relation types.

    Never auto-merge CONTRADICTS or SUPERSEDES.
    """
    if is_supersede:
        return RELATION_SUPERSEDES
    if is_contradiction:
        return RELATION_CONTRADICTS
    if is_support:
        return RELATION_SUPPORTS
    if sim_score >= 0.85:
        return RELATION_EQUIVALENT
    if sim_score >= 0.50:
        return RELATION_RELATED
    return RELATION_RELATED


async def register_relation_candidate(
    session: AsyncSession,
    *,
    project_id: str,
    source_unit_id: str,
    target_unit_id: str,
    edge_type: str,
    weight: float,
    evidence_mapping: dict[str, Any] | None = None,
) -> KnowledgeGraphEdge:
    """Record a semantic edge candidate.

    Enforces that CONTRADICTS and SUPERSEDES relations remain isolated candidates
    with provenance evidence, without destructive auto-merge.
    """
    if edge_type not in VALID_RELATIONS:
        raise ValueError(f"Invalid relation type: {edge_type}. Must be one of {VALID_RELATIONS}")

    metadata = evidence_mapping or {}
    metadata.setdefault("is_candidate", True)
    metadata.setdefault("auto_merged", False)

    # In case of CONTRADICTS / SUPERSEDES, record strict audit trail
    if edge_type in {RELATION_CONTRADICTS, RELATION_SUPERSEDES}:
        metadata["requires_human_or_eval_resolution"] = True

    edge = KnowledgeGraphEdge(
        id=str(uuid.uuid4()),
        project_id=project_id,
        source_unit_id=source_unit_id,
        target_unit_id=target_unit_id,
        edge_type=edge_type,
        weight=weight,
        calibrated_weight=weight,
        metadata_json=metadata,
    )
    session.add(edge)
    return edge


async def resolve_temporal_supersedes(
    session: AsyncSession,
    *,
    current_version: DocumentVersion,
    document_id: str,
) -> dict[str, Any]:
    """Link versions along temporal axes without overlapping validity intervals.

    Selects active predecessor according to time validity (valid_from <= cur_start < valid_to),
    rather than naive version_no sorting, preventing validity overlap on out-of-order ingest.
    """
    stmt = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.id != current_version.id,
        )
        .order_by(DocumentVersion.valid_from.asc())
    )
    res = await session.execute(stmt)
    all_prev = list(res.scalars().all())

    now_utc = datetime.now(UTC)
    cur_start = current_version.source_published_at or current_version.observed_at or now_utc
    if cur_start.tzinfo is None:
        cur_start = cur_start.replace(tzinfo=UTC)

    max_valid_to = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    if not all_prev:
        current_version.valid_from = cur_start
        current_version.valid_to = max_valid_to
        current_version.supersedes_id = None
        return {"action": "INITIAL", "active_version_id": current_version.id}

    # Find the version active at cur_start: valid_from <= cur_start < valid_to
    active_at_start = None
    for v in all_prev:
        raw_from = getattr(v, "source_published_at", None) or getattr(v, "observed_at", None) or getattr(v, "valid_from", None) or getattr(v, "created_at", None)
        v_from = raw_from.replace(tzinfo=UTC) if raw_from and raw_from.tzinfo is None else raw_from
        raw_to = v.valid_to or max_valid_to
        v_to = raw_to.replace(tzinfo=UTC) if raw_to and raw_to.tzinfo is None else raw_to
        if v_from and v_to and v_from <= cur_start < v_to:
            active_at_start = v
            break

    if active_at_start is not None:
        # Normal newer or mid-timeline version: current supersedes active_at_start
        orig_active_valid_to = active_at_start.valid_to or max_valid_to
        current_version.supersedes_id = active_at_start.id
        current_version.valid_from = cur_start
        current_version.valid_to = orig_active_valid_to
        if v_from:
            active_at_start.valid_from = v_from
        active_at_start.valid_to = cur_start
        session.add(active_at_start)

        # Rewire successor versions that were pointing to active_at_start
        # (e.g. v1 -> v2, inserting v_mid between their dates leaves v2 pointing to v_mid, maintaining continuous chain)
        for v in all_prev:
            if v.id != current_version.id and v.supersedes_id == active_at_start.id:
                raw_v = (
                    getattr(v, "source_published_at", None)
                    or getattr(v, "observed_at", None)
                    or getattr(v, "valid_from", None)
                )
                v_start = raw_v.replace(tzinfo=UTC) if raw_v and raw_v.tzinfo is None else raw_v
                if v_start and v_start >= cur_start:
                    v.supersedes_id = current_version.id
                    session.add(v)

        action = "SUPERSEDED"
        active_v = next(
            (
                v for v in all_prev
                if v.valid_to and (v.valid_to.replace(tzinfo=UTC) if v.valid_to.tzinfo is None else v.valid_to) >= max_valid_to
            ),
            current_version,
        )
        active_id = active_v.id
    else:
        # Out-of-order older version arriving late, or gap in timeline
        future_vers = []
        past_vers = []
        for v in all_prev:
            raw_from = getattr(v, "source_published_at", None) or getattr(v, "observed_at", None) or getattr(v, "valid_from", None) or getattr(v, "created_at", None)
            vf = raw_from.replace(tzinfo=UTC) if raw_from and raw_from.tzinfo is None else raw_from
            raw_to = v.valid_to or max_valid_to
            vt = raw_to.replace(tzinfo=UTC) if raw_to and raw_to.tzinfo is None else raw_to
            if vf and vf > cur_start:
                future_vers.append((v, vf))
            elif vt and vt <= cur_start:
                past_vers.append((v, vt))

        current_version.valid_from = cur_start
        if future_vers:
            earliest_future_v, earliest_future_from = min(future_vers, key=lambda x: x[1])
            current_version.valid_to = earliest_future_from
            if not earliest_future_v.supersedes_id:
                earliest_future_v.supersedes_id = current_version.id
                session.add(earliest_future_v)
        else:
            current_version.valid_to = max_valid_to

        if past_vers:
            latest_past_v, _ = max(past_vers, key=lambda x: x[1])
            current_version.supersedes_id = latest_past_v.id
        else:
            current_version.supersedes_id = None

        action = "OUT_OF_ORDER_ARCHIVED"
        active_v = next((v for v in all_prev if v.valid_to and v.valid_to >= max_valid_to), current_version)
        active_id = active_v.id

    # Strictly guarantee non-overlapping intervals across all versions
    for v in all_prev:
        if v.id == current_version.id:
            continue
        v_from = v.valid_from.replace(tzinfo=UTC) if v.valid_from and v.valid_from.tzinfo is None else v.valid_from
        v_to = v.valid_to.replace(tzinfo=UTC) if v.valid_to and v.valid_to.tzinfo is None else v.valid_to
        c_from = current_version.valid_from
        c_to = current_version.valid_to

        if v_from < c_from and v_to > c_from:
            v.valid_to = c_from
            session.add(v)
        elif c_from <= v_from < c_to:
            current_version.valid_to = v_from

    return {
        "action": action,
        "active_version_id": active_id,
        "current_version_id": current_version.id,
        "supersedes_id": current_version.supersedes_id,
    }


async def run_dedup_and_temporal_stage(
    session: AsyncSession,
    *,
    document_version: DocumentVersion,
    document_id: str,
    project_id: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute Phase 2B stage: Exact Dedup, Near Dedup, and Temporal Supersedes with audit."""
    start_time = datetime.now(UTC)

    # 1. Exact Version Dedup Check (by file_hash)
    exact_dup_stmt = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.id != document_version.id,
            DocumentVersion.file_hash == document_version.file_hash,
        )
        .order_by(desc(DocumentVersion.created_at))
    )
    exact_dup_version = (await session.execute(exact_dup_stmt)).scalars().first()
    is_exact_dup_version = exact_dup_version is not None

    # 2. Block-Level Exact & Near Dedup Check with Block-Type Specific Thresholds
    curr_blocks = (
        await session.execute(
            select(CanonicalBlock)
            .where(CanonicalBlock.document_version_id == document_version.id)
            .order_by(CanonicalBlock.ordinal)
        )
    ).scalars().all()

    prior_blocks = (
        await session.execute(
            select(CanonicalBlock)
            .join(DocumentVersion, CanonicalBlock.document_version_id == DocumentVersion.id)
            .where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.id != document_version.id,
            )
        )
    ).scalars().all()

    exact_block_matches = 0
    near_dup_candidates: list[dict[str, Any]] = []
    prior_hashes = {b.content_hash for b in prior_blocks if b.content_hash}

    # Precompute MinHash signatures once for all prior blocks and build LSH index
    prior_sig_by_id: dict[str, list[int]] = {}
    prior_by_id: dict[str, CanonicalBlock] = {}
    for pb in prior_blocks:
        prior_by_id[pb.id] = pb
        t = re.findall(r"\w+", pb.normalized_text.lower())
        prior_sig_by_id[pb.id] = compute_minhash_signature(t, num_perm=128)

    lsh_index = build_lsh_band_index(prior_sig_by_id, num_bands=16, rows_per_band=8)

    for b in curr_blocks:
        if b.content_hash and b.content_hash in prior_hashes:
            exact_block_matches += 1
        else:
            threshold = get_block_type_threshold(b.block_type)
            b_tokens = re.findall(r"\w+", b.normalized_text.lower())
            b_sig = compute_minhash_signature(b_tokens, num_perm=128)

            # Query bounded candidates using LSH bands
            candidate_ids = query_lsh_candidates(b_sig, lsh_index, num_bands=16, rows_per_band=8)
            cand_blocks = [prior_by_id[cid] for cid in candidate_ids if cid in prior_by_id]
            eval_blocks = cand_blocks if (cand_blocks or len(prior_blocks) > 30) else prior_blocks

            for pb in eval_blocks:
                pb_sig = prior_sig_by_id.get(pb.id)
                sim = estimate_minhash_similarity(b_sig, pb_sig) if pb_sig else compute_text_similarity(b.normalized_text, pb.normalized_text)
                if sim >= threshold:
                    is_contra, is_supp, is_super = detect_semantic_signals(b.normalized_text, pb.normalized_text)
                    rel = classify_relation_candidate(
                        sim,
                        is_contradiction=is_contra,
                        is_supersede=is_super,
                        is_support=is_supp,
                    )
                    near_dup_candidates.append({
                        "source_block_id": b.id,
                        "target_block_id": pb.id,
                        "source_version_id": document_version.id,
                        "target_version_id": pb.document_version_id,
                        "block_type": b.block_type,
                        "threshold": threshold,
                        "similarity_score": round(sim, 3),
                        "similarity": round(sim, 3),
                        "relation": rel,
                        "relation_type": rel,
                        "is_contradiction": is_contra,
                        "is_support": is_supp,
                        "is_supersede": is_super,
                        "auto_merged": False,
                        "evidence": f"MinHash similarity {sim:.3f} >= {threshold} on {b.block_type} [relation={rel}]",
                    })
                    break  # Found best candidate match for this block

    # 3. Temporal Resolution (Non-overlapping bi-temporal validity)
    temporal_result = await resolve_temporal_supersedes(
        session,
        current_version=document_version,
        document_id=document_id,
    )

    # 4. Update DocumentVersion metadata with Evidence Mapping & Dedup audit
    meta = dict(document_version.metadata_json or {})
    meta.update({
        "is_exact_duplicate": is_exact_dup_version,
        "exact_duplicate_of": exact_dup_version.id if exact_dup_version else None,
        "exact_block_matches": exact_block_matches,
        "near_duplicate_candidates_count": len(near_dup_candidates),
        "dedup_candidates": near_dup_candidates,
        "dedup_relations": list({c["relation_type"] for c in near_dup_candidates}),
    })
    document_version.metadata_json = meta
    session.add(document_version)

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    # 5. Record StageRun
    if run_id:
        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="DEDUP_TEMPORAL",
            status="SUCCESS",
            duration_ms=duration_ms,
            metrics_json={
                "exact_duplicate_found": is_exact_dup_version,
                "exact_duplicate_of": exact_dup_version.id if exact_dup_version else None,
                "exact_block_matches": exact_block_matches,
                "near_duplicate_candidates_count": len(near_dup_candidates),
                "near_duplicate_candidates": near_dup_candidates,
                "temporal_action": temporal_result["action"],
                "active_version_id": temporal_result["active_version_id"],
            },
        )
        session.add(stage_run)

    return {
        "exact_duplicate_found": is_exact_dup_version,
        "exact_duplicate_of": exact_dup_version.id if exact_dup_version else None,
        "exact_block_matches": exact_block_matches,
        "near_duplicate_candidates": near_dup_candidates,
        "temporal_action": temporal_result["action"],
        **temporal_result,
    }
