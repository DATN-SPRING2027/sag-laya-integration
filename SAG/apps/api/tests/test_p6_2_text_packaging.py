"""P6-2 Packet 3 TDD regression tests: tiktoken recovery, pyproject deps, compose alignment, engine_manager."""

from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from sag_api.core import text_utils
from sag_api.core.config import Settings
from sag_api.sag.engine_manager import EngineManager
from sag_api.services.search_index_service import (
    build_embedding_model_signature,
    put_cached_text_embedding,
)

_API_ROOT = Path(__file__).resolve().parents[1]
_SAG_ROOT = Path(__file__).resolve().parents[3]


def test_tiktoken_fallback_increments_every_call_warns_once_and_recovers_after_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """tiktoken must never @lru_cache None forever; must count every fallback call and recover after cooldown."""
    text_utils.reset_tiktoken_fallback_state()
    try:
        fake_now = [1000.0]

        with (
            caplog.at_level("WARNING", logger="sag_api.text_utils"),
            patch.object(text_utils.time, "monotonic", side_effect=lambda: fake_now[0]),
            patch("tiktoken.get_encoding", side_effect=RuntimeError("transient cold cache")) as mock_get,
        ):
            for _ in range(3):
                est = text_utils.estimate_tokens("Quy định chi tiết thi hành.")
                assert est > 0

        # Cooldown prevents hammering tiktoken.get_encoding on every call, yet every fallback increments counter!
        assert mock_get.call_count == 1
        warning_records = [r for r in caplog.records if "tiktoken" in r.getMessage().lower()]
        assert len(warning_records) == 1
        assert text_utils.get_tiktoken_fallback_status() == {
            "degraded": True,
            "fallback_count": 3,
            "reason": "load:RuntimeError",
        }

        # Advance past cooldown -> real tiktoken encoding loads and recovers cleanly!
        fake_now[0] += text_utils._TIKTOKEN_RETRY_COOLDOWN_SECONDS + 1.0
        import tiktoken

        real_enc = tiktoken.get_encoding("cl100k_base")
        sample = "Hello world <|endoftext|> Tiếng Việt"
        expected_real = len(real_enc.encode(sample, disallowed_special=()))

        with (
            patch.object(text_utils.time, "monotonic", side_effect=lambda: fake_now[0]),
            patch("tiktoken.get_encoding", return_value=real_enc) as mock_recover,
        ):
            got_1 = text_utils.estimate_tokens(sample)
            got_2 = text_utils.estimate_tokens(sample)

        assert got_1 == expected_real
        assert got_2 == expected_real
        assert mock_recover.call_count == 1  # Cached once recovered
        status = text_utils.get_tiktoken_fallback_status()
        assert status["degraded"] is False
        assert status["fallback_count"] == 3
    finally:
        text_utils.reset_tiktoken_fallback_state()


def test_pyproject_declares_all_five_missing_runtime_dependencies() -> None:
    """Direct runtime imports Pillow, regex, tiktoken, beautifulsoup4, markdownify must be declared."""
    pyproject_path = _API_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    raw_deps = data["project"]["dependencies"]
    dep_names = {
        re.split(r"[<>=!~;\[\s]", dep, maxsplit=1)[0].strip().lower()
        for dep in raw_deps
    }
    for required in ("pillow", "regex", "tiktoken", "beautifulsoup4", "markdownify"):
        assert required in dep_names, f"Missing direct runtime dependency {required!r} in pyproject.toml"


def test_compose_files_map_pr75_env_vars_and_align_sag_data_dir() -> None:
    """compose.yaml must expose PR #75 env vars and compose.postgres.yaml must align SAG_DATA_DIR=/data/engine."""
    base_compose = yaml.safe_load((_SAG_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    pg_compose = yaml.safe_load((_SAG_ROOT / "compose.postgres.yaml").read_text(encoding="utf-8"))

    api_env = base_compose["services"]["api"]["environment"]
    pg_api_env = pg_compose["services"]["api"]["environment"]

    expected_vars = (
        "SAG_DECISION_BASE_URL",
        "SAG_DECISION_API_KEY",
        "SAG_DECISION_TIMEOUT_MS",
        "SAG_JOB_CONCURRENCY",
        "SAG_SEARCH_STRATEGY",
        "SAG_SEARCH_MIN_DENSE_SCORE",
        "SAG_SEARCH_RECONCILE_INTERVAL_SECONDS",
        "SAG_SESSION_CONTEXT_MODE",
        "SAG_AGENT_MAX_STEPS",
        "SAG_EVAL_LLM_JUDGE_ENABLED",
        "SAG_LOCAL_PARSE_WORKERS",
        "SAG_LOCAL_PARSE_TIMEOUT_SECONDS",
        "SAG_LOCAL_PARSE_MAX_TASKS_PER_CHILD",
        "SAG_MINERU_IO_CONCURRENCY",
        "SAG_FAST_PARSE_CONCURRENCY",
        "SAG_PER_PROJECT_HEAVY_PARSE_LIMIT",
        "SAG_EXCEL_MAX_ROWS_PER_SHEET",
        "SAG_EXCEL_MAX_COLS_PER_SHEET",
        "SAG_PRINCIPAL_ASSERTION_ISSUER",
        "SAG_PRINCIPAL_ASSERTION_AUDIENCE",
        "SAG_PRINCIPAL_ASSERTION_JWKS_URL",
        "SAG_PRINCIPAL_ASSERTION_MAX_LIFETIME_SECONDS",
        "SAG_PRINCIPAL_ASSERTION_CLOCK_SKEW_SECONDS",
        "SAG_PRINCIPAL_ASSERTION_JWKS_CACHE_TTL_SECONDS",
        "SAG_PRINCIPAL_ASSERTION_MAX_PROJECT_IDS",
    )
    missing = [var for var in expected_vars if var not in api_env]
    assert missing == [], f"Missing env vars in SAG/compose.yaml api.environment: {missing}"

    assert api_env["SAG_DATA_DIR"] == "/data/engine"
    assert pg_api_env["SAG_DATA_DIR"] == "/data/engine"


@pytest.mark.asyncio
async def test_engine_manager_removed_dead_shared_cache_wrapper_and_preserves_cache_metering() -> None:
    """Redundant _sag_shared_cache_wrapped block must be removed from engine_manager while caching still works."""
    src = inspect.getsource(EngineManager._configure_embedding_client)
    assert "_sag_shared_cache_wrapped" not in src
    assert "_cached_batch_generate" not in src

    api_calls: list[list[str]] = []

    class FakeEmbedding:
        async def generate(self, text: str, **_kwargs):
            api_calls.append([text])
            return [0.2, 0.4, 0.6]

        async def batch_generate(self, texts: list[str], **_kwargs):
            api_calls.append(list(texts))
            return [[0.2, 0.4, 0.6] for _ in texts]

    cfg = Settings(_env_file=None)
    sig = build_embedding_model_signature(settings_obj=cfg)
    put_cached_text_embedding("cached-p62-text", [0.9, 0.1, 0.0], model_signature=sig)

    fake_engine = SimpleNamespace(resources=SimpleNamespace(embedding=FakeEmbedding()))
    mgr = EngineManager(cfg)
    mgr._configure_embedding_request_dimensions(fake_engine)

    vecs = await fake_engine.resources.embedding.batch_generate(["cached-p62-text", "uncached-p62-text"])
    assert vecs[0] == [0.9, 0.1, 0.0]
    assert vecs[1] == [0.2, 0.4, 0.6]
    assert api_calls == [["uncached-p62-text"]]
