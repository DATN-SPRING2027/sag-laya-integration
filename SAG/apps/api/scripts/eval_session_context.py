"""Replay synthetic session fixtures through unchanged legacy chat services.

No network is used unless --live is explicit. No production database is opened.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import platform
import statistics
import subprocess
import sys
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from sag_api.core.config import settings  # noqa: E402
from sag_api.core.principal_assertion import VerifiedPrincipal  # noqa: E402
from sag_api.db.base import Base  # noqa: E402
from sag_api.db.models import Agent, AgentBinding, Message, Thread  # noqa: E402
from sag_api.enums import MessageRole  # noqa: E402
from sag_api.generation.prompt import estimate_tokens  # noqa: E402
from sag_api.sag import EngineManager  # noqa: E402
from sag_api.services import agent_domain, agent_service  # noqa: E402
from sag_api.services.eval.session_context_accounting import (  # noqa: E402
    BaselineBudgetExceeded,
    CallLedger,
    TrackedLLM,
)
from sag_api.tools import registry  # noqa: E402

CORPUS = API_ROOT / "tests" / "fixtures" / "session_context.jsonl"
FILLER = (
    "Ghi chú luyện tập cá nhân: tôi đã đi bộ quanh công viên và quan sát cây xanh. "
    "Buổi tập này không đưa ra quyết định về dự án, ngân sách, mã cấu hình hay người phụ trách. "
    "Assistant xác nhận đã ghi nhận hoạt động và không bổ sung yêu cầu nghiệp vụ. "
)


def load_cases(path=CORPUS):
    cases = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    for case in cases:
        messages = expand_case(case)
        if case["split"] not in {"calibration", "holdout", "validation"} or case["group"] not in {
            "normal",
            "full_session",
        }:
            raise ValueError("invalid corpus split/group")
        for item in case["gold_items"]:
            if not item["aliases"]:
                raise ValueError("gold item must have aliases")
            for evidence in item["evidence"]:
                if evidence["quote"] not in messages[evidence["at"]]["content"]:
                    raise ValueError("gold quote does not match source message")
    return cases


def expand_case(case):
    if not 0 <= case["message_count"] <= 2000 or not 1 <= case["filler_repeat"] <= 30:
        raise ValueError("corpus expansion exceeds permitted size")
    facts = {fact["at"]: fact for fact in case["facts"]}
    if len(facts) != len(case["facts"]) or any(not 0 <= at < case["message_count"] for at in facts):
        raise ValueError("invalid fact position")
    start = datetime(2026, 10, 1, tzinfo=UTC)
    result = []
    for index in range(case["message_count"]):
        fact = facts.get(index, {})
        result.append(
            {
                "id": hashlib.sha256(f'{case["id"]}:{index}'.encode()).hexdigest()[:32],
                "role": fact.get("role", "user" if index % 2 == 0 else "assistant"),
                "content": fact.get("content", f"Hoạt động số {index}. " + FILLER * case["filler_repeat"]),
                "created_at": (start + timedelta(minutes=index)).isoformat(),
            }
        )
    return result


def _normalized(text):
    return "".join(
        character for character in unicodedata.normalize("NFKC", text).casefold() if character.isalnum()
    )


def score_answer(case, answer):
    found = [
        item["id"]
        for item in case["gold_items"]
        if any(_normalized(alias) in _normalized(answer) for alias in item["aliases"])
    ]
    return {
        "literal_item_recall": len(found) / len(case["gold_items"]) if case["gold_items"] else None,
        "matched_item_ids": found,
        "semantic_correctness": "requires_review",
    }


def _validate_evidence_entries(entries, *, field_name: str) -> list[dict]:
    if not isinstance(entries, (list, tuple)):
        raise ValueError(f"{field_name} must be a list of evidence objects")
    validated = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{field_name}[{idx}] must be a dict")
        for key in ("message_id", "kind", "text"):
            if key not in entry or not isinstance(entry[key], str):
                raise ValueError(f"{field_name}[{idx}] missing or invalid string field {key!r}")
        if not entry["message_id"].strip():
            raise ValueError(f"{field_name}[{idx}].message_id must be non-empty")
        if entry["kind"] not in {"user_statement", "assistant_statement"}:
            raise ValueError(f"{field_name}[{idx}].kind must be 'user_statement' or 'assistant_statement'")
        validated.append(
            {
                "message_id": entry["message_id"],
                "kind": entry["kind"],
                "text": entry["text"],
            }
        )
    return validated


def parse_fitted_evidence_messages(evidence_messages) -> list[dict]:
    if not isinstance(evidence_messages, (list, tuple)):
        raise ValueError("evidence_messages must be a list of SESSION_EVIDENCE_JSON strings")
    extracted: list[dict] = []
    for msg_idx, raw_msg in enumerate(evidence_messages):
        if isinstance(raw_msg, dict):
            if "content" not in raw_msg or not isinstance(raw_msg["content"], str):
                raise ValueError(f"evidence_messages[{msg_idx}] dict missing string 'content'")
            content = raw_msg["content"]
        elif isinstance(raw_msg, str):
            content = raw_msg
        else:
            raise ValueError(f"evidence_messages[{msg_idx}] must be a string or message dict")
        prefix = "SESSION_EVIDENCE_JSON\n"
        if not content.startswith(prefix):
            raise ValueError(f"evidence_messages[{msg_idx}] does not start with SESSION_EVIDENCE_JSON")
        try:
            payload = json.loads(content[len(prefix) :])
        except json.JSONDecodeError as error:
            raise ValueError(f"evidence_messages[{msg_idx}] contains invalid JSON") from error
        if isinstance(payload, list):
            items_payload = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items_payload = payload["items"]
        else:
            raise ValueError(f"evidence_messages[{msg_idx}] JSON must be a list of evidence items")
        extracted.extend(_validate_evidence_entries(items_payload, field_name=f"evidence_messages[{msg_idx}]"))
    return extracted


def score_selected_evidence(
    case: dict,
    expanded_messages: list[dict],
    selected_evidence,
    *,
    allowed_kinds: frozenset[str] = frozenset({"user_statement", "assistant_statement"}),
) -> dict:
    if not isinstance(case, dict) or "gold_items" not in case or not isinstance(case["gold_items"], list):
        raise ValueError("case must contain a 'gold_items' list")
    if not isinstance(expanded_messages, (list, tuple)) or not expanded_messages:
        raise ValueError("expanded_messages must be a non-empty list")
    entries = _validate_evidence_entries(selected_evidence, field_name="selected_evidence")
    items = [item for item in case["gold_items"] if item.get("evidence")]
    covered = []
    for item in items:
        item_ok = True
        for ev in item["evidence"]:
            if not isinstance(ev, dict) or "at" not in ev or "quote" not in ev:
                raise ValueError("malformed gold evidence item")
            at = ev["at"]
            quote = ev["quote"]
            if not isinstance(at, int) or not (0 <= at < len(expanded_messages)) or not isinstance(quote, str):
                raise ValueError("gold evidence position/quote out of bounds")
            expected_msg = expanded_messages[at]
            expected_msg_id = expected_msg["id"]
            expected_kind = f"{expected_msg['role']}_statement"
            if not any(
                entry["message_id"] == expected_msg_id
                and entry["kind"] == expected_kind
                and entry["kind"] in allowed_kinds
                and quote in entry["text"]
                for entry in entries
            ):
                item_ok = False
                break
        if item_ok:
            covered.append(item["id"])
    return {
        "item_evidence_recall": len(covered) / len(items) if items else None,
        "gold_evidence_items": len(items),
        "covered_evidence_items": covered,
        "uncovered_evidence_items": [item["id"] for item in items if item["id"] not in covered],
    }


def score_fitted_evidence_messages(
    case: dict,
    expanded_messages: list[dict],
    evidence_messages,
    *,
    allowed_kinds: frozenset[str] = frozenset({"user_statement", "assistant_statement"}),
) -> dict:
    entries = parse_fitted_evidence_messages(evidence_messages)
    return score_selected_evidence(case, expanded_messages, entries, allowed_kinds=allowed_kinds)


async def replay_case(case, client, ledger, *, web_enabled, history_override=None):
    messages = expand_case(case)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tables = [Agent.__table__, Thread.__table__, Message.__table__, AgentBinding.__table__]
    start = time.monotonic()
    call_start = len(ledger.calls)
    result = {
        "case_id": case["id"],
        "group": case["group"],
        "scenario": case["scenario"],
        "web_enabled": web_enabled,
        "history_message_count": len(messages),
    }
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        principal = VerifiedPrincipal(
            "synthetic-user",
            "synthetic-org",
            frozenset({"synthetic-project"}),
            "baseline-fixture",
            "fixture",
            "fixture",
            0,
            2_000_000_000,
        )
        async with factory() as session:
            agent = Agent(id="baseline-agent", owner_key="synthetic-owner", name="Baseline Agent", persona={})
            thread = Thread(id="baseline-thread", agent_id=agent.id, owner_key="synthetic-owner", title="Baseline")
            session.add_all([agent, thread])
            await session.flush()
            session.add_all(
                [
                    Message(
                        id=m["id"],
                        thread_id=thread.id,
                        role=MessageRole(m["role"]),
                        content=m["content"],
                        citations=[],
                        steps=[],
                        attachments=[],
                        created_at=datetime.fromisoformat(m["created_at"]),
                    )
                    for m in messages
                ]
            )
            await session.commit()
            ledger.stage = "compression"
            if time.monotonic() >= ledger.deadline:
                raise BaselineBudgetExceeded("benchmark resource limit reached")
            async with asyncio.timeout_at(ledger.deadline):
                plan = await agent_domain.prepare_ask(
                    session,
                    agent=agent,
                    thread=thread,
                    query=case["query"],
                    principal=principal,
                    owner_key="synthetic-owner",
                    llm=client if history_override is None else None,
                    context_deadline=ledger.deadline,
                )
            if history_override is not None:
                # Benchmark-only candidate seam: same prompt/runtime/tool policy,
                # selected history replaces legacy context without a compression LLM call.
                selected = agent_domain.build_ask_context(
                    agent=agent, query=case["query"], history=history_override
                )
                selected.user_message_id = plan.user_message_id
                plan = selected
        initial_history = "\n".join(str(m["content"]) for m in plan.messages[1:-1])
        gold_items = case["gold_items"]
        result["loaded_message_count"] = min(len(messages), settings.history_load_limit)
        result["compression_called"] = len(ledger.calls) > call_start
        result["gold_verbatim_in_initial_context"] = sum(
            bool(item["evidence"]) and all(evidence["quote"] in initial_history for evidence in item["evidence"])
            for item in gold_items
        )
        result["gold_items_with_evidence"] = sum(bool(item["evidence"]) for item in gold_items)
        result["gold_item_count"] = len(gold_items)
        ledger.stage = "generation"
        result["status"] = "no_terminal_event"
        if time.monotonic() >= ledger.deadline:
            raise BaselineBudgetExceeded("benchmark resource limit reached")
        async with asyncio.timeout_at(ledger.deadline):
            async for event in agent_service.generate_stream(
                factory,
                plan=plan,
                agent=agent,
                thread_id=thread.id,
                engine_manager=EngineManager(settings),
                llm=client,
                tool_registry=registry,
                knowledge_only=not web_enabled,
                principal=principal,
            ):
                if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                    result["status"] = event.type
                    payload = event.data["payload"]
                    result["answer"] = str(payload.get("output") or "")
                    result["answer_status"] = payload.get("answer_status")
                    result["runtime_usage"] = payload.get("usage")
                    if event.type != "run.completed":
                        result["runtime_error_code"] = (payload.get("error") or {}).get("code")
        result.update(score_answer(case, result.get("answer", "")))
        async with factory() as session:
            persisted = await session.scalars(select(Message).where(Message.thread_id == thread.id))
            result["persisted_message_count"] = len(persisted.all())
    except Exception as error:
        result.update(status="error", error_type=type(error).__name__)
    finally:
        await engine.dispose()
    result["duration_ms"] = round((time.monotonic() - start) * 1000, 3)
    result["accounting"] = ledger.report(ledger.calls[call_start:])
    return result


def manifest(cases, *, corpus_path=CORPUS):
    return {
        "corpus_sha256": hashlib.sha256(Path(corpus_path).read_bytes()).hexdigest(),
        "expansion_sha256": hashlib.sha256(
            json.dumps([expand_case(c) for c in cases], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "cases": [
            {
                "id": case["id"],
                "split": case["split"],
                "group": case["group"],
                "messages": case["message_count"],
                "estimated_history_tokens": sum(
                    estimate_tokens(m["content"]) for m in expand_case(case)[-settings.history_load_limit :]
                ),
            }
            for case in cases
        ],
    }


async def run(args):
    corpus_path = Path(getattr(args, "corpus", None) or CORPUS)
    cases = [case for case in load_cases(corpus_path) if case["split"] == args.split]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    if not cases:
        raise ValueError("no matching corpus cases")
    report = {
        "schema_version": 1,
        "mode": "live" if args.live else "corpus_validation_only",
        "created_at": datetime.now(UTC).isoformat(),
        "manifest": manifest(cases, corpus_path=corpus_path),
        "scope": "synthetic disposable SQLite, real prepare_ask/generate_stream; no document sources",
        "holdout_executed": args.live and args.split == "holdout",
        "results": [],
    }
    report["config"] = {
        key: getattr(settings, key)
        for key in (
            "llm_provider",
            "llm_model",
            "llm_context_window",
            "llm_max_tokens",
            "llm_max_retries",
            "llm_timeout_ms",
            "llm_temperature",
            "history_load_limit",
            "history_keep_recent",
            "agent_max_steps",
        )
    }
    report["python_version"] = platform.python_version()
    report["dependency_versions"] = {
        name: importlib.metadata.version(name) for name in ("litellm", "sqlalchemy", "aiosqlite")
    }
    report["source_sha256"] = {
        str(path.relative_to(API_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            Path(__file__),
            API_ROOT / "sag_api/services/agent_domain.py",
            API_ROOT / "sag_api/services/agent_service.py",
            API_ROOT / "sag_api/generation/llm.py",
            API_ROOT / "sag_api/generation/prompt.py",
            API_ROOT / "sag_api/services/eval/session_context_accounting.py",
        ]
    }
    report["llm_extra_body_sha256"] = hashlib.sha256(
        json.dumps(settings.llm_extra_body, sort_keys=True).encode(),
    ).hexdigest()
    report["git_revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=API_ROOT,
        text=True,
    ).strip()
    if args.live:
        if not settings.llm_configured:
            raise ValueError("configured LLM credentials are required for --live")
        ledger = CallLedger(
            max_calls=args.max_calls,
            max_seconds=args.max_seconds,
            max_estimated_tokens=args.max_estimated_tokens,
        )
        client = TrackedLLM(settings, ledger)
        profiles = [False, True] if args.profile == "both" else [args.profile == "web-enabled"]
        report["resource_caps"] = {
            "logical_calls": args.max_calls,
            "seconds": args.max_seconds,
            "worst_case_estimated_tokens": args.max_estimated_tokens,
        }
        report["requested_replays"] = len(cases) * len(profiles)
        with ledger.capture_deployments():
            for case in cases:
                for web_enabled in profiles:
                    if len(ledger.calls) >= ledger.max_calls or time.monotonic() >= ledger.deadline:
                        break
                    result = await replay_case(case, client, ledger, web_enabled=web_enabled)
                    report["results"].append(result)
                    print(
                        json.dumps(
                            {
                                key: result.get(key)
                                for key in (
                                    "case_id",
                                    "web_enabled",
                                    "status",
                                    "literal_item_recall",
                                    "duration_ms",
                                )
                            }
                        ),
                        flush=True,
                    )
                    if result.get("error_type") == BaselineBudgetExceeded.__name__:
                        break
        report["accounting"] = ledger.report()
        report["execution_status"] = (
            "completed"
            if (
                len(report["results"]) == report["requested_replays"]
                and all(result["status"] == "run.completed" for result in report["results"])
            )
            else "partial"
        )
        report["summaries"] = []
        for group in ("normal", "full_session"):
            for enabled in profiles:
                rows = [r for r in report["results"] if r["group"] == group and r["web_enabled"] == enabled]
                scores = [r["literal_item_recall"] for r in rows if r.get("literal_item_recall") is not None]
                durations = sorted(r["duration_ms"] for r in rows)
                report["summaries"].append(
                    {
                        "group": group,
                        "web_enabled": enabled,
                        "n": len(rows),
                        "mean_literal_item_recall": statistics.mean(scores) if scores else None,
                        "p50_ms": statistics.median(durations) if durations else None,
                        "p95_ms": (
                            durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else None
                        ),
                        "small_sample": len(rows) < 20,
                    }
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.output), "cases": len(cases), "live": args.live}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call the configured provider with synthetic text")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--split", choices=["calibration", "holdout", "validation"], default="calibration")
    parser.add_argument("--case", action="append")
    parser.add_argument("--profile", choices=["both", "web-disabled", "web-enabled"], default="both")
    parser.add_argument("--output", type=Path, default=API_ROOT / ".data/session-context/baseline.json")
    parser.add_argument("--max-calls", type=int, default=64)
    parser.add_argument("--max-seconds", type=float, default=1200)
    parser.add_argument("--max-estimated-tokens", type=int, default=2_000_000)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)  # never dump provider exceptions or request bodies from a benchmark
    try:
        asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
