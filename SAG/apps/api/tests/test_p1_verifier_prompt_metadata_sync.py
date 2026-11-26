"""TDD tests for Wave 1.1C:
- 3-site synchronized temporal/authority/conflict metadata rendering
  1. tools/builtin.py:_format_sections (passage & event branches)
  2. services/evidence_service.py:_search_prompt_messages
  3. services/agent_service.py:_verify_grounded_entailment (via verifier_meta_by_n)
- Zero legacy drift when metadata is unset
- SHA-256 content_hash immutability invariant (Audit B2)
- BFF-safe citations (no private keys like _verifier_meta leaked)
- Conditional conflict/authority guardrail prompts & standing scene_notes
- GetEntityTool route_decision forwarding into plan_query
- SearchContextTool ready_documents stat preference
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sag_api.core.db as core_db
from sag_agent import EventType
from sag_agent.runtime import CancellationToken
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.enums import AUTHORITY_TIER_LABELS, DocumentStatus
from sag_api.generation.prompt import build_citations
from sag_api.sag.dto import GraphEventInfo, RetrievedSection, SearchOutcome
from sag_api.services import (
    agent_service,
    evidence_service,
    query_strategy_planner,
    search_unit_retrieval_service,
)
from sag_api.services.agent_domain import AskPlan
from sag_api.services.temporal_metadata_format import format_evidence_metadata_suffix
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import GetEntityTool, SearchContextTool, _format_sections
from sag_api.tools.registry import ToolRegistry


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="test-user",
        organization_id="tenant-a",
        allowed_project_ids=frozenset({"project-a"}),
        issuer="test-issuer",
        key_id="test-key",
        token_id="test-token",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-a",
        allowed_partition_ids=frozenset({"partition-a"}),
    )


def _section(
    *,
    chunk_id: str = "su-1",
    heading: str = "Leave Policy",
    content: str = "Annual leave entitlement is 15 days per calendar year.",
    doc_id: str = "doc-1",
    ver_id: str = "ver-1",
    doc_name: str = "hr_policy.pdf",
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    valid_to: datetime | None = None,
    is_superseded: bool = False,
    conflict_status: str | None = None,
    scope_tags: tuple[str, ...] = (),
) -> RetrievedSection:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return RetrievedSection(
        chunk_id=chunk_id,
        search_unit_id=chunk_id,
        block_from_id="b-1",
        block_to_id="b-2",
        section_path="HR > Leave",
        heading=heading,
        content=content,
        content_hash=content_hash,
        score=0.92,
        raw_dense_score=0.86,
        rank=0,
        source_id="src-1",
        source_config_id="cfg-1",
        canonical_evidence_verified=True,
        document_id=doc_id,
        document_version_id=ver_id,
        document_name=doc_name,
        version_no=1,
        page_from=2,
        page_to=2,
        anchor="p2-b1",
        authority_tier=authority_tier,
        effective_from=effective_from,
        valid_to=valid_to,
        is_superseded=is_superseded,
        conflict_status=conflict_status,
        scope_tags=scope_tags,
    )


@asynccontextmanager
async def _noop_session_factory():
    yield SimpleNamespace()


# ---------------------------------------------------------------------------
# 1. 3-Site Synchronized Rendering (builtin, evidence_service, agent_service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meta_kwargs", "expected_suffix"),
    [
        (
            {"authority_tier": 1, "effective_from": datetime(2025, 6, 15, tzinfo=UTC)},
            "(Chính thức · Hiệu lực 2025-06-15)",
        ),
        (
            {
                "is_superseded": True,
                "authority_tier": 1,
                "effective_from": datetime(2024, 1, 1, tzinfo=UTC),
            },
            "(Bản cũ · Chính thức · Hiệu lực 2024-01-01)",
        ),
        (
            {
                "conflict_status": "ACTIVE_CONFLICT",
                "authority_tier": 2,
                "effective_from": datetime(2025, 6, 15, tzinfo=UTC),
            },
            "(Xung đột · Quy chuẩn · Hiệu lực 2025-06-15)",
        ),
        (
            {
                "conflict_status": "SUPERSEDED_BY_UNIT",
                "authority_tier": 3,
                "effective_from": datetime(2024, 5, 1, tzinfo=UTC),
            },
            "(Bản cũ · Hướng dẫn · Hiệu lực 2024-05-01)",
        ),
        (
            {
                "is_superseded": True,
                "valid_to": datetime(2025, 6, 15, tzinfo=UTC),
            },
            "(Bản cũ · Hết HL 2025-06-15)",
        ),
    ],
)
async def test_three_site_metadata_suffix_sync_across_tool_search_and_verifier(
    meta_kwargs: dict,
    expected_suffix: str,
) -> None:
    """Exact same suffix from format_evidence_metadata_suffix renders identically at all 3 sites."""
    sec = _section(**meta_kwargs)
    computed = format_evidence_metadata_suffix(sec)
    assert computed == expected_suffix
    assert evidence_service.format_evidence_metadata_suffix(sec) == expected_suffix
    assert "[" not in computed and "]" not in computed
    assert len(computed) <= 50

    # Site 1a: tools/builtin.py:_format_sections (passage branch)
    passage_rendered = _format_sections([sec], offset=0)
    passage_lines = passage_rendered.splitlines()
    assert passage_lines[0] == f"[1] {sec.heading} {expected_suffix}"
    assert passage_lines[1].startswith("Locator: file=hr_policy.pdf;")
    assert passage_lines[2] == sec.content

    # Site 1b: tools/builtin.py:_format_sections (event branch)
    ev = GraphEventInfo(
        id="ev-1",
        source_id="src-1",
        source_config_id="cfg-1",
        chunk_id=sec.chunk_id,
        title="Ban hành chính sách nghỉ phép",
        summary="Cập nhật ngày nghỉ phép năm.",
    )
    event_rendered = _format_sections([sec], offset=0, events=[ev])
    event_lines = event_rendered.splitlines()
    assert event_lines[0] == f"[1] Sự kiện: {ev.title} {expected_suffix}"
    assert event_lines[1].startswith("Locator: file=hr_policy.pdf;")
    assert f"Bằng chứng nguyên văn:\n{sec.content}" in event_rendered

    # Site 2: services/evidence_service.py:_search_prompt_messages
    search_msgs = evidence_service._search_prompt_messages("Chính sách nghỉ phép?", [sec])
    assert len(search_msgs) == 2
    assert f"[1] {sec.heading} {expected_suffix}\nlocator: " in search_msgs[1]["content"]
    assert search_msgs[1]["content"].endswith(sec.content)

    # Site 3: services/agent_service.py:_verify_grounded_entailment
    captured_messages: list[list[dict[str, str]]] = []

    async def fake_complete(messages, **_kwargs):
        captured_messages.append(messages)
        return '{"answerability": "answered", "supported": true, "reason": "ok"}'

    llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_complete))
    built_cits = build_citations([sec], {"cfg-1": {"id": "src-1", "name": "HR"}})
    claim_cit = {**built_cits[0], "claim_level": "claim"}
    verifier_meta_by_n = {
        1: {
            "authority_tier": sec.authority_tier,
            "effective_from": sec.effective_from,
            "valid_to": sec.valid_to,
            "is_superseded": sec.is_superseded,
            "conflict_status": sec.conflict_status,
        }
    }
    status, reason = await agent_service._verify_grounded_entailment(
        llm,
        query="Chính sách nghỉ phép có hiệu lực từ khi nào?",
        answer="Chính sách quy định 15 ngày phép năm [1].",
        internal_citations=[claim_cit],
        session_citations=[],
        verifier_content_by_n={1: sec.content},
        verifier_meta_by_n=verifier_meta_by_n,
    )
    assert (status, reason) == ("answered", None)
    assert len(captured_messages) == 1
    sys_prompt = captured_messages[0][0]["content"]
    user_prompt = captured_messages[0][1]["content"]
    assert f"[1] {sec.document_name} — {sec.heading} {expected_suffix}: {sec.content}" in user_prompt
    # Verifier system prompt explicitly accepts header metadata attributes
    assert "header metadata" in sys_prompt
    assert "(Chính thức · Hiệu lực YYYY-MM-DD)" in sys_prompt


@pytest.mark.asyncio
async def test_entailment_verifier_preserves_metadata_suffix_on_budget_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget-cap truncation prefix at _verify_grounded_entailment must retain metadata suffix."""
    captured_user_prompts: list[str] = []

    async def fake_complete(messages, **_kwargs):
        captured_user_prompts.append(messages[1]["content"])
        return '{"answerability": "answered", "supported": true, "reason": "ok"}'

    llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_complete))
    eff = datetime(2025, 6, 15, tzinfo=UTC)
    expected_suffix = "(Chính thức · Hiệu lực 2025-06-15)"
    meta_by_n = {
        1: {
            "authority_tier": 1,
            "effective_from": eff,
            "valid_to": None,
            "is_superseded": False,
            "conflict_status": None,
        },
        2: {
            "authority_tier": 1,
            "effective_from": eff,
            "valid_to": None,
            "is_superseded": False,
            "conflict_status": None,
        },
    }
    cits = [
        {
            "n": 1,
            "claim_level": "claim",
            "document_name": "doc_01.pdf",
            "heading": "Section 1",
            "snippet": "A" * 200,
        },
        {
            "n": 2,
            "claim_level": "claim",
            "document_name": "doc_02.pdf",
            "heading": "Section 2",
            "snippet": "B" * 200,
        },
    ]
    prefix_1 = f"[1] doc_01.pdf — Section 1 {expected_suffix}: "
    prefix_2 = f"[2] doc_02.pdf — Section 2 {expected_suffix}: "
    line_1_body = "A" * 300
    line_2_body = "B" * 300
    line_1_len = len(prefix_1) + len(line_1_body) + 1  # +1 for newline in used_chars

    # Sub-branch 1: remaining_chars > len(prefix_2) + 16 -> truncated body with ellipsis, full suffix kept
    monkeypatch.setattr(
        agent_service,
        "_VERIFIER_MAX_TOTAL_EVIDENCE_CHARS",
        line_1_len + len(prefix_2) + 40,
    )
    status_1, reason_1 = await agent_service._verify_grounded_entailment(
        llm,
        query="Summarize",
        answer="Summary [1] [2].",
        internal_citations=cits,
        session_citations=[],
        verifier_content_by_n={1: line_1_body, 2: line_2_body},
        verifier_meta_by_n=meta_by_n,
    )
    assert (status_1, reason_1) == ("answered", None)
    assert f"{prefix_2}{'B' * 40}…" in captured_user_prompts[-1]

    # Sub-branch 2: len(prefix_2) <= remaining_chars <= len(prefix_2) + 16 -> prefix.rstrip() keeps full suffix
    monkeypatch.setattr(
        agent_service,
        "_VERIFIER_MAX_TOTAL_EVIDENCE_CHARS",
        line_1_len + len(prefix_2) + 5,
    )
    status_2, reason_2 = await agent_service._verify_grounded_entailment(
        llm,
        query="Summarize",
        answer="Summary [1] [2].",
        internal_citations=cits,
        session_citations=[],
        verifier_content_by_n={1: line_1_body, 2: line_2_body},
        verifier_meta_by_n=meta_by_n,
    )
    assert (status_2, reason_2) == ("answered", None)
    assert prefix_2.rstrip() in captured_user_prompts[-1].splitlines()


