"""P6-2 Packet 2 TDD regression tests: SSE openai.py, LiteLLM stream_options/uninstall, unified router & callers."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from sag_agent import AgentEvent, EventType
from sag_api.api.v1 import decision as decision_api
from sag_api.api.v1 import openai as openai_api
from sag_api.api.v1 import search as search_api
from sag_api.core.config import Settings, settings
from sag_api.core.litellm_policy import (
    apply_litellm_completion_policy,
    install_litellm_policy,
    uninstall_litellm_policy,
)
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.generation.llm import LLMClient
from sag_api.schemas.system import ModelConfigUpdate
from sag_api.services import agent_domain, agent_service, decision_router
from sag_api.services.agent_domain import AskPlan
from sag_api.services.agent_service import AgentStreamEvent
from sag_api.services.decision_router import (
    IntentFlags,
    UnifiedRouteDecision,
    clear_decision_route_cache,
    route_query_async,
)
from sag_api.tools.registry import ToolRegistry


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p62",
        organization_id="tenant-p62",
        allowed_project_ids=frozenset({"proj-p62"}),
        issuer="test-issuer",
        key_id="kid-1",
        token_id="tok-1",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-p62",
        allowed_partition_ids=frozenset({"part-p62"}),
    )


def _valid_decision_json() -> str:
    return json.dumps(
        {
            "coarse_intent": "KNOWLEDGE",
            "question_scope": "project",
            "memory_scope": "project_documents",
            "temporal_mode": "atemporal",
            "answer_shape": "fact_answer",
            "intent_flags": {
                "needs_retrieval": True,
                "needs_time": False,
                "needs_web": False,
                "needs_clarification": False,
                "is_pure_math_or_transform": False,
            },
            "domain": "technical",
            "confidence": 0.91,
            "reason_codes": ["DOC_LOOKUP"],
            "order_mode": "time_desc",
            "explicit_count": 3,
            "target_unit": "document",
            "is_catalog_query": True,
            "retrieval_mode_hint": "multi_hop",
            "subqueries": ["Sub Q1?", "Sub Q2?"],
            "topic_slot_key": "slot:policy",
            "hierarchy_bias": "recent",
            "entities": ["PostgreSQL", "Qdrant"],
        }
    )


def _reassemble_sse(body_text: str) -> str:
    assembled: list[str] = []
    for line in body_text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            obj = json.loads(line[6:])
            delta_content = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta_content:
                assembled.append(delta_content)
    return "".join(assembled)


def test_openai_sse_split_unbuffered_citation_tail_handles_inline_code_and_markdown_links() -> None:
    """R-06: Inline code `[1, 2]` and markdown links [1](https://...) must not freeze SSE streaming."""
    flush_code, pending_code = openai_api._split_unbuffered_citation_tail("Use `[1, 2]` in Python")
    assert flush_code == "Use `[1, 2]` in Python"
    assert pending_code == ""

    flush_link, pending_link = openai_api._split_unbuffered_citation_tail(
        "See [1](https://example.com) for details"
    )
    assert flush_link == "See [1](https://example.com) for details"
    assert pending_link == ""

    # Math interval [0, 1] must not stall streaming either
    flush_math, pending_math = openai_api._split_unbuffered_citation_tail("Range [0, 1] is normalized")
    assert flush_math == "Range [0, 1] is normalized"
    assert pending_math == ""


@pytest.mark.asyncio
async def test_openai_sse_midstream_tool_no_double_stream_and_no_silent_tail_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-06: Mid-stream TOOL_STARTED must not double-stream; leading \\n and ' !' must not drop tail text."""
    agent = SimpleNamespace(id="agent-1", name="Tester", persona={}, is_default=False)
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **_kwargs: AskPlan(
            query="q",
            user_message_id="u-1",
            messages=[{"role": "user", "content": "q"}],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "KNOWLEDGE", "degraded": False},
        ),
    )

    async def _run_sse(events_list: list[AgentStreamEvent]) -> str:
        async def _fake_stream(*_args, **_kwargs):
            for ev in events_list:
                yield ev

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", _fake_stream)
        req = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="q")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-1",
            body=req,
            _user=SimpleNamespace(name="User"),
            owner_key="owner-1",
            principal=_principal(),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        chunks = [chunk async for chunk in resp.body_iterator]
        body_text = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
        return _reassemble_sse(body_text)

    # 1) Mid-stream TOOL_STARTED after streamed prefix must NOT duplicate prefix on RUN_COMPLETED
    mid_tool_text = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Let me check. "}}),
            AgentStreamEvent(
                type=EventType.TOOL_STARTED.value,
                data={"payload": {"name": "search_context"}},
            ),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Let me check. Final grounded answer [1].", "citations": []}},
            ),
        ]
    )
    assert mid_tool_text == "Let me check. Final grounded answer [1]."
    assert mid_tool_text.count("Let me check.") == 1

    # 2) Leading newline stripped in canonical output must NOT silently drop the rest of the answer
    leading_nl_text = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "\nHello world"}}),
            AgentStreamEvent(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": " [1] and 500 more words"}},
            ),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Hello world [1] and 500 more words", "citations": []}},
            ),
        ]
    )
    assert "and 500 more words" in leading_nl_text
    assert leading_nl_text.count("Hello world") == 1

    # 3) Space-before-punctuation collapsed in canonical output must NOT silently drop tail text
    space_punct_text = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Hello ! "}}),
            AgentStreamEvent(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": "See [1] and 500 more words"}},
            ),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Hello! See [1] and 500 more words", "citations": []}},
            ),
        ]
    )
    assert "and 500 more words" in space_punct_text
    assert space_punct_text.count("Hello") == 1


@pytest.mark.asyncio
async def test_openai_chat_completions_respects_knowledge_only_and_resolved_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai.py must not hardcode web_enabled=True, has_sources=True before checking agent & sources."""
    agent = SimpleNamespace(
        id="agent-ko",
        name="OfflineBot",
        persona={"knowledge_only": True},
        is_default=False,
    )
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))

    route_calls: list[dict] = []

    async def fake_route(query, **kwargs):
        route_calls.append({"query": query, **kwargs})
        return UnifiedRouteDecision(
            query=query,
            coarse_intent="KNOWLEDGE",
            question_scope="project",
            memory_scope="project_documents",
            temporal_mode="atemporal",
            answer_shape="fact_answer",
            intent_flags=IntentFlags(needs_retrieval=True),
            domain="general",
            confidence=0.9,
        )

    monkeypatch.setattr(openai_api, "route_query_async", fake_route)

    stream_kwargs: list[dict] = []

    async def fake_stream(*_args, **kwargs):
        stream_kwargs.append(kwargs)
        yield AgentStreamEvent(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": "OK", "citations": []}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)

    req = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="Policy?")],
        stream=False,
    )
    await openai_api.chat_completions(
        agent_id="agent-ko",
        body=req,
        _user=SimpleNamespace(name="User"),
        owner_key="owner-1",
        principal=_principal(),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
        llm=SimpleNamespace(configured=True),
        tool_registry=SimpleNamespace(),
        agent_runtime=SimpleNamespace(),
    )
    assert len(route_calls) == 1
    assert route_calls[0]["web_enabled"] is False
    assert route_calls[0]["has_sources"] is False
    assert stream_kwargs[0].get("knowledge_only") is True


