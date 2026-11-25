from pathlib import Path

from sag_api.core.config import _DEFAULT_ENV_FILE, Settings
from sag_api.services import settings_service


def test_default_dotenv_is_resolved_from_repository_root() -> None:
    project_root = Path(__file__).resolve().parents[3]

    assert _DEFAULT_ENV_FILE == project_root / ".env"
    assert _DEFAULT_ENV_FILE.is_absolute()
    assert Settings.model_config["env_file"] == _DEFAULT_ENV_FILE


def test_dotenv_is_loaded_and_process_environment_takes_precedence(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SAG_LLM_PROVIDER=anthropic\n"
        "SAG_LLM_MODEL=claude-test-model\n"
        "SAG_SILICONFLOW_BASE_URL=https://api.siliconflow.com/v1\n"
        "SAG_SILICONFLOW_API_KEY=unit-test-key\n",
        encoding="utf-8",
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    for name in ("SAG_LLM_PROVIDER", "SAG_LLM_MODEL", "SAG_SILICONFLOW_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    class DotenvSettings(Settings):
        model_config = {**Settings.model_config, "env_file": env_file}

    monkeypatch.chdir(runtime_dir)
    loaded = DotenvSettings()
    assert loaded.llm_provider == "anthropic"
    assert loaded.llm_model == "claude-test-model"
    assert loaded.siliconflow_base_url == "https://api.siliconflow.com/v1"
    assert loaded.siliconflow_api_key == "unit-test-key"

    monkeypatch.setenv("SAG_LLM_MODEL", "process-env-model")
    assert DotenvSettings().llm_model == "process-env-model"


def test_mineru_official_api_token_is_sole_mineru_configuration(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SAG_MINERU_PROVIDER=302\n"
        "SAG_MINERU_BASE_URL=https://api.302ai.cn\n"
        "SAG_MINERU_OFFICIAL_API_TOKEN=official-token-v4-test\n"
        "SAG_MINERU_OFFICIAL_MODEL=vlm\n",
        encoding="utf-8",
    )
    for name in (
        "SAG_MINERU_PROVIDER",
        "SAG_MINERU_BASE_URL",
        "SAG_MINERU_OFFICIAL_API_TOKEN",
        "SAG_MINERU_OFFICIAL_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    class DotenvSettings(Settings):
        model_config = {**Settings.model_config, "env_file": env_file}

    loaded = DotenvSettings()
    assert loaded.mineru_provider == "official"
    assert loaded.mineru_base_url == "https://mineru.net"
    assert loaded.mineru_official_api_token == "official-token-v4-test"
    assert loaded.mineru_configured is True
    assert loaded.effective_document_parser == "mineru"

    unconfigured = Settings(
        _env_file=None,
        mineru_official_api_token="",
    )
    assert unconfigured.mineru_official_api_token is None
    assert unconfigured.mineru_configured is False
    assert unconfigured.effective_document_parser == "markitdown"


def test_legacy_laya_and_openrouter_decision_config_shims_migrate_to_siliconflow() -> None:
    migrated_laya = Settings(
        _env_file=None,
        decision_provider="laya",
        decision_model="typesafe/jev-1.14",
    )
    assert migrated_laya.decision_provider == "siliconflow"
    assert migrated_laya.decision_model == "Kev-4b"

    migrated_openrouter = Settings(
        _env_file=None,
        decision_provider="openrouter",
    )
    assert migrated_openrouter.decision_provider == "siliconflow"
    assert migrated_openrouter.decision_model == "Kev-4b"

    cleaned_overrides = settings_service._normalize_overrides(
        {
            "decision_provider": "openrouter",
            "openrouter_api_key": "legacy-secret",
            "laya_model_path": "/models/laya",
            "laya_device": "cpu",
        }
    )
    assert cleaned_overrides == {
        "decision_provider": "siliconflow",
        "decision_model": "Kev-4b",
    }


def test_r2_object_ingest_settings_removed() -> None:
    for removed_field in (
        "object_ingest_allowed_hosts",
        "object_ingest_allow_private_hosts_in_dev",
        "object_ingest_timeout_seconds",
        "remote_snapshot_gc_enabled",
    ):
        assert removed_field not in Settings.model_fields
        assert not hasattr(Settings, removed_field)
