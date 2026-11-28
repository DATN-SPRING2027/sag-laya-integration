"""Internal service-only endpoint for Continuum 1-on-1 DM Assistant grounded synthesis."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response

from sag_api.core.config import settings
from sag_api.core.deps import get_llm
from sag_api.core.errors import BadRequestError, ForbiddenError, ServiceUnavailableError
from sag_api.core.logging import sanitize_request_id
from sag_api.core.principal_assertion import require_bff_service_token, required_bff_header
from sag_api.generation.llm import LLMClient
from sag_api.schemas.dm_assistant import DmAssistantAnswerRequest, DmAssistantAnswerResponse
from sag_api.services.dm_assistant_service import generate_dm_assistant_answer

router = APIRouter(prefix="/internal/dm-assistant", tags=["dm-assistant"])
_HEX24_RE = re.compile(r"^[a-fA-F0-9]{24}$")


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


async def require_dm_assistant_context(request: Request) -> DmAssistantContext:
    """Enforce fail-closed BFF service token, strict 24-hex Continuum headers, and no query params."""
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


@router.post(
    "/answer",
    response_model=DmAssistantAnswerResponse,
    summary="Synthesize a grounded answer strictly from Continuum-supplied 1-on-1 DM excerpts",
    dependencies=[Depends(require_dm_assistant_context)],
)
async def answer_dm_question(
    request: Request,
    response: Response,
    body: DmAssistantAnswerRequest,
    context: DmAssistantContext = Depends(require_dm_assistant_context),
    llm: LLMClient = Depends(get_llm),
) -> DmAssistantAnswerResponse:
    raw_request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-Id")
    request_id = sanitize_request_id(raw_request_id) if raw_request_id else None
    result = await generate_dm_assistant_answer(
        body=body,
        llm=llm,
        organization_id=context.organization_id,
        conversation_id=context.conversation_id,
        request_id=request_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return result
