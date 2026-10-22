from pathlib import Path

from sag_api.core.config import _DEFAULT_ENV_FILE, Settings


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
