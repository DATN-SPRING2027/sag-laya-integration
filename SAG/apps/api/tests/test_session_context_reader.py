"""Fixture reader invariants; no live database or provider acceptance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sag_api.services.session_context.budget import BudgetExceeded, BudgetLimits, BudgetState
from sag_api.services.session_context.reader import FixtureTraceStore, read_history
from sag_api.services.session_context.types import ContextContractError, SessionKey, SessionScope, TraceMessage


def authority():
    return SessionScope(SessionKey("owner", "agent", "thread"), "issuer", "subject", "org", "tenant",
                        frozenset({"project"}), frozenset(), frozenset({"source"}), None, 2000)


def work(limit=100000, *, clock=lambda: 1):
    return BudgetState(BudgetLimits(100, 10, 2, 200, limit, 10), clock=clock)


def rows(count):
    scope = authority()
    return tuple(TraceMessage(scope.key, f"m{i:05}", "user", f"🙂 Quyết định {i}.",
                              datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
                              provenance="recorded", partition_fingerprint=scope.partition_fingerprint)
                 for i in range(count))


@pytest.mark.parametrize("size", [1, 8, 37, 200, 1000])
def test_reads_entire_history_without_duplicates_or_last_n_limit(size):
    original = rows(431)
    budget = work()
    store = FixtureTraceStore(tuple(reversed(original)), scope=authority(), generation="g1", budget=budget)
    result = read_history(store, scope=authority(), page_size=size, budget=budget, now=100)
    assert result.messages == original
    assert result.ordinals == tuple(range(431))
    assert result.snapshot == store.snapshot
    assert result.coverage.eligible_ids == frozenset(m.message_id for m in original)
    assert not result.coverage.visited_ids  # Reading is not successful synthesis.
    assert budget.calls == 0
    assert budget.index_units > len(original)


def test_keyset_ties_use_id_and_utc_order_and_cursors_bind_snapshot_and_scope():
    original = rows(3)
    same_instant = original[0].created_at.astimezone(timezone(timedelta(hours=7)))
    original = tuple(replace(m, created_at=same_instant) for m in original)
    budget = work()
    store = FixtureTraceStore(tuple(reversed(original)), scope=authority(), generation="g1", budget=budget)
    first = store.read_page(scope=authority(), page_size=1, budget=budget, now=100)
    second = store.read_page(scope=authority(), page_size=2, cursor=first.next_cursor, budget=budget, now=100)
    assert first.items + second.items == original
    assert second.next_cursor is None
    newer = FixtureTraceStore(original, scope=authority(), generation="g2", budget=budget)
    with pytest.raises(ContextContractError, match="cursor"):
        newer.read_page(scope=authority(), page_size=1, cursor=first.next_cursor, budget=budget, now=100)
    with pytest.raises(ContextContractError, match="scope"):
        store.read_page(scope=replace(authority(), source_ids=frozenset()), page_size=1, budget=budget, now=100)


def test_prefilter_closes_dependencies_before_hierarchy_and_preserves_denied_gaps():
    user, source, assistant, legacy = rows(4)
    source = replace(source, source_ids=frozenset({"source"}))
    assistant = replace(assistant, role="assistant", dependencies=(source.reference,))
    legacy = replace(legacy, provenance="unknown", partition_fingerprint=None)
    revoked = replace(authority(), source_ids=frozenset())
    budget = work()
    store = FixtureTraceStore((user, source, assistant, legacy), scope=revoked, generation="g1", budget=budget)
    result = read_history(store, scope=revoked, page_size=1, budget=budget, now=100)
    assert result.messages == (user,)
    assert result.ordinals == (0,)
    assert result.excluded_count == 3
    assert result.unknown_provenance


def test_dependency_on_a_later_page_is_checked_after_complete_read():
    assistant, user, source = rows(3)
    source = replace(source, source_ids=frozenset({"source"}))
    assistant = replace(assistant, role="assistant", dependencies=(source.reference,))
    scope = replace(authority(), source_ids=frozenset())
    budget = work()
    store = FixtureTraceStore((assistant, user, source), scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=1, budget=budget, now=100)
    assert history.messages == (user,)
    assert history.ordinals == (1,)


def test_empty_thread_is_complete_read_without_fabricated_message():
    budget = work()
    store = FixtureTraceStore((), scope=authority(), generation="g1", budget=budget)
    result = read_history(store, scope=authority(), page_size=2, budget=budget, now=100)
    assert result.messages == result.ordinals == ()
    assert not result.unknown_provenance


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_page_size_cannot_silently_change_read_scope(size):
    budget = work()
    store = FixtureTraceStore(rows(1), scope=authority(), generation="g1", budget=budget)
    with pytest.raises(ContextContractError):
        read_history(store, scope=authority(), page_size=size, budget=budget, now=100)


def test_duplicate_ids_and_cross_thread_input_are_rejected():
    item = rows(1)[0]
    with pytest.raises(ContextContractError):
        FixtureTraceStore((item, item), scope=authority(), generation="g1", budget=work())
    other = replace(item, key=SessionKey("owner", "agent", "other"))
    with pytest.raises(ContextContractError):
        FixtureTraceStore((other,), scope=authority(), generation="g1", budget=work())


def test_budget_covers_cold_build_pages_and_long_content_without_returning_partial_success():
    with pytest.raises(BudgetExceeded):
        FixtureTraceStore(rows(2), scope=authority(), generation="g1", budget=work(1))
    budget = work()
    store = FixtureTraceStore(rows(3), scope=authority(), generation="g1", budget=budget)
    bounded = work(3)
    with pytest.raises(BudgetExceeded):
        read_history(store, scope=authority(), page_size=1, budget=bounded, now=100)
    assert bounded.calls == 0
    long_message = replace(rows(1)[0], content="🙂" * 10000)
    with pytest.raises(BudgetExceeded):
        FixtureTraceStore((long_message,), scope=authority(), generation="g1", budget=work(5))


def test_cancel_deadline_and_expiry_stop_reading():
    budget = work()
    store = FixtureTraceStore(rows(1), scope=authority(), generation="g1", budget=budget)
    budget.cancel()
    with pytest.raises(BudgetExceeded, match="cancelled"):
        read_history(store, scope=authority(), page_size=1, budget=budget, now=100)
    with pytest.raises(BudgetExceeded, match="deadline"):
        read_history(store, scope=authority(), page_size=1, budget=work(clock=lambda: 10), now=100)
    with pytest.raises(ContextContractError, match="expired"):
        read_history(store, scope=authority(), page_size=1, budget=work(), now=2000)


@pytest.mark.parametrize("fault", ["omission", "snapshot", "duplicate", "reorder", "no_progress"])
def test_corrupted_or_incomplete_page_stream_never_becomes_a_successful_read(fault):
    class BrokenStore(FixtureTraceStore):
        def read_page(self, **kwargs):
            page = super().read_page(**kwargs)
            if fault == "snapshot":
                return replace(page, snapshot=replace(page.snapshot, generation="changed"))
            if fault == "omission":
                return replace(page, items=page.items[:-1], next_cursor=None)
            if fault == "duplicate":
                return replace(page, items=(page.items[0], page.items[0]))
            if fault == "reorder":
                return replace(page, items=tuple(reversed(page.items)))
            return replace(page, items=())

    budget = work()
    store = BrokenStore(rows(4), scope=authority(), generation="g1", budget=budget)
    with pytest.raises(ContextContractError):
        read_history(store, scope=authority(), page_size=2, budget=budget, now=100)
