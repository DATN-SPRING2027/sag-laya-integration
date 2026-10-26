"""Native runtime/SSE full-session contracts with an offline provider boundary."""

import asyncio
import json
from dataclasses import replace

import pytest
import test_session_context_storage as storage_fixtures
from test_session_context_runtime import Counter, principal
from test_session_context_storage import raw

from sag_agent import AgentRuntime
from sag_api.core.config import Settings
from sag_api.db.models import Agent, Message
from sag_api.enums import MessageStatus
from sag_api.generation.llm import LLMClient
from sag_api.sag import EngineManager
from sag_api.services.agent_domain import AskPlan
from sag_api.services.agent_service import generate_stream
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context.budget import BudgetExceeded
from sag_api.services.session_context.runtime import prepare_context
from sag_api.tools.registry import ToolRegistry

storage_db = storage_fixtures.storage_db
QUERY = "List all decisions from the entire session."


async def full_plan(factory, scope):
    async with factory() as session:
        session.add_all([raw("first", content="DECISION A-100: use blue theme."),
                         raw("middle", content="A routine status note.", ordinal=1),
                         raw("last", content="DECISION Z-900: ship Tuesday.", ordinal=2)])
        user = raw("current", content=QUERY, ordinal=3)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(session, key=scope.key, principal=principal(scope), user_message=user,
                                        query=QUERY, mode="full_session", counter=Counter())
        agent = await session.get(Agent, "agent")
        return agent, AskPlan(query=QUERY, messages=context.messages(agent, QUERY), user_message_id=user.id,
                              session_context=context)


class ProviderBoundary:
    """Returns exact synthetic decisions; this does not measure model quality."""

    def __init__(self, *, pause=False):
        self.pause = pause
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, settings, **request):
        assert settings.llm_max_retries == 0 and request["stream"] is False
        self.requests.append(request)
        self.started.set()
        if self.pause:
            await asyncio.wait_for(self.release.wait(), timeout=10)
        messages = request["messages"]
        assert messages[-1]["content"] == QUERY
        prefix, content = messages[-2]["content"].split("\n", 1)
        payload = json.loads(content)
        if prefix == "SESSION_EVIDENCE_JSON":
            output = {"items": [{"text": item["text"], "supports": [{"id": item["id"], "quote": item["text"]}]}
                                for item in payload if item["text"].startswith("DECISION")]}
        else:
            assert prefix == "GENERATED_ITEMS_JSON"
            output = {"answer": "\n".join(item["text"] for item in payload),
                      "supports": [support for item in payload for support in item["supports"]]}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20}}


def stream(factory, scope, agent, plan, *, runtime=None):
    config = Settings(_env_file=None, llm_api_key="offline-test", llm_model="Qwen/Qwen3-8B")
    return generate_stream(factory, plan=plan, agent=agent, thread_id="thread", engine_manager=EngineManager(config),
                           llm=LLMClient(config), tool_registry=ToolRegistry(), knowledge_only=True,
                           principal=principal(scope), runtime=runtime)


@pytest.mark.asyncio
async def test_native_full_runtime_reports_all_nested_calls_and_last_decision(storage_db, monkeypatch):
    factory, scope = storage_db
    agent, plan = await full_plan(factory, scope)
    provider = ProviderBoundary()
    monkeypatch.setattr(model_module, "session_completion", provider)
    events = [event async for event in stream(factory, scope, agent, plan)]
    terminal = next(event.data["payload"] for event in events if event.type == "run.completed")
    assert all(decision in terminal["output"] for decision in ("A-100", "Z-900"))
    assert terminal["session_context"]["state"] == "ready"
    assert terminal["session_context"]["visited"] == terminal["session_context"]["eligible"] == 3
    assert len(provider.requests) == terminal["session_accounting"]["logical_calls"] == 2
    assert terminal["session_accounting"]["accounting_complete"] is True
    assert terminal["usage"]["requests"] == 2
    assert terminal["usage"]["input_tokens"] == 24 and terminal["usage"]["output_tokens"] == 40
    async with factory() as session:
        saved = await session.get(Message, terminal["message_id"])
        assert saved.status == MessageStatus.OK and saved.content == terminal["output"]


