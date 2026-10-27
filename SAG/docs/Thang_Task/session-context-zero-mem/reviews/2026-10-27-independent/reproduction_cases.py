"""Independent synthetic PR40 regressions. No app DB, env files or paid provider."""

import asyncio
import copy
import json
import os
from datetime import UTC, datetime

import pytest
import test_session_context_postgres as pg_fixtures
import test_session_context_storage as storage_fixtures
from sqlalchemy.ext.asyncio import AsyncSession
from test_session_context_full_runtime import ProviderBoundary, full_plan, stream
from test_session_context_runtime import Counter, principal
from test_session_context_storage import raw

from sag_api.core.config import settings
from sag_api.core.errors import ValidationError
from sag_api.db.models import Message, SessionMessageProvenance, Source, SourceProjectMapping, Thread
from sag_api.services.agent_domain import AskPlan, delete_message, persist_answer, prepare_ask
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context.aggregate import aggregate_session
from sag_api.services.session_context.budget import TokenUsage
from sag_api.services.session_context.runtime import prepare_context
from sag_api.services.session_context.storage import record_provenance
from sag_api.services.session_context.types import ContextContractError

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
    factory, scope, context = native_pg
    monkeypatch.setattr(settings, "session_context_mode", "on")
    midpoint, index_updated, release_lease = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = AsyncSession.execute

    async def instrumented(session, statement, *args, **kwargs):
        result = await execute(session, statement, *args, **kwargs)
        rendered = str(statement)
        if session is context.lease_session and "FROM session_context_indexes" in rendered and "FOR " in rendered:
            midpoint.set()
            await asyncio.sleep(0.05)
        if rendered.startswith("UPDATE session_context_indexes"):
            index_updated.set()
        return result

    monkeypatch.setattr(AsyncSession, "execute", instrumented)

    async def reader():
        async with context.lease(factory, principal(scope)):
            release_lease.set()

    async def writer():
        await midpoint.wait()
        async with factory() as session:
            await delete_message(session, "agent", "thread", "fact", owner_key="owner")

    outcomes = await asyncio.wait_for(asyncio.gather(reader(), writer(), return_exceptions=True), timeout=10)
    labels = [
        {"type": type(outcome).__name__, "sqlstate": getattr(getattr(outcome, "orig", None), "sqlstate", None)}
        for outcome in outcomes
    ]
    assert all(not isinstance(outcome, BaseException) for outcome in outcomes), labels
    async with factory() as session:
        assert await session.get(Message, "fact") is None
        with pytest.raises(ContextContractError):
            await context.revalidate(session, principal(scope))


@pytest.mark.asyncio
async def test_runtime_evaluation_resume_must_reject_changed_model_and_caps(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path.cwd() / "scripts"))
    import verify_session_runtime as harness

    monkeypatch.setattr(harness.model, "ModelTokenCounter", lambda *a, **k: Counter())
    calls = []

    async def fake_candidate(case, *, web_enabled):
        calls.append((case["id"], web_enabled, harness.settings.llm_model))
        return {
            "case_id": case["id"],
            "group": case["group"],
            "web_enabled": web_enabled,
            "variant": "runtime",
            "status": "run.completed",
            "answer": "UNKNOWN",
            "answer_status": "no_answer",
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
    report = json.loads(output.read_text(encoding="utf-8"))
    report["results"].pop()
    report["elapsed_seconds"] = 12.5
    prior_call = {
        "status": "completed",
        "input_tokens": 10,
        "output_tokens": 5,
        "observed_deployment_failures": 0,
        "observed_deployment_attempts": 1,
    }
    report["reserved_tokens"] = 321
    report["experiment_accounting"] = {"calls": [prior_call]}
    frozen_bytes = json.dumps(report, indent=2, ensure_ascii=False)
    output.write_text(frozen_bytes, encoding="utf-8")

    # 1. Changing model or caps must raise ValueError before any candidate call and without overwriting report.
    bad_mutations = [
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
    ]
    for mutate in bad_mutations:
        mutated_report = copy.deepcopy(report)
        mutated_args = copy.deepcopy(base_args)
        mutate(mutated_report, mutated_args)
        output.write_text(json.dumps(mutated_report), encoding="utf-8")
        before_text = output.read_text(encoding="utf-8")
        with pytest.raises(ValueError):
            await harness.run(mutated_args)
        assert not calls, "Rejected resume must not invoke candidate/provider"
        assert output.read_text(encoding="utf-8") == before_text, "Rejected resume must not overwrite report"

    # 2. Valid resume with identical manifest runs only the missing row and preserves cumulative accounting.
    output.write_text(frozen_bytes, encoding="utf-8")
    await harness.run(base_args)
    assert len(calls) == 1
    resumed = json.loads(output.read_text(encoding="utf-8"))
    assert len(resumed["results"]) == 4
    assert resumed["experiment_accounting"]["calls"][0] == prior_call
    assert resumed["reserved_tokens"] >= 321
    assert resumed["elapsed_seconds"] >= 12.5
