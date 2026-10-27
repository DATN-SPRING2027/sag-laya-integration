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

    def __init__(self, context, *, corrupt_quote=False, missing_usage=False, cancel=False, unvisited_support=False,
                 reference_only_map=False, reference_only_reduce=False, reduce_id=None, map_id=None,
                 corrupt_reduce_quote=False, string_references=False):
        self.context = context
        self.corrupt_quote = corrupt_quote
        self.missing_usage = missing_usage
        self.cancel = cancel
        self.unvisited_support = unvisited_support
        self.reference_only_map = reference_only_map
        self.reference_only_reduce = reference_only_reduce
        self.reduce_id = reduce_id
        self.map_id = map_id
        self.corrupt_reduce_quote = corrupt_reduce_quote
        self.string_references = string_references
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
                    support = {"id": self.map_id or item["id"]}
                    if self.corrupt_quote or not self.reference_only_map:
                        support["quote"] = "invalid raw quote" if self.corrupt_quote else quote
                    items.append({"text": quote, "supports": [support]})
            output = {"items": items}
        else:
            assert prefix == "GENERATED_ITEMS_JSON"
            self.reduce_items = payload
            output = {"answer": "\n".join(item["text"] for item in payload),
                      "supports": [support for item in payload for support in item["supports"]]}
            if self.reference_only_reduce:
                output["supports"] = [{"id": support["id"]} for support in output["supports"]]
            if self.reduce_id:
                output = {"answer": "UNMAPPED-REDUCE-CLAIM", "supports": [{"id": self.reduce_id}]}
            if self.corrupt_reduce_quote:
                output = {"answer": "INVALID-QUOTE-CLAIM",
                          "supports": [{"id": payload[0]["supports"][0]["id"], "quote": "invalid raw quote"}]}
            if self.unvisited_support:
                # Adversarial model output: this quote exists in the authorized
                # snapshot, but was never mapped or sent as a reduce item.
                quote = self.context.original[-1].text.split("\n", 1)[0]
                output = {"answer": quote, "supports": [{"id": "S8", "quote": quote}]}
        if self.string_references:
            for item in output.get("items", [output]):
                item["supports"] = [support["id"] if "quote" not in support else support
                                    for support in item["supports"]]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_only_map", [True, False])
@pytest.mark.parametrize("string_references", [False, True])
async def test_id_only_reduce_uses_exact_canonical_mapped_pairs_not_expanded_original_span(storage_db,
                                                                                        reference_only_map,
                                                                                        string_references):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, reference_only_map=reference_only_map, reference_only_reduce=True,
                          string_references=string_references)
    result = await aggregate_session(context, model, QUERY, input_limit=1500)
    assert result.complete is True and result.reason is None
    assert model.reduce_items
    expected = {(support["id"], support["quote"]) for item in model.reduce_items for support in item["supports"]}
    assert {(support["id"], support["quote"]) for support in result.supports} == expected
    assert all(f"D-{index}" in result.answer for index in (0, 3, 7))
    known = {f"S{i}": item for i, item in enumerate(context.original, start=1)}
    for support in result.supports:
        assert support["quote"] in known[support["id"]].text
        if reference_only_map:
            assert support["quote"] == known[support["id"]].text
        else:
            assert support["quote"] != known[support["id"]].text


@pytest.mark.asyncio
@pytest.mark.parametrize("reduce_id", ["S999", "S2"])
@pytest.mark.parametrize("string_references", [False, True])
async def test_id_only_reduce_rejects_unknown_or_visited_but_unmapped_reference(storage_db, reduce_id,
                                                                             string_references):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, reference_only_map=True, reference_only_reduce=True, reduce_id=reduce_id,
                          string_references=string_references)
    result = await aggregate_session(context, model, QUERY, input_limit=1500)
    assert_visible_partial(result)
    assert result.visited_ids == context.selection.coverage.eligible_ids
    assert model.reduce_items
    assert result.reason == "synthesis_not_ready"
    assert "UNMAPPED-REDUCE-CLAIM" not in result.answer
    assert reduce_id not in {support["id"] for support in result.supports}


@pytest.mark.asyncio
@pytest.mark.parametrize("string_references", [False, True])
async def test_id_only_map_cannot_reference_authorized_message_outside_current_batch(storage_db, string_references):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, reference_only_map=True, reference_only_reduce=True, map_id="S8",
                          string_references=string_references)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert len(model.map_batches) == 1 and "S8" not in {item["id"] for item in model.map_batches[0]}
    assert not result.visited_ids and not result.supports
    assert result.reason == "map_not_ready"


@pytest.mark.asyncio
async def test_reference_only_protocol_never_repairs_explicit_invalid_reduce_quote(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)
    model = SemanticModel(context, reference_only_map=True, reference_only_reduce=True, corrupt_reduce_quote=True)
    result = await aggregate_session(context, model, QUERY, input_limit=1500)
    assert_visible_partial(result)
    assert "INVALID-QUOTE-CLAIM" not in result.answer
    assert model.reduce_items
    assert all(support["quote"] != "invalid raw quote" for support in result.supports)
    assert result.reason == "synthesis_not_ready"


