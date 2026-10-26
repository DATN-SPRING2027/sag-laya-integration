"""Opt-in paired runtime evaluation, synthetic isolated DBs, durable resume.

Never opens application DB or writes real chats. Bad completed answers are never retried.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import eval_session_context as baseline
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Agent, Message, SessionContextIndex, Thread
from sag_api.enums import MessageRole
from sag_api.generation.llm import LLMClient, _attr
from sag_api.sag import EngineManager
from sag_api.services import agent_domain, agent_service
from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM
from sag_api.services.session_context import model
from sag_api.services.session_context.budget import BudgetLimits, BudgetState
from sag_api.services.session_context.index import sync_index
from sag_api.services.session_context.runtime import configured_encoder
from sag_api.services.session_context.scope import resolve_scope
from sag_api.services.session_context.storage import record_provenance
from sag_api.services.session_context.types import SessionKey
from sag_api.tools import registry

INDEX_TEMPLATES = {}  # Standalone synthetic harness only, never an application cache.


async def candidate(case, *, web_enabled):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    principal = VerifiedPrincipal(
        "synthetic-user",
        "synthetic-org",
        frozenset({"synthetic-project"}),
        "runtime-fixture",
        "fixture",
        "fixture",
        0,
        2_000_000_000,
    )
    result = {
        "case_id": case["id"],
        "group": case["group"],
        "web_enabled": web_enabled,
        "variant": "runtime",
        "status": "no_terminal_event",
    }
    started = time.monotonic()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            agent = Agent(id="runtime-agent", owner_key="synthetic-owner", name="Baseline Agent", persona={})
            thread = Thread(id="runtime-thread", agent_id=agent.id, owner_key="synthetic-owner", title="Synthetic")
            session.add_all([agent, thread])
            await session.flush()
            scope = await resolve_scope(
                session,
                key=SessionKey(thread.owner_key, agent.id, thread.id),
                principal=principal,
                candidate_source_ids=frozenset(),
            )
            for item in baseline.expand_case(case):
                message = Message(
                    id=item["id"],
                    thread_id=thread.id,
                    role=MessageRole(item["role"]),
                    content=item["content"],
                    citations=[],
                    steps=[],
                    attachments=[],
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
                session.add(message)
                await session.flush()
                # Trusted synthetic producer only. Never stamp unknown real legacy assistants.
                await record_provenance(session, message, scope=scope)
            await session.commit()
            cold = time.monotonic()
            if case["group"] == "normal":
                template = INDEX_TEMPLATES.get(case["id"])
                result["index_template_reused"] = template is not None
                if template is not None:
                    session.add(SessionContextIndex(**copy.deepcopy(template)))
                    await session.commit()
                    result["cold_index_work_units"] = 0
                else:
                    encoder = await asyncio.to_thread(configured_encoder)
                    work = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 32000000, time.monotonic() + 180))
                    async with asyncio.timeout(180):
                        await sync_index(session, scope=scope, encoder=encoder, budget=work)
                    result["cold_index_work_units"] = work.index_units
                    row = await session.scalar(select(SessionContextIndex))
                    INDEX_TEMPLATES[case["id"]] = {
                        attribute.key: copy.deepcopy(getattr(row, attribute.key))
                        for attribute in row.__mapper__.column_attrs
                    }
            result["cold_index_ms"] = round((time.monotonic() - cold) * 1000, 2)
            warm = time.monotonic()
            plan = await agent_domain.prepare_ask(
                session,
                agent=agent,
                thread=thread,
                query=case["query"],
                principal=principal,
                owner_key=thread.owner_key,
                llm=LLMClient(settings),
                context_mode=case["group"],
            )
            result["prepare_ms"] = round((time.monotonic() - warm) * 1000, 2)
        async for event in agent_service.generate_stream(
            factory,
            plan=plan,
            agent=agent,
            thread_id=thread.id,
            engine_manager=EngineManager(settings),
            llm=LLMClient(settings),
            tool_registry=registry,
            knowledge_only=not web_enabled,
            principal=principal,
        ):
            if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                payload = event.data["payload"]
                result.update(
                    status=event.type,
                    answer=str(payload.get("output") or ""),
                    answer_status=payload.get("answer_status"),
                    accounting=payload.get("session_accounting"),
                    context=payload.get("session_context"),
                    error_code=(payload.get("error") or {}).get("code"),
                )
        result.update(baseline.score_answer(case, result.get("answer", "")))
    except Exception as error:
        result.update(status="error", error_type=type(error).__name__)
    finally:
        await engine.dispose()
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


async def run(args):
    cases = [case for case in baseline.load_cases() if case["split"] == args.split]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    root = baseline.API_ROOT
    paths = sorted((root / "sag_api/services/session_context").glob("*.py")) + [
        root / "sag_api/services/agent_domain.py",
        root / "sag_api/services/agent_service.py",
        Path(__file__),
    ]
    manifest = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    output = Path(args.output)
    if output.exists():
        report = json.loads(output.read_text(encoding="utf-8"))
        if report["source_sha256"] != manifest or report["split"] != args.split:
            raise ValueError("resume source/split mismatch; use a new output path")
    else:
        report = {
            "split": args.split,
            "source_sha256": manifest,
            "corpus_sha256": hashlib.sha256(baseline.CORPUS.read_bytes()).hexdigest(),
            "results": [],
            "scope": "synthetic trusted provenance; source-free; cold index separate; actual runtime/SDK",
            "caps": {"calls": args.max_calls, "seconds": args.max_seconds, "tokens": args.max_tokens},
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    previous = report.get("experiment_accounting", {})
    ledger = CallLedger(max_calls=args.max_calls, max_seconds=args.max_seconds, max_estimated_tokens=args.max_tokens)
    started = time.monotonic()
    spent = report.get("elapsed_seconds", 0)
    ledger.deadline = started + max(0, args.max_seconds - spent)
    ledger.calls = previous.get("calls", [])
    ledger.reserved_tokens = report.get("reserved_tokens", 0)
    counter = model.ModelTokenCounter(settings.llm_model, template_kwargs={"enable_thinking": False})
    original = model.session_completion

    async def observed(finite, **kwargs):
        record = ledger.begin(
            stream=bool(kwargs.get("stream")),
            estimated_input=counter.count(kwargs["messages"], kwargs.get("tools")),
            max_output=finite.llm_max_tokens,
        )
        ledger.observe_attempt(record)
        try:
            response = await original(finite, **kwargs)
            if not kwargs.get("stream"):
                record["synthetic_output"] = _attr(_attr(response, "choices")[0], "message").content
                ledger.usage(record, _attr(response, "usage"))
                record["status"] = "completed"
                return response

            async def stream():
                try:
                    async for chunk in response:
                        choices = _attr(chunk, "choices") or []
                        if choices:
                            text = _attr(_attr(choices[0], "delta"), "content")
                            if isinstance(text, str):
                                record["synthetic_output"] = record.get("synthetic_output", "") + text
                        ledger.usage(record, _attr(chunk, "usage"))
                        yield chunk
                    record["status"] = "completed"
                finally:
                    if record["status"] != "completed":
                        record["status"] = "failed"
                    await response.aclose()

            return stream()
        except BaseException:
            record["status"] = "failed"
            raise

    def save():
        report["elapsed_seconds"] = spent + time.monotonic() - started
        report["experiment_accounting"] = ledger.report()
        report["reserved_tokens"] = ledger.reserved_tokens
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    client = TrackedLLM(settings, ledger)
    with ledger.capture_deployments(), patch.object(model, "session_completion", observed):
        for case in cases:
            for web in (False, True):
                for variant in ("legacy", "runtime") if args.variants == "both" else (args.variants,):
                    key = (case["id"], web, variant)
                    if any((row["case_id"], row["web_enabled"], row["variant"]) == key for row in report["results"]):
                        continue
                    if len(ledger.calls) >= args.max_calls or time.monotonic() >= ledger.deadline:
                        save()
                        return
                    settings.session_context_mode = "off" if variant == "legacy" else "on"
                    if variant == "legacy":
                        result = await baseline.replay_case(case, client, ledger, web_enabled=web)
                        result["variant"] = variant
                    else:
                        result = await candidate(case, web_enabled=web)
                    report["results"].append(result)
                    save()
                    print(
                        json.dumps(
                            {
                                k: result.get(k)
                                for k in (
                                    "case_id",
                                    "variant",
                                    "web_enabled",
                                    "status",
                                    "literal_item_recall",
                                    "error_type",
                                )
                            }
                        ),
                        flush=True,
                    )
    save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--split", choices=("calibration", "holdout"), required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--variants", choices=("both", "legacy", "runtime"), default="both")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-calls", type=int, default=180)
    parser.add_argument("--max-seconds", type=float, default=2400)
    parser.add_argument("--max-tokens", type=int, default=4000000)
    arguments = parser.parse_args()
    logging.disable(logging.CRITICAL)
    asyncio.run(run(arguments))
