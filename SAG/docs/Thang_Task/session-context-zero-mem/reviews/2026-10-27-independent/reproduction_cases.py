"""Independent synthetic PR40 regressions. No app DB, env files or paid provider."""

import copy
import json
import os
from datetime import UTC, datetime

import pytest
import test_session_context_postgres as pg_fixtures
import test_session_context_storage as storage_fixtures
from test_session_context_full_runtime import ProviderBoundary, full_plan, stream
from test_session_context_runtime import Counter, principal
from test_session_context_storage import raw

from sag_api.core.config import settings
from sag_api.core.errors import ValidationError
from sag_api.db.models import Message, SessionMessageProvenance, Source, SourceProjectMapping, Thread
from sag_api.services.agent_domain import AskPlan, persist_answer, prepare_ask
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context.aggregate import aggregate_session
from sag_api.services.session_context.budget import TokenUsage
from sag_api.services.session_context.runtime import prepare_context
from sag_api.services.session_context.storage import record_provenance

storage_db = storage_fixtures.storage_db
native_pg = pg_fixtures.native_pg


@pytest.mark.asyncio
async def test_full_session_must_not_bypass_explicit_source_filter(storage_db, monkeypatch):
    factory, scope = storage_db
    monkeypatch.setattr(settings, "session_context_mode", "on")
    agent, plan = await full_plan(factory, scope)
    provider = ProviderBoundary()
    monkeypatch.setattr(model_module, "session_completion", provider)
    async with factory() as session:
        session.add(Source(id="source", name="Chosen source", sag_source_config_id="chosen"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="mapping",
                source_id="source",
                organization_id="org",
                project_id="project",
                state="CONFIRMED",
                confirmed_at=datetime.now(UTC),
                confirmed_by="synthetic",
                approval_ref="synthetic",
            )
        )
        await session.commit()
        thread = await session.get(Thread, "thread")
        for query, mode in [(plan.query, "full_session"), ("Liệt kê tất cả quyết định từ đầu cuộc chat.", None)]:
            with pytest.raises(ValidationError) as caught:
                await prepare_ask(
                    session,
                    agent=agent,
                    thread=thread,
                    query=query,
                    source_ids=["source"],
                    principal=principal(scope),
                    owner_key=scope.key.owner_key,
                    context_mode=mode,
                )
            assert caught.value.code == "session_context_source_filter_unsupported"
        user = await session.get(Message, "current")
        with pytest.raises(ValidationError) as caught:
            await prepare_context(
                session,
                key=scope.key,
                principal=principal(scope),
                user_message=user,
                query=plan.query,
                mode="full_session",
                requested_source_ids=["source"],
                counter=Counter(),
            )
        assert caught.value.code == "session_context_source_filter_unsupported"
        empty_filter_ctx = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=plan.query,
            mode="full_session",
            requested_source_ids=[],
            counter=Counter(),
        )
        assert empty_filter_ctx.scope.requested_source_ids == frozenset()
    manual_plan = AskPlan(
        query=plan.query,
        messages=plan.messages,
        user_message_id="current",
        source_ids=["source"],
        session_context=plan.session_context,
    )
    with pytest.raises(ValidationError) as caught:
        async for _ in stream(factory, scope, agent, manual_plan):
            pass
    assert caught.value.code == "session_context_source_filter_unsupported"
    assert not provider.requests


