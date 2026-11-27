"""FastAPI dependency: xác thực + singleton cấp ứng dụng. Đơn người dùng, không workspace/vai trò."""

from __future__ import annotations

import secrets
from typing import Literal

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.errors import AuthError, ForbiddenError
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import (
    VerifiedPrincipal as AssertionVerifiedPrincipal,
)
from sag_api.core.principal_assertion import (
    require_principal_assertion,
    require_project_read_principal,
)
from sag_api.core.security import decode_token
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.auth_service import get_user
from sag_api.services.dsh_integration_service import authenticate_connector

_bearer = HTTPBearer(auto_error=False)
_AuthKind = Literal["jwt", "connector"]
log = get_logger("deps")


def _require_matching_auth_mode(payload: dict) -> None:
    if settings.auth_mode in {"password", "continuum"} and payload.get("auth_mode") != settings.auth_mode:
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


async def require_model_config_operator(
    user: User = Depends(get_current_user),
) -> User:
    """Restrict shared model settings to explicitly configured users in multi-user modes."""
    if settings.auth_mode == "local":
        return user

    operator_ids = {
        user_id.strip()
        for user_id in settings.model_config_operator_ids.split(",")
        if user_id.strip()
    }
    if str(user.id) not in operator_ids:
        raise ForbiddenError("Người dùng không có quyền thay đổi cấu hình model dùng chung")
    return user


async def require_operator_or_service_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Allow trusted BFF/service credentials or explicit model-config operators."""
    bff_tokens = request.headers.getlist("X-SAG-BFF-Service-Token")
    if bff_tokens:
        if (
            len(bff_tokens) == 1
            and settings.bff_service_token
            and secrets.compare_digest(bff_tokens[0], settings.bff_service_token)
        ):
            request.state.auth_kind = "bff_service"
            return None
        raise ForbiddenError("Invalid Continuum service credential")

    if creds is not None and creds.credentials and settings.service_api_key:
        if secrets.compare_digest(creds.credentials, settings.service_api_key):
            log.warning(
                "Authenticated internal service via SAG_SERVICE_API_KEY method=%s path=%s",
                request.method,
                request.url.path,
            )
            request.state.auth_kind = "service_api_key"
            return None

    if creds is None or not creds.credentials:
        raise AuthError("Thiếu token xác thực")
    user = await get_current_user(request, token=creds.credentials, session=session)
    return await require_model_config_operator(user=user)


async def require_authenticated_or_service_token(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Allow trusted BFF/service credentials or any authenticated user for read-only UI capabilities."""
    bff_tokens = request.headers.getlist("X-SAG-BFF-Service-Token")
    if bff_tokens:
        if (
            len(bff_tokens) == 1
            and settings.bff_service_token
            and secrets.compare_digest(bff_tokens[0], settings.bff_service_token)
        ):
            request.state.auth_kind = "bff_service"
            return None
        raise ForbiddenError("Invalid Continuum service credential")

    if creds is not None and creds.credentials and settings.service_api_key:
        if secrets.compare_digest(creds.credentials, settings.service_api_key):
            request.state.auth_kind = "service_api_key"
            return None

    if creds is None or not creds.credentials:
        raise AuthError("Thiếu token xác thực")
    return await get_current_user(request, token=creds.credentials, session=session)


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


async def _resolve_path_source_access(
    request: Request,
    principal: AssertionVerifiedPrincipal,
    session: AsyncSession,
):
    from sag_api.core.errors import NotFoundError
    from sag_api.services.source_service import get_authorized_source

    source_id = request.path_params.get("source_id") or request.query_params.get("source_id")
    if not source_id:
        raise NotFoundError("信源不存在")
    source = await get_authorized_source(session, principal=principal, source_id=source_id)
    if source is None:
        raise NotFoundError("信源不存在")
    document_id = request.path_params.get("document_id") or request.query_params.get("document_id")
    if document_id:
        from sag_api.services.source_service import get_authorized_document

        document = await get_authorized_document(
            session,
            principal=principal,
            source_ids=[source.id],
            document_id=document_id,
        )
        if document is None:
            raise NotFoundError("Document not found")
    return source


async def require_path_source_access(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Use the scoped session for GETs; mutations still require a BE assertion."""
    principal = (
        await require_project_read_principal(request)
        if request.method == "GET"
        else await require_principal_assertion(request)
    )
    return await _resolve_path_source_access(request, principal, session)
