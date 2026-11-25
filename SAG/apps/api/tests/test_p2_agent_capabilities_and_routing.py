"""TDD regression tests for P2 Step 3: ToolCapability, zero agent regexes, 1-call wiring, and degraded propagation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from sag_agent import AgentEvent, EventType, ToolCapability
from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.services import agent_service
from sag_api.services.agent_domain import AskPlan
from sag_api.tools import ToolRegistry
from sag_api.tools.builtin import (
    GetEntityTool,
    GetTimeTool,
    OpenWebPageTool,
    SearchContextTool,
    WebSearchTool,
)


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-1",
        organization_id="org-1",
        allowed_project_ids=frozenset({"proj-1"}),
        issuer="continuum-be",
        key_id="kid-1",
        token_id="jti-1",
        issued_at=1_700_000_000,
        expires_at=1_700_000_900,
        tenant_id="org-1",
        allowed_partition_ids=frozenset({"part:proj-1"}),
    )


def test_builtin_tool_capabilities_and_no_agent_semantic_regexes() -> None:
    assert ToolCapability.KNOWLEDGE_SEARCH in SearchContextTool.meta.capabilities
    assert ToolCapability.RESEARCH in SearchContextTool.meta.capabilities
    assert ToolCapability.ENTITY_LOOKUP in GetEntityTool.meta.capabilities
    assert ToolCapability.RESEARCH in GetEntityTool.meta.capabilities
    assert ToolCapability.TIME_LOOKUP in GetTimeTool.meta.capabilities
    assert ToolCapability.RESEARCH not in GetTimeTool.meta.capabilities
    assert ToolCapability.WEB_SEARCH in WebSearchTool.meta.capabilities
    assert ToolCapability.RESEARCH in WebSearchTool.meta.capabilities
    assert ToolCapability.WEB_FETCH in OpenWebPageTool.meta.capabilities
    assert ToolCapability.RESEARCH in OpenWebPageTool.meta.capabilities

    for removed in (
        "_DIRECT_INTENT",
        "_DIRECT_RESPONSE_HINT",
        "_CLARIFICATION_FIRST_INTENT",
        "_SIMPLE_ARITHMETIC",
        "_TIME_INTENT",
        "_TIME_ONLY_INTENT",
        "_RELATIVE_TIME_INTENT",
        "_TIME_SENSITIVE_INTENT",
        "_PUBLIC_TIME_SENSITIVE_INTENT",
        "_EXPLICIT_RESEARCH_INTENT",
        "_PUBLIC_EXTERNAL_LOOKUP_INTENT",
        "_RESEARCH_TOOL",
        "_SESSION_CITATION_REFERENCE",
        "_should_classify_question_scope",
        "_needs_current_or_external_lookup",
    ):
        assert not hasattr(agent_service, removed), f"{removed} must be removed from agent_service"


def test_mcp_tool_defaults_to_empty_capabilities_and_reads_explicit_metadata() -> None:
    from sag_api.tools.mcp import MCPTool, _extract_mcp_capabilities

    unannotated = MCPTool(
        SimpleNamespace(),
        remote_name="read_price_info",
        local_name="mcp__srv__read_price_info",
        description="Read price and search documents",
        parameters={"type": "object", "properties": {}},
    )
    assert unannotated.meta.capabilities == frozenset()

    explicit = _extract_mcp_capabilities(
        SimpleNamespace(name="web_lookup", annotations={"capabilities": ["web_search", "research"]}),
        configured_capabilities=None,
    )
    assert explicit == frozenset({ToolCapability.WEB_SEARCH, ToolCapability.RESEARCH})


@pytest.mark.asyncio
async def test_dich_vu_du_an_does_not_bypass_grounding_and_uses_structured_intent() -> None:
    from sag_api.services.agent_service import _initial_tool_choice

    tools = tuple(
        agent_service._adapt_tool(t, SimpleNamespace(citation_offset=0, citation_numbers_by_call_id={}), [])
        for t in (SearchContextTool(), GetTimeTool(), WebSearchTool())
    )
    # "dịch vụ dự án" previously matched unanchored "dịch" in _DIRECT_RESPONSE_HINT -> "auto".
    # With structured routing (KNOWLEDGE, project, needs_retrieval=True), it must enforce search_context!
    choice = await _initial_tool_choice(
        "Dịch vụ dự án hỗ trợ các tính năng gì?",
        tools,
        knowledge_only=False,
        scoped=False,
        route_decision={
            "coarse_intent": "KNOWLEDGE",
            "question_scope": "project",
            "intent_flags": {
                "needs_retrieval": True,
                "needs_time": False,
                "needs_web": False,
                "needs_clarification": False,
                "is_pure_math_or_transform": False,
            },
            "confidence": 0.9,
        },
        require_project_grounding=True,
    )
    assert choice == {"type": "function", "function": {"name": "search_context"}}


@pytest.mark.asyncio
async def test_unknown_scope_from_degraded_router_keeps_retrieval_and_propagates_degraded(monkeypatch) -> None:
    from sag_api.services import decision_router

    decision_router.clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "disabled")

    captured_runtime_def = {}

    class _NoToolAnswerRuntime:
        def run(self, definition, *, history, **kwargs):
            del history, kwargs
            captured_runtime_def["def"] = definition

            async def _events():
                now = datetime.now(UTC)
                yield AgentEvent(type=EventType.RUN_STARTED, run_id="r1", sequence=1, timestamp=now, payload={})
                yield AgentEvent(
                    type=EventType.MESSAGE_DELTA,
                    run_id="r1",
                    sequence=2,
                    timestamp=now,
                    payload={"role": "assistant", "delta": "Tôi đoán vậy."},
                )
                yield AgentEvent(
                    type=EventType.RUN_COMPLETED,
                    run_id="r1",
                    sequence=3,
                    timestamp=now,
                    payload={"output": "Tôi đoán vậy."},
                )

            return _events()

    @asynccontextmanager
    async def _sf():
        yield SimpleNamespace()

    source = SimpleNamespace(id="src-1", name="Docs", sag_source_config_id="cfg-1")
    monkeypatch.setattr(
        agent_service,
        "resolve_sources",
        AsyncMock(return_value=[source]),
    )
    monkeypatch.setattr(
        agent_service,
        "resolve_mcp_specs",
        AsyncMock(return_value=[]),
    )

    reg = ToolRegistry()
    reg.register(SearchContextTool())
    reg.register(GetTimeTool())

    llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value='{"answerability":"answered","supported":true}'),
    )
    plan = AskPlan(
        query="Thời tiết ở Sao Hỏa trong dự án?",
        messages=[{"role": "user", "content": "Thời tiết ở Sao Hỏa trong dự án?"}],
        route_decision=None,
    )

    events = [
        ev
        async for ev in agent_service.generate_stream(
            _sf,
            plan=plan,
            agent=SimpleNamespace(id="ag-1", name="Bot", persona={}, model=None),
            thread_id=None,
            engine_manager=SimpleNamespace(),
            llm=llm,
            tool_registry=reg,
            runtime=_NoToolAnswerRuntime(),
            knowledge_only=False,
            principal=_principal(),
        )
    ]

    started = next(e for e in events if e.type == "run.started")
    completed = next(e for e in events if e.type == "run.completed")
    assert started.data["payload"]["degraded"] is True
    assert started.data["payload"]["degraded_reason"] == "decision_router_disabled"
    assert started.data["payload"]["question_scope"] == "unknown"
    assert started.data["payload"]["grounding_required"] is True
    assert completed.data["payload"]["degraded"] is True
    assert completed.data["payload"]["degraded_reason"] == "decision_router_disabled"
    # Because grounding was required and search_context was never called, fail-closed to no_answer!
    assert completed.data["payload"]["answer_status"] == "no_answer"
    assert captured_runtime_def["def"].initial_tool_choice == {
        "type": "function",
        "function": {"name": "search_context"},
    }


@pytest.mark.asyncio
async def test_decision_route_endpoint_exposes_s4_08_and_degraded_fields(monkeypatch) -> None:
    from sag_api.api.v1.decision import router as decision_router_api
    from sag_api.core.deps import get_current_user
    from sag_api.services import decision_router

    decision_router.clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "disabled")

    app = FastAPI()
    app.include_router(decision_router_api, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post("/api/v1/decision/route", json={"query": "Quy định là gì?"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["coarse_intent"] == "AMBIGUOUS"
    assert data["question_scope"] == "unknown"
    assert data["answer_shape"] == "fact_answer"
    assert data["intent_flags"]["needs_retrieval"] is True
    assert data["degraded"] is True
    assert data["degraded_reason"] == "decision_router_disabled"
    assert data["reason_codes"] == ["DECISION_ROUTER_DISABLED"]


@pytest.mark.asyncio
async def test_search_and_openai_and_single_call_prepare_ask_propagate_degraded(monkeypatch) -> None:
    from sag_api.api.v1 import openai as openai_api
    from sag_api.api.v1.search import _build_query_route, _with_query_route_stats
    from sag_api.core.deps import (
        get_agent_runtime,
        get_current_user,
        get_engine_manager,
        get_llm,
        get_tool_registry,
    )
    from sag_api.core.principal_assertion import require_principal_assertion
    from sag_api.services import agent_domain, decision_router

    decision_router.clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "disabled")

    # 1) Search route + stats propagation
    route_plan = await _build_query_route("Chính sách nghỉ phép", None, None)
    assert route_plan.trace["degraded"] is True
    assert route_plan.trace["degraded_reason"] == "decision_router_disabled"
    merged_stats = _with_query_route_stats({"sources": 1}, route_plan)
    assert merged_stats["degraded"] is True
    assert merged_stats["degraded_reason"] == "decision_router_disabled"

    # 2) Strictly 1 router call across prepare_ask + generate_stream even with session_context_mode="off"
    monkeypatch.setattr(settings, "session_context_mode", "off")
    calls = 0

    async def _one_call_router(query, **_kwargs):
        nonlocal calls
        calls += 1
        return decision_router.UnifiedRouteDecision(
            query=query,
            coarse_intent="KNOWLEDGE",
            question_scope="general",
            memory_scope="none",
            temporal_mode="atemporal",
            answer_shape="fact_answer",
            intent_flags=decision_router.IntentFlags(needs_retrieval=False),
            domain="general",
            confidence=0.9,
            degraded=False,
        )

    monkeypatch.setattr(decision_router, "route_query_async", _one_call_router)
    monkeypatch.setattr(agent_service, "route_query_async", _one_call_router)

    fake_session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        add=lambda *_a, **_kw: None,
        commit=AsyncMock(),
        refresh=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))),
    )
    monkeypatch.setattr(agent_domain, "get_thread", AsyncMock(return_value=SimpleNamespace(id="t1", title="New")))
    monkeypatch.setattr(agent_domain, "_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_domain, "resolve_user_display_name", AsyncMock(return_value="Thang"))

    plan = await agent_domain.prepare_ask(
        fake_session,
        agent=SimpleNamespace(id="a1", name="Agent", persona={}, is_default=False),
        thread=SimpleNamespace(id="t1", title="New"),
        query="Giải thích HTTP?",
        principal=_principal(),
        owner_key="owner-1",
        llm=SimpleNamespace(configured=True, complete=AsyncMock(return_value="{}")),
    )
    assert plan.route_decision is not None
    assert calls == 1

    @asynccontextmanager
    async def _sf():
        yield SimpleNamespace()

    class _DirectRuntime:
        def run(self, definition, *, history, **kwargs):
            del definition, history, kwargs

            async def _events():
                now = datetime.now(UTC)
                yield AgentEvent(type=EventType.RUN_STARTED, run_id="r2", sequence=1, timestamp=now, payload={})
                yield AgentEvent(
                    type=EventType.MESSAGE_DELTA,
                    run_id="r2",
                    sequence=2,
                    timestamp=now,
                    payload={"role": "assistant", "delta": "HTTP is stateless."},
                )
                yield AgentEvent(
                    type=EventType.RUN_COMPLETED,
                    run_id="r2",
                    sequence=3,
                    timestamp=now,
                    payload={"output": "HTTP is stateless."},
                )

            return _events()

    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="m1"))
    _ = [
        e
        async for e in agent_service.generate_stream(
            _sf,
            plan=plan,
            agent=SimpleNamespace(id="a1", name="Agent", persona={}, is_default=False),
            thread_id=None,
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True, complete=AsyncMock(return_value="{}")),
            tool_registry=ToolRegistry(),
            runtime=_DirectRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    assert calls == 1

    # 3) /v1/chat/completions degraded header & sag metadata
    monkeypatch.setattr(
        openai_api,
        "route_query_async",
        AsyncMock(
            return_value=decision_router.UnifiedRouteDecision(
                query="Hi",
                coarse_intent="AMBIGUOUS",
                question_scope="unknown",
                memory_scope="hybrid_both",
                temporal_mode="atemporal",
                answer_shape="fact_answer",
                intent_flags=decision_router.IntentFlags(needs_retrieval=True),
                domain="general",
                confidence=0.0,
                degraded=True,
                degraded_reason="decision_router_disabled",
            )
        ),
    )
    monkeypatch.setattr(
        openai_api.svc,
        "get_agent",
        AsyncMock(return_value=SimpleNamespace(id="a1", name="Bot", persona={}, is_default=False)),
    )
    monkeypatch.setattr(
        openai_api.agent_service,
        "generate_stream",
        lambda *_a, **_kw: _async_iter(
            [
                agent_service.AgentStreamEvent(
                    type="run.started",
                    data={"payload": {"degraded": True, "degraded_reason": "decision_router_disabled"}},
                ),
                agent_service.AgentStreamEvent(
                    type="run.completed",
                    data={
                        "payload": {
                            "output": "Fallback reply",
                            "citations": [],
                            "degraded": True,
                            "degraded_reason": "decision_router_disabled",
                        }
                    },
                ),
            ]
        ),
    )

    app = FastAPI()
    app.include_router(openai_api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1", name="U")
    app.dependency_overrides[require_principal_assertion] = _principal
    app.dependency_overrides[get_engine_manager] = lambda: SimpleNamespace()
    app.dependency_overrides[get_llm] = lambda: SimpleNamespace(configured=True)
    app.dependency_overrides[get_tool_registry] = lambda: ToolRegistry()
    app.dependency_overrides[get_agent_runtime] = lambda: SimpleNamespace()

    from sag_api.core.conversation_ownership import get_conversation_owner
    from sag_api.core.db import get_session

    app.dependency_overrides[get_conversation_owner] = lambda: "owner-1"

    async def _db():
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = _db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        non_stream = await client.post(
            "/api/v1/openai/a1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}], "stream": False},
        )
        assert non_stream.status_code == 200
        assert non_stream.json()["sag"]["degraded"] is True
        assert non_stream.json()["sag"]["degraded_reason"] == "decision_router_disabled"

        stream_resp = await client.post(
            "/api/v1/openai/a1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}], "stream": True},
        )
        assert stream_resp.status_code == 200
        assert stream_resp.headers.get("x-sag-degraded") == "true"
        assert '"degraded": true' in stream_resp.text


async def _async_iter(items):
    for item in items:
        yield item
