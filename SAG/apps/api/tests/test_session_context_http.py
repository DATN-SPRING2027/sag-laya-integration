"""In-process ASGI/SSE contract, not browser or live-provider acceptance.

Real JWT authentication, scoped ownership, the real agents router and SQLite
storage are exercised. Only local tokenizer/inference and external provider
boundaries are doubled; authentication dependencies are never overridden.
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_session_context_backfill import authenticated_backfill_db as _auth_db
from test_session_context_backfill import scoped_token
from test_session_context_runtime import Counter, Encoder

from sag_agent import AgentRuntime
from sag_api.api.v1 import agents as agents_api
from sag_api.core.config import Settings, settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_agent_runtime, get_engine_manager, get_llm, get_tool_registry
from sag_api.core.errors import ApiError
from sag_api.db.models import Message
from sag_api.generation import LLMClient
from sag_api.sag import EngineManager
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context import runtime as context_module
from sag_api.tools.registry import ToolRegistry

authenticated_backfill_db = _auth_db
PATH = "/api/v1/agents/agent/threads/thread/ask"
QUERY = "Recall the stored choices."


@pytest.fixture
async def session_http(authenticated_backfill_db, monkeypatch):
    factory, token = authenticated_backfill_db
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "llm_model", "Qwen/Qwen3-8B")
    monkeypatch.setattr(agents_api, "SessionLocal", factory)
    monkeypatch.setattr(context_module, "ModelTokenCounter", lambda *args, **kwargs: Counter())
    monkeypatch.setattr(context_module, "configured_encoder", lambda: Encoder())
    requests = []

    async def provider(config, **request):
        requests.append(request)
        assert request["stream"] is False
        assert request["messages"][-1]["content"] == QUERY
        assert "Old answer with unknown transitive evidence" not in str(request["messages"])
        prefix, raw = request["messages"][-2]["content"].split("\n", 1)
        payload = json.loads(raw)
        if prefix == "SESSION_EVIDENCE_JSON":
            output = {"items": [{"text": item["text"], "supports": [{"id": item["id"], "quote": item["text"]}]}
                                for item in payload if "BLUE-472" in item["text"]]}
        else:
            assert prefix == "GENERATED_ITEMS_JSON"
            output = {"answer": "\n".join(item["text"] for item in payload),
                      "supports": [support for item in payload for support in item["supports"]]}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20}}

    monkeypatch.setattr(model_module, "session_completion", provider)
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def domain_error(request, error):
        return JSONResponse(status_code=error.status_code, content=error.to_envelope())

    async def database():
        async with factory() as session:
            yield session

    config = Settings(_env_file=None, llm_api_key="offline-test", llm_model="Qwen/Qwen3-8B")
    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_engine_manager] = lambda: EngineManager(config)
    app.dependency_overrides[get_llm] = lambda: LLMClient(config)
    app.dependency_overrides[get_tool_registry] = lambda: ToolRegistry()
    async with AgentRuntime() as runtime:
        app.dependency_overrides[get_agent_runtime] = lambda: runtime
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid",
                                     headers={"authorization": "Bearer " + token}) as client:
            yield client, factory, requests


@pytest.mark.asyncio
async def test_http_explicit_full_mode_serializes_visible_partial_and_persists_it(session_http):
    client, factory, requests = session_http
    response = await client.post(PATH, json={"query": QUERY, "context_mode": "full_session", "web_enabled": False})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: ")]
    terminal = next(event["payload"] for event in events if event["type"] == "run.completed")
    assert terminal["session_context"]["mode"] == "full_session"
    assert terminal["session_context"]["state"] == "partial"
    assert terminal["session_context"]["reason"] == "unknown_provenance"
    assert terminal["session_context"]["visited"] == terminal["session_context"]["eligible"] == 1
    assert "chưa đầy đủ" in terminal["output"][:160].casefold() and "BLUE-472" in terminal["output"]
    assert terminal["session_accounting"]["logical_calls"] == len(requests) == 2
    assert terminal["session_accounting"]["accounting_complete"] is True
    assert any(event["type"] == "message.delta" and "chưa đầy đủ" in event["payload"].get("delta", "").casefold()
               for event in events)
    async with factory() as session:
        saved = await session.get(Message, terminal["message_id"])
        assert saved.content == terminal["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("context_mode", ["all", 42, {"mode": "full_session"}])
async def test_http_rejects_invalid_context_mode_before_any_model_request(session_http, context_mode):
    client, _, requests = session_http
    response = await client.post(PATH, json={"query": QUERY, "context_mode": context_mode})
    assert response.status_code == 422 and not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("token,status", [("invalid-signature", 401), (None, 404)])
async def test_http_ask_rejects_unverified_or_different_project_token(session_http, token, status):
    client, _, requests = session_http
    token = token or scoped_token(project="other-project")
    response = await client.post(PATH, json={"query": QUERY, "context_mode": "full_session"},
                                 headers={"authorization": "Bearer " + token})
    assert response.status_code == status and not requests
    assert response.headers["content-type"].startswith("application/json")