# ---------------------------------------------------------------------------
# 2. Zero Legacy Drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_legacy_drift_when_metadata_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset metadata produces exact legacy output with zero extra spaces/brackets and no conditional guardrails."""
    legacy_sec = _section(
        authority_tier=None,
        effective_from=None,
        valid_to=datetime(9999, 12, 31, tzinfo=UTC),
        is_superseded=False,
        conflict_status=None,
    )
    assert format_evidence_metadata_suffix(legacy_sec) == ""

    # Site 1a: _format_sections passage branch
    passage = _format_sections([legacy_sec], offset=0)
    passage_lines = passage.splitlines()
    assert passage_lines[0] == "[1] Leave Policy"
    assert not passage_lines[0].endswith(" ")

    # Site 1b: _format_sections event branch
    ev = GraphEventInfo(
        id="ev-1",
        source_id="src-1",
        source_config_id="cfg-1",
        chunk_id=legacy_sec.chunk_id,
        title="Policy Event",
        summary="Event summary.",
    )
    event_out = _format_sections([legacy_sec], offset=0, events=[ev])
    event_lines = event_out.splitlines()
    assert event_lines[0] == "[1] Sự kiện: Policy Event"
    assert not event_lines[0].endswith(" ")

    # Site 2: _search_prompt_messages
    empty_msgs = evidence_service._search_prompt_messages("Leave?", [])
    legacy_msgs = evidence_service._search_prompt_messages("Leave?", [legacy_sec])
    assert legacy_msgs[0]["content"] == empty_msgs[0]["content"]
    assert "When excerpts carry authority" not in legacy_msgs[0]["content"]
    assert "Chính thức > Quy chuẩn" not in legacy_msgs[0]["content"]
    assert "[1] Leave Policy\nlocator: " in legacy_msgs[1]["content"]

    # Site 1c: SearchContextTool.invoke has NO conditional conflict guardrail on legacy sections
    async def fake_retrieve(*_args, **_kwargs):
        return SearchOutcome(
            query="Leave?",
            sections=[legacy_sec],
            stats={"ready_documents": 1, "ready_versions": 1, "candidates": 1},
        )

    monkeypatch.setattr(
        search_unit_retrieval_service, "retrieve_search_unit_sections", fake_retrieve
    )
    tool_ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[SimpleNamespace(id="src-1", name="HR", sag_source_config_id="cfg-1")],
        principal=_principal(),
        evidence_token_budget=8000,
    )
    res = await SearchContextTool().invoke({"query": "Leave?"}, tool_ctx)
    header, body = res.content.split("\n\n", 1)
    assert "Lưu ý thẩm quyền & xung đột" not in header
    assert body.startswith("[1] Leave Policy\nLocator:")

    # Site 3: _verify_grounded_entailment evidence line has exact legacy format
    seen_user: list[str] = []

    async def fake_complete(messages, **_kwargs):
        seen_user.append(messages[1]["content"])
        return '{"answerability": "answered", "supported": true, "reason": "ok"}'

    llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_complete))
    built_cits = build_citations([legacy_sec], {"cfg-1": {"id": "src-1", "name": "HR"}})
    status, reason = await agent_service._verify_grounded_entailment(
        llm,
        query="Leave?",
        answer="15 days [1].",
        internal_citations=[{**built_cits[0], "claim_level": "claim"}],
        session_citations=[],
        verifier_content_by_n={1: legacy_sec.content},
        verifier_meta_by_n={},
    )
    assert (status, reason) == ("answered", None)
    assert f"[1] hr_policy.pdf — Leave Policy: {legacy_sec.content}" in seen_user[0]


