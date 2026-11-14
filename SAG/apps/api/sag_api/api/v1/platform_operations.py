"""Service-only platform operations telemetry router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.api.v1.system import _qdrant_ready
from sag_api.core.db import get_session
from sag_api.core.errors import BadRequestError
from sag_api.core.principal_assertion import require_bff_service_token
from sag_api.schemas.platform_operations import PlatformAiOperationsOut
from sag_api.services import platform_operations_service as platform_ops_svc

router = APIRouter(prefix="/platform/ai", tags=["platform-ai"])
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


@router.get("/operations", response_model=PlatformAiOperationsOut)
async def get_platform_operations(
    request: Request,
    _service_auth: None = Depends(require_bff_service_token),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Return service-authenticated AI runtime, job, and corpus metadata without tenant content."""
    if request.query_params:
        raise BadRequestError("Unsupported query parameters")

    runtime = getattr(request.app.state, "knowledge_runtime", None)
    knowledge_ready = bool(runtime and runtime.ready)
    qdrant_ready = await _qdrant_ready(request)

    result = await platform_ops_svc.read_platform_operations(
        session,
        knowledge_runtime_ready=knowledge_ready,
        qdrant_ready=qdrant_ready,
    )
    return JSONResponse(
        status_code=200,
        content=result.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )
