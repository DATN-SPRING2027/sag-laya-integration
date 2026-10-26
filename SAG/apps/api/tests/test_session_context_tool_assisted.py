"""Native document-tool protocol with real SQL authorization, not DataEngine acceptance."""

import json
from datetime import UTC, datetime

import pytest
import test_session_context_storage as storage_fixtures
from sqlalchemy import update
from test_session_context_runtime import Counter, Encoder, principal
from test_session_context_storage import raw

from sag_api.core.config import Settings, settings
from sag_api.db.models import (
    Agent,
    AgentBinding,
    Message,
    SessionMessageProvenance,
    Source,
    SourceProjectMapping,
    Thread,
)
from sag_api.enums import BindingTargetType
from sag_api.generation.llm import LLMClient
from sag_api.sag import EngineManager
from sag_api.services.agent_domain import filter_messages_for_scope, prepare_ask
from sag_api.services.agent_service import generate_stream
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context import runtime as context_runtime
from sag_api.tools.base import Tool, ToolMeta, ToolResult
from sag_api.tools.builtin import GetTimeTool
from sag_api.tools.registry import ToolRegistry

storage_db = storage_fixtures.storage_db
QUERY = "Find the confirmed budget code in the attached document."


class TraceableDocumentTool(Tool):
    meta = ToolMeta(name="search_context", description="Read an authorized attached document.",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}},
                                "required": ["query"]})

    def __init__(self, *, padding=0):
        self.calls = []
        self.padding = padding

    async def invoke(self, arguments, context):
        assert {source.id for source in context.sources} == {"source1"}
        assert context.principal.allowed_project_ids == frozenset({"project"})
        self.calls.append(arguments)
        number = context.citation_offset + 1
        return ToolResult(content=f"[{number}] The confirmed budget code is DOC-204.\n" + "x" * self.padding,
            citations=[{"n": number, "chunk_id": "chunk-1", "source_id": "source1", "source_name": "Budget memo",
                        "document_id": "document-1", "document_version_id": "version-1", "page_from": 1,
                        "page_to": 1, "anchor": "budget", "snippet": "The confirmed budget code is DOC-204."}],
            data={"section_count": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_rounds,large_results", [(1, False), (2, False), (2, True)])
async def test_context_on_preserves_document_tool_protocol_and_source_provenance(
    storage_db, monkeypatch, tool_rounds, large_results
):
    factory, scope = storage_db
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(context_runtime, "configured_encoder", lambda: Encoder())
    monkeypatch.setattr(context_runtime, "ModelTokenCounter", lambda *args, **kwargs: Counter())
    async with factory() as session:
        session.add(Source(id="source1", name="Budget memo", sag_source_config_id="memo-config"))
        await session.flush()
        session.add(SourceProjectMapping(source_id="source1", organization_id=scope.organization_id,
                    project_id="project", state="CONFIRMED", confirmed_at=datetime.now(UTC),
                    confirmed_by="fixture", approval_ref="fixture-grant"))
        session.add(AgentBinding(agent_id="agent", target_type=BindingTargetType.SOURCE, target_id="source1"))
        session.add(raw("old-chat", content="I need the budget from my document."))
        await session.commit()
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        plan = await prepare_ask(session, agent=agent, thread=thread, query=QUERY,
                                 source_ids=["source1"], principal=principal(scope), owner_key="owner")
        assert plan.session_context is not None

    requests = []

    async def completion(config, **request):
        assert config.llm_max_retries == 0 and request["stream"] is True
        requests.append(request)
        assert sum(message.get("content") == QUERY for message in request["messages"]) == 1
        frames = [message for message in request["messages"] if message.get("role") == "user"
                  and str(message.get("content", "")).startswith("SESSION_EVIDENCE_JSON\n")]
        assert len(frames) == 1
        if len(requests) <= tool_rounds:
            async def chunks():
                yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"lookup-{len(requests)}",
                    "type": "function",
                    "function": {"name": "search_context", "arguments": json.dumps({"query": QUERY})}}]},
                    "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 40, "completion_tokens": 8}}
        else:
            native_calls = [call for message in request["messages"] for call in message.get("tool_calls", [])]
            tool_results = [message for message in request["messages"] if message.get("role") == "tool"]
            expected_calls = ({f"lookup-{tool_rounds}"} if large_results else
                              {f"lookup-{index}" for index in range(1, tool_rounds + 1)})
            assert {call["id"] for call in native_calls} == expected_calls
            assert {message["tool_call_id"] for message in tool_results} == expected_calls
            assert all("DOC-204" in message["content"] for message in tool_results)

            async def chunks():
                number = tool_rounds if large_results else 1
                output = json.dumps({"answer": f"The confirmed budget code is DOC-204 [{number}].", "supports": []})
                yield {"choices": [{"delta": {"content": output}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 60, "completion_tokens": 14}}
        return chunks()

    monkeypatch.setattr(model_module, "session_completion", completion)
    tool = TraceableDocumentTool(padding=17000 if large_results else 0)
    registry = ToolRegistry()
    registry.register(tool)
    config = Settings(_env_file=None, llm_api_key="offline-test", llm_model="Qwen/Qwen3-8B")
    events = [event async for event in generate_stream(factory, plan=plan, agent=agent, thread_id="thread",
        engine_manager=EngineManager(config), llm=LLMClient(config), tool_registry=registry,
        knowledge_only=True, principal=principal(scope))]

    terminal = next((event.data["payload"] for event in events if event.type == "run.completed"), None)
    assert terminal is not None, [(event.type, event.data.get("payload", {}).get("error")) for event in events]
    number = tool_rounds if large_results else 1
    assert terminal["answer_status"] == "answered" and f"DOC-204 [{number}]" in terminal["output"]
    assert len(requests) == terminal["session_accounting"]["logical_calls"] == tool_rounds + 1
    assert len(tool.calls) == tool_rounds
    assert requests[0]["tool_choice"] == {"type": "function", "function": {"name": "search_context"}}
    assert terminal["citations"][0]["source_id"] == "source1"
    assert terminal["citations"][0]["claim_level"] == "claim"
    assert terminal["citations"][0]["n"] == number
    assert all(Counter().count(request["messages"], request.get("tools")) <= 8192 for request in requests)
    ceiling = min(7, settings.agent_max_steps + 1)
    assert plan.session_context.budget.limits.max_calls == ceiling
    assert plan.session_context.budget.limits.max_total_tokens == 12288 * ceiling
    async with factory() as session:
        message = await session.get(Message, terminal["message_id"])
        provenance = await session.get(SessionMessageProvenance, message.id)
        assert provenance.source_ids == ["source1"]
        assert plan.user_message_id in {reference["message_id"] for reference in provenance.dependencies}
        await session.execute(update(SourceProjectMapping).where(SourceProjectMapping.source_id == "source1").values(
            state="REVOKED", revoked_at=datetime.now(UTC), revoked_by="fixture", revocation_ref="fixture-revoke"))
        await session.commit()
        visible = await filter_messages_for_scope(session, [message], principal=principal(scope), owner_key="owner")
        assert "DOC-204" not in visible[0].content


@pytest.mark.asyncio
async def test_context_on_can_answer_time_only_query_from_actual_native_system_tool(storage_db, monkeypatch):
    factory, scope = storage_db
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(context_runtime, "configured_encoder", lambda: Encoder())
    monkeypatch.setattr(context_runtime, "ModelTokenCounter", lambda *args, **kwargs: Counter())
    query = "What time is it?"
    async with factory() as session:
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        plan = await prepare_ask(session, agent=agent, thread=thread, query=query,
                                 principal=principal(scope), owner_key="owner")
    requests, observed_time = [], []

    async def completion(config, **request):
        requests.append(request)

        async def chunks():
            if len(requests) == 1:
                delta = {"tool_calls": [{"index": 0, "id": "clock-1", "type": "function",
                    "function": {"name": "get_time", "arguments": json.dumps({"timezone": "UTC"})}}]}
                finish = "tool_calls"
            else:
                result = next(message for message in request["messages"] if message.get("role") == "tool")
                assert result["tool_call_id"] == "clock-1" and "UTC" in result["content"]
                observed_time.append(result["content"])
                delta = {"content": json.dumps({"answer": result["content"], "supports": []})}
                finish = "stop"
            yield {"choices": [{"delta": delta, "finish_reason": finish}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": 20}}
        return chunks()

    monkeypatch.setattr(model_module, "session_completion", completion)
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    config = Settings(_env_file=None, llm_api_key="offline-test", llm_model="Qwen/Qwen3-8B")
    events = [event async for event in generate_stream(factory, plan=plan, agent=agent, thread_id="thread",
        engine_manager=EngineManager(config), llm=LLMClient(config), tool_registry=registry,
        knowledge_only=True, principal=principal(scope))]
    terminal = next(event.data["payload"] for event in events if event.type == "run.completed")
    assert len(requests) == 2 and observed_time
    assert terminal["answer_status"] == "answered" and terminal["output"] == observed_time[0]
