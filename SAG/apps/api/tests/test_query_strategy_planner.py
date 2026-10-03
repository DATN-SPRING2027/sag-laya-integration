from __future__ import annotations

import pytest

from sag_api.services.query_strategy_planner import RetrievalMode, plan_query, promote_multi_hop


@pytest.mark.parametrize(
    ("query", "mode"),
    [
        ('Where is "DATN-37" defined?', RetrievalMode.EXACT),
        ("What is the approval procedure?", RetrievalMode.LOCAL_FACTUAL),
        ("How is PostgreSQL related to Qdrant?", RetrievalMode.ENTITY_RELATIONAL),
        ("What changed after launch?", RetrievalMode.TEMPORAL),
        ("What changed in version 2.1?", RetrievalMode.TEMPORAL),
        ("Give me an overview of the architecture", RetrievalMode.GLOBAL_TOPIC),
        ("Why did the incident cause the service outage?", RetrievalMode.ENTITY_RELATIONAL),
    ],
)
def test_planner_is_deterministic_and_emits_stable_reason_codes(query, mode):
    first = plan_query(query)
    second = plan_query(query)

    assert first == second
    assert first.primary_strategy == mode
    assert first.planner_version == "qsp-v1"
    assert first.reason_codes


def test_planner_composes_temporal_modifier_with_exact_and_relational_modes():
    exact = plan_query('Where was "ERR-504" documented before 2025-01-01?')
    relation = plan_query("How did release 2.1 cause the outage?")
    temporal = plan_query("What changed after launch?")

    assert exact.primary_strategy == RetrievalMode.EXACT
    assert RetrievalMode.TEMPORAL in exact.modifiers
    assert RetrievalMode.TEMPORAL in relation.modifiers
    assert temporal.primary_strategy == RetrievalMode.TEMPORAL


def test_multi_hop_requires_coverage_escalation_with_evidence_reason():
    plan = plan_query("Why did the database outage cause retries, and how did retries affect latency?")

    assert plan.primary_strategy == RetrievalMode.ENTITY_RELATIONAL
    assert plan.multi_hop_eligible
    assert promote_multi_hop(plan, coverage_reason="missing_relation_facet").primary_strategy == RetrievalMode.MULTI_HOP
    assert promote_multi_hop(plan, coverage_reason=None) == plan


def test_explicit_mode_is_preserved_and_invalid_mode_is_rejected():
    plan = plan_query("Give me an overview", requested_mode="LOCAL_FACTUAL")

    assert plan.requested_strategy == RetrievalMode.LOCAL_FACTUAL
    assert plan.primary_strategy == RetrievalMode.LOCAL_FACTUAL
    assert "EXPLICIT_MODE_OVERRIDE" in plan.reason_codes
    with pytest.raises(ValueError):
        plan_query("query", requested_mode="arbitrary")


@pytest.mark.parametrize("mode", list(RetrievalMode))
def test_each_public_retrieval_mode_can_be_requested_explicitly(mode):
    plan = plan_query("plain query", requested_mode=mode)

    assert plan.requested_strategy == mode
    assert plan.primary_strategy == mode
    assert "EXPLICIT_MODE_OVERRIDE" in plan.reason_codes
    if mode == RetrievalMode.MULTI_HOP:
        assert plan.multi_hop_eligible
        assert "MULTI_HOP_EXPLICITLY_REQUESTED" in plan.reason_codes
