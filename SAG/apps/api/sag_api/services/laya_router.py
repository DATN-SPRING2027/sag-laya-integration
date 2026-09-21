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
            "is_chitchat": True,
            "need_retrieval": False,
            "suggested_strategy": "vector",
            "domain": "general",
            "confidence": 1.0,
            "latency_ms": 0.0,
            "model": "rule",
        }

    router = get_laya_router()
    if router is None:
        # Fallback an toàn nếu Laya tắt hoặc chưa sẵn sàng
        return {
            "query": query,
            "is_chitchat": False,
            "need_retrieval": True,
            "suggested_strategy": "multi",
            "domain": "general",
            "confidence": 0.5,
            "latency_ms": 0.0,
            "model": "fallback",
        }

    state = {"query": cleaned}
    if context:
        state.update(context)

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
        pred = router.predict(state, questions, **predict_kwargs)
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        answers = pred.get("answers", {})
        routing_meta = pred.get("routing", {})

        intent_res = answers.get("intent_type", {})
        intent_choice = intent_res.get("choice", "factual_lookup")
        intent_conf = intent_res.get("confidence", 0.5)

        domain_res = answers.get("domain_topic", {})
        domain_choice = domain_res.get("choice", "general")

        is_chitchat = intent_choice == "chit_chat" and intent_conf >= 0.5
        # Never let an uncertain auxiliary noul score suppress grounding for
        # factual/complex questions. False negatives are worse than one extra
        # retrieval; only high-confidence chitchat skips SAG knowledge search.
        need_retrieval = not is_chitchat
        suggested_strategy = "multi" if intent_choice == "complex_reasoning" else "vector"

        return {
            "query": query,
            "is_chitchat": is_chitchat,
            "need_retrieval": need_retrieval,
            "suggested_strategy": suggested_strategy,
            "domain": domain_choice,
            "confidence": round(intent_conf, 4),
            "latency_ms": elapsed_ms,
            "model": routing_meta.get("model", "laya"),
        }
    except Exception as exc:  # noqa: BLE001
        global _ROUTER, _ROUTER_INIT_ERROR
        # A failed model load is not query-specific. Retrying it on every
        # request can repeatedly trigger a large download or block the API.
        with _ROUTER_LOCK:
            _ROUTER = None
            _ROUTER_INIT_ERROR = str(exc)
        log.warning("Lỗi trong quá trình Laya predict (%s), chuyển về fallback.", exc)
        return {
            "query": query,
            "is_chitchat": False,
            "need_retrieval": True,
            "suggested_strategy": "vector",
            "domain": "general",
            "confidence": 0.5,
            "latency_ms": 0.0,
            "model": "fallback",
        }


def reset_laya_router_for_tests() -> None:
    """Reset the process singleton for isolated tests."""
    global _ROUTER, _ROUTER_INIT_ERROR
    with _ROUTER_LOCK:
        _ROUTER = None
        _ROUTER_INIT_ERROR = None
