"""Regression tests for Milestone P1 Commit 7: runtime, strict JSON, providers, prompts, gap/handover (S5-11..14)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from sag_agent import (
    Agent,
    AgentRuntime,
    AgentTool,
    EventType,
    ModelChunk,
    ModelProvider,
    ModelRequest,
    RunStoreError,
    RuntimeConfig,
    ToolSpec,
    Usage,
)
from sag_api.api.v1 import handover as handover_api
from sag_api.core import litellm_policy
from sag_api.core.config import Settings, settings
from sag_api.core.deps import get_llm, get_session
from sag_api.core.errors import ApiError
from sag_api.core.model_providers import get_model_provider
from sag_api.generation import llm as llm_module
from sag_api.generation.llm import LLMClient
from sag_api.generation.prompt import build_agent_messages, build_messages
from sag_api.services import knowledge_gap_question_service


@pytest.mark.asyncio
async def test_agent_runtime_max_turns_uses_user_role_and_emit_checks_store_before_push():
    """S5-12: finalize_on_max_turns appends role='user'; store.append error raises before pushing event."""
    captured_turns: list[list[dict]] = []

    class MaxTurnsProvider(ModelProvider):
        async def stream_turn(self, request: ModelRequest, cancellation):
            captured_turns.append([m.to_model_dict() for m in request.messages])
            if request.turn == 1:
                from sag_agent import ToolCall

                yield ModelChunk(
                    tool_calls=(ToolCall(id="call-1", name="echo", arguments={"text": "hi"}),),
                    finish_reason="tool_calls",
                    usage=Usage(requests=1),
                )
            else:
                yield ModelChunk(text_delta="Final answer", finish_reason="stop", usage=Usage(requests=1))

    async def echo_exec(args, _ctx):
        return str(args.get("text") or "")

    tool = AgentTool(
        spec=ToolSpec(name="echo", description="echo", parameters={"type": "object"}),
        executor=echo_exec,
    )
    runtime = AgentRuntime()
    await runtime.start()
    agent = Agent(
        name="tester",
        model=MaxTurnsProvider(),
        instructions="sys",
        tools=(tool,),
        max_turns=1,
        finalize_on_max_turns=True,
    )
    events = [
        ev
        async for ev in runtime.run(
            agent,
            history=[{"role": "user", "content": "Go"}],
        )
    ]
    assert len(captured_turns) == 2
    assert captured_turns[1][-1]["role"] == "user"
    assert any(ev.type == EventType.RUN_COMPLETED for ev in events)

    # Now verify _emit raises RunStoreError BEFORE pushing non-terminal event to handle/listeners
    class FailingStore:
        async def append(self, _event):
            raise RuntimeError("disk full")

    seen_by_listener: list[EventType] = []
    strict_rt = AgentRuntime(
        config=RuntimeConfig(store=FailingStore(), fail_on_store_error=True),
    )
    strict_rt.subscribe(lambda ev: seen_by_listener.append(ev.type))
    await strict_rt.start()
    handle = strict_rt.run(agent, history=[{"role": "user", "content": "Go"}])
    with pytest.raises(RunStoreError):
        await strict_rt._emit(handle, EventType.TURN_STARTED, turn=1, payload={})
    assert seen_by_listener == []
    assert handle._queue.empty()


def test_build_messages_empty_sections_has_abstain_rule_and_respects_language():
    """S5-11: Prompts enforce abstention when sources are empty and respect explicit language."""
    msgs_empty = build_messages("What is the secret code?", [], language="en")
    combined_en = "\n".join(m["content"] for m in msgs_empty).lower()
    assert "not contain enough" in combined_en or "do not have enough" in combined_en

    agent_msgs = build_agent_messages("Continuum AI", {}, "Where is the config?", language="vi")
    assert "vietnamese" in agent_msgs[0]["content"].lower()
    assert "enough evidence" in agent_msgs[0]["content"].lower()

    msgs_zh = build_messages("问题？", [], language="zh")
    assert "chinese" in msgs_zh[0]["content"].lower()
    assert "never respond in chinese" not in msgs_zh[0]["content"].lower()


def test_prompts_enforce_enterprise_editorial_style_no_decorative_emojis_and_clean_citations():
    """Enterprise editorial rules ban decorative heading emojis and normalize citation punctuation."""
    for lang in ("en", "vi"):
        direct_msgs = build_messages("Which frontend technologies are used?", [], language=lang)
        agent_msgs = build_agent_messages(
            "Continuum SAG",
            {},
            "Which frontend technologies are used?",
            language=lang,
        )
        for system_text in (direct_msgs[0]["content"], agent_msgs[0]["content"]):
            lowered = system_text.lower()
            assert "[formatting and tone]" in lowered
            assert "never use decorative emojis" in lowered
            assert "markdown table" in lowered
            assert "no space after the closing bracket" in lowered
            assert "never '[4] .'" in lowered


@pytest.mark.asyncio
async def test_llm_client_strict_structured_output_mode_does_not_strip_response_format(monkeypatch):
    """S5-13: Strict llm_structured_output_mode must re-raise 400/422 instead of stripping response_format."""
    calls: list[dict] = []

    class BadFormatError(Exception):
        status_code = 400

    async def fake_litellm(**kwargs):
        calls.append(kwargs)
        raise BadFormatError("response_format is unsupported")

    monkeypatch.setattr(llm_module, "_litellm_completion", fake_litellm)
    cfg = Settings(
        llm_provider="openai",
        llm_api_key="sk-test",
        llm_model="gpt-4o-mini",
        llm_structured_output_mode="json_object",
    )
    client = LLMClient(cfg)
    with pytest.raises(ApiError):
        await client.complete(
            [{"role": "user", "content": "emit json"}],
            response_format={"type": "json_object"},
        )
    assert len(calls) == 1


def test_anthropic_provider_has_valid_default_model_and_configurable_temperature():
    """S5-13: Anthropic spec uses a valid default model and allows bounded [0, 1] temperature."""
    spec = get_model_provider("anthropic")
    assert spec.default_model != "claude-sonnet-5"
    assert spec.default_model.startswith("claude-")
    assert spec.temperature_configurable is True
    assert spec.default_temperature == pytest.approx(0.3)
    assert spec.resolve_temperature(0.2) == pytest.approx(0.2)
    assert spec.resolve_temperature(1.8) == pytest.approx(1.0)
    assert spec.resolve_temperature(-0.5) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_litellm_policy_does_not_leak_startup_settings_into_draft_client(monkeypatch):
    """S5-13: Active LLMClient settings must override startup settings in litellm policy and respect deepseek opt-in."""
    import litellm

    startup = Settings(
        llm_provider="openai",
        llm_api_key="sk-startup",
        llm_model="deepseek-v4-flash",
        llm_extra_body={"enable_thinking": False},
    )
    draft = Settings(
        llm_provider="openai",
        llm_api_key="sk-draft",
        llm_model="gpt-4o-mini",
        llm_extra_body={},
    )
    seen_kwargs: list[dict] = []

    async def fake_orig_acompletion(**kwargs):
        seen_kwargs.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", fake_orig_acompletion)
    monkeypatch.setattr(llm_module, "_litellm_completion", lambda **kw: litellm.acompletion(**kw))
    callback = litellm_policy.install_litellm_policy(startup)
    try:
        client = LLMClient(draft)
        await client.complete([{"role": "user", "content": "hi"}])
        assert seen_kwargs[0].get("extra_body") is None
    finally:
        litellm_policy.uninstall_litellm_policy(callback)
        if callback in litellm.callbacks:
            litellm.callbacks.remove(callback)

    # DeepSeek v4 explicit opt-in thinking must not be overwritten
    ds_req = litellm_policy.apply_litellm_completion_policy(
        startup,
        {
            "model": "openai/deepseek-v4-flash",
            "extra_body": {"thinking": {"type": "enabled"}},
        },
    )
    assert ds_req["extra_body"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_knowledge_gap_question_routes_through_llm_client_and_handover_passes_json_object(monkeypatch):
    """S5-14: knowledge_gap_question uses LLMClient; handover_assist passes response_format=json_object."""
    gap_calls: list[dict] = []

    async def fake_client_complete(self, messages, **kwargs):
        gap_calls.append({"messages": messages, **kwargs})
        return "What is the exact token expiration TTL?"

    monkeypatch.setattr(LLMClient, "complete", fake_client_complete)
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")

    phrased = await knowledge_gap_question_service._phrase_with_configured_llm('{"gap": "ttl"}')
    assert "token expiration TTL" in phrased
    assert len(gap_calls) == 1
    assert gap_calls[0]["temperature"] == 0
    assert gap_calls[0]["max_tokens"] == 120

    # Handover assist must pass response_format={"type": "json_object"}
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")
    recorded_rf: list[dict | None] = []

    async def fake_handover_complete(messages, **kwargs):
        recorded_rf.append(kwargs.get("response_format"))
        return json.dumps(
            {
                "candidates": [{"evidenceIndex": 1, "reason": "Relevant runbook."}],
                "items": [
                    {
                        "title": "Deploy",
                        "body": "Use helm.",
                        "requirement": "Know helm.",
                        "evidenceIndices": [1],
                    }
                ],
                "answerStatus": "ANSWERED",
                "message": "ok",
                "sections": [{"text": "Use helm.", "evidenceIndices": [1]}],
            }
        )

    mock_llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=fake_handover_complete))
    app = FastAPI()
    app.include_router(handover_api.router, prefix="/api/v1")
    async def _fake_session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))

    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[get_llm] = lambda: mock_llm

    sources_payload = [
        {
            "kind": "PROJECT_DOCUMENT",
            "id": "851a2b3c4d5e6f7a8b9c0d31",
            "version": 1,
            "title": "Runbook",
            "excerpts": [
                {
                    "locatorKind": "SECTION",
                    "locatorValue": "deploy",
                    "heading": "Deploy",
                    "content": "Use helm upgrade.",
                }
            ],
        }
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for task_name in ("SOURCE_PROPOSAL", "DRAFT_GENERATION", "QUESTION_ANSWER"):
            res = await client.post(
                "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
                headers={
                    "X-SAG-BFF-Service-Token": "test-bff-token",
                    "X-Continuum-User-Id": "651a2b3c4d5e6f7a8b9c0d11",
                    "X-Continuum-Organization-Id": "651a2b3c4d5e6f7a8b9c0d10",
                    "X-Continuum-Tenant-Id": "651a2b3c4d5e6f7a8b9c0d10",
                    "X-Continuum-Project-Id": "751a2b3c4d5e6f7a8b9c0d20",
                    "X-Continuum-Security-Partition": "751a2b3c4d5e6f7a8b9c0d20",
                },
                json={"task": task_name, "query": "How to deploy?", "sources": sources_payload},
            )
            assert res.status_code == 200
    assert recorded_rf == [
        {"type": "json_object"},
        {"type": "json_object"},
        {"type": "json_object"},
    ]
