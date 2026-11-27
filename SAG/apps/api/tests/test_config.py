from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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


def test_prod_environment_enforces_debug_false_and_strong_secret_key() -> None:
    """XC-02: environment='prod' coerces unset debug=False, rejects debug=True and weak/short secret_key."""
    strong_secret = "prod-strong-secret-key-at-least-32-chars-0123456789"
    prod_default_debug = Settings(
        _env_file=None,
        environment="prod",
        secret_key=strong_secret,
    )
    assert prod_default_debug.debug is False

    with pytest.raises(ValidationError, match="SAG_DEBUG|debug"):
        Settings(
            _env_file=None,
            environment="prod",
            debug=True,
            secret_key=strong_secret,
        )

    for weak_secret in (
        "short-secret",
        "dev-insecure-secret-change-me-in-production-0123456789",
        "dev-secret-change-me-at-least-32-bytes",
        "please-change-this-in-production-0123456789",
    ):
        with pytest.raises(ValidationError, match="secret_key|SAG_SECRET_KEY"):
            Settings(
                _env_file=None,
                environment="prod",
                secret_key=weak_secret,
            )


def test_qdrant_env_alias_choices_support_both_single_and_legacy_double_prefix(monkeypatch) -> None:
    """S3-10: SAG_QDRANT_URL / SAG_QDRANT_API_KEY work alongside legacy SAG_SAG_QDRANT_* aliases."""
    for key in ("SAG_QDRANT_URL", "SAG_SAG_QDRANT_URL", "SAG_QDRANT_API_KEY", "SAG_SAG_QDRANT_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("SAG_SAG_QDRANT_URL", "http://legacy-qdrant:6333")
    monkeypatch.setenv("SAG_SAG_QDRANT_API_KEY", "legacy-key")
    legacy = Settings(_env_file=None)
    assert legacy.sag_qdrant_url == "http://legacy-qdrant:6333"
    assert legacy.sag_qdrant_api_key == "legacy-key"

    monkeypatch.setenv("SAG_QDRANT_URL", "http://canonical-qdrant:6333")
    monkeypatch.setenv("SAG_QDRANT_API_KEY", "canonical-key")
    canonical = Settings(_env_file=None)
    assert canonical.sag_qdrant_url == "http://canonical-qdrant:6333"
    assert canonical.sag_qdrant_api_key == "canonical-key"

    kw = Settings(_env_file=None, sag_qdrant_url="http://kw-qdrant:6333", sag_qdrant_api_key="kw-key")
    assert kw.sag_qdrant_url == "http://kw-qdrant:6333"
    assert kw.sag_qdrant_api_key == "kw-key"


def test_service_api_key_declared_in_settings_and_audit_logged(monkeypatch, caplog) -> None:
    """XC-01 & SAG-SEC-006: Settings.service_api_key strips blanks, dead get_verified_principal is removed, and operator/service dependency audit-logs safely."""
    import asyncio
    import logging

    from sag_api.core import deps

    assert "service_api_key" in Settings.model_fields
    assert Settings(_env_file=None, service_api_key="  ").service_api_key is None
    assert Settings(_env_file=None, service_api_key=" svc-key-123 ").service_api_key == "svc-key-123"
    assert not hasattr(deps, "get_verified_principal")

    from fastapi.security import HTTPAuthorizationCredentials
    from starlette.datastructures import Headers

    secret_val = "super-secret-internal-service-key-xyz"
    monkeypatch.setattr(deps.settings, "service_api_key", secret_val)
    fake_request = SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/system/metrics"),
        method="GET",
        headers=Headers({"authorization": f"Bearer {secret_val}"}),
        state=SimpleNamespace(),
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            deps.require_operator_or_service_token(
                fake_request,
                creds=HTTPAuthorizationCredentials(scheme="Bearer", credentials=secret_val),
                session=None,  # type: ignore[arg-type]
            )
        )
    assert all(secret_val not in rec.message for rec in caplog.records)


def test_env_examples_have_no_unknown_sag_keys_and_load_cleanly() -> None:
    """§4.2: Both .env.example files declare only valid Settings keys and parse without ValidationError."""
    sag_root = Path(__file__).resolve().parents[3]
    api_env_example = sag_root / "apps" / "api" / ".env.example"
    root_env_example = sag_root / ".env.example"

    allowed_env_names: set[str] = set()
    for field_name, field_info in Settings.model_fields.items():
        allowed_env_names.add(f"SAG_{field_name.upper()}")
        alias = getattr(field_info, "validation_alias", None)
        if alias is not None and hasattr(alias, "choices"):
            for choice in alias.choices:
                allowed_env_names.add(str(choice).upper())

    for env_path in (api_env_example, root_env_example):
        raw_keys: set[str] = set()
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key.startswith("SAG_"):
                raw_keys.add(key)
        unknown = sorted(raw_keys - allowed_env_names)
        assert unknown == [], f"{env_path} contains unknown SAG_* keys: {unknown}"

    class ApiExampleSettings(Settings):
        model_config = {**Settings.model_config, "env_file": api_env_example}

    loaded = ApiExampleSettings()
    assert loaded.search_strategy == "multi"
    assert loaded.sag_language == "en"


def test_require_authenticated_or_service_token_allows_regular_user_and_rejects_anonymous(monkeypatch) -> None:
    """Read-only /system/capabilities allows any authenticated SAG Web user while rejecting anonymous requests."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi.security import HTTPAuthorizationCredentials
    from starlette.datastructures import Headers

    from sag_api.core import deps
    from sag_api.core.errors import AuthError

    fake_user = SimpleNamespace(id="regular-member-1", is_active=True)
    monkeypatch.setattr(deps, "get_current_user", AsyncMock(return_value=fake_user))
    fake_request = SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/system/capabilities"),
        method="GET",
        headers=Headers({}),
        state=SimpleNamespace(),
    )

    resolved = asyncio.run(
        deps.require_authenticated_or_service_token(
            fake_request,
            creds=HTTPAuthorizationCredentials(scheme="Bearer", credentials="user-jwt"),
            session=None,  # type: ignore[arg-type]
        )
    )
    assert resolved is fake_user

    with pytest.raises(AuthError):
        asyncio.run(
            deps.require_authenticated_or_service_token(
                fake_request,
                creds=None,
                session=None,  # type: ignore[arg-type]
            )
        )
