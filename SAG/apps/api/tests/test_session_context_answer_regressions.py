"""Legacy grounding characterization, not a provider quality evaluation.

An answer-provider double returns the correct history fact. The real disposable
SQLite replay and terminal citation gate then determine what is published.
Typed, validated session evidence must be added separately before history can
authorize answers with web disabled; arbitrary history text cannot do so.
"""

import pytest

from sag_api.core.config import Settings
from sag_api.generation import llm as llm_module
from sag_api.services.eval.session_context_accounting import CallLedger, TrackedLLM
from scripts.eval_session_context import replay_case


@pytest.mark.asyncio
@pytest.mark.parametrize("web_enabled", [False, True])
async def test_legacy_terminal_grounding_distinguishes_history_from_document_evidence(monkeypatch, web_enabled):
    answer = "ORBIT-862"
    fact = f"Tôi chốt mã dashboard {answer}."
    query = "Viết lại mã dashboard đã chốt từ lịch sử cuộc chat."
    provider_inputs = []

    async def completion(**kwargs):
        assert kwargs["stream"], "the selected-history seam must not call compression"
        messages = kwargs["messages"]
        assert any(message.get("content") == fact for message in messages)
        assert messages[-1]["role"] == "user" and messages[-1]["content"] == query
        provider_inputs.append(messages)

        async def chunks():
            yield {"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 40, "completion_tokens": 5}}

        return chunks()

    monkeypatch.setattr(llm_module, "_litellm_completion", completion)
    case = {
        "id": "legacy-session-grounding", "split": "calibration", "language": "vi", "group": "normal",
        "scenario": "old_evidence", "message_count": 2, "filler_repeat": 1,
        "facts": [{"at": 0, "role": "user", "content": fact}], "query": query,
        "gold_items": [{"id": "dashboard", "aliases": [answer],
                        "evidence": [{"at": 0, "quote": answer}]}],
    }
    ledger = CallLedger(max_calls=2, max_seconds=30)
    client = TrackedLLM(Settings(_env_file=None, llm_api_key="offline-test", llm_model="offline-model"), ledger)
    result = await replay_case(case, client, ledger, web_enabled=web_enabled,
                               history_override=[{"role": "user", "content": fact}])

    assert provider_inputs and len(ledger.calls) == 1
    assert result["status"] == "run.completed"
    assert result["gold_verbatim_in_initial_context"] == 1
    if web_enabled:
        assert result["answer_status"] == "answered" and result["answer"] == answer
    else:
        # This reproduces the integration gap even when the provider answered
        # correctly. Do not "fix" legacy OFF by trusting unvalidated history.
        assert result["answer_status"] == "no_answer" and answer not in result["answer"]
