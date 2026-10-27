"""Finite provider boundary. No retries, unknown usage is never treated as zero."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
from openai import AsyncOpenAI

from sag_api.core.litellm_policy import apply_litellm_completion_policy
from sag_api.generation.llm import LLMClient, _attr
from sag_api.services.session_context.budget import BudgetState, TokenUsage
from sag_api.services.session_context.types import require

TOKENIZER = ("Qwen/Qwen3-8B", "b968826d9c46dd6066d109eabc6255188de91218")


async def session_completion(settings, **request):
    """One SDK attempt; bypass LiteLLM's implicit alternating-message resend."""
    require(settings.llm_provider == "openai", "unsupported_session_provider")
    request = apply_litellm_completion_policy(
        settings,
        {
            "model": settings.llm_model,
            "api_base": settings.llm_base_url,
            "temperature": settings.effective_llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            **request,
        },
    )
    request.pop("api_base", None)
    request.pop("allowed_openai_params", None)
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        max_retries=0,
        timeout=settings.llm_timeout_ms / 1000,
        http_client=httpx.AsyncClient(follow_redirects=False),
    )
    try:
        response = await client.chat.completions.create(**request)
    except BaseException:
        await client.close()
        raise
    if not request.get("stream"):
        await client.close()
        return response

    async def owned_stream():
        try:
            async for chunk in response:
                yield chunk
        finally:
            await response.close()
            await client.close()

    return owned_stream()


class ModelTokenCounter:
    def __init__(self, model: str, *, template_kwargs: dict | None = None):
        require(model == TOKENIZER[0], "unsupported_session_tokenizer")
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(
            TOKENIZER[0], revision=TOKENIZER[1], local_files_only=True, token=False, trust_remote_code=False
        )
        self.template_kwargs = dict(template_kwargs or {})

    def count(self, messages, tools=None):
        require(all(isinstance(m.get("content", ""), str) for m in messages), "session_text_only")
        tokens = self.tokenizer.apply_chat_template(
            messages, tools=tools or None, tokenize=True, add_generation_prompt=True, **self.template_kwargs
        )
        # Explicit headroom for provider framing; usage still settles the reservation.
        input_ids = tokens["input_ids"] if isinstance(tokens, Mapping) else tokens
        require(isinstance(input_ids, list) and all(type(value) is int for value in input_ids), "tokenizer_shape")
        return len(input_ids) + 256


class BudgetedLLM(LLMClient):
    def __init__(self, settings, *, budget: BudgetState, counter, before_dispatch=None):
        finite = settings.model_copy(
            update={
                "llm_max_retries": 0,
                "llm_max_tokens": budget.limits.output_reserve_tokens,
                "llm_timeout_ms": min(settings.llm_timeout_ms, 60000),
            }
        )
        super().__init__(finite, include_stream_usage=True)
        self.budget, self.counter, self.before_dispatch = budget, counter, before_dispatch
        self.calls = []

    async def _create_completion(self, messages, *, stream=False, tools=None, tool_choice=None):
        if self.before_dispatch is not None:
            await self.before_dispatch()
        reservation = self.budget.reserve_call(input_tokens=self.counter.count(messages, tools))
        record = {
            "status": "started",
            "input_tokens": None,
            "output_tokens": None,
            "reserved_input_tokens": reservation.input_tokens,
            "configured_retries": 0,
        }
        self.calls.append(record)

        def settle(usage):
            values = [_attr(usage, name) for name in ("prompt_tokens", "completion_tokens")]
            values = [v if type(v) is int and v >= 0 else None for v in values]
            record.update(input_tokens=values[0], output_tokens=values[1])
            self.budget.settle_call(reservation, TokenUsage(*values))

        try:
            async with asyncio.timeout(max(0, self.budget.limits.deadline - self.budget.clock())):
                request = {"messages": messages, "stream": stream, "response_format": {"type": "json_object"}}
                if stream:
                    request["stream_options"] = {"include_usage": True}
                if tools:
                    request.update(tools=tools, tool_choice=tool_choice or "auto")
                response = await session_completion(self._settings, **request)
        except BaseException:
            record["status"] = "failed"
            settle(None)
            raise
        if not stream:
            record["status"] = "completed"
            settle(_attr(response, "usage"))
            return response

        async def observed_stream():
            usage = None
            try:
                async with asyncio.timeout(max(0, self.budget.limits.deadline - self.budget.clock())):
                    async for chunk in response:
                        current = _attr(chunk, "usage")
                        if current is not None:
                            usage = current  # cumulative snapshots, not additive usage
                        yield chunk
                record["status"] = "completed"
            except BaseException as error:
                record["status"] = (
                    "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed"
                )
                raise
            finally:
                try:
                    settle(usage if record["status"] == "completed" else None)
                finally:
                    await self._close_stream(response)

        return observed_stream()

    def accounting(self):
        complete = all(
            row["status"] == "completed" and row["input_tokens"] is not None and row["output_tokens"] is not None
            for row in self.calls
        )
        return {
            "logical_calls": len(self.calls),
            "accounting_complete": complete,
            "input_tokens": sum(row["input_tokens"] or 0 for row in self.calls) if complete else None,
            "output_tokens": sum(row["output_tokens"] or 0 for row in self.calls) if complete else None,
            "monetary_cost": None,
            "calls": list(self.calls),
        }
