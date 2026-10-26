"""Baseline evidence and accounting must remain separate from model quality claims."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import UpstreamError
from sag_api.generation import llm as llm_module
from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM

SCRIPT = Path(__file__).parents[1] / "scripts" / "eval_session_context.py"
spec = importlib.util.spec_from_file_location("eval_session_context", SCRIPT)
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)


def config():
    return Settings(_env_file=None, llm_api_key="offline-test", llm_model="offline-model")


@pytest.mark.asyncio
async def test_complete_and_stream_both_count_and_stream_usage_is_not_double_counted(monkeypatch):
    async def completion(**kwargs):
        if not kwargs.get("stream"):
            return {"choices": [{"message": {"content": "summary"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3}}
        assert kwargs["stream_options"] == {"include_usage": True}

        async def chunks():
            for _ in range(2):
                yield {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 4}}
        return chunks()

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    ledger = CallLedger(max_calls=5, max_seconds=10)
    client = TrackedLLM(config(), ledger)
    ledger.stage = "compression"
    assert await client.complete([{"role": "user", "content": "synthetic"}]) == "summary"
    ledger.stage = "generation"
    stream = await client._create_completion([{"role": "user", "content": "synthetic"}], stream=True)
    async for _ in stream:
        pass
    result = ledger.report()
    assert result["logical_calls"] == 2
    assert result["known_input_tokens"] == 31
    assert result["known_output_tokens"] == 7
    assert [call["stage"] for call in result["calls"]] == ["compression", "generation"]
    assert "synthetic" not in str(result)


@pytest.mark.asyncio
async def test_stream_usage_option_is_opt_in_so_production_defaults_do_not_change(monkeypatch):
    observed = []

    async def completion(**kwargs):
        observed.append(kwargs)
        return None

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    await llm_module.LLMClient(config())._create_completion([], stream=True)
    assert "stream_options" not in observed[0]
    await llm_module.LLMClient(config(), include_stream_usage=True)._create_completion([], stream=True)
    assert observed[1]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_missing_usage_and_failure_are_unknown_not_zero(monkeypatch):
    async def completion(**kwargs):
        if kwargs["messages"][0]["content"] == "fail":
            raise RuntimeError("secret-that-must-not-be-recorded")
        return {"choices": [{"message": {"content": "answer"}}]}

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    ledger = CallLedger(max_calls=4, max_seconds=10)
    client = TrackedLLM(config(), ledger)
    await client.complete([{"role": "user", "content": "success without usage"}])
    with pytest.raises(UpstreamError):
        await client.complete([{"role": "user", "content": "fail"}])
    report = ledger.report()
    assert report["input_tokens"] is None
    assert report["output_tokens"] is None
    assert report["accounting_complete"] is False
    assert report["calls"][1]["status"] == "failed"
    assert "secret-that-must-not-be-recorded" not in str(report)


@pytest.mark.asyncio
async def test_budget_refuses_before_call(monkeypatch):
    called = []

    async def completion(**kwargs):
        called.append(1)
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    ledger = CallLedger(max_calls=1, max_seconds=10)
    client = TrackedLLM(config(), ledger)
    await client.complete([{"role": "user", "content": "a"}])
    with pytest.raises(UpstreamError):
        await client.complete([{"role": "user", "content": "b"}])
    assert len(called) == 1


def test_deployment_retry_observation_never_implies_failed_tokens_are_free():
    ledger = CallLedger(max_calls=5, max_seconds=10)
    record = ledger.begin(stream=False, estimated_input=1, max_output=4)
    ledger.observe_attempt(record)
    ledger.observe_failure(record)
    ledger.observe_attempt(record)
    ledger.usage(record, {"prompt_tokens": 5, "completion_tokens": 2})
    record["status"] = "completed"
    result = ledger.report()
    assert result["calls"][0]["observed_deployment_attempts"] == 2
    assert result["accounting_complete"] is False
    assert result["input_tokens"] is None
    assert result["known_input_tokens"] == 5


@pytest.mark.asyncio
async def test_early_stream_close_keeps_usage_unknown_and_releases_upstream(monkeypatch):
    closed = []

    async def completion(**kwargs):
        async def chunks():
            try:
                yield {"choices": [{"delta": {"content": "partial"}}]}
                yield {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}
            finally:
                closed.append(True)
        return chunks()

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    ledger = CallLedger(max_calls=3, max_seconds=10)
    stream = await TrackedLLM(config(), ledger)._create_completion([], stream=True)
    await anext(stream)
    await stream.aclose()
    assert closed == [True]
    assert ledger.calls[0]["status"] == "cancelled"
    assert ledger.report()["input_tokens"] is None


def test_callback_registration_is_removed_even_when_replay_raises():
    import litellm

    previous = list(litellm.callbacks)
    ledger = CallLedger(max_calls=1, max_seconds=1)
    with pytest.raises(ValueError, match="synthetic failure"):
        with ledger.capture_deployments():
            assert len(litellm.callbacks) == len(previous) + 1
            raise ValueError("synthetic failure")
    assert litellm.callbacks == previous


def test_invalid_usage_is_unknown_instead_of_coerced_into_billed_tokens():
    ledger = CallLedger(max_calls=1, max_seconds=1)
    record = ledger.begin(stream=False, estimated_input=1, max_output=1)
    ledger.usage(record, {"prompt_tokens": True, "completion_tokens": -1})
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None


def test_corpus_is_reproducible_gold_is_valid_and_holdout_is_not_selected():
    cases = baseline.load_cases()
    assert len(cases) >= 20
    selected = [case for case in cases if case["split"] == "calibration"]
    assert selected and len(selected) < len(cases)
    for case in cases:
        first = baseline.expand_case(case)
        assert first == baseline.expand_case(case)
        assert len({message["id"] for message in first}) == len(first)
        for item in case["gold_items"]:
            for evidence in item["evidence"]:
                assert evidence["quote"] in first[evidence["at"]]["content"]
    assert max(len(baseline.expand_case(case)) for case in selected) > 200
    compressed = [case for case in selected if case["scenario"].startswith("compression")]
    assert compressed
    from sag_api.generation.prompt import estimate_tokens
    for case in compressed:
        assert sum(estimate_tokens(m["content"]) for m in baseline.expand_case(case)[-200:]) > 51200


def test_literal_grader_does_not_call_substring_evidence_recall():
    case = {"gold_items": [{"id": "d", "aliases": ["PORT-862"], "evidence": []}]}
    score = baseline.score_answer(case, "Tôi đề xuất PORT-862 nhưng chưa xác nhận.")
    assert score["literal_item_recall"] == 1
    assert score["semantic_correctness"] == "requires_review"
    assert baseline.score_answer(case, "Không biết")["literal_item_recall"] == 0


def test_only_exhaustive_requests_are_grouped_as_full_session():
    for case in baseline.load_cases():
        if case["scenario"] == "approval_distinction":
            assert case["group"] == "normal"
        if case["group"] == "full_session":
            assert "tất cả" in case["query"]


@pytest.mark.asyncio
async def test_disposable_replay_uses_real_history_limit_and_has_no_gold_in_prompt(monkeypatch):
    async def completion(**kwargs):
        text = str(kwargs["messages"])
        assert "gold_items" not in text
        assert "only-in-gold" not in text
        if kwargs.get("stream"):
            async def chunks():
                yield {"choices": [{"delta": {"content": "UNKNOWN"}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 1}}
            return chunks()
        return {"choices": [{"message": {"content": "UNKNOWN"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1}}

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    case = {"id": "offline-test", "split": "calibration", "language": "vi", "group": "normal",
            "scenario": "outside_window", "message_count": 220, "filler_repeat": 1,
            "facts": [{"at": 0, "role": "user", "content": "Chốt mã OLD-724."}],
            "query": "Viết lại mã đã chốt. Nếu thiếu, trả UNKNOWN.",
            "gold_items": [{"id": "d", "aliases": ["only-in-gold"],
                            "evidence": [{"at": 0, "quote": "OLD-724"}]}]}
    ledger = CallLedger(max_calls=5, max_seconds=20)
    result = await baseline.replay_case(case, TrackedLLM(config(), ledger), ledger, web_enabled=True)
    assert result["history_message_count"] == 220
    assert result["loaded_message_count"] == 200
    assert result["gold_verbatim_in_initial_context"] == 0
    assert result["literal_item_recall"] == 0
    assert result["status"] == "run.completed"
