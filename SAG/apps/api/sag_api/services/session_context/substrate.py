"""Incremental Multi-View Token-Free Memory Substrate for SAG-ECCE.

Reuses `SAG main` infrastructure:
- `search_index_service`: `COMMON_STOPWORDS`, `compute_sparse_bm25_vector`, `compute_unit_embeddings`,
  `get_cached_text_embedding`, `put_cached_text_embedding`
- `generation.prompt`: `estimate_tokens`
- `igraph`: C-accelerated `personalized_pagerank(..., damping=0.6, implementation="prpack")`
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import igraph as ig

from sag_api.core.bm25_tokenizer import tokenize_bm25_tokens
from sag_api.core.logging import get_logger
from sag_api.core.text_utils import normalize_text
from sag_api.generation.prompt import estimate_tokens
from sag_api.services.search_index_service import (
    COMMON_STOPWORDS,
    compute_sparse_bm25_vector,
    compute_unit_embeddings,
    get_cached_text_embedding,
    put_cached_text_embedding,
)
from sag_api.services.session_context.extractor import (
    ExtractedTurnAnalysis,
    extract_content_terms,
    extract_structural_entities_and_sentences,
    extract_turns_structured_batch,
    normalize_session_text,
)
from sag_api.services.session_context.models import (
    DecisionLedgerEntry,
    EntityMention,
    EpisodeSegment,
    IndexedSentence,
    IndexedTurn,
    SlidingWindowNode,
)

FALLBACK_VECTOR_DIM = 256
log = get_logger("sag.session_context.substrate")
EPISODE_TIME_GAP_SECONDS = 1800.0  # 30 minutes
EPISODE_CENTROID_COS_THRESHOLD = 0.72
EPISODE_LEXICAL_JACCARD_THRESHOLD = 0.18
EPISODE_MAX_TURNS = 12
SOFT_ALIAS_WEIGHT = 0.65
SENTENCE_GATE_TOP_K = 3
SENTENCE_GATE_MIN_COS = 0.12
PPR_DAMPING = 0.60
PPR_MAX_SUBGRAPH_TURNS = 400
PPR_TOP_HYBRID_SEEDS = 220
BM25_K1 = 1.5
BM25_B = 0.75


def l2_normalize(vec: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 1e-12:
        return tuple(0.0 for _ in vec)
    return tuple(float(x) / norm for x in vec)


def cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


def deterministic_ngram_vector(text: str, dim: int = FALLBACK_VECTOR_DIM) -> tuple[float, ...]:
    """Deterministic Unicode word + char-3/4gram signed feature hashing fallback (<1ms, 0 API cost)."""
    cleaned = normalize_session_text(text).casefold()
    if not cleaned:
        return tuple(0.0 for _ in range(dim))
    tokens = re.findall(r"\w+", cleaned)
    acc = [0.0] * dim
    features: list[tuple[str, float]] = []
    for tok in tokens:
        features.append((f"w:{tok}", 1.6))
        if len(tok) >= 3:
            for i in range(len(tok) - 2):
                features.append((f"c3:{tok[i : i + 3]}", 0.55))
    for i in range(len(tokens) - 1):
        features.append((f"bg:{tokens[i]}_{tokens[i + 1]}", 1.15))

    for feat, weight in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        val = int.from_bytes(digest, "little")
        idx = val % dim
        sign = 1.0 if ((val >> 32) & 1) == 0 else -1.0
        acc[idx] += sign * weight
    return l2_normalize(acc)


def tokenize_bm25_terms(text: str) -> tuple[dict[str, int], int]:
    """Tokenize text identically to search_index_service.compute_sparse_bm25_vector for dynamic corpus IDF."""
    tokenized = tokenize_bm25_tokens(text, filter_stopwords=True)
    if not tokenized.tokens:
        return {}, 1
    return dict(Counter(tokenized.expanded_tokens)), max(1, len(tokenized.tokens))


def declared_embedder_dim(embedder: Any | None) -> int | None:
    """Return explicit positive integer vector dimension declared on `embedder`, if any."""
    if embedder is None:
        return FALLBACK_VECTOR_DIM
    for attr in ("dim", "dimensions", "_dimensions"):
        raw = getattr(embedder, attr, None)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
    cfg = getattr(embedder, "config", None)
    if cfg is not None:
        for attr in ("dim", "dimensions"):
            raw = getattr(cfg, attr, None)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
    return None


def compute_embedding_signature(embedder: Any | None) -> str:
    """Compute non-empty deterministic signature identifying the active embedding space."""
    if embedder is None:
        return "deterministic-ngram-v1"
    model_name = str(
        getattr(embedder, "_model", "")
        or getattr(embedder, "model", "")
        or getattr(embedder, "model_name", "")
        or getattr(getattr(embedder, "config", None), "model", "")
        or ""
    ).strip()
    if model_name:
        return f"http:{model_name}"[:128]
    dim_val = declared_embedder_dim(embedder)
    cls_name = f"{type(embedder).__module__}.{type(embedder).__qualname__}"
    if dim_val is not None:
        return f"embedder:{cls_name}:d{dim_val}"[:128]
    return f"embedder:{cls_name}"[:128]


async def embed_texts_with_provenance(
    texts: Sequence[str],
    *,
    embedder: Any | None = None,
) -> tuple[list[tuple[float, ...]], str]:
    """Batch-embed texts with model-scoped cache keys and return `(vectors, actual_source_signature)`."""
    if not texts:
        return [], compute_embedding_signature(embedder)
    if embedder is None:
        return [deterministic_ngram_vector(t) for t in texts], "deterministic-ngram-v1"

    emb_sig = compute_embedding_signature(embedder)
    from sag_api.services.search_index_service import build_embedding_model_signature

    cache_sig = build_embedding_model_signature(embedder)
    expected_dim = declared_embedder_dim(embedder)
    results: list[tuple[float, ...] | None] = [None] * len(texts)
    missing_indices: list[int] = []
    missing_texts: list[str] = []
    for idx, txt in enumerate(texts):
        cached = get_cached_text_embedding(txt, model_signature=cache_sig)
        if (
            cached is not None
            and len(cached) > 0
            and (expected_dim is None or len(cached) == expected_dim)
        ):
            results[idx] = l2_normalize(cached)
        else:
            missing_indices.append(idx)
            missing_texts.append(txt)

    if missing_texts:
        try:
            computed = await compute_unit_embeddings(embedder, missing_texts)
            if len(computed) != len(missing_texts):
                raise ValueError("Embedder returned mismatched vector count")
            for idx, txt, raw_vec in zip(missing_indices, missing_texts, computed, strict=True):
                if not raw_vec:
                    raise ValueError("Embedder returned empty vector")
                if expected_dim is not None and len(raw_vec) != expected_dim:
                    raise ValueError(
                        f"Embedder {emb_sig} returned dim={len(raw_vec)}, expected {expected_dim}"
                    )
                put_cached_text_embedding(txt, raw_vec, model_signature=cache_sig)
                results[idx] = l2_normalize(raw_vec)
        except Exception as exc:  # noqa: BLE001 - fail-open to deterministic local projection if HTTP embedder errors
            log.warning(
                "Session embedder failed during batch embed (sig=%s); falling back to deterministic-ngram-v1: %s",
                emb_sig,
                type(exc).__name__,
            )
            return [deterministic_ngram_vector(t) for t in texts], "deterministic-ngram-v1"

    finalized = [
        vec if vec is not None else deterministic_ngram_vector(texts[i])
        for i, vec in enumerate(results)
    ]
    if finalized:
        first_dim = len(finalized[0])
        if any(len(v) != first_dim for v in finalized):
            # Re-embed cleanly if a stale cache entry had a drifted dimension for the same model label
            try:
                recomputed = await compute_unit_embeddings(embedder, list(texts))
                if len(recomputed) != len(texts) or any(not rv for rv in recomputed):
                    raise ValueError("Invalid recomputed vector batch")
                ref_dim = len(recomputed[0])
                if any(len(rv) != ref_dim for rv in recomputed):
                    raise ValueError("Inconsistent vector dimensions within single embedder batch")
                rebuilt: list[tuple[float, ...]] = []
                for txt, raw_vec in zip(texts, recomputed, strict=True):
                    put_cached_text_embedding(txt, raw_vec, model_signature=cache_sig)
                    rebuilt.append(l2_normalize(raw_vec))
                return rebuilt, emb_sig
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Session embedder failed during dimension-drift recompute "
                    "(sig=%s); falling back to deterministic-ngram-v1: %s",
                    emb_sig,
                    type(exc).__name__,
                )
                return [deterministic_ngram_vector(t) for t in texts], "deterministic-ngram-v1"

    return finalized, emb_sig


async def embed_texts_with_main_cache(
    texts: Sequence[str],
    *,
    embedder: Any | None = None,
) -> list[tuple[float, ...]]:
    """Batch-embed texts via SAG main's HTTP/SiliconFlow embedder + model-scoped LRU cache."""
    vectors, _ = await embed_texts_with_provenance(texts, embedder=embedder)
    return vectors