@pytest.mark.asyncio
async def test_fallback_on_failed_reduce_uses_verified_quotes_not_untrusted_map_absence_text(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)

    class HallucinatedMapTextModel(SemanticModel):
        async def complete(self, messages, **kwargs):
            raw_out = await super().complete(messages, **kwargs)
            prefix, _ = messages[-2]["content"].split("\n", 1)
            if prefix == "SESSION_EVIDENCE_JSON":
                data = json.loads(raw_out)
                if data["items"]:
                    data["items"][0]["text"] = "UNVERIFIED_GLOBAL_ABSENCE: Không có quyết định nào trong toàn phiên."
                return json.dumps(data)
            return "invalid-reduce-json"

    model = HallucinatedMapTextModel(context, reference_only_map=True)
    result = await aggregate_session(context, model, QUERY, input_limit=1500)
    assert_visible_partial(result)
    assert result.reason == "synthesis_not_ready"
    assert "UNVERIFIED_GLOBAL_ABSENCE" not in result.answer
    assert "DECISION D-0" in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_item",
    [
        {"text": "Unsupported commentary", "supports": []},
        {"text": "Unknown support id", "supports": [{"id": "S999"}]},
        {"text": "Out of batch support id", "supports": [{"id": "S8"}]},
        {"text": "Bad quote", "supports": [{"id": "S1", "quote": "not-in-source"}]},
    ],
)
async def test_map_batch_with_unsupported_item_keeps_valid_items_and_marks_partial(storage_db, bad_item):
    factory, scope = storage_db
    context = await full_context(factory, scope)

    class MixedMapBatchModel(SemanticModel):
        async def complete(self, messages, **kwargs):
            raw_out = await super().complete(messages, **kwargs)
            prefix, _ = messages[-2]["content"].split("\n", 1)
            if prefix == "SESSION_EVIDENCE_JSON" and len(self.map_batches) == 1:
                data = json.loads(raw_out)
                data["items"].append(bad_item)
                return json.dumps(data)
            return raw_out

    model = MixedMapBatchModel(context, reference_only_map=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert result.reason == "map_not_ready"
    assert all(f"D-{index}" in result.answer for index in (0, 3, 7))
    assert "Unsupported commentary" not in result.answer
    assert "Unknown support id" not in result.answer
    assert "Out of batch support id" not in result.answer
    assert "Bad quote" not in result.answer
    first_batch_ids = {item["message_id"] for item in model.map_batches[0]}
    assert "m000" in result.visited_ids
    assert "m001" in first_batch_ids and "m001" not in result.visited_ids
    assert result.visited_ids < context.selection.coverage.eligible_ids


@pytest.mark.asyncio
async def test_second_map_batch_with_all_unsupported_items_preserves_first_batch_valid_items(storage_db):
    factory, scope = storage_db
    context = await full_context(factory, scope)

    class SecondBatchAllUnsupportedModel(SemanticModel):
        async def complete(self, messages, **kwargs):
            raw_out = await super().complete(messages, **kwargs)
            prefix, _ = messages[-2]["content"].split("\n", 1)
            if prefix == "SESSION_EVIDENCE_JSON" and len(self.map_batches) == 2:
                return json.dumps({"items": [{"text": "No valid support", "supports": []}]})
            return raw_out

    model = SecondBatchAllUnsupportedModel(context, reference_only_map=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert result.reason == "map_not_ready"
    assert len(model.map_batches) == 2
    assert "D-0" in result.answer
    second_batch_only = {item["message_id"] for item in model.map_batches[1]} - {
        item["message_id"] for item in model.map_batches[0]
    }
    assert not (result.visited_ids & second_batch_only)
    assert "m000" in result.visited_ids


@pytest.mark.asyncio
async def test_large_multi_message_map_batches_are_bounded_and_reserve_one_call_for_reduce(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        for index in range(120):
            content = (
                f"DECISION D-{index:03d}: approve milestone {index}.\n"
                if index in {10, 50, 110}
                else f"Operational filler {index}. "
            )
            content += "Routine status update without policy changes. " * 25
            session.add(raw(f"long-{index:03d}", content=content, ordinal=index + 20))
        user = raw("current-long", content=QUERY, ordinal=200)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=QUERY,
            mode="full_session",
            counter=Counter(),
        )
    context.budget.limits = replace(context.budget.limits, max_calls=3)
    model = SemanticModel(context, reference_only_map=True)
    result = await aggregate_session(context, model, QUERY, input_limit=28672)
    assert_visible_partial(result)
    assert result.reason == "calls"
    assert len(model.map_batches) == 2
    assert model.reduce_items
    for req in model.requests[:-1]:
        assert context.counter.count(req) <= 10240
    assert "D-010" in result.answer and "D-050" in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_stage", ["second_map", "reduce"])
async def test_provider_timeout_on_second_map_or_reduce_returns_visible_partial(storage_db, fail_stage):
    import httpx

    factory, scope = storage_db
    context = await full_context(factory, scope)

    class TimeoutLaterModel(SemanticModel):
        async def complete(self, messages, **kwargs):
            prefix, _ = messages[-2]["content"].split("\n", 1)
            if prefix == "SESSION_EVIDENCE_JSON" and len(self.map_batches) >= 1 and fail_stage == "second_map":
                count = self.context.counter.count(messages)
                reservation = self.context.budget.reserve_call(input_tokens=count)
                self.context.budget.settle_call(reservation, None)
                raise httpx.ReadTimeout("provider read timed out")
            if prefix == "GENERATED_ITEMS_JSON" and fail_stage == "reduce":
                count = self.context.counter.count(messages)
                reservation = self.context.budget.reserve_call(input_tokens=count)
                self.context.budget.settle_call(reservation, None)
                raise httpx.ReadTimeout("provider reduce timed out")
            return await super().complete(messages, **kwargs)

    model = TimeoutLaterModel(context, reference_only_map=True)
    result = await aggregate_session(context, model, QUERY, input_limit=600)
    assert_visible_partial(result)
    assert result.reason == ("map_not_ready" if fail_stage == "second_map" else "synthesis_not_ready")
    assert "DECISION D-0" in result.answer
