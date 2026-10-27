"""Independent synthetic PR40 regressions. No app DB, env files or paid provider."""

import asyncio
import json
import os
from datetime import UTC, datetime

import pytest
import test_session_context_postgres as pg_fixtures
import test_session_context_storage as storage_fixtures
from sag_api.core.config import settings
from sag_api.db.models import Message, SessionMessageProvenance, Source, SourceProjectMapping
from sag_api.services.agent_domain import AskPlan, delete_message, persist_answer
from sag_api.services.session_context import model as model_module
from sag_api.services.session_context.aggregate import aggregate_session
from sag_api.services.session_context.budget import TokenUsage
from sag_api.services.session_context.runtime import prepare_context
from sqlalchemy.ext.asyncio import AsyncSession
from test_session_context_full_runtime import ProviderBoundary, full_plan, stream
from test_session_context_runtime import Counter, principal
from test_session_context_storage import raw

storage_db = storage_fixtures.storage_db
native_pg = pg_fixtures.native_pg


@pytest.mark.asyncio
async def test_full_session_must_not_bypass_explicit_source_filter(storage_db, monkeypatch):
    factory, scope = storage_db
    agent, plan = await full_plan(factory, scope)
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
        user = await session.get(Message, "current")
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=plan.query,
            mode="full_session",
            requested_source_ids=["source"],
            counter=Counter(),
        )
    plan = AskPlan(
        query=plan.query,
        messages=context.messages(agent, plan.query),
        user_message_id="current",
        source_ids=["source"],
        session_context=context,
    )
    provider = ProviderBoundary()
    monkeypatch.setattr(model_module, "session_completion", provider)
    events = [e async for e in stream(factory, scope, agent, plan)]
    terminal = next(e.data["payload"] for e in events if e.type == "run.completed")
    print(
        "SOURCE_FILTER",
        json.dumps(
            {
                "answer_status": terminal["answer_status"],
                "output": terminal["output"],
                "citations": terminal["citations"],
                "tools": next(e.data["payload"]["tools"] for e in events if e.type == "run.started"),
            }
        ),
    )
    assert terminal["answer_status"] == "no_answer", "Explicit @Source requires document evidence"


@pytest.mark.asyncio
async def test_every_dispatched_full_batch_must_be_in_answer_provenance(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        for i in range(6):
            session.add(raw(f"m{i}", content=f"DECISION {i}: " + "filler " * 40, ordinal=i))
        user = raw("current", content="List all decisions", ordinal=8)
        session.add(user)
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
            context.note_dispatch()  # Exact before_dispatch behavior in agent_service.
            reservation = context.budget.reserve_call(input_tokens=context.counter.count(messages))
            payload = json.loads(messages[-2]["content"].split("\n", 1)[1])
            context.budget.settle_call(reservation, TokenUsage(10, 10))
            if messages[-2]["content"].startswith("SESSION_EVIDENCE_JSON"):
                self.maps += 1
                dispatched.update(x["message_id"] for x in payload)
                if self.maps == 2:
                    return "malformed-map"
                return json.dumps({"items": [{"text": "DECISION 0", "supports": [{"id": payload[0]["id"]}]}]})
            return "malformed-reduce"  # Publish the validated first-map fallback.

    async with context.lease(factory, principal(scope)):
        result = await aggregate_session(context, FailingSecondBatch(), "List all decisions", input_limit=650)
        answer_id = await persist_answer(
            factory, "thread", result.answer, [], session_context=context, principal=principal(scope)
        )
    async with factory() as session:
        provenance = await session.get(SessionMessageProvenance, answer_id)
        captured = {x["message_id"] for x in provenance.dependencies}
    print(
        "DISPATCH_PROVENANCE",
        json.dumps({"sent": sorted(dispatched), "captured": sorted(captured), "reason": result.reason}),
    )
    assert dispatched <= captured, "The failed map was sent to the model but disappeared from provenance"


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("SESSION_CONTEXT_NATIVE_PG_QA") != "1", reason="explicit disposable PG QA only")
async def test_lease_and_delete_must_not_take_index_and_message_in_opposite_order(native_pg, monkeypatch):
    factory, scope, context = native_pg
    monkeypatch.setattr(settings, "session_context_mode", "on")
    messages_locked, index_updated, release_lease = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = AsyncSession.execute

    async def instrumented(session, statement, *args, **kwargs):
        rendered = str(statement)
        if session is context.lease_session and "FROM session_context_indexes" in rendered and "FOR UPDATE" in rendered:
            messages_locked.set()
            await index_updated.wait()
        result = await execute(session, statement, *args, **kwargs)
        if rendered.startswith("UPDATE session_context_indexes"):
            index_updated.set()
        return result

    monkeypatch.setattr(AsyncSession, "execute", instrumented)

    async def reader():
        async with context.lease(factory, principal(scope)):
            release_lease.set()

    async def writer():
        await messages_locked.wait()
        async with factory() as session:
            await delete_message(session, "agent", "thread", "fact", owner_key="owner")

    outcomes = await asyncio.wait_for(asyncio.gather(reader(), writer(), return_exceptions=True), timeout=10)
    labels = [
        {"type": type(outcome).__name__, "sqlstate": getattr(getattr(outcome, "orig", None), "sqlstate", None)}
        for outcome in outcomes
    ]
    print("LOCK_ORDER", labels)
    assert all(not isinstance(outcome, BaseException) for outcome in outcomes), labels


@pytest.mark.asyncio
async def test_runtime_evaluation_resume_must_reject_changed_model_and_caps(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path.cwd() / "scripts"))
    import verify_session_runtime as harness

    root = Path.cwd().parents[1] / "docs/Thang_Task/session-context-zero-mem/evidence"
    report = json.loads((root / "t9-holdout-runtime-v4.json").read_text(encoding="utf-8"))
    removed = next(row for row in reversed(report["results"]) if row["variant"] == "runtime")
    report["results"].remove(removed)
    output = tmp_path / "synthetic-resume.json"
    output.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(harness.settings, "llm_model", "SYNTHETIC-NEW-MODEL")
    monkeypatch.setattr(harness.model, "ModelTokenCounter", lambda *a, **k: Counter())
    calls = []

    async def fake_candidate(case, *, web_enabled):
        calls.append(harness.settings.llm_model)
        return dict(removed)

    monkeypatch.setattr(harness, "candidate", fake_candidate)
    args = SimpleNamespace(
        split="holdout",
        case=None,
        output=str(output),
        max_calls=500,
        max_seconds=10000,
        max_tokens=100000000,
        variants="runtime",
    )
    try:
        await harness.run(args)
    except ValueError:
        return
    saved = json.loads(output.read_text(encoding="utf-8"))
    print(
        "RESUME",
        json.dumps(
            {
                "actual_model": calls,
                "reported_model": saved["config"]["llm_model"],
                "requested_calls_cap": 500,
                "reported_calls_cap": saved["caps"]["calls"],
            }
        ),
    )
    assert not calls, "A resumed frozen report accepted work under a different model and caps"
