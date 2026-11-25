"""TDD regression tests for P2 Step 5: RetrievalPlan, query_analysis, and S4-11 preservation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sag_api.sag import RetrievedSection
from sag_api.services import query_analysis as qa
from sag_api.services import retrieval_plan as rp
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision
from sag_api.services.query_strategy_planner import RetrievalMode, plan_query
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool


def test_query_analysis_preserves_vietnamese_content_and_removes_semantic_regexes() -> None:
    for removed in (
        "_QUERY_NOISE",
        "_QUERY_NOISE_RE",
        "_remove_query_noise",
        "_QUERY_INSTRUCTION_TERMS",
        "_TEMPORAL_CUES",
        "_RELATION_CUES",
        "_RELATION_STEM_GROUPS",
        "_GLOBAL_CUES",
    ):
        assert not hasattr(qa, removed), f"Legacy semantic list/regex {removed} must be removed from query_analysis"

    for removed_rp in (
        "_DOC_UNIT_WORDS",
        "_TOPIC_UNIT_WORDS",
        "_PASSAGE_UNIT_WORDS",
        "_EXPLICIT_DOC_COUNT_RE",
        "_EXPLICIT_TOPIC_COUNT_RE",
        "_EXPLICIT_PASSAGE_COUNT_RE",
        "_DOC_COUNT_QUESTION_RE",
        "_DOC_ALL_RE",
        "_DOC_LIST_RE",
        "_DOC_OPEN_RE",
        "_DOC_SUMMARIZE_RE",
        "_COMPARE_RE",
        "_TIME_DESC_RE",
        "_TIME_ASC_RE",
        "_CATALOG_META_TOKENS",
        "_SOURCE_NAME_ALIASES",
        "_TOPIC_FILTER_CUES_RE",
        "_BOUND_SCOPE_NAME_RE",
        "extract_query_cardinality_and_shape",
        "is_unfiltered_scope_catalog_query",
    ):
        assert not hasattr(rp, removed_rp), (
            f"Legacy semantic regex/helper {removed_rp} must be removed from retrieval_plan"
        )

    assert not hasattr(surs, "_CONFLICT_LINEAGE_CUE_RE")

    # Vietnamese words previously stripped by _QUERY_NOISE must remain intact in normalized_phrase
    analysis = qa.analyze_query("Nội dung mới nhất về chính sách là gì?")
    for expected_sub in ("nội", "dung", "mới", "nhất", "về", "là", "gì"):
        assert expected_sub in analysis.normalized_phrase


def test_plan_query_wires_unified_route_decision_and_rebuild_preserves_all_axes_s4_11() -> None:
    route_decision = UnifiedRouteDecision(
        query="Liệt kê các tài liệu mới nhất trong dự án",
        coarse_intent="KNOWLEDGE",
        question_scope="project",
        memory_scope="document_kb",
        temporal_mode="current_effective",
        answer_shape="document_list",
        intent_flags=IntentFlags(needs_retrieval=True),
        domain="technical",
        confidence=0.95,
        order_mode="time_desc",
        explicit_count=None,
        target_unit="document",
        is_catalog_query=True,
        retrieval_mode_hint="global_topic",
        subqueries=("tài liệu kiến trúc", "tài liệu bảo mật"),
    )
    strat = plan_query(
        route_decision.query,
        route_decision=route_decision,
    )
    plan = strat.retrieval_plan
    assert strat.primary_strategy == RetrievalMode.GLOBAL_TOPIC
    assert plan.target == rp.TargetUnit.DOCUMENT
    assert plan.output == rp.OutputMode.LIST
    assert plan.order == rp.OrderMode.TIME_DESC
    assert plan.answer_shape == rp.AnswerShape.DOCUMENT_LIST
    assert plan.is_unfiltered_catalog is True
    assert plan.cardinality.effective_n == 50
    assert plan.decomposition.mode == rp.DecompositionMode.PARALLEL
    assert plan.decomposition.subqueries == ("tài liệu kiến trúc", "tài liệu bảo mật")

    # Build a plan with cursor and explicit shape, then rebuild via S4-11 helper
    with_cursor = rp.build_retrieval_plan(
        route_decision.query,
        answer_shape="document_list",
        order_mode="time_desc",
        target_unit="document",
        is_catalog_query=True,
        cursor="cursor-opaque-token",
    )
    rebuilt = rp.rebuild_retrieval_plan_with_top_k(with_cursor, 12)
    assert rebuilt.target == rp.TargetUnit.DOCUMENT
    assert rebuilt.output == rp.OutputMode.LIST
    assert rebuilt.order == rp.OrderMode.TIME_DESC
    assert rebuilt.answer_shape == rp.AnswerShape.DOCUMENT_LIST
    assert rebuilt.is_unfiltered_catalog is True
    assert rebuilt.cardinality.requested_n == 12
    assert rebuilt.cardinality.effective_n == 12
    assert rebuilt.cardinality.count_source == rp.CountSource.API
    assert rebuilt.cardinality.cursor == "cursor-opaque-token"


@pytest.mark.asyncio
async def test_search_unit_and_builtin_tool_preserve_plan_axes_with_top_k_override(monkeypatch) -> None:
    captured_plans: list[rp.RetrievalPlan] = []

    async def _fake_inner(_em, _sources, _query, *, query_strategy_plan=None, **_kwargs):
        if query_strategy_plan is not None:
            captured_plans.append(query_strategy_plan.retrieval_plan)
        sec = RetrievedSection(
            chunk_id="su-1",
            search_unit_id="su-1",
            source_config_id="cfg-1",
            document_id="doc-1",
            document_version_id="ver-1",
            document_name="01_SPEC.md",
            version_no=1,
            heading="Overview",
            section_path="Overview",
            content="Verified spec excerpt.",
            score=0.91,
            rank=0,
            page_from=1,
            page_to=1,
            block_from_id="b-1",
            block_to_id="b-1",
            anchor="h1-0",
            canonical_evidence_verified=True,
        )
        return SimpleNamespace(query=_query, sections=[sec], stats={"ready_versions": 1, "candidates": 1})

    monkeypatch.setattr(surs, "_retrieve_search_unit_sections", _fake_inner)

    base_strat = plan_query(
        "Liệt kê các tài liệu mới nhất",
        answer_shape="document_list",
        order_mode="time_desc",
        is_catalog_query=True,
    )
    await surs.retrieve_search_unit_sections(
        SimpleNamespace(),
        [SimpleNamespace(id="s-1", name="Spec", sag_source_config_id="cfg-1")],
        "Liệt kê các tài liệu mới nhất",
        principal=None,
        top_k=15,
        query_strategy_plan=base_strat,
    )
    assert len(captured_plans) == 1
    assert captured_plans[0].answer_shape == rp.AnswerShape.DOCUMENT_LIST
    assert captured_plans[0].order == rp.OrderMode.TIME_DESC
    assert captured_plans[0].target == rp.TargetUnit.DOCUMENT
    assert captured_plans[0].output == rp.OutputMode.LIST
    assert captured_plans[0].is_unfiltered_catalog is True
    assert captured_plans[0].cardinality.effective_n == 15

    # Now verify SearchContextTool forwards ctx.route_decision and preserves is_unfiltered_catalog & min_catalog_k=50
    captured_plans.clear()
    monkeypatch.setattr(surs, "_retrieve_search_unit_sections", _fake_inner)
    ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[SimpleNamespace(id="s-1", name="Spec", sag_source_config_id="cfg-1")],
        principal=None,
        persona={},
        citation_offset=0,
        evidence_token_budget=8000,
        route_decision={
            "answer_shape": "document_list",
            "order_mode": "time_desc",
            "target_unit": "document",
            "is_catalog_query": True,
            "retrieval_mode_hint": "auto",
            "subqueries": [],
        },
    )
    await SearchContextTool().invoke({"query": "Danh sách tất cả tài liệu", "top_k": 5}, ctx)
    assert len(captured_plans) == 1
    assert captured_plans[0].target == rp.TargetUnit.DOCUMENT
    assert captured_plans[0].output == rp.OutputMode.LIST
    assert captured_plans[0].is_unfiltered_catalog is True
    assert captured_plans[0].cardinality.effective_n == 50
