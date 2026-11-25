"""Regression tests for Milestone P5 Area A: sag_language, flags, config, security & pricing."""

from __future__ import annotations

import unicodedata
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sag_api.core.config import Settings
from sag_api.generation.prompt import build_agent_messages, build_messages
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.incremental_processor import (
    IncrementalDocumentProcessor,
    detect_document_language,
)
from sag_api.schemas.system import ModelConfigUpdate
from sag_api.services import evidence_service, settings_service


def test_sag_language_settings_schema_and_overrides():
    """S2-06/S3-09/S5-15: sag_language supports en/zh/vi without forcing 'en'."""
    for lang in ("en", "zh", "vi"):
        assert Settings(sag_language=f" {lang.upper()} ").sag_language == lang
        assert ModelConfigUpdate(sag_language=f" {lang.upper()} ").sag_language == lang

    with pytest.raises(ValidationError):
        Settings(sag_language="fr")

    with pytest.raises(ValidationError):
        ModelConfigUpdate(sag_language="fr")

    assert ModelConfigUpdate(sag_language=None).sag_language is None

    normalized = settings_service._normalize_overrides({"sag_language": " ZH "})
    assert normalized["sag_language"] == "zh"
    normalized_vi = settings_service._normalize_overrides({"sag_language": "vi"})
    assert normalized_vi["sag_language"] == "vi"
    dropped = settings_service._normalize_overrides({"sag_language": "invalid"})
    assert "sag_language" not in dropped


def test_detect_document_language_nfd_and_scripts():
    """detect_document_language handles NFD Vietnamese, CJK Han, English, and configured overrides."""
    vi_nfd = unicodedata.normalize("NFD", "Tài liệu hướng dẫn triển khai hệ thống.")
    zh_text = "系统部署与检索增强生成指南。"
    en_text = "System deployment and retrieval augmented generation guide."

    assert detect_document_language(vi_nfd) == "vi"
    assert detect_document_language(vi_nfd, configured="") == "vi"
    assert detect_document_language(vi_nfd, configured="auto") == "vi"
    assert detect_document_language(zh_text) == "zh"
    assert detect_document_language(en_text) == "en"
    assert detect_document_language(en_text, configured="vi") == "vi"
    assert detect_document_language(vi_nfd, configured="en") == "en"


def test_prompts_and_evidence_respect_sag_language(monkeypatch):
    """generation/prompt.py and evidence_service._search_prompt_messages respect en/zh/vi."""
    en_msgs = build_messages("Hello?", [], language="en")
    zh_msgs = build_messages("你好？", [], language="zh")
    vi_msgs = build_messages("Xin chào?", [], language="vi")
    assert "never respond in chinese" in en_msgs[0]["content"].lower()
    assert "never respond in chinese" not in zh_msgs[0]["content"].lower()
    assert "respond in chinese" in zh_msgs[0]["content"].lower()
    assert "vietnamese" in vi_msgs[0]["content"].lower()

    agent_zh = build_agent_messages("SAG", {}, "你好？", language="zh")
    assert "never respond in chinese" not in agent_zh[0]["content"].lower()
    assert "respond in chinese" in agent_zh[0]["content"].lower()

    monkeypatch.setattr(evidence_service.settings, "sag_language", "zh")
    ev_zh = evidence_service._search_prompt_messages("问题？", [])
    assert "never chinese" not in ev_zh[0]["content"].lower()
    assert "chinese" in ev_zh[0]["content"].lower()


def test_config_builder_maps_vi_to_en_at_zleap_boundary():
    """zleap.sag.EngineConfig only supports Literal['zh', 'en']; 'vi' maps to 'en' at boundary."""
    assert build_engine_config(Settings(sag_language="zh")).language == "zh"
    assert build_engine_config(Settings(sag_language="en")).language == "en"
    assert build_engine_config(Settings(sag_language="vi")).language == "en"


@pytest.mark.parametrize(
    ("detected", "expected_pm_lang"),
    [("zh", "zh"), ("vi", "en"), ("en", "en")],
)
@pytest.mark.asyncio
async def test_incremental_processor_extract_honors_language_and_zleap_boundary(
    detected: str,
    expected_pm_lang: str,
):
    """_extract honors detected zh/en and maps vi -> en only at PromptManager boundary."""
    from zleap.sag.core.prompt.manager import PromptManager

    pm = PromptManager(language="en" if detected == "zh" else "zh")
    captured: dict[str, object] = {}

    async def fake_extract(chunk_set, options, observer=None, cancellation=None):
        captured["pm_language"] = pm.language
        captured["guidance_rules"] = options.guidance_rules
        return SimpleNamespace(stats={"token_usage": 0, "zero_event_chunks": []})

    engine = SimpleNamespace(
        resources=SimpleNamespace(prompts=pm),
        extract=fake_extract,
    )
    proc = IncrementalDocumentProcessor(engine, "src-cfg", max_concurrency=1)
    proc._detected_language = detected

    chunk_set = SimpleNamespace(chunk_ids=("c1",))
    checkpoint = SimpleNamespace(
        processed_chunk_ids=[],
        token_usage=0,
        eventless_chunk_ids=[],
    )

    async def should_pause() -> bool:
        return False

    async def on_checkpoint(_cp) -> None:
        return None

    cancelled, _ = await proc._extract(checkpoint, chunk_set, should_pause, on_checkpoint)
    assert cancelled is False
    assert proc._detected_language == detected
    assert captured["pm_language"] == expected_pm_lang
    assert len(captured["guidance_rules"]) >= 1
