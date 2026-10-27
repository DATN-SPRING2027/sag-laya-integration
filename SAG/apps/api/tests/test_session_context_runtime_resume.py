"""Offline regression coverage for the live harness's resume boundary."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag_api.services.eval.session_context_accounting import BaselineBudgetExceeded, CallLedger


@pytest.fixture
def resume_run(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    import verify_session_runtime as harness

    case = {"id": "resume-fixture", "split": "calibration", "group": "normal"}
    args = SimpleNamespace(
        split="calibration", case=None, variants="runtime", output=str(tmp_path / "report.json"),
        max_calls=10, max_seconds=600, max_tokens=6000,
    )
    monkeypatch.setattr(harness.baseline, "load_cases", lambda: [case])
    monkeypatch.setattr(harness.settings, "llm_base_url", "https://explicit-provider.invalid/v1")
    monkeypatch.setattr(harness.settings, "session_context_mode", "off")
    provider_calls, candidate_calls = [], []

    class Counter:
        def count(self, *args, **kwargs):
            return 100

    async def provider(*args, **kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic"))],
            usage=SimpleNamespace(prompt_tokens=80, completion_tokens=20),
        )

    async def candidate(case, *, web_enabled):
        candidate_calls.append(web_enabled)
        calls = []
        status = "run.completed"
        try:
            await harness.model.session_completion(
                SimpleNamespace(llm_max_tokens=4096), messages=[{"role": "user", "content": "synthetic"}]
            )
            calls.append({"status": "completed", "input_tokens": 80, "output_tokens": 20})
        except BaselineBudgetExceeded:
            status = "error"
        return {
            "case_id": case["id"], "variant": "runtime", "web_enabled": web_enabled, "status": status,
            "accounting": {
                "logical_calls": len(calls), "accounting_complete": True,
                "input_tokens": 80 * len(calls), "output_tokens": 20 * len(calls), "calls": calls,
            },
        }

    monkeypatch.setattr(harness.model, "ModelTokenCounter", lambda *a, **kw: Counter())
    monkeypatch.setattr(harness.model, "session_completion", provider)
    monkeypatch.setattr(harness, "candidate", candidate)

    def report(*, retries=0):
        ledger = CallLedger(max_calls=10, max_seconds=600, max_estimated_tokens=100000)
        record = ledger.begin(stream=False, estimated_input=100, max_output=4096, retries=retries)
        ledger.observe_attempt(record)
        record.update(status="completed", input_tokens=80, output_tokens=20)
        return {
            **harness.build_runtime_manifest(args, [case]),
            "results": [{
                "case_id": case["id"], "variant": "runtime", "web_enabled": False,
                "status": "run.completed", "accounting": copy.deepcopy(ledger.report()),
            }],
            "elapsed_seconds": 12.5, "reserved_tokens": ledger.reserved_tokens,
            "experiment_accounting": ledger.report(),
        }

    return SimpleNamespace(
        harness=harness, args=args, case=case, output=Path(args.output), report=report,
        provider_calls=provider_calls, candidate_calls=candidate_calls,
    )


@pytest.mark.parametrize("endpoint", [None, "", "   "])
async def test_runtime_harness_rejects_implicit_endpoint_before_work(resume_run, monkeypatch, endpoint):
    run = resume_run
    monkeypatch.setattr(run.harness.settings, "llm_base_url", endpoint)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://implicit-provider.invalid/v1")
    with pytest.raises(ValueError, match="explicit.*SAG_LLM_BASE_URL"):
        await run.harness.run(run.args)
    assert not run.candidate_calls and not run.provider_calls
    assert not run.output.exists()


async def test_explicit_endpoint_resume_ignores_sdk_environment(resume_run, monkeypatch):
    run = resume_run
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-a.invalid/v1")
    report = run.report()
    run.output.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-b.invalid/v1")
    assert run.harness.build_runtime_manifest(run.args, [run.case]) == {
        key: report[key] for key in run.harness.REQUIRED_MANIFEST_KEYS
    }
    await run.harness.run(run.args)
    assert run.candidate_calls == [True]
    assert not run.provider_calls  # 4196 already reserved; a second 4196 cannot fit cap 6000.
    assert json.loads(run.output.read_text(encoding="utf-8"))["reserved_tokens"] == 4196


@pytest.mark.parametrize("mutation", [
    "reduced_total", "increased_total", "missing_input", "missing_output", "missing_retries", "schema_v2",
])
async def test_resume_rejects_unverifiable_reservations_before_work(resume_run, mutation):
    run = resume_run
    report = run.report()
    call = report["experiment_accounting"]["calls"][0]
    if mutation == "reduced_total":
        report["reserved_tokens"] = 100
    elif mutation == "increased_total":
        report["reserved_tokens"] += 1
    elif mutation == "schema_v2":
        report["schema_version"] = 2
    else:
        call.pop({
            "missing_input": "estimated_input_tokens", "missing_output": "max_output_tokens",
            "missing_retries": "configured_retries",
        }[mutation], None)
    original = json.dumps(report)
    run.output.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        await run.harness.run(run.args)
    assert not run.candidate_calls and not run.provider_calls
    assert run.output.read_text(encoding="utf-8") == original


async def test_valid_resume_keeps_full_reservation_and_uses_only_remaining_budget(resume_run):
    run = resume_run
    run.args.max_tokens = 10000
    run.output.write_text(json.dumps(run.report()), encoding="utf-8")
    await run.harness.run(run.args)
    saved = json.loads(run.output.read_text(encoding="utf-8"))
    assert run.candidate_calls == [True] and len(run.provider_calls) == 1
    assert saved["reserved_tokens"] == 8392
    assert len(saved["experiment_accounting"]["calls"]) == 2
    assert saved["elapsed_seconds"] >= 12.5
    run.harness.validate_runtime_resume(saved, run.harness.build_runtime_manifest(run.args, [run.case]))


def test_reservation_validation_includes_retry_allowance(resume_run):
    run = resume_run
    run.args.max_tokens = 20000
    report = run.report(retries=2)
    expected = run.harness.build_runtime_manifest(run.args, [run.case])
    assert report["reserved_tokens"] == 12588
    run.harness.validate_runtime_resume(report, expected)
    report["reserved_tokens"] = 4196
    with pytest.raises(ValueError):
        run.harness.validate_runtime_resume(report, expected)


async def test_explicit_zero_call_accounting_can_resume(resume_run):
    run = resume_run
    report = run.report()
    empty = CallLedger(max_calls=10, max_seconds=600).report()
    report["experiment_accounting"] = empty
    report["results"][0]["accounting"] = copy.deepcopy(empty)
    report["reserved_tokens"] = 0
    run.output.write_text(json.dumps(report), encoding="utf-8")
    await run.harness.run(run.args)
    saved = json.loads(run.output.read_text(encoding="utf-8"))
    assert len(run.provider_calls) == 1
    assert saved["reserved_tokens"] == 4196
    run.harness.validate_runtime_resume(saved, run.harness.build_runtime_manifest(run.args, [run.case]))


def test_ledger_retains_output_reserve_even_with_unknown_usage():
    ledger = CallLedger(max_calls=2, max_seconds=30, max_estimated_tokens=10000)
    record = ledger.begin(stream=False, estimated_input=100, max_output=4096, retries=1)
    record["status"] = "failed"
    assert record["max_output_tokens"] == 4096
    assert ledger.reserved_tokens == 8392
    assert ledger.report()["input_tokens"] is None


@pytest.mark.asyncio
async def test_crash_after_provider_attempt_persists_accounting_and_blocks_budget_reopen(
    resume_run, monkeypatch
):
    run = resume_run
    run.args.max_calls = 2
    run.args.max_tokens = 20000

    async def crashing_candidate(case, *, web_enabled):
        run.candidate_calls.append(web_enabled)
        await run.harness.model.session_completion(
            SimpleNamespace(llm_max_tokens=4096),
            messages=[{"role": "user", "content": "synthetic"}],
        )
        if web_enabled:
            raise RuntimeError("simulated crash after provider call before replay checkpoint")
        return {
            "case_id": case["id"],
            "variant": "runtime",
            "web_enabled": web_enabled,
            "status": "run.completed",
            "accounting": {
                "logical_calls": 1,
                "accounting_complete": True,
                "input_tokens": 80,
                "output_tokens": 20,
                "calls": [{"status": "completed", "input_tokens": 80, "output_tokens": 20}],
            },
        }

    monkeypatch.setattr(run.harness, "candidate", crashing_candidate)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await run.harness.run(run.args)

    assert len(run.provider_calls) == 2
    saved = json.loads(run.output.read_text(encoding="utf-8"))
    assert len(saved["experiment_accounting"]["calls"]) == 2
    assert saved["reserved_tokens"] == 8392

    with pytest.raises(ValueError, match="in-flight|reconcil|call count"):
        await run.harness.run(run.args)
    assert len(run.provider_calls) == 2


@pytest.mark.asyncio
async def test_sigkill_mid_provider_dispatch_persists_inflight_reservation_and_blocks_resume(
    resume_run, monkeypatch
):
    run = resume_run
    run.args.max_calls = 2
    run.args.max_tokens = 20000
    mid_flight_snapshots = []

    async def sigkill_provider(*args, **kwargs):
        run.provider_calls.append(kwargs)
        if len(run.provider_calls) == 2:
            mid_flight_snapshots.append(run.output.read_text(encoding="utf-8"))
            raise RuntimeError("transport dropped after remote dispatch")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="synthetic"))],
            usage=SimpleNamespace(prompt_tokens=80, completion_tokens=20),
        )

    monkeypatch.setattr(run.harness.model, "session_completion", sigkill_provider)
    with pytest.raises(RuntimeError, match="transport dropped"):
        await run.harness.run(run.args)

    assert len(run.provider_calls) == 2
    assert len(mid_flight_snapshots) == 1
    mid_flight = json.loads(mid_flight_snapshots[0])
    assert len(mid_flight["experiment_accounting"]["calls"]) == 2
    assert mid_flight["experiment_accounting"]["calls"][-1]["status"] == "started"
    assert mid_flight["experiment_accounting"]["calls"][-1]["in_flight"] is True
    assert mid_flight["reserved_tokens"] == 8392

    # Simulate abrupt process death (SIGKILL) while the second provider call was still in flight.
    run.output.write_text(mid_flight_snapshots[0], encoding="utf-8")
    with pytest.raises(ValueError, match="in-flight|started|reconcil"):
        await run.harness.run(run.args)
    assert len(run.provider_calls) == 2


@pytest.mark.asyncio
async def test_crash_after_legacy_tracked_llm_attempt_persists_accounting_and_blocks_resume(
    resume_run, monkeypatch
):
    run = resume_run
    run.args.variants = "legacy"
    run.args.max_calls = 2
    run.args.max_tokens = 20000

    async def crashing_legacy_replay(case, client, ledger, *, web_enabled):
        run.candidate_calls.append(("legacy", web_enabled))
        record = ledger.begin(stream=False, estimated_input=100, max_output=4096, retries=0)
        ledger.observe_attempt(record)
        run.provider_calls.append({"variant": "legacy", "web_enabled": web_enabled})
        ledger.settle(record, status="completed", duration_ms=5.0)
        record.update(input_tokens=80, output_tokens=20)
        if web_enabled:
            raise RuntimeError("simulated legacy crash after provider call")
        return {
            "case_id": case["id"],
            "variant": "legacy",
            "web_enabled": web_enabled,
            "status": "completed",
            "accounting": {
                "logical_calls": 1,
                "accounting_complete": True,
                "input_tokens": 80,
                "output_tokens": 20,
                "calls": [copy.deepcopy(record)],
            },
        }

    monkeypatch.setattr(run.harness.baseline, "replay_case", crashing_legacy_replay)
    with pytest.raises(RuntimeError, match="simulated legacy crash"):
        await run.harness.run(run.args)

    assert len(run.provider_calls) == 2
    saved = json.loads(run.output.read_text(encoding="utf-8"))
    assert len(saved["experiment_accounting"]["calls"]) == 2
    assert saved["reserved_tokens"] == 8392

    with pytest.raises(ValueError, match="in-flight|reconcil|call count"):
        await run.harness.run(run.args)
    assert len(run.provider_calls) == 2


def test_validation_corpus_loads_and_locks_manifest(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    import eval_session_context as baseline
    import verify_session_runtime as harness

    corpus = Path(__file__).parent / "fixtures" / "session_context_validation_f2_f3.jsonl"
    cases = baseline.load_cases(corpus)
    assert len(cases) == 12
    normal = [c for c in cases if c["group"] == "normal"]
    full = [c for c in cases if c["group"] == "full_session"]
    long_normal = [c for c in normal if c["message_count"] > 200 or c["filler_repeat"] > 1]
    assert len(normal) == 10 and len(full) == 2 and len(long_normal) >= 4
    monkeypatch.setattr(harness.settings, "llm_base_url", "https://explicit-provider.invalid/v1")
    args = SimpleNamespace(
        corpus=corpus,
        split="validation",
        case=None,
        variants="both",
        output="unused.json",
        max_calls=180,
        max_seconds=2400,
        max_tokens=4000000,
    )
    manifest = harness.build_runtime_manifest(args, cases)
    assert manifest["split"] == "validation"
    assert len(manifest["case_ids"]) == 12


def test_strict_evidence_recall_helpers_score_raw_and_fitted_and_reject_malformed_schema():
    from scripts.eval_session_context import (
        expand_case,
        score_fitted_evidence_messages,
        score_selected_evidence,
    )

    case = {
        "id": "unit-ev-score",
        "split": "validation",
        "language": "vi",
        "group": "normal",
        "scenario": "long_session_retention",
        "message_count": 10,
        "filler_repeat": 1,
        "facts": [
            {"at": 2, "role": "user", "content": "Chốt mã định tuyến KRONOS-418."},
            {"at": 6, "role": "user", "content": "Chốt cổng dự phòng PAY-SIGMA-79."},
        ],
        "query": "Nêu mã định tuyến và cổng dự phòng.",
        "gold_items": [
            {"id": "routing", "aliases": ["KRONOS-418"], "evidence": [{"at": 2, "quote": "KRONOS-418"}]},
            {"id": "gateway", "aliases": ["PAY-SIGMA-79"], "evidence": [{"at": 6, "quote": "PAY-SIGMA-79"}]},
        ],
    }
    expanded = expand_case(case)
    raw_full = [
        {"message_id": expanded[2]["id"], "kind": "user_statement", "text": expanded[2]["content"]},
        {"message_id": expanded[6]["id"], "kind": "user_statement", "text": expanded[6]["content"]},
    ]
    scored_full = score_selected_evidence(case, expanded, raw_full)
    assert scored_full["item_evidence_recall"] == 1.0
    assert scored_full["covered_evidence_items"] == ["routing", "gateway"]

    raw_half = [raw_full[0]]
    scored_half = score_selected_evidence(case, expanded, raw_half)
    assert scored_half["item_evidence_recall"] == 0.5
    assert scored_half["covered_evidence_items"] == ["routing"]
    assert scored_half["uncovered_evidence_items"] == ["gateway"]

    fitted_msg = "SESSION_EVIDENCE_JSON\n" + json.dumps(
        [
            {
                "id": "S1",
                "kind": "user_statement",
                "message_id": expanded[2]["id"],
                "start": 0,
                "end": len(expanded[2]["content"]),
                "text": expanded[2]["content"],
            }
        ],
        ensure_ascii=False,
    )
    scored_fitted = score_fitted_evidence_messages(case, expanded, [fitted_msg])
    assert scored_fitted["item_evidence_recall"] == 0.5

    for bad_raw in (
        None,
        "not-a-list",
        [{"message_id": expanded[2]["id"], "kind": "user_statement", "quote": "KRONOS-418"}],
        [{"message_id": "", "kind": "user_statement", "text": "KRONOS-418"}],
        [{"message_id": expanded[2]["id"], "kind": "invalid_kind", "text": "KRONOS-418"}],
    ):
        with pytest.raises(ValueError):
            score_selected_evidence(case, expanded, bad_raw)

    for bad_fitted in (
        None,
        ["NOT_EVIDENCE_HEADER\n[]"],
        ["SESSION_EVIDENCE_JSON\n{bad json"],
        ["SESSION_EVIDENCE_JSON\n[{\"id\": \"S1\"}]"],
    ):
        with pytest.raises(ValueError):
            score_fitted_evidence_messages(case, expanded, bad_fitted)


@pytest.mark.asyncio
async def test_dispatched_fitted_prompt_captures_post_fit_dropped_evidence_and_links_sha256(
    tmp_path, monkeypatch
):
    import test_session_context_storage as storage_fixtures
    from test_session_context_runtime import Counter, Encoder, principal
    from test_session_context_storage import raw

    from sag_api.core.config import Settings
    from sag_api.db.models import Agent
    from sag_api.services.session_context import model as model_module
    from sag_api.services.session_context.model import BudgetedLLM
    from sag_api.services.session_context.runtime import prepare_context
    from scripts.eval_session_context import score_fitted_evidence_messages, score_selected_evidence

    async for factory, scope in storage_fixtures.storage_db.__wrapped__():
        async with factory() as session:
            m1 = raw("m-fit-1", content="Chốt mã định tuyến KRONOS-418.", ordinal=1)
            m2 = raw("m-fit-2", content="Chốt cổng dự phòng PAY-SIGMA-79. " + ("chi tiết bổ sung " * 120), ordinal=2)
            user = raw("m-fit-q", content="Nêu mã định tuyến và cổng dự phòng.", ordinal=3)
            session.add_all([m1, m2, user])
            await session.commit()
            await session.refresh(user)
            ctx = await prepare_context(
                session,
                key=scope.key,
                principal=principal(scope),
                user_message=user,
                query=user.content,
                mode="normal",
                encoder=Encoder(),
                counter=Counter(),
            )
            agent = await session.get(Agent, "agent")

        case = {
            "gold_items": [
                {"id": "routing", "evidence": [{"at": 0, "quote": "KRONOS-418"}]},
                {"id": "gateway", "evidence": [{"at": 1, "quote": "PAY-SIGMA-79"}]},
            ]
        }
        expanded = [
            {"id": "m-fit-1", "role": "user", "content": m1.content},
            {"id": "m-fit-2", "role": "user", "content": m2.content},
        ]
        raw_selected = [
            {"message_id": item.span.ref.message_id, "kind": item.kind, "text": item.text}
            for item in ctx.original
        ]
        assert score_selected_evidence(case, expanded, raw_selected)["item_evidence_recall"] == 1.0

        # Force fit() with a tight input limit so only the shorter evidence item fits.
        from dataclasses import replace

        base_messages = ctx.messages(agent, "Nêu mã định tuyến và cổng dự phòng.")
        full_tokens = ctx.counter.count(base_messages, None)
        ctx.budget.limits = replace(
            ctx.budget.limits,
            context_window_tokens=ctx.budget.limits.output_reserve_tokens + full_tokens - 50,
        )
        fitted_messages = ctx.fit(base_messages, None)

        async def fake_completion(finite, **kwargs):
            return {
                "choices": [{"message": {"content": "KRONOS-418"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            }

        monkeypatch.setattr(model_module, "session_completion", fake_completion)
        client = BudgetedLLM(
            Settings(_env_file=None, llm_api_key="offline-test"),
            budget=ctx.budget,
            counter=ctx.counter,
            on_dispatch=ctx.note_dispatch,
        )
        await client._create_completion(fitted_messages)

        assert len(ctx.dispatched_calls) == 1
        assert ctx.dispatched_calls[0]["messages_sha256"] == client.calls[0]["messages_sha256"]
        assert ctx.dispatched_evidence_messages
        fitted_score = score_fitted_evidence_messages(case, expanded, ctx.dispatched_evidence_messages)
        assert fitted_score["item_evidence_recall"] == 0.5
        assert fitted_score["covered_evidence_items"] == ["routing"]
        assert fitted_score["uncovered_evidence_items"] == ["gateway"]


@pytest.mark.asyncio
async def test_cancelled_stream_in_tracked_llm_keeps_unknown_usage(monkeypatch):
    import asyncio

    from sag_api.core.config import Settings
    from sag_api.generation import llm as llm_module
    from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM

    async def fake_litellm(**kwargs):
        async def gen():
            yield {
                "choices": [{"delta": {"content": "partial"}}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 3},
            }
            raise asyncio.CancelledError()

        return gen()

    monkeypatch.setattr(llm_module, "_litellm_completion", fake_litellm)
    ledger = CallLedger(max_calls=5, max_seconds=30)
    client = TrackedLLM(Settings(_env_file=None, llm_api_key="offline-test", llm_model="offline"), ledger)
    stream = await client._create_completion([{"role": "user", "content": "hi"}], stream=True)
    with pytest.raises(asyncio.CancelledError):
        async for _ in stream:
            pass
    assert len(ledger.calls) == 1
    call = ledger.calls[0]
    assert call["status"] == "cancelled"
    assert call["in_flight"] is False
    assert call["usage_snapshots"] == 1
    assert call["input_tokens"] is None
    assert call["output_tokens"] is None
    assert ledger.report()["accounting_complete"] is False


@pytest.mark.asyncio
async def test_experiment_wall_time_deadline_cuts_off_active_candidate_replay(resume_run, monkeypatch):
    import asyncio

    run = resume_run
    run.args.max_seconds = 0.05

    async def slow_candidate(case, *, web_enabled):
        deadline = run.harness._EXPERIMENT_DEADLINE.get()
        assert deadline is not None
        async with asyncio.timeout_at(deadline):
            await asyncio.sleep(5)

    monkeypatch.setattr(run.harness, "candidate", slow_candidate)
    with pytest.raises(TimeoutError):
        await run.harness.run(run.args)