@pytest.mark.asyncio
async def test_litellm_policy_stream_options_opt_out_idempotent_and_end_to_end_retry_succeeds() -> None:
    """stream_options=None opt-out must survive double policy application and end-to-end LLMClient retry."""
    import litellm

    cfg = Settings(_env_file=None, llm_api_key="test-key")
    req = {"model": "openai/qwen3.6-flash", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    fallback = dict(req)
    fallback["stream_options"] = None
    after_llm = apply_litellm_completion_policy(cfg, fallback)
    after_hook = apply_litellm_completion_policy(cfg, after_llm)
    assert "stream_options" not in after_llm
    assert "stream_options" not in after_hook
    assert not any(k.startswith("_sag") for k in after_llm)

    # End-to-end LLMClient._create_completion with installed LiteLLM policy
    seen_kwargs: list[dict] = []
    orig = litellm.acompletion

    async def fake_acompletion(*_args, **kwargs):
        seen_kwargs.append(dict(kwargs))
        if "stream_options" in kwargs:
            raise RuntimeError("400 Bad Request: stream_options is not supported by this provider")
        return ["ok-stream"]

    litellm.acompletion = fake_acompletion
    cb = install_litellm_policy(cfg)
    try:
        client = LLMClient(cfg)
        result = await client._create_completion(
            [{"role": "user", "content": "hi"}],
            stream=True,
        )
        assert result == ["ok-stream"]
        assert len(seen_kwargs) == 2
        assert seen_kwargs[0].get("stream_options") == {"include_usage": True}
        assert "stream_options" not in seen_kwargs[1]
    finally:
        uninstall_litellm_policy(cb)
        litellm.acompletion = orig


def test_litellm_policy_out_of_order_uninstall_restores_original_acompletion() -> None:
    """Out-of-order uninstall_litellm_policy(cb1); uninstall_litellm_policy(cb2) must restore orig."""
    import litellm

    cfg = Settings(_env_file=None)
    orig_fn = litellm.acompletion
    cb1 = install_litellm_policy(cfg)
    cb2 = install_litellm_policy(cfg)
    cb3 = install_litellm_policy(cfg)
    try:
        uninstall_litellm_policy(cb2)
        uninstall_litellm_policy(cb1)
        uninstall_litellm_policy(cb3)
        assert litellm.acompletion is orig_fn
        assert cb1 not in litellm.callbacks
        assert cb2 not in litellm.callbacks
        assert cb3 not in litellm.callbacks
    finally:
        litellm.acompletion = orig_fn


@pytest.mark.asyncio
async def test_decision_router_llm_cache_enabled_and_sf_test_key_bypass_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='llm' must respect _DECISION_ROUTE_CACHE; production must have zero sf-test-key bypass."""
    assert "sf-test-key" not in inspect.getsource(decision_router)

    clear_decision_route_cache()
    calls = 0

    class StubLLM:
        configured = True

        async def complete(self, messages, *, response_format=None, temperature=None, max_tokens=None):
            nonlocal calls
            del messages, response_format, temperature, max_tokens
            calls += 1
            return _valid_decision_json()

    monkeypatch.setattr(settings, "decision_provider", "llm")
    stub = StubLLM()
    d1 = await route_query_async("Chính sách nghỉ phép?", llm_client=stub, use_cache=True)
    d2 = await route_query_async("Chính sách nghỉ phép?", llm_client=stub, use_cache=True)
    assert calls == 1
    assert d1.coarse_intent == d2.coarse_intent == "KNOWLEDGE"

    # With prior_turns, cache must NOT be reused
    await route_query_async(
        "Chính sách nghỉ phép?",
        prior_turns=[{"role": "user", "content": "Hi"}],
        llm_client=stub,
        use_cache=True,
    )
    assert calls == 2

    # Even when api_key == "sf-test-key", cache is NOT magically bypassed (only use_cache=False / clear_cache)
    clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "siliconflow")
    monkeypatch.setattr(settings, "decision_api_key", "")
    monkeypatch.setattr(settings, "siliconflow_api_key", "sf-test-key")
    monkeypatch.setattr(settings, "siliconflow_base_url", "https://router.example.com/v1")
    http_calls = 0

    async def fake_call_openai(**_kwargs):
        nonlocal http_calls
        http_calls += 1
        return _valid_decision_json(), "router-model", None

    monkeypatch.setattr(decision_router, "_call_openai_compatible_router", fake_call_openai)
    await route_query_async("Cache check query?", use_cache=True)
    await route_query_async("Cache check query?", use_cache=True)
    assert http_calls == 1


@pytest.mark.asyncio
async def test_model_config_update_and_decision_route_response_schemas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ModelConfigUpdate accepts llm & openai_compatible; DecisionRouteResponse exposes all 9 PR #75 fields."""
    assert ModelConfigUpdate(decision_provider="llm").decision_provider == "llm"
    assert ModelConfigUpdate(decision_provider="openai_compatible").decision_provider == "openai_compatible"
    assert ModelConfigUpdate(decision_provider="siliconflow").decision_provider == "siliconflow"
    assert ModelConfigUpdate(decision_provider="disabled").decision_provider == "disabled"

    clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "llm")
    mock_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value=_valid_decision_json()),
    )

    from sag_api.core.deps import get_current_user

    app = FastAPI()
    app.state.llm = mock_llm
    app.include_router(decision_api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="u1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        resp = await client.post("/api/v1/decision/route", json={"query": "Liệt kê 3 tài liệu mới nhất"})
    assert resp.status_code == 200
    data = resp.json()
    assert mock_llm.complete.await_count == 1
    assert data["order_mode"] == "time_desc"
    assert data["explicit_count"] == 3
    assert data["target_unit"] == "document"
    assert data["is_catalog_query"] is True
    assert data["retrieval_mode_hint"] == "multi_hop"
    assert data["subqueries"] == ["Sub Q1?", "Sub Q2?"]
    assert data["topic_slot_key"] == "slot:policy"
    assert data["hierarchy_bias"] == "recent"
    assert data["entities"] == ["PostgreSQL", "Qdrant"]


@pytest.mark.asyncio
async def test_router_call_sites_forward_llm_client_web_enabled_and_prior_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify llm_client, web_enabled, and prior_turns are wired across search, agent_service, agent_domain."""
    monkeypatch.setattr(settings, "decision_provider", "llm")
    stub_llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value=_valid_decision_json()))

    def _dummy_decision(query: str) -> UnifiedRouteDecision:
        return UnifiedRouteDecision(
            query=query,
            coarse_intent="KNOWLEDGE",
            question_scope="project",
            memory_scope="project_documents",
            temporal_mode="atemporal",
            answer_shape="fact_answer",
            intent_flags=IntentFlags(needs_retrieval=True),
            domain="technical",
            confidence=0.9,
        )

    # 1) api/v1/search._build_query_route forwards llm_client
    search_calls: list[dict] = []

    async def fake_search_route(query: str, **kwargs):
        search_calls.append(kwargs)
        return _dummy_decision(query)

    monkeypatch.setattr(search_api, "route_query_async", fake_search_route)
    await search_api._build_query_route("test query", ["src-1"], None, llm=stub_llm)
    assert search_calls[-1]["llm_client"] is stub_llm

    # 2) agent_service._initial_tool_choice & generate_stream forward llm_client
    agent_calls: list[dict] = []

    async def fake_agent_route(query: str, **kwargs):
        agent_calls.append(kwargs)
        return _dummy_decision(query)

    monkeypatch.setattr(agent_service, "route_query_async", fake_agent_route)
    await agent_service._initial_tool_choice(
        "test query",
        (),
        knowledge_only=True,
        scoped=False,
        llm_client=stub_llm,
    )
    assert agent_calls[-1]["llm_client"] is stub_llm

    @asynccontextmanager
    async def _sf():
        yield SimpleNamespace()

    class _MinRuntime:
        def run(self, *_a, **_kw):
            async def _ev():
                now = datetime.now(UTC)
                yield AgentEvent(type=EventType.RUN_STARTED, run_id="r", sequence=1, timestamp=now, payload={})
                yield AgentEvent(
                    type=EventType.RUN_COMPLETED,
                    run_id="r",
                    sequence=2,
                    timestamp=now,
                    payload={"output": "Done."},
                )

            return _ev()

    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    agent_calls.clear()
    _ = [
        ev
        async for ev in agent_service.generate_stream(
            _sf,
            plan=AskPlan(query="q", messages=[{"role": "user", "content": "q"}], route_decision=None),
            agent=SimpleNamespace(id="a1", name="Bot", persona={}, is_default=False),
            thread_id=None,
            engine_manager=SimpleNamespace(),
            llm=stub_llm,
            tool_registry=ToolRegistry(),
            runtime=_MinRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    assert agent_calls[-1]["llm_client"] is stub_llm

    # 3) agent_domain.prepare_ask wires web_enabled into route_query_async
    monkeypatch.setattr(settings, "session_context_mode", "off")
    domain_calls: list[dict] = []

    async def fake_domain_route(query: str, **kwargs):
        domain_calls.append(kwargs)
        return _dummy_decision(query)

    monkeypatch.setattr(decision_router, "route_query_async", fake_domain_route)
    fake_session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        add=lambda *_a, **_kw: None,
        commit=AsyncMock(),
        refresh=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))),
    )
    monkeypatch.setattr(agent_domain, "get_thread", AsyncMock(return_value=SimpleNamespace(id="t1", title="New")))
    monkeypatch.setattr(agent_domain, "_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_domain, "resolve_user_display_name", AsyncMock(return_value="User"))
    await agent_domain.prepare_ask(
        fake_session,
        agent=SimpleNamespace(id="a1", name="Agent", persona={}, is_default=False),
        thread=SimpleNamespace(id="t1", title="New"),
        query="Thời tiết hôm nay?",
        principal=_principal(),
        owner_key="owner-1",
        llm=stub_llm,
        web_enabled=True,
    )
    assert domain_calls[-1]["web_enabled"] is True
    assert domain_calls[-1]["llm_client"] is stub_llm


@pytest.mark.asyncio
async def test_session_context_and_regenerate_forward_web_enabled_and_prior_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify session_context/runtime.py and answer_actions_service.py forward prior_turns & web_enabled."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.db.base import Base
    from sag_api.db.models import Agent, Message, Thread
    from sag_api.enums import MessageRole, MessageStatus
    from sag_api.services import answer_actions_service
    from sag_api.services.session_context import runtime as sc_runtime

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "session_context_qdrant_sync_enabled", False)
    monkeypatch.setattr(settings, "decision_provider", "llm")
    sc_runtime.SESSION_INDEX_CACHE.clear()

    sc_calls: list[dict] = []
    regen_calls: list[dict] = []

    def _dummy_decision(query: str) -> UnifiedRouteDecision:
        return UnifiedRouteDecision(
            query=query,
            coarse_intent="KNOWLEDGE",
            question_scope="project",
            memory_scope="session_history",
            temporal_mode="atemporal",
            answer_shape="fact_answer",
            intent_flags=IntentFlags(needs_retrieval=True),
            domain="technical",
            confidence=0.9,
        )

    async def fake_sc_route(query: str, **kwargs):
        sc_calls.append({"query": query, **kwargs})
        return _dummy_decision(query)

    async def fake_regen_route(query: str, **kwargs):
        regen_calls.append({"query": query, **kwargs})
        return _dummy_decision(query)

    monkeypatch.setattr(sc_runtime, "route_query_async", fake_sc_route)
    monkeypatch.setattr(decision_router, "route_query_async", fake_regen_route)

    stub_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value='{"turns": []}'),
    )
    try:
        async with factory() as session:
            ag = Agent(id="ag-reg", owner_key="owner-1", name="Bot", persona={})
            session.add(ag)
            await session.flush()
            th = Thread(id="th-reg", agent_id=ag.id, owner_key="owner-1", title="T")
            session.add(th)
            await session.flush()
            now = datetime.now(UTC)
            m1 = Message(
                id="m-u1",
                thread_id=th.id,
                role=MessageRole.USER,
                content="We picked PostgreSQL 18.",
                status=MessageStatus.OK,
                created_at=now,
            )
            m2 = Message(
                id="m-a1",
                thread_id=th.id,
                role=MessageRole.ASSISTANT,
                content="Noted PostgreSQL 18.",
                status=MessageStatus.OK,
                created_at=datetime.fromtimestamp(now.timestamp() + 1, tz=UTC),
            )
            m3 = Message(
                id="m-u2",
                thread_id=th.id,
                role=MessageRole.USER,
                content="Why did we pick it?",
                status=MessageStatus.OK,
                created_at=datetime.fromtimestamp(now.timestamp() + 2, tz=UTC),
            )
            m4 = Message(
                id="m-a2",
                thread_id=th.id,
                role=MessageRole.ASSISTANT,
                content="Because of ACID guarantees.",
                reply_to_message_id="m-u2",
                status=MessageStatus.OK,
                created_at=datetime.fromtimestamp(now.timestamp() + 3, tz=UTC),
            )
            session.add_all([m1, m2, m3, m4])
            await session.commit()

            await sc_runtime.prepare_session_context_for_ask(
                session,
                thread_id=th.id,
                exclude_message_id="m-u2",
                query="Why did we pick it?",
                principal=_principal(),
                owner_key="owner-1",
                requested_source_ids=None,
                budget_tokens=1200,
                llm=stub_llm,
                web_enabled=True,
            )
            assert sc_calls[-1]["web_enabled"] is True
            assert sc_calls[-1]["prior_turns"]
            assert any("PostgreSQL 18" in str(t.get("content", "")) for t in sc_calls[-1]["prior_turns"])

            await answer_actions_service.prepare_regenerate(
                session,
                agent=ag,
                thread=th,
                target_message_id="m-a2",
                principal=_principal(),
                owner_key="owner-1",
                llm=stub_llm,
                web_enabled=True,
            )
            assert regen_calls[-1]["web_enabled"] is True
            assert regen_calls[-1]["prior_turns"]
            assert any("PostgreSQL 18" in str(t.get("content", "")) for t in regen_calls[-1]["prior_turns"])
    finally:
        await engine.dispose()
