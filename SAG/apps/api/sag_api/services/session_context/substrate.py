"""Incremental Multi-View Token-Free Memory Substrate for SAG-ECCE.

Reuses `SAG main` infrastructure:
- `search_index_service`: `compute_sparse_bm25_vector`, `compute_unit_embeddings`,
  `get_cached_text_embedding`, `put_cached_text_embedding`
- `generation.prompt`: `estimate_tokens`
- `igraph`: C-accelerated `personalized_pagerank(..., damping=0.6, implementation="prpack")`
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import igraph as ig

from sag_api.generation.prompt import estimate_tokens
from sag_api.services.search_index_service import (
    compute_sparse_bm25_vector,
    compute_unit_embeddings,
    get_cached_text_embedding,
    put_cached_text_embedding,
)
from sag_api.services.session_context.extractor import (
    classify_epistemic_state,
    extract_content_terms,
    extract_turn_entities_and_sentences,
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
EPISODE_TIME_GAP_SECONDS = 1800.0  # 30 minutes
EPISODE_CENTROID_COS_THRESHOLD = 0.72
EPISODE_LEXICAL_JACCARD_THRESHOLD = 0.18
EPISODE_MAX_TURNS = 12
SOFT_ALIAS_WEIGHT = 0.65
SENTENCE_GATE_TOP_K = 3
SENTENCE_GATE_MIN_COS = 0.12
PPR_DAMPING = 0.60


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


async def embed_texts_with_main_cache(
    texts: Sequence[str],
    *,
    embedder: Any | None = None,
) -> list[tuple[float, ...]]:
    """Batch-embed texts via SAG main's HTTP/SiliconFlow embedder + shared LRU cache, or deterministic fallback."""
    if not texts:
        return []
    if embedder is None:
        return [deterministic_ngram_vector(t) for t in texts]

    results: list[tuple[float, ...] | None] = [None] * len(texts)
    missing_indices: list[int] = []
    missing_texts: list[str] = []
    for idx, txt in enumerate(texts):
        cached = get_cached_text_embedding(txt)
        if cached is not None:
            results[idx] = l2_normalize(cached)
        else:
            missing_indices.append(idx)
            missing_texts.append(txt)

    if missing_texts:
        try:
            computed = await compute_unit_embeddings(embedder, missing_texts)
            for idx, txt, raw_vec in zip(missing_indices, missing_texts, computed, strict=True):
                put_cached_text_embedding(txt, raw_vec)
                results[idx] = l2_normalize(raw_vec)
        except Exception:  # noqa: BLE001 - fail-open to deterministic local projection if HTTP embedder errors
            return [deterministic_ngram_vector(t) for t in texts]

    return [vec if vec is not None else deterministic_ngram_vector(texts[i]) for i, vec in enumerate(results)]


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

    def __init__(self, thread_id: str, *, scope_signature: str = "") -> None:
        self.thread_id = thread_id
        self.scope_signature = scope_signature
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
        self.total_token_words: int = 0

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
        project_aliases: Sequence[tuple[str, str]] = (),
    ) -> int:
        """Incrementally index only unseen messages in chronological order."""
        target_mode = f"embedder:{type(embedder).__name__}" if embedder is not None else "deterministic_ngram"
        if self.embedding_mode not in {"unset", target_mode}:
            # Vector space changed -> rebuild cleanly in the new vector space
            self.__init__(self.thread_id, scope_signature=self.scope_signature)
        self.embedding_mode = target_mode

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
            return 0

        # Pre-extract entities & sentences for all pending turns (0 LLM calls)
        extracted_per_msg: list[tuple[list[EntityMention], list[tuple[str, tuple[str, ...]]]]] = []
        texts_to_embed: list[str] = []
        turn_vec_offsets: list[int] = []
        sent_vec_ranges: list[tuple[int, int]] = []

        base_ordinal = len(self.turns)
        for offset, msg in enumerate(pending):
            t_id = str(msg["id"])
            content = normalize_session_text(str(msg["content"]))
            mentions, sent_items = extract_turn_entities_and_sentences(
                t_id,
                base_ordinal + offset,
                content,
                project_aliases=project_aliases,
            )
            extracted_per_msg.append((mentions, sent_items))
            turn_vec_offsets.append(len(texts_to_embed))
            texts_to_embed.append(content)
            s_start_idx = len(texts_to_embed)
            for s_text, _ in sent_items:
                texts_to_embed.append(s_text)
            sent_vec_ranges.append((s_start_idx, len(texts_to_embed)))

        # Single batch embedding call across all new turns + sentences
        all_vectors = await embed_texts_with_main_cache(texts_to_embed, embedder=embedder)

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

            mentions, sent_items = extracted_per_msg[offset]
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
            for term in content_terms:
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1
            word_len = max(1, len(re.findall(r"\w+", content.casefold())))
            self.total_token_words += word_len
            avg_len = self.total_token_words / max(1, ordinal + 1)

            sparse_vec = sparse_dict_from_bm25(
                content,
                avg_doc_len=avg_len,
                corpus_doc_freqs=self.doc_freqs,
                total_corpus_docs=max(50, ordinal + 1),
            )
            epistemic_status, slot_key, is_anaphoric = classify_epistemic_state(content, role=role)

            episode = self._assign_or_open_episode(
                turn_id=t_id,
                turn_vec=t_vec,
                content_terms=content_terms,
                ts=ts,
            )

            # Anaphoric confirmation resolution ("Chốt phương án 2 như anh Minh vừa đề xuất nhé")
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
                epistemic_status=epistemic_status,
                topic_slot_key=slot_key,
                is_anaphoric_or_short=is_anaphoric,
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
            time_gap > EPISODE_TIME_GAP_SECONDS
            or len(cur.turn_ids) >= EPISODE_MAX_TURNS
            or drift_detected
        )

        if should_split:
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
        for mention in turn.entities:
            self.entity_surfaces.setdefault(mention.key, mention.surface)
            turn_counts = self.entity_to_turn_counts.setdefault(mention.key, {})
            occ = max(1, turn.content.casefold().count(mention.surface.casefold()))
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
        slot = turn.topic_slot_key or (turn.entities[0].key if turn.entities else None)
        if slot is None and turn.epistemic_status not in {"OPEN_ISSUE", "CONFIRMED"}:
            return
        slot_key = slot or f"slot:general:{turn.episode_id}"

        superseded_ids: list[str] = []
        if turn.epistemic_status in {"CONFIRMED", "NEGATED"} and slot is not None:
            for prior_entry in self.decision_ledger:
                if prior_entry.topic_slot_key != slot_key or prior_entry.valid_to_ts is not None:
                    continue
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

    def score_hybrid_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        *,
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """V1: Unicode True BM25 + Dense Cosine hybrid score across all authorized session turns."""
        avg_len = self.total_token_words / max(1, len(self.turns))
        q_sparse = sparse_dict_from_bm25(
            query,
            avg_doc_len=avg_len,
            corpus_doc_freqs=self.doc_freqs,
            total_corpus_docs=max(50, len(self.turns)),
        )
        q_terms = extract_content_terms(query)
        dense_raw: dict[str, float] = {}
        lex_raw: dict[str, float] = {}
        for turn in self.turns:
            if allowed_turn_ids is not None and turn.turn_id not in allowed_turn_ids:
                continue
            dense_raw[turn.turn_id] = max(0.0, cosine_sim(query_vec, turn.dense_vector))
            bm25 = sparse_dot(q_sparse, turn.sparse_bm25)
            overlap = (len(q_terms & turn.content_terms) / len(q_terms)) if q_terms else 0.0
            lex_raw[turn.turn_id] = bm25 + 1.5 * overlap

        d_norm = min_max_normalize(dense_raw)
        l_norm = min_max_normalize(lex_raw)
        return {tid: 0.52 * d_norm.get(tid, 0.0) + 0.48 * l_norm.get(tid, 0.0) for tid in dense_raw}

    def score_graph_ppr_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        hybrid_scores: dict[str, float],
        *,
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """V2: Zero-Mem Query-Conditioned Entity-Sentence Sparse Activation + igraph PPR (damping=0.6)."""
        visible_turns = [
            t for t in self.turns if allowed_turn_ids is None or t.turn_id in allowed_turn_ids
        ]
        if not visible_turns:
            return {}

        q_mentions, _ = extract_turn_entities_and_sentences("query", 0, query)
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

        # 3. Build bipartite Entity <-> Turn + intra-episode Turn_i <-> Turn_{i+1} igraph.Graph
        active_entities = sorted(self.entity_to_turn_counts.keys())
        if not active_entities:
            return dict(hybrid_scores)

        turn_idx_map = {t.turn_id: idx for idx, t in enumerate(visible_turns)}
        ent_offset = len(visible_turns)
        ent_idx_map = {e_key: ent_offset + idx for idx, e_key in enumerate(active_entities)}
        total_vertices = ent_offset + len(active_entities)

        edges: list[tuple[int, int]] = []
        weights: list[float] = []

        # Relative frequency bipartite edges: w(p, e) = c(p, e) / sum_{e' in E(p)} c(p, e')
        for turn in visible_turns:
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
        for idx in range(len(visible_turns) - 1):
            t_cur = visible_turns[idx]
            t_nxt = visible_turns[idx + 1]
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
        for turn in visible_turns:
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
        turn_ppr = {t.turn_id: float(ppr_vals[turn_idx_map[t.turn_id]]) for t in visible_turns}
        return min_max_normalize(turn_ppr)

    def score_hierarchy_view(
        self,
        query: str,
        query_vec: tuple[float, ...],
        hybrid_scores: dict[str, float],
        *,
        temporal_mode: str = "atemporal",
        allowed_turn_ids: set[str] | None = None,
    ) -> dict[str, float]:
        """V3: 4-Level Temporal-Semantic Hierarchy (Session -> Drift Episode -> 3-Turn Window -> Turn)."""
        q_terms = extract_content_terms(query)
        q_lower = query.casefold()
        prefers_early = bool(re.search(r"\b(?:đầu\s+phiên|đầu\s+buổi|lúc\s+đầu|ban\s+đầu|first|beginning)\b", q_lower))
        prefers_recent = bool(
            re.search(r"\b(?:vừa\s+bàn|lúc\s+nãy|mới\s+nhất|gần\s+nhất|tiếp\s+tục|just\s+now|latest)\b", q_lower)
        )

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

    def invalidate_thread(self, thread_id: str) -> int:
        keys = [k for k in self._store if k[1] == thread_id]
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    def clear(self) -> None:
        self._store.clear()


SESSION_INDEX_CACHE = BoundedSessionIndexCache(max_threads=128)
