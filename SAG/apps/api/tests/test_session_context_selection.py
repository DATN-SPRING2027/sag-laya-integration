"""Selection/closure invariants; these assertions are not semantic answer accuracy."""

import json
import os
import time
from dataclasses import replace

import pytest
from test_session_context_graph import feature, fixture
from test_session_context_reader import authority, rows, work

from sag_api.services.session_context.budget import BudgetExceeded, BudgetLimits, BudgetState
from sag_api.services.session_context.graph import LocalFeatureEncoder, build_graph
from sag_api.services.session_context.hierarchy import build_hierarchy
from sag_api.services.session_context.reader import FixtureTraceStore, read_history
from sag_api.services.session_context.scope import make_snapshot, validate_selection
from sag_api.services.session_context.selection import SelectionCaps, fuse_scores, select_context
from sag_api.services.session_context.types import ContextContractError


def caps(**changes):
    return replace(SelectionCaps(1, 1, 1, 20, 10000, 2, 20, 0.6, 0.6), **changes)


def choose(graph, budget, query, features=None, **changes):
    hierarchy = build_hierarchy(graph.history, scope=authority(), budget=budget, window_turns=2,
                                episode_windows=2, local_radius=1, gap_seconds=600, now=100)
    return select_context(graph, hierarchy, query, features or feature(query), scope=authority(),
                          budget=budget, caps=caps(**changes), now=100)


def test_fusion_normalizes_each_view_handles_flat_missing_and_both_routes():
    graph, hierarchy = {"a": 100.0, "b": 50.0}, {"b": 0.2, "c": 0.2}
    relational = fuse_scores(graph, hierarchy, route="relational", rho=0.6)
    local = fuse_scores(graph, hierarchy, route="local", rho=0.6)
    assert relational == {"a": 0.6, "b": 0.4, "c": 0.4}
    assert local == {"a": 0.4, "b": 0.6, "c": 0.6}
    assert fuse_scores({"a": 0.0}, {}, route="local", rho=0.6) == {}


def test_bridge_and_local_closure_add_evidence_and_deduplicate_raw_messages():
    messages, graph, budget = fixture(["Alpha owns Beta.", "Beta owns Gamma.", "Gamma uses PIN-472."],
                                      entities=[("Alpha", "Beta"), ("Beta", "Gamma"), ("Gamma",)])
    result = choose(graph, budget, "Alpha?", feature("Alpha?", entities=("Alpha",)))
    ids = [span.ref.message_id for span in result.selection.spans]
    assert "m00000" in ids and "m00001" in ids
    assert len(ids) == len(set(ids))
    assert result.route == "relational"
    records = {m.message_id: m for m in messages}
    assert validate_selection(result.selection, scope=authority(), current_snapshot=make_snapshot(
        authority(), records, generation="g1"), records=records, now=100)


