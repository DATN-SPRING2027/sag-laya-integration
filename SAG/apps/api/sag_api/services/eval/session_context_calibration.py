"""Pure calibration measurements. Gold is consumed only after context selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from sag_api.core.config import settings
from sag_api.core.litellm_policy import apply_litellm_completion_policy
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
    covered = [
        item["id"]
        for item in items
        if all(
            messages[evidence["at"]]["id"] in selected_ids
            and evidence["quote"] in messages[evidence["at"]]["content"]
            for evidence in item["evidence"]
        )
    ]
    return {
        "item_evidence_recall": len(covered) / len(items) if items else None,
        "gold_evidence_items": len(items),
        "covered_evidence_items": covered,
    }


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
    if not all(
        row["accounting"]["accounting_complete"]
        and all(
            type(row["accounting"][key]) is int and row["accounting"][key] >= 0
            for key in ("input_tokens", "output_tokens")
        )
        for row in rows
    ):
        return {"reduction": None, "reason": "unknown_usage"}
    totals = [
        sum(row["accounting"]["input_tokens"] + row["accounting"]["output_tokens"] for row in group)
        for group in (baseline, candidate)
    ]
    return {
        "baseline_tokens": totals[0],
        "candidate_tokens": totals[1],
        "reduction": 1 - totals[1] / totals[0] if totals[0] else None,
        "pairs": len(before),
    }


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


def provider_route_sha256(cfg=None) -> str:
    active = settings if cfg is None else cfg
    if not active.llm_base_url or not active.llm_base_url.strip():
        raise ValueError(
            "calibration provider replay requires an explicit SAG_LLM_BASE_URL; "
            "SDK environment/default routes are not frozen"
        )
    legacy_probe = apply_litellm_completion_policy(
        active,
        {
            "model": active.routed_llm_model,
            "api_base": active.llm_base_url,
            "temperature": active.effective_llm_temperature,
            "max_tokens": active.llm_max_tokens,
        },
    )
    legacy_probe.pop("api_base", None)
    legacy_probe.pop("base_url", None)
    session_probe = apply_litellm_completion_policy(
        active,
        {
            "model": active.llm_model,
            "api_base": active.llm_base_url,
            "temperature": active.effective_llm_temperature,
            "max_tokens": min(active.llm_max_tokens, 4096),
            "response_format": {"type": "json_object"},
        },
    )
    session_probe.pop("api_base", None)
    session_probe.pop("base_url", None)
    descriptor = {
        "provider": active.llm_provider,
        "routed_model": active.routed_llm_model,
        "session_model": active.llm_model,
        "effective_temperature": active.effective_llm_temperature,
        "structured_output_mode": active.llm_structured_output_mode,
        "endpoint": _sanitize_endpoint_url(active.llm_base_url),
        "legacy_request_policy": legacy_probe,
        "session_request_policy": session_probe,
    }
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()


def _is_nonneg_int(value: object) -> bool:
    return type(value) is int and value >= 0


def validate_resume(previous: dict, current: dict) -> None:
    if previous.get("in_flight_replay") is not None:
        raise ValueError("provider resume cannot proceed: in-flight replay requires reconciliation")
    fields = (
        "corpus_sha256",
        "config",
        "provider_route_sha256",
        "locked_profile",
        "profiles",
        "hierarchy",
        "llm_extra_body_sha256",
        "dependency_versions",
        "case_ids",
    )
    if any(previous.get(key) != current.get(key) or key not in previous or key not in current for key in fields):
        raise ValueError("provider resume configuration mismatch")
    for route_doc in (previous, current):
        route = route_doc.get("provider_route_sha256")
        if not isinstance(route, str) or not route.strip():
            raise ValueError("provider resume configuration mismatch")
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
    ):
        if (
            not previous.get("source_sha256", {}).get(name)
            or previous["source_sha256"][name] != current.get("source_sha256", {}).get(name)
        ):
            raise ValueError("provider resume source mismatch")
    if "accounting" in previous or "results" in previous:
        accounting = previous.get("accounting")
        if not isinstance(accounting, dict) or not isinstance(accounting.get("calls"), list):
            raise ValueError("provider resume accounting missing or malformed")
        calls = accounting["calls"]
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("provider resume call record must be an object")
            if call.get("status") not in {"completed", "failed", "cancelled"} or call.get("in_flight") is True:
                raise ValueError("provider resume cannot proceed: in-flight or started call requires reconciliation")
            if not _is_nonneg_int(call.get("estimated_input_tokens")) or not _is_nonneg_int(
                call.get("configured_retries")
            ):
                raise ValueError("provider resume call reservation metadata missing or invalid")
            if "max_output_tokens" in call and not _is_nonneg_int(call["max_output_tokens"]):
                raise ValueError("provider resume call max_output_tokens invalid")
        expected_reserved = sum(
            (call["estimated_input_tokens"] + call.get("max_output_tokens", settings.llm_max_tokens))
            * (1 + call["configured_retries"])
            for call in calls
        )
        if "reserved_tokens" in previous and (
            not _is_nonneg_int(previous["reserved_tokens"]) or previous["reserved_tokens"] != expected_reserved
        ):
            raise ValueError("provider resume reserved_tokens mismatch")
        if "results" in previous:
            if not isinstance(previous["results"], list) or not isinstance(
                previous.get("excluded_attempts", []), list
            ):
                raise ValueError("provider resume results list malformed")
            all_rows = previous["results"] + previous.get("excluded_attempts", [])
            rows_with_acc = [row["accounting"] for row in all_rows if isinstance(row, dict) and "accounting" in row]
            for row_acc in rows_with_acc:
                if not isinstance(row_acc, dict) or not isinstance(row_acc.get("calls"), list):
                    raise ValueError("provider resume row accounting malformed")
                for row_call in row_acc["calls"]:
                    if (
                        not isinstance(row_call, dict)
                        or row_call.get("status") not in {"completed", "failed", "cancelled"}
                        or row_call.get("in_flight") is True
                    ):
                        raise ValueError("provider resume row has unsettled call")
            if rows_with_acc and sum(len(r["calls"]) for r in rows_with_acc) != len(calls):
                raise ValueError("provider resume accounting call count does not match recorded rows")
