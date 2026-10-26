"""Hierarchy is a non-generative structural index, not answer-quality evaluation."""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_session_context_reader import authority, rows, work

from sag_api.services.session_context.budget import BudgetExceeded
from sag_api.services.session_context.hierarchy import build_hierarchy
from sag_api.services.session_context.reader import FixtureTraceStore, read_history
from sag_api.services.session_context.types import ContextContractError, TraceMessage
from scripts.eval_session_context import expand_case


def build(messages, *, page_size=3, budget=None, **parameters):
    budget = budget or work()
    scope = authority()
    store = FixtureTraceStore(messages, scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=page_size, budget=budget, now=100)
    return build_hierarchy(history, scope=scope, budget=budget, window_turns=parameters.get("window_turns", 3),
                           episode_windows=2, local_radius=1, gap_seconds=parameters.get("gap_seconds", 600), now=100)


def test_every_message_is_a_turn_and_parents_resolve_to_original_unicode_spans():
    original = rows(431)
    hierarchy = build(original)
    turns = [u for u in hierarchy.units if u.kind == "turn"]
    assert len(turns) == 431
    assert {u.kind for u in hierarchy.units} == {"turn", "window", "episode", "local"}
    raw = {m.message_id: m for m in original}
    for unit in hierarchy.units:
        spans = hierarchy.resolve_spans(unit.unit_id, budget=work())
        assert spans
        for span in spans:
            item = raw[span.ref.message_id]
            assert span.ref == item.reference
            assert item.content[span.start:span.end] == item.content
    assert not hierarchy.coverage.visited_ids


def test_ids_structure_and_snapshot_are_independent_of_page_size_and_input_order():
    original = rows(29)
    assert build(original, page_size=1) == build(tuple(reversed(original)), page_size=17)


def test_neighbors_windows_and_episodes_never_bridge_excluded_messages_or_time_gaps():
    original = list(rows(7))
    original[2] = replace(original[2], provenance="unknown")
    original[5:] = [replace(m, created_at=m.created_at + timedelta(hours=2)) for m in original[5:]]
    hierarchy = build(tuple(original))
    allowed_segments = [{original[i].message_id for i in segment} for segment in ([0, 1], [3, 4], [5, 6])]
    for unit in hierarchy.units:
        ids = {s.ref.message_id for s in hierarchy.resolve_spans(unit.unit_id, budget=work())}
        assert any(ids <= segment for segment in allowed_segments)


def test_gap_boundary_is_explicit_and_does_not_claim_semantic_episode_segmentation():
    original = rows(2)
    assert len([u for u in build(original, gap_seconds=60).units if u.kind == "window"]) == 1
    assert len([u for u in build(original, gap_seconds=59).units if u.kind == "window"]) == 2


def test_empty_content_is_covered_without_invalid_zero_width_evidence():
    original = (replace(rows(1)[0], content=""),)
    hierarchy = build(original)
    assert hierarchy.coverage.eligible_ids == frozenset({original[0].message_id})
    assert hierarchy.resolve_spans(hierarchy.units[0].unit_id, budget=work()) == ()
    assert build(()).units == ()


def test_multiple_empty_messages_have_distinct_turn_identity_and_parent_links():
    original = tuple(replace(m, content="") for m in rows(3))
    hierarchy = build(original)
    turns = [u for u in hierarchy.units if u.kind == "turn"]
    assert len({u.unit_id for u in turns}) == 3
    assert len({u.unit_id for u in hierarchy.units}) == len(hierarchy.units)
    assert {u.origin.message_id for u in turns} == {m.message_id for m in original}


def test_local_units_with_same_neighbors_keep_distinct_center_turns():
    hierarchy = build(rows(2))
    local = [u for u in hierarchy.units if u.kind == "local"]
    assert local[0].children == local[1].children
    assert local[0].anchor_id != local[1].anchor_id
    assert local[0].unit_id != local[1].unit_id


def test_scope_and_expiry_are_rechecked_before_hierarchy_build():
    budget = work()
    scope = authority()
    store = FixtureTraceStore(rows(2), scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=1, budget=budget, now=100)
    for current, instant in ((replace(scope, source_ids=frozenset()), 100), (scope, 2000)):
        with pytest.raises(ContextContractError):
            build_hierarchy(history, scope=current, budget=budget, window_turns=3,
                            episode_windows=2, local_radius=1, gap_seconds=600, now=instant)


def test_truncated_history_cannot_preserve_complete_coverage_denominator():
    budget = work()
    scope = authority()
    store = FixtureTraceStore(rows(3), scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=1, budget=budget, now=100)
    truncated = replace(history, messages=history.messages[:1], ordinals=history.ordinals[:1])
    with pytest.raises(ContextContractError, match="coverage"):
        build_hierarchy(truncated, scope=scope, budget=budget, window_turns=3,
                        episode_windows=2, local_radius=1, gap_seconds=600, now=100)


def test_budget_limits_node_and_edge_construction_and_span_resolution():
    budget = work()
    scope = authority()
    store = FixtureTraceStore(rows(10), scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=3, budget=budget, now=100)
    with pytest.raises(BudgetExceeded):
        build_hierarchy(history, scope=scope, budget=work(1), window_turns=3,
                        episode_windows=2, local_radius=1, gap_seconds=600, now=100)
    hierarchy = build(rows(10))
    with pytest.raises(BudgetExceeded):
        hierarchy.resolve_spans(hierarchy.units[-1].unit_id, budget=work(1))


@pytest.mark.parametrize("parameters", [{"window_turns": 0}, {"window_turns": True},
                                        {"gap_seconds": float("inf")}, {"gap_seconds": -1}])
def test_invalid_parameters_are_rejected(parameters):
    with pytest.raises(ContextContractError):
        build(rows(2), **parameters)


def test_calibration_corpus_keeps_every_original_message_and_gold_quote_resolvable():
    corpus = Path(__file__).parent / "fixtures/session_context.jsonl"
    for line in corpus.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["split"] != "calibration":
            continue
        expanded = expand_case(case)
        scope = authority()
        messages = tuple(TraceMessage(scope.key, m["id"], m["role"], m["content"],
                                     datetime.fromisoformat(m["created_at"]), provenance="recorded",
                                     partition_fingerprint=scope.partition_fingerprint) for m in expanded)
        hierarchy = build(messages, budget=work(1000000))
        turns = [u for u in hierarchy.units if u.kind == "turn"]
        assert len(turns) == case["message_count"]
        for item in case["gold_items"]:
            for evidence in item["evidence"]:
                spans = hierarchy.resolve_spans(turns[evidence["at"]].unit_id, budget=work())
                assert evidence["quote"] in messages[evidence["at"]].content[spans[0].start:spans[0].end]
