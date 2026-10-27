"""Read-only audit of committed synthetic evidence. Never invokes runtime/provider."""

import hashlib
import json
from collections import Counter
from pathlib import Path

root = next(p for p in Path(__file__).resolve().parents if (p / "SAG/apps/api").is_dir())
api = root / "SAG/apps/api"
evidence = root / "SAG/docs/Thang_Task/session-context-zero-mem/evidence"


def sha(data):
    return hashlib.sha256(data).hexdigest()


cases = {
    x["id"]: x
    for x in map(json.loads, (api / "tests/fixtures/session_context.jsonl").read_text(encoding="utf-8").splitlines())
}
reports = {}
for split in ("calibration", "holdout"):
    path = evidence / f"t9-{split}-runtime-v4.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    reports[split] = report
    review = [
        json.loads(x)
        for x in (evidence / f"runtime-{split}.semantic-review.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_key = {(r["case_id"], r["web_enabled"]): r for r in review}
    rows = report["results"]
    print(
        split,
        json.dumps(
            {
                "manifest_entries": len(report["source_sha256"]),
                "source_mismatches": [
                    name for name, h in report["source_sha256"].items() if sha((api / name).read_bytes()) != h
                ],
                "corpus_hash_matches": sha((api / "tests/fixtures/session_context.jsonl").read_bytes())
                == report["corpus_sha256"],
                "review_report_hashes_match": all(r["raw_report_sha256"] == sha(path.read_bytes()) for r in review),
                "review_answer_hashes_match": all(
                    sha(r.get("answer", "").encode()) == by_key[(r["case_id"], r["web_enabled"])]["answer_sha256"]
                    for r in rows
                    if r["variant"] == "runtime" and r["status"] == "run.completed"
                ),
                "results": len(rows),
                "experiment_calls": len(report["experiment_accounting"]["calls"]),
                "complete_usage": report["experiment_accounting"]["accounting_complete"],
                "known_input_tokens": report["experiment_accounting"]["known_input_tokens"],
                "known_output_tokens": report["experiment_accounting"]["known_output_tokens"],
                "correct_by_group": dict(Counter(r["group"] for r in review if r["semantically_correct"])),
                "false_absence": sum(r["false_absence_claim"] for r in review),
                "execution_failures": [
                    {"case": r["case_id"], "web": r["web_enabled"], "error": r.get("error_type")}
                    for r in rows
                    if r["variant"] == "runtime" and r["status"] != "run.completed"
                ],
            }
        ),
    )
    if split == "holdout":
        for web in (False, True):
            long = [
                r
                for r in rows
                if r["group"] == "normal"
                and r["web_enabled"] == web
                and (cases[r["case_id"]]["message_count"] > 200 or cases[r["case_id"]]["filler_repeat"] > 1)
            ]
            totals = {
                v: sum(
                    r["accounting"]["input_tokens"] + r["accounting"]["output_tokens"]
                    for r in long
                    if r["variant"] == v
                )
                for v in ("legacy", "runtime")
            }
            print("long_normal", web, totals, "reduction", 1 - totals["runtime"] / totals["legacy"])
        full = {
            v: {
                "calls": sum(
                    r["accounting"]["logical_calls"] for r in rows if r["group"] == "full_session" and r["variant"] == v
                ),
                "tokens": sum(
                    r["accounting"]["input_tokens"] + r["accounting"]["output_tokens"]
                    for r in rows
                    if r["group"] == "full_session" and r["variant"] == v
                ),
            }
            for v in ("legacy", "runtime")
        }
        print("full_holdout", json.dumps(full))
        cursor = 0
        for row in rows:
            calls = report["experiment_accounting"]["calls"][cursor : cursor + row["accounting"]["logical_calls"]]
            cursor += row["accounting"]["logical_calls"]
            if (
                row["variant"] == "runtime"
                and row["case_id"] in ("holdout-compression-early", "holdout-compression-middle")
                and row["web_enabled"]
            ):
                case = cases[row["case_id"]]
                gold = case["gold_items"][0]["evidence"][0]
                mid = sha(f"{case['id']}:{gold['at']}".encode())[:32]
                raw = next(f["content"] for f in case["facts"] if f["at"] == gold["at"])
                kept = [x for x in row["context"]["evidence"] if x["message_id"] == mid]
                print(
                    "normal_trace",
                    json.dumps(
                        {
                            "case": row["case_id"],
                            "kept_gold": kept,
                            "gold_hash": sha(raw.encode()),
                            "expected_span": [0, len(raw)],
                            "raw_provider": [x.get("synthetic_output") for x in calls],
                            "visible": row["answer"],
                        }
                    ),
                )
            if row["variant"] == "runtime" and row["group"] == "full_session":
                outputs = [call.get("synthetic_output", "") for call in calls]
                print(
                    "full_trace",
                    json.dumps(
                        {
                            "case": row["case_id"],
                            "web": row["web_enabled"],
                            "context": {k: row["context"][k] for k in ("state", "eligible", "visited", "reason")},
                            "outputs": outputs,
                            "visible": row["answer"],
                        }
                    ),
                )
for key in (
    "source_sha256",
    "corpus_sha256",
    "caps",
    "config",
    "models",
    "dependency_versions",
    "provider_extra_body_sha256",
):
    print("calibration_equals_holdout", key, reports["calibration"][key] == reports["holdout"][key])
