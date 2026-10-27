"""Calibration selection cannot use holdout or turn missing usage into savings."""

import json

import pytest
from test_session_context_graph import stub_encoder
from test_session_context_reader import work

from sag_api.services.eval.session_context_calibration import (
    calibration_cases,
    choose_profile,
    evidence_metrics,
    paired_tokens,
    profiles,
    validate_resume,
)


def test_calibration_loader_excludes_holdout_before_any_expansion(tmp_path):
    path = tmp_path / "corpus.jsonl"
    path.write_text('\n'.join(json.dumps(c) for c in [
        {"id": "calibration-test", "split": "calibration", "group": "normal"},
        {"id": "holdout-test", "split": "holdout", "secret": "not for tuning"},
    ]), encoding="utf-8")
    assert [case["id"] for case in calibration_cases(path)] == ["calibration-test"]
    assert "not for tuning" not in str(calibration_cases(path))


def test_gold_evidence_is_scored_only_after_selection_and_requires_all_supports():
    case = {"gold_items": [{"id": "gold", "evidence": [{"at": 0, "quote": "A"}, {"at": 2, "quote": "B"}]}]}
    messages = [{"id": "a", "content": "A"}, {"id": "x", "content": "noise"}, {"id": "b", "content": "B"}]
    assert evidence_metrics(case, messages, {"a"})["item_evidence_recall"] == 0
    assert evidence_metrics(case, messages, {"a", "b"})["item_evidence_recall"] == 1
    assert evidence_metrics({"gold_items": []}, messages, set())["item_evidence_recall"] is None


def test_profile_is_locked_from_normal_evidence_not_full_session_or_answer_scores():
    sweep = [{"profile": name, "group": group, "item_evidence_recall": value}
             for name, group, value in [("compact", "normal", 0.5), ("balanced", "normal", 1.0),
                                        ("balanced", "full_session", 0.0), ("wide", "normal", 1.0)]]
    assert choose_profile(sweep) == "balanced"
    assert choose_profile([{"profile": "compact", "group": "normal", "item_evidence_recall": 0.5}]) is None


def test_unknown_usage_or_absent_pairs_cannot_pass_token_savings_gate():
    def row(case, inputs, outputs, complete=True):
        return {"case_id": case, "status": "run.completed", "accounting": {"accounting_complete": complete,
                "input_tokens": inputs, "output_tokens": outputs}}
    assert paired_tokens([row("a", 100, 20)], [row("a", 20, 10)])["reduction"] == 0.75
    assert paired_tokens([row("a", 100, 20)], [row("a", None, 10, False)])["reduction"] is None
    assert paired_tokens([row("a", 100, 20)], [row("b", 20, 10)])["reduction"] is None
    blocked = {**row("a", 0, 0), "status": "run.failed"}
    assert paired_tokens([row("a", 100, 20)], [blocked])["reduction"] is None


def test_profiles_are_explicit_with_increasing_resource_envelopes():
    assert set(profiles()) == {"compact", "balanced", "wide"}
    assert profiles()["compact"].max_codepoints < profiles()["balanced"].max_codepoints
    assert profiles()["balanced"].max_messages < profiles()["wide"].max_messages


@pytest.mark.parametrize(
    "field",
    [
        "config",
        "provider_route_sha256",
        "profiles",
        "hierarchy",
        "llm_extra_body_sha256",
        "source_sha256",
                                  "dependency_versions", "case_ids"])
def test_provider_resume_cannot_mix_changed_payload_or_retrieval_configuration(field):
    original = {
        "corpus_sha256": "corpus",
        "config": {},
        "provider_route_sha256": "route-a",
        "locked_profile": "balanced",
        "profiles": {},
                "hierarchy": {}, "llm_extra_body_sha256": "body", "case_ids": ["case"],
                "dependency_versions": {"litellm": "same"}, "source_sha256": {name: "same" for name in (
                    "graph.py", "selection.py", "hierarchy.py", "reader.py", "scope.py", "budget.py", "types.py",
                    "eval_session_context.py", "agent_domain.py", "agent_service.py", "llm.py", "prompt.py")}}
    validate_resume(original, original)
    changed = {**original, field: "changed" if field != "source_sha256"
               else {**original[field], "prompt.py": "changed"}}
    with pytest.raises(ValueError, match="resume"):
        validate_resume(original, changed)


