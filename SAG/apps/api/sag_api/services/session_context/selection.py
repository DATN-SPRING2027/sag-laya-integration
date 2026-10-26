"""Bounded offline dual-view retrieval, fusion and atomic provenance closure."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from sag_api.services.search_index_service import COMMON_STOPWORDS
from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.graph import TextFeatures, TraceGraph, cosine, graph_scores
from sag_api.services.session_context.hierarchy import TraceHierarchy
from sag_api.services.session_context.scope import _check_time
from sag_api.services.session_context.types import ContextSelection, Coverage, EvidenceSpan, SessionScope, require


@dataclass(frozen=True, slots=True)
class SelectionCaps:
    main_limit: int
    bridge_limit: int
    local_limit: int
    max_messages: int
    max_codepoints: int
    beam: int
    steps: int
    damping: float
    rho: float

    def __post_init__(self):
        require(all(type(v) is int and v > 0 for v in (self.main_limit, self.max_messages, self.max_codepoints,
                                                      self.beam, self.steps)), "selection_limits")
        require(all(type(v) is int and v >= 0 for v in (self.bridge_limit, self.local_limit)), "closure_limits")
        require(all(type(v) in (int, float) and math.isfinite(v) and 0 < v < 1
                    for v in (self.damping, self.rho)), "selection_weights")


def normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in scores.values()), "view_scores")
    positive = {key: value for key, value in scores.items() if value > 0}
    if not positive:
        return {}
    low, high = min(positive.values()), max(positive.values())
    return {key: (value - low) / (high - low) if high > low else 1.0 for key, value in positive.items()}


def fuse_scores(graph: dict[str, float], hierarchy: dict[str, float], *,
                route: Literal["relational", "local"], rho: float) -> dict[str, float]:
    require(route in {"relational", "local"} and type(rho) in (int, float) and 0 < rho < 1, "fusion_route")
    g, h = normalize_scores(graph), normalize_scores(hierarchy)
    graph_weight = rho if route == "relational" else 1 - rho
    return {key: graph_weight * g.get(key, 0) + (1 - graph_weight) * h.get(key, 0) for key in sorted(g.keys() | h)}


def _terms(text: str) -> list[str]:
    # Raw session text: no financial-statement rewriting or hash collisions.
    return re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold())


def lexical_scores(graph: TraceGraph, query: str, *, budget: BudgetState) -> dict[str, float]:
    budget.consume_index_units(1 + len(query))
    query_terms = set(_terms(query)) - COMMON_STOPWORDS
    counts = []
    for message in graph.history.messages:
        budget.consume_index_units(1 + len(message.content))
        counts.append(Counter(_terms(message.content)))
    frequency = Counter(term for document in counts for term in document)
    lengths = [sum(document.values()) for document in counts]
    average = sum(lengths) / len(lengths) if lengths else 1.0
    result = {}
    for message, document, length in zip(graph.history.messages, counts, lengths, strict=True):
        budget.consume_index_units(1 + len(query_terms))
        score = 0.0
        for term in query_terms & document.keys():
            idf = math.log1p((len(counts) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = document[term]
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / (average or 1)))
        result[message.message_id] = score
    return normalize_scores(result)


def hierarchy_scores(graph: TraceGraph, hierarchy: TraceHierarchy, query: TextFeatures,
                     lexical: dict[str, float], *, beam: int, budget: BudgetState) -> dict[str, float]:
    require(hierarchy.snapshot == graph.history.snapshot
            and hierarchy.coverage.eligible_ids == graph.history.coverage.eligible_ids, "hierarchy_snapshot_changed")
    budget.consume_index_units(len(hierarchy.units) + len(graph.features))
    lookup = {unit.unit_id: unit for unit in hierarchy.units}
    features = {m.message_id: f for m, f in zip(graph.history.messages, graph.features, strict=True)}
    vectors, sizes, scores = {}, {}, {}
    for unit in hierarchy.units:
        budget.consume_index_units(1 + len(unit.children))
        if unit.kind == "turn":
            require(unit.origin is not None and unit.origin.message_id in features, "hierarchy_origin")
            feature = features[unit.origin.message_id]
            require(unit.origin.content_hash == feature.content_hash, "hierarchy_origin_changed")
            vector, size, lex = feature.vector, 1, lexical.get(unit.origin.message_id, 0)
        else:
            require(bool(unit.children) and all(child in vectors for child in unit.children), "hierarchy_children")
            size = sum(sizes[child] for child in unit.children)
            budget.consume_index_units(len(query.vector) * len(unit.children))
            vector = tuple(math.fsum(vectors[c][i] * sizes[c] for c in unit.children) / size
                           for i in range(len(query.vector)))
            lex = max(scores[child][1] for child in unit.children)
        budget.consume_index_units(len(vector))
        semantic = cosine(vector, query.vector) if math.hypot(*vector) else 0.0
        vectors[unit.unit_id], sizes[unit.unit_id] = vector, size
        scores[unit.unit_id] = (0.5 * semantic + 0.5 * lex, lex)

    def best(ids):
        budget.consume_index_units(len(ids) * max(1, len(ids).bit_length()))
        return sorted((key for key in ids if scores[key][0] > 0), key=lambda key: (-scores[key][0], key))[:beam]

    episodes = best([u.unit_id for u in hierarchy.units if u.kind == "episode"])
    result = {}
    for episode in episodes:
        for window in best(lookup[episode].children):
            for turn in best(lookup[window].children):
                message_id = lookup[turn].origin.message_id
                result[message_id] = 0.25 * scores[episode][0] + 0.25 * scores[window][0] + 0.5 * scores[turn][0]
    return result


@dataclass(frozen=True, slots=True)
class SelectionResult:
    selection: ContextSelection
    route: Literal["relational", "local"]
    graph_scores: tuple[tuple[str, float], ...]
    hierarchy_scores: tuple[tuple[str, float], ...]
    groups: tuple[tuple[str, ...], ...]


def select_context(graph: TraceGraph, hierarchy: TraceHierarchy, text: str, query: TextFeatures, *,
                   scope: SessionScope, budget: BudgetState, caps: SelectionCaps,
                   now: float | None = None) -> SelectionResult:
    """Normal mode only. No approval inference or automatic conflict resolution.

    Reauthorize and validate_selection with fresh raw rows before model/output.
    Codepoint packing is explicit; it is not a tokenizer-based billing limit.
    """
    require(isinstance(text, str) and bool(text.strip()), "query")
    raw_graph = graph_scores(graph, text, query, scope=scope, budget=budget,
                             steps=caps.steps, damping=caps.damping, now=now)
    lexical = lexical_scores(graph, text, budget=budget)
    g = normalize_scores(raw_graph)
    g = {key: 0.5 * g.get(key, 0) + 0.5 * lexical.get(key, 0) for key in raw_graph}
    h = hierarchy_scores(graph, hierarchy, query, lexical, beam=caps.beam, budget=budget)
    temporal = {"đó", "vừa", "trước", "sau", "latest", "earlier", "that", "then"}
    route = "local" if not query.mentions or set(_terms(text)) & temporal else "relational"
    budget.consume_index_units((len(g) + len(h)) * max(1, len(g).bit_length()))
    fused = fuse_scores(g, h, route=route, rho=caps.rho)
    order = {m.message_id: i for i, m in enumerate(graph.history.messages)}
    main = sorted((key for key, value in fused.items() if value > 0), key=lambda key: (-fused[key], order[key]))
    main = main[:caps.main_limit]
    main_entities = {entity for key in main for entity in graph.context_entities[order[key]]}
    budget.consume_index_units(sum(len(values) + 1 for values in graph.context_entities))
    bridges = [key for key in sorted(raw_graph, key=lambda key: (-raw_graph[key], order[key]))
               if key not in main and raw_graph[key] > 0
               and main_entities.intersection(graph.context_entities[order[key]])]
    budget.consume_index_units(len(hierarchy.units))
    units = {unit.unit_id: unit for unit in hierarchy.units}
    main_turns = {unit.unit_id for unit in hierarchy.units if unit.kind == "turn"
                  and unit.origin.message_id in main}
    neighbors = []
    for unit in hierarchy.units:
        if unit.kind == "local" and unit.anchor_id in main_turns:
            budget.consume_index_units(len(unit.children))
            neighbors.extend(units[child].origin.message_id for child in unit.children
                             if units[child].origin.message_id not in main)
    roots = list(dict.fromkeys(main + bridges[:caps.bridge_limit] + list(dict.fromkeys(neighbors))[:caps.local_limit]))
    records = {message.message_id: message for message in graph.history.messages}
    chosen, groups, spans = set(), [], []
    skipped = False
    used_codepoints = 0
    for root in roots:
        closure, stack = set(), [root]
        while stack:
            key = stack.pop()
            if key in closure:
                continue
            require(key in records, "dependency_unavailable")
            message = records[key]
            budget.consume_index_units(1 + len(message.dependencies))
            closure.add(key)
            for ref in message.dependencies:
                require(ref.message_id in records
                        and graph.features[order[ref.message_id]].content_hash == ref.content_hash,
                        "dependency_changed")
                stack.append(ref.message_id)
        additions = closure - chosen
        cost = sum(len(records[key].content) for key in additions)
        if len(chosen | closure) > caps.max_messages or used_codepoints + cost > caps.max_codepoints:
            skipped = True
            continue
        budget.consume_index_units(len(additions) + (cost + 1023) // 1024)
        groups.append(tuple(sorted(closure, key=order.__getitem__)))
        for key in sorted(additions, key=order.__getitem__):
            message = records[key]
            if message.content:
                spans.append(EvidenceSpan(message.reference, 0, len(message.content)))
        chosen.update(closure)
        used_codepoints += cost
    budget.consume_index_units(0)
    _check_time(scope, now)
    reason = "selection_cap" if skipped else "unknown_provenance" if graph.history.unknown_provenance else None
    selection = ContextSelection(graph.history.snapshot, "normal", "partial" if reason else "ready", tuple(spans),
                                 Coverage(graph.history.coverage.eligible_ids, frozenset(chosen)), reason)
    return SelectionResult(selection, route, tuple(g.items()), tuple(h.items()), tuple(groups))