# ---------------------------------------------------------------------------
# 3. SHA-256 content_hash Invariant (Audit B2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_traceable_evidence_sha256_invariant_with_metadata() -> None:
    """Metadata formatting must never mutate section.content; SHA-256 verification stays exact."""
    raw_content = "Official leave allocation is 18 days per year."
    sec = _section(
        content=raw_content,
        authority_tier=1,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
        conflict_status="ACTIVE_CONFLICT",
    )
    src = SimpleNamespace(id="src-1", name="HR", sag_source_config_id="cfg-1")

    # Render through both formatting sites
    _ = _format_sections([sec])
    _ = evidence_service._search_prompt_messages("leave days?", [sec])
    assert sec.content == raw_content
    assert hashlib.sha256(sec.content.encode("utf-8")).hexdigest() == sec.content_hash

    resolved = await evidence_service.resolve_traceable_evidence([sec], [src])
    assert len(resolved) == 1
    assert resolved[0].content == raw_content
    assert resolved[0].authority_tier == 1
    assert resolved[0].conflict_status == "ACTIVE_CONFLICT"

    # Tampering with content (as if suffix were mutated into body) must fail SHA-256 verification
    tampered = sec.model_copy(
        update={"content": f"{raw_content} {format_evidence_metadata_suffix(sec)}"}
    )
    assert await evidence_service.resolve_traceable_evidence([tampered], [src]) == []


