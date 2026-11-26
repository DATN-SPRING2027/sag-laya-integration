"""Phase 2B (P2B-BE) TDD tests for Interactive Web Session Conflict API (`/api/v1/conflicts`).

Covers all 7 verification groups:
1. Unauthenticated / invalid JWT / inactive user / missing Continuum scope -> 401/403 fail-closed.
2. Single-project Continuum SSO JWT (`GET /api/v1/conflicts`, filters `status`,
   `conflict_subtype`, `source_id`, `document_id`, `include_inactive`, pagination,
   `Cache-Control: no-store`, strict query param & body validation).
3. Multi-project Continuum SSO JWT (`project_id="all"`, `allowed_project_ids=["proj-a", "proj-b"]`):
   lists both projects, `?project_id=proj-a` narrows, `?project_id=proj-c` -> 403,
   `?project_id=all` -> 400, never leaks `proj-c` or cross-project unmapped sources.
4. Anti-oracle 404 equivalence across `POST /resolve` & `POST /reopen`: nonexistent UUID,
   unauthorized project ID, stored `P_exec` partition, live-drifted `P_exec` partition,
   missing live partition metadata, and revoked `SourceProjectMapping` ALL return identical 404.
5. `?include_inactive=true` STILL blocks live-drifted `P_exec`, missing live partition metadata,
   and revoked `SourceProjectMapping` while returning caller-authorized revoked rows.
6. Full HITL lifecycle via `/api/v1/conflicts`: `WINNER_B`, `WINNER_A` (swap + `evidence_json`
   swap + invariant `pair_key`), `KEEP_BOTH_SCOPED`, `reopen`, `409` on revoked side,
   `actor == principal.subject`.
7. BFF non-regression: `/api/v1/projects/{project_id}/conflicts` STILL rejects Continuum user
   Bearer JWT without `X-SAG-BFF-Service-Token` (403).
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
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.api.v1 import api_router
from sag_api.core.config import settings
from sag_api.core.deps import get_session
from sag_api.core.errors import ApiError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_project_read_principal
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
    User,
)
from sag_api.db.models.conflict_resolution import compute_conflict_pair_key
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
    DocumentStatus,
    SourceStatus,
)

TENANT_ID = "org_conflicts_p2b"
OTHER_TENANT_ID = "org_other_p2b"
PROJ_A = "proj-a"
PROJ_B = "proj-b"
PROJ_C = "proj-c"
PART_EXEC = "part_exec_secret"

USER_ACTIVE_ID = "usr_p2b_reviewer_01"
USER_SECOND_ID = "usr_p2b_reviewer_02"
USER_INACTIVE_ID = "usr_p2b_inactive"

T_JAN = datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC)
T_MAR = datetime(2025, 3, 15, 0, 0, 0, tzinfo=UTC)
T_JUN = datetime(2025, 6, 15, 0, 0, 0, tzinfo=UTC)
VALID_TO_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _web_jwt_headers(
    *,
    user_id: str = USER_ACTIVE_ID,
    org_id: str = TENANT_ID,
    tenant_id: str | None = None,
    project_id: str = PROJ_A,
    partition_id: str | None = None,
    allowed_project_ids: list[str] | None = None,
    allowed_partition_ids: list[str] | None = None,
    auth_mode: str = "continuum",
    include_scope: bool = True,
) -> dict[str, str]:
    extra: dict[str, Any] = {"auth_mode": auth_mode}
    if include_scope:
        resolved_tenant = org_id if tenant_id is None else tenant_id
        resolved_partition = project_id if partition_id is None else partition_id
        extra.update(
            {
                "org_id": org_id,
                "tenant_id": resolved_tenant,
                "project_id": project_id,
                "security_partition": resolved_partition,
                "jti": secrets.token_urlsafe(12),
            }
        )
        if allowed_project_ids is not None:
            extra["allowed_project_ids"] = allowed_project_ids
        if allowed_partition_ids is not None:
            extra["allowed_partition_ids"] = allowed_partition_ids
    token = create_access_token(user_id, extra=extra)
    return {"Authorization": f"Bearer {token}"}


def _bff_headers(
    *,
    token: str = "test-bff-p2b-secret",
    user_id: str = USER_ACTIVE_ID,
    org_id: str = TENANT_ID,
    project_id: str = PROJ_A,
) -> dict[str, str]:
    return {
        "X-SAG-BFF-Service-Token": token,
        "X-Continuum-User-Id": user_id,
        "X-Continuum-Organization-Id": org_id,
        "X-Continuum-Tenant-Id": org_id,
        "X-Continuum-Project-Id": project_id,
        "X-Continuum-Security-Partition": project_id,
    }


def _make_principal(
    *,
    subject: str = USER_ACTIVE_ID,
    tenant_id: str = TENANT_ID,
    allowed_project_ids: frozenset[str] = frozenset({PROJ_A}),
    allowed_partition_ids: frozenset[str] = frozenset({PROJ_A}),
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject=subject,
        organization_id=tenant_id,
        allowed_project_ids=allowed_project_ids,
        issuer="continuum-sag-session",
        key_id="sag-hs256-session",
        token_id="jti-p2b-override",
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
    tenant_id: str = TENANT_ID,
    project_id: str = PROJ_A,
    source_id: str | None = None,
    create_source: bool = True,
    spm_state: str | None = "CONFIRMED",
    filename: str | None = None,
    document_id: str | None = None,
    logical_source_id: str | None = None,
    version_no: int = 1,
    partition_id: str | None = None,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    section_path: str = "Policy > General",
    page_from: int = 1,
    page_to: int = 2,
    doc_status: DocumentStatus = DocumentStatus.READY,
    doc_is_active: bool = True,
    ver_status: str = "READY",
    search_status: str = "SEARCH_READY",
) -> dict[str, Any]:
    resolved_partition = project_id if partition_id is None else partition_id
    src_id = source_id or f"src-{tag}"

    src = await session.get(Source, src_id)
    if src is None and create_source:
        src = Source(
            id=src_id,
            name=f"Source {tag}",
            sag_source_config_id=f"cfg-{src_id}",
            status=SourceStatus.ACTIVE,
        )
        session.add(src)
        await session.flush()

    spm_id = f"spm-{src_id}-{project_id}-{tenant_id}"
    if spm_state is not None:
        existing_spm = await session.get(SourceProjectMapping, spm_id)
        if existing_spm is None:
            spm = SourceProjectMapping(
                id=spm_id,
                source_id=src_id,
                organization_id=tenant_id,
                project_id=project_id,
                state=spm_state,
                mapping_version=1,
                confirmed_at=T_JAN,
                confirmed_by="admin",
                approval_ref=f"appr-{spm_id}",
                revoked_at=T_JUN if spm_state == "REVOKED" else None,
                revoked_by="admin" if spm_state == "REVOKED" else None,
                revocation_ref=f"rev-{spm_id}" if spm_state == "REVOKED" else None,
            )
            session.add(spm)
            await session.flush()

    doc_id = document_id or f"doc-{tag}"
    doc = Document(
        id=doc_id,
        tenant_id=tenant_id,
        project_id=project_id,
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
        metadata_json={"security_partition_id": resolved_partition},
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
        security_partition_id=resolved_partition,
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
        "source_id": src_id,
        "spm_id": spm_id,
        "doc": doc,
        "ver": ver,
        "unit": unit,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "document_id": doc.id,
        "logical_source_id": doc.logical_source_id,
        "version_id": ver.id,
        "unit_id": unit.id,
        "content_hash": unit.content_hash,
        "partition_id": resolved_partition,
    }


async def _seed_conflict_row(
    session: AsyncSession,
    *,
    resolution_id: str,
    side_a: dict[str, Any],
    side_b: dict[str, Any],
    tenant_id: str | None = None,
    project_id: str | None = None,
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
    resolved_tenant = tenant_id or side_a["tenant_id"]
    resolved_project = project_id or side_a["project_id"]

    graph_edge_id: str | None = None
    if with_contradicts_edge:
        con_edge = KnowledgeGraphEdge(
            id=f"edge-con-{resolution_id}",
            project_id=resolved_project,
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
            project_id=resolved_project,
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
        tenant_id=resolved_tenant,
        project_id=resolved_project,
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
async def web_conflicts_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-p2b-secret")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(conn, _rec) -> None:  # noqa: ANN001
        conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as s:
        s.add_all(
            [
                User(
                    id=USER_ACTIVE_ID,
                    email="reviewer1@example.com",
                    password_hash="not-used-in-continuum-sso",
                    name="Reviewer One",
                    is_active=True,
                ),
                User(
                    id=USER_SECOND_ID,
                    email="reviewer2@example.com",
                    password_hash="not-used-in-continuum-sso",
                    name="Reviewer Two",
                    is_active=True,
                ),
                User(
                    id=USER_INACTIVE_ID,
                    email="inactive@example.com",
                    password_hash="not-used-in-continuum-sso",
                    name="Inactive User",
                    is_active=False,
                ),
            ]
        )
        await s.commit()

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
# 1. Fail-Closed Auth: Unauthenticated, Invalid JWT, Missing Continuum Scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_conflicts_fail_closed_auth_and_missing_scope(
    web_conflicts_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client: httpx.AsyncClient = web_conflicts_env.client
    base = "/api/v1/conflicts"

    # Unauthenticated -> 401 on all 3 endpoints
    assert (await client.get(base)).status_code == 401
    assert (await client.post(f"{base}/res-1/resolve", json={"action": "WINNER_B"})).status_code == 401
    assert (await client.post(f"{base}/res-1/reopen", json={})).status_code == 401

    # Invalid JWT -> 401 on all 3 endpoints
    bad_headers = {"Authorization": "Bearer not-a-valid-jwt"}
    assert (await client.get(base, headers=bad_headers)).status_code == 401
    assert (
        await client.post(f"{base}/res-1/resolve", headers=bad_headers, json={"action": "WINNER_B"})
    ).status_code == 401
    assert (await client.post(f"{base}/res-1/reopen", headers=bad_headers, json={})).status_code == 401

    # Inactive user -> 401
    inactive_headers = _web_jwt_headers(user_id=USER_INACTIVE_ID)
    assert (await client.get(base, headers=inactive_headers)).status_code == 401

    # Valid user JWT missing Continuum scope claims -> 403 fail-closed
    no_scope_headers = _web_jwt_headers(include_scope=False)
    assert (await client.get(base, headers=no_scope_headers)).status_code == 403
    assert (
        await client.post(f"{base}/res-1/resolve", headers=no_scope_headers, json={"action": "WINNER_B"})
    ).status_code == 403
    assert (await client.post(f"{base}/res-1/reopen", headers=no_scope_headers, json={})).status_code == 403

    # Multi-project "all" JWT without allowed_project_ids -> 403 fail-closed
    broken_all_headers = _web_jwt_headers(project_id="all", partition_id="all")
    assert (await client.get(base, headers=broken_all_headers)).status_code == 403

    # Non-continuum auth_mode without X-SAG-Principal-Assertion -> 403 fail-closed
    monkeypatch.setattr(settings, "auth_mode", "password")
    pwd_headers = _web_jwt_headers(auth_mode="password", include_scope=False)
    assert (await client.get(base, headers=pwd_headers)).status_code == 403
    monkeypatch.setattr(settings, "auth_mode", "continuum")

    # Empty tenant / projects / partitions on VerifiedPrincipal -> 403 fail-closed on all 3 endpoints
    valid_user_headers = _web_jwt_headers()
    for bad_principal in (
        _make_principal(tenant_id=""),
        _make_principal(allowed_project_ids=frozenset()),
        _make_principal(allowed_partition_ids=frozenset()),
    ):
        web_conflicts_env.app.dependency_overrides[require_project_read_principal] = (
            lambda p=bad_principal: p
        )
        try:
            assert (await client.get(base, headers=valid_user_headers)).status_code == 403
            assert (
                await client.post(
                    f"{base}/res-1/resolve",
                    headers=valid_user_headers,
                    json={"action": "WINNER_B"},
                )
            ).status_code == 403
            assert (
                await client.post(f"{base}/res-1/reopen", headers=valid_user_headers, json={})
            ).status_code == 403
        finally:
            web_conflicts_env.app.dependency_overrides.pop(require_project_read_principal, None)


# ---------------------------------------------------------------------------
# 2. Single-Project Continuum SSO JWT: Filters, Pagination, No-Store, Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_single_project_list_filters_source_document_pagination_and_validation(
    web_conflicts_env,
) -> None:
    async with web_conflicts_env.session_factory() as s:
        doc_a = await _seed_doc_unit(
            s,
            tag="sp-a",
            project_id=PROJ_A,
            source_id="src-alpha",
            filename="handbook-2024.md",
            logical_source_id="continuum:doc:alpha",
            text="Annual leave quota is 12 days per year.",
            version_no=1,
            authority_tier=3,
            effective_from=T_JAN,
            section_path="HR > Leave",
            page_from=2,
            page_to=3,
        )
        doc_b = await _seed_doc_unit(
            s,
            tag="sp-b",
            project_id=PROJ_A,
            source_id="src-beta",
            filename="policy-2025.md",
            logical_source_id="continuum:doc:beta",
            text="Annual leave quota is 18 days per year effective June 2025.",
            version_no=2,
            authority_tier=1,
            effective_from=T_JUN,
            section_path="Executive > Leave",
            page_from=1,
            page_to=1,
        )
        doc_c = await _seed_doc_unit(
            s,
            tag="sp-c",
            project_id=PROJ_A,
            source_id="src-gamma",
            filename="travel-policy.md",
            logical_source_id="continuum:doc:gamma",
            text="Standard travel cap is 500 EUR per trip.",
            version_no=1,
            authority_tier=2,
            effective_from=T_MAR,
        )
        doc_rev = await _seed_doc_unit(
            s,
            tag="sp-rev",
            project_id=PROJ_A,
            source_id="src-revoked",
            filename="recalled-travel.md",
            text="Recalled travel cap is 100 EUR per trip.",
            ver_status="REVOKED",
            search_status="REVOKED",
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-sp-1",
            side_a=doc_a,
            side_b=doc_b,
            subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            status=ConflictResolutionStatus.ACTIVE,
            mode=ConflictResolutionMode.AUTO_POLICY,
            confidence=0.95,
            cosine_similarity=0.90,
            effective_valid_to=T_JUN,
            reason="2025 executive leave policy supersedes 2024 handbook.",
            updated_at=datetime(2025, 6, 20, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-sp-2",
            side_a=doc_a,
            side_b=doc_c,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.PENDING_REVIEW,
            mode=ConflictResolutionMode.UNRESOLVED,
            confidence=0.83,
            cosine_similarity=0.85,
            scope_difference_summary="HR vs Travel rules",
            reason="Requires reviewer adjudication.",
            degraded_fallback=True,
            updated_at=datetime(2025, 6, 21, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-sp-3",
            side_a=doc_b,
            side_b=doc_c,
            subtype=ConflictSubtype.SCOPED_EXCEPTION,
            status=ConflictResolutionStatus.NEEDS_REVALIDATION,
            mode=ConflictResolutionMode.UNRESOLVED,
            confidence=0.79,
            updated_at=datetime(2025, 6, 22, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-sp-rev",
            side_a=doc_c,
            side_b=doc_rev,
            subtype=ConflictSubtype.HARD_CONTRADICTION,
            status=ConflictResolutionStatus.REVOKED_WINNER,
            mode=ConflictResolutionMode.UNRESOLVED,
            confidence=0.72,
            updated_at=datetime(2025, 6, 23, 0, 0, 0, tzinfo=UTC),
        )
        await s.commit()

    client: httpx.AsyncClient = web_conflicts_env.client
    headers = _web_jwt_headers(project_id=PROJ_A)
    base = "/api/v1/conflicts"

    # Default GET -> 3 active conflicts ordered by updated_at DESC + Cache-Control: no-store
    r_all = await client.get(base, headers=headers)
    assert r_all.status_code == 200, r_all.text
    assert r_all.headers.get("cache-control") == "no-store"
    body = r_all.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [i["id"] for i in body["items"]] == ["res-sp-3", "res-sp-2", "res-sp-1"]

    # Hydrated side_a & side_b verification
    item1 = body["items"][2]
    assert item1["project_id"] == PROJ_A
    assert item1["side_a"]["document_id"] == doc_a["document_id"]
    assert item1["side_a"]["document_name"] == "handbook-2024.md"
    assert "12 days" in item1["side_a"]["text_snapshot"]
    assert item1["side_b"]["document_id"] == doc_b["document_id"]
    assert item1["side_b"]["document_name"] == "policy-2025.md"
    assert "18 days" in item1["side_b"]["text_snapshot"]

    # Repeatable status filter (matches FE badge query!)
    r_badge = await client.get(
        f"{base}?status=PENDING_REVIEW&status=NEEDS_REVALIDATION&limit=1",
        headers=headers,
    )
    assert r_badge.status_code == 200
    assert r_badge.json()["total"] == 2
    assert len(r_badge.json()["items"]) == 1
    assert r_badge.json()["items"][0]["id"] == "res-sp-3"

    # Repeatable conflict_subtype filter
    r_sub = await client.get(
        f"{base}?conflict_subtype=DIRECT_SUPERSEDES&conflict_subtype=SCOPED_EXCEPTION",
        headers=headers,
    )
    assert r_sub.status_code == 200
    assert [i["id"] for i in r_sub.json()["items"]] == ["res-sp-3", "res-sp-1"]

    # source_id filter matches both winner_doc.source_id and loser_doc.source_id
    r_src_beta = await client.get(f"{base}?source_id=src-beta", headers=headers)
    assert r_src_beta.status_code == 200
    assert [i["id"] for i in r_src_beta.json()["items"]] == ["res-sp-3", "res-sp-1"]

    r_src_alpha = await client.get(f"{base}?source_id=src-alpha", headers=headers)
    assert r_src_alpha.status_code == 200
    assert [i["id"] for i in r_src_alpha.json()["items"]] == ["res-sp-2", "res-sp-1"]

    # document_id filter matches Document.id and logical_source_id
    r_doc = await client.get(f"{base}?document_id={doc_b['document_id']}", headers=headers)
    assert r_doc.status_code == 200
    assert [i["id"] for i in r_doc.json()["items"]] == ["res-sp-3", "res-sp-1"]

    r_logical = await client.get(f"{base}?document_id={doc_c['logical_source_id']}", headers=headers)
    assert r_logical.status_code == 200
    assert [i["id"] for i in r_logical.json()["items"]] == ["res-sp-3", "res-sp-2"]

    # Explicit authorized project_id filter succeeds; unauthorized returns 403
    assert (await client.get(f"{base}?project_id={PROJ_A}", headers=headers)).status_code == 200
    assert (await client.get(f"{base}?project_id={PROJ_B}", headers=headers)).status_code == 403

    # include_inactive=true + pagination
    r_inc = await client.get(f"{base}?include_inactive=true&limit=2&offset=1", headers=headers)
    assert r_inc.status_code == 200
    assert r_inc.json()["total"] == 4
    assert [i["id"] for i in r_inc.json()["items"]] == ["res-sp-3", "res-sp-2"]

    # Strict query parameter validation -> 400 / 422
    assert (await client.get(f"{base}?debug=1", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?project_id=all", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?project_id=ALL", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?project_id=bad!id", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?project_id=", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?source_id=%20%20", headers=headers)).status_code == 400
    assert (await client.get(f"{base}?document_id=%20%20", headers=headers)).status_code == 400

    # Duplicate single-value query params -> 400
    for dup_query in (
        f"project_id={PROJ_A}&project_id={PROJ_A}",
        "source_id=src-alpha&source_id=src-beta",
        f"document_id={doc_a['document_id']}&document_id={doc_b['document_id']}",
        "include_inactive=false&include_inactive=true",
        "limit=10&limit=20",
        "offset=0&offset=5",
    ):
        assert (await client.get(f"{base}?{dup_query}", headers=headers)).status_code == 400, dup_query

    # Out-of-bounds / invalid enum query params -> 422
    assert (await client.get(f"{base}?status=BOGUS", headers=headers)).status_code == 422
    assert (await client.get(f"{base}?conflict_subtype=BOGUS", headers=headers)).status_code == 422
    assert (await client.get(f"{base}?limit=0", headers=headers)).status_code == 422
    assert (await client.get(f"{base}?limit=201", headers=headers)).status_code == 422
    assert (await client.get(f"{base}?offset=-1", headers=headers)).status_code == 422
    assert (await client.get(f"{base}?source_id={'s' * 129}", headers=headers)).status_code == 422

    # POST strict validation: resolution_id, query params, malformed JSON, NaN, oversized body, extra="forbid"
    assert (
        await client.post(f"{base}/bad!id/resolve", headers=headers, json={"action": "WINNER_B"})
    ).status_code == 400
    assert (
        await client.post(f"{base}/res-sp-1/resolve?debug=1", headers=headers, json={"action": "WINNER_B"})
    ).status_code == 400
    assert (
        await client.post(f"{base}/res-sp-1/reopen?debug=1", headers=headers, json={})
    ).status_code == 400
    assert (
        await client.post(
            f"{base}/res-sp-1/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=b"{broken",
        )
    ).status_code == 400
    assert (
        await client.post(
            f"{base}/res-sp-1/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=b'{"action":"WINNER_B","reason":NaN}',
        )
    ).status_code == 400
    oversized = b'{"action":"WINNER_B","reason":"' + (b"x" * (17 * 1024)) + b'"}'
    assert (
        await client.post(
            f"{base}/res-sp-1/resolve",
            headers={**headers, "Content-Type": "application/json"},
            content=oversized,
        )
    ).status_code == 400
    assert (
        await client.post(
            f"{base}/res-sp-1/resolve",
            headers=headers,
            json={"action": "WINNER_B", "spoof": True},
        )
    ).status_code == 422
    assert (
        await client.post(
            f"{base}/res-sp-1/reopen",
            headers=headers,
            json={"reason": "ok", "extra": 1},
        )
    ).status_code == 422


# ---------------------------------------------------------------------------
# 3. Multi-Project Continuum SSO JWT (`project_id="all"`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_multi_project_all_scope_narrowing_and_isolation(
    web_conflicts_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared_projects: list[str] = []
    from sag_api.services import knowledge_conflict_service as kcs

    monkeypatch.setattr(kcs, "_clear_project_caches_safely", lambda pid: cleared_projects.append(pid))

    async with web_conflicts_env.session_factory() as s:
        a1 = await _seed_doc_unit(s, tag="mp-a1", project_id=PROJ_A, source_id="src-shared", text="Proj A rule 1.")
        a2 = await _seed_doc_unit(s, tag="mp-a2", project_id=PROJ_A, text="Proj A rule 2.")

        b1 = await _seed_doc_unit(s, tag="mp-b1", project_id=PROJ_B, text="Proj B rule 1.")
        b2 = await _seed_doc_unit(s, tag="mp-b2", project_id=PROJ_B, text="Proj B rule 2.")
        # Shared source CONFIRMED in PROJ_A, but REVOKED in PROJ_B -> conflict in PROJ_B MUST be hidden!
        b_unmapped = await _seed_doc_unit(
            s,
            tag="mp-b-unmapped",
            project_id=PROJ_B,
            source_id="src-shared",
            create_source=False,
            spm_state="REVOKED",
            text="SECRET cross-project revoked mapping doc.",
        )

        # Unauthorized project PROJ_C
        c1 = await _seed_doc_unit(s, tag="mp-c1", project_id=PROJ_C, text="SECRET PROJ_C rule 1.")
        c2 = await _seed_doc_unit(s, tag="mp-c2", project_id=PROJ_C, text="SECRET PROJ_C rule 2.")

        # Different tenant reusing PROJ_A name
        t1 = await _seed_doc_unit(
            s, tag="mp-t1", tenant_id=OTHER_TENANT_ID, project_id=PROJ_A, text="SECRET OTHER TENANT 1."
        )
        t2 = await _seed_doc_unit(
            s, tag="mp-t2", tenant_id=OTHER_TENANT_ID, project_id=PROJ_A, text="SECRET OTHER TENANT 2."
        )

        await _seed_conflict_row(
            s,
            resolution_id="res-mp-a",
            side_a=a1,
            side_b=a2,
            updated_at=datetime(2025, 6, 20, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-mp-b",
            side_a=b1,
            side_b=b2,
            updated_at=datetime(2025, 6, 21, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-mp-b-unmapped",
            side_a=b1,
            side_b=b_unmapped,
            updated_at=datetime(2025, 6, 22, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-mp-c",
            side_a=c1,
            side_b=c2,
            updated_at=datetime(2025, 6, 23, 0, 0, 0, tzinfo=UTC),
        )
        await _seed_conflict_row(
            s,
            resolution_id="res-mp-other-tenant",
            side_a=t1,
            side_b=t2,
            updated_at=datetime(2025, 6, 24, 0, 0, 0, tzinfo=UTC),
        )
        await s.commit()

    client: httpx.AsyncClient = web_conflicts_env.client
    multi_headers = _web_jwt_headers(
        project_id="all",
        partition_id="all",
        allowed_project_ids=[PROJ_A, PROJ_B],
        allowed_partition_ids=[PROJ_A, PROJ_B],
    )
    base = "/api/v1/conflicts"

    # Lists both PROJ_A and PROJ_B; never leaks PROJ_C, OTHER_TENANT_ID, or revoked-in-PROJ_B shared source
    r_all = await client.get(base, headers=multi_headers)
    assert r_all.status_code == 200, r_all.text
    assert r_all.headers.get("cache-control") == "no-store"
    assert r_all.json()["total"] == 2
    assert [i["id"] for i in r_all.json()["items"]] == ["res-mp-b", "res-mp-a"]
    assert "SECRET" not in r_all.text

    # Narrow to PROJ_A
    r_a = await client.get(f"{base}?project_id={PROJ_A}", headers=multi_headers)
    assert r_a.status_code == 200
    assert [i["id"] for i in r_a.json()["items"]] == ["res-mp-a"]

    # Narrow to PROJ_B
    r_b = await client.get(f"{base}?project_id={PROJ_B}", headers=multi_headers)
    assert r_b.status_code == 200
    assert [i["id"] for i in r_b.json()["items"]] == ["res-mp-b"]

    # Request unauthorized PROJ_C -> 403
    assert (await client.get(f"{base}?project_id={PROJ_C}", headers=multi_headers)).status_code == 403

    # Explicit ?project_id=all -> 400
    assert (await client.get(f"{base}?project_id=all", headers=multi_headers)).status_code == 400

    # Mutate conflict in PROJ_B under multi-project scope and verify cache clear uses row.project_id (PROJ_B)
    r_res_b = await client.post(
        f"{base}/res-mp-b/resolve",
        headers=multi_headers,
        json={"action": "WINNER_B", "reason": "Resolve proj-b conflict"},
    )
    assert r_res_b.status_code == 200, r_res_b.text
    assert r_res_b.json()["project_id"] == PROJ_B
    assert cleared_projects[-1] == PROJ_B

    r_reopen_b = await client.post(
        f"{base}/res-mp-b/reopen",
        headers=multi_headers,
        json={"reason": "Reopen proj-b conflict"},
    )
    assert r_reopen_b.status_code == 200, r_reopen_b.text
    assert cleared_projects[-1] == PROJ_B

    # Mutating conflict with REVOKED mapping in PROJ_B (even though CONFIRMED in PROJ_A) -> 404 anti-oracle!
    r_unmapped = await client.post(
        f"{base}/res-mp-b-unmapped/resolve",
        headers=multi_headers,
        json={"action": "WINNER_B"},
    )
    assert r_unmapped.status_code == 404


# ---------------------------------------------------------------------------
# 4. Anti-Oracle 404 Equivalence across POST /resolve & POST /reopen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_anti_oracle_404_equivalence_resolve_and_reopen(web_conflicts_env) -> None:
    async with web_conflicts_env.session_factory() as s:
        pub_a = await _seed_doc_unit(
            s, tag="ao-pub-a", project_id=PROJ_A, partition_id=PROJ_A, text="Public policy A."
        )
        # 1. Unauthorized project PROJ_C
        proj_c_1 = await _seed_doc_unit(
            s, tag="ao-c1", project_id=PROJ_C, partition_id=PROJ_C, text="Proj C rule 1."
        )
        proj_c_2 = await _seed_doc_unit(
            s, tag="ao-c2", project_id=PROJ_C, partition_id=PROJ_C, text="Proj C rule 2."
        )
        # 2. Stored P_exec partition
        exec_doc = await _seed_doc_unit(
            s, tag="ao-exec", project_id=PROJ_A, partition_id=PART_EXEC, text="Executive secret."
        )
        # 3. Live-drifted P_exec partition (stored says PROJ_A, live DocumentVersion is PART_EXEC)
        drifted_doc = await _seed_doc_unit(
            s, tag="ao-drift", project_id=PROJ_A, partition_id=PART_EXEC, text="Drifted exec secret."
        )
        # 4. Missing live partition metadata
        missing_meta_doc = await _seed_doc_unit(
            s, tag="ao-nometa", project_id=PROJ_A, partition_id=PROJ_A, text="Stripped partition metadata."
        )
        missing_meta_doc["ver"].metadata_json = {}
        # 5. Revoked SourceProjectMapping
        rev_spm_doc = await _seed_doc_unit(
            s,
            tag="ao-revspm",
            project_id=PROJ_A,
            partition_id=PROJ_A,
            spm_state="REVOKED",
            text="Revoked source mapping doc.",
        )

        await _seed_conflict_row(s, resolution_id="res-ao-unauth-proj", side_a=proj_c_1, side_b=proj_c_2)
        await _seed_conflict_row(s, resolution_id="res-ao-stored-exec", side_a=pub_a, side_b=exec_doc)
        drifted_row = await _seed_conflict_row(
            s, resolution_id="res-ao-live-drift", side_a=pub_a, side_b=drifted_doc
        )
        drifted_row.winner_security_partition_id = PROJ_A
        await _seed_conflict_row(s, resolution_id="res-ao-missing-meta", side_a=pub_a, side_b=missing_meta_doc)
        await _seed_conflict_row(s, resolution_id="res-ao-revoked-spm", side_a=pub_a, side_b=rev_spm_doc)
        await s.commit()

    client: httpx.AsyncClient = web_conflicts_env.client
    headers = _web_jwt_headers(project_id=PROJ_A)
    base = "/api/v1/conflicts"
    missing_uuid = "00000000-0000-4000-8000-000000000000"

    baseline_resolve = await client.post(
        f"{base}/{missing_uuid}/resolve",
        headers=headers,
        json={"action": "WINNER_B", "reason": "probe"},
    )
    baseline_reopen = await client.post(
        f"{base}/{missing_uuid}/reopen",
        headers=headers,
        json={"reason": "probe"},
    )
    assert baseline_resolve.status_code == 404
    assert baseline_reopen.status_code == 404
    expected_code = baseline_resolve.json()["error"]["code"]
    assert baseline_reopen.json()["error"]["code"] == expected_code

    for target_id in (
        "res-ao-unauth-proj",
        "res-ao-stored-exec",
        "res-ao-live-drift",
        "res-ao-missing-meta",
        "res-ao-revoked-spm",
    ):
        r_res = await client.post(
            f"{base}/{target_id}/resolve",
            headers=headers,
            json={"action": "WINNER_B", "reason": "probe"},
        )
        r_reopen = await client.post(
            f"{base}/{target_id}/reopen",
            headers=headers,
            json={"reason": "probe"},
        )
        assert r_res.status_code == 404, f"resolve {target_id}: {r_res.status_code} {r_res.text}"
        assert r_reopen.status_code == 404, f"reopen {target_id}: {r_reopen.status_code} {r_reopen.text}"
        assert r_res.json()["error"]["code"] == expected_code
        assert r_reopen.json()["error"]["code"] == expected_code
        assert r_res.json()["error"]["message"] == f"Conflict resolution '{target_id}' not found"
        assert r_reopen.json()["error"]["message"] == f"Conflict resolution '{target_id}' not found"

    # Authorized dual-partition principal CAN resolve & reopen res-ao-stored-exec
    dual_principal = _make_principal(
        subject="exec_web_reviewer",
        allowed_project_ids=frozenset({PROJ_A}),
        allowed_partition_ids=frozenset({PROJ_A, PART_EXEC}),
    )
    web_conflicts_env.app.dependency_overrides[require_project_read_principal] = lambda: dual_principal
    try:
        r_ok = await client.post(
            f"{base}/res-ao-stored-exec/resolve",
            headers=headers,
            json={"action": "WINNER_B", "reason": "Exec authorized"},
        )
        assert r_ok.status_code == 200, r_ok.text
        assert r_ok.json()["resolved_by"] == "exec_web_reviewer"
    finally:
        web_conflicts_env.app.dependency_overrides.pop(require_project_read_principal, None)


# ---------------------------------------------------------------------------
# 5. `?include_inactive=true` Still Enforces Live Partition ACL & CONFIRMED SPM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_include_inactive_still_blocks_drifted_missing_meta_and_revoked_spm(
    web_conflicts_env,
) -> None:
    async with web_conflicts_env.session_factory() as s:
        pub_base = await _seed_doc_unit(
            s, tag="inc-w-base", project_id=PROJ_A, partition_id=PROJ_A, text="Public baseline rule."
        )
        drifted_exec = await _seed_doc_unit(
            s,
            tag="inc-w-drift",
            project_id=PROJ_A,
            partition_id=PART_EXEC,
            text="TOP SECRET EXECUTIVE PAYROLL.",
        )
        missing_meta = await _seed_doc_unit(
            s,
            tag="inc-w-nometa",
            project_id=PROJ_A,
            partition_id=PROJ_A,
            text="TOP SECRET UNTAGGED VERSION.",
        )
        missing_meta["ver"].metadata_json = {}
        rev_spm = await _seed_doc_unit(
            s,
            tag="inc-w-revspm",
            project_id=PROJ_A,
            partition_id=PROJ_A,
            spm_state="REVOKED",
            text="TOP SECRET REVOKED MAPPING.",
        )
        auth_revoked = await _seed_doc_unit(
            s,
            tag="inc-w-authrev",
            project_id=PROJ_A,
            partition_id=PROJ_A,
            ver_status="REVOKED",
            search_status="REVOKED",
            text="Authorized revoked public guideline.",
        )

        drifted_row = await _seed_conflict_row(
            s, resolution_id="res-inc-w-drift", side_a=pub_base, side_b=drifted_exec
        )
        drifted_row.winner_security_partition_id = PROJ_A
        await _seed_conflict_row(s, resolution_id="res-inc-w-nometa", side_a=pub_base, side_b=missing_meta)
        await _seed_conflict_row(s, resolution_id="res-inc-w-revspm", side_a=pub_base, side_b=rev_spm)
        await _seed_conflict_row(
            s,
            resolution_id="res-inc-w-authrev",
            side_a=pub_base,
            side_b=auth_revoked,
            status=ConflictResolutionStatus.REVOKED_WINNER,
        )
        await s.commit()

    client: httpx.AsyncClient = web_conflicts_env.client
    headers = _web_jwt_headers(project_id=PROJ_A)

    r_inc = await client.get("/api/v1/conflicts?include_inactive=true", headers=headers)
    assert r_inc.status_code == 200, r_inc.text
    assert r_inc.headers.get("cache-control") == "no-store"
    body = r_inc.json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == ["res-inc-w-authrev"]
    assert "TOP SECRET" not in r_inc.text


# ---------------------------------------------------------------------------
# 6. Full HITL Lifecycle via `/api/v1/conflicts`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_full_hitl_lifecycle_resolve_swap_keep_both_reopen_and_409(
    web_conflicts_env,
) -> None:
    async with web_conflicts_env.session_factory() as s:
        doc_a = await _seed_doc_unit(
            s,
            tag="life-a",
            project_id=PROJ_A,
            text="Expense limit without approval is 5M VND.",
            authority_tier=3,
            effective_from=T_JAN,
            section_path="Finance > Baseline",
        )
        doc_b = await _seed_doc_unit(
            s,
            tag="life-b",
            project_id=PROJ_A,
            text="Expense limit without approval is 10M VND.",
            authority_tier=1,
            effective_from=T_JUN,
            section_path="Executive > Limits",
        )
        doc_rev = await _seed_doc_unit(
            s,
            tag="life-rev",
            project_id=PROJ_A,
            text="Revoked expense draft 50M VND.",
            ver_status="REVOKED",
            search_status="REVOKED",
        )
        row = await _seed_conflict_row(
            s,
            resolution_id="res-life-1",
            side_a=doc_a,
            side_b=doc_b,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        orig_pair_key = row.pair_key
        row.evidence_json = {
            "winner_version_id": doc_b["version_id"],
            "winner_authority_tier": 1,
            "winner_effective_from": T_JUN.isoformat(),
            "winner_scope_tags": ["global"],
            "winner_section_path": "Executive > Limits",
            "loser_version_id": doc_a["version_id"],
            "loser_authority_tier": 3,
            "loser_effective_from": T_JAN.isoformat(),
            "loser_scope_tags": ["vn-local"],
            "loser_section_path": "Finance > Baseline",
        }
        await _seed_conflict_row(
            s,
            resolution_id="res-life-rev",
            side_a=doc_a,
            side_b=doc_rev,
            status=ConflictResolutionStatus.PENDING_REVIEW,
        )
        await s.commit()

    client: httpx.AsyncClient = web_conflicts_env.client
    base = "/api/v1/conflicts"
    h1 = _web_jwt_headers(user_id=USER_ACTIVE_ID, project_id=PROJ_A)
    h2 = _web_jwt_headers(user_id=USER_SECOND_ID, project_id=PROJ_A)

    # 1. WINNER_B
    r_wb = await client.post(
        f"{base}/res-life-1/resolve",
        headers=h1,
        json={"action": "WINNER_B", "reason": "Adopt 10M executive policy"},
    )
    assert r_wb.status_code == 200, r_wb.text
    assert r_wb.headers.get("cache-control") == "no-store"
    wb = r_wb.json()
    assert wb["status"] == "ACTIVE"
    assert wb["resolution_mode"] == "MANUAL_HITL"
    assert wb["winner_document_id"] == doc_b["document_id"]
    assert wb["loser_document_id"] == doc_a["document_id"]
    assert wb["pair_key"] == orig_pair_key
    assert wb["resolved_by"] == USER_ACTIVE_ID
    assert wb["audit_history"][-1]["action"] == "WINNER_B"
    assert wb["audit_history"][-1]["actor"] == USER_ACTIVE_ID

    # 2. WINNER_A -> symmetric swap + evidence_json swap + invariant pair_key
    r_wa = await client.post(
        f"{base}/res-life-1/resolve",
        headers=h2,
        json={"action": "WINNER_A", "reason": "Keep 5M baseline"},
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
    assert wa["resolved_by"] == USER_SECOND_ID
    assert wa["audit_history"][-1]["action"] == "WINNER_A"
    assert wa["audit_history"][-1]["actor"] == USER_SECOND_ID

    async with web_conflicts_env.session_factory() as s:
        updated = await s.get(KnowledgeConflictResolution, "res-life-1")
        assert updated is not None
        assert updated.evidence_json["winner_version_id"] == doc_a["version_id"]
        assert updated.evidence_json["loser_version_id"] == doc_b["version_id"]
        assert updated.evidence_json["winner_authority_tier"] == 3
        assert updated.evidence_json["loser_authority_tier"] == 1
        assert updated.evidence_json["winner_scope_tags"] == ["vn-local"]
        assert updated.evidence_json["loser_scope_tags"] == ["global"]

    # 3. KEEP_BOTH_SCOPED
    r_kb = await client.post(
        f"{base}/res-life-1/resolve",
        headers=h1,
        json={"action": "KEEP_BOTH_SCOPED", "reason": "Distinct regional rules"},
    )
    assert r_kb.status_code == 200, r_kb.text
    assert r_kb.headers.get("cache-control") == "no-store"
    kb = r_kb.json()
    assert kb["status"] == "DISMISSED_SCOPED"
    assert kb["resolution_mode"] == "KEEP_BOTH_SCOPED"
    assert kb["effective_valid_to"] is None
    assert kb["resolved_by"] == USER_ACTIVE_ID

    async with web_conflicts_env.session_factory() as s:
        sup_edges = (
            await s.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert sup_edges == []

    # 4. Reopen back to PENDING_REVIEW
    r_reopen = await client.post(
        f"{base}/res-life-1/reopen",
        headers=h2,
        json={"reason": "Legal requested another pass"},
    )
    assert r_reopen.status_code == 200, r_reopen.text
    assert r_reopen.headers.get("cache-control") == "no-store"
    ro = r_reopen.json()
    assert ro["status"] == "PENDING_REVIEW"
    assert ro["resolution_mode"] == "UNRESOLVED"
    assert ro["effective_valid_to"] is None
    assert ro["resolved_by"] == USER_SECOND_ID
    assert ro["audit_history"][-1]["action"] == "REOPEN"
    assert ro["audit_history"][-1]["actor"] == USER_SECOND_ID

    async with web_conflicts_env.session_factory() as s:
        con_edge = await s.get(KnowledgeGraphEdge, "edge-con-res-life-1")
        assert con_edge is not None
        assert con_edge.metadata_json["is_candidate"] is True

    # 5. 409 on choosing revoked winner or reopening conflict with revoked side
    r_rev_resolve = await client.post(
        f"{base}/res-life-rev/resolve",
        headers=h1,
        json={"action": "WINNER_B", "reason": "try revoked"},
    )
    assert r_rev_resolve.status_code == 409

    r_rev_reopen = await client.post(
        f"{base}/res-life-rev/reopen",
        headers=h1,
        json={"reason": "try reopen revoked"},
    )
    assert r_rev_reopen.status_code == 409


# ---------------------------------------------------------------------------
# 7. BFF Non-Regression: `/api/v1/projects/{project_id}/conflicts` Remains Fail-Closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bff_router_still_rejects_web_user_jwt_without_service_token(
    web_conflicts_env,
) -> None:
    client: httpx.AsyncClient = web_conflicts_env.client
    web_headers = _web_jwt_headers(project_id=PROJ_A)
    bff_base = f"/api/v1/projects/{PROJ_A}/conflicts"

    # Web JWT works on interactive /api/v1/conflicts
    assert (await client.get("/api/v1/conflicts", headers=web_headers)).status_code == 200

    # Same Web JWT without X-SAG-BFF-Service-Token MUST STILL be rejected (403) on BFF router
    assert (await client.get(bff_base, headers=web_headers)).status_code == 403
    assert (
        await client.post(f"{bff_base}/res-any/resolve", headers=web_headers, json={"action": "WINNER_B"})
    ).status_code == 403
    assert (
        await client.post(f"{bff_base}/res-any/reopen", headers=web_headers, json={})
    ).status_code == 403

    # Proper BFF service headers still succeed on BFF router
    assert (await client.get(bff_base, headers=_bff_headers(project_id=PROJ_A))).status_code == 200
