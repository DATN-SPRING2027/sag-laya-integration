"""P6-2 Packet 1 TDD regression tests: grounding, query_analysis, json_parser, agent context, runtime, verifier."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_agent import Agent, AgentMessage, AgentRuntime, EventType, ModelChunk
from sag_api.generation.json_parser import (
    decode_json_object,
    extract_fenced_blocks_and_outside,
    strip_think_blocks,
)
from sag_api.generation.prompt import build_citations
from sag_api.sag import RetrievedSection
from sag_api.services import agent_service, evidence_service
from sag_api.services.agent_domain import AskPlan
from sag_api.services.evidence_service import (
    _sections_cover_query_anchors,
    build_evidence_pack,
    build_tool_evidence_pack,
)
from sag_api.services.query_analysis import extract_query_features
from sag_api.services.query_strategy_planner import RetrievalMode, plan_query
from sag_api.tools.registry import ToolRegistry


def _section(
    content: str,
    *,
    chunk_id: str = "c1",
    doc_name: str = "policy.pdf",
    heading: str = "Chính sách",
    doc_id: str = "d1",
    ver_id: str = "v1",
) -> RetrievedSection:
    return RetrievedSection(
        chunk_id=chunk_id,
        search_unit_id=chunk_id,
        source_id="s1",
        source_config_id="sc1",
        document_id=doc_id,
        document_version_id=ver_id,
        document_name=doc_name,
        page_from=1,
        page_to=1,
        anchor=f"p1-{chunk_id}",
        heading=heading,
        content=content,
        score=0.95,
        canonical_evidence_verified=True,
    )


def test_date_anchor_or_groups_match_either_dmy_or_iso_and_reject_wrong_date() -> None:
    """R-04: Bridged date representations (15/03/2025 OR 2025-03-15) must form ONE OR-group."""
    sec_dmy = _section("Ngày 15/03/2025, công ty ban hành quy định mới về làm việc từ xa.")
    sec_iso = _section("On 2025-03-15, the release gate was updated.")
    sec_wrong = _section("Ngày 20/04/2025, công ty ban hành quy định khác.")

    for query, matching_sec in [
        ("Chính sách ngày 15/03/2025 là gì?", sec_dmy),
        ("Chính sách ngày 15/03/2025 là gì?", sec_iso),
        ("What happened on 2025-03-15?", sec_iso),
        ("What happened on 2025-03-15?", sec_dmy),
    ]:
        groups = evidence_service._query_anchor_groups(query)
        assert len(groups) == 1
        assert set(groups[0]) >= {"20250315", "15032025"}
        assert _sections_cover_query_anchors(groups, [matching_sec]) is True

        tpack = build_tool_evidence_pack(
            [matching_sec],
            query=query,
            render=lambda xs: "\n".join(x.content for x in xs),
            context_budget_tokens=4000,
        )
        epack = build_evidence_pack(
            query,
            [matching_sec],
            context_window_tokens=16000,
            reserved_output_tokens=2000,
        )
        assert tpack.status == "sufficient", f"tool_pack failed for {query!r}: {tpack.no_answer_reason}"
        assert epack.status == "sufficient", f"search_pack failed for {query!r}: {epack.no_answer_reason}"

    # Wrong date must STILL fail weak_evidence
    tpack_wrong = build_tool_evidence_pack(
        [sec_wrong],
        query="Chính sách ngày 15/03/2025 là gì?",
        render=lambda xs: xs[0].content,
        context_budget_tokens=4000,
    )
    epack_wrong = build_evidence_pack(
        "Chính sách ngày 15/03/2025 là gì?",
        [sec_wrong],
        context_window_tokens=16000,
        reserved_output_tokens=2000,
    )
    assert tpack_wrong.status == "weak" and tpack_wrong.no_answer_reason == "weak_evidence"
    assert epack_wrong.status == "weak" and epack_wrong.no_answer_reason == "weak_evidence"

    # Two distinct dates remain ANDed across groups, while each date matches either DMY or ISO
    two_date_q = "So sánh thay đổi giữa ngày 15/03/2025 và 2025-04-20"
    assert len(evidence_service._query_anchor_groups(two_date_q)) == 2
    assert (
        build_evidence_pack(
            two_date_q,
            [sec_dmy],
            context_window_tokens=16000,
            reserved_output_tokens=2000,
        ).status
        == "weak"
    )
    assert (
        build_evidence_pack(
            two_date_q,
            [sec_dmy, sec_wrong],
            context_window_tokens=16000,
            reserved_output_tokens=2000,
        ).status
        == "sufficient"
    )


def test_query_analysis_tightens_identifier_and_path_regexes_preserving_real_ids() -> None:
    """Common prose tokens (Q1..Q4, B2B, TP.HCM, Node.js, 100.5triệu) must not hijack EXACT mode or weak_evidence."""
    biz_query = "Doanh thu Q1 và Q4 của mô hình B2B, B2C tại TP.HCM dùng Node.js đạt 100.5triệu ra sao?"
    biz_features = extract_query_features(biz_query)
    assert biz_features.identifier_terms == ()
    assert biz_features.path_terms == ()
    assert evidence_service._query_anchor_groups(biz_query) == ()
    assert plan_query(biz_query).primary_strategy != RetrievalMode.EXACT

    sec_paraphrase = _section(
        "Báo cáo quý 1 tại Thành phố Hồ Chí Minh cho khách hàng doanh nghiệp đạt 100,5 triệu đồng."
    )
    assert (
        build_evidence_pack(
            biz_query,
            [sec_paraphrase],
            context_window_tokens=16000,
            reserved_output_tokens=2000,
        ).status
        == "sufficient"
    )

    tech_query = "Kiểm tra mã INV-2025-001, ADR-004, API_KEY_V2 trong src/app.py và /etc/nginx/conf.d"
    tech_features = extract_query_features(tech_query)
    assert "INV-2025-001" in tech_features.identifier_terms
    assert "ADR-004" in tech_features.identifier_terms
    assert "API_KEY_V2" in tech_features.identifier_terms
    assert "src/app.py" in tech_features.path_terms
    assert "/etc/nginx/conf.d" in tech_features.path_terms
    assert plan_query(tech_query).primary_strategy == RetrievalMode.EXACT


def test_json_parser_preserves_in_string_think_and_rejects_unclosed_think_inline_braces() -> None:
    """In-string <think> must survive intact; truncated unclosed <think> prose must not yield garbage JSON."""
    in_str = '{"answerability":"answered","answer":"Use <think> tags [1]."}'
    assert strip_think_blocks(in_str) == in_str
    assert decode_json_object(in_str) == {
        "answerability": "answered",
        "answer": "Use <think> tags [1].",
    }

    in_str_pair = '<think>outer reasoning</think>\n{"answer":"Explain <think>demo</think> tag [1]."}'
    assert decode_json_object(in_str_pair) == {"answer": "Explain <think>demo</think> tag [1]."}

    unclosed_inline = '<think>User asked about {"foo": 1} but cut off'
    assert decode_json_object(unclosed_inline) is None

    unclosed_mid_multiline = '<think>Thinking:\n{"foo": 1}\nWait, let me reconsider...'
    assert decode_json_object(unclosed_mid_multiline) is None

    fenced_tight, _ = extract_fenced_blocks_and_outside('```json{"a":1}```')
    assert fenced_tight == ['{"a":1}']
    fenced_tab, _ = extract_fenced_blocks_and_outside('```json\t{"a":1}```')
    assert fenced_tab == ['{"a":1}']


def test_fit_agent_context_preserves_real_user_question_prevents_swiss_cheese_and_no_double_reserve() -> None:
    """R-05: finalize_on_max_turns must keep real user question; no Swiss-cheese roles; no double reserve."""
    msgs_max_turns = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "REAL USER QUESTION ABOUT BUDGET 2025"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "search_context", "arguments": '{"query":"budget"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc1", "name": "search_context", "content": "EVIDENCE " * 300},
        {"role": "user", "content": "The tool-call limit has been reached. Give the best final answer."},
    ]
    fitted, _ = agent_service._fit_agent_context(
        msgs_max_turns,
        [],
        context_window_tokens=850,
        reserved_output_tokens=200,
    )
    contents = [str(m.get("content", "")) for m in fitted]
    assert any("REAL USER QUESTION ABOUT BUDGET 2025" in c for c in contents)
    assert any("tool-call limit has been reached" in c for c in contents)

    # Also verify explicit AgentMessage metadata={"forced_final": True} works even with custom final text
    from sag_agent import ToolCall

    agent_msgs = [
        AgentMessage(role="system", content="S" * 100),
        AgentMessage(role="user", content="ORIGINAL CUSTOM QUESTION"),
        AgentMessage(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="tc-meta", name="search_context", arguments={"query": "q"}),),
        ),
        AgentMessage(role="tool", tool_call_id="tc-meta", name="search_context", content="FILLER " * 300),
        AgentMessage(role="user", content="Custom finalize prompt.", metadata={"forced_final": True}),
    ]
    fitted_meta, _ = agent_service._fit_agent_context(
        agent_msgs,
        [],
        context_window_tokens=450,
        reserved_output_tokens=100,
    )
    meta_contents = [str(m.get("content", "")) for m in fitted_meta]
    assert any("ORIGINAL CUSTOM QUESTION" in c for c in meta_contents)
    assert any("Custom finalize prompt." in c for c in meta_contents)

    # Swiss-cheese prevention: no consecutive assistant roles and no orphaned leading assistant
    msgs_swiss = [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "Turn 1 user"},
        {"role": "assistant", "content": "Turn 1 assistant"},
        {"role": "user", "content": "Turn 2 VERY BIG USER MESSAGE " + ("X " * 400)},
        {"role": "assistant", "content": "Turn 2 short assistant"},
        {"role": "user", "content": "Turn 3 latest user"},
    ]
    fitted_swiss, _ = agent_service._fit_agent_context(
        msgs_swiss,
        [],
        context_window_tokens=450,
        reserved_output_tokens=100,
    )
    roles = [m["role"] for m in fitted_swiss]
    for prev, curr in zip(roles, roles[1:], strict=False):
        assert not (prev == "assistant" and curr == "assistant"), f"Non-contiguous roles: {roles}"
    non_sys_roles = [r for r in roles if r != "system"]
    assert non_sys_roles[0] == "user", f"Orphaned assistant before first user: {roles}"

    # No double output_reserve subtraction: 32k ctx with 20k output_reserve leaves ~12k budget, NOT 0
    _, rem_32k = agent_service._fit_agent_context(
        [{"role": "system", "content": "Sys"}, {"role": "user", "content": "Q"}],
        [],
        context_window_tokens=32768,
        reserved_output_tokens=20000,
    )
    assert rem_32k > 10000


@pytest.mark.asyncio
async def test_agent_runtime_drive_handles_base_exception_without_unbound_local_or_active_leak() -> None:
    """Non-Exception BaseException must not trigger UnboundLocalError or leak run_id in runtime._active."""

    class BrokenModel:
        async def stream_turn(self, request, cancellation):
            raise BaseExceptionGroup("boom", [KeyboardInterrupt("x")])
            yield ModelChunk()  # pragma: no cover

    rt = AgentRuntime()
    await rt.start()
    handle = rt.run(Agent(name="test", model=BrokenModel()), "hi")
    await asyncio.sleep(0.05)

    assert handle._task is not None and handle._task.done()
    task_exc = handle._task.exception()
    assert isinstance(task_exc, BaseExceptionGroup)
    assert not isinstance(task_exc, UnboundLocalError)
    assert handle.done is True
    assert handle.run_id not in rt._active


@pytest.mark.asyncio
async def test_entailment_verifier_sees_full_chunk_and_doc_name_caps_size_and_logs_degraded_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verifier must see full chunk text + document_name, cap giant catalogs, and degrade visibly on error."""
    long_content = (
        "Đoạn mở đầu không chứa số liệu quan trọng. " * 35
    ) + "Hạn mức phê duyệt đặc biệt là 750 triệu VND."
    sec_long = _section(
        long_content,
        doc_name="quy_trinh_phe_duyet.pdf",
        heading="Điều 1",
    )
    built = build_citations([sec_long], {"sc1": {"id": "s1", "name": "Src"}})
    assert len(built[0]["snippet"]) <= 1205  # Public snippet stays bounded
    assert "750 triệu VND" not in built[0]["snippet"]

    seen_prompts: list[str] = []

    async def fake_complete(messages, **_kwargs):
        seen_prompts.append("\n".join(str(m.get("content") or "") for m in messages))
        return '{"answerability": "answered", "supported": true, "reason": "ok"}'

    llm_ok = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_complete))
    status, reason = await agent_service._verify_grounded_entailment(
        llm_ok,
        query="Hạn mức phê duyệt đặc biệt là bao nhiêu?",
        answer="Hạn mức phê duyệt đặc biệt là 750 triệu VND [1].",
        internal_citations=[{**built[0], "claim_level": "claim"}],
        session_citations=[],
    )
    assert (status, reason) == ("answered", None)
    assert "750 triệu VND" in seen_prompts[0]
    assert "quy_trinh_phe_duyet.pdf" in seen_prompts[0]
    assert "Điều 1" in seen_prompts[0]

    # Giant catalog cap: 40 citations with 3000-char bodies must be bounded while preserving all 40 filenames
    seen_prompts.clear()
    giant_cits = [
        {
            "n": i,
            "claim_level": "claim",
            "document_name": f"doc_{i:02d}.pdf",
            "heading": f"Section {i}",
            "snippet": "A" * 1200,
            "_verifier_content": "B" * 3000,
        }
        for i in range(1, 41)
    ]
    await agent_service._verify_grounded_entailment(
        llm_ok,
        query="Liệt kê tất cả tài liệu",
        answer="Danh sách " + " ".join(f"doc_{i:02d}.pdf [{i}]" for i in range(1, 41)),
        internal_citations=giant_cits,
        session_citations=[],
    )
    assert len(seen_prompts[0]) < 32_000
    assert "doc_01.pdf" in seen_prompts[0]
    assert "doc_40.pdf" in seen_prompts[0]

    # Invalid answerability enum must NOT be silently coerced to "answered"
    assert agent_service._parse_entailment_verdict(
        '{"answerability": "unsupported", "supported": true}'
    )[0] is None

    # SAG-CHAT-001: Timeout on lexically supported cited answer preserves answer with degraded reason;
    # non-timeout verifier error still fails closed with entailment_verifier_failed + degraded=True.
    caplog.set_level(logging.WARNING)
    timeout_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(side_effect=TimeoutError("verifier timed out")),
    )
    timeout_status, timeout_reason = await agent_service._verify_grounded_entailment(
        timeout_llm,
        query="Hạn mức?",
        answer="750 triệu VND [1].",
        internal_citations=[{**built[0], "claim_level": "claim"}],
        session_citations=[],
    )
    assert (timeout_status, timeout_reason) == ("answered", "entailment_verifier_timeout")

    failing_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(side_effect=RuntimeError("verifier upstream 500")),
    )
    err_status, err_reason = await agent_service._verify_grounded_entailment(
        failing_llm,
        query="Hạn mức?",
        answer="750 triệu VND [1].",
        internal_citations=[{**built[0], "claim_level": "claim"}],
        session_citations=[],
    )
    assert (err_status, err_reason) == ("no_answer", "entailment_verifier_failed")
    assert any("entailment" in rec.getMessage().lower() for rec in caplog.records)

    # End-to-end generate_stream must expose degraded=True & strip private _verifier_content from public citations
    @asynccontextmanager
    async def _noop_session():
        yield SimpleNamespace()

    source = SimpleNamespace(id="s1", name="Src", sag_source_config_id="sc1")
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-1"))

    class StubRuntime:
        async def start(self):
            return None

        def run(self, *_args, **_kwargs):
            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="r1",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "r1", "sequence": 1},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="r1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"output": "Hạn mức là 750 triệu VND [1]."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r1", "sequence": 2},
                )

            return _events()

    plan = AskPlan(
        query="Hạn mức?",
        user_message_id="u1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "Hạn mức?"}],
        citations=built,
        route_decision={"coarse_intent": "KNOWLEDGE", "question_scope": "project", "confidence": 0.9},
    )
    agent = SimpleNamespace(id="a1", name="Tester", persona={}, is_default=True)
    # 1) When verifier fails, RUN_COMPLETED marks degraded=True with entailment_verifier_failed
    events_fail = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session,
            plan=plan,
            agent=agent,
            thread_id="t1",
            engine_manager=SimpleNamespace(),
            llm=failing_llm,
            tool_registry=ToolRegistry(),
            runtime=StubRuntime(),
            knowledge_only=True,
        )
    ]
    completed_fail = next(e for e in events_fail if e.type == EventType.RUN_COMPLETED.value)
    assert completed_fail.data["payload"]["answer_status"] == "no_answer"
    assert completed_fail.data["payload"]["no_answer_reason"] == "entailment_verifier_failed"
    assert completed_fail.data["payload"]["degraded"] is True
    assert completed_fail.data["payload"]["degraded_reason"] == "entailment_verifier_failed"

    # 2) When verifier succeeds, public citations MUST NOT leak _verifier_content
    events_ok = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session,
            plan=plan,
            agent=agent,
            thread_id="t1",
            engine_manager=SimpleNamespace(),
            llm=llm_ok,
            tool_registry=ToolRegistry(),
            runtime=StubRuntime(),
            knowledge_only=True,
        )
    ]
    completed_ok = next(e for e in events_ok if e.type == EventType.RUN_COMPLETED.value)
    assert completed_ok.data["payload"]["answer_status"] == "answered"
    assert "_verifier_content" not in completed_ok.data["payload"]["citations"][0]


