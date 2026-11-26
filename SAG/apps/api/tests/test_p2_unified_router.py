"""TDD regression tests for P2 Step 2: Unified 1-call structured output decision router."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sag_api.core.config import settings
from sag_api.core.token_usage import get_router_degraded_metrics, reset_runtime_token_usage


def _valid_router_payload(**overrides):
    base = {
        "coarse_intent": "KNOWLEDGE",
        "question_scope": "project",
        "memory_scope": "project_documents",
        "temporal_mode": "current_effective",
        "answer_shape": "fact_answer",
        "intent_flags": {
            "needs_retrieval": True,
            "needs_time": False,
            "needs_web": False,
            "needs_clarification": False,
            "is_pure_math_or_transform": False,
        },
        "domain": "technical",
        "confidence": 0.92,
        "reason_codes": ["PROJECT_POLICY_LOOKUP"],
        "order_mode": "relevance",
        "explicit_count": None,
        "target_unit": "passage",
        "is_catalog_query": False,
        "retrieval_mode_hint": "auto",
        "subqueries": [],
        "topic_slot_key": None,
        "hierarchy_bias": "none",
        "entities": [],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_unified_router_calls_openai_compatible_chat_completions_with_json_schema(monkeypatch) -> None:
    from sag_api.services.decision_router import clear_decision_route_cache, route_query_async

    clear_decision_route_cache()
    reset_runtime_token_usage()

    captured_requests: list[tuple[str, dict]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append((str(request.url), body))
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(_valid_router_payload())}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )

    monkeypatch.setattr(settings, "decision_provider", "openai_compatible")
    monkeypatch.setattr(settings, "decision_base_url", "https://router.example.com/v1")
    monkeypatch.setattr(settings, "decision_api_key", "router-test-key")
    monkeypatch.setattr(settings, "decision_model", "qwen-router-3b")
    monkeypatch.setattr(settings, "llm_structured_output_mode", "json_schema")

    decision = await route_query_async(
        "Chính sách bảo mật dịch vụ dự án là gì?",
        has_sources=True,
        http_transport=httpx.MockTransport(_handler),
    )

    assert len(captured_requests) == 1
    url, body = captured_requests[0]
    assert url == "https://router.example.com/v1/chat/completions"
    assert body["model"] == "qwen-router-3b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert decision.degraded is False
    assert decision.degraded_reason is None
    assert decision.coarse_intent == "KNOWLEDGE"
    assert decision.question_scope == "project"
    assert decision.memory_scope == "project_documents"
    assert decision.intent_flags.needs_retrieval is True


@pytest.mark.asyncio
async def test_unified_router_supports_llm_provider_protocol(monkeypatch) -> None:
    from sag_api.services.decision_router import clear_decision_route_cache, route_query_async

    clear_decision_route_cache()
    monkeypatch.setattr(settings, "decision_provider", "llm")
    monkeypatch.setattr(settings, "llm_structured_output_mode", "json_object")

    payload = _valid_router_payload(
        coarse_intent="CHAT",
        question_scope="general",
        memory_scope="none",
        intent_flags={
            "needs_retrieval": False,
            "needs_time": False,
            "needs_web": False,
            "needs_clarification": False,
            "is_pure_math_or_transform": False,
        },
        confidence=0.95,
    )
    complete = AsyncMock(return_value=json.dumps(payload))
    llm = SimpleNamespace(configured=True, complete=complete)

    decision = await route_query_async("Xin chào!", llm_client=llm, use_cache=False)
    complete.assert_awaited_once()
    assert complete.await_args.kwargs.get("response_format") == {"type": "json_object"}
    assert decision.coarse_intent == "CHAT"
    assert decision.question_scope == "general"
    assert decision.intent_flags.needs_retrieval is False
    assert decision.degraded is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup_kind", "expected_reason_prefix"),
    [
        ("disabled", "decision_router_disabled"),
        ("missing_key", "decision_api_key_missing"),
        ("http_502", "decision_http_502"),
        ("timeout", "decision_timeout"),
        ("invalid_json", "decision_invalid_json"),
        ("invalid_schema", "decision_invalid_schema"),
    ],
)
async def test_unified_router_fails_closed_to_degraded_without_regex_or_silent_project(
    monkeypatch,
    caplog,
    setup_kind: str,
    expected_reason_prefix: str,
) -> None:
    from sag_api.services.decision_router import clear_decision_route_cache, route_query_async

    clear_decision_route_cache()
    reset_runtime_token_usage()

    transport = None
    if setup_kind == "disabled":
        monkeypatch.setattr(settings, "decision_provider", "disabled")
    elif setup_kind == "missing_key":
        monkeypatch.setattr(settings, "decision_provider", "siliconflow")
        monkeypatch.setattr(settings, "decision_api_key", None)
        monkeypatch.setattr(settings, "siliconflow_api_key", None)
    else:
        monkeypatch.setattr(settings, "decision_provider", "siliconflow")
        monkeypatch.setattr(settings, "siliconflow_api_key", "sf-key")

        def _handler(request: httpx.Request) -> httpx.Response:
            if setup_kind == "http_502":
                return httpx.Response(502, json={"error": "bad gateway"})
            if setup_kind == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if setup_kind == "invalid_json":
                return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"coarse_intent": "BOGUS"}'}}]},
            )

        transport = httpx.MockTransport(_handler)

    # Query has both doc cues and session cues; regex fallback MUST NOT run.
    with caplog.at_level("WARNING"):
        decision = await route_query_async(
            "Tài liệu quy định dịch vụ dự án là gì?",
            http_transport=transport,
            use_cache=False,
        )

    assert decision.degraded is True
    assert decision.degraded_reason == expected_reason_prefix
    assert decision.memory_scope == "hybrid_both"
    assert decision.question_scope == "unknown"
    assert decision.coarse_intent == "AMBIGUOUS"
    assert decision.intent_flags.needs_retrieval is True
    assert decision.answer_shape == "fact_answer"
    assert decision.confidence == 0.0
    assert decision.order_mode == "relevance"
    assert decision.is_catalog_query is False
    assert get_router_degraded_metrics().get(expected_reason_prefix, 0) >= 1
    assert any("degraded" in rec.message.lower() for rec in caplog.records)


def test_unified_router_removes_legacy_regex_helpers() -> None:
    import sag_api.services.decision_router as dr

    for removed in (
        "infer_ecce_routing_signals",
        "_fallback_route",
        "_FULL_AUDIT_RE",
        "_SESSION_CUE_RE",
        "_DOC_CUE_RE",
        "_LINEAGE_CUE_RE",
        "_TEMPORAL_TARGET_RE",
        "_EFFECTIVE_STATE_RE",
        "_PAST_ANCHOR_RE",
    ):
        assert not hasattr(dr, removed), f"Legacy symbol {removed} must be removed"
