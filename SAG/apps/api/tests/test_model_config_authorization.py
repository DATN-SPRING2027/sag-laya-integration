from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sag_api.api.v1 import system
from sag_api.core.config import settings
from sag_api.core.deps import get_current_user, get_session, require_platform_configuration_operator
from sag_api.core.errors import ApiError, ForbiddenError


def _app_for_user(user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.to_envelope())

    async def current_user() -> SimpleNamespace:
        return SimpleNamespace(id=user_id)

    app.state.session_opened = 0

    async def session() -> Any:
        app.state.session_opened += 1
        yield None

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_session] = session
    app.state.knowledge_runtime = SimpleNamespace(apply_settings=AsyncMock())
    return app


def _side_effect_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    spies = {
        "save_system_preferences": AsyncMock(return_value={"timezone": "UTC"}),
        "save_model_config": AsyncMock(return_value={"llm_model": "attacker-model"}),
        "model_setup_status": AsyncMock(return_value={"required": True}),
        "save_302_quick_setup": AsyncMock(return_value={"llm_model": "attacker-model"}),
        "save_302_mineru_setup": AsyncMock(return_value={"document_parser": "mineru"}),
        "get_dsh_state": AsyncMock(),
        "update_dsh_settings": AsyncMock(),
        "regenerate_dsh_token": AsyncMock(),
        "provider_call": AsyncMock(),
    }
    monkeypatch.setattr(system.settings_service, "effective_model_config", lambda: {})
    monkeypatch.setattr(system.settings_service, "save_system_preferences", spies["save_system_preferences"])
    monkeypatch.setattr(system.settings_service, "save_model_config", spies["save_model_config"])
    monkeypatch.setattr(system.settings_service, "model_setup_status", spies["model_setup_status"])
    monkeypatch.setattr(system.settings_service, "save_302_quick_setup", spies["save_302_quick_setup"])
    monkeypatch.setattr(system.settings_service, "save_302_mineru_setup", spies["save_302_mineru_setup"])
    monkeypatch.setattr(system.dsh_integration_service, "get_or_create_state", spies["get_dsh_state"])
    monkeypatch.setattr(
        system.dsh_integration_service,
        "update_default_source_and_publish",
        spies["update_dsh_settings"],
    )
    monkeypatch.setattr(
        system.dsh_integration_service,
        "regenerate_token_and_publish",
        spies["regenerate_dsh_token"],
    )
    monkeypatch.setattr(
        system,
        "LLMClient",
        lambda _settings: SimpleNamespace(configured=True, complete=spies["provider_call"]),
    )
    return spies


@pytest.mark.parametrize(
    ("auth_mode", "method", "path", "body"),
    [
        ("local", "PUT", "/api/v1/system/model-config", {"llm_model": "attacker-model"}),
        ("local", "POST", "/api/v1/system/model-config/test", {"llm_base_url": "https://attacker.example/v1"}),
        ("continuum", "PUT", "/api/v1/system/model-config", {"llm_model": "attacker-model"}),
        (
            "continuum",
            "POST",
            "/api/v1/system/model-config/test",
            {"llm_base_url": "https://attacker.example/v1"},
        ),
        ("continuum", "POST", "/api/v1/system/model-setup/302", {"api_key": "test-only-key"}),
        ("continuum", "POST", "/api/v1/system/model-config/mineru/302", None),
        ("password", "PUT", "/api/v1/system/model-config", {"llm_model": "attacker-model"}),
        ("password", "POST", "/api/v1/system/model-config/test", {"llm_base_url": "https://attacker.example/v1"}),
    ],
)
async def test_user_without_platform_permission_cannot_access_shared_model_settings(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(settings, "auth_mode", auth_mode)
    monkeypatch.setenv("SAG_MODEL_CONFIG_OPERATOR_IDS", "operator-id")
    app = _app_for_user("ordinary-user-id")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_legacy_allowlist_does_not_grant_platform_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setenv("SAG_MODEL_CONFIG_OPERATOR_IDS", "other-id, operator-id")

    with pytest.raises(ForbiddenError):
        await require_platform_configuration_operator(SimpleNamespace(id="operator-id"))


@pytest.mark.parametrize("auth_mode", ["local", "continuum", "password"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/system/dsh/export", None),
        ("PUT", "/api/v1/system/dsh/settings", {"default_source_id": "source-id"}),
        ("POST", "/api/v1/system/dsh/regenerate", None),
        ("PUT", "/api/v1/system/preferences", {"timezone": "UTC"}),
        ("PUT", "/api/v1/system/model-config", {"llm_model": "attacker-model"}),
        ("POST", "/api/v1/system/model-config/test", {"llm_base_url": "https://attacker.example/v1"}),
        ("POST", "/api/v1/system/model-setup/302", {"api_key": "test-only-key"}),
        ("POST", "/api/v1/system/model-config/mineru/302", None),
    ],
)
async def test_user_without_current_platform_permission_has_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(settings, "auth_mode", auth_mode)
    monkeypatch.setenv("SAG_MODEL_CONFIG_OPERATOR_IDS", "operator-id")
    spies = _side_effect_spies(monkeypatch)
    app = _app_for_user("operator-id")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            json=body,
            headers={
                "X-Platform-Role": "PLATFORM_OPERATOR",
                "X-Platform-Permissions": "platform.ai.operations.read",
            },
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert all(spy.await_count == 0 for spy in spies.values())
    assert app.state.knowledge_runtime.apply_settings.await_count == 0
    assert app.state.session_opened == 0


async def test_local_mode_is_denied_without_a_platform_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_mode", "local")

    with pytest.raises(ForbiddenError):
        await require_platform_configuration_operator(SimpleNamespace(id="local-user-id"))
