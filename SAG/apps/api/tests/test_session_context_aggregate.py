"""Bounded synthesis contracts; the semantic model double is not quality acceptance."""

import asyncio
import json
from dataclasses import replace

import pytest
import test_session_context_storage as storage_fixtures
from test_session_context_runtime import Counter, principal
from test_session_context_storage import raw

from sag_api.enums import MessageRole
from sag_api.services.session_context.aggregate import aggregate_session
from sag_api.services.session_context.budget import TokenUsage
from sag_api.services.session_context.runtime import prepare_context

storage_db = storage_fixtures.storage_db
QUERY = "List all decisions from the entire session."


async def full_context(factory, scope, *, unknown_assistant=False, oversized_middle=False):
    async with factory() as session:
        for index in range(8):
            content = (f"DECISION D-{index}: choose option {index}.\n" if index in {0, 3, 7} else "Status note.\n")
            content += "Routine operational note without a decision. " * 6
            if oversized_middle and index == 3:
                content += "A message too large for this test's forced map input window. " * 80
            session.add(raw(f"m{index:03}", content=content, ordinal=index))
        if unknown_assistant:
            session.add(raw("unknown-assistant", role=MessageRole.ASSISTANT, ordinal=8))
        user = raw("current", content=QUERY, ordinal=9)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return await prepare_context(session, key=scope.key, principal=principal(scope), user_message=user,
                                     query=QUERY, mode="full_session", counter=Counter())


class SemanticModel:
    """Exact fixture semantics and real budget accounting at each fake dispatch."""

    def __init__(self, context, *, corrupt_quote=False, missing_usage=False, cancel=False, unvisited_support=False):
        self.context = context
        self.corrupt_quote = corrupt_quote
        self.missing_usage = missing_usage
        self.cancel = cancel
        self.unvisited_support = unvisited_support
        self.requests = []
        self.map_batches = []
        self.reduce_items = []

    async def complete(self, messages, **kwargs):
        assert messages[-1]["content"] == QUERY, "the current query must remain separate and unchanged"
        count = self.context.counter.count(messages)
        reservation = self.context.budget.reserve_call(input_tokens=count)
        self.requests.append(messages)
        if self.cancel:
            self.context.budget.settle_call(reservation, None)
            raise asyncio.CancelledError
        content = messages[-2]["content"]
        prefix, payload = content.split("\n", 1)
        payload = json.loads(payload)
        if prefix == "SESSION_EVIDENCE_JSON":
            self.map_batches.append(payload)
            items = []
            for item in payload:
                if item["text"].startswith("DECISION"):
                    quote = item["text"].split("\n", 1)[0]
                    support = {"id": item["id"], "quote": "invalid raw quote" if self.corrupt_quote else quote}
                    items.append({"text": quote, "supports": [support]})
            output = {"items": items}
        else:
            assert prefix == "GENERATED_ITEMS_JSON"
            self.reduce_items = payload
            output = {"answer": "\n".join(item["text"] for item in payload),
                      "supports": [support for item in payload for support in item["supports"]]}
            if self.unvisited_support:
                # Adversarial model output: this quote exists in the authorized
                # snapshot, but was never mapped or sent as a reduce item.
                quote = self.context.original[-1].text.split("\n", 1)[0]
                output = {"answer": quote, "supports": [{"id": "S8", "quote": quote}]}
        self.context.budget.settle_call(
            reservation, None if self.missing_usage else TokenUsage(count, 20))
        return json.dumps(output)


def assert_visible_partial(result):
    assert result.complete is False and result.reason
    assert any(word in result.answer[:160].casefold() for word in ("partial", "chưa", "một phần", "không đầy đủ"))


@pytest.mark.asyncio
async def test_full_traversal_reaches_decision_in_last_batch_with_stable_global_ids(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    assert not context.selection.coverage.visited_ids
    model = SemanticModel(context)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert result.complete is True and result.reason is None
    assert result.visited_ids == context.selection.coverage.eligible_ids
    assert len(model.map_batches) >= 2
    transmitted = [item for batch in model.map_batches for item in batch]
    assert {item["id"] for item in transmitted} == {f"S{i}" for i in range(1, 9)}
    originals = {f"S{i}": item for i, item in enumerate(context.original, start=1)}
    for item in transmitted:
        assert item["message_id"] == originals[item["id"]].span.ref.message_id
        assert item["text"] == originals[item["id"]].text
    assert len(result.visited_ids) == 8  # Overlapping transmissions do not inflate coverage.
    assert model.map_batches[-1][-1]["message_id"] == "m007"
    assert all(f"D-{index}" in result.answer for index in (0, 3, 7))
    assert len(model.reduce_items) == 3 and {item["id"] for item in result.supports} == {"S1", "S4", "S8"}
    assert context.budget.calls == len(model.requests) <= 12
    assert all(context.counter.count(messages) <= 600 for messages in model.requests)


@pytest.mark.asyncio
async def test_call_budget_exhaustion_is_visible_partial_and_never_claims_full_coverage(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    context.budget.limits = replace(context.budget.limits, max_calls=1)
    model = SemanticModel(context)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert len(model.requests) == context.budget.calls == 1
    assert result.visited_ids < context.selection.coverage.eligible_ids


@pytest.mark.asyncio
async def test_corrupt_support_quote_does_not_mark_the_batch_as_visited(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, corrupt_quote=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    invalid_batch = {item["message_id"] for item in model.map_batches[0]}
    assert not (result.visited_ids & invalid_batch)
    assert result.visited_ids != context.selection.coverage.eligible_ids


@pytest.mark.asyncio
async def test_missing_usage_prevents_any_next_paid_dispatch_and_full_success(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, missing_usage=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert len(model.requests) == context.budget.calls == 1
    assert context.budget.remaining_tokens is None


@pytest.mark.asyncio
async def test_cancellation_never_publishes_a_false_complete_result(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, cancel=True)
    try:
        result = await aggregate_session(context, model, QUERY, input_limit=600)
    except asyncio.CancelledError:
        result = None
    if result is not None:
        assert_visible_partial(result)
    assert len(model.requests) == 1
    assert context.selection.state != "ready"
    assert not context.selection.coverage.visited_ids


@pytest.mark.asyncio
async def test_unknown_legacy_assistant_keeps_full_session_result_partial(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope, unknown_assistant=True)
    model = SemanticModel(context)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert "unknown-assistant" not in result.visited_ids
    assert context.indexed.history.unknown_provenance


@pytest.mark.asyncio
async def test_reduce_cannot_publish_support_from_unvisited_original_outside_map_items(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope, oversized_middle=True)
    model = SemanticModel(context, unvisited_support=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert "m007" not in result.visited_ids and model.reduce_items
    known_ids = {f"S{i}" for i, item in enumerate(context.original, start=1)
                 if item.span.ref.message_id in result.visited_ids}
    assert {support["id"] for support in result.supports} <= known_ids
    assert "D-7" not in result.answer
