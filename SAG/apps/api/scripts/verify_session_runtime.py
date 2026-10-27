"""Opt-in paired runtime evaluation, synthetic isolated DBs, durable resume.

Never opens application DB or writes real chats. Bad completed answers are never retried.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import eval_session_context as baseline
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.litellm_policy import apply_litellm_completion_policy
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
from sag_api.services.session_context.graph import ENCODER, NER
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
                result["index_template_reused"] = bool(template and template.get("state") == "ready")
                if template is not None:
                    session.add(SessionContextIndex(**copy.deepcopy(template)))
                    await session.commit()
                if template is not None and template.get("state") == "ready":
                    result["cold_index_work_units"] = 0
                    result["index_state"] = "ready"
                else:
                    encoder = await asyncio.to_thread(configured_encoder)
                    work = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 32000000, time.monotonic() + 180))
                    try:
                        async with asyncio.timeout(180):
                            await sync_index(session, scope=scope, encoder=encoder, budget=work)
                    finally:
                        result["cold_index_work_units"] = work.index_units
                        row = await session.scalar(select(SessionContextIndex))
                        if row is not None:
                            result["index_state"] = row.state
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


REQUIRED_MANIFEST_KEYS = (
    "schema_version",
    "split",
    "case_ids",
    "variants",
    "source_sha256",
    "corpus_sha256",
    "scope",
    "caps",
    "config",
    "provider_route_sha256",
    "provider_extra_body_sha256",
    "models",
    "dependency_versions",
)
ACCOUNTING_SUMMARY_KEYS = (
    "logical_calls",
    "accounting_complete",
    "input_tokens",
    "output_tokens",
    "known_input_tokens",
    "known_output_tokens",
)


def _is_nonneg_int(value) -> bool:
    return type(value) is int and value >= 0


def _is_finite_nonneg(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _sanitize_endpoint_url(raw_url: str | None) -> dict:
    if raw_url is None:
        return {"configured": False, "scheme": "", "host": "", "port": None, "path": ""}
    stripped = str(raw_url).strip()
    if not stripped:
        return {"configured": False, "scheme": "", "host": "", "port": None, "path": ""}
    parsed = urlsplit(stripped)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid provider base_url port") from error
    return {
        "configured": True,
        "scheme": parsed.scheme.lower(),
        "host": (parsed.hostname or "").lower(),
        "port": port,
        "path": parsed.path.rstrip("/"),
        "userinfo_sha256": (
            hashlib.sha256(f"{parsed.username or ''}:{parsed.password or ''}".encode()).hexdigest()
            if parsed.username is not None or parsed.password is not None
            else None
        ),
        "query_sha256": hashlib.sha256(parsed.query.encode()).hexdigest() if parsed.query else None,
        "fragment_sha256": hashlib.sha256(parsed.fragment.encode()).hexdigest() if parsed.fragment else None,
    }


def _provider_route_sha256() -> str:
    legacy_probe = apply_litellm_completion_policy(
        settings,
        {
            "model": settings.routed_llm_model,
            "api_base": settings.llm_base_url,
            "temperature": settings.effective_llm_temperature,
            "max_tokens": settings.llm_max_tokens,
        },
    )
    legacy_probe.pop("api_base", None)
    legacy_probe.pop("base_url", None)
    session_probe = apply_litellm_completion_policy(
        settings,
        {
            "model": settings.llm_model,
            "api_base": settings.llm_base_url,
            "temperature": settings.effective_llm_temperature,
            "max_tokens": min(settings.llm_max_tokens, 4096),
            "response_format": {"type": "json_object"},
        },
    )
    session_probe.pop("api_base", None)
    session_probe.pop("base_url", None)
    descriptor = {
        "provider": settings.llm_provider,
        "routed_model": settings.routed_llm_model,
        "session_model": settings.llm_model,
        "effective_temperature": settings.effective_llm_temperature,
        "structured_output_mode": settings.llm_structured_output_mode,
        "endpoint": _sanitize_endpoint_url(settings.llm_base_url),
        "legacy_request_policy": legacy_probe,
        "session_request_policy": session_probe,
    }
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def build_runtime_manifest(args, cases):
    root = baseline.API_ROOT
    paths = sorted((root / "sag_api/services/session_context").glob("*.py")) + [
        root / "sag_api/core/litellm_policy.py",
        root / "sag_api/core/model_providers.py",
        root / "sag_api/services/agent_domain.py",
        root / "sag_api/services/agent_service.py",
        root / "sag_api/generation/llm.py",
        root / "sag_api/generation/prompt.py",
        root / "sag_api/services/eval/session_context_accounting.py",
        root / "scripts/eval_session_context.py",
        Path(__file__).resolve(),
    ]
    manifest = {
        path.resolve().relative_to(root.resolve()).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    variants = ["legacy", "runtime"] if getattr(args, "variants", "both") == "both" else [args.variants]
    return {
        "schema_version": 2,
        "split": args.split,
        "case_ids": [case["id"] for case in cases],
        "variants": variants,
        "source_sha256": manifest,
        "corpus_sha256": hashlib.sha256(baseline.CORPUS.read_bytes()).hexdigest(),
        "scope": "synthetic trusted provenance; source-free; cold index separate; actual runtime/SDK",
        "caps": {"calls": args.max_calls, "seconds": args.max_seconds, "tokens": args.max_tokens},
        "config": {
            key: getattr(settings, key)
            for key in (
                "llm_provider",
                "llm_model",
                "llm_context_window",
                "llm_max_tokens",
                "llm_max_retries",
                "llm_timeout_ms",
                "llm_temperature",
                "agent_max_steps",
                "history_load_limit",
                "history_keep_recent",
            )
        },
        "provider_route_sha256": _provider_route_sha256(),
        "provider_extra_body_sha256": hashlib.sha256(
            json.dumps(settings.llm_extra_body, sort_keys=True).encode()
        ).hexdigest(),
        "models": {"encoder": list(ENCODER), "ner": list(NER), "tokenizer": list(model.TOKENIZER)},
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in (
                "openai",
                "httpx",
                "litellm",
                "sqlalchemy",
                "transformers",
                "torch",
                "sentence-transformers",
            )
        },
    }


def _validate_call_record(call: object, *, require_observer_counts: bool) -> None:
    if not isinstance(call, dict):
        raise ValueError("cannot safely resume: call record must be an object; start a new experiment or reconcile")
    if call.get("status") not in {"started", "completed", "failed", "cancelled"}:
        raise ValueError("cannot safely resume: invalid call status; start a new experiment or reconcile")
    for token_key in ("input_tokens", "output_tokens"):
        if token_key not in call:
            raise ValueError(f"cannot safely resume: missing {token_key} in call; start a new experiment or reconcile")
        token_val = call[token_key]
        if token_val is not None and not _is_nonneg_int(token_val):
            raise ValueError(f"cannot safely resume: invalid {token_key} in call; start a new experiment or reconcile")
    if require_observer_counts:
        for obs_key in ("observed_deployment_attempts", "observed_deployment_failures"):
            if obs_key not in call or not _is_nonneg_int(call[obs_key]):
                raise ValueError(
                    f"cannot safely resume: invalid {obs_key} in call; start a new experiment or reconcile"
                )
    for opt_token_key in ("cached_tokens", "reasoning_tokens"):
        if opt_token_key in call and call[opt_token_key] is not None and not _is_nonneg_int(call[opt_token_key]):
            raise ValueError(f"cannot safely resume: invalid {opt_token_key} in call")
    for counter_key in ("usage_snapshots", "configured_retries", "estimated_input_tokens", "reserved_input_tokens"):
        if counter_key in call and not _is_nonneg_int(call[counter_key]):
            raise ValueError(f"cannot safely resume: invalid {counter_key} in call")
    for time_key in ("duration_ms", "ttft_ms"):
        if time_key in call and call[time_key] is not None and not _is_finite_nonneg(call[time_key]):
            raise ValueError(f"cannot safely resume: invalid {time_key} in call")


def validate_runtime_resume(report, expected):
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise ValueError("cannot safely resume: report is malformed; start a new experiment or reconcile")
    for key in REQUIRED_MANIFEST_KEYS:
        if key not in report or report[key] != expected[key]:
            raise ValueError(f"resume manifest mismatch on {key}; start a new experiment or reconcile")
    for acct_key in ("elapsed_seconds", "reserved_tokens", "experiment_accounting"):
        if acct_key not in report:
            raise ValueError(
                f"cannot safely resume: missing {acct_key}; start a new experiment or reconcile manually"
            )
    elapsed = report["elapsed_seconds"]
    reserved = report["reserved_tokens"]
    accounting = report["experiment_accounting"]
    if not _is_finite_nonneg(elapsed):
        raise ValueError("cannot safely resume: invalid elapsed_seconds; start a new experiment or reconcile")
    if not _is_nonneg_int(reserved):
        raise ValueError("cannot safely resume: invalid reserved_tokens; start a new experiment or reconcile")
    if not isinstance(accounting, dict) or "calls" not in accounting or not isinstance(accounting["calls"], list):
        raise ValueError("cannot safely resume: invalid experiment_accounting; start a new experiment or reconcile")
    if "reserved_tokens" in accounting and (
        not _is_nonneg_int(accounting["reserved_tokens"]) or accounting["reserved_tokens"] != reserved
    ):
        raise ValueError("cannot safely resume: inconsistent reserved_tokens in experiment_accounting")
    calls = accounting["calls"]
    for call in calls:
        _validate_call_record(call, require_observer_counts=True)
    recomputed = CallLedger(max_calls=1, max_seconds=1, max_estimated_tokens=1).report(calls)
    if any(key in accounting for key in ACCOUNTING_SUMMARY_KEYS):
        for key in ACCOUNTING_SUMMARY_KEYS:
            if (
                key not in accounting
                or type(accounting[key]) is not type(recomputed[key])
                or accounting[key] != recomputed[key]
            ):
                raise ValueError(
                    f"cannot safely resume: inconsistent experiment_accounting.{key}; "
                    "start a new experiment or reconcile"
                )
    if calls:
        min_reserved = sum(
            call.get("estimated_input_tokens", 0) * (1 + call.get("configured_retries", 0)) for call in calls
        )
        if reserved <= 0 or reserved < min_reserved:
            raise ValueError(
                "cannot safely resume: reserved_tokens is smaller than recorded call reservations; "
                "start a new experiment or reconcile"
            )
    seen_keys = set()
    rows_with_accounting = []
    for row in report["results"]:
        if not isinstance(row, dict):
            raise ValueError("cannot safely resume: result row is not an object")
        case_id, variant, web_enabled, status = (
            row.get("case_id"),
            row.get("variant"),
            row.get("web_enabled"),
            row.get("status"),
        )
        if (
            case_id not in expected["case_ids"]
            or variant not in expected["variants"]
            or type(web_enabled) is not bool
            or not isinstance(status, str)
            or not status
        ):
            raise ValueError("cannot safely resume: malformed result row metadata")
        row_key = (case_id, web_enabled, variant)
        if row_key in seen_keys:
            raise ValueError("cannot safely resume: duplicate result row in report")
        seen_keys.add(row_key)
        row_acc = row.get("accounting")
        if row_acc is not None:
            if (
                not isinstance(row_acc, dict)
                or not _is_nonneg_int(row_acc.get("logical_calls"))
                or not isinstance(row_acc.get("calls"), list)
                or len(row_acc["calls"]) != row_acc["logical_calls"]
                or type(row_acc.get("accounting_complete")) is not bool
            ):
                raise ValueError("cannot safely resume: malformed result row accounting")
            for row_call in row_acc["calls"]:
                _validate_call_record(row_call, require_observer_counts=False)
            row_complete = all(
                c["status"] == "completed" and c["input_tokens"] is not None and c["output_tokens"] is not None
                for c in row_acc["calls"]
            )
            if row_acc["accounting_complete"] and not row_complete:
                raise ValueError("cannot safely resume: result row claims complete accounting with unknown usage")
            if not row_acc["accounting_complete"] and (
                row_acc.get("input_tokens") is not None or row_acc.get("output_tokens") is not None
            ):
                raise ValueError("cannot safely resume: incomplete row accounting must not coerce usage to integers")
            rows_with_accounting.append(row_acc)
    sum_row_calls = sum(r["logical_calls"] for r in rows_with_accounting)
    if sum_row_calls > len(calls):
        raise ValueError(
            "cannot safely resume: experiment_accounting has fewer calls than recorded result rows; "
            "start a new experiment or reconcile"
        )
    if rows_with_accounting and len(rows_with_accounting) == len(report["results"]) and sum_row_calls != len(calls):
        raise ValueError(
            "cannot safely resume: experiment_accounting call count does not match result rows; "
            "start a new experiment or reconcile"
        )


async def run(args):
    cases = [case for case in baseline.load_cases() if case["split"] == args.split]
    if args.case:
        requested = list(args.case)
        by_id = {case["id"]: case for case in cases}
        cases = [by_id[cid] for cid in requested if cid in by_id]
    expected = build_runtime_manifest(args, cases)
    output = Path(args.output)
    ledger = CallLedger(max_calls=args.max_calls, max_seconds=args.max_seconds, max_estimated_tokens=args.max_tokens)
    started = time.monotonic()
    if output.exists():
        report = json.loads(output.read_text(encoding="utf-8"))
        validate_runtime_resume(report, expected)
        spent = float(report["elapsed_seconds"])
        ledger.calls = copy.deepcopy(report["experiment_accounting"]["calls"])
        ledger.reserved_tokens = report["reserved_tokens"]
    else:
        spent = 0.0
        report = {
            **expected,
            "results": [],
            "elapsed_seconds": 0.0,
            "experiment_accounting": ledger.report(),
            "reserved_tokens": 0,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    ledger.deadline = started + max(0.0, args.max_seconds - spent)
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
        report["elapsed_seconds"] = max(spent, spent + time.monotonic() - started)
        report["experiment_accounting"] = ledger.report()
        report["reserved_tokens"] = ledger.reserved_tokens
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    executed_any = False
    client = TrackedLLM(settings, ledger)
    with ledger.capture_deployments(), patch.object(model, "session_completion", observed):
        for case in cases:
            for web in (False, True):
                for variant in ("legacy", "runtime") if args.variants == "both" else (args.variants,):
                    key = (case["id"], web, variant)
                    if any((row["case_id"], row["web_enabled"], row["variant"]) == key for row in report["results"]):
                        continue
                    if (
                        len(ledger.calls) >= args.max_calls
                        or ledger.reserved_tokens >= args.max_tokens
                        or spent >= args.max_seconds
                        or time.monotonic() >= ledger.deadline
                    ):
                        if executed_any or not output.exists():
                            save()
                        return
                    executed_any = True
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
    if executed_any or not output.exists():
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
