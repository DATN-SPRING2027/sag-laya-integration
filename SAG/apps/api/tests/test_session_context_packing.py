import json
from types import SimpleNamespace

from sag_api.services.session_context.packing import build_memory_messages, decode_memory_answer, fit_memory_messages
from sag_api.services.session_context.types import EvidenceSpan, MessageReference, ResolvedEvidence


def evidence():
    def item(mid, kind, text):
        return ResolvedEvidence(
            EvidenceSpan(MessageReference(mid, "a" * 64), 0, len(text)), kind, text, frozenset(), frozenset()
        )

    return (item("u", "user_statement", "I confirm BLUE."), item("a", "assistant_statement", "I propose RED."))


class Counter:
    def count(self, messages, tools=None):
        return sum(len(m["content"]) for m in messages) + len(str(tools or []))


def test_raw_history_is_inert_data_and_current_query_is_last_once():
    query = "Which choice did I confirm?"
    messages = build_memory_messages(SimpleNamespace(name="Test", persona={}), query, evidence())
    assert messages[-1] == {"role": "user", "content": query}
    assert sum(m["content"] == query for m in messages) == 1
    assert not any(m["role"] == "assistant" for m in messages)
    assert "assistant_statement" in messages[-2]["content"]
    assert "user_statement" in messages[-2]["content"]


def test_support_must_quote_exact_authorized_span_and_not_unknown_id():
    raw = json.dumps({"answer": "BLUE", "supports": [{"id": "S1", "quote": "confirm BLUE"}]})
    result = decode_memory_answer(raw, evidence())
    assert result[0] == "BLUE" and result[1]
    assert decode_memory_answer(raw.replace("S1", "S9"), evidence())[0] == "UNKNOWN"
    assert decode_memory_answer(raw.replace("confirm BLUE", "confirm RED"), evidence())[0] == "UNKNOWN"


def test_dependency_group_is_never_partially_retained_to_fit_tokens():
    items = evidence()
    messages = build_memory_messages(SimpleNamespace(name="Test", persona={}), "Question", items)
    base = Counter().count(build_memory_messages(SimpleNamespace(name="Test", persona={}), "Question", ()))
    fitted, kept = fit_memory_messages(messages, [], items, (("u", "a"),), counter=Counter(), input_limit=base + 20)
    assert kept == ()
    assert "confirm BLUE" not in str(fitted) and "propose RED" not in str(fitted)
    assert fitted[-1] == messages[-1]