@pytest.mark.asyncio
async def test_replay_checkpoint_includes_paid_call_ledger_before_next_replay(monkeypatch, tmp_path):
    import copy
    from types import SimpleNamespace

    from scripts import calibrate_session_context as harness

    checkpoints = []

    async def replay(case, client, ledger, **kwargs):
        if checkpoints:
            raise RuntimeError("interrupt after checkpoint")
        record = ledger.begin(stream=True, estimated_input=10, max_output=20)
        ledger.observe_attempt(record)
        ledger.usage(record, {"prompt_tokens": 10, "completion_tokens": 2})
        record["status"] = "completed"
        return {"case_id": case["id"], "status": "run.completed", "duration_ms": 100}

    monkeypatch.setattr(harness, "replay_case", replay)
    monkeypatch.setattr(harness, "save", lambda path, report: checkpoints.append(copy.deepcopy(report)))
    args = SimpleNamespace(resume_provider_from=None, max_calls=10, max_estimated_tokens=10000,
                           output=tmp_path / "checkpoint.json")
    with pytest.raises(RuntimeError, match="interrupt after checkpoint"):
        await harness.live_replay([{"id": "case"}], {"case": {"balanced": []}}, args,
                                  {"locked_profile": "balanced", "results": []})
    assert checkpoints[0]["accounting"]["logical_calls"] == 1
    assert checkpoints[0]["accounting"]["input_tokens"] == 10
    assert checkpoints[0]["reserved_tokens"] == 30


