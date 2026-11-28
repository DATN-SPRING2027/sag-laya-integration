"""Internal service-only endpoint for Continuum 1-on-1 DM Assistant grounded synthesis."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sag_api.core.config import settings
from sag_api.core.deps import get_llm
from sag_api.core.errors import (
    ApiError,
    BadRequestError,
    ForbiddenError,
    PayloadTooLargeError,
    ServiceUnavailableError,
)
from sag_api.core.logging import request_id_var, sanitize_request_id
from sag_api.core.principal_assertion import require_bff_service_token, required_bff_header
from sag_api.generation.llm import LLMClient
from sag_api.schemas.dm_assistant import (
    DmAssistantAnswerRequest,
    DmAssistantAnswerResponse,
    DmAssistantErrorEnvelopeOut,
)
from sag_api.services.dm_assistant_service import generate_dm_assistant_answer

DM_ASSISTANT_ANSWER_PATH = "/api/v1/internal/dm-assistant/answer"
DM_ASSISTANT_MAX_BODY_BYTES = 65_536
_HEX24_PATTERN = r"^[a-fA-F0-9]{24}$"
_HEX24_RE = re.compile(_HEX24_PATTERN)
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}

_DM_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Unsupported query parameters, invalid Content-Length, or unconfigured LLM provider.",
    },
    403: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Missing or invalid X-SAG-BFF-Service-Token or required 24-hex Continuum context headers.",
    },
    413: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Request body exceeds the 64 KiB hard HTTP byte limit.",
    },
    422: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Request payload failed schema validation (sanitized envelope without echoed input values).",
    },
    502: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Upstream LLM returned empty, malformed, non-JSON, or fabricated-citation output.",
    },
    503: {
        "model": DmAssistantErrorEnvelopeOut,
        "description": "Service token authentication is not configured or the upstream LLM provider is temporarily unavailable.",
    },
}


@dataclass(frozen=True, slots=True)
class DmAssistantContext:
    organization_id: str
    user_id: str
    conversation_id: str


def _require_hex24_header(request: Request, name: str) -> str:
    value = required_bff_header(request, name, max_length=24)
    if not _HEX24_RE.fullmatch(value):
        raise ForbiddenError("Invalid Continuum service context")
    return value


def _resolve_request_id(request: Request) -> str | None:
    rid_ctx = request_id_var.get()
    if rid_ctx and rid_ctx != "-":
        return rid_ctx
    raw_rid = getattr(request.state, "request_id", None) or request.headers.get("X-Request-Id")
    return sanitize_request_id(raw_rid) if raw_rid else None


def _check_content_length_header(request: Request, max_body_bytes: int) -> None:
    content_lengths = request.headers.getlist("content-length")
    if not content_lengths:
        return
    if len(content_lengths) > 1:
        raise BadRequestError("Duplicate Content-Length header")
    raw_length = content_lengths[0].strip()
    if not raw_length or not raw_length.isascii() or not raw_length.isdigit():
        raise BadRequestError("Invalid Content-Length header")
    if int(raw_length) > max_body_bytes:
        raise PayloadTooLargeError("DM assistant request body exceeds 64 KiB limit")


async def verify_dm_assistant_request_headers(request: Request) -> DmAssistantContext:
    """Enforce fail-closed BFF token, strict 24-hex Continuum headers, and no query params before body read."""
    if not (settings.bff_service_token or "").strip():
        raise ServiceUnavailableError("Continuum service authentication is not configured")
    await require_bff_service_token(request)

    organization_id = _require_hex24_header(request, "X-Continuum-Organization-Id")
    user_id = _require_hex24_header(request, "X-Continuum-User-Id")
    conversation_id = _require_hex24_header(request, "X-Continuum-Conversation-Id")

    if request.query_params:
        raise BadRequestError("Unsupported query parameters")

    return DmAssistantContext(
        organization_id=organization_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )


async def _enforce_body_limit_on_request(request: Request, max_body_bytes: int) -> None:
    """Read and cap body bytes on a Starlette Request when mounted without the ASGI middleware."""
    _check_content_length_header(request, max_body_bytes)
    buffered = bytearray()
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise PayloadTooLargeError("DM assistant request body exceeds 64 KiB limit")
        buffered.extend(chunk)
    request._body = bytes(buffered)


class DmAssistantGuardMiddleware:
    """Pure ASGI pre-parser auth and 64 KiB body-cap guard scoped to the DM assistant endpoint."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = DM_ASSISTANT_MAX_BODY_BYTES) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path", "").rstrip("/") != DM_ASSISTANT_ANSWER_PATH
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = _resolve_request_id(request)

        try:
            context = await verify_dm_assistant_request_headers(request)
            _check_content_length_header(request, self._max_body_bytes)
        except ApiError as exc:
            await self._send_error(scope, receive, send, exc, request_id=request_id)
            return

        buffered = bytearray()
        total_bytes = 0
        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "http.disconnect":
                return
            if msg_type != "http.request":
                continue
            chunk = message.get("body", b"")
            total_bytes += len(chunk)
            if total_bytes > self._max_body_bytes:
                await self._send_error(
                    scope,
                    receive,
                    send,
                    PayloadTooLargeError("DM assistant request body exceeds 64 KiB limit"),
                    request_id=request_id,
                )
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break

        scope.setdefault("state", {})["dm_assistant_context"] = context
        payload_bytes = bytes(buffered)
        body_delivered = False

        async def replay_receive() -> Message:
            nonlocal body_delivered
            if not body_delivered:
                body_delivered = True
                return {"type": "http.request", "body": payload_bytes, "more_body": False}
            return await receive()

        async def send_with_no_store(message: Message) -> None:
            if message.get("type") == "http.response.start":
                raw_headers = list(message.get("headers", []))
                has_cache_control = any(k.lower() == b"cache-control" for k, _ in raw_headers)
                if not has_cache_control:
                    raw_headers.append((b"cache-control", b"no-store"))
                    message = {**message, "headers": raw_headers}
            await send(message)

        await self.app(scope, replay_receive, send_with_no_store)

    @staticmethod
    async def _send_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        exc: ApiError,
        *,
        request_id: str | None,
    ) -> None:
        response = JSONResponse(
            status_code=exc.status_code,
            content=exc.to_envelope(request_id=request_id),
            headers=_NO_STORE_HEADERS,
        )
        await response(scope, receive, send)


