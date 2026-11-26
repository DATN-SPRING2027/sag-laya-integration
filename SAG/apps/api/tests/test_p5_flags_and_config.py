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


def test_dead_strict_filtering_removed_and_agent_max_steps_unified():
    """S2-06/Focus C: dead document_strict_filtering/enable_strict_filtering removed; AGENT_MAX_STEPS == 6."""
    import inspect

    from sag_api.services import agent_service

    assert "document_strict_filtering" not in Settings.model_fields
    assert not hasattr(Settings(), "document_strict_filtering")
    sig = inspect.signature(IncrementalDocumentProcessor.__init__)
    assert "enable_strict_filtering" not in sig.parameters

    assert Settings().agent_max_steps == 6
    assert agent_service.AGENT_MAX_STEPS == 6


def test_search_strategy_default_capabilities_and_qdrant_provider_clarity():
    """S3-09/S4-08: search_strategy defaults to 'multi'; /capabilities reports canonical_hybrid; Qdrant clarity."""
    from pathlib import Path

    from sag_api.api.v1 import system as system_api
    from sag_api.sag import config_builder
    from sag_api.sag.qdrant_store import QdrantVectorStore

    assert Settings().search_strategy == "multi"
    assert settings_service.QUICK_SETUP_302["search_strategy"] == "multi"

    caps = system_api._capabilities()
    assert caps["canonical_search_strategy"] == "canonical_hybrid"
    assert caps["search_strategy"] in {"multi", "vector"}

    assert QdrantVectorStore.backend_provider == "qdrant"
    # zleap.sag.core.storage.schema.prepare_vector_schema skips Qdrant unless provider == "es".
    assert QdrantVectorStore.provider == "es"
    cb_source = Path(config_builder.__file__).read_text(encoding="utf-8")
    assert "TODO(REQ-7" not in cb_source


@pytest.mark.asyncio
async def test_canonical_search_reports_effective_strategy_canonical_hybrid():
    """S4-08: retrieve_search_unit_sections sets effective_strategy='canonical_hybrid' and search.py propagates it."""
    from sag_api.api.v1 import search as search_api
    from sag_api.services.search_unit_retrieval_service import retrieve_search_unit_sections

    outcome = await retrieve_search_unit_sections(
        engine_manager=SimpleNamespace(),
        sources=[],
        query="test query",
        principal=None,
    )
    assert outcome.stats["effective_strategy"] == "canonical_hybrid"

    plan = search_api._QueryRoutePlan(
        strategy="multi_es_fast",
        need_retrieval=True,
        trace={"effective_strategy": "multi_es_fast", "degraded": False},
        strategy_plan=SimpleNamespace(),
    )
    merged = search_api._with_query_route_stats(outcome.stats, plan)
    assert merged["query_route"]["effective_strategy"] == "canonical_hybrid"


def test_session_context_off_default_and_ecce_synthetic_disclaimers():
    """S4-12/S5-15/Focus C: session_context_mode defaults to 'off' and ECCE benchmark docs/code mark synthetic."""
    from pathlib import Path

    from tests import test_ecce_ablation_benchmark
    from tests.eval_ecce_baseline import benchmark_suite

    assert Settings().session_context_mode == "off"
    bench_src = Path(benchmark_suite.__file__).read_text(encoding="utf-8")
    test_src = Path(test_ecce_ablation_benchmark.__file__).read_text(encoding="utf-8")
    assert "SYNTHETIC OFFLINE BENCHMARK" in bench_src
    assert "SYNTHETIC OFFLINE BENCHMARK" in test_src

