"""Compositional 7-Axis RetrievalPlan IR, validator, and slot/cardinality resolver.

Replaces hardcoded top-k overrides and flat profile enumerations with an
orthogonal 7-axis specification:
1. TargetUnit      (PASSAGE, DOCUMENT, TOPIC, FACT, TURN)
2. SelectionMode   (RELEVANCE, FILTER, LOOKUP)
3. FieldPredicate  (validated metadata/scope/temporal constraints)
4. CardinalitySpec (ONE, N, ALL with API > QUERY > DEFAULT precedence)
5. OrderMode       (RELEVANCE, TIME_DESC, TIME_ASC, FIELD)
6. Decomposition   (NONE, PARALLEL, SEQUENTIAL)
7. OutputMode      (ANSWER, LIST, COUNT, SUMMARIZE, COMPARE, OPEN)
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from sag_api.core.config import FIXED_SEARCH_TOP_K

RETRIEVAL_PLAN_VERSION = "qsp-v2"


class TargetUnit(StrEnum):
    PASSAGE = "PASSAGE"
    DOCUMENT = "DOCUMENT"
    TOPIC = "TOPIC"
    FACT = "FACT"
    TURN = "TURN"


class SelectionMode(StrEnum):
    RELEVANCE = "RELEVANCE"
    FILTER = "FILTER"
    LOOKUP = "LOOKUP"


class OrderMode(StrEnum):
    RELEVANCE = "RELEVANCE"
    TIME_DESC = "TIME_DESC"
    TIME_ASC = "TIME_ASC"
    FIELD = "FIELD"


class DecompositionMode(StrEnum):
    NONE = "NONE"
    PARALLEL = "PARALLEL"
    SEQUENTIAL = "SEQUENTIAL"


class OutputMode(StrEnum):
    ANSWER = "ANSWER"
    LIST = "LIST"
    COUNT = "COUNT"
    SUMMARIZE = "SUMMARIZE"
    COMPARE = "COMPARE"
    OPEN = "OPEN"


class CardinalityMode(StrEnum):
    ONE = "ONE"
    N = "N"
    ALL = "ALL"


class CountSource(StrEnum):
    API = "API"
    QUERY = "QUERY"
    DEFAULT = "DEFAULT"


class AnswerShape(StrEnum):
    FACT_ANSWER = "fact_answer"
    MULTI_FACET_COMPARE = "multi_facet_compare"
    DOCUMENT_LIST = "document_list"
    EXHAUSTIVE_COUNT = "exhaustive_count"
    SUMMARIZE_SET = "summarize_set"
    OPEN_DOCUMENT = "open_document"


VALID_ANSWER_SHAPES: frozenset[str] = frozenset(item.value for item in AnswerShape)

FIELD_REGISTRY: dict[str, tuple[str, ...]] = {
    "document_name": ("eq", "contains", "in"),
    "source_id": ("eq", "in"),
    "content_type": ("eq", "in"),
    "time_range": ("gte", "lte", "between"),
    "temporal_mode": ("eq",),
    "node_id": ("eq", "in"),
}

# Default and maximum slot caps per (TargetUnit, OutputMode)
_CARDINALITY_BOUNDS: dict[tuple[TargetUnit, OutputMode], tuple[int, int]] = {
    (TargetUnit.PASSAGE, OutputMode.ANSWER): (FIXED_SEARCH_TOP_K, 20),
    (TargetUnit.PASSAGE, OutputMode.COMPARE): (8, 15),
    (TargetUnit.PASSAGE, OutputMode.SUMMARIZE): (8, 20),
    (TargetUnit.PASSAGE, OutputMode.LIST): (8, 25),
    (TargetUnit.PASSAGE, OutputMode.COUNT): (20, 50),
    (TargetUnit.PASSAGE, OutputMode.OPEN): (1, 5),
    (TargetUnit.DOCUMENT, OutputMode.OPEN): (1, 50),
    (TargetUnit.DOCUMENT, OutputMode.LIST): (10, 50),
    (TargetUnit.DOCUMENT, OutputMode.SUMMARIZE): (10, 50),
    (TargetUnit.DOCUMENT, OutputMode.COMPARE): (6, 20),
    (TargetUnit.DOCUMENT, OutputMode.ANSWER): (5, 25),
    (TargetUnit.DOCUMENT, OutputMode.COUNT): (50, 100),
    (TargetUnit.TOPIC, OutputMode.LIST): (10, 30),
    (TargetUnit.TOPIC, OutputMode.SUMMARIZE): (10, 30),
    (TargetUnit.TOPIC, OutputMode.ANSWER): (8, 25),
    (TargetUnit.FACT, OutputMode.ANSWER): (FIXED_SEARCH_TOP_K, 20),
    (TargetUnit.FACT, OutputMode.LIST): (10, 30),
    (TargetUnit.FACT, OutputMode.COUNT): (30, 100),
    (TargetUnit.TURN, OutputMode.ANSWER): (8, 25),
    (TargetUnit.TURN, OutputMode.LIST): (15, 50),
    (TargetUnit.TURN, OutputMode.SUMMARIZE): (20, 50),
}


@dataclass(frozen=True, slots=True)
class FieldPredicate:
    field: str
    op: str
    value: Any

    def is_supported(self) -> bool:
        allowed_ops = FIELD_REGISTRY.get(self.field)
        return allowed_ops is not None and self.op in allowed_ops

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "op": self.op,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class CardinalitySpec:
    mode: CardinalityMode
    requested_n: int | None
    effective_n: int
    max_cap: int
    count_source: CountSource
    cursor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "requested_n": self.requested_n,
            "effective_n": self.effective_n,
            "max_cap": self.max_cap,
            "count_source": self.count_source.value,
            "cursor": self.cursor,
        }


@dataclass(frozen=True, slots=True)
class DecompositionSpec:
    mode: DecompositionMode
    subqueries: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "subqueries": list(self.subqueries),
        }


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    planner_version: str
    target: TargetUnit
    selection: SelectionMode
    constraints: tuple[FieldPredicate, ...]
    cardinality: CardinalitySpec
    order: OrderMode
    decomposition: DecompositionSpec
    output: OutputMode
    answer_shape: AnswerShape = AnswerShape.FACT_ANSWER
    is_unfiltered_catalog: bool = False
    fallback_used: bool = False
    unsupported_reason: str | None = None
    temporal_mode: str = "atemporal"
    entities: tuple[str, ...] = ()

    @property
    def per_document_limit(self) -> int | None:
        """When target is DOCUMENT, return at most 1 representative section per document."""
        return 1 if self.target == TargetUnit.DOCUMENT else None

    def as_trace(self) -> dict[str, Any]:
        return {
            "planner_version": self.planner_version,
            "target": self.target.value,
            "selection": self.selection.value,
            "constraints": [item.as_dict() for item in self.constraints],
            "cardinality": self.cardinality.as_dict(),
            "order": self.order.value,
            "decomposition": self.decomposition.as_dict(),
            "output": self.output.value,
            "answer_shape": self.answer_shape.value,
            "is_unfiltered_catalog": self.is_unfiltered_catalog,
            "per_document_limit": self.per_document_limit,
            "fallback_used": self.fallback_used,
            "unsupported_reason": self.unsupported_reason,
        }


def _normalize_answer_shape(answer_shape: str | None) -> AnswerShape:
    normalized = (answer_shape or "").strip().lower()
    if normalized in VALID_ANSWER_SHAPES:
        return AnswerShape(normalized)
    return AnswerShape.FACT_ANSWER


def _normalize_order_mode(order_mode: str | None) -> OrderMode:
    normalized = (order_mode or "").strip().lower()
    if normalized == "time_desc":
        return OrderMode.TIME_DESC
    if normalized == "time_asc":
        return OrderMode.TIME_ASC
    if normalized == "field":
        return OrderMode.FIELD
    return OrderMode.RELEVANCE


def _normalize_target_unit(target_unit: str | None) -> TargetUnit | None:
    if not target_unit:
        return None
    normalized = str(target_unit).strip().upper()
    try:
        return TargetUnit(normalized)
    except ValueError:
        return None


def _shape_to_target_output(
    answer_shape: AnswerShape,
    *,
    target_unit: TargetUnit | None,
    explicit_count: int | None,
    is_catalog_query: bool | None,
) -> tuple[TargetUnit, OutputMode, CardinalityMode]:
    """Resolve (TargetUnit, OutputMode, CardinalityMode) from structured UnifiedRouteDecision signals."""
    if answer_shape == AnswerShape.DOCUMENT_LIST:
        allowed_list_targets = {TargetUnit.DOCUMENT, TargetUnit.TOPIC, TargetUnit.FACT, TargetUnit.TURN}
        resolved_target = target_unit if target_unit in allowed_list_targets else TargetUnit.DOCUMENT
        card = (
            CardinalityMode.ALL
            if (explicit_count is None and is_catalog_query and resolved_target == TargetUnit.DOCUMENT)
            else CardinalityMode.N
        )
        return resolved_target, OutputMode.LIST, card
    if answer_shape == AnswerShape.EXHAUSTIVE_COUNT:
        allowed_count_targets = {TargetUnit.DOCUMENT, TargetUnit.FACT}
        resolved_target = target_unit if target_unit in allowed_count_targets else TargetUnit.DOCUMENT
        return resolved_target, OutputMode.COUNT, CardinalityMode.ALL
    if answer_shape == AnswerShape.SUMMARIZE_SET:
        allowed_sum_targets = {TargetUnit.DOCUMENT, TargetUnit.TOPIC, TargetUnit.TURN}
        resolved_target = target_unit if target_unit in allowed_sum_targets else TargetUnit.DOCUMENT
        return resolved_target, OutputMode.SUMMARIZE, CardinalityMode.N
    if answer_shape == AnswerShape.OPEN_DOCUMENT:
        card = CardinalityMode.ONE if (explicit_count is None or explicit_count == 1) else CardinalityMode.N
        return TargetUnit.DOCUMENT, OutputMode.OPEN, card
    if answer_shape == AnswerShape.MULTI_FACET_COMPARE:
        allowed_cmp_targets = {TargetUnit.PASSAGE, TargetUnit.DOCUMENT}
        resolved_target = target_unit if target_unit in allowed_cmp_targets else TargetUnit.PASSAGE
        return resolved_target, OutputMode.COMPARE, CardinalityMode.N
    resolved_target = target_unit or TargetUnit.PASSAGE
    return resolved_target, OutputMode.ANSWER, CardinalityMode.N


def build_retrieval_plan(
    query: str,
    *,
    requested_top_k: int | None = None,
    answer_shape: str | None = None,
    order_mode: str | None = None,
    explicit_count: int | None = None,
    target_unit: str | None = None,
    is_catalog_query: bool | None = None,
    strategy_mode: str | None = None,
    clauses: tuple[str, ...] = (),
    multi_hop: bool = False,
    exact_lookup: bool = False,
    constraints: tuple[FieldPredicate, ...] = (),
    cursor: str | None = None,
    temporal_mode: str | None = None,
    entities: tuple[str, ...] = (),
) -> RetrievalPlan:
    """Compile structured routing signals + API parameters into a validated 7-axis RetrievalPlan."""
    query = unicodedata.normalize("NFC", query or "")
    resolved_shape = _normalize_answer_shape(answer_shape)
    resolved_order = _normalize_order_mode(order_mode)
    normalized_target = _normalize_target_unit(target_unit)
    clean_count = (
        int(explicit_count)
        if isinstance(explicit_count, int) and not isinstance(explicit_count, bool) and explicit_count >= 1
        else None
    )

    target, output, card_mode = _shape_to_target_output(
        resolved_shape,
        target_unit=normalized_target,
        explicit_count=clean_count,
        is_catalog_query=is_catalog_query,
    )

    if strategy_mode == "GLOBAL_TOPIC" and target == TargetUnit.PASSAGE and output == OutputMode.ANSWER:
        target = TargetUnit.TOPIC
        output = OutputMode.SUMMARIZE

    is_unfiltered_catalog = bool(
        is_catalog_query
        and target == TargetUnit.DOCUMENT
        and output in {OutputMode.LIST, OutputMode.COUNT}
        and clean_count is None
    )

    # Validate constraints against FIELD_REGISTRY
    valid_constraints: list[FieldPredicate] = []
    fallback_used = False
    unsupported_reason: str | None = None
    for pred in constraints:
        if pred.is_supported():
            valid_constraints.append(pred)
        else:
            fallback_used = True
            unsupported_reason = f"unsupported_predicate:{pred.field}:{pred.op}"

    # Determine SelectionMode
    if exact_lookup or strategy_mode == "EXACT":
        selection = SelectionMode.LOOKUP
    elif valid_constraints and output in {OutputMode.LIST, OutputMode.COUNT} and not query.strip():
        selection = SelectionMode.FILTER
    else:
        selection = SelectionMode.RELEVANCE

    # Determine DecompositionSpec
    effective_subqueries = tuple(c for c in clauses if c.strip()) if len(clauses) >= 2 else ()
    if effective_subqueries:
        decomp = DecompositionSpec(mode=DecompositionMode.PARALLEL, subqueries=effective_subqueries)
    elif multi_hop or strategy_mode == "MULTI_HOP":
        decomp = DecompositionSpec(mode=DecompositionMode.SEQUENTIAL, subqueries=())
    else:
        decomp = DecompositionSpec(mode=DecompositionMode.NONE, subqueries=())

    # Resolve CardinalitySpec with strict precedence: API > QUERY > DEFAULT
    default_n, max_cap = _CARDINALITY_BOUNDS.get(
        (target, output),
        (FIXED_SEARCH_TOP_K, 20),
    )
    if (
        target == TargetUnit.PASSAGE
        and output == OutputMode.COMPARE
        and len(decomp.subqueries) >= 2
    ):
        default_n = min(10, max(6, 3 * len(decomp.subqueries)))
        max_cap = max(max_cap, 15)

    if requested_top_k is not None:
        if requested_top_k < 1:
            raise ValueError("top_k must be positive")
        api_cap = max(max_cap, 50) if requested_top_k > max_cap else max_cap
        effective_n = min(max(1, int(requested_top_k)), api_cap)
        resolved_mode = CardinalityMode.ONE if effective_n == 1 else CardinalityMode.N
        cardinality = CardinalitySpec(
            mode=resolved_mode,
            requested_n=int(requested_top_k),
            effective_n=effective_n,
            max_cap=api_cap,
            count_source=CountSource.API,
            cursor=cursor,
        )
    elif clean_count is not None:
        effective_n = min(max(1, clean_count), max_cap)
        resolved_mode = CardinalityMode.ONE if effective_n == 1 else card_mode
        cardinality = CardinalitySpec(
            mode=resolved_mode,
            requested_n=clean_count,
            effective_n=effective_n,
            max_cap=max_cap,
            count_source=CountSource.QUERY,
            cursor=cursor,
        )
    else:
        effective_default_n = (
            max_cap
            if (target == TargetUnit.DOCUMENT and output == OutputMode.LIST and card_mode == CardinalityMode.ALL)
            else default_n
        )
        cardinality = CardinalitySpec(
            mode=card_mode,
            requested_n=None,
            effective_n=effective_default_n,
            max_cap=max_cap,
            count_source=CountSource.DEFAULT,
            cursor=cursor,
        )

    clean_temporal = (temporal_mode or "").strip().lower() or "atemporal"
    clean_entities = tuple(
        dict.fromkeys(
            unicodedata.normalize("NFC", str(ent)).strip()
            for ent in entities
            if str(ent).strip()
        )
    )
    return RetrievalPlan(
        planner_version=RETRIEVAL_PLAN_VERSION,
        target=target,
        selection=selection,
        constraints=tuple(valid_constraints),
        cardinality=cardinality,
        order=resolved_order,
        decomposition=decomp,
        output=output,
        answer_shape=resolved_shape,
        is_unfiltered_catalog=is_unfiltered_catalog,
        fallback_used=fallback_used,
        unsupported_reason=unsupported_reason,
        temporal_mode=clean_temporal,
        entities=clean_entities,
    )


def rebuild_retrieval_plan_with_top_k(
    plan: RetrievalPlan,
    requested_top_k: int | None,
) -> RetrievalPlan:
    """Apply an explicit top_k override onto an existing RetrievalPlan without dropping other axes (S4-11)."""
    if requested_top_k is None:
        return plan
    if requested_top_k < 1:
        raise ValueError("top_k must be positive")
    _, base_max_cap = _CARDINALITY_BOUNDS.get(
        (plan.target, plan.output),
        (FIXED_SEARCH_TOP_K, 20),
    )
    if (
        plan.target == TargetUnit.PASSAGE
        and plan.output == OutputMode.COMPARE
        and len(plan.decomposition.subqueries) >= 2
    ):
        base_max_cap = max(base_max_cap, 15)
    api_cap = max(base_max_cap, 50) if requested_top_k > base_max_cap else base_max_cap
    effective_n = min(max(1, int(requested_top_k)), api_cap)
    resolved_mode = CardinalityMode.ONE if effective_n == 1 else CardinalityMode.N
    updated_cardinality = CardinalitySpec(
        mode=resolved_mode,
        requested_n=int(requested_top_k),
        effective_n=effective_n,
        max_cap=api_cap,
        count_source=CountSource.API,
        cursor=plan.cardinality.cursor,
    )
    return replace(plan, cardinality=updated_cardinality)


def group_sections_by_document(
    sections: list[Any],
    *,
    limit: int,
    relative_floor: float = 0.50,
    absolute_floor: float = 0.35,
) -> list[Any]:
    """Group ACL-verified sections by document identity (1 representative section per document).

    Enforces a dynamic relevance cutoff so that if a user asks for 20 documents
    but only 7 documents are genuinely relevant, we return 7 instead of padding
    with 13 irrelevant documents.
    """
    if not sections or limit <= 0:
        return []

    best_by_doc: dict[str, Any] = {}
    ordered_doc_keys: list[str] = []
    for section in sections:
        doc_key = str(
            getattr(section, "document_id", None)
            or getattr(section, "document_version_id", None)
            or getattr(section, "document_name", None)
            or getattr(section, "chunk_id", None)
            or ""
        )
        if not doc_key:
            continue
        score = float(getattr(section, "score", 0.0) or 0.0)
        existing = best_by_doc.get(doc_key)
        if existing is None:
            ordered_doc_keys.append(doc_key)
            best_by_doc[doc_key] = section
        elif score > float(getattr(existing, "score", 0.0) or 0.0):
            best_by_doc[doc_key] = section

    if not ordered_doc_keys:
        return []

    representatives = sorted(
        (best_by_doc[k] for k in ordered_doc_keys),
        key=lambda sec: (
            -float(getattr(sec, "score", 0.0) or 0.0),
            int(getattr(sec, "rank", 0) or 0),
            str(getattr(sec, "document_id", None) or ""),
        ),
    )
    top_score = float(getattr(representatives[0], "score", 0.0) or 0.0)
    min_score = max(absolute_floor, top_score * relative_floor)
    high_confidence_semantic_floor = 0.70

    selected: list[Any] = []
    for sec in representatives:
        if len(selected) >= limit:
            break
        score = float(getattr(sec, "score", 0.0) or 0.0)
        raw_dense = getattr(sec, "raw_dense_score", None)
        is_high_conf_semantic = (
            raw_dense is not None
            and isinstance(raw_dense, (int, float))
            and not isinstance(raw_dense, bool)
            and float(raw_dense) >= high_confidence_semantic_floor
        )
        if not is_high_conf_semantic and (score < absolute_floor or (selected and score < min_score)):
            continue
        rank = len(selected)
        if hasattr(sec, "model_copy"):
            selected.append(sec.model_copy(update={"rank": rank}))
        else:
            selected.append(sec)
    return selected
