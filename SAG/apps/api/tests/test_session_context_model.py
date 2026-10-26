"""Real provider boundary doubles check reservations, retries, usage and failure guards."""

import asyncio
import json
import time

import httpx
import pytest
from openai import APIStatusError, AsyncOpenAI

from sag_api.core.config import Settings
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context.budget import BudgetExceeded, BudgetLimits, BudgetState
from sag_api.services.session_context.model import BudgetedLLM


class Counter:
    def count(self, messages, tools=None):
        return 20


def work(calls=2):
    return BudgetState(BudgetLimits(12288, 4096, calls, 262144, 100000, time.monotonic() + 30))


@pytest.mark.asyncio
async def test_bounded_provider_disables_retries_and_uses_last_cumulative_usage(monkeypatch):
    async def completion(settings, **kwargs):
        assert settings.llm_max_retries == 0 and settings.llm_max_tokens == 4096
        assert kwargs["stream_options"] == {"include_usage": True}

        async def chunks():
            for _ in range(2):
                yield {"choices": [{"delta": {"content": "A"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 2}}

        return chunks()

    monkeypatch.setattr(model_module, "session_completion", completion)
    budget = work()
    client = BudgetedLLM(Settings(_env_file=None, llm_api_key="offline-test"), budget=budget, counter=Counter())
    assert "".join([x async for x in client.stream_complete([{"role": "user", "content": "q"}])]) == "AA"
    assert budget.calls == 1 and budget.known_used_tokens == 14
    assert client.accounting()["accounting_complete"] and client.accounting()["input_tokens"] == 12


@pytest.mark.asyncio
async def test_unknown_provider_usage_blocks_followup_instead_of_free_call(monkeypatch):
    async def completion(settings, **kwargs):
        async def chunks():
            yield {"choices": [{"delta": {"content": "A"}}]}

        return chunks()

    monkeypatch.setattr(model_module, "session_completion", completion)
    budget = work()
    client = BudgetedLLM(Settings(_env_file=None, llm_api_key="offline-test"), budget=budget, counter=Counter())
    assert "".join([x async for x in client.stream_complete([{"role": "user", "content": "q"}])]) == "A"
    assert not client.accounting()["accounting_complete"]
    with pytest.raises(BudgetExceeded):
        await client._create_completion([{"role": "user", "content": "q2"}])
    assert budget.calls == 1


def sdk_transport(monkeypatch, handler):
    """Keep the installed OpenAI SDK; replace only its HTTP transport."""
    clients = []
    options = []
    original_http_client = httpx.AsyncClient

    class TransportClient(original_http_client):
        def __init__(self, **kwargs):
            assert kwargs.get("follow_redirects") is False, "production must explicitly disable redirects"
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))

    def client(**kwargs):
        options.append(dict(kwargs))
        assert kwargs["http_client"].follow_redirects is False
        instance = AsyncOpenAI(**kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(model_module.httpx, "AsyncClient", TransportClient)
    monkeypatch.setattr(model_module, "AsyncOpenAI", client)
    return clients, options


def provider_settings():
    return Settings(
        _env_file=None,
        llm_provider="openai",
        llm_api_key="offline-test",
        llm_model="Qwen/Qwen3-8B",
        llm_base_url="https://provider.invalid/v1",
        llm_max_retries=2,
        llm_timeout_ms=120000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_sdk_errors_make_one_physical_request_without_role_rewrite_or_retry(monkeypatch, status):
    attempts = []
    messages = [{"role": "user", "content": "old"}, {"role": "user", "content": "current"}]

    async def handler(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(status, json={"error": {
            "message": "Conversation roles must alternate user/assistant/user/assistant/",
            "type": "invalid_request_error", "code": "invalid_request",
        }})

    clients, options = sdk_transport(monkeypatch, handler)
    budget = work()
    client = BudgetedLLM(provider_settings(), budget=budget, counter=Counter())
    with pytest.raises(APIStatusError) as caught:
        await client._create_completion(messages)
    assert caught.value.status_code == status
    assert len(attempts) == len(clients) == 1
    assert attempts[0]["messages"] == messages
    assert options[0]["max_retries"] == 0 and options[0]["timeout"] == 60
    assert clients[0].is_closed()
    assert budget.calls == 1 and budget.remaining_tokens is None
    assert not client.accounting()["accounting_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 308])
async def test_sdk_rejects_provider_redirect_without_second_physical_request(monkeypatch, status):
    attempts = []

    async def handler(request):
        attempts.append(request)
        if request.url.host == "provider.invalid":
            return httpx.Response(status, headers={"location": "https://redirected.invalid/v1/chat/completions"},
                                  json={"error": {"message": "redirect disallowed", "type": "redirect"}})
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
                                        "model": "offline", "choices": [],
                                        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}})

    clients, _ = sdk_transport(monkeypatch, handler)
    budget = work()
    client = BudgetedLLM(provider_settings(), budget=budget, counter=Counter())
    with pytest.raises(APIStatusError) as caught:
        await client._create_completion([{"role": "user", "content": "q"}])
    assert caught.value.status_code == status
    assert len(attempts) == 1 and attempts[0].url.host == "provider.invalid"
    assert clients[0].is_closed()
    assert budget.calls == 1 and budget.remaining_tokens is None
    assert not client.accounting()["accounting_complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_usage", [True, False])
async def test_real_sdk_stream_usage_is_observed_and_http_client_closed(monkeypatch, has_usage):
    attempts = []
    chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 1, "model": "offline",
             "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"}]}
    if has_usage:
        chunk["usage"] = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}

    async def handler(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    clients, _ = sdk_transport(monkeypatch, handler)
    budget = work()
    client = BudgetedLLM(provider_settings(), budget=budget, counter=Counter())
    assert "".join([part async for part in client.stream_complete([{"role": "user", "content": "q"}])]) == "A"
    assert len(attempts) == 1 and attempts[0]["stream_options"] == {"include_usage": True}
    assert clients[0].is_closed()
    assert client.accounting()["accounting_complete"] is has_usage
    if has_usage:
        assert budget.known_used_tokens == 14
    else:
        assert budget.remaining_tokens is None


@pytest.mark.asyncio
async def test_input_cap_rejects_before_constructing_sdk_or_sending_http(monkeypatch):
    async def handler(request):
        raise AssertionError("over-cap prompt must not dispatch")

    class TooLarge:
        def count(self, messages, tools=None):
            return 8193

    clients, _ = sdk_transport(monkeypatch, handler)
    budget = work()
    client = BudgetedLLM(provider_settings(), budget=budget, counter=TooLarge())
    with pytest.raises(BudgetExceeded, match="context_window"):
        await client._create_completion([{"role": "user", "content": "q"}])
    assert not clients and budget.calls == 0


@pytest.mark.asyncio
async def test_total_deadline_cancels_real_sdk_attempt_without_retry(monkeypatch):
    attempts = []

    async def handler(request):
        attempts.append(request)
        await asyncio.Event().wait()

    clients, _ = sdk_transport(monkeypatch, handler)
    budget = BudgetState(BudgetLimits(12288, 4096, 2, 262144, 100000, time.monotonic() + 0.05))
    client = BudgetedLLM(provider_settings(), budget=budget, counter=Counter())
    with pytest.raises(TimeoutError):
        await client._create_completion([{"role": "user", "content": "q"}])
    assert len(attempts) == 1 and clients[0].is_closed()
    assert not client.accounting()["accounting_complete"] and budget.remaining_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_usage", [False, True])
async def test_cancelled_sdk_stream_keeps_unknown_usage_and_releases_connection(monkeypatch, partial_usage):
    blocked = asyncio.Event()
    closed = []
    attempts = []

    class BlockingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 1, "model": "offline",
                     "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": None}]}
            if partial_usage:
                chunk["usage"] = {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            blocked.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        attempts.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BlockingBody())

    clients, _ = sdk_transport(monkeypatch, handler)
    budget = work()
    client = BudgetedLLM(provider_settings(), budget=budget, counter=Counter())

    async def consume():
        async for _ in client.stream_complete([{"role": "user", "content": "q"}]):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(blocked.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1 and closed and clients[0].is_closed()
    assert not client.accounting()["accounting_complete"]
    assert budget.remaining_tokens is None
    with pytest.raises(BudgetExceeded):
        await client._create_completion([{"role": "user", "content": "q2"}])
    assert budget.calls == 1 and len(attempts) == 1