@pytest.mark.asyncio
async def test_every_dispatched_full_batch_must_be_in_answer_provenance(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add(Source(id="src-b2", name="Batch2 Source", sag_source_config_id="src-b2"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="map-b2",
                source_id="src-b2",
                organization_id="org",
                project_id="project",
                state="CONFIRMED",
                confirmed_at=datetime.now(UTC),
                confirmed_by="synthetic",
                approval_ref="synthetic",
            )
        )
        for i in range(6):
            session.add(raw(f"m{i}", content=f"DECISION {i}: " + "filler " * 40, ordinal=i))
        user = raw("current", content="List all decisions", ordinal=8)
        session.add(user)
        await session.commit()
        m4 = await session.get(Message, "m4")
        from dataclasses import replace

        await record_provenance(
            session, m4, scope=replace(scope, source_ids=frozenset({"src-b2"})), source_ids=frozenset({"src-b2"})
        )
        await session.commit()
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=user.content,
            mode="full_session",
            counter=Counter(),
        )
    dispatched = set()

    class FailingSecondBatch:
        maps = 0

        async def complete(self, messages):
            reservation = context.budget.reserve_call(input_tokens=context.counter.count(messages))
            context.note_dispatch()
            payload = json.loads(messages[-2]["content"].split("\n", 1)[1])
            context.budget.settle_call(reservation, TokenUsage(10, 10))
            if messages[-2]["content"].startswith("SESSION_EVIDENCE_JSON"):
                self.maps += 1
                dispatched.update(x["message_id"] for x in payload)
                if self.maps == 2:
                    return "malformed-map"
                return json.dumps({"items": [{"text": "DECISION 0", "supports": [{"id": payload[0]["id"]}]}]})
            return "malformed-reduce"

    async with context.lease(factory, principal(scope)):
        result = await aggregate_session(context, FailingSecondBatch(), "List all decisions", input_limit=650)
        answer_id = await persist_answer(
            factory, "thread", result.answer, [], session_context=context, principal=principal(scope)
        )
    async with factory() as session:
        provenance = await session.get(SessionMessageProvenance, answer_id)
        captured = {x["message_id"] for x in provenance.dependencies}
        captured_sources = set(provenance.source_ids)
    assert dispatched <= captured, "The failed map was sent to the model but disappeared from provenance"
    assert "m4" in dispatched and "src-b2" in captured_sources
    assert "m4" not in result.visited_ids, "Failed map batch must not be marked visited"


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("SESSION_CONTEXT_NATIVE_PG_QA") != "1", reason="explicit disposable PG QA only")
async def test_lease_and_delete_must_not_take_index_and_message_in_opposite_order(native_pg, monkeypatch):
    await pg_fixtures.assert_lease_and_delete_message_race_at_message_lock_boundary(native_pg, monkeypatch)


def test_encoder_internal_typeerror_must_not_be_retried():
    import time

    from sag_api.services.session_context.budget import BudgetLimits, BudgetState
    from sag_api.services.session_context.index import _encode

    class FailingEncoder:
        model_id = "typeerror-model"
        calls = 0

        def featurize_many(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TypeError("synthetic internal inference TypeError")
            return ("second attempt accepted",)

    encoder = FailingEncoder()
    work = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 10000, time.monotonic() + 30))
    with pytest.raises(TypeError, match="synthetic internal inference TypeError"):
        _encode(encoder, ("synthetic",), work, {})
    assert encoder.calls == 1