@pytest.mark.asyncio
async def test_repeat_resume_keeps_time_spent_on_prior_failed_attempts(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts import calibrate_session_context as harness

    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps({"results": [{"duration_ms": 10000}],
                                    "excluded_attempts": [{"duration_ms": 30000}]}), encoding="utf-8")
    observed = {}

    def ledger(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("stop before any call")

    monkeypatch.setattr(harness, "CallLedger", ledger)
    args = SimpleNamespace(resume_provider_from=previous, max_calls=10, max_estimated_tokens=10000)
    with pytest.raises(RuntimeError, match="stop before any call"):
        await harness.live_replay([], {}, args, {})
    assert observed["max_seconds"] == 1160


def test_batching_retains_every_raw_chunk_offset_and_does_not_truncate():
    encoder = stub_encoder(4)

    class BatchEncoder(type(encoder.encoder)):
        @staticmethod
        def encode(texts, **kwargs):
            return [(1.0, 0.0) for _ in texts]

    class BatchNer(type(encoder.ner)):
        def __call__(self, texts, **kwargs):
            return [super(BatchNer, self).__call__(text) for text in texts]

    encoder.encoder, encoder.ner = BatchEncoder(), BatchNer()
    actual = encoder.featurize_many(("🙂abcDEF", "Z"), batch_size=2, budget=work())
    expected = stub_encoder(4)
    assert actual == tuple(expected.featurize(text, budget=work()) for text in ("🙂abcDEF", "Z"))
    assert encoder.encoder_calls == encoder.ner_calls == 2  # Three chunks in two batches.
    assert encoder.encoder_tokens == encoder.ner_tokens == 14
    encoder.encoder_calls = encoder.encoder_tokens = encoder.ner_calls = encoder.ner_tokens = 0
    cached = encoder.featurize_many(("🙂abcDEF", "🙂abcDEF"), batch_size=2, budget=work(), deduplicate=True)
    assert cached[0] == cached[1] == expected.featurize("🙂abcDEF", budget=work())
    assert encoder.encoder_calls == encoder.ner_calls == 1
    assert encoder.encoder_tokens == encoder.ner_tokens == 11


@pytest.mark.asyncio
async def test_candidate_replay_uses_selected_raw_history_without_compression_or_gold(monkeypatch):
    from sag_api.core.config import Settings
    from sag_api.generation import llm as llm_module
    from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM
    from scripts.eval_session_context import replay_case

    query = "Viết lại mã đã chốt. Nếu thiếu, ghi UNKNOWN."
    seen = []

    async def completion(**kwargs):
        assert kwargs["stream"], "candidate cannot call compression"
        messages = kwargs["messages"]
        text = str(messages)
        assert "only-in-gold" not in text and "gold_items" not in text
        assert sum(m.get("content") == query for m in messages) == 1
        assert any(m.get("content") == "Chốt OLD-724." for m in messages)
        seen.append(messages)

        async def chunks():
            yield {"choices": [{"delta": {"content": "OLD-724"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 12, "completion_tokens": 3}}
        return chunks()

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    case = {"id": "candidate-seam", "split": "calibration", "language": "vi", "group": "normal",
            "scenario": "compression_middle", "message_count": 180, "filler_repeat": 12,
            "facts": [{"at": 80, "role": "user", "content": "Chốt OLD-724."}], "query": query,
            "gold_items": [{"id": "gold", "aliases": ["only-in-gold"],
                            "evidence": [{"at": 80, "quote": "OLD-724"}]}]}
    ledger = CallLedger(max_calls=3, max_seconds=30)
    client = TrackedLLM(Settings(_env_file=None, llm_api_key="offline-test", llm_model="offline-model"), ledger)
    result = await replay_case(case, client, ledger, web_enabled=True,
                               history_override=[{"role": "user", "content": "Chốt OLD-724."}])
    assert result["status"] == "run.completed" and seen
    assert len(ledger.calls) == 1
    assert result["compression_called"] is False
    assert result["gold_verbatim_in_initial_context"] == 1


@pytest.mark.asyncio
async def test_calibration_live_resume_rejects_changed_or_missing_provider_endpoint(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from sag_api.services.eval.session_context_calibration import provider_route_sha256
    from scripts import calibrate_session_context as harness

    monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-a.invalid/v1")
    route_a = provider_route_sha256(harness.settings)
    base_report = {
        "corpus_sha256": "corpus",
        "config": {},
        "provider_route_sha256": route_a,
        "locked_profile": "balanced",
        "profiles": {},
        "hierarchy": {},
        "llm_extra_body_sha256": "body",
        "case_ids": ["case"],
        "dependency_versions": {"litellm": "same"},
        "source_sha256": {
            name: "same"
            for name in (
                "graph.py",
                "selection.py",
                "hierarchy.py",
                "reader.py",
                "scope.py",
                "budget.py",
                "types.py",
                "eval_session_context.py",
                "agent_domain.py",
                "agent_service.py",
                "llm.py",
                "prompt.py",
            )
        },
        "results": [],
        "excluded_attempts": [],
        "accounting": {"calls": []},
    }
    previous_path = tmp_path / "previous.json"
    previous_path.write_text(json.dumps(base_report), encoding="utf-8")

    monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-b.invalid/v1")
    args = SimpleNamespace(
        resume_provider_from=previous_path,
        max_calls=10,
        max_estimated_tokens=10000,
        output=tmp_path / "resumed.json",
    )
    with pytest.raises(ValueError, match="resume|explicit.*SAG_LLM_BASE_URL"):
        await harness.live_replay([], {}, args, dict(base_report))

    monkeypatch.setattr(harness.settings, "llm_base_url", "")
    with pytest.raises(ValueError, match="explicit.*SAG_LLM_BASE_URL|resume"):
        await harness.live_replay([], {}, args, dict(base_report))