def sparse_dict_from_bm25(
    text: str,
    *,
    avg_doc_len: float = 64.0,
    corpus_doc_freqs: dict[str, int] | None = None,
    total_corpus_docs: int = 200,
) -> dict[int, float]:
    raw = compute_sparse_bm25_vector(
        text,
        avg_doc_len=max(8.0, avg_doc_len),
        corpus_doc_freqs=corpus_doc_freqs,
        total_corpus_docs=max(10, total_corpus_docs),
    )
    return dict(zip(raw["indices"], raw["values"], strict=False))


def sparse_dot(q_sparse: dict[int, float], d_sparse: dict[int, float]) -> float:
    if not q_sparse or not d_sparse:
        return 0.0
    if len(q_sparse) > len(d_sparse):
        q_sparse, d_sparse = d_sparse, q_sparse
    return sum(weight * d_sparse.get(idx, 0.0) for idx, weight in q_sparse.items())


def min_max_normalize(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi - lo <= 1e-9:
        return {k: (1.0 if v > 0.0 else 0.0) for k, v in scores.items()}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


class IncrementalSessionIndex:
    """Append-only multi-view session memory substrate (O(Delta) per turn, 0 full-cache resets)."""

    def __init__(self, thread_id: str, *, scope_signature: str = "", tenant_id: str = "") -> None:
        self.thread_id = thread_id
        self.scope_signature = scope_signature
        self.tenant_id = tenant_id
        self.embedding_mode: str = "unset"
        self.turns: list[IndexedTurn] = []
        self.turns_by_id: dict[str, IndexedTurn] = {}
        self.windows: list[SlidingWindowNode] = []
        self.windows_by_id: dict[str, SlidingWindowNode] = {}
        self.episodes: list[EpisodeSegment] = []
        self.episodes_by_id: dict[str, EpisodeSegment] = {}
        self.decision_ledger: list[DecisionLedgerEntry] = []
        self.entity_to_turn_counts: dict[str, dict[str, int]] = {}
        self.entity_to_sentences: dict[str, list[IndexedSentence]] = {}
        self.entity_surfaces: dict[str, str] = {}
        self.soft_alias_neighbors: dict[str, set[str]] = {}
        self.doc_freqs: dict[str, int] = {}
        self.term_postings: dict[str, list[tuple[str, int]]] = {}
        self.total_token_words: int = 0
        # Incremental PostgreSQL cursor & Continuum ACL cache state (O(Delta) DB read)
        self.last_db_created_at: Any = None
        self.last_db_id: str | None = None
        self.seen_db_count: int = 0
        self.cached_cited_source_ids: set[str] = set()
        self.had_hidden_acl_turns: bool = False
        self.persisted_embedding_signature: str = ""
        self.persistence_active: bool = False
        self.qdrant_sync_pending: bool = False
        self.qdrant_rebuild_pending: bool = False
        self.seen_raw_ids: set[str] = set()
        self.boundary_ts_ids: set[str] = set()
        self.inherited_turn_count: int = 0
        self.inherited_window_count: int = 0
        self.inherited_episode_count: int = 0
        self.inherited_decision_count: int = 0
        self.force_new_episode_next: bool = False
        self.inherited_lineage_signature: str = ""
        self.extraction_degraded: bool = False
        self.extraction_degraded_reason: str | None = None
        self.embedding_degraded: bool = False
        self.embedding_degraded_reason: str | None = None

    def seal_inherited_boundary(self) -> None:
        """Freeze inherited ancestor prefix boundaries so own units never reference ancestor Message IDs."""
        self.inherited_turn_count = len(self.turns)
        self.inherited_window_count = len(self.windows)
        self.inherited_episode_count = len(self.episodes)
        self.inherited_decision_count = len(self.decision_ledger)
        if self.episodes:
            self.force_new_episode_next = True

    @property
    def active_decisions(self) -> list[DecisionLedgerEntry]:
        return [
            d
            for d in self.decision_ledger
            if d.valid_to_ts is None and d.epistemic_status in {"CONFIRMED", "OPEN_ISSUE", "PROPOSED", "CONSIDERING"}
        ]

    async def append_messages(
        self,
        raw_messages: Sequence[dict[str, Any]],
        *,
        embedder: Any | None = None,
        llm: Any | None = None,
        project_aliases: Sequence[tuple[str, str]] = (),
        tenant_id: str | None = None,
    ) -> int:
        """Incrementally index only unseen messages in chronological order."""
        target_mode = compute_embedding_signature(embedder)
        if self.embedding_mode not in {"unset", target_mode}:
            # Vector space changed -> rebuild cleanly in the new vector space
            self.__init__(self.thread_id, scope_signature=self.scope_signature, tenant_id=self.tenant_id)

        pending: list[dict[str, Any]] = []
        for msg in raw_messages:
            turn_id = str(msg.get("id") or "")
            if not turn_id or turn_id in self.turns_by_id:
                continue
            if msg.get("acl_hidden"):
                continue
            content = normalize_session_text(str(msg.get("content") or ""))
            if not content:
                continue
            pending.append(msg)

        if not pending:
            if self.embedding_mode == "unset":
                self.embedding_mode = target_mode
            return 0

        # Structured LLM turn analysis (batched & tenant/thread-scoped cached) + structural E0/E1 spans
        base_ordinal = len(self.turns)
        eff_tenant = (
            str(tenant_id or self.tenant_id or (f"scope:{self.scope_signature}" if self.scope_signature else ""))
            .strip()
            or None
        )
        analyses, batch_degraded, batch_degraded_reason = await extract_turns_structured_batch(
            pending,
            llm=llm,
            project_aliases=project_aliases,
            base_ordinal=base_ordinal,
            tenant_id=eff_tenant,
            thread_id=self.thread_id,
        )
        if batch_degraded:
            self.extraction_degraded = True
            self.extraction_degraded_reason = self.extraction_degraded_reason or batch_degraded_reason

        extracted_per_msg: list[ExtractedTurnAnalysis] = analyses
        texts_to_embed: list[str] = []
        turn_vec_offsets: list[int] = []
        sent_vec_ranges: list[tuple[int, int]] = []

        for offset, msg in enumerate(pending):
            content = normalize_session_text(str(msg["content"]))
            sent_items = extracted_per_msg[offset].sentences
            turn_vec_offsets.append(len(texts_to_embed))
            texts_to_embed.append(content)
            s_start_idx = len(texts_to_embed)
            for s_text, _ in sent_items:
                texts_to_embed.append(s_text)
            sent_vec_ranges.append((s_start_idx, len(texts_to_embed)))

        # Single batch embedding call across all new turns + sentences (with provenance verification)
        all_vectors, actual_sig = await embed_texts_with_provenance(texts_to_embed, embedder=embedder)
        if embedder is not None and actual_sig == "deterministic-ngram-v1":
            self.embedding_degraded = True
            self.embedding_degraded_reason = (
                self.embedding_degraded_reason or "http_embedder_fallback_deterministic_ngram"
            )
        if self.turns and actual_sig != self.embedding_mode:
            # HTTP embedder degraded mid-thread -> re-project entire thread in uniform fallback space
            prior_records = [
                {
                    "id": t.turn_id,
                    "role": t.role,
                    "content": t.content,
                    "created_at": t.created_at_ts,
                    "cited_source_ids": list(t.cited_source_ids),
                }
                for t in self.turns
            ]
            inh_k = self.inherited_turn_count
            inh_sig = self.inherited_lineage_signature
            saved_tenant = self.tenant_id
            was_emb_degraded = self.embedding_degraded
            emb_degraded_reason = self.embedding_degraded_reason
            self.__init__(
                self.thread_id,
                scope_signature=self.scope_signature,
                tenant_id=saved_tenant,
            )
            if was_emb_degraded or actual_sig == "deterministic-ngram-v1":
                self.embedding_degraded = True
                self.embedding_degraded_reason = (
                    emb_degraded_reason or "http_embedder_degraded_mid_thread"
                )
            if inh_k > 0:
                await self.append_messages(
                    prior_records[:inh_k],
                    embedder=None,
                    llm=llm,
                    project_aliases=project_aliases,
                    tenant_id=eff_tenant,
                )
                self.seal_inherited_boundary()
                self.inherited_lineage_signature = inh_sig
            await self.append_messages(
                [*prior_records[inh_k:], *pending],
                embedder=None,
                llm=llm,
                project_aliases=project_aliases,
                tenant_id=eff_tenant,
            )
            return len(pending)
        self.embedding_mode = actual_sig

        for offset, msg in enumerate(pending):
            t_id = str(msg["id"])
            ordinal = len(self.turns)
            role = str(msg.get("role") or "user")
            content = normalize_session_text(str(msg["content"]))
            created_at = msg.get("created_at")
            if isinstance(created_at, datetime):
                ts = created_at.timestamp()
                iso = created_at.astimezone(UTC).isoformat() if created_at.tzinfo else created_at.isoformat()
            elif isinstance(created_at, (int, float)):
                ts = float(created_at)
                iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
            else:
                ts = float(ordinal * 60)
                iso = ""

            analysis = extracted_per_msg[offset]
            mentions = list(analysis.entities)
            sent_items = list(analysis.sentences)
            t_vec = all_vectors[turn_vec_offsets[offset]]
            s_lo, s_hi = sent_vec_ranges[offset]
            s_vecs = all_vectors[s_lo:s_hi]

            indexed_sentences = tuple(
                IndexedSentence(
                    sentence_id=f"{t_id}:s{s_idx}",
                    turn_id=t_id,
                    ordinal=s_idx,
                    text=s_text,
                    entity_keys=s_keys,
                    dense_vector=s_vec,
                )
                for s_idx, ((s_text, s_keys), s_vec) in enumerate(zip(sent_items, s_vecs, strict=True))
            )

            content_terms = frozenset(extract_content_terms(content))
            term_freqs, word_len = tokenize_bm25_terms(content)
            for word, tf in term_freqs.items():
                self.doc_freqs[word] = self.doc_freqs.get(word, 0) + 1
                self.term_postings.setdefault(word, []).append((t_id, tf))
            self.total_token_words += word_len
            avg_len = self.total_token_words / max(1, ordinal + 1)

            sparse_vec = sparse_dict_from_bm25(
                content,
                avg_doc_len=avg_len,
                corpus_doc_freqs=self.doc_freqs,
                total_corpus_docs=max(50, ordinal + 1),
            )
            epistemic_status = analysis.epistemic_status
            slot_key = analysis.topic_slot_key
            is_anaphoric = analysis.is_anaphoric_or_short
            superseded_ent_keys = analysis.superseded_entity_keys

            episode = self._assign_or_open_episode(
                turn_id=t_id,
                turn_vec=t_vec,
                content_terms=content_terms,
                ts=ts,
            )

            # Anaphoric confirmation resolution
            companion_ids: tuple[str, ...] = ()
            if (
                epistemic_status == "CONFIRMED"
                and is_anaphoric
                and len(episode.turn_ids) >= 2
            ):
                prev_turn = self.turns_by_id[episode.turn_ids[-2]]
                if slot_key is None and prev_turn.topic_slot_key is not None:
                    slot_key = prev_turn.topic_slot_key
                if prev_turn.epistemic_status in {"PROPOSED", "CONSIDERING", "FACT"}:
                    companion_ids = (prev_turn.turn_id,)

            window_node = self._append_sliding_window(episode, t_id)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            cited_sources = tuple(sorted(str(s) for s in (msg.get("cited_source_ids") or ()) if s))
            if cited_sources:
                self.cached_cited_source_ids.update(cited_sources)

            turn_obj = IndexedTurn(
                turn_id=t_id,
                thread_id=self.thread_id,
                ordinal=ordinal,
                role=role,
                content=content,
                created_at_ts=ts,
                created_at_iso=iso,
                token_count=max(1, estimate_tokens(content)),
                content_hash=content_hash,
                episode_id=episode.episode_id,
                window_id=window_node.window_id,
                dense_vector=t_vec,
                sparse_bm25=sparse_vec,
                content_terms=content_terms,
                entities=tuple(mentions),
                sentences=indexed_sentences,
                term_freqs=term_freqs,
                doc_word_len=word_len,
                epistemic_status=epistemic_status,
                topic_slot_key=slot_key,
                superseded_entity_keys=superseded_ent_keys,
                is_anaphoric_or_short=is_anaphoric,
                is_interrogative=analysis.is_interrogative,
                intrinsic_epistemic_status=epistemic_status,
                extraction_degraded=analysis.degraded,
                cited_source_ids=cited_sources,
            )
            self.turns.append(turn_obj)
            self.turns_by_id[t_id] = turn_obj

            # Update window vector/terms now that turn_obj is registered
            self._refresh_window_representation(window_node)
            self._register_turn_entities(turn_obj)
            self._update_decision_ledger(turn_obj, companion_turn_ids=companion_ids)

        return len(pending)

    def _assign_or_open_episode(
        self,
        *,
        turn_id: str,
        turn_vec: tuple[float, ...],
        content_terms: frozenset[str],
        ts: float,
    ) -> EpisodeSegment:
        if not self.episodes:
            ep = EpisodeSegment(
                episode_id=f"{self.thread_id}:ep0",
                thread_id=self.thread_id,
                ordinal=0,
                turn_ids=[turn_id],
                centroid=list(turn_vec),
                content_terms=set(content_terms),
                start_ts=ts,
                end_ts=ts,
            )
            self.episodes.append(ep)
            self.episodes_by_id[ep.episode_id] = ep
            return ep

        cur = self.episodes[-1]
        time_gap = max(0.0, ts - cur.end_ts) if ts > 0 and cur.end_ts > 0 else 0.0
        sem_sim = cosine_sim(cur.centroid, turn_vec)
        union_terms = cur.content_terms | content_terms
        lex_jaccard = (len(cur.content_terms & content_terms) / len(union_terms)) if union_terms else 0.0

        drift_detected = (
            len(cur.turn_ids) >= 3
            and sem_sim < EPISODE_CENTROID_COS_THRESHOLD
            and lex_jaccard < EPISODE_LEXICAL_JACCARD_THRESHOLD
        )
        should_split = (
            self.force_new_episode_next
            or time_gap > EPISODE_TIME_GAP_SECONDS
            or len(cur.turn_ids) >= EPISODE_MAX_TURNS
            or drift_detected
        )

        if should_split:
            self.force_new_episode_next = False
            ep_ord = len(self.episodes)
            ep = EpisodeSegment(
                episode_id=f"{self.thread_id}:ep{ep_ord}",
                thread_id=self.thread_id,
                ordinal=ep_ord,
                turn_ids=[turn_id],
                centroid=list(turn_vec),
                content_terms=set(content_terms),
                start_ts=ts,
                end_ts=ts,
            )
            self.episodes.append(ep)
            self.episodes_by_id[ep.episode_id] = ep
            return ep

        n_prev = len(cur.turn_ids)
        cur.turn_ids.append(turn_id)
        if len(cur.centroid) == len(turn_vec):
            blended = [(cur.centroid[i] * n_prev + turn_vec[i]) / (n_prev + 1) for i in range(len(turn_vec))]
            cur.centroid = list(l2_normalize(blended))
        cur.content_terms.update(content_terms)
        cur.end_ts = max(cur.end_ts, ts)
        return cur

    def _append_sliding_window(self, episode: EpisodeSegment, new_turn_id: str) -> SlidingWindowNode:
        window_turn_ids = tuple(episode.turn_ids[-3:])
        win_ord = len(self.windows)
        win_id = f"{episode.episode_id}:w{win_ord}"
        node = SlidingWindowNode(
            window_id=win_id,
            episode_id=episode.episode_id,
            ordinal=win_ord,
            turn_ids=window_turn_ids,
            dense_vector=(),
            content_terms=frozenset(),
        )
        self.windows.append(node)
        self.windows_by_id[win_id] = node
        episode.window_ids.append(win_id)
        if not new_turn_id:
            return node
        return node

    def _refresh_window_representation(self, window: SlidingWindowNode) -> None:
        turns = [self.turns_by_id[tid] for tid in window.turn_ids if tid in self.turns_by_id]
        if not turns:
            return
        dim = len(turns[0].dense_vector)
        avg = [sum(t.dense_vector[i] for t in turns) / len(turns) for i in range(dim)]
        merged_terms: set[str] = set()
        for t in turns:
            merged_terms.update(t.content_terms)
        window.dense_vector = l2_normalize(avg)
        window.content_terms = frozenset(merged_terms)

    def _register_turn_entities(self, turn: IndexedTurn) -> None:
        norm_content_cf = normalize_text(turn.content, preserve_code=True).casefold()
        for mention in turn.entities:
            self.entity_surfaces.setdefault(mention.key, mention.surface)
            turn_counts = self.entity_to_turn_counts.setdefault(mention.key, {})
            norm_surface_cf = normalize_text(mention.surface, preserve_code=True).casefold()
            occ = max(1, norm_content_cf.count(norm_surface_cf)) if norm_surface_cf else 1
            turn_counts[turn.turn_id] = turn_counts.get(turn.turn_id, 0) + occ
            for alias_key in mention.soft_aliases:
                self.soft_alias_neighbors.setdefault(mention.key, set()).add(alias_key)
                self.soft_alias_neighbors.setdefault(alias_key, set()).add(mention.key)

        for sent in turn.sentences:
            for e_key in sent.entity_keys:
                self.entity_to_sentences.setdefault(e_key, []).append(sent)

    def _update_decision_ledger(
        self,
        turn: IndexedTurn,
        *,
        companion_turn_ids: tuple[str, ...] = (),
    ) -> None:
        if turn.epistemic_status == "FACT" and turn.topic_slot_key is None:
            return

        superseded_set = set(turn.superseded_entity_keys)
        non_replaced_entities = [e.key for e in turn.entities if e.key not in superseded_set]
        fallback_ent = non_replaced_entities[0] if non_replaced_entities else (
            turn.entities[0].key if turn.entities else None
        )
        slot = turn.topic_slot_key or fallback_ent
        if slot is None and turn.epistemic_status not in {"OPEN_ISSUE", "CONFIRMED"}:
            return
        slot_key = slot or f"slot:general:{turn.episode_id}"

        superseded_ids: list[str] = []
        adopted_slot: str | None = None
        if turn.epistemic_status in {"CONFIRMED", "NEGATED"} and (slot is not None or superseded_set):
            for prior_entry in self.decision_ledger:
                if prior_entry.valid_to_ts is not None:
                    continue
                same_slot = slot is not None and prior_entry.topic_slot_key == slot_key
                explicit_entity_replace = bool(superseded_set) and (
                    prior_entry.topic_slot_key in superseded_set
                    or (
                        prior_entry.topic_slot_key.startswith("entity:")
                        and bool(set(prior_entry.entity_keys) & superseded_set)
                    )
                )
                if not same_slot and not explicit_entity_replace:
                    continue

                if turn.topic_slot_key is None and explicit_entity_replace and adopted_slot is None:
                    adopted_slot = prior_entry.topic_slot_key

                if prior_entry.turn_id in companion_turn_ids:
                    # Companion proposal is being confirmed by this anaphoric turn, not contradicted
                    prior_entry.valid_to_ts = turn.created_at_ts
                    prior_entry.superseded_by_turn_id = turn.turn_id
                    continue
                if turn.epistemic_status == "CONFIRMED" and prior_entry.epistemic_status in {
                    "PROPOSED",
                    "CONSIDERING",
                    "CONFIRMED",
                    "NEGATED",
                }:
                    prior_entry.valid_to_ts = turn.created_at_ts
                    prior_entry.superseded_by_turn_id = turn.turn_id
                    prior_entry.epistemic_status = "SUPERSEDED"
                    superseded_ids.append(prior_entry.turn_id)
                    prior_turn = self.turns_by_id.get(prior_entry.turn_id)
                    if prior_turn is not None:
                        prior_turn.epistemic_status = "SUPERSEDED"
                        prior_turn.superseded_by_turn_id = turn.turn_id
                elif turn.epistemic_status == "NEGATED" and prior_entry.epistemic_status in {
                    "PROPOSED",
                    "CONSIDERING",
                    "CONFIRMED",
                }:
                    prior_entry.valid_to_ts = turn.created_at_ts
                    prior_entry.superseded_by_turn_id = turn.turn_id
                    prior_entry.epistemic_status = "SUPERSEDED"
                    superseded_ids.append(prior_entry.turn_id)
                    prior_turn = self.turns_by_id.get(prior_entry.turn_id)
                    if prior_turn is not None:
                        prior_turn.epistemic_status = "SUPERSEDED"
                        prior_turn.superseded_by_turn_id = turn.turn_id

        if adopted_slot is not None:
            slot_key = adopted_slot
            turn.topic_slot_key = adopted_slot

        if superseded_ids:
            turn.supersedes_turn_ids = tuple(superseded_ids)

        if turn.epistemic_status in {"PROPOSED", "CONSIDERING", "CONFIRMED", "NEGATED", "OPEN_ISSUE"}:
            entity_keys = tuple(e.key for e in turn.entities)
            for cid in companion_turn_ids:
                comp = self.turns_by_id.get(cid)
                if comp is not None:
                    entity_keys = tuple(dict.fromkeys([*entity_keys, *(e.key for e in comp.entities)]))
            self.decision_ledger.append(
                DecisionLedgerEntry(
                    entry_id=f"{self.thread_id}:dec{len(self.decision_ledger)}",
                    topic_slot_key=slot_key,
                    turn_id=turn.turn_id,
                    turn_ordinal=turn.ordinal,
                    epistemic_status=turn.epistemic_status,
                    summary_text=turn.content[:320],
                    entity_keys=entity_keys,
                    companion_turn_ids=companion_turn_ids,
                    valid_from_ts=turn.created_at_ts,
                    valid_to_ts=turn.created_at_ts if turn.epistemic_status == "NEGATED" else None,
                    supersedes_turn_ids=turn.supersedes_turn_ids,
                )
            )

    def score_bm25_dynamic(
        self,
        query: str,
        *,
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """Exact corpus-wide Okapi BM25 over term_postings (zero IDF skew between early & late turns)."""
        q_term_freqs, _ = tokenize_bm25_terms(query)
        if not q_term_freqs or not self.turns:
            return {}

        total_docs = max(10, len(self.turns))
        avg_doc_len = max(8.0, self.total_token_words / max(1, len(self.turns)))
        scores: dict[str, float] = {}

        for word, q_tf in q_term_freqs.items():
            postings = self.term_postings.get(word)
            if not postings:
                continue
            df = self.doc_freqs.get(word, 0)
            if word in COMMON_STOPWORDS:
                idf = 0.1
            else:
                idf = math.log(1.0 + (max(0, total_docs - df) + 0.5) / (df + 0.5))
            q_weight = idf * (1.0 + 0.25 * (q_tf - 1))

            for turn_id, tf in postings:
                if allowed_turn_ids is not None and turn_id not in allowed_turn_ids:
                    continue
                turn_obj = self.turns_by_id.get(turn_id)
                if turn_obj is None:
                    continue
                len_norm = 1.0 - BM25_B + BM25_B * (turn_obj.doc_word_len / avg_doc_len)
                tf_weight = (tf * (BM25_K1 + 1.0)) / (tf + BM25_K1 * len_norm)
                scores[turn_id] = scores.get(turn_id, 0.0) + (q_weight * tf_weight)

        return scores

    def score_hybrid_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        *,
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """V1: Dynamic Corpus-Wide True BM25 + Dense Cosine hybrid score across authorized turns."""
        bm25_scores = self.score_bm25_dynamic(query, allowed_turn_ids=allowed_turn_ids)
        q_terms = extract_content_terms(query)
        dense_raw: dict[str, float] = {}
        lex_raw: dict[str, float] = {}
        for turn in self.turns:
            if allowed_turn_ids is not None and turn.turn_id not in allowed_turn_ids:
                continue
            dense_raw[turn.turn_id] = max(0.0, cosine_sim(query_vec, turn.dense_vector))
            bm25 = bm25_scores.get(turn.turn_id, 0.0)
            overlap = (len(q_terms & turn.content_terms) / len(q_terms)) if q_terms else 0.0
            lex_raw[turn.turn_id] = bm25 + 1.5 * overlap

        d_norm = min_max_normalize(dense_raw)
        l_norm = min_max_normalize(lex_raw)
        return {tid: 0.52 * d_norm.get(tid, 0.0) + 0.48 * l_norm.get(tid, 0.0) for tid in dense_raw}

    def _select_ppr_subgraph_turns(
        self,
        visible_turns: list[IndexedTurn],
        entity_act: dict[str, float],
        hybrid_scores: dict[str, float],
        visible_id_set: set[str],
    ) -> list[IndexedTurn]:
        """Bound PPR graph size to <= PPR_MAX_SUBGRAPH_TURNS for 1M-token / 25k-turn sessions."""
        if len(visible_turns) <= PPR_MAX_SUBGRAPH_TURNS:
            return visible_turns

        candidate_ids: set[str] = set()
        for e_key, act_val in entity_act.items():
            if act_val >= 0.08:
                for tid in self.entity_to_turn_counts.get(e_key, ()):
                    if tid in visible_id_set:
                        candidate_ids.add(tid)

        ranked_hybrid = sorted(
            (t.turn_id for t in visible_turns),
            key=lambda tid: hybrid_scores.get(tid, 0.0),
            reverse=True,
        )
        for tid in ranked_hybrid[:PPR_TOP_HYBRID_SEEDS]:
            candidate_ids.add(tid)

        # Expand +-1 intra-episode neighbors for continuity until cap
        expanded_ids = set(candidate_ids)
        for tid in list(candidate_ids):
            if len(expanded_ids) >= PPR_MAX_SUBGRAPH_TURNS:
                break
            turn_obj = self.turns_by_id.get(tid)
            if turn_obj is None:
                continue
            for delta in (-1, 1):
                nb_ord = turn_obj.ordinal + delta
                if 0 <= nb_ord < len(self.turns):
                    nb = self.turns[nb_ord]
                    if nb.episode_id == turn_obj.episode_id and nb.turn_id in visible_id_set:
                        expanded_ids.add(nb.turn_id)

        if len(expanded_ids) > PPR_MAX_SUBGRAPH_TURNS:
            prioritized = sorted(
                expanded_ids,
                key=lambda tid: (
                    1 if any(m.key in entity_act for m in self.turns_by_id[tid].entities) else 0,
                    hybrid_scores.get(tid, 0.0),
                ),
                reverse=True,
            )[:PPR_MAX_SUBGRAPH_TURNS]
            expanded_ids = set(prioritized)

        return [t for t in visible_turns if t.turn_id in expanded_ids]

    def score_graph_ppr_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        hybrid_scores: dict[str, float],
        *,
        allowed_turn_ids: set[str] | None = None,
        query_entities: Sequence[EntityMention] = (),
    ) -> dict[str, float]:
        """V2: Query-Conditioned Entity-Sentence Sparse Activation + Bounded igraph PPR."""
        visible_turns = [
            t for t in self.turns if allowed_turn_ids is None or t.turn_id in allowed_turn_ids
        ]
        if not visible_turns:
            return {}

        struct_mentions, _ = extract_structural_entities_and_sentences("query", 0, query)
        q_mentions_by_key: dict[str, EntityMention] = {m.key: m for m in struct_mentions}
        for m in query_entities:
            q_mentions_by_key.setdefault(m.key, m)
        q_mentions = list(q_mentions_by_key.values())
        q_terms = extract_content_terms(query)

        # 1. Seed entity activations (exact + lexical surface overlap + soft alias expansion)
        entity_act: dict[str, float] = {}
        for m in q_mentions:
            exact_hit = m.key in self.entity_to_turn_counts
            if exact_hit:
                entity_act[m.key] = max(entity_act.get(m.key, 0.0), 1.0)
            alias_weight = 0.12 if exact_hit else SOFT_ALIAS_WEIGHT
            for alias_key in self.soft_alias_neighbors.get(m.key, ()):
                if alias_key in self.entity_to_turn_counts:
                    entity_act[alias_key] = max(entity_act.get(alias_key, 0.0), alias_weight)

        for e_key, surface in self.entity_surfaces.items():
            surf_terms = extract_content_terms(surface)
            if surf_terms and surf_terms.issubset(q_terms):
                entity_act[e_key] = max(entity_act.get(e_key, 0.0), 0.90)
            elif surf_terms and (surf_terms & q_terms):
                ratio = len(surf_terms & q_terms) / len(surf_terms)
                if ratio >= 0.5:
                    entity_act[e_key] = max(entity_act.get(e_key, 0.0), 0.65 * ratio)

        # 2. Query-Conditioned Entity <-> Sentence Sparse Propagation (2 hops, gated by sentence-query cosine)
        visible_id_set = {t.turn_id for t in visible_turns}
        for _hop in range(2):
            if not entity_act:
                break
            updates: dict[str, float] = {}
            for e_key, act_val in list(entity_act.items()):
                if act_val < 0.08:
                    continue
                candidate_sents = [
                    s for s in self.entity_to_sentences.get(e_key, ()) if s.turn_id in visible_id_set
                ]
                scored_sents = [
                    (cosine_sim(query_vec, s.dense_vector), s) for s in candidate_sents
                ]
                scored_sents.sort(key=lambda item: item[0], reverse=True)
                for s_sim, sent in scored_sents[:SENTENCE_GATE_TOP_K]:
                    if s_sim < SENTENCE_GATE_MIN_COS:
                        continue
                    propagated = act_val * s_sim * 0.72
                    for co_entity in sent.entity_keys:
                        if co_entity != e_key and co_entity in self.entity_to_turn_counts:
                            updates[co_entity] = max(updates.get(co_entity, 0.0), propagated)
            for k, v in updates.items():
                entity_act[k] = max(entity_act.get(k, 0.0), v)

        # 3. Build bounded Ego-Subgraph Entity <-> Turn + intra-episode Turn_i <-> Turn_{i+1} igraph.Graph
        subgraph_turns = self._select_ppr_subgraph_turns(
            visible_turns,
            entity_act,
            hybrid_scores,
            visible_id_set,
        )
        subgraph_ent_set: set[str] = set(entity_act.keys())
        for turn in subgraph_turns:
            for m in turn.entities:
                subgraph_ent_set.add(m.key)
                subgraph_ent_set.update(
                    nb for nb in self.soft_alias_neighbors.get(m.key, ()) if nb in self.entity_to_turn_counts
                )
        active_entities = sorted(e for e in subgraph_ent_set if e in self.entity_to_turn_counts)
        if not active_entities:
            return dict(hybrid_scores)

        turn_idx_map = {t.turn_id: idx for idx, t in enumerate(subgraph_turns)}
        ent_offset = len(subgraph_turns)
        ent_idx_map = {e_key: ent_offset + idx for idx, e_key in enumerate(active_entities)}
        total_vertices = ent_offset + len(active_entities)

        edges: list[tuple[int, int]] = []
        weights: list[float] = []

        # Relative frequency bipartite edges: w(p, e) = c(p, e) / sum_{e' in E(p)} c(p, e')
        for turn in subgraph_turns:
            p_idx = turn_idx_map[turn.turn_id]
            counts_for_turn: list[tuple[int, int]] = []
            for m in turn.entities:
                e_idx = ent_idx_map.get(m.key)
                if e_idx is not None:
                    c = self.entity_to_turn_counts.get(m.key, {}).get(turn.turn_id, 1)
                    counts_for_turn.append((e_idx, c))
            denom = float(sum(c for _, c in counts_for_turn))
            if denom > 0:
                for e_idx, c in counts_for_turn:
                    edges.append((p_idx, e_idx))
                    weights.append(c / denom)

        # Strictly intra-episode, intra-thread continuity edges (fixes Zero-Mem cross-sample bug)
        for idx in range(len(subgraph_turns) - 1):
            t_cur = subgraph_turns[idx]
            t_nxt = subgraph_turns[idx + 1]
            if (
                t_cur.thread_id == t_nxt.thread_id
                and t_cur.episode_id == t_nxt.episode_id
                and (t_nxt.ordinal - t_cur.ordinal) == 1
            ):
                edges.append((idx, idx + 1))
                weights.append(0.15)

        # Soft alias edges between entity vertices
        seen_alias_pairs: set[tuple[int, int]] = set()
        for e_a, neighbors in self.soft_alias_neighbors.items():
            idx_a = ent_idx_map.get(e_a)
            if idx_a is None:
                continue
            for e_b in neighbors:
                idx_b = ent_idx_map.get(e_b)
                if idx_b is None or idx_a == idx_b:
                    continue
                pair = (min(idx_a, idx_b), max(idx_a, idx_b))
                if pair not in seen_alias_pairs:
                    seen_alias_pairs.add(pair)
                    edges.append(pair)
                    weights.append(SOFT_ALIAS_WEIGHT * 0.4)

        if not edges:
            return dict(hybrid_scores)

        reset = [0.0] * total_vertices
        for turn in subgraph_turns:
            reset[turn_idx_map[turn.turn_id]] = 0.30 * max(0.0, hybrid_scores.get(turn.turn_id, 0.0))
        for e_key, act_val in entity_act.items():
            e_idx = ent_idx_map.get(e_key)
            if e_idx is not None:
                reset[e_idx] = act_val

        if sum(reset) <= 1e-12:
            return dict(hybrid_scores)

        graph = ig.Graph(n=total_vertices, edges=edges, directed=False)
        ppr_vals = graph.personalized_pagerank(
            damping=PPR_DAMPING,
            reset=reset,
            weights=weights,
            implementation="prpack",
        )
        turn_ppr = {
            t.turn_id: (float(ppr_vals[turn_idx_map[t.turn_id]]) if t.turn_id in turn_idx_map else 0.0)
            for t in visible_turns
        }
        return min_max_normalize(turn_ppr)

    def score_hierarchy_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        hybrid_scores: dict[str, float],
        *,
        temporal_mode: str = "atemporal",
        hierarchy_bias: str = "none",
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """V3: 4-Level Temporal-Semantic Hierarchy (Session -> Drift Episode -> 3-Turn Window -> Turn)."""
        q_terms = extract_content_terms(query)
        prefers_early = hierarchy_bias == "early"
        prefers_recent = hierarchy_bias == "recent"

        ep_scores: dict[str, float] = {}
        n_eps = max(1, len(self.episodes))
        for ep in self.episodes:
            sem = max(0.0, cosine_sim(query_vec, ep.centroid))
            lex = (len(q_terms & ep.content_terms) / len(q_terms)) if q_terms else 0.0
            pos_ratio = ep.ordinal / max(1, n_eps - 1) if n_eps > 1 else 1.0
            pos_bonus = 0.0
            if prefers_early:
                pos_bonus = 0.20 * (1.0 - pos_ratio)
            elif prefers_recent or temporal_mode == "current_effective":
                pos_bonus = 0.12 * pos_ratio
            ep_scores[ep.episode_id] = 0.55 * sem + 0.35 * lex + pos_bonus
        ep_norm = min_max_normalize(ep_scores)

        win_scores: dict[str, float] = {}
        for win in self.windows:
            sem = max(0.0, cosine_sim(query_vec, win.dense_vector))
            lex = (len(q_terms & win.content_terms) / len(q_terms)) if q_terms else 0.0
            win_scores[win.window_id] = 0.60 * sem + 0.40 * lex
        win_norm = min_max_normalize(win_scores)

        turn_best_win: dict[str, float] = {}
        for win in self.windows:
            w_val = win_norm.get(win.window_id, 0.0)
            for tid in win.turn_ids:
                if w_val > turn_best_win.get(tid, 0.0):
                    turn_best_win[tid] = w_val

        combined: dict[str, float] = {}
        for turn in self.turns:
            if allowed_turn_ids is not None and turn.turn_id not in allowed_turn_ids:
                continue
            combined[turn.turn_id] = (
                0.25 * ep_norm.get(turn.episode_id, 0.0)
                + 0.30 * turn_best_win.get(turn.turn_id, 0.0)
                + 0.45 * hybrid_scores.get(turn.turn_id, 0.0)
            )
        return min_max_normalize(combined)


class BoundedSessionIndexCache:
    """Process-local LRU cache of IncrementalSessionIndex keyed by (scope_key, thread_id)."""

    def __init__(self, max_threads: int = 128) -> None:
        self._max_threads = max_threads
        self._store: OrderedDict[tuple[str, str], IncrementalSessionIndex] = OrderedDict()

    def get_or_create(self, *, scope_key: str, thread_id: str, scope_signature: str) -> IncrementalSessionIndex:
        cache_key = (scope_key, thread_id)
        existing = self._store.get(cache_key)
        if existing is not None and existing.scope_signature == scope_signature:
            self._store.move_to_end(cache_key)
            return existing

        fresh = IncrementalSessionIndex(thread_id, scope_signature=scope_signature)
        self._store[cache_key] = fresh
        self._store.move_to_end(cache_key)
        while len(self._store) > self._max_threads:
            self._store.popitem(last=False)
        return fresh

    def find_thread_entries(self, thread_id: str) -> list[IncrementalSessionIndex]:
        return [idx for (_, tid), idx in self._store.items() if tid == thread_id]

    def invalidate_thread(self, thread_id: str) -> int:
        keys = [k for k in self._store if k[1] == thread_id]
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    def clear(self) -> None:
        self._store.clear()


SESSION_INDEX_CACHE = BoundedSessionIndexCache(max_threads=128)
