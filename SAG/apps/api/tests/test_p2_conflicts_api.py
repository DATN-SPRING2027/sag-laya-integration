"""Phase 2 Wave 2.2 (P2-D) TDD tests for Conflict Inbox & HITL Resolution REST API.

Covers:
- Strict Pydantic v2 `extra="forbid"` schemas and bounds
- Fail-closed Continuum BFF auth, project/tenant scope, and path ID validation
- `GET /api/v1/projects/{project_id}/conflicts` hydrated side_a/side_b, filters,
  `logical_source_id` matching, `include_inactive`, pagination, `Cache-Control: no-store`
- Dual-partition ACL isolation with ZERO partition existence oracle (HTTP 404, not 403)
- `POST .../{resolution_id}/resolve` (`WINNER_B`, `WINNER_A` symmetric swap with
  invariant `pair_key`, `KEEP_BOTH_SCOPED`, graph edge sync, audit actor)
- `POST .../{resolution_id}/reopen` (from `ACTIVE` & `DISMISSED_SCOPED`, rejects
  revoked side with HTTP 409)
- Strict input validation (malformed JSON, NaN/Infinity, oversized body, unknown
  fields/query params, invalid enum query values)
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.api.v1 import api_router
from sag_api.api.v1.conflicts import require_conflicts_bff_principal
from sag_api.core.config import settings
from sag_api.core.deps import get_session
from sag_api.core.errors import ApiError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.core.security import create_access_token
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    KnowledgeConflictResolution,
    KnowledgeGraphEdge,
    SearchUnit,
    Source,
    SourceProjectMapping,
)
from sag_api.db.models.conflict_resolution import compute_conflict_pair_key
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
    DocumentStatus,
    SourceStatus,
)
from sag_api.schemas.conflict_resolution import (
    ConflictActionResponse,
    ConflictItemOut,
    ConflictListResponse,
    ConflictReopenRequest,
    ConflictResolveRequest,
    ConflictSideOut,
)

TENANT_ID = "org_conflicts_p2d"
PROJECT_ID = "proj_conflicts_p2d"
OTHER_PROJECT_ID = "proj_other_p2d"
PART_PUBLIC = PROJECT_ID
PART_EXEC = "part_exec_secret"

T_JAN = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
T_MAR = datetime(2025, 3, 15, 0, 0, 0, tzinfo=UTC)
T_JUN = datetime(2025, 6, 15, 0, 0, 0, tzinfo=UTC)
VALID_TO_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bff_headers(
    *,
    token: str = "test-bff-conflicts-secret",
    user_id: str = "reviewer_001",
    org_id: str = TENANT_ID,
    tenant_id: str | None = None,
    project_id: str = PROJECT_ID,
    partition_id: str | None = None,
    allowed_project_ids: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-SAG-BFF-Service-Token": token,
        "X-Continuum-User-Id": user_id,
        "X-Continuum-Organization-Id": org_id,
        "X-Continuum-Project-Id": project_id,
        "X-Continuum-Security-Partition": project_id if partition_id is None else partition_id,
    }
    if tenant_id is not None:
        headers["X-Continuum-Tenant-Id"] = tenant_id
    else:
        headers["X-Continuum-Tenant-Id"] = org_id
    if allowed_project_ids is not None:
        headers["X-Continuum-Allowed-Project-Ids"] = allowed_project_ids
    return headers


def _direct_user_jwt_headers(
    *,
    user_id: str = "reviewer_001",
    org_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> dict[str, str]:
    token = create_access_token(
        user_id,
        extra={
            "auth_mode": "continuum",
            "org_id": org_id,
            "tenant_id": org_id,
            "project_id": project_id,
            "security_partition": project_id,
            "jti": secrets.token_urlsafe(12),
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _make_principal(
    *,
    subject: str = "reviewer_hitl_42",
    tenant_id: str = TENANT_ID,
    allowed_project_ids: frozenset[str] = frozenset({PROJECT_ID}),
    allowed_partition_ids: frozenset[str] = frozenset({PART_PUBLIC}),
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject=subject,
        organization_id=tenant_id,
        allowed_project_ids=allowed_project_ids,
        issuer="continuum-be-bff",
        key_id="kid-p2d",
        token_id="jti-p2d",
        issued_at=1_700_000_000,
        expires_at=1_700_000_900,
        tenant_id=tenant_id,
        allowed_partition_ids=allowed_partition_ids,
    )


async def _seed_doc_unit(
    session: AsyncSession,
    *,
    tag: str,
    text: str,
    filename: str | None = None,
    document_id: str | None = None,
    logical_source_id: str | None = None,
    version_no: int = 1,
    partition_id: str = PART_PUBLIC,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    section_path: str = "Policy > Leave",
    page_from: int = 2,
    page_to: int = 3,
    doc_status: DocumentStatus = DocumentStatus.READY,
    doc_is_active: bool = True,
    ver_status: str = "READY",
    search_status: str = "SEARCH_READY",
) -> dict[str, Any]:
    src_id = f"src-{tag}"
    src = Source(
        id=src_id,
        name=f"Source {tag}",
        sag_source_config_id=f"cfg-{src_id}",
        status=SourceStatus.ACTIVE,
    )
    session.add(src)
    await session.flush()

    spm = SourceProjectMapping(
        id=f"spm-{src_id}",
        source_id=src_id,
        organization_id=TENANT_ID,
        project_id=PROJECT_ID,
        state="CONFIRMED",
        mapping_version=1,
        confirmed_at=T_JAN,
        confirmed_by="admin",
        approval_ref=f"appr-{src_id}",
    )
    session.add(spm)
    await session.flush()

    doc_id = document_id or f"doc-{tag}"
    doc = Document(
        id=doc_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        source_id=src_id,
        logical_source_id=logical_source_id or f"continuum:project_document:{tag}",
        filename=filename or f"{tag}.md",
        storage_path=f"/tmp/{tag}.md",
        status=doc_status,
        is_active=doc_is_active,
    )
    session.add(doc)
    await session.flush()

    ver_id = f"ver-{tag}"
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc.id,
        version_no=version_no,
        file_hash=_sha256(f"file-{tag}-{version_no}"),
        authority_tier=authority_tier,
        effective_from=effective_from,
        observed_at=T_JAN,
        valid_from=T_JAN,
        valid_to=VALID_TO_FOREVER,
        status=ver_status,
        search_status=search_status,
        search_ready_at=T_JAN if search_status == "SEARCH_READY" else None,
        metadata_json={"security_partition_id": partition_id},
    )
    session.add(ver)
    await session.flush()

    blk_id = f"blk-{tag}"
    blk = CanonicalBlock(
        id=blk_id,
        document_version_id=ver.id,
        ordinal=1,
        block_type="paragraph",
        page_from=page_from,
        page_to=page_to,
        section_path=section_path,
        normalized_text=text,
        content_hash=_sha256(text),
    )
    session.add(blk)
    await session.flush()

    unit_id = f"unit-{tag}"
    unit = SearchUnit(
        id=unit_id,
        document_version_id=ver.id,
        block_from_id=blk.id,
        block_to_id=blk.id,
        security_partition_id=partition_id,
        content_hash=_sha256(text),
        token_count=max(1, len(text.split())),
        page_from=page_from,
        page_to=page_to,
        section_path=section_path,
        text_snapshot=text,
    )
    session.add(unit)
    await session.flush()
    return {
        "source": src,
        "doc": doc,
        "ver": ver,
        "unit": unit,
        "document_id": doc.id,
        "logical_source_id": doc.logical_source_id,
        "version_id": ver.id,
        "unit_id": unit.id,
        "content_hash": unit.content_hash,
        "partition_id": partition_id,
    }


async def _seed_conflict_row(
    session: AsyncSession,
    *,
    resolution_id: str,
    side_a: dict[str, Any],
    side_b: dict[str, Any],
    subtype: ConflictSubtype = ConflictSubtype.HARD_CONTRADICTION,
    status: ConflictResolutionStatus = ConflictResolutionStatus.PENDING_REVIEW,
    mode: ConflictResolutionMode = ConflictResolutionMode.UNRESOLVED,
    confidence: float = 0.87,
    cosine_similarity: float | None = 0.91,
    effective_valid_to: datetime | None = None,
    scope_difference_summary: str | None = None,
    reason: str = "Conflicting policy values detected.",
    degraded_fallback: bool = False,
    with_contradicts_edge: bool = True,
    with_supersedes_edge: bool = False,
    updated_at: datetime = T_JUN,
) -> KnowledgeConflictResolution:
    graph_edge_id: str | None = None
    if with_contradicts_edge:
        con_edge = KnowledgeGraphEdge(
            id=f"edge-con-{resolution_id}",
            project_id=PROJECT_ID,
            source_unit_id=side_b["unit_id"],
            target_unit_id=side_a["unit_id"],
            edge_type="CONTRADICTS",
            weight=confidence,
            calibrated_weight=confidence,
            metadata_json={
                "is_candidate": status == ConflictResolutionStatus.PENDING_REVIEW,
                "conflict_subtype": subtype.value,
                "resolution_status": status.value,
                "resolution_id": resolution_id,
            },
        )
        session.add(con_edge)
        await session.flush()
        graph_edge_id = con_edge.id

    if with_supersedes_edge:
        sup_edge = KnowledgeGraphEdge(
            id=f"edge-sup-{resolution_id}",
            project_id=PROJECT_ID,
            source_unit_id=side_b["unit_id"],
            target_unit_id=side_a["unit_id"],
            edge_type="SUPERSEDES",
            weight=1.0,
            calibrated_weight=1.0,
            metadata_json={
                "is_candidate": False,
                "conflict_subtype": subtype.value,
                "resolution_id": resolution_id,
            },
        )
        session.add(sup_edge)
        await session.flush()
        if graph_edge_id is None:
            graph_edge_id = sup_edge.id

    row = KnowledgeConflictResolution(
        id=resolution_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        security_partition_id=side_a["partition_id"],
        winner_security_partition_id=side_b["partition_id"],
        pair_key=compute_conflict_pair_key(
            side_b["version_id"],
            side_b["unit_id"],
            side_a["version_id"],
            side_a["unit_id"],
        ),
        winner_document_id=side_b["document_id"],
        winner_version_id=side_b["version_id"],
        winner_unit_id=side_b["unit_id"],
        winner_content_hash=side_b["content_hash"],
        loser_document_id=side_a["document_id"],
        loser_version_id=side_a["version_id"],
        loser_unit_id=side_a["unit_id"],
        loser_content_hash=side_a["content_hash"],
        conflict_subtype=subtype.value,
        status=status.value,
        resolution_mode=mode.value,
        confidence=confidence,
        cosine_similarity=cosine_similarity,
        effective_valid_to=effective_valid_to,
        scope_difference_summary=scope_difference_summary,
        reason=reason,
        degraded_fallback=degraded_fallback,
        graph_edge_id=graph_edge_id,
        audit_history_json=[
            {
                "action": "DETECTED",
                "actor": "system",
                "reason": reason,
                "from_status": None,
                "to_status": status.value,
                "timestamp": updated_at.isoformat(),
            }
        ],
        created_at=T_MAR,
        updated_at=updated_at,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def conflicts_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-conflicts-secret")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(conn, _rec) -> None:  # noqa: ANN001
        conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_err(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)

    async def _override_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sag.test") as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            session_factory=session_factory,
        )

    await engine.dispose()


# ---------------------------------------------------------------------------
# 1. Schema Strictness (`extra="forbid"` + bounds)
# ---------------------------------------------------------------------------


def test_conflicts_schema_extra_forbid_and_bounds() -> None:
    with pytest.raises(PydanticValidationError):
        ConflictSideOut(
            document_id="d1",
            document_name="a.md",
            version_id="v1",
            unit_id="u1",
            security_partition_id="p1",
            unexpected="boom",  # type: ignore[call-arg]
        )

    with pytest.raises(PydanticValidationError):
        ConflictResolveRequest(action="WINNER_B", extra_key="nope")  # type: ignore[call-arg]

    with pytest.raises(PydanticValidationError):
        ConflictResolveRequest(action="INVALID_ACTION")  # type: ignore[arg-type]

    with pytest.raises(PydanticValidationError):
        ConflictResolveRequest(action="WINNER_B", reason="x" * 2001)

    with pytest.raises(PydanticValidationError):
        ConflictReopenRequest(reason="ok", extra_key=1)  # type: ignore[call-arg]

    with pytest.raises(PydanticValidationError):
        ConflictReopenRequest(reason="y" * 2001)

    side = ConflictSideOut(
        document_id="d1",
        document_name="a.md",
        version_id="v1",
        unit_id="u1",
        security_partition_id="p1",
    )
    with pytest.raises(PydanticValidationError):
        ConflictItemOut(
            id="r1",
            tenant_id="t1",
            project_id="p1",
            security_partition_id="p1",
            winner_security_partition_id="p1",
            pair_key="k1",
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
            resolution_mode=ConflictResolutionMode.UNRESOLVED,
            confidence=1.5,
            created_at=T_JAN,
            updated_at=T_JAN,
            side_a=side,
            side_b=side,
        )

    with pytest.raises(PydanticValidationError):
        ConflictListResponse(items=[], total=0, limit=201, offset=0)

    with pytest.raises(PydanticValidationError):
        ConflictListResponse(items=[], total=0, limit=50, offset=-1, extra_field=True)  # type: ignore[call-arg]

    with pytest.raises(PydanticValidationError):
        ConflictActionResponse(
            id="r1",
            tenant_id="t1",
            project_id="p1",
            security_partition_id="p1",
            winner_security_partition_id="p1",
            pair_key="k1",
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            status=ConflictResolutionStatus.ACTIVE,
            resolution_mode=ConflictResolutionMode.MANUAL_HITL,
            winner_document_id="d2",
            winner_version_id="v2",
            winner_unit_id="u2",
            loser_document_id="d1",
            loser_version_id="v1",
            loser_unit_id="u1",
            created_at=T_JAN,
            updated_at=T_JAN,
            unknown_field="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# 2. Fail-Closed Continuum BFF Auth, Tenant/Project Scope & Path Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflicts_fail_closed_bff_auth_and_scope_validation(conflicts_env) -> None:
    client: httpx.AsyncClient = conflicts_env.client
    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"

    # Missing BFF service token -> 403 on all 3 endpoints
    assert (await client.get(base)).status_code == 403
    assert (await client.post(f"{base}/res-1/resolve", json={"action": "WINNER_B"})).status_code == 403
    assert (await client.post(f"{base}/res-1/reopen", json={})).status_code == 403

    # Wrong BFF service token -> 403
    assert (await client.get(base, headers=_bff_headers(token="wrong-secret"))).status_code == 403

    # Direct user Bearer JWT without BFF service token -> 403 (zero bypass!)
    assert (await client.get(base, headers=_direct_user_jwt_headers())).status_code == 403

    # Unauthorized project_id (BFF header mismatch) -> 403
    assert (await client.get(base, headers=_bff_headers(project_id=OTHER_PROJECT_ID))).status_code == 403

    # Mismatched X-Continuum-Tenant-Id -> 403
    assert (await client.get(base, headers=_bff_headers(tenant_id="org_evil"))).status_code == 403
    assert (await client.get(base, headers=_bff_headers(tenant_id=""))).status_code == 403

    # "all" multi-project scope explicitly rejected with 400 (single-project only)
    all_headers = _bff_headers(
        project_id="all",
        partition_id="all",
        allowed_project_ids=f"{PROJECT_ID},{OTHER_PROJECT_ID}",
    )
    assert (await client.get("/api/v1/projects/all/conflicts", headers=all_headers)).status_code == 400

    # Invalid project_id format (even if BFF headers mirror it) -> 400
    bad_pid = "bad!project"
    assert (
        await client.get(
            f"/api/v1/projects/{bad_pid}/conflicts",
            headers=_bff_headers(project_id=bad_pid, partition_id=bad_pid),
        )
    ).status_code == 400

    # Invalid resolution_id format -> 400
    valid_headers = _bff_headers()
    assert (
        await client.post(f"{base}/bad!res/resolve", headers=valid_headers, json={"action": "WINNER_B"})
    ).status_code == 400
    assert (
        await client.post(f"{base}/{'a' * 65}/reopen", headers=valid_headers, json={})
    ).status_code == 400

    # Dependency override check: principal without project membership or empty tenant -> 403
    conflicts_env.app.dependency_overrides[require_conflicts_bff_principal] = lambda: _make_principal(
        allowed_project_ids=frozenset({OTHER_PROJECT_ID})
    )
    try:
        assert (await client.get(base)).status_code == 403
    finally:
        conflicts_env.app.dependency_overrides.pop(require_conflicts_bff_principal, None)

    conflicts_env.app.dependency_overrides[require_conflicts_bff_principal] = lambda: _make_principal(
        tenant_id=""
    )
    try:
        assert (await client.get(base)).status_code == 403
    finally:
        conflicts_env.app.dependency_overrides.pop(require_conflicts_bff_principal, None)


# ---------------------------------------------------------------------------
# 3. GET Conflict Inbox: Hydration, Filters, logical_source_id, include_inactive, Pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_conflicts_hydrated_fields_filters_pagination_and_no_store(conflicts_env) -> None:
    async with conflicts_env.session_factory() as s:
        doc_a = await _seed_doc_unit(
            s,
            tag="list-a",
            filename="handbook-2024.md",
            logical_source_id="continuum:project_document:logical-doc-a",
            text="Annual leave quota is 12 days per year under 2024 handbook.",
            version_no=1,
            authority_tier=3,
            effective_from=T_JAN,
            section_path="HR > Leave Policy",
            page_from=4,
            page_to=5,
        )
        doc_b = await _seed_doc_unit(
            s,
            tag="list-b",
            filename="policy-2025.md",
            logical_source_id="continuum:project_document:logical-doc-b",
            text="Annual leave quota is 18 days per year effective June 2025.",
            version_no=2,
            authority_tier=1,
            effective_from=T_JUN,
            section_path="Executive Policy > Annual Leave",
            page_from=1,
            page_to=2,
        )
        doc_c = await _seed_doc_unit(
            s,
            tag="list-c",
            filename="finance-eu.md",
            logical_source_id="continuum:project_document:logical-doc-c",
            text="EU subsidiary travel cap is 500 EUR per trip.",
            version_no=1,
            authority_tier=2,
            effective_from=T_MAR,
            section_path="Finance > Travel",
            page_from=7,
            page_to=7,
        )
        doc_revoked = await _seed_doc_unit(
            s,
            tag="list-rev",
            filename="recalled-memo.md",
            text="Recalled draft travel cap is 100 EUR per trip.",
            ver_status="REVOKED",
            search_status="REVOKED",
        )

        await _seed_conflict_row(
            s,
            resolution_id="res-list-1",
            side_a=doc_a,
            side_b=doc_b,
            subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            status=ConflictResolutionStatus.ACTIVE,
            mode=ConflictResolutionMode.AUTO_POLICY,
            confidence=0.94,
            cosine_similarity=0.89,
            effective_valid_to=T_JUN,
            reason="2025 policy directly supersedes 2024 handbook.",
            updated_at=datetime(2025, 6, 20, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-list-2",
            side_a=doc_a,
            side_b=doc_c,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
            mode=ConflictResolutionMode.UNRESOLVED,
            confidence=0.82,
            cosine_similarity=0.84,
            scope_difference_summary="Different department rules",
            reason="Incompatible travel/leave clauses.",
            degraded_fallback=True,
            updated_at=datetime(2025, 6, 21, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-list-revoked",
            side_a=doc_c,
            side_b=doc_revoked,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.REVOKED_WINNER,
            mode=ConflictResolutionMode.UNRESOLVED,
            confidence=0.75,
            updated_at=datetime(2025, 6, 22, 0, 0, 0, tzinfo=UTC),
        )
        await s.commit()

    headers = _bff_headers()
    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"

    # Default GET (include_inactive=False) -> returns 2 active-doc conflicts ordered by updated_at DESC
    resp = await conflicts_env.client.get(base, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == ["res-list-2", "res-list-1"]

    # Validate full hydrated side_a & side_b on res-list-1
    item1 = body["items"][1]
    assert item1["conflict_subtype"] == "DIRECT_SUPERSEDES"
    assert item1["status"] == "ACTIVE"
    assert item1["resolution_mode"] == "AUTO_POLICY"
    assert item1["confidence"] == pytest.approx(0.94)
    assert item1["cosine_similarity"] == pytest.approx(0.89)
    assert item1["side_a"]["document_id"] == doc_a["document_id"]
    assert item1["side_a"]["document_name"] == "handbook-2024.md"
    assert item1["side_a"]["version_id"] == doc_a["version_id"]
    assert item1["side_a"]["version_no"] == 1
    assert item1["side_a"]["unit_id"] == doc_a["unit_id"]
    assert item1["side_a"]["section_path"] == "HR > Leave Policy"
    assert item1["side_a"]["page_from"] == 4
    assert item1["side_a"]["page_to"] == 5
    assert "12 days" in item1["side_a"]["text_snapshot"]
    assert item1["side_a"]["authority_tier"] == 3
    assert item1["side_a"]["effective_from"] == T_JAN.isoformat().replace("+00:00", "Z")
    assert item1["side_a"]["security_partition_id"] == PART_PUBLIC

    assert item1["side_b"]["document_id"] == doc_b["document_id"]
    assert item1["side_b"]["document_name"] == "policy-2025.md"
    assert item1["side_b"]["version_no"] == 2
    assert item1["side_b"]["authority_tier"] == 1
    assert "18 days" in item1["side_b"]["text_snapshot"]

    # Status filter
    resp_pending = await conflicts_env.client.get(
        f"{base}?status=PENDING_REVIEW", headers=headers
    )
    assert resp_pending.status_code == 200
    assert resp_pending.json()["total"] == 1
    assert resp_pending.json()["items"][0]["id"] == "res-list-2"
    assert resp_pending.json()["items"][0]["degraded_fallback"] is True

    # Subtype filter
    resp_sub = await conflicts_env.client.get(
        f"{base}?conflict_subtype=DIRECT_SUPERSEDES", headers=headers
    )
    assert resp_sub.status_code == 200
    assert [i["id"] for i in resp_sub.json()["items"]] == ["res-list-1"]

    # document_id filter matching Document.id
    resp_doc_id = await conflicts_env.client.get(
        f"{base}?document_id={doc_b['document_id']}", headers=headers
    )
    assert resp_doc_id.status_code == 200
    assert [i["id"] for i in resp_doc_id.json()["items"]] == ["res-list-1"]

    # document_id filter matching logical_source_id
    resp_logical = await conflicts_env.client.get(
        f"{base}?document_id={doc_c['logical_source_id']}", headers=headers
    )
    assert resp_logical.status_code == 200
    assert [i["id"] for i in resp_logical.json()["items"]] == ["res-list-2"]

    # include_inactive=True surfaces revoked conflict
    resp_inc = await conflicts_env.client.get(
        f"{base}?include_inactive=true", headers=headers
    )
    assert resp_inc.status_code == 200
    assert resp_inc.json()["total"] == 3
    assert [i["id"] for i in resp_inc.json()["items"]] == [
        "res-list-revoked",
        "res-list-2",
        "res-list-1",
    ]

    # Pagination (limit=1, offset=1) preserves total=3
    resp_page = await conflicts_env.client.get(
        f"{base}?include_inactive=true&limit=1&offset=1", headers=headers
    )
    assert resp_page.status_code == 200
    page_body = resp_page.json()
    assert page_body["total"] == 3
    assert page_body["limit"] == 1
    assert page_body["offset"] == 1
    assert [i["id"] for i in page_body["items"]] == ["res-list-2"]


# ---------------------------------------------------------------------------
# 4. Dual-Partition ACL Isolation & ZERO Partition Existence Oracle (404, not 403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_partition_acl_zero_existence_oracle(conflicts_env) -> None:
    async with conflicts_env.session_factory() as s:
        pub_1 = await _seed_doc_unit(
            s,
            tag="acl-pub-1",
            text="Public baseline policy states 5 working days notice.",
            partition_id=PART_PUBLIC,
            authority_tier=3,
            effective_from=T_JAN,
        )
        pub_2 = await _seed_doc_unit(
            s,
            tag="acl-pub-2",
            text="Public updated policy states 10 working days notice.",
            partition_id=PART_PUBLIC,
            authority_tier=2,
            effective_from=T_MAR,
        )
        exec_doc = await _seed_doc_unit(
            s,
            tag="acl-exec-1",
            text="Executive confidential rule overrides notice period to 30 days.",
            partition_id=PART_EXEC,
            authority_tier=1,
            effective_from=T_JUN,
        )
        # Live-partition drift check: conflict row claims PART_PUBLIC, live version moved to PART_EXEC
        drifted_doc = await _seed_doc_unit(
            s,
            tag="acl-drift-1",
            text="Policy reclassified into executive partition.",
            partition_id=PART_EXEC,
            authority_tier=1,
            effective_from=T_JUN,
        )

        await _seed_conflict_row(
            s,
            resolution_id="res-pub-only",
            side_a=pub_1,
            side_b=pub_2,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-cross-exec",
            side_a=pub_1,
            side_b=exec_doc,
            subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        drifted_row = await _seed_conflict_row(
            s,
            resolution_id="res-live-drifted",
            side_a=pub_2,
            side_b=drifted_doc,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        # Simulate stale stored partition column while live DocumentVersion is PART_EXEC
        drifted_row.winner_security_partition_id = PART_PUBLIC
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    missing_id = "00000000-0000-4000-8000-000000000000"

    # Principal with ONLY {PART_PUBLIC}
    public_principal = _make_principal(
        subject="user_public_only",
        allowed_partition_ids=frozenset({PART_PUBLIC}),
    )
    conflicts_env.app.dependency_overrides[require_conflicts_bff_principal] = lambda: public_principal
    try:
        # GET hides BOTH res-cross-exec and res-live-drifted
        r_list = await conflicts_env.client.get(base)
        assert r_list.status_code == 200
        assert r_list.json()["total"] == 1
        assert [i["id"] for i in r_list.json()["items"]] == ["res-pub-only"]

        # POST /resolve on out-of-partition ID MUST return 404 (identical to nonexistent ID!)
        r_missing_resolve = await conflicts_env.client.post(
            f"{base}/{missing_id}/resolve",
            json={"action": "WINNER_B", "reason": "probe"},
        )
        r_exec_resolve = await conflicts_env.client.post(
            f"{base}/res-cross-exec/resolve",
            json={"action": "WINNER_B", "reason": "probe"},
        )
        r_drift_resolve = await conflicts_env.client.post(
            f"{base}/res-live-drifted/resolve",
            json={"action": "WINNER_B", "reason": "probe"},
        )
        assert r_missing_resolve.status_code == 404
        assert r_exec_resolve.status_code == 404
        assert r_drift_resolve.status_code == 404
        assert r_exec_resolve.json()["error"]["code"] == r_missing_resolve.json()["error"]["code"]
        assert r_exec_resolve.json()["error"]["message"] == "Conflict resolution 'res-cross-exec' not found"
        assert r_drift_resolve.json()["error"]["message"] == "Conflict resolution 'res-live-drifted' not found"

        # POST /reopen on out-of-partition ID MUST ALSO return 404 (not 403!)
        r_missing_reopen = await conflicts_env.client.post(
            f"{base}/{missing_id}/reopen",
            json={"reason": "probe"},
        )
        r_exec_reopen = await conflicts_env.client.post(
            f"{base}/res-cross-exec/reopen",
            json={"reason": "probe"},
        )
        assert r_missing_reopen.status_code == 404
        assert r_exec_reopen.status_code == 404
        assert r_exec_reopen.json()["error"]["code"] == r_missing_reopen.json()["error"]["code"]
        assert r_exec_reopen.json()["error"]["message"] == "Conflict resolution 'res-cross-exec' not found"
    finally:
        conflicts_env.app.dependency_overrides.pop(require_conflicts_bff_principal, None)

    # Principal with {PART_PUBLIC, PART_EXEC} sees and can resolve + reopen res-cross-exec
    dual_principal = _make_principal(
        subject="exec_reviewer_01",
        allowed_partition_ids=frozenset({PART_PUBLIC, PART_EXEC}),
    )
    conflicts_env.app.dependency_overrides[require_conflicts_bff_principal] = lambda: dual_principal
    try:
        r_list_dual = await conflicts_env.client.get(base)
        assert r_list_dual.status_code == 200
        assert r_list_dual.json()["total"] == 3
        ids = {i["id"] for i in r_list_dual.json()["items"]}
        assert ids == {"res-pub-only", "res-cross-exec", "res-live-drifted"}

        r_res_ok = await conflicts_env.client.post(
            f"{base}/res-cross-exec/resolve",
            json={"action": "WINNER_B", "reason": "Executive policy confirmed"},
        )
        assert r_res_ok.status_code == 200, r_res_ok.text
        assert r_res_ok.json()["status"] == "ACTIVE"
        assert r_res_ok.json()["resolved_by"] == "exec_reviewer_01"

        r_reopen_ok = await conflicts_env.client.post(
            f"{base}/res-cross-exec/reopen",
            json={"reason": "Re-review requested"},
        )
        assert r_reopen_ok.status_code == 200, r_reopen_ok.text
        assert r_reopen_ok.json()["status"] == "PENDING_REVIEW"
    finally:
        conflicts_env.app.dependency_overrides.pop(require_conflicts_bff_principal, None)


# ---------------------------------------------------------------------------
# 5. POST /resolve: WINNER_B, WINNER_A (Symmetric Swap), KEEP_BOTH_SCOPED, 409 Revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_conflict_winner_b_winner_a_swap_and_keep_both_scoped(conflicts_env) -> None:
    async with conflicts_env.session_factory() as s:
        doc_a = await _seed_doc_unit(
            s,
            tag="res-a",
            text="Expense limit without approval is 5M VND.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        doc_b = await _seed_doc_unit(
            s,
            tag="res-b",
            text="Expense limit without approval is 10M VND.",
            authority_tier=2,
            effective_from=T_MAR,
        )
        doc_revoked = await _seed_doc_unit(
            s,
            tag="res-rev",
            text="Revoked draft expense policy 50M VND.",
            ver_status="REVOKED",
            search_status="REVOKED",
        )
        row = await _seed_conflict_row(
            s,
            resolution_id="res-hitl-flow",
            side_a=doc_a,
            side_b=doc_b,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        orig_pair_key = row.pair_key
        await _seed_conflict_row(
            s,
            resolution_id="res-revoked-winner",
            side_a=doc_a,
            side_b=doc_revoked,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    headers = _bff_headers(user_id="hitl_auditor_7")

    # 1. WINNER_B
    r_wb = await conflicts_env.client.post(
        f"{base}/res-hitl-flow/resolve",
        headers=headers,
        json={"action": "WINNER_B", "reason": "Adopt 10M VND policy"},
    )
    assert r_wb.status_code == 200, r_wb.text
    assert r_wb.headers.get("cache-control") == "no-store"
    wb = r_wb.json()
    assert wb["status"] == "ACTIVE"
    assert wb["resolution_mode"] == "MANUAL_HITL"
    assert wb["winner_document_id"] == doc_b["document_id"]
    assert wb["loser_document_id"] == doc_a["document_id"]
    assert wb["pair_key"] == orig_pair_key
    assert wb["resolved_by"] == "hitl_auditor_7"
    assert wb["resolved_at"] is not None
    assert wb["effective_valid_to"] is not None
    assert wb["reason"] == "Adopt 10M VND policy"
    assert wb["audit_history"][-1]["action"] == "WINNER_B"
    assert wb["audit_history"][-1]["actor"] == "hitl_auditor_7"

    async with conflicts_env.session_factory() as s:
        sup_edges = (
            await s.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert len(sup_edges) == 1
        assert sup_edges[0].source_unit_id == doc_b["unit_id"]
        assert sup_edges[0].target_unit_id == doc_a["unit_id"]
        con_edge = await s.get(KnowledgeGraphEdge, "edge-con-res-hitl-flow")
        assert con_edge is not None
        assert con_edge.metadata_json["is_candidate"] is False

    # 2. WINNER_A -> symmetric swap, pair_key stays invariant!
    r_wa = await conflicts_env.client.post(
        f"{base}/res-hitl-flow/resolve",
        headers=_bff_headers(user_id="hitl_auditor_8"),
        json={"action": "WINNER_A", "reason": "Revert to 5M VND baseline"},
    )
    assert r_wa.status_code == 200, r_wa.text
    assert r_wa.headers.get("cache-control") == "no-store"
    wa = r_wa.json()
    assert wa["status"] == "ACTIVE"
    assert wa["resolution_mode"] == "MANUAL_HITL"
    assert wa["pair_key"] == orig_pair_key
    assert wa["winner_document_id"] == doc_a["document_id"]
    assert wa["winner_version_id"] == doc_a["version_id"]
    assert wa["winner_unit_id"] == doc_a["unit_id"]
    assert wa["loser_document_id"] == doc_b["document_id"]
    assert wa["loser_version_id"] == doc_b["version_id"]
    assert wa["loser_unit_id"] == doc_b["unit_id"]
    assert wa["resolved_by"] == "hitl_auditor_8"
    assert wa["audit_history"][-1]["action"] == "WINNER_A"
    assert wa["audit_history"][-1]["actor"] == "hitl_auditor_8"

    # 3. KEEP_BOTH_SCOPED -> DISMISSED_SCOPED, SUPERSEDES deleted, CONTRADICTS is_candidate=False
    r_kb = await conflicts_env.client.post(
        f"{base}/res-hitl-flow/resolve",
        headers=_bff_headers(user_id="hitl_auditor_9"),
        json={"action": "KEEP_BOTH_SCOPED", "reason": "Applies to distinct departments"},
    )
    assert r_kb.status_code == 200, r_kb.text
    assert r_kb.headers.get("cache-control") == "no-store"
    kb = r_kb.json()
    assert kb["status"] == "DISMISSED_SCOPED"
    assert kb["resolution_mode"] == "KEEP_BOTH_SCOPED"
    assert kb["effective_valid_to"] is None
    assert kb["resolved_by"] == "hitl_auditor_9"
    assert kb["audit_history"][-1]["action"] == "KEEP_BOTH_SCOPED"
    assert kb["audit_history"][-1]["actor"] == "hitl_auditor_9"

    async with conflicts_env.session_factory() as s:
        sup_after = (
            await s.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert sup_after == []
        con_edge = await s.get(KnowledgeGraphEdge, "edge-con-res-hitl-flow")
        assert con_edge is not None
        assert con_edge.metadata_json["is_candidate"] is False

    # 4. Choosing a REVOKED winner version -> 409 Conflict
    r_rev = await conflicts_env.client.post(
        f"{base}/res-revoked-winner/resolve",
        headers=headers,
        json={"action": "WINNER_B", "reason": "try revoked"},
    )
    assert r_rev.status_code == 409


# ---------------------------------------------------------------------------
# 6. POST /reopen: Reversible from ACTIVE & DISMISSED_SCOPED, 409 on Revoked Side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reopen_conflict_from_active_and_dismissed_and_rejects_revoked(conflicts_env) -> None:
    async with conflicts_env.session_factory() as s:
        d1 = await _seed_doc_unit(
            s, tag="reopen-1", text="SLA response window is 4 hours.", authority_tier=2, effective_from=T_JAN
        )
        d2 = await _seed_doc_unit(
            s, tag="reopen-2", text="SLA response window is 2 hours.", authority_tier=1, effective_from=T_MAR
        )
        d3 = await _seed_doc_unit(
            s, tag="reopen-3", text="Enterprise SLA response window is 1 hour.", authority_tier=1, effective_from=T_JUN
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-reopen-active",
            side_a=d1,
            side_b=d2,
            subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            status=ConflictResolutionStatus.ACTIVE,
            mode=ConflictResolutionMode.MANUAL_HITL,
            effective_valid_to=T_MAR,
            with_contradicts_edge=True,
            with_supersedes_edge=True,
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-reopen-dismissed",
            side_a=d1,
            side_b=d3,
            subtype=ConflictSubtype.SCOPED_EXCEPTION,
            status=ConflictResolutionStatus.DISMISSED_SCOPED,
            mode=ConflictResolutionMode.KEEP_BOTH_SCOPED,
            with_contradicts_edge=True,
            with_supersedes_edge=False,
        )
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    headers = _bff_headers(user_id="reopener_alice")

    # 1. Reopen ACTIVE with explicit reason
    r1 = await conflicts_env.client.post(
        f"{base}/res-reopen-active/reopen",
        headers=headers,
        json={"reason": "Legal requested fresh review"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.headers.get("cache-control") == "no-store"
    b1 = r1.json()
    assert b1["status"] == "PENDING_REVIEW"
    assert b1["resolution_mode"] == "UNRESOLVED"
    assert b1["effective_valid_to"] is None
    assert b1["resolved_by"] == "reopener_alice"
    assert b1["audit_history"][-1]["action"] == "REOPEN"
    assert b1["audit_history"][-1]["actor"] == "reopener_alice"

    async with conflicts_env.session_factory() as s:
        assert await s.get(KnowledgeGraphEdge, "edge-sup-res-reopen-active") is None
        con1 = await s.get(KnowledgeGraphEdge, "edge-con-res-reopen-active")
        assert con1 is not None
        assert con1.metadata_json["is_candidate"] is True

    # 2. Reopen DISMISSED_SCOPED with empty body (reason defaults to "")
    r2 = await conflicts_env.client.post(
        f"{base}/res-reopen-dismissed/reopen",
        headers=headers,
        content=b"",
    )
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("cache-control") == "no-store"
    assert r2.json()["status"] == "PENDING_REVIEW"
    assert r2.json()["resolution_mode"] == "UNRESOLVED"

    async with conflicts_env.session_factory() as s:
        con2 = await s.get(KnowledgeGraphEdge, "edge-con-res-reopen-dismissed")
        assert con2 is not None
        assert con2.metadata_json["is_candidate"] is True

        # Revoke d2 version -> reopening MUST fail with 409 Conflict
        ver2 = await s.get(DocumentVersion, d2["version_id"])
        assert ver2 is not None
        ver2.status = "REVOKED"
        await s.commit()

    r3 = await conflicts_env.client.post(
        f"{base}/res-reopen-active/reopen",
        headers=headers,
        json={"reason": "cannot reopen revoked"},
    )
    assert r3.status_code == 409


# ---------------------------------------------------------------------------
# 7. Strict Input Validation: extra="forbid", Malformed JSON, NaN, Enums, Oversized Body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_input_validation_extra_forbid_malformed_json_nan_enums_and_body_limit(
    conflicts_env,
) -> None:
    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    headers = _bff_headers()

    # Unknown extra keys on POST bodies -> 422
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers=headers,
            json={"action": "WINNER_B", "actor": "spoofed"},
        )
    ).status_code == 422
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/reopen",
            headers=headers,
            json={"reason": "ok", "force": True},
        )
    ).status_code == 422

    # Missing / invalid action -> 422
    assert (
        await conflicts_env.client.post(f"{base}/res-any/resolve", headers=headers, json={})
    ).status_code == 422
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve", headers=headers, json={"action": "DROP_BOTH"}
        )
    ).status_code == 422

    # Reason > 2000 chars -> 422
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers=headers,
            json={"action": "WINNER_B", "reason": "a" * 2001},
        )
    ).status_code == 422

    # Malformed JSON -> 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=b"{broken json",
        )
    ).status_code == 400

    # JSON NaN / Infinity constant rejected -> 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=b'{"action": "WINNER_B", "reason": NaN}',
        )
    ).status_code == 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/reopen",
            headers={**headers, "Content-Type": "application/json"},
            content=b'{"reason": Infinity}',
        )
    ).status_code == 400

    # Non-object JSON -> 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers=headers,
            json=["WINNER_B"],
        )
    ).status_code == 400

    # Oversized body (> 16 KiB) -> 400
    oversized = b'{"action":"WINNER_B","reason":"' + (b"x" * (17 * 1024)) + b'"}'
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=oversized,
        )
    ).status_code == 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/reopen",
            headers={**headers, "Content-Type": "application/json"},
            content=oversized,
        )
    ).status_code == 400

    # Invalid query enum values & out-of-range pagination on GET -> 422
    assert (await conflicts_env.client.get(f"{base}?status=NOT_A_REAL_STATUS", headers=headers)).status_code == 422
    assert (await conflicts_env.client.get(f"{base}?conflict_subtype=BOGUS", headers=headers)).status_code == 422
    assert (await conflicts_env.client.get(f"{base}?limit=0", headers=headers)).status_code == 422
    assert (await conflicts_env.client.get(f"{base}?limit=201", headers=headers)).status_code == 422
    assert (await conflicts_env.client.get(f"{base}?offset=-1", headers=headers)).status_code == 422

    # Unknown query parameters & blank document_id -> 400
    assert (await conflicts_env.client.get(f"{base}?debug=1", headers=headers)).status_code == 400
    assert (await conflicts_env.client.get(f"{base}?document_id=%20%20", headers=headers)).status_code == 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/resolve?debug=1", headers=headers, json={"action": "WINNER_B"}
        )
    ).status_code == 400
    assert (
        await conflicts_env.client.post(
            f"{base}/res-any/reopen?debug=1", headers=headers, json={}
        )
    ).status_code == 400


# ---------------------------------------------------------------------------
# 8. Triad Round 2 Hardening Regressions (Fixes 1, 2, 12b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_inactive_enforces_live_partition_acl_confirmed_spm_and_non_empty_meta(
    conflicts_env,
) -> None:
    """Fix 1: GET /conflicts?include_inactive=true MUST enforce live partition ACL,
    CONFIRMED SourceProjectMapping, and non-empty live partition metadata on both sides,
    while still surfacing caller-authorized revoked rows."""
    async with conflicts_env.session_factory() as s:
        pub_base = await _seed_doc_unit(
            s,
            tag="inc-pub-base",
            text="Public baseline security guideline.",
            partition_id=PART_PUBLIC,
        )
        # 1. Live-drifted executive doc (stored conflict claims PART_PUBLIC, live version moved to PART_EXEC)
        drifted_exec = await _seed_doc_unit(
            s,
            tag="inc-drift-exec",
            text="SECRET EXECUTIVE COMPENSATION BOARD MINUTES.",
            partition_id=PART_EXEC,
        )
        # 2. Revoked SourceProjectMapping doc
        revoked_spm_doc = await _seed_doc_unit(
            s,
            tag="inc-rev-spm",
            text="Unmapped source confidential memo.",
            partition_id=PART_PUBLIC,
        )
        spm_rev = await s.get(SourceProjectMapping, f"spm-{revoked_spm_doc['source'].id}")
        assert spm_rev is not None
        spm_rev.state = "REVOKED"
        spm_rev.revoked_at = T_JUN
        spm_rev.revoked_by = "admin"
        spm_rev.revocation_ref = "rev-spm-1"
        # 3. Missing live security_partition_id metadata
        empty_meta_doc = await _seed_doc_unit(
            s,
            tag="inc-empty-meta",
            text="Version missing security partition metadata.",
            partition_id=PART_PUBLIC,
        )
        empty_meta_doc["ver"].metadata_json = {}
        # 4. Legitimate caller-authorized revoked document (CONFIRMED SPM + PART_PUBLIC)
        auth_revoked_doc = await _seed_doc_unit(
            s,
            tag="inc-auth-rev",
            text="Caller-authorized revoked public policy.",
            partition_id=PART_PUBLIC,
            ver_status="REVOKED",
            search_status="REVOKED",
        )

        drifted_row = await _seed_conflict_row(
            s,
            resolution_id="res-inc-live-drifted",
            side_a=pub_base,
            side_b=drifted_exec,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        drifted_row.winner_security_partition_id = PART_PUBLIC

        await _seed_conflict_row(
            s,
            resolution_id="res-inc-revoked-spm",
            side_a=pub_base,
            side_b=revoked_spm_doc,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-inc-empty-meta",
            side_a=pub_base,
            side_b=empty_meta_doc,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-inc-auth-revoked",
            side_a=pub_base,
            side_b=auth_revoked_doc,
            status=ConflictResolutionStatus.REVOKED_WINNER,
        )
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    public_principal = _make_principal(
        subject="user_pub_inc",
        allowed_partition_ids=frozenset({PART_PUBLIC}),
    )
    conflicts_env.app.dependency_overrides[require_conflicts_bff_principal] = (
        lambda: public_principal
    )
    try:
        r_inc = await conflicts_env.client.get(f"{base}?include_inactive=true")
        assert r_inc.status_code == 200, r_inc.text
        body = r_inc.json()
        assert body["total"] == 1
        assert [item["id"] for item in body["items"]] == ["res-inc-auth-revoked"]
        assert "SECRET EXECUTIVE" not in r_inc.text
    finally:
        conflicts_env.app.dependency_overrides.pop(require_conflicts_bff_principal, None)


@pytest.mark.asyncio
async def test_resolve_and_reopen_fail_closed_404_on_missing_live_partition_and_revoked_spm(
    conflicts_env,
) -> None:
    """Fix 2: POST /resolve & POST /reopen MUST fail closed (HTTP 404, zero oracle;
    service raises ForbiddenError) on missing live partition metadata or revoked SPM."""
    from sag_api.core.errors import ForbiddenError
    from sag_api.services.knowledge_conflict_service import reopen_conflict, resolve_conflict

    async with conflicts_env.session_factory() as s:
        pub_a = await _seed_doc_unit(
            s, tag="fc-pub-a", text="Public rule A baseline.", partition_id=PART_PUBLIC
        )
        no_part_doc = await _seed_doc_unit(
            s, tag="fc-no-part", text="Doc with stripped partition metadata.", partition_id=PART_PUBLIC
        )
        no_part_doc["ver"].metadata_json = {}
        rev_spm_doc = await _seed_doc_unit(
            s, tag="fc-rev-spm", text="Doc whose source mapping was revoked.", partition_id=PART_PUBLIC
        )
        spm = await s.get(SourceProjectMapping, f"spm-{rev_spm_doc['source'].id}")
        assert spm is not None
        spm.state = "REVOKED"
        spm.revoked_at = T_JUN
        spm.revoked_by = "admin"
        spm.revocation_ref = "rev-fc-1"

        await _seed_conflict_row(
            s,
            resolution_id="res-fc-no-part",
            side_a=pub_a,
            side_b=no_part_doc,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-fc-rev-spm",
            side_a=pub_a,
            side_b=rev_spm_doc,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    headers = _bff_headers(user_id="reviewer_fc")

    for res_id in ("res-fc-no-part", "res-fc-rev-spm"):
        r_res = await conflicts_env.client.post(
            f"{base}/{res_id}/resolve",
            headers=headers,
            json={"action": "WINNER_B", "reason": "attempt bypass"},
        )
        assert r_res.status_code == 404
        assert r_res.json()["error"]["message"] == f"Conflict resolution '{res_id}' not found"

        r_reopen = await conflicts_env.client.post(
            f"{base}/{res_id}/reopen",
            headers=headers,
            json={"reason": "attempt reopen bypass"},
        )
        assert r_reopen.status_code == 404
        assert r_reopen.json()["error"]["message"] == f"Conflict resolution '{res_id}' not found"

    # Service-layer ForbiddenError check
    async with conflicts_env.session_factory() as s:
        for res_id in ("res-fc-no-part", "res-fc-rev-spm"):
            with pytest.raises(ForbiddenError):
                await resolve_conflict(
                    s,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    allowed_partition_ids={PART_PUBLIC},
                    resolution_id=res_id,
                    action="WINNER_B",
                    reason="probe",
                    actor="reviewer_fc",
                )
            with pytest.raises(ForbiddenError):
                await reopen_conflict(
                    s,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    allowed_partition_ids={PART_PUBLIC},
                    resolution_id=res_id,
                    reason="probe",
                    actor="reviewer_fc",
                )


@pytest.mark.asyncio
async def test_resolve_winner_a_swaps_evidence_json_winner_loser_fields(conflicts_env) -> None:
    """Fix 12b: POST /resolve with action='WINNER_A' swaps winner/loser fields in evidence_json."""
    async with conflicts_env.session_factory() as s:
        doc_a = await _seed_doc_unit(
            s,
            tag="swap-ev-a",
            text="Baseline approval threshold is 5M VND.",
            authority_tier=3,
            effective_from=T_JAN,
            section_path="Finance > Baseline",
        )
        doc_b = await _seed_doc_unit(
            s,
            tag="swap-ev-b",
            text="Updated approval threshold is 10M VND.",
            authority_tier=1,
            effective_from=T_JUN,
            section_path="Executive > Thresholds",
        )
        row = await _seed_conflict_row(
            s,
            resolution_id="res-swap-ev",
            side_a=doc_a,
            side_b=doc_b,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        row.evidence_json = {
            "winner_version_id": doc_b["version_id"],
            "winner_authority_tier": 1,
            "winner_effective_from": T_JUN.isoformat(),
            "winner_valid_from": T_JUN.isoformat(),
            "winner_scope_tags": ["global"],
            "winner_section_path": "Executive > Thresholds",
            "loser_version_id": doc_a["version_id"],
            "loser_authority_tier": 3,
            "loser_effective_from": T_JAN.isoformat(),
            "loser_valid_from": T_JAN.isoformat(),
            "loser_scope_tags": ["vn-local"],
            "loser_section_path": "Finance > Baseline",
        }
        await s.commit()

    base = f"/api/v1/projects/{PROJECT_ID}/conflicts"
    resp = await conflicts_env.client.post(
        f"{base}/res-swap-ev/resolve",
        headers=_bff_headers(user_id="reviewer_swap"),
        json={"action": "WINNER_A", "reason": "Keep baseline threshold"},
    )
    assert resp.status_code == 200, resp.text

    async with conflicts_env.session_factory() as s:
        updated = await s.get(KnowledgeConflictResolution, "res-swap-ev")
        assert updated is not None
        ev = updated.evidence_json
        assert ev["winner_version_id"] == doc_a["version_id"]
        assert ev["loser_version_id"] == doc_b["version_id"]
        assert ev["winner_authority_tier"] == 3
        assert ev["loser_authority_tier"] == 1
        assert ev["winner_effective_from"] == T_JAN.isoformat()
        assert ev["loser_effective_from"] == T_JUN.isoformat()
        assert ev["winner_valid_from"] == T_JAN.isoformat()
        assert ev["loser_valid_from"] == T_JUN.isoformat()
        assert ev["winner_scope_tags"] == ["vn-local"]
        assert ev["loser_scope_tags"] == ["global"]
        assert ev["winner_section_path"] == "Finance > Baseline"
        assert ev["loser_section_path"] == "Executive > Thresholds"