@pytest.mark.asyncio
async def test_full_runtime_call_cap_returns_visible_partial_without_second_provider_request(storage_db, monkeypatch):
    factory, scope = storage_db
    agent, plan = await full_plan(factory, scope)
    context = plan.session_context
    context.budget.limits = replace(context.budget.limits, max_calls=1)
    provider = ProviderBoundary()
    monkeypatch.setattr(model_module, "session_completion", provider)
    events = [event async for event in stream(factory, scope, agent, plan)]
    terminal = next(event.data["payload"] for event in events if event.type == "run.completed")
    assert terminal["session_context"]["state"] == "partial"
    assert terminal["session_context"]["reason"] == "calls"
    assert "chưa đầy đủ" in terminal["output"][:160].casefold()
    assert len(provider.requests) == terminal["session_accounting"]["logical_calls"] == 1
    assert terminal["usage"]["requests"] == 1


@pytest.mark.asyncio
async def test_registered_run_cancellation_stops_full_map_and_persists_cancelled_message(storage_db, monkeypatch):
    factory, scope = storage_db
    agent, plan = await full_plan(factory, scope)
    provider = ProviderBoundary(pause=True)
    monkeypatch.setattr(model_module, "session_completion", provider)
    events, run_ids = [], []
    registered = asyncio.Event()

    async with AgentRuntime() as runtime:
        async def consume():
            async for event in stream(factory, scope, agent, plan, runtime=runtime):
                events.append(event)
                if event.type == "run.started":
                    run_ids.append(event.data["run_id"])
                    registered.set()

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(provider.started.wait(), timeout=6)
            await asyncio.wait_for(registered.wait(), timeout=6)
            handle = runtime.get_run(run_ids[0])
            assert handle is not None and not handle.done
            handle.cancel()
            await asyncio.wait_for(task, timeout=6)
        finally:
            provider.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    terminal = next(event.data["payload"] for event in events if event.type == "run.cancelled")
    assert not any(event.type == "run.completed" for event in events)
    assert len(provider.requests) == terminal["session_accounting"]["logical_calls"] == 1
    assert terminal["session_accounting"]["accounting_complete"] is False
    assert plan.session_context.budget.remaining_tokens is None
    assert terminal["session_context"]["state"] == "partial"
    async with factory() as session:
        saved = await session.get(Message, terminal["message_id"])
        assert saved.status == MessageStatus.CANCELLED and saved.content == ""


@pytest.mark.asyncio
async def test_full_runtime_preserves_more_than_128_valid_decision_supports(storage_db, monkeypatch):
    """The normal decoder's support cap must not silently erase a valid full answer."""
    factory, scope = storage_db
    expected = [f"D{index:03}" for index in range(130)]
    async with factory() as session:
        session.add_all([raw(f"many-{index:03}", content=f"DECISION {code}: x.", ordinal=index)
                         for index, code in enumerate(expected)])
        user = raw("current", content=QUERY, ordinal=131)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(session, key=scope.key, principal=principal(scope), user_message=user,
                                        query=QUERY, mode="full_session", counter=Counter())
        agent = await session.get(Agent, "agent")
        plan = AskPlan(query=QUERY, messages=context.messages(agent, QUERY), user_message_id=user.id,
                       session_context=context)
    provider = ProviderBoundary()
    monkeypatch.setattr(model_module, "session_completion", provider)
    events = [event async for event in stream(factory, scope, agent, plan)]
    terminal = next(event.data["payload"] for event in events if event.type == "run.completed")
    assert terminal["session_context"]["visited"] == terminal["session_context"]["eligible"] == 130
    assert all(code in terminal["output"] for code in expected)
    assert terminal["session_context"]["state"] == "ready"
    assert terminal["answer_status"] == "answered"
    assert len(provider.requests) == terminal["session_accounting"]["logical_calls"] == 2


@pytest.mark.asyncio
async def test_full_runtime_unknown_usage_stops_after_map_and_never_claims_completion(storage_db, monkeypatch):
    factory, scope = storage_db
    agent, plan = await full_plan(factory, scope)

    class MissingUsage(ProviderBoundary):
        async def __call__(self, settings, **request):
            response = await super().__call__(settings, **request)
            response.pop("usage")
            return response

    provider = MissingUsage()
    monkeypatch.setattr(model_module, "session_completion", provider)
    events = []
    try:
        async for event in stream(factory, scope, agent, plan):
            events.append(event)
    except BudgetExceeded as error:
        # The HTTP route converts this structured service error to run.failed.
        # No unvalidated answer may escape while accounting is incomplete.
        assert error.reason == "unknown_usage"
    assert len(provider.requests) == plan.session_context.budget.calls == 1
    assert plan.session_context.budget.remaining_tokens is None
    assert plan.session_context.selection.state == "partial"
    assert plan.session_context.active_model.accounting()["accounting_complete"] is False
    for event in events:
        if event.type == "run.completed":
            assert event.data["payload"]["session_context"]["state"] == "partial"
            assert "chưa đầy đủ" in event.data["payload"]["output"][:160].casefold()