@pytest.mark.asyncio
async def test_runtime_evaluation_resume_must_reject_changed_model_and_caps(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path.cwd() / "scripts"))
    import verify_session_runtime as harness

    monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-a.invalid/v1")
    monkeypatch.setattr(harness.settings, "llm_api_key", "sk-secret-api-key-never-log-0123456789")
    monkeypatch.setattr(harness.model, "ModelTokenCounter", lambda *a, **k: Counter())
    calls = []

    async def fake_candidate(case, *, web_enabled):
        calls.append((case["id"], web_enabled, harness.settings.llm_model, harness.settings.llm_base_url))
        return {
            "case_id": case["id"],
            "group": case["group"],
            "web_enabled": web_enabled,
            "variant": "runtime",
            "status": "run.completed",
            "answer": "UNKNOWN",
            "answer_status": "no_answer",
            "accounting": {
                "logical_calls": 0,
                "accounting_complete": True,
                "input_tokens": 0,
                "output_tokens": 0,
                "monetary_cost": None,
                "calls": [],
            },
        }

    monkeypatch.setattr(harness, "candidate", fake_candidate)
    output = tmp_path / "synthetic-resume.json"
    base_args = SimpleNamespace(
        split="holdout",
        case=["holdout-recent", "holdout-unknown"],
        output=str(output),
        max_calls=50,
        max_seconds=600,
        max_tokens=100000,
        variants="runtime",
    )
    # Create an initial frozen report for the 2-case subset, then remove 1 row to simulate resume.
    await harness.run(base_args)
    assert len(calls) == 4
    calls.clear()
    raw_report_text = output.read_text(encoding="utf-8")
    assert "sk-secret-api-key-never-log-0123456789" not in raw_report_text

    # Also verify URL userinfo/query secrets never leak into manifest text.
    monkeypatch.setattr(
        harness.settings,
        "llm_base_url",
        "https://user:url-secret-pass@provider-a.invalid/v1?api_key=query-secret-token",
    )
    secret_manifest_text = json.dumps(harness.build_runtime_manifest(base_args, []))
    assert "url-secret-pass" not in secret_manifest_text
    assert "query-secret-token" not in secret_manifest_text
    assert "sk-secret-api-key-never-log-0123456789" not in secret_manifest_text
    monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-a.invalid/v1")

    report = json.loads(raw_report_text)
    report["results"].pop()
    report["elapsed_seconds"] = 12.5
    prior_call = {
        "stage": "generation",
        "stream": False,
        "status": "completed",
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "usage_snapshots": 1,
        "observed_deployment_failures": 0,
        "observed_deployment_attempts": 1,
        "configured_retries": 0,
        "estimated_input_tokens": 10,
        "max_output_tokens": 311,
        "duration_ms": 12.0,
        "ttft_ms": None,
    }
    report["results"][0]["accounting"] = {
        "logical_calls": 1,
        "accounting_complete": True,
        "input_tokens": 10,
        "output_tokens": 5,
        "monetary_cost": None,
        "calls": [
            {
                "status": "completed",
                "input_tokens": 10,
                "output_tokens": 5,
                "reserved_input_tokens": 10,
                "configured_retries": 0,
            }
        ],
    }
    report["reserved_tokens"] = 321
    report["experiment_accounting"] = harness.CallLedger(
        max_calls=50, max_seconds=600, max_estimated_tokens=100000
    ).report([prior_call])
    frozen_bytes = json.dumps(report, indent=2, ensure_ascii=False)
    output.write_text(frozen_bytes, encoding="utf-8")

    # 1. Changing endpoint, model, caps, or corrupting/dropping accounting must raise ValueError
    # before any candidate call and without overwriting report.
    bad_mutations = [
        lambda r, a: monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-b.invalid/v1"),
        lambda r, a: setattr(a, "max_calls", 500),
        lambda r, a: setattr(a, "max_seconds", 10000),
        lambda r, a: setattr(a, "max_tokens", 999999),
        lambda r, a: setattr(a, "variants", "legacy,runtime"),
        lambda r, a: setattr(a, "case", ["holdout-unknown", "holdout-recent"]),
        lambda r, a: r["config"].__setitem__("llm_model", "OTHER-MODEL"),
        lambda r, a: r.__setitem__("corpus_sha256", "0" * 64),
        lambda r, a: r.__setitem__("provider_extra_body_sha256", "0" * 64),
        lambda r, a: r["models"].__setitem__("tokenizer", ["other", "rev"]),
        lambda r, a: r["dependency_versions"].__setitem__("openai", "0.0.0"),
        lambda r, a: r.pop("caps", None),
        lambda r, a: r.pop("provider_route_sha256", None),
        lambda r, a: r.pop("experiment_accounting", None),
        lambda r, a: r.pop("reserved_tokens", None),
        lambda r, a: r.pop("elapsed_seconds", None),
        lambda r, a: r.__setitem__("reserved_tokens", -1),
        lambda r, a: r.__setitem__("reserved_tokens", True),
        lambda r, a: r.__setitem__("reserved_tokens", 0),
        lambda r, a: r.__setitem__("elapsed_seconds", -0.5),
        lambda r, a: r.__setitem__("elapsed_seconds", float("nan")),
        lambda r, a: r.__setitem__("elapsed_seconds", float("inf")),
        lambda r, a: r["experiment_accounting"].__setitem__("calls", "not-a-list"),
        lambda r, a: r["experiment_accounting"].__setitem__("logical_calls", 99),
        lambda r, a: r["experiment_accounting"]["calls"][0].__setitem__("input_tokens", -5),
        lambda r, a: (
            r["experiment_accounting"]["calls"][0].__setitem__("input_tokens", None),
            r["experiment_accounting"].__setitem__("accounting_complete", True),
        ),
        lambda r, a: (
            r["experiment_accounting"]["calls"][0].__setitem__("input_tokens", None),
            r["experiment_accounting"].__setitem__("accounting_complete", False),
            r["experiment_accounting"].__setitem__("input_tokens", 0),
        ),
        lambda r, a: (
            r["experiment_accounting"].__setitem__("calls", []),
            r["experiment_accounting"].__setitem__("logical_calls", 0),
            r["experiment_accounting"].__setitem__("input_tokens", 0),
            r["experiment_accounting"].__setitem__("output_tokens", 0),
            r["experiment_accounting"].__setitem__("known_input_tokens", 0),
            r["experiment_accounting"].__setitem__("known_output_tokens", 0),
        ),
    ]
    for mutate in bad_mutations:
        monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-a.invalid/v1")
        mutated_report = copy.deepcopy(report)
        mutated_args = copy.deepcopy(base_args)
        mutate(mutated_report, mutated_args)
        output.write_text(json.dumps(mutated_report, allow_nan=True), encoding="utf-8")
        before_text = output.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            await harness.run(mutated_args)
        assert not calls, "Rejected resume must not invoke candidate/provider"
        assert output.read_text(encoding="utf-8") == before_text, "Rejected resume must not overwrite report"
    monkeypatch.setattr(harness.settings, "llm_base_url", "https://provider-a.invalid/v1")

    # 2. Explicit zero-call report (where completed results genuinely made 0 LLM calls) is accepted on resume.
    zero_call_report = copy.deepcopy(report)
    zero_call_report["results"][0]["accounting"]["logical_calls"] = 0
    zero_call_report["results"][0]["accounting"]["calls"] = []
    zero_call_report["results"][0]["accounting"]["input_tokens"] = 0
    zero_call_report["results"][0]["accounting"]["output_tokens"] = 0
    zero_call_report["reserved_tokens"] = 0
    zero_call_report["experiment_accounting"] = harness.CallLedger(
        max_calls=50, max_seconds=600, max_estimated_tokens=100000
    ).report([])
    output.write_text(json.dumps(zero_call_report), encoding="utf-8")
    await harness.run(base_args)
    assert len(calls) == 1
    calls.clear()

    # 3. Exhausted budget on resume (calls, tokens, or seconds) must not launch new work or reset totals.
    for exhausted_field, exhausted_value, cap_key in (
        ("max_calls", 1, "calls"),
        ("max_tokens", 321, "tokens"),
        ("max_seconds", 12.5, "seconds"),
    ):
        exhausted_report = copy.deepcopy(report)
        exhausted_args = copy.deepcopy(base_args)
        setattr(exhausted_args, exhausted_field, exhausted_value)
        exhausted_report["caps"][cap_key] = exhausted_value
        output.write_text(json.dumps(exhausted_report), encoding="utf-8")
        await harness.run(exhausted_args)
        assert not calls, f"Exhausted {exhausted_field} must not launch candidate work"
        saved_exhausted = json.loads(output.read_text(encoding="utf-8"))
        assert len(saved_exhausted["results"]) == 3
        assert saved_exhausted["reserved_tokens"] == exhausted_report["reserved_tokens"]
        assert saved_exhausted["elapsed_seconds"] >= exhausted_report["elapsed_seconds"]
        assert saved_exhausted["experiment_accounting"]["calls"] == [prior_call]

    # 4. Valid resume with identical manifest runs only the missing row and preserves cumulative accounting.
    output.write_text(frozen_bytes, encoding="utf-8")
    await harness.run(base_args)
    assert len(calls) == 1
    resumed = json.loads(output.read_text(encoding="utf-8"))
    assert len(resumed["results"]) == 4
    assert resumed["experiment_accounting"]["calls"][0] == prior_call
    assert resumed["reserved_tokens"] >= 321
    assert resumed["elapsed_seconds"] >= 12.5


