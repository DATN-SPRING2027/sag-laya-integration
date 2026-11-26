"""Regression tests for Milestone P1 Commit 5: project grounding with web enabled & OpenAI SSE safety (S5-02, S5-07)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_agent import EventType
from sag_api.api.v1 import openai as openai_api
from sag_api.core.config import Settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.generation.llm import LLMClient
from sag_api.services import agent_service
from sag_api.services.agent_domain import AskPlan
from sag_api.services.agent_service import AgentStreamEvent
from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision
from sag_api.tools.builtin import SearchContextTool, WebSearchTool
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


@asynccontextmanager
async def _noop_session_factory():
    yield SimpleNamespace()


@pytest.mark.asyncio
async def test_project_scope_with_web_enabled_forces_search_context_and_citation_gate(monkeypatch):
    """S5-02: question_scope='project' with web_enabled=True must force search_context and enforce citation gate."""
    source = SimpleNamespace(id="src-1", name="Project Docs", sag_source_config_id="cfg-1")
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-1"))
    monkeypatch.setattr(
        agent_service,
        "route_query_async",
        AsyncMock(
            return_value=UnifiedRouteDecision(
                query="What is the project architecture?",
                coarse_intent="KNOWLEDGE",
                question_scope="project",
                memory_scope="project_documents",
                temporal_mode="atemporal",
                answer_shape="fact_answer",
                intent_flags=IntentFlags(needs_retrieval=True),
                domain="technical",
                confidence=0.9,
                degraded=False,
            )
        ),
    )

    captured: dict = {}

    class StubRuntime:
        async def start(self):
            return None

        def run(self, definition, history, context, metadata=None):
            captured["definition"] = definition
            captured["metadata"] = metadata

            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="run-1",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "run-1", "sequence": 1},
                )
                yield SimpleNamespace(
                    type=EventType.MESSAGE_DELTA,
                    run_id="run-1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"role": "assistant", "delta": "Uncited claim from parametric memory."},
                    to_dict=lambda: {"type": "message.delta", "run_id": "run-1", "sequence": 2},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="run-1",
                    sequence=3,
                    timestamp=3,
                    turn=1,
                    version="1",
                    payload={"output": "Uncited claim from parametric memory."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "run-1", "sequence": 3},
                )

            return _events()

    reg = ToolRegistry()
    reg.register(SearchContextTool())
    reg.register(WebSearchTool())
    plan = AskPlan(
        query="Dịch vụ API trong dự án có giới hạn rate limit bao nhiêu?",
        user_message_id="u-1",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Dịch vụ API trong dự án có giới hạn rate limit bao nhiêu?"},
        ],
        citations=[],
        route_decision={"is_chitchat": False, "confidence": 0.1},
    )
    agent = SimpleNamespace(id="agent-1", name="Tester", persona={}, is_default=True)
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value='{"scope": "project"}'))

    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=agent,
            thread_id="t-1",
            engine_manager=SimpleNamespace(),
            llm=llm,
            tool_registry=reg,
            runtime=StubRuntime(),
            knowledge_only=False,
            principal=_principal(),
        )
    ]

    assert captured["definition"].initial_tool_choice == {
        "type": "function",
        "function": {"name": "search_context"},
    }
    started = next(e for e in events if e.type == EventType.RUN_STARTED.value)
    assert started.data["payload"].get("grounding_required") is True
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == "no_answer"
    assert completed.data["payload"]["no_answer_reason"] == "weak_evidence"


@pytest.mark.asyncio
async def test_openai_sse_buffers_grounded_stream_and_sanitizes_errors(monkeypatch):
    """S5-07: OpenAI SSE must buffer grounded deltas, emit finish_reason='error' without Chinese, and forward params."""
    agent = SimpleNamespace(id="agent-1", name="Tester", persona={})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **_kwargs: AskPlan(
            query="q",
            user_message_id="u-1",
            messages=[{"role": "user", "content": "q"}],
            citations=[{"n": 1, "source_id": "s1"}],
        ),
    )

    forwarded_kwargs: list[dict] = []

    async def fake_stream_rewritten(*_args, **kwargs):
        forwarded_kwargs.append(kwargs)
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": True}},
        )
        # Simulate un-buffered delta with invalid [99] that RUN_COMPLETED strips
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "Answer [99]."}},
        )
        yield AgentStreamEvent(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": "Answer.", "citations": []}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream_rewritten)

    req = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="q")],
        stream=True,
        temperature=0.2,
        max_tokens=256,
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
    assert "[99]" not in body_text
    assert "Answer." in body_text
    assert forwarded_kwargs[0].get("temperature") == 0.2
    assert forwarded_kwargs[0].get("max_tokens") == 256

    # Error stream must emit finish_reason="error", no finish_reason="stop", no Chinese "[错误]"
    async def fake_stream_error(*_args, **_kwargs):
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": False}},
        )
        yield AgentStreamEvent(
            type=EventType.RUN_FAILED.value,
            data={"payload": {"error": {"code": "upstream_error", "message": "Provider failed"}}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream_error)
    err_resp = await openai_api.chat_completions(
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
    err_chunks = [chunk async for chunk in err_resp.body_iterator]
    err_text = "".join(c.decode() if isinstance(c, bytes) else c for c in err_chunks)
    assert "[错误]" not in err_text
    assert '"finish_reason": "error"' in err_text
    assert '"finish_reason": "stop"' not in err_text
    assert "Provider failed" in err_text

    # Non-streaming with empty citations override must return citations=[] (not plan.citations)
    non_stream_req = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="q")],
        stream=False,
    )
    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream_rewritten)
    non_stream_res = await openai_api.chat_completions(
        agent_id="agent-1",
        body=non_stream_req,
        _user=SimpleNamespace(name="User"),
        owner_key="owner-1",
        principal=_principal(),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
        llm=SimpleNamespace(configured=True),
        tool_registry=SimpleNamespace(),
        agent_runtime=SimpleNamespace(),
    )
    assert non_stream_res["sag"]["citations"] == []


@pytest.mark.asyncio
async def test_llm_client_create_completion_respects_temperature_and_max_tokens_override(monkeypatch):
    """S5-07: LLMClient._create_completion honors explicit temperature and max_tokens overrides."""
    captured: dict = {}

    async def fake_litellm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None)

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", fake_litellm)
    cfg = Settings(
        llm_provider="openai",
        llm_api_key="sk-test",
        llm_model="gpt-4o-mini",
        llm_temperature=0.7,
        llm_max_tokens=1024,
        llm_context_window=4096,
    )
    client = LLMClient(cfg)
    await client.complete(
        [{"role": "user", "content": "hi"}],
        temperature=0.15,
        max_tokens=300,
    )
    assert captured["temperature"] == pytest.approx(0.15)
    assert captured["max_tokens"] == 300


@pytest.mark.asyncio
async def test_openai_sse_ungrounded_stream_does_not_leak_stripped_citation_brackets(monkeypatch):
    """PROBE-5C: Unbuffered grounding_required=False SSE must not leak stripped [99]/[S99] before RUN_COMPLETED."""
    agent = SimpleNamespace(id="agent-1", name="Tester", persona={})
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
        ),
    )

    async def fake_stream_ungrounded_with_split_citation(*_args, **_kwargs):
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": False}},
        )
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "See [docs](https://example.com) and "}},
        )
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "General fact ["}},
        )
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "99]."}},
        )
        yield AgentStreamEvent(
            type=EventType.RUN_COMPLETED.value,
            data={
                "payload": {
                    "output": "See [docs](https://example.com) and General fact.",
                    "citations": [],
                }
            },
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream_ungrounded_with_split_citation)
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
    assert "[99]" not in body_text
    assert "99]" not in body_text
    assert "[docs](https://example.com)" in body_text
    # Reassemble streamed content deltas
    import json as _json

    assembled = []
    for line in body_text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            obj = _json.loads(line[6:])
            delta_content = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta_content:
                assembled.append(delta_content)
    assert "".join(assembled) == "See [docs](https://example.com) and General fact."


@pytest.mark.asyncio
async def test_openai_nonstream_incomplete_grounded_does_not_leak_raw_parts(monkeypatch):
    """openai.py:212: Non-streaming incomplete grounded run without RUN_COMPLETED must not emit raw parts."""
    agent = SimpleNamespace(id="agent-1", name="Tester", persona={})
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
        ),
    )

    async def fake_incomplete_grounded(*_args, **_kwargs):
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": True}},
        )
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "Unverified leak [99]"}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_incomplete_grounded)
    non_stream_req = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="q")],
        stream=False,
    )
    non_stream_res = await openai_api.chat_completions(
        agent_id="agent-1",
        body=non_stream_req,
        _user=SimpleNamespace(name="User"),
        owner_key="owner-1",
        principal=_principal(),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
        llm=SimpleNamespace(configured=True),
        tool_registry=SimpleNamespace(),
        agent_runtime=SimpleNamespace(),
    )
    assert non_stream_res["choices"][0]["message"]["content"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deltas", "canonical_output"),
    [
        (["Hello", " [", "1", ",", " 2", "]", " world"], "Hello world"),
        (["Hello [1, S2", "] world"], "Hello world"),
        (["Hello [1, 2 ", "] world"], "Hello world"),
    ],
)
async def test_openai_sse_ungrounded_compound_citation_tail_does_not_leak(
    monkeypatch, deltas: list[str], canonical_output: str
):
    """PROBE-5C: Split and token-by-token compound citations must be held until RUN_COMPLETED resolves them."""
    import json as _json

    agent = SimpleNamespace(id="agent-1", name="Tester", persona={})
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
        ),
    )

    async def fake_stream(*_args, **_kwargs):
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": False}},
        )
        for piece in deltas:
            yield AgentStreamEvent(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": piece}},
            )
        yield AgentStreamEvent(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": canonical_output, "citations": []}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
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

    assembled = []
    for line in body_text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            obj = _json.loads(line[6:])
            delta_content = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta_content:
                assembled.append(delta_content)

    assert "[1, 2]" not in body_text
    assert "[1, S2]" not in body_text
    assert "".join(assembled) == canonical_output


@pytest.mark.asyncio
async def test_openai_sse_ungrounded_markdown_link_streams_immediately(monkeypatch):
    """PROBE-5C: Markdown links in ungrounded SSE must stream immediately without getting stuck."""
    import json as _json

    agent = SimpleNamespace(id="agent-1", name="Tester", persona={})
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
        ),
    )

    emitted_before_completion: list[str] = []
    completed_emitted = False

    async def fake_stream(*_args, **_kwargs):
        nonlocal completed_emitted
        yield AgentStreamEvent(
            type=EventType.RUN_STARTED.value,
            data={"payload": {"grounding_required": False}},
        )
        yield AgentStreamEvent(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "See [docs](https://example.com) now."}},
        )
        completed_emitted = True
        yield AgentStreamEvent(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": "See [docs](https://example.com) now.", "citations": []}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
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
    # Consume chunks one-by-one and verify the markdown link arrives before RUN_COMPLETED
    it = resp.body_iterator.__aiter__()
    while True:
        try:
            raw = await it.__anext__()
        except StopAsyncIteration:
            break
        text = raw.decode() if isinstance(raw, bytes) else raw
        for line in text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                obj = _json.loads(line[6:])
                choice = (obj.get("choices") or [{}])[0]
                delta_content = choice.get("delta", {}).get("content")
                if delta_content:
                    assert not completed_emitted
                    emitted_before_completion.append(delta_content)

    assert emitted_before_completion == ["See [docs](https://example.com) now."]

