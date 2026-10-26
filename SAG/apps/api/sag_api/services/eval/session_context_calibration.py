"""Pure calibration measurements. Gold is consumed only after context selection."""

from __future__ import annotations

import json
from pathlib import Path

from sag_api.services.session_context.selection import SelectionCaps


def calibration_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [case for case in cases if case["split"] == "calibration"]
    if not selected or len({case["id"] for case in selected}) != len(selected):
        raise ValueError("invalid calibration split")
    return selected


def profiles() -> dict[str, SelectionCaps]:
    return {
        "compact": SelectionCaps(2, 1, 1, 8, 8000, 2, 16, 0.6, 0.6),
        "balanced": SelectionCaps(4, 2, 1, 16, 16000, 4, 20, 0.6, 0.6),
        "wide": SelectionCaps(6, 3, 2, 24, 24000, 6, 24, 0.6, 0.6),
    }


def evidence_metrics(case: dict, messages: list[dict], selected_ids: set[str]) -> dict:
    items = [item for item in case["gold_items"] if item["evidence"]]
    covered = [item["id"] for item in items if all(
        messages[evidence["at"]]["id"] in selected_ids
        and evidence["quote"] in messages[evidence["at"]]["content"] for evidence in item["evidence"])]
    return {"item_evidence_recall": len(covered) / len(items) if items else None,
            "gold_evidence_items": len(items), "covered_evidence_items": covered}


def choose_profile(rows: list[dict], *, expected_normal_ids: set[str] | None = None) -> str | None:
    for name in profiles():
        normal = [row for row in rows if row["profile"] == name and row["group"] == "normal"]
        if expected_normal_ids is not None and {row["case_id"] for row in normal} != expected_normal_ids:
            continue
        if normal and all(row["item_evidence_recall"] in (None, 1.0) for row in normal):
            return name
    return None


def paired_tokens(baseline: list[dict], candidate: list[dict]) -> dict:
    before = {row["case_id"]: row for row in baseline}
    after = {row["case_id"]: row for row in candidate}
    if len(before) != len(baseline) or len(after) != len(candidate):
        raise ValueError("duplicate paired case")
    if not before or before.keys() != after.keys():
        return {"reduction": None, "reason": "unpaired_cases"}
    rows = baseline + candidate
    if not all(row.get("status") == "run.completed" for row in rows):
        return {"reduction": None, "reason": "incomplete_replays"}
    if not all(row["accounting"]["accounting_complete"] and all(
        type(row["accounting"][key]) is int and row["accounting"][key] >= 0
        for key in ("input_tokens", "output_tokens")) for row in rows):
        return {"reduction": None, "reason": "unknown_usage"}
    totals = [sum(row["accounting"]["input_tokens"] + row["accounting"]["output_tokens"] for row in group)
              for group in (baseline, candidate)]
    return {"baseline_tokens": totals[0], "candidate_tokens": totals[1],
            "reduction": 1 - totals[1] / totals[0] if totals[0] else None, "pairs": len(before)}


def validate_resume(previous: dict, current: dict) -> None:
    fields = ("corpus_sha256", "config", "locked_profile", "profiles", "hierarchy", "llm_extra_body_sha256",
              "dependency_versions", "case_ids")
    if any(previous.get(key) != current.get(key) or key not in previous or key not in current for key in fields):
        raise ValueError("provider resume configuration mismatch")
    for name in ("graph.py", "selection.py", "hierarchy.py", "reader.py", "scope.py", "budget.py", "types.py",
                 "eval_session_context.py", "agent_domain.py", "agent_service.py", "llm.py", "prompt.py"):
        if (not previous.get("source_sha256", {}).get(name)
                or previous["source_sha256"][name] != current.get("source_sha256", {}).get(name)):
            raise ValueError("provider resume source mismatch")
