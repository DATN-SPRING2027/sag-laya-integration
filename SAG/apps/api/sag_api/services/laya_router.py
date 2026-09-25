"""Laya Decision Engine integration for query routing and intent classification."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock
from typing import Any

from sag_api.core.config import settings
from sag_api.core.logging import get_logger

log = get_logger("laya_router")

_ROUTER: Any | None = None
_ROUTER_INIT_ERROR: str | None = None
_ROUTER_LOCK = Lock()
CHAT_HIGH_CONFIDENCE = 0.65


def _local_bundle_is_multilingual_only() -> bool:
    if not settings.laya_model_path:
        return False
    model_root = Path(settings.laya_model_path).expanduser()
    return not (model_root / "rl_agent_config.json").is_file() and (
        model_root / "multilingual" / "rl_agent_config.json"
    ).is_file()


def _model_specs() -> dict[str, object]:
    """Return model specs for either an explicit local checkpoint or HF cache."""
    if settings.laya_model_path:
        model_root = Path(settings.laya_model_path).expanduser()
        root_checkpoint = (model_root / "rl_agent_config.json").is_file()
        multilingual_checkpoint = (model_root / "multilingual" / "rl_agent_config.json").is_file()
        if root_checkpoint and multilingual_checkpoint:
            return {
                "english": (str(model_root), None),
                "multilingual": (str(model_root), "multilingual"),
            }
        local_spec = (str(model_root), None) if root_checkpoint else (str(model_root), "multilingual")
        return {
            "english": local_spec,
            "multilingual": local_spec,
        }
    return {
        "english": ("convaiinnovations/laya", None),
        "multilingual": ("convaiinnovations/laya", "multilingual"),
    }


def get_laya_router() -> Any | None:
    """Lazy-load the Laya Router singleton."""
    global _ROUTER, _ROUTER_INIT_ERROR
    if not settings.enable_laya:
        return None
    if _ROUTER is not None:
        return _ROUTER
    if _ROUTER_INIT_ERROR is not None:
        return None
    with _ROUTER_LOCK:
        if _ROUTER is not None:
            return _ROUTER
        if _ROUTER_INIT_ERROR is not None:
            return None
        try:
            from laya import Router

            log.info("Khởi tạo Laya Decision Router (model_path=%s)...", settings.laya_model_path or "huggingface")
            _ROUTER = Router(
                models=_model_specs(),
                device=settings.laya_device,
                max_loaded=1,
                default="multilingual",
                preload=False,
            )
            return _ROUTER
        except Exception as exc:  # noqa: BLE001
            _ROUTER_INIT_ERROR = str(exc)
            log.warning("Không thể khởi tạo Laya Router (%s). Chuyển về chế độ mặc định.", exc)
            return None


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


def route_query(query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Phân tích câu hỏi người dùng qua Laya trong ~30ms để định tuyến chiến lược.

    Trả về dict gồm:
      - is_chitchat: bool (nếu là chào hỏi, cảm ơn, hỏi thăm -> không cần search DB)
      - need_retrieval: bool (có cần tra cứu tri thức tài liệu không)
      - suggested_strategy: "vector" | "multi" (chiến lược tìm kiếm đề xuất)
      - domain: str (technical / business_hr / general)
      - confidence: float
      - latency_ms: float
      - model: str (multilingual / english)
    """
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

    router = get_laya_router()
    if router is None:
        # Fallback an toàn nếu Laya tắt hoặc chưa sẵn sàng.
        reason = "laya_disabled" if not settings.enable_laya else "laya_unavailable"
        return _fallback_route(query, reason)

    state = {"query": cleaned}
    if context:
        state.update({key: value for key, value in context.items() if key != "query"})

    questions = {
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
        "need_knowledge": {
            "type": "noul",
            "instructions": "Does answering `query` require searching documents or reference knowledge?",
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

    start_time = time.perf_counter()
    try:
        predict_kwargs = {"model": "multilingual"} if _local_bundle_is_multilingual_only() else {}
        try:
            pred = router.predict(state, questions, **predict_kwargs)
        except TypeError as error:
            # Keep lightweight test doubles and older Laya adapters compatible
            # when the optional model selector is not part of their signature.
            if predict_kwargs and "unexpected keyword argument" in str(error):
                pred = router.predict(state, questions)
            else:
                raise
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        answers = pred.get("answers", {})
        routing_meta = pred.get("routing", {})

        intent_res = answers.get("intent_type", {})
        intent_choice = str(intent_res.get("choice", "factual_lookup")).strip().lower()
        intent_conf = _confidence(intent_res.get("confidence", 0.5))

        domain_res = answers.get("domain_topic", {})
        domain_choice = domain_res.get("choice", "general")

        is_chat_intent = intent_choice in {"chit_chat", "chat", "greeting"}
        is_command = intent_choice in {"command", "tool_call", "action"}
        if is_chat_intent and intent_conf >= CHAT_HIGH_CONFIDENCE:
            coarse_intent = "CHAT"
            reason_codes = ["HIGH_CONFIDENCE_CHAT"]
        elif is_chat_intent:
            coarse_intent = "AMBIGUOUS"
            reason_codes = ["CHAT_CONFIDENCE_LOW"]
        elif is_command:
            coarse_intent = "COMMAND"
            reason_codes = ["COMMAND_INTENT"]
        elif intent_conf < CHAT_HIGH_CONFIDENCE:
            coarse_intent = "AMBIGUOUS"
            reason_codes = ["INTENT_CONFIDENCE_LOW"]
        else:
            coarse_intent = "KNOWLEDGE"
            reason_codes = ["KNOWLEDGE_INTENT"]

        # False negatives are worse than one extra retrieval: only high-
        # confidence CHAT is allowed to suppress the knowledge path.
        is_chitchat = coarse_intent == "CHAT"
        need_retrieval = coarse_intent != "CHAT"
        suggested_strategy = (
            "multi" if intent_choice == "complex_reasoning" or coarse_intent == "AMBIGUOUS" else "vector"
        )

        return {
            "query": query,
            "coarse_intent": coarse_intent,
            "is_chitchat": is_chitchat,
            "need_retrieval": need_retrieval,
            "suggested_strategy": suggested_strategy,
            "domain": domain_choice,
            "confidence": intent_conf,
            "latency_ms": elapsed_ms,
            "model": routing_meta.get("model", "laya"),
            "fallback_used": False,
            "fallback_reason": None,
            "reason_codes": reason_codes,
        }
    except Exception as exc:  # noqa: BLE001
        global _ROUTER, _ROUTER_INIT_ERROR
        # A failed model load is not query-specific. Retrying it on every
        # request can repeatedly trigger a large download or block the API.
        with _ROUTER_LOCK:
            _ROUTER = None
            _ROUTER_INIT_ERROR = str(exc)
        log.warning("Lỗi trong quá trình Laya predict (%s), chuyển về fallback.", exc)
        reason = (
            "laya_unavailable"
            if "model path not found" in str(exc).lower()
            else "laya_predict_failed"
        )
        return _fallback_route(query, reason)


def reset_laya_router_for_tests() -> None:
    """Reset the process singleton for isolated tests."""
    global _ROUTER, _ROUTER_INIT_ERROR
    with _ROUTER_LOCK:
        _ROUTER = None
        _ROUTER_INIT_ERROR = None
