"""Finite request budget, independent of defaults or provider-specific prices."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from sag_api.services.session_context.types import ContextContractError, require


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


class BudgetExceeded(ContextContractError):
    code = "session_context_budget_exhausted"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    context_window_tokens: int
    output_reserve_tokens: int
    max_calls: int
    max_total_tokens: int
    max_index_units: int
    deadline: float

    def __post_init__(self):
        require(all(_count(value) and value > 0 for value in (
            self.context_window_tokens, self.output_reserve_tokens, self.max_calls,
            self.max_total_tokens, self.max_index_units,
        )), "budget_limits")
        require(self.output_reserve_tokens < self.context_window_tokens, "output_reserve")
        require(type(self.deadline) in (int, float) and math.isfinite(self.deadline), "deadline")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None

    def __post_init__(self):
        require(all(value is None or _count(value) for value in (self.input_tokens, self.output_tokens)), "usage")


@dataclass(frozen=True, slots=True)
class CallReservation:
    budget_id: str
    call_id: int
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetState:
    """Single-dispatcher state. Reserve synchronously before launching a call.

    Each retry needs a reservation. Unknown usage stops further calls; the
    caller must not hide SDK retries or treat an unobserved attempt as free.
    Token caps rely on the caller's complete model-specific input count. An
    estimate is a planning bound, not a guaranteed bill or context-window cap.
    """

    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.calls = 0
        self.index_units = 0
        self.known_used_tokens = 0
        self.held_unknown_tokens = 0
        self._unknown = False
        self._overrun = False
        self._cancelled = False
        self._budget_id = uuid4().hex
        self._pending: dict[int, CallReservation] = {}

    @property
    def remaining_tokens(self) -> int | None:
        if self._unknown:
            return None
        return max(0, self.limits.max_total_tokens - self.known_used_tokens
                   - sum(token.total for token in self._pending.values()))

    def _check(self):
        instant = self.clock()
        require(type(instant) in (int, float) and math.isfinite(instant), "clock")
        for condition, reason in (
            (self._cancelled, "cancelled"), (self._unknown, "unknown_usage"),
            (self._overrun, "overrun"), (instant >= self.limits.deadline, "deadline"),
        ):
            if condition:
                raise BudgetExceeded(reason)

    def reserve_call(self, *, input_tokens: int) -> CallReservation:
        self._check()
        require(_count(input_tokens), "input_tokens")
        if self.calls >= self.limits.max_calls:
            raise BudgetExceeded("calls")
        total = input_tokens + self.limits.output_reserve_tokens
        if total > self.limits.context_window_tokens:
            raise BudgetExceeded("context_window")
        if total > self.remaining_tokens:
            raise BudgetExceeded("tokens")
        self.calls += 1
        token = CallReservation(self._budget_id, self.calls, input_tokens, self.limits.output_reserve_tokens)
        self._pending[token.call_id] = token
        return token

    def settle_call(self, token: CallReservation, usage: TokenUsage | None) -> None:
        require(isinstance(token, CallReservation) and self._pending.get(token.call_id) == token,
                "invalid_settlement")
        require(usage is None or isinstance(usage, TokenUsage), "usage")
        del self._pending[token.call_id]
        values = (None, None) if usage is None else (usage.input_tokens, usage.output_tokens)
        self.known_used_tokens += sum(value for value in values if value is not None)
        if any(value is None for value in values):
            self._unknown = True
            self.held_unknown_tokens += token.total
            return
        if (values[0] > token.input_tokens or values[1] > token.output_tokens
                or self.known_used_tokens > self.limits.max_total_tokens):
            self._overrun = True
            raise BudgetExceeded("overrun")

    def consume_index_units(self, count: int) -> None:
        self._check()
        require(_count(count), "index_units")
        if self.index_units + count > self.limits.max_index_units:
            raise BudgetExceeded("index_units")
        self.index_units += count

    def cancel(self) -> None:
        self._cancelled = True
