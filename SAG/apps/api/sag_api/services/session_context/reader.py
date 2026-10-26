"""Pinned in-memory fixture reader. No production storage adapter or last-N cutoff."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.scope import _check_time, eligible_message_ids, make_snapshot
from sag_api.services.session_context.types import Coverage, SessionScope, SessionSnapshot, TraceMessage, require


def order_key(message: TraceMessage) -> tuple[datetime, str]:
    return message.created_at.astimezone(UTC), message.message_id


def trace_work(message: TraceMessage) -> int:
    """Logical work, not tokens: row, codepoint blocks and dependency edges."""
    return 1 + (len(message.content) + 1023) // 1024 + len(message.dependencies) + len(message.source_ids)


@dataclass(frozen=True, slots=True)
class PageCursor:
    snapshot: SessionSnapshot
    after: tuple[datetime, str]


@dataclass(frozen=True, slots=True)
class TracePage:
    snapshot: SessionSnapshot
    items: tuple[TraceMessage, ...] = field(repr=False)
    next_cursor: PageCursor | None


@dataclass(frozen=True, slots=True)
class HistoryRead:
    snapshot: SessionSnapshot
    messages: tuple[TraceMessage, ...] = field(repr=False)
    ordinals: tuple[int, ...]
    coverage: Coverage
    excluded_count: int
    unknown_provenance: bool


class FixtureTraceStore:
    """Copy immutable synthetic rows under a fixed scope/generation.

    Does not accept ORM Message rows: provenance must be supplied explicitly by
    the fixture producer. A real adapter needs T5/T6 authorization and fences.
    """

    def __init__(self, messages: tuple[TraceMessage, ...], *, scope: SessionScope,
                 generation: str, budget: BudgetState):
        require(isinstance(messages, tuple), "fixture_rows")
        records = {}
        for message in messages:
            budget.consume_index_units(1)
            require(isinstance(message, TraceMessage) and message.key == scope.key, "fixture_scope")
            budget.consume_index_units(trace_work(message))
            require(message.message_id not in records, "duplicate_message")
            records[message.message_id] = message
        budget.consume_index_units(len(records) * len(records).bit_length())
        self._rows = tuple(sorted(records.values(), key=order_key))
        self._keys = tuple(map(order_key, self._rows))
        self._snapshot = make_snapshot(scope, records, generation=generation)
        budget.consume_index_units(0)

    @property
    def snapshot(self) -> SessionSnapshot:
        return self._snapshot

    def read_page(self, *, scope: SessionScope, page_size: int, budget: BudgetState,
                  cursor: PageCursor | None = None, now: float | None = None) -> TracePage:
        _check_time(scope, now)
        require(self.snapshot.scope_fingerprint == scope.fingerprint, "page_scope")
        require(type(page_size) is int and page_size > 0, "page_size")
        budget.consume_index_units(1)  # Page access, including terminal empty page.
        start = 0
        if cursor is not None:
            require(isinstance(cursor, PageCursor) and cursor.snapshot == self.snapshot, "page_cursor")
            start = bisect_left(self._keys, cursor.after)
            require(start < len(self._keys) and self._keys[start] == cursor.after, "page_cursor")
            start += 1
        end = min(start + page_size, len(self._rows))
        for index in range(start, end):
            budget.consume_index_units(trace_work(self._rows[index]))
        items = self._rows[start:end]
        next_cursor = PageCursor(self.snapshot, self._keys[end - 1]) if end < len(self._rows) else None
        return TracePage(self.snapshot, items, next_cursor)


def read_history(store: FixtureTraceStore, *, scope: SessionScope, page_size: int,
                 budget: BudgetState, now: float | None = None) -> HistoryRead:
    """Return a complete pinned read, then authorize dependency closure globally.

    A budget/contract failure raises without returning a partial successful read.
    Reading/indexing does not mark messages as visited for answer synthesis.
    """
    snapshot = store.snapshot
    records: dict[str, TraceMessage] = {}
    cursor = None
    previous = None
    while True:
        page = store.read_page(scope=scope, page_size=page_size, budget=budget, cursor=cursor, now=now)
        require(page.snapshot == snapshot, "page_snapshot_changed")
        require(len(page.items) <= page_size, "page_size")
        for message in page.items:
            key = order_key(message)
            require(message.key == scope.key and message.message_id not in records, "page_identity")
            require(previous is None or previous < key, "page_order")
            records[message.message_id] = message
            previous = key
        if page.next_cursor is None:
            break
        require(bool(page.items) and page.next_cursor.snapshot == snapshot
                and page.next_cursor.after == previous, "page_progress")
        cursor = page.next_cursor
    for message in records.values():
        budget.consume_index_units(trace_work(message))  # Final digest and ACL/edge scan.
    require(store.snapshot == snapshot and make_snapshot(scope, records, generation=snapshot.generation) == snapshot,
            "read_snapshot_changed")
    eligible = eligible_message_ids(scope, records, now=now)
    messages, ordinals = [], []
    for ordinal, message in enumerate(records.values()):
        if message.message_id in eligible:
            messages.append(message)
            ordinals.append(ordinal)
    budget.consume_index_units(0)
    return HistoryRead(snapshot, tuple(messages), tuple(ordinals), Coverage(eligible, frozenset()),
                       len(records) - len(eligible), any(m.provenance == "unknown" or m.partition_fingerprint is None
                                                        for m in records.values()))