# ---------------------------------------------------------------------------
# 4. BFF-Safe Citations & End-to-End _adapt_tool / generate_stream Sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_stream_and_adapt_tool_sync_verifier_meta_without_leaking_private_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_adapt_tool & generate_stream feed verifier_meta_by_n while stripping all private keys from public citations."""
    sec = _section(
        authority_tier=1,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
    )
    expected_suffix = "(Chính thức · Hiệu lực 2025-06-15)"
    source = SimpleNamespace(id="src-1", name="HR", sag_source_config_id="cfg-1")

    async def fake_retrieve(*_args, **_kwargs):
        return SearchOutcome(
            query="chính sách nghỉ phép",
            sections=[sec],
            stats={"ready_documents": 1, "ready_versions": 1, "candidates": 1},
        )

    monkeypatch.setattr(
        search_unit_retrieval_service, "retrieve_search_unit_sections", fake_retrieve
    )
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    persist_mock = AsyncMock(return_value="msg-1")
    monkeypatch.setattr(agent_service, "persist_answer", persist_mock)

    # Part A: Direct _adapt_tool check
    host_ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[source],
        principal=_principal(),
        evidence_token_budget=8000,
    )
    citations_buf: list[dict] = []
    verifier_content: dict[int, str] = {}
    verifier_meta: dict[int, dict] = {}
    adapted = agent_service._adapt_tool(
        SearchContextTool(),
        host_ctx,
        citations_buf,
        verifier_content,
        verifier_meta,
    )
    rt_ctx = SimpleNamespace(
        tool_call_id="call-meta-1",
        cancellation=CancellationToken(),
        _on_progress=AsyncMock(),
    )
    rt_res = await adapted.execute({"query": "chính sách nghỉ phép"}, rt_ctx)
    assert expected_suffix in rt_res.content
    assert 1 in verifier_meta
    assert format_evidence_metadata_suffix(verifier_meta[1]) == expected_suffix
    assert citations_buf and all(
        not str(k).startswith("_") for cit in citations_buf for k in cit
    )
    assert all(
        not str(k).startswith("_")
        for cit in rt_res.artifacts["citations"]
        for k in cit
    )

    # Part B: End-to-end generate_stream executing the adapted tool + verifier + scene_notes check
    captured_history: list[list[dict]] = []
    captured_verifier_prompts: list[list[dict[str, str]]] = []

    class ToolCallingStubRuntime:
        async def start(self):
            return None

        def run(self, definition, *, history, context, metadata):
            del context, metadata
            captured_history.append(list(history))
            search_tool = next(t for t in definition.tools if t.spec.name == "search_context")

            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="r-meta",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "r-meta", "sequence": 1},
                )
                tool_res = await search_tool.execute(
                    {"query": "chính sách nghỉ phép"},
                    SimpleNamespace(
                        tool_call_id="call-live-1",
                        cancellation=CancellationToken(),
                        _on_progress=AsyncMock(),
                    ),
                )
                yield SimpleNamespace(
                    type=EventType.TOOL_COMPLETED,
                    run_id="r-meta",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={
                        "tool_call_id": "call-live-1",
                        "name": "search_context",
                        "duration_ms": 5,
                        "details": tool_res.details,
                        "artifacts": tool_res.artifacts,
                    },
                    to_dict=lambda: {"type": "tool.completed", "run_id": "r-meta", "sequence": 2},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="r-meta",
                    sequence=3,
                    timestamp=3,
                    turn=2,
                    version="1",
                    payload={
                        "output": "Chính sách Chính thức hiệu lực 2025-06-15 quy định 15 ngày phép năm [1]."
                    },
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-meta", "sequence": 3},
                )

            return _events()

    async def fake_verifier(messages, **_kwargs):
        captured_verifier_prompts.append(messages)
        return json.dumps({"answerability": "answered", "supported": True, "reason": "ok"})

    reg = ToolRegistry()
    reg.register(SearchContextTool())
    # Also seed a plan citation carrying private _verifier_meta to verify stripping on plan.citations too
    plan = AskPlan(
        query="Chính sách nghỉ phép có hiệu lực từ khi nào?",
        user_message_id="u-meta",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Chính sách nghỉ phép có hiệu lực từ khi nào?"},
        ],
        citations=[],
        route_decision={
            "coarse_intent": "KNOWLEDGE",
            "question_scope": "project",
            "confidence": 0.95,
        },
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
    llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_verifier))

    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=agent,
            thread_id="t-meta",
            engine_manager=SimpleNamespace(),
            llm=llm,
            tool_registry=reg,
            runtime=ToolCallingStubRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]

    # 1. Verifier received the exact same metadata suffix
    assert len(captured_verifier_prompts) == 1
    assert expected_suffix in captured_verifier_prompts[0][1]["content"]
    assert "header metadata" in captured_verifier_prompts[0][0]["content"]

    # 2. Public citations emitted on RUN_COMPLETED & persisted contain ZERO private keys
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == "answered"
    emitted_cits = completed.data["payload"]["citations"]
    assert len(emitted_cits) == 1
    for key in emitted_cits[0]:
        assert not str(key).startswith("_"), f"private key leaked into public citation: {key}"
    assert "_verifier_meta" not in emitted_cits[0]
    assert "_verifier_content" not in emitted_cits[0]

    persisted_cits = persist_mock.await_args.args[3]
    assert len(persisted_cits) == 1
    assert all(not str(k).startswith("_") for k in persisted_cits[0])

    # 3. Standing scene_notes contains exact AUTHORITY_TIER_LABELS hierarchy (no "Nội bộ")
    system_blob = "\n".join(
        str(m.get("content") or "")
        for m in captured_history[0]
        if m.get("role") == "system"
    )
    for label in AUTHORITY_TIER_LABELS.values():
        assert label in system_blob, f"missing standing scene_notes authority label: {label}"
    assert "Bản cũ" in system_blob
    assert "Xung đột" in system_blob
    assert "Hiệu lực" in system_blob
    assert "Nội bộ" not in system_blob


