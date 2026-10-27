"""Recompute F2/F3 diagnostic validation metrics from immutable v1/v2 artifacts.

Writes a new corrigendum report without modifying any historical artifact.
Uses strict evidence scoring helpers from scripts/eval_session_context.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from scripts.eval_session_context import (  # noqa: E402
    expand_case,
    load_cases,
    score_fitted_evidence_messages,
    score_selected_evidence,
)

REPO_ROOT = API_ROOT.parents[2]
DOC_DIR = REPO_ROOT / "SAG" / "docs" / "Thang_Task" / "session-context-zero-mem"
EVIDENCE_DIR = DOC_DIR / "evidence"
DEFAULT_CORPUS = API_ROOT / "tests" / "fixtures" / "session_context_validation_f2_f3.jsonl"
DEFAULT_V1 = EVIDENCE_DIR / "f2-f3-independent-validation-runtime-v1.json"
DEFAULT_V2 = EVIDENCE_DIR / "f2-f3-independent-validation-runtime-v2.json"
DEFAULT_SEMANTIC = EVIDENCE_DIR / "f2-f3-independent-validation.semantic-review.jsonl"
DEFAULT_SUMMARY = EVIDENCE_DIR / "f2-f3-independent-validation.summary.json"
DEFAULT_OUTPUT = EVIDENCE_DIR / "f2-f3-independent-validation.corrigendum.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_corrigendum(
    *,
    corpus_path: Path = DEFAULT_CORPUS,
    v1_path: Path = DEFAULT_V1,
    v2_path: Path = DEFAULT_V2,
    semantic_path: Path = DEFAULT_SEMANTIC,
    summary_path: Path = DEFAULT_SUMMARY,
) -> dict:
    cases = {case["id"]: case for case in load_cases(corpus_path)}
    expanded_by_id = {cid: expand_case(case) for cid, case in cases.items()}
    v1 = json.loads(v1_path.read_text(encoding="utf-8"))
    v2 = json.loads(v2_path.read_text(encoding="utf-8"))
    semantic_rows = [
        json.loads(line)
        for line in semantic_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    v2_runtime_rows = {
        (row["case_id"], row["web_enabled"]): row
        for row in v2["results"]
        if row.get("variant") == "runtime"
    }

    recomputed_normal_rows = []
    raw_covered_total = 0
    raw_gold_total = 0
    prefit_covered_total = 0
    prefit_gold_total = 0

    for cid, case in sorted(cases.items()):
        if case["group"] != "normal":
            continue
        expanded = expanded_by_id[cid]
        for web_enabled in (False, True):
            row = v2_runtime_rows[(cid, web_enabled)]
            raw_metrics = score_selected_evidence(case, expanded, row.get("raw_selected_evidence"))
            prefit_metrics = score_fitted_evidence_messages(
                case, expanded, row.get("fitted_evidence_messages")
            )
            historical_pair = next(
                item
                for item in summary["paired_results"]
                if item["case_id"] == cid and item["web_enabled"] is web_enabled
            )
            historical_sem = next(
                item
                for item in semantic_rows
                if item["case_id"] == cid and item["web_enabled"] is web_enabled
            )
            raw_covered_total += len(raw_metrics["covered_evidence_items"])
            raw_gold_total += raw_metrics["gold_evidence_items"]
            prefit_covered_total += len(prefit_metrics["covered_evidence_items"])
            prefit_gold_total += prefit_metrics["gold_evidence_items"]
            recomputed_normal_rows.append(
                {
                    "case_id": cid,
                    "web_enabled": web_enabled,
                    "historical_summary_raw_gold_evidence_recall": historical_pair["raw_gold_evidence_recall"],
                    "historical_semantic_review_raw_gold_evidence_recall": historical_sem[
                        "raw_gold_evidence_recall"
                    ],
                    "recomputed_raw_gold_evidence_recall": raw_metrics["item_evidence_recall"],
                    "raw_gold_evidence_numerator": len(raw_metrics["covered_evidence_items"]),
                    "raw_gold_evidence_denominator": raw_metrics["gold_evidence_items"],
                    "raw_covered_evidence_items": raw_metrics["covered_evidence_items"],
                    "raw_uncovered_evidence_items": raw_metrics["uncovered_evidence_items"],
                    "recomputed_pre_fit_evidence_messages_recall": prefit_metrics["item_evidence_recall"],
                    "pre_fit_evidence_numerator": len(prefit_metrics["covered_evidence_items"]),
                    "pre_fit_evidence_denominator": prefit_metrics["gold_evidence_items"],
                    "post_fit_dispatched_evidence_recall": "NOT PROVEN",
                    "post_fit_dispatched_evidence_reason": (
                        "v2 recorded plan.messages before memory.fit rather than "
                        "SessionContext.dispatched_evidence_messages at BudgetedLLM._create_completion"
                    ),
                }
            )

    v1_elapsed = float(v1.get("elapsed_seconds", 0.0))
    v2_elapsed = float(v2.get("elapsed_seconds", 0.0))
    v1_replay_sum = round(sum(float(r.get("duration_ms") or 0.0) for r in v1["results"]) / 1000.0, 3)
    v2_replay_sum = round(sum(float(r.get("duration_ms") or 0.0) for r in v2["results"]) / 1000.0, 3)

    return {
        "schema_version": 1,
        "artifact_type": "corrigendum",
        "diagnostic_only": True,
        "final_acceptance_verdict": "FAIL",
        "rollout_recommendation": "NO-GO (keep SAG_SESSION_CONTEXT_MODE=off)",
        "source_artifacts_sha256": {
            corpus_path.relative_to(REPO_ROOT).as_posix(): _sha256(corpus_path),
            v1_path.relative_to(REPO_ROOT).as_posix(): _sha256(v1_path),
            v2_path.relative_to(REPO_ROOT).as_posix(): _sha256(v2_path),
            semantic_path.relative_to(REPO_ROOT).as_posix(): _sha256(semantic_path),
            summary_path.relative_to(REPO_ROOT).as_posix(): _sha256(summary_path),
        },
        "recomputation_script": {
            "path": Path(__file__).resolve().relative_to(REPO_ROOT).as_posix(),
            "sha256": _sha256(Path(__file__).resolve()),
            "scoring_helpers": [
                "scripts/eval_session_context.py::score_selected_evidence",
                "scripts/eval_session_context.py::score_fitted_evidence_messages",
            ],
        },
        "discrepancy_root_cause": {
            "affected_fields": [
                "f2-f3-independent-validation.semantic-review.jsonl::raw_gold_evidence_recall",
                "f2-f3-independent-validation.summary.json::paired_results[*].raw_gold_evidence_recall",
                "f2-f3-independent-validation.summary.json::gate_matrix.normal_raw_gold_evidence_recall",
                "f2-f3-independent-validation.summary.json::gate_matrix.normal_fitted_gold_evidence_recall",
            ],
            "explanation": (
                "scripts/verify_session_runtime.py stored candidate raw_selected_evidence as dicts with keys "
                "{'message_id', 'kind', 'text'}. The one-off summary generator read x.get('quote', '') instead "
                "of x['text'] when computing per-row raw_gold_evidence_recall, producing 0.0 for all 20 normal "
                "runtime rows while hardcoding observed=1.0 in gate_matrix.normal_raw_gold_evidence_recall. "
                "Additionally, fitted_evidence_messages in v2 was captured from plan.messages before memory.fit "
                "rather than from SessionContext.dispatched_evidence_messages at BudgetedLLM._create_completion."
            ),
        },
        "metric_definitions": {
            "raw_gold_evidence_recall": {
                "formula": (
                    "count(gold_items where all required evidence entries match an item in "
                    "raw_selected_evidence with entry.message_id == expanded_messages[ev.at].id, "
                    "entry.kind == f'{expanded_messages[ev.at].role}_statement', and ev.quote in entry.text) / "
                    "count(gold_items with evidence)"
                ),
                "recomputed_row_count": len(recomputed_normal_rows),
                "recomputed_row_min": min(r["recomputed_raw_gold_evidence_recall"] for r in recomputed_normal_rows),
                "recomputed_row_mean": sum(
                    r["recomputed_raw_gold_evidence_recall"] for r in recomputed_normal_rows
                )
                / len(recomputed_normal_rows),
                "total_covered_gold_items": raw_covered_total,
                "total_gold_items": raw_gold_total,
                "status": "PROVEN_FROM_V2_RAW_SELECTED_EVIDENCE",
            },
            "pre_fit_gold_evidence_recall": {
                "formula": (
                    "count(gold_items where all required evidence entries match an item parsed from "
                    "pre-fit SESSION_EVIDENCE_JSON in plan.messages with entry.message_id == "
                    "expanded_messages[ev.at].id, entry.kind == f'{expanded_messages[ev.at].role}_statement', "
                    "and ev.quote in entry.text) / count(gold_items with evidence)"
                ),
                "recomputed_row_count": len(recomputed_normal_rows),
                "recomputed_row_min": min(
                    r["recomputed_pre_fit_evidence_messages_recall"] for r in recomputed_normal_rows
                ),
                "recomputed_row_mean": sum(
                    r["recomputed_pre_fit_evidence_messages_recall"] for r in recomputed_normal_rows
                )
                / len(recomputed_normal_rows),
                "total_covered_gold_items": prefit_covered_total,
                "total_gold_items": prefit_gold_total,
                "status": "PROVEN_PRE_FIT_ONLY",
            },
            "fitted_gold_evidence_recall_post_memory_fit": {
                "formula": (
                    "count(gold_items where all required evidence entries match an item parsed from "
                    "SessionContext.dispatched_evidence_messages captured at BudgetedLLM._create_completion "
                    "after memory.fit) / count(gold_items with evidence)"
                ),
                "observed": None,
                "status": "NOT PROVEN",
                "reason": (
                    "Historical v2 artifact captured plan.messages prior to agent_service.generate_stream -> "
                    "memory.fit -> SessionContext.note_dispatch. Post-fit prompt capture is now implemented in "
                    "SessionContext.note_dispatch and verify_session_runtime.py for future runs."
                ),
            },
        },
        "experiment_governance_audit": {
            "v1_elapsed_seconds": v1_elapsed,
            "v2_elapsed_seconds": v2_elapsed,
            "combined_v1_v2_elapsed_seconds": round(v1_elapsed + v2_elapsed, 2),
            "v1_replay_duration_seconds": v1_replay_sum,
            "v2_replay_duration_seconds": v2_replay_sum,
            "combined_replay_duration_seconds": round(v1_replay_sum + v2_replay_sum, 3),
            "experiment_wall_time_cap_seconds": 2400.0,
            "exceeded_experiment_wall_time_cap": (v1_elapsed + v2_elapsed) > 2400.0,
            "source_hash_changed_between_v1_and_v2": (
                v1["source_sha256"].get("scripts/verify_session_runtime.py")
                != v2["source_sha256"].get("scripts/verify_session_runtime.py")
            ),
            "v1_verify_session_runtime_sha256": v1["source_sha256"].get("scripts/verify_session_runtime.py"),
            "v2_verify_session_runtime_sha256": v2["source_sha256"].get("scripts/verify_session_runtime.py"),
            "corpus_blindness_status": "EXPOSED_DIAGNOSTIC_CORPUS_NOT_REUSABLE_FOR_FINAL_ACCEPTANCE",
        },
        "normal_rows": recomputed_normal_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    corrigendum = build_corrigendum()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(corrigendum, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": len(corrigendum["normal_rows"])}), flush=True)


if __name__ == "__main__":
    main()
