"""Project-scoped BFF endpoints for the Cross-Document Conflict Inbox & HITL Resolution API."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.deps import get_current_user, get_session
from sag_api.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from sag_api.core.principal_assertion import (
    VerifiedPrincipal,
    require_bff_service_token,
    require_project_bff_principal,
    require_project_read_principal,
)
from sag_api.db.models import KnowledgeConflictResolution, User
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
)
from sag_api.schemas.conflict_resolution import (
    ConflictActionResponse,
    ConflictItemOut,
    ConflictListResponse,
    ConflictReopenRequest,
    ConflictResolveRequest,
    ConflictSideOut,
)
from sag_api.services.agent_domain import _reject_json_constant
from sag_api.services.knowledge_conflict_service import (
    ConflictListItem,
    ConflictSideSnapshot,
    list_project_conflicts,
    reopen_conflict,
    resolve_conflict,
)

router = APIRouter(prefix="/projects/{project_id}/conflicts", tags=["conflicts"])
web_router = APIRouter(prefix="/conflicts", tags=["conflicts"])

_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_SAFE_RESOLUTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_MAX_BODY_BYTES = 16 * 1024
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_ALLOWED_LIST_QUERY_PARAMS = frozenset(
    {"status", "conflict_subtype", "document_id", "include_inactive", "limit", "offset"}
)
_SINGLE_VALUE_LIST_QUERY_PARAMS = ("document_id", "include_inactive", "limit", "offset")
_WEB_ALLOWED_LIST_QUERY_PARAMS = frozenset(
    {
        "project_id",
        "source_id",
        "status",
        "conflict_subtype",
        "document_id",
        "include_inactive",
        "limit",
        "offset",
    }
)
_WEB_SINGLE_VALUE_LIST_QUERY_PARAMS = (
    "project_id",
    "source_id",
    "document_id",
    "include_inactive",
    "limit",
    "offset",
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


async def require_conflicts_bff_principal(request: Request) -> VerifiedPrincipal:
    """Enforce Continuum BFF service authentication unconditionally (no direct user-JWT bypass)."""
    await require_bff_service_token(request)
    return await require_project_bff_principal(request)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _normalize_audit_history(entries: Sequence[Any] | None) -> list[dict[str, Any]]:
    if not entries:
        return []
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        raw_at = entry.get("at")
        if isinstance(raw_at, datetime):
            entry["at"] = (_as_utc(raw_at) or raw_at).isoformat()
        elif not (isinstance(raw_at, str) and raw_at.strip()):
            raw_ts = entry.get("timestamp")
            if isinstance(raw_ts, datetime):
                entry["at"] = (_as_utc(raw_ts) or raw_ts).isoformat()
            elif isinstance(raw_ts, str) and raw_ts.strip():
                entry["at"] = raw_ts.strip()
        normalized.append(entry)
    return normalized


def _verify_project_and_tenant_scope(
    *,
    project_id: str,
    principal: VerifiedPrincipal,
    x_continuum_tenant_id: str | None,
) -> None:
    if not _SAFE_PROJECT_ID_RE.fullmatch(project_id):
        raise BadRequestError("Invalid project_id format")
    if project_id.lower() == "all":
        raise BadRequestError("Single-project scope is required; 'all' is not allowed")
    if project_id not in principal.allowed_project_ids or not principal.tenant_id:
        raise ForbiddenError(
            f"Principal '{principal.subject}' is not authorized to access project '{project_id}'"
        )
    if x_continuum_tenant_id is not None and x_continuum_tenant_id != principal.tenant_id:
        raise ForbiddenError("X-Continuum-Tenant-Id does not match the verified principal")


def _validate_resolution_id(resolution_id: str) -> None:
    if not _SAFE_RESOLUTION_ID_RE.fullmatch(resolution_id):
        raise BadRequestError("Invalid resolution_id format")


async def _parse_json_body(
    request: Request,
    model_cls: type[_ModelT],
) -> _ModelT:
    raw_bytes = await request.body()
    if len(raw_bytes) > _MAX_BODY_BYTES:
        raise BadRequestError("Request body exceeds the maximum allowed size")
    if not raw_bytes or not raw_bytes.strip():
        raw_obj: Any = {}
    else:
        try:
            raw_obj = json.loads(raw_bytes, parse_constant=_reject_json_constant)
        except ValueError as exc:
            raise BadRequestError("Malformed JSON request body") from exc
    if not isinstance(raw_obj, dict):
        raise BadRequestError("Request body must be a JSON object")
    try:
        return model_cls.model_validate(raw_obj)
    except PydanticValidationError as exc:
        first_msg = (
            exc.errors()[0].get("msg", "Invalid request body")
            if exc.errors()
            else "Invalid request body"
        )
        raise ValidationError(str(first_msg)) from exc


def _to_side_out(side: ConflictSideSnapshot) -> ConflictSideOut:
    return ConflictSideOut(
        document_id=side.document_id,
        document_name=side.document_name,
        version_id=side.version_id,
        version_no=side.version_no,
        unit_id=side.unit_id,
        section_path=side.section_path,
        page_from=side.page_from,
        page_to=side.page_to,
        text_snapshot=side.text_snapshot,
        authority_tier=side.authority_tier,
        effective_from=_as_utc(side.effective_from),
        security_partition_id=side.security_partition_id,
    )


def _to_item_out(item: ConflictListItem) -> ConflictItemOut:
    return ConflictItemOut(
        id=item.id,
        tenant_id=item.tenant_id,
        project_id=item.project_id,
        security_partition_id=item.security_partition_id,
        winner_security_partition_id=item.winner_security_partition_id,
        pair_key=item.pair_key,
        conflict_subtype=ConflictSubtype(item.conflict_subtype),
        status=ConflictResolutionStatus(item.status),
        resolution_mode=ConflictResolutionMode(item.resolution_mode),
        confidence=item.confidence,
        cosine_similarity=item.cosine_similarity,
        effective_valid_to=_as_utc(item.effective_valid_to),
        scope_difference_summary=item.scope_difference_summary,
        reason=item.reason,
        degraded_fallback=item.degraded_fallback,
        graph_edge_id=item.graph_edge_id,
        resolved_by=item.resolved_by,
        resolved_at=_as_utc(item.resolved_at),
        created_at=_as_utc(item.created_at) or datetime.now(UTC),
        updated_at=_as_utc(item.updated_at) or datetime.now(UTC),
        audit_history=_normalize_audit_history(item.audit_history),
        side_a=_to_side_out(item.side_a),
        side_b=_to_side_out(item.side_b),
    )


def _to_action_response(row: KnowledgeConflictResolution) -> ConflictActionResponse:
    return ConflictActionResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        security_partition_id=row.security_partition_id,
        winner_security_partition_id=row.winner_security_partition_id,
        pair_key=row.pair_key,
        conflict_subtype=ConflictSubtype(row.conflict_subtype),
        status=ConflictResolutionStatus(row.status),
        resolution_mode=ConflictResolutionMode(row.resolution_mode),
        winner_document_id=row.winner_document_id,
        winner_version_id=row.winner_version_id,
        winner_unit_id=row.winner_unit_id,
        loser_document_id=row.loser_document_id,
        loser_version_id=row.loser_version_id,
        loser_unit_id=row.loser_unit_id,
        effective_valid_to=_as_utc(row.effective_valid_to),
        reason=row.reason or "",
        degraded_fallback=bool(row.degraded_fallback),
        graph_edge_id=row.graph_edge_id,
        resolved_by=row.resolved_by,
        resolved_at=_as_utc(row.resolved_at),
        created_at=_as_utc(row.created_at) or datetime.now(UTC),
        updated_at=_as_utc(row.updated_at) or datetime.now(UTC),
        audit_history=_normalize_audit_history(row.audit_history_json),
    )


@router.get(
    "",
    response_model=ConflictListResponse,
    summary="List ACL-filtered cross-document conflicts for a project with hydrated side snapshots",
)
async def list_conflicts_endpoint(
    project_id: str,
    request: Request,
    status: list[ConflictResolutionStatus] | None = Query(default=None),
    conflict_subtype: list[ConflictSubtype] | None = Query(default=None),
    document_id: str | None = Query(default=None, min_length=1, max_length=256),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_continuum_tenant_id: str | None = Header(None, alias="X-Continuum-Tenant-Id"),
    principal: VerifiedPrincipal = Depends(require_conflicts_bff_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_project_and_tenant_scope(
        project_id=project_id,
        principal=principal,
        x_continuum_tenant_id=x_continuum_tenant_id,
    )
    unknown_keys = sorted(set(request.query_params.keys()) - _ALLOWED_LIST_QUERY_PARAMS)
    if unknown_keys:
        raise BadRequestError(f"Unsupported query parameters: {', '.join(unknown_keys)}")
    for single_key in _SINGLE_VALUE_LIST_QUERY_PARAMS:
        if len(request.query_params.getlist(single_key)) > 1:
            raise BadRequestError(f"Duplicate {single_key} query parameter is not allowed")

    cleaned_document_id: str | None = None
    if document_id is not None:
        cleaned_document_id = document_id.strip()
        if not cleaned_document_id:
            raise BadRequestError("document_id must not be blank")

    items, total = await list_project_conflicts(
        session,
        tenant_id=principal.tenant_id,
        project_id=project_id,
        allowed_partition_ids=principal.allowed_partition_ids,
        status_filter=[member.value for member in status] if status else None,
        conflict_subtype=[member.value for member in conflict_subtype] if conflict_subtype else None,
        document_id=cleaned_document_id,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    out = ConflictListResponse(
        items=[_to_item_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )


@router.post(
    "/{resolution_id}/resolve",
    response_model=ConflictActionResponse,
    summary="Apply a Human-in-the-Loop resolution decision to a project conflict",
)
async def resolve_conflict_endpoint(
    project_id: str,
    resolution_id: str,
    request: Request,
    x_continuum_tenant_id: str | None = Header(None, alias="X-Continuum-Tenant-Id"),
    principal: VerifiedPrincipal = Depends(require_conflicts_bff_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_project_and_tenant_scope(
        project_id=project_id,
        principal=principal,
        x_continuum_tenant_id=x_continuum_tenant_id,
    )
    _validate_resolution_id(resolution_id)
    if request.query_params:
        unknown = ", ".join(sorted(set(request.query_params.keys())))
        raise BadRequestError(f"Unsupported query parameters: {unknown}")

    payload = await _parse_json_body(request, ConflictResolveRequest)
    try:
        row = await resolve_conflict(
            session,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            allowed_partition_ids=principal.allowed_partition_ids,
            resolution_id=resolution_id,
            action=payload.action,
            reason=payload.reason,
            actor=principal.subject,
        )
    except ForbiddenError as exc:
        # Collapse out-of-partition denials into 404 to prevent partition existence oracles.
        raise NotFoundError(f"Conflict resolution '{resolution_id}' not found") from exc

    out = _to_action_response(row)
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )


@router.post(
    "/{resolution_id}/reopen",
    response_model=ConflictActionResponse,
    summary="Reversibly reopen a resolved conflict back to PENDING_REVIEW",
)
async def reopen_conflict_endpoint(
    project_id: str,
    resolution_id: str,
    request: Request,
    x_continuum_tenant_id: str | None = Header(None, alias="X-Continuum-Tenant-Id"),
    principal: VerifiedPrincipal = Depends(require_conflicts_bff_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_project_and_tenant_scope(
        project_id=project_id,
        principal=principal,
        x_continuum_tenant_id=x_continuum_tenant_id,
    )
    _validate_resolution_id(resolution_id)
    if request.query_params:
        unknown = ", ".join(sorted(set(request.query_params.keys())))
        raise BadRequestError(f"Unsupported query parameters: {unknown}")

    payload = await _parse_json_body(request, ConflictReopenRequest)
    try:
        row = await reopen_conflict(
            session,
            tenant_id=principal.tenant_id,
            project_id=project_id,
            allowed_partition_ids=principal.allowed_partition_ids,
            resolution_id=resolution_id,
            reason=payload.reason,
            actor=principal.subject,
        )
    except ForbiddenError as exc:
        # Collapse out-of-partition denials into 404 to prevent partition existence oracles.
        raise NotFoundError(f"Conflict resolution '{resolution_id}' not found") from exc

    out = _to_action_response(row)
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )


# ---------------------------------------------------------------------------
# Interactive Web Session Router (`/api/v1/conflicts`)
# ---------------------------------------------------------------------------


def _verify_web_principal_scope(principal: VerifiedPrincipal) -> None:
    if (
        not (principal.tenant_id or "").strip()
        or not principal.allowed_project_ids
        or not principal.allowed_partition_ids
    ):
        raise ForbiddenError(
            f"Principal '{principal.subject}' does not have an active tenant/project/partition scope"
        )


@web_router.get(
    "",
    response_model=ConflictListResponse,
    summary="List ACL-filtered cross-document conflicts for the active web session",
)
async def list_web_conflicts_endpoint(
    request: Request,
    project_id: str | None = Query(default=None),
    source_id: str | None = Query(default=None, min_length=1, max_length=128),
    status: list[ConflictResolutionStatus] | None = Query(default=None),
    conflict_subtype: list[ConflictSubtype] | None = Query(default=None),
    document_id: str | None = Query(default=None, min_length=1, max_length=256),
    include_inactive: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_web_principal_scope(principal)

    unknown_keys = sorted(set(request.query_params.keys()) - _WEB_ALLOWED_LIST_QUERY_PARAMS)
    if unknown_keys:
        raise BadRequestError(f"Unsupported query parameters: {', '.join(unknown_keys)}")
    for single_key in _WEB_SINGLE_VALUE_LIST_QUERY_PARAMS:
        if len(request.query_params.getlist(single_key)) > 1:
            raise BadRequestError(f"Duplicate {single_key} query parameter is not allowed")

    if project_id is not None:
        if not _SAFE_PROJECT_ID_RE.fullmatch(project_id):
            raise BadRequestError("Invalid project_id format")
        if project_id.lower() == "all":
            raise BadRequestError("Single-project filter is required; 'all' is not allowed")
        if project_id not in principal.allowed_project_ids:
            raise ForbiddenError(
                f"Principal '{principal.subject}' is not authorized to access project '{project_id}'"
            )
        scoped_project_ids = [project_id]
    else:
        scoped_project_ids = sorted(principal.allowed_project_ids)

    cleaned_source_id: str | None = None
    if source_id is not None:
        cleaned_source_id = source_id.strip()
        if not cleaned_source_id:
            raise BadRequestError("source_id must not be blank")

    cleaned_document_id: str | None = None
    if document_id is not None:
        cleaned_document_id = document_id.strip()
        if not cleaned_document_id:
            raise BadRequestError("document_id must not be blank")

    items, total = await list_project_conflicts(
        session,
        tenant_id=principal.tenant_id,
        project_ids=scoped_project_ids,
        allowed_partition_ids=principal.allowed_partition_ids,
        status_filter=[member.value for member in status] if status else None,
        conflict_subtype=[member.value for member in conflict_subtype] if conflict_subtype else None,
        source_id=cleaned_source_id,
        document_id=cleaned_document_id,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    out = ConflictListResponse(
        items=[_to_item_out(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )


@web_router.post(
    "/{resolution_id}/resolve",
    response_model=ConflictActionResponse,
    summary="Apply a Human-in-the-Loop resolution decision via the active web session",
)
async def resolve_web_conflict_endpoint(
    resolution_id: str,
    request: Request,
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_web_principal_scope(principal)
    _validate_resolution_id(resolution_id)
    if request.query_params:
        unknown = ", ".join(sorted(set(request.query_params.keys())))
        raise BadRequestError(f"Unsupported query parameters: {unknown}")

    payload = await _parse_json_body(request, ConflictResolveRequest)
    try:
        row = await resolve_conflict(
            session,
            tenant_id=principal.tenant_id,
            allowed_project_ids=principal.allowed_project_ids,
            allowed_partition_ids=principal.allowed_partition_ids,
            resolution_id=resolution_id,
            action=payload.action,
            reason=payload.reason,
            actor=principal.subject,
        )
    except ForbiddenError as exc:
        # Collapse out-of-partition/unmapped denials into 404 to prevent existence oracles.
        raise NotFoundError(f"Conflict resolution '{resolution_id}' not found") from exc

    out = _to_action_response(row)
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )


@web_router.post(
    "/{resolution_id}/reopen",
    response_model=ConflictActionResponse,
    summary="Reversibly reopen a resolved conflict via the active web session",
)
async def reopen_web_conflict_endpoint(
    resolution_id: str,
    request: Request,
    _user: User = Depends(get_current_user),
    principal: VerifiedPrincipal = Depends(require_project_read_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    _verify_web_principal_scope(principal)
    _validate_resolution_id(resolution_id)
    if request.query_params:
        unknown = ", ".join(sorted(set(request.query_params.keys())))
        raise BadRequestError(f"Unsupported query parameters: {unknown}")

    payload = await _parse_json_body(request, ConflictReopenRequest)
    try:
        row = await reopen_conflict(
            session,
            tenant_id=principal.tenant_id,
            allowed_project_ids=principal.allowed_project_ids,
            allowed_partition_ids=principal.allowed_partition_ids,
            resolution_id=resolution_id,
            reason=payload.reason,
            actor=principal.subject,
        )
    except ForbiddenError as exc:
        # Collapse out-of-partition/unmapped denials into 404 to prevent existence oracles.
        raise NotFoundError(f"Conflict resolution '{resolution_id}' not found") from exc

    out = _to_action_response(row)
    return JSONResponse(
        status_code=200,
        content=out.model_dump(mode="json"),
        headers=_NO_STORE_HEADERS,
    )
