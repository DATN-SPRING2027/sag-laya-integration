"""FastAPI dependency: xác thực + singleton cấp ứng dụng. Đơn người dùng, không workspace/vai trò."""

from __future__ import annotations

import os
import secrets
from typing import Literal

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import AuthError
from sag_api.core.identity import VerifiedPrincipal
from sag_api.core.security import decode_token
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.auth_service import get_user
from sag_api.services.dsh_integration_service import authenticate_connector

_bearer = HTTPBearer(auto_error=False)
_AuthKind = Literal["jwt", "connector"]


def _require_matching_auth_mode(payload: dict) -> None:
    if settings.auth_mode == "password" and payload.get("auth_mode") != "password":
        raise AuthError("Chế độ xác thực đã thay đổi, vui lòng đăng nhập lại")


async def _get_bearer_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if creds is None:
        raise AuthError("Thiếu token xác thực")
    return creds.credentials


async def _authenticate_user_principal(
    session: AsyncSession,
    token: str,
) -> tuple[User, _AuthKind] | None:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        user = await authenticate_connector(session, token)
        return (user, "connector") if user is not None else None
    user_id = payload.get("sub")
    _require_matching_auth_mode(payload)
    user = await get_user(session, user_id) if user_id else None
    return (user, "jwt") if user is not None and user.is_active else None


async def authenticate_user_token(session: AsyncSession, token: str) -> User | None:
    """Authenticate either token kind for user-only callers such as MCP."""
    principal = await _authenticate_user_principal(session, token)
    return principal[0] if principal is not None else None


async def get_current_user(
    request: Request,
    token: str = Depends(_get_bearer_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as error:
        raise AuthError("Token không hợp lệ hoặc đã hết hạn") from error
    user_id = payload.get("sub")
    _require_matching_auth_mode(payload)
    user = await get_user(session, user_id) if user_id else None
    if user is None or not user.is_active:
        raise AuthError("Người dùng không tồn tại hoặc đã bị vô hiệu hóa")
    request.state.user = user
    request.state.auth_kind = "jwt"
    return user


async def get_current_user_or_connector(
    request: Request,
    token: str = Depends(_get_bearer_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Authenticate approved knowledge operations with JWT or local connector token."""
    principal = await _authenticate_user_principal(session, token)
    if principal is None:
        raise AuthError("Token không hợp lệ hoặc đã hết hạn")
    user, auth_kind = principal
    request.state.user = user
    request.state.auth_kind = auth_kind
    return user


def get_engine_manager(request: Request) -> EngineManager:
    return request.app.state.engine_manager


def get_job_queue(request: Request) -> JobQueue | None:
    return getattr(request.app.state, "job_queue", None)


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def get_tool_registry():
    """Registry công cụ Agent (công cụ tìm kiếm/thực thể tích hợp sẵn + công cụ MCP được inject lúc runtime)."""
    from sag_api.tools import registry

    return registry


async def get_verified_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> VerifiedPrincipal:
    """Verify principal identity and authorized project/security partition scopes at API boundary."""
    if creds is None or not creds.credentials:
        raise AuthError("Missing authentication token", code=ErrorCode.UNAUTHORIZED)

    token = creds.credentials
    # Hỗ trợ credential riêng biệt (service_api_key) cho internal service nếu được cấu hình.
    # Tuyệt đối không chấp nhận settings.secret_key (khóa ký HMAC) làm bearer token.
    service_api_key = getattr(settings, "service_api_key", None) or os.getenv("SAG_SERVICE_API_KEY")
    if service_api_key and secrets.compare_digest(token, service_api_key):
        principal = VerifiedPrincipal(
            user_id="service:internal",
            tenant_id="tenant_continuum_default",
            allowed_projects=frozenset({"*"}),
            allowed_partitions=frozenset({"*"}),
            is_service=True,
            roles=("service", "admin"),
        )
        request.state.principal = principal
        return principal

    try:
        payload = decode_token(token)
    except jwt.PyJWTError as err:
        raise AuthError("Token is invalid or expired", code=ErrorCode.UNAUTHORIZED) from err

    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token is missing subject claim", code=ErrorCode.UNAUTHORIZED)

    tenant_id = (
        payload.get("tenant_id")
        or payload.get("orgId")
        or payload.get("org_id")
        or "tenant_continuum_default"
    )

    roles_list = payload.get("roles", [])
    roles = tuple(roles_list) if isinstance(roles_list, (list, tuple)) else ()

    is_service = bool(
        str(user_id).startswith("service:")
        or payload.get("role") == "service"
        or "service" in roles
        or payload.get("iss") == "continuum-backend"
    )

    allowed_projects_set: set[str] = set()
    if payload.get("activeProjectId"):
        allowed_projects_set.add(str(payload["activeProjectId"]))
    if payload.get("project_id"):
        allowed_projects_set.add(str(payload["project_id"]))
    for key in ("projects", "allowed_projects"):
        val = payload.get(key)
        if isinstance(val, (list, set, tuple)):
            allowed_projects_set.update(str(p) for p in val)
        elif isinstance(val, str) and val.strip():
            allowed_projects_set.add(val.strip())
    if is_service and not allowed_projects_set:
        allowed_projects_set.add("*")

    allowed_partitions_set: set[str] = set()
    for key in ("security_partition", "security_partitions", "allowed_partitions"):
        val = payload.get(key)
        if isinstance(val, (list, set, tuple)):
            allowed_partitions_set.update(str(p) for p in val)
        elif isinstance(val, str) and val.strip():
            allowed_partitions_set.add(val.strip())

    principal = VerifiedPrincipal(
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        allowed_projects=frozenset(allowed_projects_set),
        allowed_partitions=frozenset(allowed_partitions_set),
        is_service=is_service,
        roles=roles,
    )
    request.state.principal = principal
    return principal
