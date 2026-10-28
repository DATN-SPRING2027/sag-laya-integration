"""Decision routing through SiliconFlow's System One API."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from sag_api.core.config import settings
from sag_api.core.logging import get_logger

log = get_logger("decision_router")
CHAT_HIGH_CONFIDENCE = 0.65
VALID_MEMORY_SCOPES = frozenset(
    {"none", "session_history", "project_documents", "hybrid_both", "full_session_audit"}
)
VALID_TEMPORAL_MODES = frozenset(
    {"current_effective", "change_lineage", "past_point", "atemporal"}
)
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
    "memory_scope": {
        "type": "choice",
        "instructions": "Which evidence source does `query` primarily require?",
        "criteria": {
            "none": "Casual greetings, pleasantries, or standalone requests needing no prior chat or documents",
            "session_history": (
                "Recalling earlier chat turns, user preferences, assigned owners, open tasks, bugs, or team decisions"
            ),
            "project_documents": (
                "Looking up uploaded documents, policies, specifications, contracts, or canonical knowledge base"
            ),
            "hybrid_both": (
                "Comparing, cross-checking, or combining chat decisions/discussions with uploaded project documents"
            ),
            "full_session_audit": (
                "Exhaustive summary or complete list of all decisions, tasks, or topics across the entire conversation"
            ),
        },
    },
    "temporal_mode": {
        "type": "choice",
        "instructions": "How does `query` treat time and decision updates?",
        "criteria": {
            "current_effective": "Needs the latest active/confirmed decision, current owner, or current working state",
            "change_lineage": "Asks why, how, or when a decision/plan changed from an earlier option to a newer one",
            "past_point": "Asks specifically when something happened or what was said at a past meeting/date",
            "atemporal": "General factual, conceptual, or procedural question without temporal state transitions",
        },
    },
}

_FULL_AUDIT_RE = re.compile(
    r"(?i)(?:(?:tổng\s+hợp|liệt\s+kê|điểm\s+lại|tóm\s+tắt|rút\s+ra)\s+(?:tất\s+cả|toàn\s+bộ|mọi)"
    r".*(?:từ\s+đầu|cuộc\s+trò\s+chuyện|buổi\s+chat|phiên|quyết\s+định|công\s+việc)"
    r"|(?:summarize|list|compile)\s+(?:all|every).*(?:from\s+the\s+start|entire\s+session|entire\s+conversation|decisions))"
)
_SESSION_CUE_RE = re.compile(
    r"(?i)(?:\b(?:chúng\s+ta|chúng\s+mình|tụi\s+mình|mình\s+vừa|mình\s+đã|vừa\s+bàn|lúc\s+nãy|lúc\s+đầu|ban\s+đầu|"
    r"buổi\s+họp|phiên\s+trước|cuộc\s+chat|chốt|thống\s+nhất|phương\s+án\s+(?:nào|đó|trên|mới|cũ)|"
    r"tiếp\s+tục\s+công\s+việc|ai\s+(?:nhận|phụ\s+trách|làm|đảm\s+nhiệm)|anh\s+\w+|chị\s+\w+|"
    r"we\s+(?:agreed|discussed|chose|decided)|earlier\s+in\s+chat|our\s+decision|"
    r"continue\s+(?:yesterday|where\s+we\s+left))\b)"
)
_DOC_CUE_RE = re.compile(
    r"(?i)(?:\b(?:tài\s+liệu|quy\s+định|quy\s+chuẩn|chính\s+sách|hợp\s+đồng|báo\s+cáo|biên\s+bản|"
    r"điều\s+\d+|khoản\s+\d+|mục\s+\d+|adr[-_ ]?\d+|rfc[-_ ]?\d+|spec|specification|policy|document|contract)\b)"
)
_LINEAGE_CUE_RE = re.compile(
    r"(?i)(?:(?:vì\s+sao|tại\s+sao|lý\s+do\s+(?:gì|nào)).*(?:chuyển|đổi|bỏ|thay|quay\s+lại)"
    r"|(?:chuyển\s+từ|đổi\s+từ|thay\s+đổi\s+từ).*(?:sang|qua)"
    r"|(?:lịch\s+sử\s+thay\s+đổi|quá\s+trình\s+thay\s+đổi|why\s+did\s+we\s+(?:switch|change|drop|revert)|"
    r"switched\s+from\s+.+\s+to))"
)
_TEMPORAL_TARGET_RE = re.compile(
    r"(?i)(?:\b(?:khi\s+nào|vào\s+ngày\s+nào|ngày\s+nào|ngày\s+mấy|lúc\s+mấy\s+giờ|bao\s+lâu|trước\s+hay\s+sau|"
    r"thứ\s+tự\s+thời\s+gian|when\s+did|what\s+date|how\s+long|before\s+or\s+after)\b)"
)
_EFFECTIVE_STATE_RE = re.compile(
    r"(?i)(?:\b(?:chốt|quyết\s+định\s+(?:cuối|hiện\s+tại|hiện\s+hành|chọn)|cuối\s+cùng\s+(?:chọn|dùng)|"
    r"đang\s+dùng|hiện\s+tại|hiện\s+giờ|ai\s+(?:nhận|phụ\s+trách|đảm\s+nhiệm)|còn\s+lỗi\s+nào|"
    r"việc\s+gì\s+đang\s+dở|tiếp\s+tục\s+công\s+việc|which\s+\w+\s+did\s+we\s+(?:choose|pick|select)|"
    r"current\s+(?:decision|status|owner)|final\s+decision|who\s+(?:owns|is\s+responsible|took))\b)"
)
_PAST_ANCHOR_RE = re.compile(
    r"(?i)(?:\b(?:hôm\s+qua|tuần\s+trước|tháng\s+trước|buổi\s+họp\s+trước|đầu\s+phiên|đầu\s+buổi|"
    r"yesterday|last\s+week|last\s+meeting|earlier\s+session)\b)"
)


def infer_ecce_routing_signals(query: str, *, coarse_intent: str = "AMBIGUOUS") -> tuple[str, str]:
    """Deterministic Zero-Mem/ECCE signal inference separating temporal filters from targets."""
    cleaned = query.strip()
    if not cleaned or coarse_intent == "CHAT":
        return "none", "atemporal"

    if _FULL_AUDIT_RE.search(cleaned):
        memory_scope = "full_session_audit"
    else:
        has_session = bool(_SESSION_CUE_RE.search(cleaned))
        has_doc = bool(_DOC_CUE_RE.search(cleaned))
        if has_session and has_doc:
            memory_scope = "hybrid_both"
        elif has_session:
            memory_scope = "session_history"
        else:
            memory_scope = "project_documents"

    # Zero-Mem _requires_temporal_route principle:
    # A past time anchor ("tuần trước ai nhận task X?") is a filter on an entity/state lookup,
    # NOT a temporal target unless the question explicitly asks *when / how long / before-or-after*.
    if _LINEAGE_CUE_RE.search(cleaned):
        temporal_mode = "change_lineage"
    elif _TEMPORAL_TARGET_RE.search(cleaned):
        temporal_mode = "past_point"
    elif _EFFECTIVE_STATE_RE.search(cleaned):
        temporal_mode = "current_effective"
    elif _PAST_ANCHOR_RE.search(cleaned):
        temporal_mode = "past_point"
    else:
        temporal_mode = "atemporal"

    return memory_scope, temporal_mode


def _fallback_route(query: str, reason: str) -> dict[str, Any]:
    memory_scope, temporal_mode = infer_ecce_routing_signals(query, coarse_intent="AMBIGUOUS")
    return {
        "query": query,
        "coarse_intent": "AMBIGUOUS",
        "is_chitchat": False,
        "need_retrieval": True,
        "suggested_strategy": "multi",
        "domain": "general",
        "memory_scope": memory_scope,
        "temporal_mode": temporal_mode,
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

    inferred_scope, inferred_temporal = infer_ecce_routing_signals(query, coarse_intent=coarse_intent)
    scope_result = answers.get("memory_scope")
    scope_choice = (
        str(scope_result.get("choice", "")).strip().lower()
        if isinstance(scope_result, dict)
        else ""
    )
    if coarse_intent == "CHAT":
        memory_scope = "none"
    elif scope_choice in VALID_MEMORY_SCOPES and scope_choice != "none":
        memory_scope = scope_choice
    else:
        memory_scope = inferred_scope

    temporal_result = answers.get("temporal_mode")
    temporal_choice = (
        str(temporal_result.get("choice", "")).strip().lower()
        if isinstance(temporal_result, dict)
        else ""
    )
    if coarse_intent == "CHAT":
        temporal_mode = "atemporal"
    elif temporal_choice in VALID_TEMPORAL_MODES:
        temporal_conf = (
            _confidence(temporal_result.get("confidence", 0.5))
            if isinstance(temporal_result, dict)
            else 0.0
        )
        if temporal_conf < 0.70 and inferred_temporal in {"change_lineage", "current_effective"}:
            temporal_mode = inferred_temporal
        else:
            temporal_mode = temporal_choice
    else:
        temporal_mode = inferred_temporal

    return {
        "query": query,
        "coarse_intent": coarse_intent,
        "is_chitchat": coarse_intent == "CHAT",
        "need_retrieval": coarse_intent != "CHAT",
        "suggested_strategy": (
            "multi"
            if intent_choice == "complex_reasoning"
            or coarse_intent == "AMBIGUOUS"
            or memory_scope in {"hybrid_both", "full_session_audit"}
            or temporal_mode == "change_lineage"
            else "vector"
        ),
        "domain": domain_choice,
        "memory_scope": memory_scope,
        "temporal_mode": temporal_mode,
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
            "memory_scope": "none",
            "temporal_mode": "atemporal",
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