class DmAssistantRoute(APIRoute):
    """Defense-in-depth pre-parser guard if router is mounted without DmAssistantGuardMiddleware."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original_route_handler = super().get_route_handler()

        async def guarded_route_handler(request: Request) -> Response:
            if getattr(request.state, "dm_assistant_context", None) is None:
                context = await verify_dm_assistant_request_headers(request)
                await _enforce_body_limit_on_request(request, DM_ASSISTANT_MAX_BODY_BYTES)
                request.state.dm_assistant_context = context
            response = await original_route_handler(request)
            response.headers.setdefault("Cache-Control", "no-store")
            return response

        return guarded_route_handler


router = APIRouter(prefix="/internal/dm-assistant", tags=["dm-assistant"], route_class=DmAssistantRoute)


async def require_dm_assistant_context(
    request: Request,
    _x_sag_bff_service_token: str = Header(
        ...,
        alias="X-SAG-BFF-Service-Token",
        min_length=1,
        description="Continuum BFF service-to-service shared secret token.",
    ),
    _x_continuum_organization_id: str = Header(
        ...,
        alias="X-Continuum-Organization-Id",
        min_length=24,
        max_length=24,
        pattern=_HEX24_PATTERN,
        description="24-hex Continuum organization ObjectId.",
    ),
    _x_continuum_user_id: str = Header(
        ...,
        alias="X-Continuum-User-Id",
        min_length=24,
        max_length=24,
        pattern=_HEX24_PATTERN,
        description="24-hex Continuum requesting user ObjectId.",
    ),
    _x_continuum_conversation_id: str = Header(
        ...,
        alias="X-Continuum-Conversation-Id",
        min_length=24,
        max_length=24,
        pattern=_HEX24_PATTERN,
        description="24-hex Continuum 1-on-1 DM conversation ObjectId.",
    ),
) -> DmAssistantContext:
    """Return pre-verified Continuum service context and declare exact OpenAPI headers."""
    del (
        _x_sag_bff_service_token,
        _x_continuum_organization_id,
        _x_continuum_user_id,
        _x_continuum_conversation_id,
    )
    existing = getattr(request.state, "dm_assistant_context", None)
    if isinstance(existing, DmAssistantContext):
        return existing
    return await verify_dm_assistant_request_headers(request)


@router.post(
    "/answer",
    response_model=DmAssistantAnswerResponse,
    responses=_DM_ERROR_RESPONSES,
    summary="Synthesize a grounded answer strictly from Continuum-supplied 1-on-1 DM excerpts",
)
async def answer_dm_question(
    request: Request,
    response: Response,
    body: DmAssistantAnswerRequest,
    context: DmAssistantContext = Depends(require_dm_assistant_context),
    llm: LLMClient = Depends(get_llm),
) -> DmAssistantAnswerResponse:
    request_id = _resolve_request_id(request)
    result = await generate_dm_assistant_answer(
        body=body,
        llm=llm,
        organization_id=context.organization_id,
        conversation_id=context.conversation_id,
        request_id=request_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
