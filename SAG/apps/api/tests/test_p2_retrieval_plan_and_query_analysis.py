"""TDD regression tests for P2 Step 5: RetrievalPlan, query_analysis, and S4-11 preservation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sag_api.sag import RetrievedSection
from sag_api.services import canonical_service  # noqa: F401
from sag_api.services import query_analysis as qa
from sag_api.services import retrieval_plan as rp
from sag_api.services import retrieval_service as rs
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision
from sag_api.services.query_strategy_planner import RetrievalMode, plan_query
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool, WebSearchTool


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
        "_SALIENT_PROPER_TERM_RE",
    ):
        assert not hasattr(qa, removed), f"Legacy semantic list/regex {removed} must be removed from query_analysis"

    from sag_api.parsing import canonical
    from sag_api.services.session_context import extractor as sc_extractor

    for removed_dead in (
        "_CONTINUOUS_SCRIPT_CHAR_RE",
        "_MULTILINGUAL_SENTENCE_SPLIT_RE",
        "_MULTILINGUAL_CLAUSE_SPLIT_RE",
    ):
        assert not hasattr(canonical, removed_dead), f"Dead unused regex {removed_dead} must be removed from canonical"
    assert not hasattr(sc_extractor, "_SENTENCE_SPLIT_RE"), "Dead _SENTENCE_SPLIT_RE must be removed from extractor"

    from sag_api.tools import builtin as builtin_tools

    assert not hasattr(builtin_tools, "_RECENT_QUERY_MARKERS"), (
        "_RECENT_QUERY_MARKERS keyword heuristic must be removed from tools/builtin.py"
    )

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


def test_decompose_query_clauses_structural_only_and_subqueries_fail_closed() -> None:
    # EN/VI conjunctions must NOT split via semantic regex — fail closed to single query when subqueries unavailable
    en_q = "Why was Northpeak's invoice disputed, and how was the billing calculation corrected?"
    vi_q = "Sự khác nhau giữa quy trình cấp 1 và khẩn cấp là gì, và khi nào cần áp dụng?"
    assert qa.decompose_query_clauses(en_q) == (en_q,)
    assert qa.decompose_query_clauses(vi_q) == (vi_q,)
    assert qa.decompose_query_clauses(en_q, subqueries=()) == (en_q,)
    assert qa.decompose_query_clauses(en_q, subqueries=("only one",)) == (en_q,)

    # Structural punctuation (? / ;) still splits deterministically
    assert qa.decompose_query_clauses("What is retention? What port is used?") == (
        "What is retention",
        "What port is used",
    )
    assert qa.decompose_query_clauses("Retention policy; Port configuration") == (
        "Retention policy",
        "Port configuration",
    )

    # Router subqueries (>= 2) override and propagate through analyze_query & plan_query
    routed_subs = ("Why was Northpeak's invoice disputed?", "How was the billing calculation corrected?")
    assert qa.decompose_query_clauses(en_q, subqueries=routed_subs) == routed_subs

    degraded_analysis = qa.analyze_query(en_q, subqueries=(), multi_hop_hint=False)
    assert degraded_analysis.features.clauses == ()
    assert degraded_analysis.features.multi_hop is False

    routed_analysis = qa.analyze_query(en_q, subqueries=routed_subs, multi_hop_hint=True)
    assert routed_analysis.features.clauses == routed_subs
    assert routed_analysis.features.multi_hop is True

    strat = plan_query(
        en_q,
        route_decision=UnifiedRouteDecision(
            query=en_q,
            coarse_intent="KNOWLEDGE",
            question_scope="project",
            memory_scope="project_documents",
            temporal_mode="change_lineage",
            answer_shape="fact_answer",
            intent_flags=IntentFlags(needs_retrieval=True),
            domain="business_hr",
            confidence=0.9,
            retrieval_mode_hint="multi_hop",
            subqueries=routed_subs,
            entities=("Northpeak", "Đà Nẵng"),
        ),
    )
    assert strat.multi_hop_eligible is True
    assert strat.retrieval_plan.decomposition.subqueries == routed_subs
    assert strat.retrieval_plan.temporal_mode == "change_lineage"
    assert strat.retrieval_plan.entities == ("Northpeak", "Đà Nẵng")


def test_salient_query_terms_unicode_and_entities_and_artifact_prefixes() -> None:
    salient = qa.extract_salient_query_terms(
        "What caused the MedThink latency spike on GPU nodes using HTTP2 near Đà Nẵng?",
        entities=("PostgreSQL", "Đà Nẵng"),
    )
    for expected in ("medthink", "gpu", "http2", "đànẵng", "nẵng", "postgresql"):
        assert expected in salient

    # Vietnamese accented time/structural units must not be treated as artifact codes
    assert surs._extract_artifact_codes("Ngày 15 tháng 10 năm 2026 tại điều 15 mục 12 bước 03") == set()
    assert surs._extract_artifact_codes("Fixed in PR-24108 and INC-9821") == {"pr 24108", "inc 9821"}


@pytest.mark.asyncio
async def test_propagation_of_subqueries_multi_hop_temporal_and_web_time_range(monkeypatch) -> None:
    # 1. rerank_sections respects temporal_mode for superseded and accepts subqueries/entities
    sup_sec = RetrievedSection(
        chunk_id="sup-1",
        document_id="d1",
        heading="Old Spec",
        content="Retention is 30 days.",
        score=0.95,
        raw_dense_score=0.95,
        is_superseded=True,
    )
    assert rs.rerank_sections("previous superseded retention", [sup_sec], limit=5).sections == []
    assert (
        len(
            rs.rerank_sections(
                "retention history",
                [sup_sec],
                limit=5,
                temporal_mode="change_lineage",
            ).sections
        )
        == 1
    )

    # 2. select_diverse_sections honors explicit multi_hop=True even without conjunction regex
    docs = [
        RetrievedSection(
            chunk_id=f"d{i}",
            document_id=f"doc-{i}",
            document_name=f"Doc{i}.md",
            heading="Info",
            content=f"Unique section content number {i} for query topic.",
            score=0.90 - 0.12 * i,
            rank=i,
        )
        for i in range(3)
    ]
    selected_sh, _ = rs.select_diverse_sections("topic overview", docs, limit=3, multi_hop=False)
    selected_mh, _ = rs.select_diverse_sections("topic overview", docs, limit=3, multi_hop=True)
    assert len(selected_mh) >= len(selected_sh)

    # 3. WebSearchTool derives time_range from structured ctx.route_decision instead of _RECENT_QUERY_MARKERS
    import httpx

    from sag_api.core.config import settings

    captured_payloads: list[dict] = []

    class _FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            captured_payloads.append(_kwargs["json"])
            return httpx.Response(
                200,
                request=httpx.Request("POST", _url),
                json={"results": [{"title": "T", "url": "https://ex.com", "content": "ok"}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(settings, "llm_api_key", "k-test")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.302.ai/v1")

    ctx_recent = ToolContext(
        engine_manager=SimpleNamespace(),
        route_decision={"temporal_mode": "current_effective", "intent_flags": {"needs_time": False}},
    )
    await WebSearchTool().invoke({"query": "hôm nay thời tiết thế nào"}, ctx_recent)
    assert captured_payloads[-1].get("time_range") == "week"

    ctx_atemporal = ToolContext(
        engine_manager=SimpleNamespace(),
        route_decision={"temporal_mode": "atemporal", "intent_flags": {"needs_time": False}},
    )
    await WebSearchTool().invoke({"query": "today weather without flag"}, ctx_atemporal)
    assert "time_range" not in captured_payloads[-1]


@pytest.mark.asyncio
async def test_search_route_and_legacy_retrieve_propagate_subqueries_and_temporal(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from sag_api.api.v1 import search as search_api

    routed_subs = ("What caused the outage?", "How was it mitigated?")
    fake_decision = UnifiedRouteDecision(
        query="What caused the outage, and how was it mitigated?",
        coarse_intent="KNOWLEDGE",
        question_scope="project",
        memory_scope="project_documents",
        temporal_mode="change_lineage",
        answer_shape="fact_answer",
        intent_flags=IntentFlags(needs_retrieval=True),
        domain="technical",
        confidence=0.92,
        retrieval_mode_hint="multi_hop",
        subqueries=routed_subs,
        entities=("OutageService",),
    )
    monkeypatch.setattr(search_api, "route_query_async", AsyncMock(return_value=fake_decision))

    route_plan = await search_api._build_query_route(
        fake_decision.query,
        ["src-1"],
        None,
        trace_strategy_plan=True,
    )
    assert route_plan.trace["query_analysis"]["features"]["clauses"] == list(routed_subs)
    assert route_plan.trace["query_analysis"]["features"]["multi_hop"] is True
    assert "MULTI_HOP_CUE" in route_plan.trace["reason_codes"]

    sup_sec = RetrievedSection(
        chunk_id="c-sup",
        document_id="d1",
        heading="History",
        content="OutageService v1 was replaced after the incident.",
        score=0.92,
        raw_dense_score=0.92,
        source_id="src-1",
        source_config_id="cfg-1",
        is_superseded=True,
    )
    engine = SimpleNamespace(
        search_many=AsyncMock(return_value=SimpleNamespace(query=fake_decision.query, sections=[sup_sec], stats={})),
        grep_chunks=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        rs,
        "_hidden_document_derivatives",
        AsyncMock(return_value=SimpleNamespace(source_ids=set(), source_config_ids=set(), source_keys=set())),
    )

    outcome = await rs.retrieve_relevant_sections(
        engine,
        [SimpleNamespace(id="src-1", sag_source_config_id="cfg-1")],
        fake_decision.query,
        query_strategy_plan=route_plan.strategy_plan,
    )
    assert [s.chunk_id for s in outcome.sections] == ["c-sup"]
    assert outcome.stats["retrieval_plan"]["decomposition"]["subqueries"] == list(routed_subs)
