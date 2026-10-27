"""Calibration-only structural sweep and opt-in paired provider replay.

Default validates the manifest. --sweep loads pinned local encoders; --live
also calls the configured answer provider with synthetic text. No holdout mode.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sag_api.core.config import settings  # noqa: E402
from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM  # noqa: E402
from sag_api.services.eval.session_context_calibration import (  # noqa: E402
    calibration_cases,
    choose_profile,
    evidence_metrics,
    paired_tokens,
    profiles,
    provider_route_sha256,
    validate_resume,
)
from sag_api.services.session_context.budget import BudgetLimits, BudgetState  # noqa: E402
from sag_api.services.session_context.graph import (  # noqa: E402
    EntityMention,
    LocalFeatureEncoder,
    TextFeatures,
    build_graph,
)
from sag_api.services.session_context.hierarchy import build_hierarchy  # noqa: E402
from sag_api.services.session_context.reader import FixtureTraceStore, read_history  # noqa: E402
from sag_api.services.session_context.scope import make_snapshot, validate_selection  # noqa: E402
from sag_api.services.session_context.selection import select_context  # noqa: E402
from sag_api.services.session_context.types import SessionKey, SessionScope, TraceMessage  # noqa: E402
from scripts.eval_session_context import CORPUS, expand_case, replay_case  # noqa: E402


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _persist_atomic(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def save(path: Path, report: dict):
    _persist_atomic(path, report)


def usage(encoder):
    return {key: getattr(encoder, key) for key in ("encoder_calls", "encoder_tokens", "ner_calls", "ner_tokens")}


def run_sweep(cases, args, report):
    import torch

    torch.set_num_threads(4)
    deadline = time.monotonic() + args.max_seconds

    def work():
        return BudgetState(BudgetLimits(128000, 4096, 4, 200000, 100_000_000, deadline))

    load_started = time.monotonic()
    encoder = LocalFeatureEncoder(chunk_codepoints=384, budget=work())
    report.update(
        model_id=encoder.model_id,
        model_load_ms=round((time.monotonic() - load_started) * 1000, 3),
        cpu_threads=4,
        hierarchy={"window_turns": 4, "episode_windows": 4, "local_radius": 1, "gap_seconds": 1800},
    )
    selected_contexts = {}
    for case in cases:
        print(json.dumps({"phase": "features_started", "case_id": case["id"]}), flush=True)
        started, before, budget = time.monotonic(), usage(encoder), work()
        expanded = expand_case(case)
        scope = SessionScope(
            SessionKey("calibration-owner", "calibration-agent", case["id"]),
            "fixture-issuer",
            "fixture-subject",
            "fixture-org",
            "",
            frozenset({"fixture-project"}),
            frozenset(),
            frozenset(),
            None,
            2_000_000_000,
        )
        messages = tuple(
            TraceMessage(
                scope.key,
                m["id"],
                m["role"],
                m["content"],
                datetime.fromisoformat(m["created_at"]),
                provenance="recorded",
                partition_fingerprint=scope.partition_fingerprint,
            )
            for m in expanded
        )
        store = FixtureTraceStore(messages, scope=scope, generation="calibration-v1", budget=budget)
        history = read_history(store, scope=scope, page_size=37, budget=budget)
        cache_key = sha(
            json.dumps(
                {
                    "messages": expanded,
                    "scope": scope.fingerprint,
                    "model": encoder.model_id,
                    "source": report["source_sha256"]["graph.py"],
                },
                sort_keys=True,
            ).encode()
        )
        cache_path = args.output.parent / "features" / f"{case['id']}-{cache_key[:16]}.json"
        reused = args.resume and cache_path.exists()
        if reused:
            artifact = json.loads(cache_path.read_text(encoding="utf-8"))
            if artifact["cache_key"] != cache_key:
                raise ValueError("feature cache identity mismatch")
            features = tuple(
                TextFeatures(
                    f["content_hash"],
                    f["model_id"],
                    tuple(f["vector"]),
                    tuple(EntityMention(**e) for e in f["mentions"]),
                )
                for f in artifact["features"]
            )
        else:
            features = encoder.featurize_many(
                tuple(m.content for m in history.messages), batch_size=16, budget=budget, deduplicate=True
            )
            chunks = [
                m.content[start : start + 384]
                for m in history.messages
                for start in range(0, max(1, len(m.content)), 384)
            ]
            artifact = {
                "cache_key": cache_key,
                "features": [asdict(f) for f in features],
                "cold_feature_ms": round((time.monotonic() - started) * 1000, 3),
                "logical_chunks": len(chunks),
                "unique_chunks": len(set(chunks)),
                "feature_usage": {key: usage(encoder)[key] - before[key] for key in before},
            }
            save(cache_path, artifact)
        build_started = time.monotonic()
        graph = build_graph(history, features, scope=scope, budget=budget, gap_seconds=1800)
        hierarchy = build_hierarchy(history, scope=scope, budget=budget, **report["hierarchy"])
        build_ms = (time.monotonic() - build_started) * 1000
        query_started = time.monotonic()
        query = encoder.featurize(case["query"], budget=budget)
        query_ms = (time.monotonic() - query_started) * 1000
        records = {m.message_id: m for m in messages}
        case_contexts = {}
        for name, caps in profiles().items():
            selection_started = time.monotonic()
            result = select_context(graph, hierarchy, case["query"], query, scope=scope, budget=budget, caps=caps)
            resolved = validate_selection(
                result.selection,
                scope=scope,
                current_snapshot=make_snapshot(scope, records, generation="calibration-v1"),
                records=records,
            )
            selected_ids = {item.span.ref.message_id for item in resolved}
            # T3b returns full-message spans; reject any future partial-span drift
            # before scoring quote containment using message IDs.
            if any(
                item.span.start != 0 or item.span.end != len(records[item.span.ref.message_id].content)
                for item in resolved
            ):
                raise ValueError("calibration metric requires full raw spans")
            contexts = [{"role": m.role, "content": m.content} for m in messages if m.message_id in selected_ids]
            row = {
                "case_id": case["id"],
                "group": case["group"],
                "profile": name,
                "selected_ids": sorted(selected_ids),
                "selected_messages": len(contexts),
                "selected_codepoints": sum(len(m["content"]) for m in contexts),
                "selection_state": result.selection.state,
                "selection_reason": result.selection.reason,
                "route": result.route,
                "selection_ms": round((time.monotonic() - selection_started) * 1000, 3),
                **evidence_metrics(case, expanded, selected_ids),
            }
            report["sweep"].append(row)
            case_contexts[name] = contexts
            warm_started = time.monotonic()
            warm_query = encoder.featurize(case["query"], budget=budget)
            warm_result = select_context(
                graph, hierarchy, case["query"], warm_query, scope=scope, budget=budget, caps=caps
            )
            if warm_result.selection.spans != result.selection.spans:
                raise ValueError("warm selection changed with identical input")
            row["warm_query_ms"] = round((time.monotonic() - warm_started) * 1000, 3)
            row["cold_query_ms_excluding_model_load"] = round(
                artifact["cold_feature_ms"] + build_ms + query_ms + row["selection_ms"], 3
            )
        report["feature_measurements"].append(
            {
                "case_id": case["id"],
                "reused_feature_artifact": reused,
                "cold_feature_ms": artifact["cold_feature_ms"],
                "feature_usage": artifact["feature_usage"],
                "logical_chunks": artifact["logical_chunks"],
                "unique_chunks": artifact["unique_chunks"],
                "current_case_ms": round((time.monotonic() - started) * 1000, 3),
                "work_units": budget.index_units,
            }
        )
        selected_contexts[case["id"]] = case_contexts
        save(args.output, report)
        print(
            json.dumps(
                {
                    "phase": "features_completed",
                    "case_id": case["id"],
                    "ms": report["feature_measurements"][-1]["current_case_ms"],
                }
            ),
            flush=True,
        )
    report["feature_usage_current_execution"] = usage(encoder)
    report["locked_profile"] = choose_profile(
        report["sweep"],
        expected_normal_ids={case["id"] for case in calibration_cases(CORPUS) if case["group"] == "normal"},
    )
    report["profile_locked_before_provider_answers"] = True
    return selected_contexts


async def live_replay(cases, contexts, args, report):
    previous = json.loads(args.resume_provider_from.read_text(encoding="utf-8")) if args.resume_provider_from else None
    replay_elapsed = (
        sum(row["duration_ms"] for row in previous["results"] + previous.get("excluded_attempts", [])) / 1000
        if previous
        else 0
    )
    prior_wall_elapsed = float(previous.get("elapsed_seconds", replay_elapsed)) if previous else 0.0
    sweep_elapsed = float(report.get("sweep_elapsed_seconds", 0.0) or 0.0)
    elapsed = max(replay_elapsed, prior_wall_elapsed) + sweep_elapsed
    max_total_seconds = float(getattr(args, "max_seconds", 1200) or 1200)
    replay_cap_seconds = min(1200.0, max_total_seconds)
    if elapsed >= replay_cap_seconds:
        raise ValueError("provider resume time budget exhausted")
    ledger = CallLedger(
        max_calls=args.max_calls,
        max_seconds=replay_cap_seconds - elapsed,
        max_estimated_tokens=args.max_estimated_tokens,
    )
    started = time.monotonic()
    env_route_present = any(
        bool(os.environ.get(env_key, "").strip())
        for env_key in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "LITELLM_BASE_URL")
    )
    if (
        previous
        or env_route_present
        or "provider_route_sha256" in report
        or (settings.llm_base_url and settings.llm_base_url.strip())
    ):
        report["provider_route_sha256"] = provider_route_sha256(settings)
    if previous:
        validate_resume(previous, report)
        ledger.calls = list(previous["accounting"]["calls"])
        ledger.reserved_tokens = sum(
            (call["estimated_input_tokens"] + call.get("max_output_tokens", settings.llm_max_tokens))
            * (1 + call["configured_retries"])
            for call in ledger.calls
        )
        report["results"] = [row for row in previous["results"] if row["status"] == "run.completed"]
        report["excluded_attempts"] = previous.get("excluded_attempts", []) + [
            row for row in previous["results"] if row["status"] != "run.completed"
        ]
        report["resume_from_sha256"] = sha(args.resume_provider_from.read_bytes())
        report["prior_replay_seconds"] = elapsed
    report["provider_resource_caps"] = {
        "cumulative_calls": args.max_calls,
        "replay_seconds": replay_cap_seconds,
        "cumulative_estimated_tokens": args.max_estimated_tokens,
    }
    output_path = Path(args.output) if getattr(args, "output", None) is not None else None

    def _sync_report_state():
        report["elapsed_seconds"] = round(max(elapsed, elapsed + time.monotonic() - started), 3)
        report["replay_duration_seconds"] = round(
            sum(
                float(row.get("duration_ms") or 0.0)
                for row in report.get("results", []) + report.get("excluded_attempts", [])
            )
            / 1000.0,
            3,
        )
        report["accounting"] = ledger.report()
        report["reserved_tokens"] = ledger.reserved_tokens

    def _persist_in_flight():
        _sync_report_state()
        if output_path is not None:
            _persist_atomic(output_path, report)

    def checkpoint():
        _sync_report_state()
        save(args.output, report)

    ledger.on_change = _persist_in_flight

    completed = {
        (row["case_id"], row["web_enabled"], row["variant"], row["iteration"]) for row in report["results"]
    }
    client = TrackedLLM(settings, ledger)
    name = report["locked_profile"]
    if name is None:
        report["live_status"] = "not_started_no_profile_covers_normal_evidence"
        return
    with ledger.capture_deployments():
        for case in cases:
            for enabled in (False, True):
                if (case["id"], enabled, "paired_legacy", 0) not in completed:
                    if (
                        len(ledger.calls) >= args.max_calls
                        or ledger.reserved_tokens >= args.max_estimated_tokens
                        or time.monotonic() >= ledger.deadline
                    ):
                        break
                    report["in_flight_replay"] = {
                        "case_id": case["id"],
                        "web_enabled": enabled,
                        "variant": "paired_legacy",
                        "iteration": 0,
                    }
                    _persist_in_flight()
                    baseline = await replay_case(case, client, ledger, web_enabled=enabled)
                    baseline.update(variant="paired_legacy", iteration=0)
                    report.pop("in_flight_replay", None)
                    report["results"].append(baseline)
                    checkpoint()
                for iteration in range(2):
                    if (case["id"], enabled, "candidate", iteration) in completed:
                        continue
                    if (
                        len(ledger.calls) >= args.max_calls
                        or ledger.reserved_tokens >= args.max_estimated_tokens
                        or time.monotonic() >= ledger.deadline
                    ):
                        break
                    report["in_flight_replay"] = {
                        "case_id": case["id"],
                        "web_enabled": enabled,
                        "variant": "candidate",
                        "iteration": iteration,
                    }
                    _persist_in_flight()
                    candidate = await replay_case(
                        case,
                        client,
                        ledger,
                        web_enabled=enabled,
                        history_override=contexts[case["id"]][name],
                    )
                    candidate.update(
                        variant="candidate",
                        iteration=iteration,
                        mode_limitation=(
                            "normal_selector_only_T8_pending" if case["group"] == "full_session" else None
                        ),
                    )
                    report.pop("in_flight_replay", None)
                    report["results"].append(candidate)
                    checkpoint()
                print(
                    json.dumps({"phase": "provider_completed", "case_id": case["id"], "web_enabled": enabled}),
                    flush=True,
                )
    _sync_report_state()
    report["execution_status"] = (
        "completed"
        if len(report["results"]) == len(cases) * 6
        and all(row["status"] == "run.completed" for row in report["results"])
        else "incomplete"
    )
    report["comparisons"] = []
    for group in ("normal", "full_session"):
        for enabled in (False, True):
            for iteration in range(2):
                for long_only in (False, True):
                    rows = [
                        r
                        for r in report["results"]
                        if r["group"] == group
                        and r["web_enabled"] == enabled
                        and (not long_only or r["history_message_count"] >= 120)
                    ]
                    before = [r for r in rows if r["variant"] == "paired_legacy"]
                    after = [r for r in rows if r["variant"] == "candidate" and r["iteration"] == iteration]
                    report["comparisons"].append(
                        {
                            "group": group,
                            "web_enabled": enabled,
                            "iteration": iteration,
                            "long_only": long_only,
                            **paired_tokens(before, after),
                            "baseline_literal": (
                                statistics.mean(r["literal_item_recall"] for r in before) if before else None
                            ),
                            "candidate_literal": (
                                statistics.mean(r["literal_item_recall"] for r in after) if after else None
                            ),
                        }
                    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-provider-from", type=Path)
    parser.add_argument("--max-calls", type=int, default=128)
    parser.add_argument("--max-estimated-tokens", type=int, default=4_000_000)
    parser.add_argument("--max-seconds", type=float, default=1800)
    parser.add_argument("--output", type=Path, default=ROOT / ".data/session-context/calibration.json")
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 3600:
        parser.error("max-seconds must be in (0,3600]")
    if not 0 < args.max_calls <= 256 or not 0 < args.max_estimated_tokens <= 8_000_000:
        parser.error("invalid finite provider experiment caps")
    if args.resume_provider_from and not (args.live and args.resume):
        parser.error("provider resume requires --live --resume")
    if args.resume_provider_from and args.resume_provider_from.resolve() == args.output.resolve():
        parser.error("provider resume must write a new output file")
    logging.disable(logging.CRITICAL)
    cases = calibration_cases(CORPUS)
    source_paths = [
        ROOT / "sag_api/services/session_context" / name
        for name in ("graph.py", "selection.py", "hierarchy.py", "reader.py", "scope.py", "budget.py", "types.py")
    ]
    source_paths += [
        Path(__file__),
        ROOT / "scripts/eval_session_context.py",
        ROOT / "sag_api/services/eval/session_context_calibration.py",
    ]
    source_paths += [
        ROOT / "sag_api" / name
        for name in (
            "services/agent_domain.py",
            "services/agent_service.py",
            "generation/llm.py",
            "generation/prompt.py",
        )
    ]
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "holdout_executed": False,
        "case_ids": [c["id"] for c in cases],
        "corpus_sha256": sha(CORPUS.read_bytes()),
        "source_sha256": {p.name: sha(p.read_bytes()) for p in source_paths},
        "profiles": {name: asdict(caps) for name, caps in profiles().items()},
        "sweep": [],
        "feature_measurements": [],
        "results": [],
        "config": {
            key: getattr(settings, key)
            for key in (
                "llm_provider",
                "llm_model",
                "llm_context_window",
                "llm_max_tokens",
                "llm_temperature",
                "llm_max_retries",
                "llm_timeout_ms",
            )
        },
    }
    if args.live or (settings.llm_base_url and settings.llm_base_url.strip()):
        report["provider_route_sha256"] = provider_route_sha256(settings)
    report["git_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report["llm_extra_body_sha256"] = sha(json.dumps(settings.llm_extra_body, sort_keys=True).encode())
    report["dependency_versions"] = {
        name: importlib.metadata.version(name)
        for name in ("litellm", "sentence-transformers", "transformers", "torch")
    }
    report["feature_execution"] = "batch16, CPU threads4, per-case exact chunk reuse, exact warm feature reuse"
    save(args.output, report)
    if args.sweep or args.live:
        contexts = run_sweep(cases, args, report)
        save(args.output, report)
        if args.live:
            asyncio.run(live_replay(cases, contexts, args, report))
    save(args.output, report)
    print(json.dumps({"report": str(args.output), "locked_profile": report.get("locked_profile")}), flush=True)


if __name__ == "__main__":
    main()
