"""Tests for Laya Decision Router integration."""

import pytest
from httpx import ASGITransport, AsyncClient

from sag_api.main import create_app
from sag_api.services import laya_router
from sag_api.services.laya_router import route_query


@pytest.fixture(autouse=True)
def fake_laya_model(monkeypatch):
    """Keep unit/API tests offline; the real checkpoint is covered by a smoke test."""

    class FakeRouter:
        def predict(self, state, questions):
            query = state["query"]
            is_greeting = "chào" in query.lower()
            return {
                "answers": {
                    "intent_type": {
                        "choice": "chit_chat" if is_greeting else "factual_lookup",
                        "confidence": 0.99,
                    },
                    "need_knowledge": {"noul": 0.1 if is_greeting else 0.95},
                    "domain_topic": {"choice": "general"},
                },
                "routing": {"model": "fake"},
            }

    monkeypatch.setattr(laya_router, "_ROUTER", FakeRouter())
    monkeypatch.setattr(laya_router, "_ROUTER_INIT_ERROR", None)
    monkeypatch.setattr(laya_router.settings, "enable_laya", True)


def test_route_empty_query():
    result = route_query("   ")
    assert result["is_chitchat"] is True
    assert result["need_retrieval"] is False
    assert result["confidence"] == 1.0


def test_route_greeting_vietnamese():
    result = route_query("Xin chào bạn, hôm nay bạn thế nào?")
    assert "query" in result
    assert "confidence" in result
    assert "latency_ms" in result
    # Chào hỏi tiếng Việt phải được nhận diện là chitchat
    assert result["coarse_intent"] == "CHAT"
    assert result["is_chitchat"] is True
    assert result["need_retrieval"] is False


def test_route_business_document_query():
    result = route_query("Quy trình xin thanh toán chi phí công tác và hóa đơn tài chính của công ty")
    assert "query" in result
    assert result["coarse_intent"] == "KNOWLEDGE"
    assert result["is_chitchat"] is False
    assert result["need_retrieval"] is True


def test_low_confidence_chat_is_ambiguous_and_keeps_retrieval(monkeypatch):
    class UncertainRouter:
        def predict(self, state, questions):
            return {
                "answers": {
                    "intent_type": {"choice": "chit_chat", "confidence": 0.5},
                    "domain_topic": {"choice": "general"},
                },
                "routing": {"model": "fake"},
            }

    monkeypatch.setattr(laya_router, "_ROUTER", UncertainRouter())
    monkeypatch.setattr(laya_router, "_ROUTER_INIT_ERROR", None)

    result = route_query("Bạn khỏe không?")

    assert result["coarse_intent"] == "AMBIGUOUS"
    assert result["is_chitchat"] is False
    assert result["need_retrieval"] is True


def test_local_bundle_uses_multilingual_checkpoint_for_both_routes(monkeypatch, tmp_path):
    multilingual = tmp_path / "multilingual"
    multilingual.mkdir()
    (multilingual / "rl_agent_config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(laya_router.settings, "laya_model_path", str(tmp_path))

    specs = laya_router._model_specs()

    assert specs["english"] == (str(tmp_path), "multilingual")
    assert specs["multilingual"] == (str(tmp_path), "multilingual")


def test_local_multilingual_bundle_predicts_with_one_cached_model(monkeypatch, tmp_path):
    multilingual = tmp_path / "multilingual"
    multilingual.mkdir()
    (multilingual / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(laya_router.settings, "laya_model_path", str(tmp_path))

    calls = []

    class FakeRouter:
        def predict(self, state, questions, **kwargs):
            calls.append(kwargs.get("model"))
            return {
                "answers": {
                    "intent_type": {"choice": "factual_lookup", "confidence": 0.9},
                    "domain_topic": {"choice": "general"},
                },
                "routing": {"model": kwargs.get("model", "unexpected")},
            }

    monkeypatch.setattr(laya_router, "_ROUTER", FakeRouter())
    monkeypatch.setattr(laya_router, "_ROUTER_INIT_ERROR", None)
    result = route_query("What database does the project currently use?")

    assert result["model"] == "multilingual"
    assert calls == ["multilingual"]


def test_route_does_not_skip_grounding_for_factual_query(monkeypatch):
    class FakeRouter:
        def predict(self, state, questions):
            return {
                "answers": {
                    "intent_type": {"choice": "factual_lookup", "confidence": 0.99},
                    "need_knowledge": {"noul": 0.1},
                    "domain_topic": {"choice": "general"},
                },
                "routing": {"model": "fake"},
            }

    monkeypatch.setattr(laya_router, "_ROUTER", FakeRouter())
    monkeypatch.setattr(laya_router, "_ROUTER_INIT_ERROR", None)
    monkeypatch.setattr(laya_router.settings, "enable_laya", True)

    result = route_query("Bạn khỏe không?")

    assert result["is_chitchat"] is False
    assert result["need_retrieval"] is True
    assert result["model"] == "fake"


def test_failed_prediction_disables_repeated_model_loads(monkeypatch):
    calls = 0

    class BrokenRouter:
        def predict(self, state, questions):
            nonlocal calls
            calls += 1
            raise FileNotFoundError("checkpoint is incomplete")

    monkeypatch.setattr(laya_router, "_ROUTER", BrokenRouter())
    monkeypatch.setattr(laya_router, "_ROUTER_INIT_ERROR", None)

    first = route_query("Hướng dẫn cài đặt hệ thống")
    second = route_query("Hướng dẫn cấu hình hệ thống")

    assert first["model"] == "fallback"
    assert second["model"] == "fallback"
    assert calls == 1
    assert first["coarse_intent"] == "AMBIGUOUS"
    assert first["fallback_used"] is True
    assert first["fallback_reason"] == "laya_predict_failed"


def test_invalid_local_model_path_is_cached_as_safe_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(laya_router.settings, "laya_model_path", str(tmp_path / "missing"))
    laya_router.reset_laya_router_for_tests()

    first = route_query("Mã bí mật kiểm thử của Continuum AI là gì?")
    first_error = laya_router._ROUTER_INIT_ERROR
    second = route_query("Xin chào")
    second_error = laya_router._ROUTER_INIT_ERROR

    assert first["model"] == "fallback"
    assert first["need_retrieval"] is True
    assert first["coarse_intent"] == "AMBIGUOUS"
    assert first["fallback_used"] is True
    assert first["fallback_reason"] == "laya_unavailable"
    assert second["model"] == "fallback"
    assert second["need_retrieval"] is True
    assert first_error
    assert first_error == second_error


@pytest.mark.asyncio
async def test_laya_api_endpoint():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/laya/route",
            json={"query": "Tôi cần hướng dẫn cài đặt hệ thống"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "Tôi cần hướng dẫn cài đặt hệ thống"
        assert "is_chitchat" in data
        assert "need_retrieval" in data
        assert "suggested_strategy" in data
        assert "latency_ms" in data