@pytest.mark.asyncio
async def test_generate_stream_strips_private_verifier_meta_seeded_on_plan_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan citations carrying private _verifier_meta feed verifier_meta_by_n yet never leak publicly."""
    sec = _section(
        authority_tier=2,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
        conflict_status="ACTIVE_CONFLICT",
    )
    expected_suffix = "(Xung đột · Quy chuẩn · Hiệu lực 2025-06-15)"
    source = SimpleNamespace(id="src-1", name="HR", sag_source_config_id="cfg-1")
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-2"))

    seeded_cit = {
        **build_citations([sec], {"cfg-1": {"id": "src-1", "name": "HR"}})[0],
        "_verifier_meta": {
            "authority_tier": 2,
            "effective_from": datetime(2025, 6, 15, tzinfo=UTC),
            "conflict_status": "ACTIVE_CONFLICT",
            "is_superseded": False,
        },
    }

    class StubRuntime:
        async def start(self):
            return None

        def run(self, *_args, **_kwargs):
            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="r-plan",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "r-plan", "sequence": 1},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="r-plan",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"output": "Tài liệu Quy chuẩn có xung đột nêu 15 ngày phép [1]."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-plan", "sequence": 2},
                )

            return _events()

    seen_verifier: list[str] = []

    async def fake_verifier(messages, **_kwargs):
        seen_verifier.append(messages[1]["content"])
        return '{"answerability": "answered", "supported": true, "reason": "ok"}'

    plan = AskPlan(
        query="Leave days?",
        user_message_id="u-2",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "Leave days?"}],
        citations=[seeded_cit],
        route_decision={"coarse_intent": "KNOWLEDGE", "question_scope": "project", "confidence": 0.9},
    )
    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True),
            thread_id="t-2",
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_verifier)),
            tool_registry=ToolRegistry(),
            runtime=StubRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    assert len(seen_verifier) == 1
    assert expected_suffix in seen_verifier[0]

    started = next(e for e in events if e.type == EventType.RUN_STARTED.value)
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    for cit in (*started.data["payload"]["citations"], *completed.data["payload"]["citations"]):
        assert "_verifier_meta" not in cit
        assert all(not str(k).startswith("_") for k in cit)


# ---------------------------------------------------------------------------
# 5. Conditional Conflict/Authority Guardrail Injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_guardrail_prompt_injection_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guardrail activates conditionally with exact hierarchy Chính thức > Quy chuẩn > Hướng dẫn > Nháp."""
    sec_t1 = _section(
        chunk_id="su-1",
        doc_id="doc-1",
        ver_id="ver-1",
        authority_tier=1,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
    )
    sec_t3 = _section(
        chunk_id="su-2",
        doc_id="doc-2",
        ver_id="ver-2",
        authority_tier=3,
        effective_from=datetime(2024, 1, 1, tzinfo=UTC),
    )
    sec_conflict = _section(
        chunk_id="su-3",
        doc_id="doc-3",
        ver_id="ver-3",
        conflict_status="ACTIVE_CONFLICT",
        authority_tier=2,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
    )
    sec_superseded = _section(
        chunk_id="su-4",
        doc_id="doc-4",
        ver_id="ver-4",
        is_superseded=True,
        valid_to=datetime(2025, 1, 1, tzinfo=UTC),
    )

    # A. evidence_service._search_prompt_messages: activates whenever ANY section has metadata suffix
    msgs_meta = evidence_service._search_prompt_messages("Chính sách?", [sec_t1])
    assert "Chính thức > Quy chuẩn > Hướng dẫn > Nháp" in msgs_meta[0]["content"]
    assert "(Bản cũ)" in msgs_meta[0]["content"]
    assert "(Xung đột)" in msgs_meta[0]["content"]
    assert "Nội bộ" not in msgs_meta[0]["content"]

    # B. SearchContextTool.invoke:
    #    - Uniform single-tier non-conflicting non-superseded -> NO guardrail
    #    - Mixed authority tiers (>=2) -> guardrail ON
    #    - ACTIVE_CONFLICT -> guardrail ON
    #    - is_superseded=True -> guardrail ON
    tool_ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[SimpleNamespace(id="src-1", name="HR", sag_source_config_id="cfg-1")],
        principal=_principal(),
        evidence_token_budget=8000,
    )

    for label, section_batch, should_inject in [
        ("single_tier_clean", [sec_t1], False),
        ("mixed_tiers", [sec_t1, sec_t3], True),
        ("active_conflict", [sec_conflict], True),
        ("superseded", [sec_superseded], True),
    ]:
        async def _fake_retrieve(*_args, _batch=section_batch, **_kwargs):
            return SearchOutcome(
                query="Chính sách?",
                sections=_batch,
                stats={"ready_documents": len(_batch), "ready_versions": len(_batch), "candidates": len(_batch)},
            )

        monkeypatch.setattr(
            search_unit_retrieval_service, "retrieve_search_unit_sections", _fake_retrieve
        )
        res = await SearchContextTool().invoke({"query": "Chính sách?"}, tool_ctx)
        header, _body = res.content.split("\n\n", 1)
        assert agent_service._CITATION_REFERENCE.findall(header) == [], (
            f"numeric citation leaked in header for {label}"
        )
        if should_inject:
            assert "Lưu ý thẩm quyền & xung đột" in header, f"missing guardrail for {label}"
            assert "Chính thức > Quy chuẩn > Hướng dẫn > Nháp" in header
            assert "Nội bộ" not in header
        else:
            assert "Lưu ý thẩm quyền & xung đột" not in header, f"unexpected guardrail for {label}"