def test_entity_free_follow_up_uses_both_views_and_local_context():
    _, graph, budget = fixture(["Tôi chọn phương án B.", "Đã ghi nhận phương án đó.", "weather unrelated"],
                               vectors=[(1.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    result = choose(graph, budget, "phương án đó?", bridge_limit=0)
    assert result.route == "local"
    assert result.graph_scores and result.hierarchy_scores
    assert {span.ref.message_id for span in result.selection.spans} >= {"m00000", "m00001"}


def test_dependency_group_is_atomic_when_context_caps_cannot_fit_it():
    messages, graph, budget = fixture(["Decision B", "Proposed B"])
    assistant = replace(messages[1], role="assistant", dependencies=(messages[0].reference,))
    history = replace(graph.history, messages=(messages[0], assistant))
    graph = build_graph(history, graph.features, scope=authority(), budget=budget, gap_seconds=600, now=100)
    result = choose(graph, budget, "Proposed B", max_messages=1, bridge_limit=0, local_limit=0)
    assert not result.selection.spans
    assert result.selection.state == "partial"
    assert result.selection.reason == "selection_cap"


def test_revocation_and_scope_change_invalidate_cached_selection():
    messages, graph, budget = fixture(["Alpha"])
    result = choose(graph, budget, "Alpha")
    current = replace(authority(), source_ids=frozenset())
    records = {m.message_id: m for m in messages}
    with pytest.raises(ContextContractError):
        validate_selection(result.selection, scope=current, current_snapshot=make_snapshot(
            current, records, generation="g1"), records=records, now=100)


def test_empty_snapshot_and_unknown_legacy_do_not_invent_evidence_or_coverage():
    _, graph, budget = fixture([])
    result = choose(graph, budget, "anything")
    assert result.selection.spans == ()
    assert result.selection.coverage.visited_ids == frozenset()
    _, graph, budget = fixture(["hidden", "visible"], denied=(0,))
    result = choose(graph, budget, "visible")
    assert result.selection.state == "partial"
    assert result.selection.reason == "unknown_provenance"


def test_caps_and_work_budget_cannot_be_unlimited_or_reset_by_retrieval():
    with pytest.raises(ContextContractError):
        caps(max_codepoints=0)
    _, graph, budget = fixture(["Alpha"])
    budget.cancel()
    with pytest.raises(BudgetExceeded):
        choose(graph, budget, "Alpha")
    with pytest.raises(BudgetExceeded):
        choose(graph, work(1), "Alpha")


def test_packing_does_not_silently_clip_a_long_raw_message():
    _, graph, budget = fixture(["🙂" * 2000])
    result = choose(graph, budget, "something", max_codepoints=100, bridge_limit=0, local_limit=0)
    assert result.selection.spans == ()
    assert result.selection.state == "partial"
    assert result.selection.coverage.visited_ids == frozenset()


def test_hierarchy_snapshot_change_is_rejected_before_any_result():
    _, graph, budget = fixture(["Alpha"])
    hierarchy = build_hierarchy(graph.history, scope=authority(), budget=budget, window_turns=2,
                                episode_windows=2, local_radius=1, gap_seconds=600, now=100)
    changed = replace(hierarchy, snapshot=replace(hierarchy.snapshot, generation="new"))
    with pytest.raises(ContextContractError):
        select_context(graph, changed, "Alpha", feature("Alpha"), scope=authority(), budget=budget,
                       caps=caps(), now=100)


@pytest.mark.parametrize("changes", [{"rho": float("nan")}, {"damping": 0}, {"beam": True},
                                     {"bridge_limit": -1}, {"max_messages": 0}])
def test_all_caps_validate_finite_explicit_limits(changes):
    with pytest.raises(ContextContractError):
        caps(**changes)


def test_old_information_is_selectable_beyond_last_200_without_entity_anchor():
    _, graph, budget = fixture(["Release code is ORBIT-472."] + ["Weather update."] * 430)
    result = choose(graph, budget, "Release code?", bridge_limit=0, local_limit=0)
    assert [span.ref.message_id for span in result.selection.spans] == ["m00000"]
    assert result.selection.coverage.eligible_ids == frozenset(f"m{i:05}" for i in range(431))


def test_no_dense_or_lexical_support_does_not_create_arbitrary_evidence():
    _, graph, budget = fixture(["Alpha"], vectors=[(-1.0, 0.0)])
    result = choose(graph, budget, "irrelevant")
    assert result.selection.spans == ()
    assert result.groups == ()


@pytest.mark.ai_smoke
@pytest.mark.skipif(os.environ.get("SAG_RUN_SESSION_CONTEXT_MODEL_SMOKE") != "1", reason="explicit local model opt-in")
def test_local_learned_models_through_selection_and_t2_validation(tmp_path):
    start = time.monotonic()
    budget = BudgetState(BudgetLimits(128000, 4096, 2, 200000, 1000000, start + 240))
    scope = replace(authority(), expires_at=time.time() + 240)
    texts = ["Nguyễn Văn An phụ trách dự án Sao Mai.", "Dự án Sao Mai đặt tại Hà Nội.",
             "Mã bàn giao cho Hà Nội là HN-472.", "Alice works in London.",
             "Tin thời tiết hôm nay không liên quan đến dự án."]
    messages = tuple(replace(m, content=text) for m, text in zip(rows(5), texts, strict=True))
    store = FixtureTraceStore(messages, scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=2, budget=budget)
    encoder = LocalFeatureEncoder(chunk_codepoints=128, budget=budget)
    graph = build_graph(history, tuple(encoder.featurize(m.content, budget=budget) for m in history.messages),
                        scope=scope, budget=budget, gap_seconds=600)
    hierarchy = build_hierarchy(history, scope=scope, budget=budget, window_turns=2,
                                episode_windows=2, local_radius=1, gap_seconds=600)
    reports = []
    for question, gold in (("Nguyễn Văn An phụ trách dự án ở đâu?", {"m00000", "m00001"}),
                           ("Mã bàn giao cho Hà Nội là gì?", {"m00002"})):
        result = select_context(graph, hierarchy, question, encoder.featurize(question, budget=budget),
                                scope=scope, budget=budget, caps=caps(main_limit=2, bridge_limit=2, max_messages=5))
        records = {m.message_id: m for m in messages}
        resolved = validate_selection(result.selection, scope=scope, current_snapshot=make_snapshot(
            scope, records, generation="g1"), records=records)
        selected = {span.ref.message_id for span in result.selection.spans}
        assert gold <= selected
        reports.append({"route": result.route, "selected_ids": sorted(selected), "validated_spans": len(resolved)})
    assert len(graph.features[0].vector) == 1024
    assert any(mention.label == "PER" for mention in graph.features[0].mentions)
    assert any(mention.label == "LOC" for mention in graph.features[1].mentions)
    assert budget.calls == 0
    report = {"model_id": encoder.model_id, "dimensions": 1024, "queries": reports,
              "encoder_calls": encoder.encoder_calls, "encoder_tokens": encoder.encoder_tokens,
              "ner_calls": encoder.ner_calls, "ner_tokens": encoder.ner_tokens,
              "llm_calls": budget.calls, "work_units": budget.index_units,
              "elapsed_seconds": round(time.monotonic() - start, 3)}
    (tmp_path / "t3b-model-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
