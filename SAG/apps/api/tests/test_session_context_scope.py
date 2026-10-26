"""Internal contracts only: these tests do not activate session recall or verify JWTs."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.errors import NotFoundError, ServiceUnavailableError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Agent, Thread
from sag_api.services.session_context import scope as scope_module
from sag_api.services.session_context.budget import BudgetExceeded, BudgetLimits, BudgetState, TokenUsage
from sag_api.services.session_context.scope import (
    eligible_message_ids,
    make_snapshot,
    resolve_scope,
    validate_selection,
)
from sag_api.services.session_context.types import (
    ContextContractError,
    ContextSelection,
    Coverage,
    EvidenceSpan,
    MessageReference,
    SessionKey,
    SessionScope,
    TraceMessage,
)

KEY = SessionKey("owner", "agent", "thread")
NOW = 100.0


def scope(**changes):
    value = SessionScope(KEY, "issuer", "subject", "org", "tenant", frozenset({"project"}),
                         frozenset({"partition"}), frozenset({"source"}), None, 2000)
    return replace(value, **changes)


def message(name="u1", **changes):
    value = TraceMessage(KEY, name, "user", "Tôi chốt phương án B.", datetime(2026, 10, 1, tzinfo=UTC),
                         "ok", "recorded", frozenset(), (), scope().partition_fingerprint)
    return replace(value, **changes)


def selection(records, current_scope=None, *, root="u1", mode="normal", state="ready", reason=None, visited=None):
    current_scope = current_scope or scope()
    eligible = eligible_message_ids(current_scope, records, now=NOW)
    snapshot = make_snapshot(current_scope, records, generation="offline-generation-1")
    coverage = Coverage(eligible, eligible if visited is None else visited)
    return ContextSelection(snapshot, mode, state,
                            (EvidenceSpan(records[root].reference, 0, len(records[root].content)),), coverage, reason)


def validate(value, records, current_scope=None, current_snapshot=None):
    current_scope = current_scope or scope()
    return validate_selection(value, scope=current_scope, records=records, now=NOW,
                              current_snapshot=current_snapshot or make_snapshot(
                                  current_scope, records, generation="offline-generation-1"))


def test_user_statement_without_document_source_can_be_session_evidence():
    records = {"u1": message()}
    resolved = validate(selection(records), records)
    assert resolved[0].kind == "user_statement"
    assert resolved[0].text == records["u1"].content
    assert resolved[0].source_ids == frozenset()
    with pytest.raises(FrozenInstanceError):
        records["u1"].role = "assistant"


def test_full_session_cannot_silently_claim_completion_when_legacy_provenance_is_unknown():
    records = {"u1": message(), "legacy": message("legacy", provenance="unknown", partition_fingerprint=None)}
    value = selection(records, mode="full_session")
    with pytest.raises(ContextContractError, match="unknown_provenance"):
        validate(value, records)
    partial = replace(value, state="partial", reason="unknown_provenance")
    assert validate(partial, records)[0].text == records["u1"].content


def test_assistant_history_is_not_promoted_to_user_approval_or_document_fact():
    user = message()
    assistant = message("a1", role="assistant", content="Tôi đề xuất phương án C.", dependencies=(user.reference,))
    records = {"u1": user, "a1": assistant}
    resolved = validate(selection(records, root="a1"), records)
    assert resolved[0].kind == "assistant_statement"
    assert user.reference in resolved[0].dependency_refs


@pytest.mark.parametrize("change", [
    {"key": SessionKey("another-owner", "agent", "thread")},
    {"key": SessionKey("owner", "another-agent", "thread")},
    {"key": SessionKey("owner", "agent", "another-thread")},
    {"organization_id": "another-org"}, {"tenant_id": "another-tenant"},
    {"project_ids": frozenset({"another-project"})},
    {"partition_ids": frozenset({"another-partition"})},
    {"source_ids": frozenset()}, {"requested_source_ids": frozenset()},
])
def test_scope_changes_invalidate_cached_selection(change):
    records = {"u1": message()}
    original = selection(records)
    with pytest.raises(ContextContractError):
        validate(original, records, scope(**change))


@pytest.mark.parametrize("change", [
    {"content": "Tôi đổi sang phương án C."}, {"role": "assistant"}, {"status": "cancelled"},
    {"source_ids": frozenset({"source"})}, {"provenance": "unknown"},
    {"created_at": datetime(2026, 10, 2, tzinfo=UTC)},
])
def test_message_mutation_invalidates_snapshot_even_without_durable_generation(change):
    records = {"u1": message()}
    previous = selection(records)
    current = {"u1": replace(records["u1"], **change)}
    with pytest.raises(ContextContractError):
        validate(previous, current)


def test_deleted_dependency_is_unavailable_and_stale_records_cannot_fake_current_snapshot():
    user = message()
    assistant = message("a1", role="assistant", dependencies=(user.reference,))
    records = {"u1": user, "a1": assistant}
    previous = selection(records, root="a1")
    deleted = {"a1": assistant}
    with pytest.raises(ContextContractError):
        validate(previous, deleted)
    changed = {"u1": replace(user, content="changed"), "a1": assistant}
    with pytest.raises(ContextContractError):
        validate(previous, changed, current_snapshot=previous.snapshot)


def test_transitive_source_revoke_invalidates_assistant_without_direct_citation():
    user = message()
    first = message("a1", role="assistant", source_ids=frozenset({"source"}), dependencies=(user.reference,))
    second = message("a2", role="assistant", dependencies=(first.reference,))
    records = {m.message_id: m for m in (user, first, second)}
    resolved = validate(selection(records, root="a2"), records)
    assert resolved[0].source_ids == frozenset({"source"})
    assert resolved[0].dependency_refs == frozenset(m.reference for m in records.values())
    revoked = scope(source_ids=frozenset())
    assert eligible_message_ids(revoked, records, now=NOW) == frozenset({"u1"})
    with pytest.raises(ContextContractError):
        validate(selection(records, revoked, root="a2"), records, revoked)


def test_new_snapshot_cannot_relabel_old_project_or_unknown_legacy_partition():
    records = {"u1": message()}
    other_project = scope(project_ids=frozenset({"other-project"}))
    assert eligible_message_ids(other_project, records, now=NOW) == frozenset()
    with pytest.raises(ContextContractError):
        validate(selection(records, other_project), records, other_project)
    legacy = {"u1": message(partition_fingerprint=None)}
    assert eligible_message_ids(scope(), legacy, now=NOW) == frozenset()


@pytest.mark.parametrize("change", [{"provenance": "unknown"}, {"status": "failed"}, {"status": "cancelled"}])
def test_unknown_legacy_or_incomplete_assistant_is_not_treated_as_source_free(change):
    records = {"u1": message(), "a1": message("a1", role="assistant", **change)}
    assert eligible_message_ids(scope(), records, now=NOW) == frozenset({"u1"})
    with pytest.raises(ContextContractError):
        validate(selection(records, root="a1"), records)


def test_cycles_and_reference_hash_mismatch_fail_closed():
    first = message("a1", role="assistant", dependencies=(MessageReference("a2", "0" * 64),))
    second = message("a2", role="assistant", dependencies=(first.reference,))
    records = {"a1": first, "a2": second}
    assert eligible_message_ids(scope(), records, now=NOW) == frozenset()
    with pytest.raises(ContextContractError):
        validate(selection(records, root="a1"), records)


def test_cycle_with_valid_content_hashes_cannot_authorize_itself():
    first = message("a1", role="assistant")
    second = message("a2", role="assistant", dependencies=(first.reference,))
    first = replace(first, dependencies=(second.reference,))
    records = {"a1": first, "a2": second}
    assert eligible_message_ids(scope(), records, now=NOW) == frozenset()


def test_snapshot_generation_dependency_metadata_and_partition_stamp_are_bound():
    user = message()
    other = message("u2")
    assistant = message("a1", role="assistant", dependencies=(user.reference,))
    records = {"u1": user, "u2": other, "a1": assistant}
    value = selection(records, root="a1")
    with pytest.raises(ContextContractError):
        validate(value, records, current_snapshot=replace(value.snapshot, generation="new-generation"))
    changed = {**records, "a1": replace(assistant, dependencies=(other.reference,))}
    with pytest.raises(ContextContractError):
        validate(value, changed)


def test_explicit_source_filter_cannot_be_widened_by_a_manually_constructed_scope():
    with pytest.raises(ContextContractError):
        scope(requested_source_ids=frozenset())


def test_long_dependency_chain_is_iterative_and_not_a_last_n_window():
    records = {"u1": message()}
    parent = records["u1"]
    for index in range(1100):
        current = message(f"a{index}", role="assistant", dependencies=(parent.reference,))
        records[current.message_id] = current
        parent = current
    resolved = validate(selection(records, root=parent.message_id), records)
    assert len(resolved[0].dependency_refs) == 1101


def test_unicode_span_offsets_are_codepoints_in_original_text():
    original = message(content="🙂 Tôi chọn B, không chọn C.")
    records = {"u1": original}
    value = replace(selection(records), spans=(EvidenceSpan(original.reference, 2, 12),))
    assert validate(value, records)[0].text == original.content[2:12]
    with pytest.raises(ContextContractError):
        validate(replace(value, spans=(EvidenceSpan(original.reference, 0, 10000),)), records)


def test_full_session_coverage_cannot_call_top_k_or_a_smaller_denominator_complete():
    records = {"u1": message(), "u2": message("u2")}
    value = selection(records, mode="full_session", visited=frozenset({"u1"}))
    with pytest.raises(ContextContractError):
        validate(value, records)
    forged = replace(value, coverage=Coverage(frozenset({"u1"}), frozenset({"u1"})))
    with pytest.raises(ContextContractError):
        validate(forged, records)
    partial = replace(value, state="partial", reason="budget_exhausted")
    assert validate(partial, records)


@pytest.mark.parametrize("state", ["indexing", "unsupported"])
def test_indexing_and_unsupported_states_cannot_carry_usable_evidence(state):
    records = {"u1": message()}
    with pytest.raises(ContextContractError):
        replace(selection(records), state=state, reason="not_ready")


def test_scope_fingerprint_is_order_independent_but_expired_principal_is_not_authority():
    assert scope(project_ids=frozenset(["p2", "p1"])).fingerprint == scope(
        project_ids=frozenset(["p1", "p2"])).fingerprint
    records = {"u1": message()}
    with pytest.raises(ContextContractError):
        validate(selection(records), records, scope(expires_at=NOW))


@pytest.mark.asyncio
async def test_scope_adapter_checks_real_thread_owner_before_source_lookup(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[Agent.__table__, Thread.__table__]))
    lookup = []

    async def authorized(session, *, principal, requested_source_ids):
        lookup.append(tuple(requested_source_ids))
        return {"source", "not-requested-by-server"}

    monkeypatch.setattr(scope_module, "get_authorized_source_ids", authorized)
    principal = VerifiedPrincipal("subject", "org", frozenset({"project"}), "issuer", "key", "token", 0, 2000)
    try:
        async with factory() as session:
            session.add(Agent(id="agent", owner_key="owner", name="Fixture"))
            await session.flush()
            session.add(Thread(id="thread", agent_id="agent", owner_key="owner", title="Fixture"))
            await session.commit()
            with pytest.raises(NotFoundError):
                await resolve_scope(session, key=replace(KEY, owner_key="other"), principal=principal,
                                    candidate_source_ids=frozenset({"source"}), now=NOW)
            assert not lookup
            result = await resolve_scope(session, key=KEY, principal=principal,
                                         candidate_source_ids=frozenset({"source"}), now=NOW)
            assert result.source_ids == frozenset({"source"})
            restricted = await resolve_scope(session, key=KEY, principal=principal,
                                             candidate_source_ids=frozenset({"source"}),
                                             requested_source_ids=frozenset(), now=NOW)
            assert restricted.source_ids == frozenset()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scope_resolver_failure_is_not_an_empty_successful_whitelist(monkeypatch):
    async def thread(*args, **kwargs):
        return None

    async def unavailable(*args, **kwargs):
        raise ServiceUnavailableError("fixture authorization unavailable")

    monkeypatch.setattr(scope_module, "get_thread", thread)
    monkeypatch.setattr(scope_module, "get_authorized_source_ids", unavailable)
    principal = VerifiedPrincipal("subject", "org", frozenset({"project"}), "issuer", "key", "token", 0, 2000)
    with pytest.raises(ServiceUnavailableError):
        await resolve_scope(None, key=KEY, principal=principal, candidate_source_ids=frozenset({"source"}), now=NOW)


def budget(**changes):
    limits = BudgetLimits(context_window_tokens=100, output_reserve_tokens=10, max_calls=2,
                          max_total_tokens=100, max_index_units=5, deadline=10)
    return BudgetState(replace(limits, **changes), clock=lambda: 1)


def test_budget_reserves_before_each_attempt_and_reconciles_provider_usage():
    state = budget()
    first = state.reserve_call(input_tokens=20)
    assert state.remaining_tokens == 70
    state.settle_call(first, TokenUsage(12, 3))
    assert state.known_used_tokens == 15
    assert state.remaining_tokens == 85
    second = state.reserve_call(input_tokens=20)
    state.settle_call(second, TokenUsage(0, 0))
    with pytest.raises(BudgetExceeded, match="calls"):
        state.reserve_call(input_tokens=1)
    with pytest.raises(ContextContractError):
        state.settle_call(first, TokenUsage(1, 1))


def test_unknown_or_partial_usage_is_not_free_and_blocks_more_calls():
    state = budget()
    token = state.reserve_call(input_tokens=20)
    state.settle_call(token, TokenUsage(5, None))
    assert state.known_used_tokens == 5
    assert state.remaining_tokens is None
    assert state.held_unknown_tokens == 30
    with pytest.raises(BudgetExceeded, match="unknown_usage"):
        state.reserve_call(input_tokens=1)


def test_a_call_reservation_from_another_request_cannot_settle_this_budget():
    first, second = budget(), budget()
    token = first.reserve_call(input_tokens=20)
    foreign = second.reserve_call(input_tokens=20)
    with pytest.raises(ContextContractError):
        first.settle_call(foreign, TokenUsage(1, 1))
    assert first.remaining_tokens == 70
    first.settle_call(token, TokenUsage(1, 1))


def test_cancel_and_late_usage_do_not_reset_budget_or_hide_billed_work():
    clock = [1]
    state = budget()
    state.clock = lambda: clock[0]
    token = state.reserve_call(input_tokens=20)
    clock[0] = 11
    state.settle_call(token, TokenUsage(12, 3))
    assert state.known_used_tokens == 15
    with pytest.raises(BudgetExceeded, match="deadline"):
        state.reserve_call(input_tokens=0)
    cancelled = budget()
    cancelled.cancel()
    with pytest.raises(BudgetExceeded, match="cancelled"):
        cancelled.consume_index_units(1)


@pytest.mark.parametrize("changes,reason", [
    ({"deadline": 1}, "deadline"), ({"max_total_tokens": 10}, "tokens"),
    ({"context_window_tokens": 20}, "context_window"),
])
def test_budget_rejects_without_starting_a_call(changes, reason):
    state = budget(**changes)
    with pytest.raises(BudgetExceeded, match=reason):
        state.reserve_call(input_tokens=20)
    assert state.calls == 0


def test_index_work_cancel_and_usage_overrun_are_bounded():
    state = budget()
    state.consume_index_units(5)
    with pytest.raises(BudgetExceeded, match="index_units"):
        state.consume_index_units(1)
    token = state.reserve_call(input_tokens=1)
    with pytest.raises(BudgetExceeded, match="overrun"):
        state.settle_call(token, TokenUsage(95, 10))
    assert state.known_used_tokens == 105
    state.cancel()
    with pytest.raises(BudgetExceeded):
        state.reserve_call(input_tokens=0)


@pytest.mark.parametrize("changes", [{"deadline": float("inf")}, {"deadline": float("nan")},
                                     {"max_calls": 0}, {"max_calls": True}, {"output_reserve_tokens": 100}])
def test_limits_are_explicit_finite_and_cannot_be_unlimited(changes):
    with pytest.raises(ContextContractError):
        budget(**changes)
