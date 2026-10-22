"""Decision routing through SiliconFlow's System One API."""

from __future__ import annotations

import time
from typing import Any

import httpx

from sag_api.core.config import settings
from sag_api.core.logging import get_logger

log = get_logger("decision_router")
CHAT_HIGH_CONFIDENCE = 0.65
_DECISION_QUESTIONS = {
    "intent_type": {
        "type": "choice",
        "instructions": "What is the primary intent of this user message in `query`?",
        "criteria": {
            "chit_chat": (
                "Greetings, saying hello or goodbye, thank you, who are you, casual pleasantries, or small talk"
            ),
            "factual_lookup": (
                "Questions asking for information, company policy, procedures, facts, code, or documentation"
            ),
            "complex_reasoning": "Comparison, synthesis, multi-step explanation, or complex analytical query",
        },
    },
    "domain_topic": {
        "type": "choice",
        "instructions": "Which domain does `query` belong to?",
        "criteria": {
            "technical": "Programming, software, engineering, system architecture, bugs, APIs",
            "business_hr": "Human resources, company policy, finance, contracts, legal, operations, reimbursement",
            "general": "General conversation, casual talk, or common knowledge",
        },
    },
}


def _fallback_route(query: str, reason: str) -> dict[str, Any]:
    return {
        "query": query,
        "coarse_intent": "AMBIGUOUS",
        "is_chitchat": False,
        "need_retrieval": True,
        "suggested_strategy": "multi",
        "domain": "general",
        "confidence": 0.0,
        "latency_ms": 0.0,
        "model": "fallback",
        "fallback_used": True,
        "fallback_reason": reason,
        "reason_codes": [reason.upper()],
    }


def _confidence(value: Any) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _route_from_answers(
    query: str,
    answers: dict,
    model: str,
    latency_ms: float,
    provider: str,
) -> dict[str, Any]:
    intent_result = answers.get("intent_type")
    if not isinstance(intent_result, dict):
        return _fallback_route(query, f"{provider}_invalid_response")

    intent_choice = str(intent_result.get("choice", "factual_lookup")).strip().lower()
    intent_confidence = _confidence(intent_result.get("confidence", 0.5))
    domain_result = answers.get("domain_topic")
    domain_choice = str(domain_result.get("choice", "general")) if isinstance(domain_result, dict) else "general"
    if domain_choice not in {"technical", "business_hr", "general"}:
        domain_choice = "general"

    is_chat_intent = intent_choice in {"chit_chat", "chat", "greeting"}
    is_command = intent_choice in {"command", "tool_call", "action"}
    if is_chat_intent and intent_confidence >= CHAT_HIGH_CONFIDENCE:
        coarse_intent, reason_codes = "CHAT", ["HIGH_CONFIDENCE_CHAT"]
    elif is_chat_intent:
        coarse_intent, reason_codes = "AMBIGUOUS", ["CHAT_CONFIDENCE_LOW"]
    elif is_command:
        coarse_intent, reason_codes = "COMMAND", ["COMMAND_INTENT"]
    elif intent_confidence < CHAT_HIGH_CONFIDENCE:
        coarse_intent, reason_codes = "AMBIGUOUS", ["INTENT_CONFIDENCE_LOW"]
    else:
        coarse_intent, reason_codes = "KNOWLEDGE", ["KNOWLEDGE_INTENT"]

    return {
        "query": query,
        "coarse_intent": coarse_intent,
        "is_chitchat": coarse_intent == "CHAT",
        "need_retrieval": coarse_intent != "CHAT",
        "suggested_strategy": (
            "multi" if intent_choice == "complex_reasoning" or coarse_intent == "AMBIGUOUS" else "vector"
        ),
        "domain": domain_choice,
        "confidence": intent_confidence,
        "latency_ms": latency_ms,
        "model": model,
        "fallback_used": False,
        "fallback_reason": None,
        "reason_codes": reason_codes,
    }


def route_query(query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify with the selected optional provider; failures keep retrieval enabled."""
    cleaned = query.strip()
    if not cleaned:
        return {
            "query": query,
            "coarse_intent": "CHAT",
            "is_chitchat": True,
            "need_retrieval": False,
            "suggested_strategy": "vector",
            "domain": "general",
            "confidence": 1.0,
            "latency_ms": 0.0,
            "model": "rule",
            "fallback_used": False,
            "fallback_reason": None,
            "reason_codes": ["EMPTY_QUERY"],
        }

    if settings.decision_provider == "disabled":
        return _fallback_route(query, "decision_router_disabled")

    state: dict[str, Any] = {"query": cleaned}
    if context:
        state.update({key: value for key, value in context.items() if key != "query"})

    if not settings.siliconflow_api_key:
        return _fallback_route(query, "siliconflow_api_key_missing")

    start_time = time.perf_counter()
    try:
        response = httpx.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/systemone",
            headers={
                "Authorization": f"Bearer {settings.siliconflow_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": settings.decision_model, "state": state, "questions": _DECISION_QUESTIONS},
            timeout=httpx.Timeout(5.0, connect=3.0),
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            return _fallback_route(query, "siliconflow_invalid_response")
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        model = str(payload.get("model") or settings.decision_model)
        return _route_from_answers(query, answers, model, latency_ms, "siliconflow")
    except httpx.HTTPStatusError as exc:
        # Status codes help diagnose provider configuration without exposing
        # the response body or request credentials in application logs.
        log.warning("SiliconFlow Decision request failed (HTTP %d)", exc.response.status_code)
        return _fallback_route(query, "siliconflow_request_failed")
    except Exception as exc:  # noqa: BLE001 - provider failure must not block retrieval
        log.warning("SiliconFlow Decision request failed (%s)", type(exc).__name__)
        return _fallback_route(query, "siliconflow_request_failed")