# ---------------------------------------------------------------------------
# 6. GetEntityTool route_decision Forwarding (tools/builtin.py:349)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_tool_forwards_route_decision_to_plan_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetEntityTool.invoke must pass route_decision=ctx.route_decision to plan_query."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_cm():
        async with factory() as s:
            yield s

    monkeypatch.setattr(core_db, "SessionLocal", session_cm)

    async with factory() as s:
        src = Source(id="src-1", name="HR", sag_source_config_id="cfg-1")
        s.add(src)
        await s.flush()
        doc = Document(
            id="doc-1",
            source_id="src-1",
            tenant_id="tenant-a",
            project_id="project-a",
            filename="hr_policy.pdf",
            storage_path="hr_policy.pdf",
            status=DocumentStatus.READY,
            is_active=True,
        )
        s.add(doc)
        await s.flush()
        s.add(
            DocumentVersion(
                id="ver-1",
                document_id="doc-1",
                version_no=1,
                file_hash="hash-1",
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=datetime.now(UTC),
                metadata_json={"security_partition_id": "partition-a"},
            )
        )
        await s.commit()

    real_plan_query = query_strategy_planner.plan_query
    plan_calls: list[dict] = []

    def spy_plan_query(query, **kwargs):
        plan_calls.append({"query": query, **kwargs})
        return real_plan_query(query, **kwargs)

    monkeypatch.setattr(query_strategy_planner, "plan_query", spy_plan_query)

    sec = _section(
        heading="Alice",
        content="Alice owns the leave policy handbook.",
        authority_tier=1,
        effective_from=datetime(2025, 6, 15, tzinfo=UTC),
    )
    mock_retrieve = AsyncMock(
        return_value=SearchOutcome(query="Alice", sections=[sec], stats={})
    )
    monkeypatch.setattr(
        search_unit_retrieval_service, "retrieve_search_unit_sections", mock_retrieve
    )

    route_decision = {
        "temporal_mode": "past_point",
        "answer_shape": "fact_answer",
        "query": "Alice phụ trách gì vào tháng 6/2024?",
    }
    ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[src],
        principal=_principal(),
        route_decision=route_decision,
    )
    try:
        res = await GetEntityTool().invoke({"name": "Alice"}, ctx)
        assert len(plan_calls) == 1
        assert plan_calls[0]["query"] == "Alice"
        assert plan_calls[0]["requested_top_k"] == 6
        assert plan_calls[0]["route_decision"] == route_decision
        called_plan = mock_retrieve.await_args.kwargs["query_strategy_plan"]
        assert called_plan.retrieval_plan.temporal_mode == "past_point"
        # Synchronized suffix also rendered inside GetEntityTool
        assert "(Chính thức · Hiệu lực 2025-06-15)" in res.content
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 7. SearchContextTool ready_documents Stat Preference (tools/builtin.py:247)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stats", "expected_total"),
    [
        ({"ready_documents": 7, "ready_versions": 19, "candidates": 7}, 7),
        ({"ready_versions": 12, "candidates": 12}, 12),
        ({"candidates": 2}, 2),
    ],
)
async def test_search_context_tool_prefers_ready_documents_over_ready_versions(
    monkeypatch: pytest.MonkeyPatch,
    stats: dict,
    expected_total: int,
) -> None:
    """Catalog summary prefers outcome.stats['ready_documents'], falling back to ready_versions/unique_ids."""
    sections = [
        _section(chunk_id="su-1", doc_id="doc-1", ver_id="ver-1", doc_name="a.md"),
        _section(chunk_id="su-2", doc_id="doc-2", ver_id="ver-2", doc_name="b.md"),
    ]

    async def fake_retrieve(*_args, **_kwargs):
        return SearchOutcome(query="list docs", sections=sections, stats=stats)

    monkeypatch.setattr(
        search_unit_retrieval_service, "retrieve_search_unit_sections", fake_retrieve
    )
    ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[SimpleNamespace(id="src-1", name="Docs", sag_source_config_id="cfg-1")],
        principal=_principal(),
        evidence_token_budget=16000,
        route_decision={"answer_shape": "document_list", "is_catalog_query": True},
    )
    res = await SearchContextTool().invoke(
        {"query": "Liệt kê tất cả các tài liệu hiện có"},
        ctx,
    )
    assert f"Tổng số tài liệu sẵn sàng tra cứu trong phạm vi hiện tại: {expected_total};" in res.content
