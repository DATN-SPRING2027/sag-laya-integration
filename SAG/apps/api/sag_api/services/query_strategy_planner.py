"""Deterministic bridge from query features to retrieval strategy modes and 7-axis RetrievalPlan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sag_api.services.query_analysis import QueryFeatures, extract_query_features
from sag_api.services.retrieval_plan import (
    DecompositionMode,
    DecompositionSpec,
    FieldPredicate,
    RetrievalPlan,
    build_retrieval_plan,
)
from sag_api.services.temporal_date_parser import extract_as_of_datetime


class RetrievalMode(StrEnum):
    EXACT = "EXACT"
    LOCAL_FACTUAL = "LOCAL_FACTUAL"
    ENTITY_RELATIONAL = "ENTITY_RELATIONAL"
    TEMPORAL = "TEMPORAL"
    GLOBAL_TOPIC = "GLOBAL_TOPIC"
    MULTI_HOP = "MULTI_HOP"


PLANNER_VERSION = "qsp-v2"


@dataclass(frozen=True, slots=True)
class QueryStrategyPlan:
    planner_version: str
    primary_strategy: RetrievalMode
    modifiers: tuple[RetrievalMode, ...]
    requested_strategy: RetrievalMode | None
    reason_codes: tuple[str, ...]
    multi_hop_eligible: bool
    retrieval_plan: RetrievalPlan = field(
        default_factory=lambda: build_retrieval_plan("")
    )

    def as_trace(self) -> dict[str, object]:
        return {
            "planner_version": self.planner_version,
            "requested_strategy": self.requested_strategy.value if self.requested_strategy else "AUTO",
            "effective_strategy": self.primary_strategy.value,
            "modifiers": [mode.value for mode in self.modifiers],
            "reason_codes": list(self.reason_codes),
            "multi_hop_eligible": self.multi_hop_eligible,
            "retrieval_plan": self.retrieval_plan.as_trace(),
        }


def _normalize_mode(value: RetrievalMode | str | None) -> RetrievalMode | None:
    if value is None:
        return None
    if isinstance(value, RetrievalMode):
        return value
    try:
        return RetrievalMode(str(value).upper())
    except ValueError as error:
        raise ValueError("Unsupported retrieval mode") from error


def _route_field(route_decision: Any, name: str, default: Any = None) -> Any:
    if route_decision is None:
        return default
    if isinstance(route_decision, Mapping):
        return route_decision.get(name, default)
    return getattr(route_decision, name, default)


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def plan_query(
    query: str,
    *,
    requested_mode: RetrievalMode | str | None = None,
    features: QueryFeatures | None = None,
    requested_top_k: int | None = None,
    answer_shape: str | None = None,
    order_mode: str | None = None,
    explicit_count: int | None = None,
    target_unit: str | None = None,
    is_catalog_query: bool | None = None,
    retrieval_mode_hint: str | None = None,
    subqueries: tuple[str, ...] | list[str] = (),
    temporal_mode: str | None = None,
    entities: tuple[str, ...] | list[str] = (),
    route_decision: Mapping[str, Any] | Any | None = None,
    constraints: tuple[FieldPredicate, ...] = (),
    as_of: datetime | None = None,
    reference_time: datetime | None = None,
) -> QueryStrategyPlan:
    """Select a stable primary mode and compile the 7-axis RetrievalPlan."""
    if route_decision is not None:
        if answer_shape is None:
            answer_shape = _route_field(route_decision, "answer_shape")
        if order_mode is None:
            order_mode = _route_field(route_decision, "order_mode")
        if explicit_count is None:
            explicit_count = _route_field(route_decision, "explicit_count")
        if target_unit is None:
            target_unit = _route_field(route_decision, "target_unit")
        if is_catalog_query is None:
            raw_cat = _route_field(route_decision, "is_catalog_query")
            is_catalog_query = bool(raw_cat) if raw_cat is not None else None
        if retrieval_mode_hint is None:
            retrieval_mode_hint = _route_field(route_decision, "retrieval_mode_hint")
        if not subqueries:
            subqueries = tuple(_route_field(route_decision, "subqueries") or ())
        if temporal_mode is None:
            temporal_mode = _route_field(route_decision, "temporal_mode")
        if not entities:
            entities = tuple(_route_field(route_decision, "entities") or ())

    hint_norm = (str(retrieval_mode_hint or "").strip().lower())
    effective_features = features or extract_query_features(
        query,
        subqueries=subqueries,
        multi_hop_hint=(hint_norm in {"multi_hop", "multi"}),
    )
    requested = _normalize_mode(requested_mode)
    reasons: list[str] = []
    modifiers: list[RetrievalMode] = []
    if requested is not None:
        primary = requested
        reasons.append("EXPLICIT_MODE_OVERRIDE")
        if requested == RetrievalMode.MULTI_HOP:
            reasons.append("MULTI_HOP_EXPLICITLY_REQUESTED")
    elif effective_features.exact_terms or effective_features.identifier_terms or effective_features.path_terms:
        primary = RetrievalMode.EXACT
        if effective_features.exact_terms:
            reasons.append("EXACT_PHRASE_PRESENT")
        if effective_features.identifier_terms:
            reasons.append("EXACT_IDENTIFIER_PRESENT")
        if effective_features.path_terms:
            reasons.append("EXACT_PATH_PRESENT")
    elif hint_norm == "exact":
        primary = RetrievalMode.EXACT
        reasons.append("ROUTER_MODE_EXACT")
    elif hint_norm == "entity_relational":
        primary = RetrievalMode.ENTITY_RELATIONAL
        reasons.append("ROUTER_MODE_ENTITY_RELATIONAL")
    elif hint_norm == "global_topic":
        primary = RetrievalMode.GLOBAL_TOPIC
        reasons.append("ROUTER_MODE_GLOBAL_TOPIC")
    elif hint_norm == "temporal":
        primary = RetrievalMode.TEMPORAL
        reasons.append("ROUTER_MODE_TEMPORAL")
    elif hint_norm in {"multi_hop", "multi"}:
        primary = RetrievalMode.MULTI_HOP
        reasons.append("ROUTER_MODE_MULTI_HOP")
    elif effective_features.temporal_cues:
        primary = RetrievalMode.TEMPORAL
        reasons.append("TEMPORAL_CUE_PRESENT")
    else:
        primary = RetrievalMode.LOCAL_FACTUAL
        reasons.append("DEFAULT_LOCAL_FACTUAL")

    if effective_features.temporal_cues and primary != RetrievalMode.TEMPORAL:
        modifiers.append(RetrievalMode.TEMPORAL)
        reasons.append("TEMPORAL_MODIFIER_PRESENT")

    router_clauses = tuple(str(s).strip() for s in subqueries if str(s).strip())
    effective_clauses = router_clauses if len(router_clauses) >= 2 else effective_features.clauses
    multi_hop_eligible = (
        requested == RetrievalMode.MULTI_HOP
        or primary == RetrievalMode.MULTI_HOP
        or effective_features.multi_hop
        or len(router_clauses) >= 2
    )
    if multi_hop_eligible:
        reasons.append("MULTI_HOP_CANDIDATE")

    clean_temporal = (str(temporal_mode or "")).strip().lower() or "atemporal"
    resolved_as_of: datetime | None = None
    if as_of is not None:
        resolved_as_of = _coerce_utc_datetime(as_of)
        if resolved_as_of is not None:
            reasons.append("EXPLICIT_AS_OF")
            if clean_temporal != "change_lineage":
                clean_temporal = "past_point"
    elif clean_temporal == "past_point":
        route_as_of = _coerce_utc_datetime(_route_field(route_decision, "as_of"))
        if route_as_of is not None:
            resolved_as_of = route_as_of
        else:
            resolved_as_of = extract_as_of_datetime(query, reference_time=reference_time)
            if resolved_as_of is None and route_decision is not None:
                route_query = _route_field(route_decision, "query")
                if isinstance(route_query, str) and route_query.strip() and route_query != query:
                    resolved_as_of = extract_as_of_datetime(
                        route_query,
                        reference_time=reference_time,
                    )
        if resolved_as_of is not None:
            reasons.append("PARSED_PAST_POINT_AS_OF")
        else:
            reasons.append("PAST_POINT_UNPARSED_FALLBACK")

    exact_lookup = bool(
        effective_features.exact_terms
        or effective_features.identifier_terms
        or effective_features.path_terms
        or primary == RetrievalMode.EXACT
    )
    retrieval_plan = build_retrieval_plan(
        query,
        requested_top_k=requested_top_k,
        answer_shape=str(answer_shape) if answer_shape else None,
        order_mode=str(order_mode) if order_mode else None,
        explicit_count=explicit_count,
        target_unit=str(target_unit) if target_unit else None,
        is_catalog_query=is_catalog_query,
        strategy_mode=primary.value,
        clauses=effective_clauses,
        multi_hop=multi_hop_eligible,
        exact_lookup=exact_lookup,
        constraints=constraints,
        temporal_mode=clean_temporal,
        entities=tuple(str(e).strip() for e in entities if str(e).strip()),
        as_of=resolved_as_of,
    )
    if retrieval_plan.target.value == "DOCUMENT":
        reasons.append(f"TARGET_DOCUMENT_{retrieval_plan.output.value}")
    if retrieval_plan.cardinality.count_source.value != "DEFAULT":
        reasons.append(f"CARDINALITY_{retrieval_plan.cardinality.count_source.value}")

    return QueryStrategyPlan(
        planner_version=PLANNER_VERSION,
        primary_strategy=primary,
        modifiers=tuple(modifiers),
        requested_strategy=requested,
        reason_codes=tuple(dict.fromkeys(reasons)),
        multi_hop_eligible=multi_hop_eligible,
        retrieval_plan=retrieval_plan,
    )


def promote_multi_hop(
    plan: QueryStrategyPlan,
    *,
    coverage_reason: str | None,
) -> QueryStrategyPlan:
    """Escalate only after a structural coverage check supplies a reason."""
    if not plan.multi_hop_eligible or not coverage_reason:
        return plan
    updated_retrieval_plan = plan.retrieval_plan
    if updated_retrieval_plan.decomposition.mode == DecompositionMode.NONE:
        updated_retrieval_plan = replace(
            updated_retrieval_plan,
            decomposition=DecompositionSpec(
                mode=DecompositionMode.SEQUENTIAL,
                subqueries=updated_retrieval_plan.decomposition.subqueries,
            ),
        )
    return QueryStrategyPlan(
        planner_version=plan.planner_version,
        primary_strategy=RetrievalMode.MULTI_HOP,
        modifiers=plan.modifiers,
        requested_strategy=plan.requested_strategy,
        reason_codes=tuple(dict.fromkeys((*plan.reason_codes, "MULTI_HOP_COVERAGE_ESCALATION", coverage_reason))),
        multi_hop_eligible=True,
        retrieval_plan=updated_retrieval_plan,
    )