def test_citation_finalizer_preserves_zero_intervals_and_survives_stray_backtick() -> None:
    """Math intervals with 0 ([0, 1], [0, 100]) stay intact; unmatched backtick does not disable parsing."""
    cits = [
        {
            "n": 1,
            "chunk_id": "c1",
            "source_id": "s1",
            "document_id": "d1",
            "document_version_id": "v1",
            "page_from": 1,
            "page_to": 1,
            "anchor": "a1",
        }
    ]
    # Math intervals containing 0 must NOT be rewritten to [1]
    math_text, math_used = agent_service._finalize_answer_citations(
        "Giá trị x thuộc [0, 1] và y thuộc [0, 100] theo tài liệu [1].",
        cits,
    )
    assert "[0, 1]" in math_text
    assert "[0, 100]" in math_text
    assert "theo tài liệu [1]." in math_text
    assert [c["n"] for c in math_used] == [1]

    # Single unmatched stray backtick must NOT disable citation filtering for the rest of the answer
    stray_text, stray_used = agent_service._finalize_answer_citations(
        "Tham số `foo có giá trị 5 [1] và sai [99].",
        cits,
    )
    assert "[1]" in stray_text
    assert "[99]" not in stray_text
    assert [c["n"] for c in stray_used] == [1]

    # Balanced inline code MUST still protect code brackets while outer prose is filtered
    balanced_text, balanced_used = agent_service._finalize_answer_citations(
        "Dùng `arr[1, 99]` theo tài liệu [1] và bỏ [99].",
        cits,
    )
    assert "`arr[1, 99]`" in balanced_text
    assert "tài liệu [1]" in balanced_text
    assert "bỏ [99]" not in balanced_text
    assert [c["n"] for c in balanced_used] == [1]

    # Reference-style markdown links [1]: https://... must NOT be stripped
    ref_text, _ = agent_service._finalize_answer_citations(
        "Xem thêm:\n\n[1]: https://example.com/spec",
        [],
    )
    assert "[1]: https://example.com/spec" in ref_text