@pytest.mark.asyncio
async def test_f6_shadow_missing_tokenizer_cache_and_corrupted_vector_dimension_and_crash_resume(
    storage_db, tmp_path, monkeypatch
):
    """Verify the 4 post-review [P2] findings:
    1. SHADOW falls back cleanly when tokenizer cache is missing (OSError -> ServiceUnavailableError).
    2. sync_index rebuilds cached vectors whose dimension does not match the encoder.
    3. Crash after provider attempt persists accounting/reservation and blocks budget reopen on resume.
    4. Calibration validate_resume locks provider_route_sha256.
    """
    import transformers
    from test_session_context_index import Encoder as DimEncoder
    from test_session_context_runtime import Encoder as RuntimeEncoder
    from test_session_context_storage import work

    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.db.models import Agent, SessionContextIndex
    from sag_api.services.eval.session_context_calibration import provider_route_sha256, validate_resume
    from sag_api.services.session_context import runtime
    from sag_api.services.session_context.index import sync_index

    # (4) Calibration resume locks provider_route_sha256
    monkeypatch.setattr(settings, "llm_base_url", "https://provider-a.invalid/v1")
    route_a = provider_route_sha256(settings)
    monkeypatch.setattr(settings, "llm_base_url", "https://provider-b.invalid/v1")
    route_b = provider_route_sha256(settings)
    cal_doc = {
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
    }
    validate_resume(cal_doc, cal_doc)
    with pytest.raises(ValueError, match="resume"):
        validate_resume(cal_doc, {**cal_doc, "provider_route_sha256": route_b})
    with pytest.raises(ValueError, match="resume"):
        validate_resume({k: v for k, v in cal_doc.items() if k != "provider_route_sha256"}, cal_doc)

    # (1) & (2) Missing tokenizer cache in SHADOW + corrupted cached vector dimension in sync_index
    factory, scope = storage_db
    monkeypatch.setattr(settings, "llm_model", "Qwen/Qwen3-8B")
    monkeypatch.setattr(runtime, "configured_encoder", lambda: RuntimeEncoder())

    def missing_tokenizer(*args, **kwargs):
        raise OSError("missing local tokenizer cache")

    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", missing_tokenizer)
    monkeypatch.setattr(settings, "session_context_mode", "shadow")
    async with factory() as session:
        session.add(raw("m1", content="First decision ORBIT-1", ordinal=1))
        session.add(raw("m2", content="Second decision ORBIT-2", ordinal=2))
        await session.commit()
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        plan = await prepare_ask(
            session,
            agent=agent,
            thread=thread,
            query="Which code?",
            principal=principal(scope),
            owner_key="owner",
        )
        assert plan.session_context is None

    monkeypatch.setattr(settings, "session_context_mode", "on")
    async with factory() as session:
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        with pytest.raises(ServiceUnavailableError):
            await prepare_ask(
                session,
                agent=agent,
                thread=thread,
                query="Which code?",
                principal=principal(scope),
                owner_key="owner",
            )

    async with factory() as session:
        enc1 = DimEncoder()
        first = await sync_index(session, scope=scope, encoder=enc1, budget=work(), batch_size=2)
        row = await session.get(SessionContextIndex, first.index_id)
        corrupted = dict(row.features)
        corrupted_m1 = dict(corrupted["m1"])
        corrupted_m1["vector"] = [1.0, 0.1, 0.9]
        corrupted["m1"] = corrupted_m1
        row.features = corrupted
        await session.commit()

    async with factory() as session:
        enc2 = DimEncoder()
        healed = await sync_index(session, scope=scope, encoder=enc2, budget=work(), batch_size=2)
        assert enc2.calls == 1
        assert all(len(f.vector) == 2 for f in healed.features)
        graph, _ = healed.views(scope=scope, budget=work())
        assert len(graph.features) >= 2
