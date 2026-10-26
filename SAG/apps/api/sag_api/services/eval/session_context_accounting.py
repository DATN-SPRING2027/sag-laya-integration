"""Opt-in baseline observer. Production LLMClient and selection stay unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sag_api.generation.llm import LLMClient, _attr
from sag_api.generation.prompt import estimate_tokens

_ACTIVE: ContextVar[tuple[Any, dict] | None] = ContextVar("session_baseline_call", default=None)


class BaselineBudgetExceeded(RuntimeError):
    pass


class CallLedger:
    """Only allowlisted metadata is retained; prompts, keys and exceptions are not."""

    def __init__(self, *, max_calls: int, max_seconds: float, max_estimated_tokens: int = 2_000_000):
        if min(max_calls, max_seconds, max_estimated_tokens) <= 0:
            raise ValueError("benchmark limits must be positive")
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.max_estimated_tokens = max_estimated_tokens
        self.reserved_tokens = 0
        self.stage = "generation"
        self.calls: list[dict] = []

    def begin(self, *, stream: bool, estimated_input: int, max_output: int, retries: int = 0) -> dict:
        reservation = (estimated_input + max_output) * (1 + retries)
        if (len(self.calls) >= self.max_calls or time.monotonic() >= self.deadline
                or self.reserved_tokens + reservation > self.max_estimated_tokens):
            raise BaselineBudgetExceeded("benchmark resource limit reached")
        self.reserved_tokens += reservation
        record = {
            "stage": self.stage, "stream": stream, "status": "started",
            "input_tokens": None, "output_tokens": None, "cached_tokens": None,
            "reasoning_tokens": None, "usage_snapshots": 0,
            "observed_deployment_attempts": 0, "observed_deployment_failures": 0,
            "configured_retries": retries, "estimated_input_tokens": estimated_input,
            "duration_ms": None, "ttft_ms": None,
        }
        self.calls.append(record)
        return record

    def observe_attempt(self, record: dict) -> None:
        record["observed_deployment_attempts"] += 1

    def observe_failure(self, record: dict) -> None:
        record["observed_deployment_failures"] += 1

    def usage(self, record: dict, raw: Any) -> None:
        if raw is None:
            return
        record["usage_snapshots"] += 1
        for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
            value = _attr(raw, source)
            record[target] = value if type(value) is int and value >= 0 else None
        for target, details in (("cached_tokens", "prompt_tokens_details"),
                                ("reasoning_tokens", "completion_tokens_details")):
            value = _attr(_attr(raw, details), target)
            record[target] = value if type(value) is int and value >= 0 else None

    def report(self, calls: list[dict] | None = None) -> dict:
        records = self.calls if calls is None else calls
        complete = all(
            item["status"] == "completed" and item["input_tokens"] is not None
            and item["output_tokens"] is not None and item["observed_deployment_failures"] == 0
            and item["observed_deployment_attempts"] == 1
            for item in records
        )
        totals = {field: sum(item[field] for item in records if item[field] is not None)
                  for field in ("input_tokens", "output_tokens")}
        return {
            "logical_calls": len(records), "accounting_complete": complete,
            "input_tokens": totals["input_tokens"] if complete else None,
            "output_tokens": totals["output_tokens"] if complete else None,
            "known_input_tokens": totals["input_tokens"], "known_output_tokens": totals["output_tokens"],
            "monetary_cost": None, "price_status": "unverified",
            "transport_retry_visibility": "deployment hooks only; SDK-internal HTTP retries are not observable",
            "stream_usage_aggregation": "last cumulative snapshot, not sum",
            "calls": records,
        }

    @contextmanager
    def capture_deployments(self):
        """Install only inside the standalone harness, never the application lifespan."""
        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        class DeploymentObserver(CustomLogger):
            async def async_pre_call_deployment_hook(self, kwargs, call_type):
                active = _ACTIVE.get()
                if active is not None:
                    active[0].observe_attempt(active[1])
                return kwargs

            async def async_post_call_failure_deployment_hook(
                self, request_data, exception, call_type, fallback_depth=None,
            ):
                active = _ACTIVE.get()
                if active is not None:
                    active[0].observe_failure(active[1])

        observer = DeploymentObserver()
        litellm.callbacks.append(observer)
        try:
            yield
        finally:
            if observer in litellm.callbacks:
                litellm.callbacks.remove(observer)


class TrackedLLM(LLMClient):
    """Replay subclass observes the original provider boundary, including compression."""

    def __init__(self, settings, ledger: CallLedger):
        super().__init__(settings, include_stream_usage=True)
        self.ledger = ledger

    async def _create_completion(self, messages, *, stream=False, tools=None, tool_choice=None):
        estimated = estimate_tokens(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False))
        record = self.ledger.begin(
            stream=stream, estimated_input=estimated, max_output=self._settings.llm_max_tokens,
            retries=self._settings.llm_max_retries,
        )
        started = time.monotonic()
        token = _ACTIVE.set((self.ledger, record))
        try:
            async with asyncio.timeout(max(0, self.ledger.deadline - time.monotonic())):
                response = await super()._create_completion(
                    messages, stream=stream, tools=tools, tool_choice=tool_choice,
                )
            if stream:
                return self._observed_stream(response, record, started)
            self.ledger.usage(record, _attr(response, "usage"))
            record["status"] = "completed"
            return response
        except BaseException as error:
            record["status"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            record["error_type"] = type(error).__name__
            raise
        finally:
            _ACTIVE.reset(token)
            if not stream or record["status"] != "started":
                record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)

    async def _observed_stream(self, stream, record, started):
        token = _ACTIVE.set((self.ledger, record))
        try:
            async with asyncio.timeout(max(0, self.ledger.deadline - time.monotonic())):
                async for chunk in stream:
                    self.ledger.usage(record, _attr(chunk, "usage"))
                    choices = _attr(chunk, "choices", []) or []
                    if record["ttft_ms"] is None and any(
                        _attr(_attr(choice, "delta", {}), "content") for choice in choices
                    ):
                        record["ttft_ms"] = round((time.monotonic() - started) * 1000, 3)
                    yield chunk
            record["status"] = "completed"
        except BaseException as error:
            record["status"] = "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit)) else "failed"
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                await self._close_stream(stream)
            finally:
                _ACTIVE.reset(token)
