"""Regression tests for provider-boundary diagnostics in the synthetic harness."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sag_api.core.config import Settings
from sag_api.services.session_context import model
from sag_api.services.session_context.budget import BudgetLimits, BudgetState
from sag_api.services.session_context.model import BudgetedLLM


class Counter:
    def count(self, messages, tools=None):
        return 20


@pytest.mark.asyncio
async def test_dispatch_callback_internal_typeerror_prevents_provider_and_is_not_retried(monkeypatch):
    seen, provider_calls = [], []

    def failed_callback(*, messages=None):
        seen.append(messages)
        if messages is not None:
            raise TypeError("internal callback failure")

    async def provider(*args, **kwargs):
        provider_calls.append(1)
        return {}

    monkeypatch.setattr(model, "session_completion", provider)
    budget = BudgetState(BudgetLimits(12288, 4096, 1, 12288, 10000, time.monotonic() + 30))
    client = BudgetedLLM(Settings(_env_file=None, llm_api_key="synthetic"), budget=budget,
                         counter=Counter(), on_dispatch=failed_callback)
    with pytest.raises(TypeError, match="internal callback failure"):
        await client._create_completion([{"role": "user", "content": "synthetic"}])
    assert len(seen) == 1 and seen[0] is not None
    assert not provider_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("last_frame", [True, False])
async def test_candidate_reports_actual_evidence_and_recall_per_dispatch(monkeypatch, last_frame):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from scripts import verify_session_runtime as harness

    case = {
        "id": "offline-fixture", "group": "normal", "query": "What were the choices?",
        "message_count": 2, "gold_items": [
            {"id": "a", "evidence": [{"at": 0, "quote": "AAA"}]},
            {"id": "b", "evidence": [{"at": 1, "quote": "BBB"}]},
        ],
    }
    expanded = [
        {"id": "m1", "role": "user", "content": "AAA", "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "m2", "role": "user", "content": "BBB", "created_at": "2026-01-01T00:01:00+00:00"},
    ]
    monkeypatch.setattr(harness.baseline, "expand_case", lambda _: expanded)
    monkeypatch.setattr(harness.baseline, "score_answer", lambda *args: {})
    monkeypatch.setattr(harness, "configured_encoder", lambda: object())

    async def fake_index(*args, **kwargs):
        return None

    def evidence(message_id, text):
        return SimpleNamespace(span=SimpleNamespace(ref=SimpleNamespace(message_id=message_id)),
                               kind="user_statement", text=text)

    entries = [evidence("m1", "AAA"), evidence("m2", "BBB")]
    frames = ["SESSION_EVIDENCE_JSON\n" + json.dumps([
        {"message_id": item.span.ref.message_id, "kind": item.kind, "text": item.text}
    ]) for item in entries]
    context = SimpleNamespace(
        original=entries,
        dispatched_evidence={"id1": entries[0], "id2": entries[1]},
        dispatched_evidence_messages=frames[:2 if last_frame else 1],
        dispatched_calls=[
            {"call_index": 1, "messages_sha256": "a" * 64, "evidence_messages": [frames[0]]},
            {"call_index": 2, "messages_sha256": "b" * 64, "evidence_messages": [frames[1]] if last_frame else []},
        ],
    )

    async def fake_prepare(*args, **kwargs):
        return SimpleNamespace(session_context=context, messages=[])

    async def fake_stream(*args, **kwargs):
        yield SimpleNamespace(type="run.completed", data={"payload": {"output": "synthetic"}})

    monkeypatch.setattr(harness, "sync_index", fake_index)
    monkeypatch.setattr(harness.agent_domain, "prepare_ask", fake_prepare)
    monkeypatch.setattr(harness.agent_service, "generate_stream", fake_stream)
    harness.INDEX_TEMPLATES.clear()
    result = await harness.candidate(case, web_enabled=False)
    assert result["status"] == "run.completed"
    assert [item["message_id"] for item in result["actual_fitted_evidence"]] == ["m1", "m2"]
    per_call = result["fitted_evidence_recall_by_call"]
    assert [(entry["call_index"], entry["messages_sha256"]) for entry in per_call] == [(1, "a" * 64), (2, "b" * 64)]
    assert per_call[0]["item_evidence_recall"] == 0.5
    assert per_call[1]["item_evidence_recall"] == (0.5 if last_frame else None)
    assert result["fitted_gold_evidence_recall"] == (0.5 if last_frame else None)
    assert result["fitted_evidence_union_recall"] == (1.0 if last_frame else 0.5)


@pytest.mark.asyncio
async def test_candidate_records_error_before_plan_without_cleanup_masking(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    from scripts import verify_session_runtime as harness

    case = {"id": "failed-before-plan", "group": "normal", "query": "synthetic", "message_count": 1,
            "gold_items": []}
    monkeypatch.setattr(harness.baseline, "expand_case", lambda _: [])
    monkeypatch.setattr(harness, "configured_encoder", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    harness.INDEX_TEMPLATES.clear()
    result = await harness.candidate(case, web_enabled=False)
    assert result["status"] == "error" and result["error_type"] == "OSError"
