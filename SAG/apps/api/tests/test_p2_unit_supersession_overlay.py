"""TDD tests for Phase 2 Wave 2.0 Foundation: shared ACL-aware unit_supersession_overlay.

Covers:
1. Active same-partition supersession in both loader & correlated EXISTS expression.
2. Cross-partition ACL isolation (Seam A — zero side-channel, partition drift guard).
3. All live winner invalidation conditions (Document inactive/blocked/hidden,
   DocumentVersion revoked/not-ready/unready-at/expired/future, SourceProjectMapping
   non-confirmed/wrong-org, deleted winner SearchUnit, disallowed source_id).
4. Temporal past_point(as_of) precision & NULL effective_valid_to handling.
5. Deterministic tie-breaking across multiple active winners targeting one loser unit.
6. Dual-side ACL gating for PENDING_REVIEW conflicts in load_active_conflict_unit_ids_for_scope.
7. Deterministic 16-char hex digest & ACL isolation in compute_scope_supersession_digest.
8. PostgreSQL + SQLite SQL compilation (no alias clashes in simple & joined outer queries).
9. Fail-closed ServiceUnavailableError on genuine SQLAlchemyError.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.errors import ServiceUnavailableError
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    KnowledgeConflictResolution,
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
)
from sag_api.services.unit_supersession_overlay import (
    SupersededUnitInfo,
    build_active_superseded_unit_exists,
    compute_scope_supersession_digest,
    load_active_conflict_unit_ids_for_scope,
    load_superseded_unit_ids_for_scope,
)

TENANT_ID = "tenant-alpha"
ORG_ID = "org-alpha"
PROJECT_ID = "proj-alpha"
P_PUBLIC = "part-public"
P_EXEC = "part-exec"
REF_TIME = datetime(2025, 7, 1, 12, 0, 0, tzinfo=UTC)
VALID_FROM = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
VALID_TO_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha256(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


async def _make_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _seed_unit_stack(
    session: AsyncSession,
    *,
    tag: str,
    source_id: str | None = None,
    partition_id: str = P_PUBLIC,
    version_partition_meta: str | None = None,
    unit_partition_id: str | None = None,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    org_id: str = ORG_ID,
    spm_state: str = "CONFIRMED",
    doc_is_active: bool = True,
    doc_status: DocumentStatus = DocumentStatus.READY,
    ver_status: str = "READY",
    ver_search_status: str = "SEARCH_READY",
    ver_search_ready_at: datetime | None = VALID_FROM,
    ver_valid_from: datetime = VALID_FROM,
    ver_valid_to: datetime = VALID_TO_FOREVER,
    create_unit: bool = True,
) -> dict[str, str]:
    """Create Source (+ Mapping), Document, DocumentVersion, CanonicalBlock, and SearchUnit."""
    src_id = source_id or f"src-{tag}"
    existing_src = await session.get(Source, src_id)
    if existing_src is None:
        session.add(
            Source(
                id=src_id,
                name=f"Source {tag}",
                sag_source_config_id=f"cfg-{src_id}",
            )
        )
        await session.flush()
        mapping_kwargs: dict = {
            "id": f"spm-{src_id}",
            "source_id": src_id,
            "organization_id": org_id,
            "project_id": project_id,
            "state": spm_state,
            "mapping_version": 1,
        }
        if spm_state == "CONFIRMED":
            mapping_kwargs.update(
                confirmed_at=VALID_FROM,
                confirmed_by="admin",
                approval_ref=f"appr-{src_id}",
            )
        elif spm_state == "REVOKED":
            mapping_kwargs.update(
                revoked_at=VALID_FROM,
                revoked_by="admin",
                revocation_ref=f"rev-{src_id}",
            )
        session.add(SourceProjectMapping(**mapping_kwargs))
        await session.flush()

    doc_id = f"doc-{tag}"
    ver_id = f"ver-{tag}"
    blk_id = f"blk-{tag}"
    unit_id = f"unit-{tag}"
    meta_part = partition_id if version_partition_meta is None else version_partition_meta
    su_part = partition_id if unit_partition_id is None else unit_partition_id

    session.add(
        Document(
            id=doc_id,
            tenant_id=tenant_id,
            project_id=project_id,
            source_id=src_id,
            filename=f"{tag}.md",
            storage_path=f"/tmp/{tag}.md",
            status=doc_status,
            is_active=doc_is_active,
        )
    )
    await session.flush()
    meta_json = {"security_partition_id": meta_part} if meta_part != "__OMIT__" else {}
    session.add(
        DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash=_sha256(f"file-{tag}"),
            valid_from=ver_valid_from,
            valid_to=ver_valid_to,
            status=ver_status,
            search_status=ver_search_status,
            search_ready_at=ver_search_ready_at,
            metadata_json=meta_json,
        )
    )
    await session.flush()
    session.add(
        CanonicalBlock(
            id=blk_id,
            document_version_id=ver_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Section 1",
            normalized_text=f"Body for {tag}",
            content_hash=_sha256(f"blk-{tag}"),
        )
    )
    await session.flush()
    if create_unit:
        session.add(
            SearchUnit(
                id=unit_id,
                document_version_id=ver_id,
                block_from_id=blk_id,
                block_to_id=blk_id,
                security_partition_id=su_part,
                content_hash=_sha256(f"unit-{tag}"),
                token_count=12,
                page_from=1,
                page_to=1,
                section_path="Section 1",
                text_snapshot=f"Body for {tag}",
            )
        )
        await session.flush()
    return {
        "source_id": src_id,
        "document_id": doc_id,
        "version_id": ver_id,
        "unit_id": unit_id,
    }


async def _seed_kcr(
    session: AsyncSession,
    *,
    res_id: str,
    winner: dict[str, str],
    loser: dict[str, str],
    loser_partition: str = P_PUBLIC,
    winner_partition: str = P_PUBLIC,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    status: str = ConflictResolutionStatus.ACTIVE.value,
    resolution_mode: str = ConflictResolutionMode.AUTO_POLICY.value,
    conflict_subtype: str = ConflictSubtype.DIRECT_SUPERSEDES.value,
    effective_valid_to: datetime | None = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC),
) -> KnowledgeConflictResolution:
    row = KnowledgeConflictResolution(
        id=res_id,
        tenant_id=tenant_id,
        project_id=project_id,
        security_partition_id=loser_partition,
        winner_security_partition_id=winner_partition,
        pair_key=compute_conflict_pair_key(
            winner["version_id"],
            winner["unit_id"],
            loser["version_id"],
            loser["unit_id"],
        ),
        winner_document_id=winner["document_id"],
        winner_version_id=winner["version_id"],
        winner_unit_id=winner["unit_id"],
        winner_content_hash=_sha256(f"w-{res_id}"),
        loser_document_id=loser["document_id"],
        loser_version_id=loser["version_id"],
        loser_unit_id=loser["unit_id"],
        loser_content_hash=_sha256(f"l-{res_id}"),
        conflict_subtype=conflict_subtype,
        status=status,
        resolution_mode=resolution_mode,
        confidence=0.95,
        effective_valid_to=effective_valid_to,
        reason="Policy updated",
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# 1. Active same-partition supersession & non-ACTIVE status filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_same_partition_supersession_hides_loser_in_loader_and_exists():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser = await _seed_unit_stack(session, tag="loser")
            winner = await _seed_unit_stack(session, tag="winner")
            other = await _seed_unit_stack(session, tag="other")
            eff_to = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
            await _seed_kcr(
                session,
                res_id="res-active-1",
                winner=winner,
                loser=loser,
                effective_valid_to=eff_to,
            )
            # Add a non-ACTIVE resolution against `other` to prove non-ACTIVE never suppresses.
            await _seed_kcr(
                session,
                res_id="res-pending-1",
                winner=winner,
                loser=other,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
                effective_valid_to=eff_to,
            )
            await session.commit()

            superseded = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser["version_id"], other["version_id"], winner["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert set(superseded.keys()) == {loser["unit_id"]}
            info = superseded[loser["unit_id"]]
            assert isinstance(info, SupersededUnitInfo)
            assert info == SupersededUnitInfo(
                loser_unit_id=loser["unit_id"],
                winner_unit_id=winner["unit_id"],
                winner_version_id=winner["version_id"],
                winner_document_id=winner["document_id"],
                effective_valid_to=eff_to,
                resolution_id="res-active-1",
            )

            exists_expr = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            visible_units = set(
                (await session.scalars(select(SearchUnit.id).where(~exists_expr))).all()
            )
            assert visible_units == {winner["unit_id"], other["unit_id"]}

            # Empty scope guards fail closed (no suppression).
            assert (
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=[],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )
                == {}
            )
            assert (
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=[loser["version_id"]],
                    tenant_ids=[],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )
                == {}
            )
            assert (
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=[loser["version_id"]],
                    tenant_ids=[TENANT_ID],
                    project_ids=[],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )
                == {}
            )
            assert (
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=[loser["version_id"]],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[],
                    reference_time=REF_TIME,
                )
                == {}
            )
            assert (
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=[loser["version_id"]],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                    allowed_source_ids=(),
                )
                == {}
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        ConflictResolutionStatus.PENDING_REVIEW.value,
        ConflictResolutionStatus.DISMISSED_SCOPED.value,
        ConflictResolutionStatus.REVOKED_WINNER.value,
        ConflictResolutionStatus.NEEDS_REVALIDATION.value,
        ConflictResolutionStatus.SUPERSEDED_BY_UPDATE.value,
    ],
)
async def test_non_active_statuses_never_suppress(status: str):
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser = await _seed_unit_stack(session, tag="l")
            winner = await _seed_unit_stack(session, tag="w")
            await _seed_kcr(session, res_id=f"res-{status}", winner=winner, loser=loser, status=status)
            await session.commit()

            superseded = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert superseded == {}

            exists_expr = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            visible = set((await session.scalars(select(SearchUnit.id).where(~exists_expr))).all())
            assert loser["unit_id"] in visible
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 2. Cross-partition ACL isolation (Seam A) & partition drift guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_partition_acl_isolation_seam_a_zero_side_channel():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser_pub = await _seed_unit_stack(session, tag="pub", partition_id=P_PUBLIC)
            winner_exec = await _seed_unit_stack(session, tag="exec", partition_id=P_EXEC)
            await _seed_kcr(
                session,
                res_id="res-xpart",
                winner=winner_exec,
                loser=loser_pub,
                loser_partition=P_PUBLIC,
                winner_partition=P_EXEC,
            )
            await session.commit()

            # Principal with {P_public, P_exec} sees winner -> loser IS suppressed.
            sup_exec = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_pub["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert set(sup_exec.keys()) == {loser_pub["unit_id"]}

            exists_exec = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            visible_exec = set(
                (await session.scalars(select(SearchUnit.id).where(~exists_exec))).all()
            )
            assert loser_pub["unit_id"] not in visible_exec
            assert winner_exec["unit_id"] in visible_exec

            # Principal with {P_public} ONLY -> zero cross-partition side-channel! Loser stays visible!
            sup_pub = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_pub["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert sup_pub == {}

            exists_pub = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            visible_pub = set(
                (
                    await session.scalars(
                        select(SearchUnit.id).where(
                            SearchUnit.security_partition_id == P_PUBLIC,
                            ~exists_pub,
                        )
                    )
                ).all()
            )
            assert visible_pub == {loser_pub["unit_id"]}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_winner_partition_metadata_or_unit_drift_revives_loser():
    """If winner_ver.metadata_json or winner SearchUnit partition drifts from kcr, fail closed."""
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser_a = await _seed_unit_stack(session, tag="la", partition_id=P_PUBLIC)
            # kcr claims winner is P_PUBLIC, but winner_ver metadata_json says P_EXEC
            winner_meta_drift = await _seed_unit_stack(
                session,
                tag="w-mdrift",
                partition_id=P_PUBLIC,
                version_partition_meta=P_EXEC,
                unit_partition_id=P_PUBLIC,
            )
            await _seed_kcr(
                session,
                res_id="res-mdrift",
                winner=winner_meta_drift,
                loser=loser_a,
                loser_partition=P_PUBLIC,
                winner_partition=P_PUBLIC,
            )

            loser_b = await _seed_unit_stack(session, tag="lb", partition_id=P_PUBLIC)
            # kcr + winner_ver say P_PUBLIC, but SearchUnit row says P_EXEC
            winner_su_drift = await _seed_unit_stack(
                session,
                tag="w-sdrift",
                partition_id=P_PUBLIC,
                version_partition_meta=P_PUBLIC,
                unit_partition_id=P_EXEC,
            )
            await _seed_kcr(
                session,
                res_id="res-sdrift",
                winner=winner_su_drift,
                loser=loser_b,
                loser_partition=P_PUBLIC,
                winner_partition=P_PUBLIC,
            )

            loser_c = await _seed_unit_stack(session, tag="lc", partition_id=P_PUBLIC)
            # winner_ver.metadata_json omits security_partition_id entirely
            winner_missing_meta = await _seed_unit_stack(
                session,
                tag="w-nometa",
                partition_id=P_PUBLIC,
                version_partition_meta="__OMIT__",
                unit_partition_id=P_PUBLIC,
            )
            await _seed_kcr(
                session,
                res_id="res-nometa",
                winner=winner_missing_meta,
                loser=loser_c,
                loser_partition=P_PUBLIC,
                winner_partition=P_PUBLIC,
            )
            await session.commit()

            # Even a principal with BOTH partitions sees zero suppression when metadata drifted.
            sup = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_a["version_id"], loser_b["version_id"], loser_c["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert sup == {}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. All live winner invalidation conditions immediately revive loser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "winner_overrides", "query_overrides"),
    [
        ("doc_inactive", {"doc_is_active": False}, {}),
        ("doc_deleting", {"doc_status": DocumentStatus.DELETING}, {}),
        ("doc_delete_failed", {"doc_status": DocumentStatus.DELETE_FAILED}, {}),
        ("doc_pausing", {"doc_status": DocumentStatus.PAUSING}, {}),
        ("doc_paused", {"doc_status": DocumentStatus.PAUSED}, {}),
        ("doc_failed", {"doc_status": DocumentStatus.FAILED}, {}),
        ("doc_hidden", {}, {"hide_winner_doc": True}),
        ("ver_revoked", {"ver_status": "REVOKED"}, {}),
        ("ver_search_pending", {"ver_search_status": "PENDING"}, {}),
        ("ver_search_ready_at_null", {"ver_search_ready_at": None}, {}),
        ("ver_expired", {"ver_valid_to": REF_TIME}, {}),
        ("ver_future", {"ver_valid_from": REF_TIME + timedelta(seconds=1)}, {}),
        ("spm_revoked", {"spm_state": "REVOKED"}, {}),
        ("spm_pending", {"spm_state": "PENDING"}, {}),
        ("spm_wrong_org", {"org_id": "other-org"}, {}),
        ("unit_deleted", {"create_unit": False}, {}),
        ("source_excluded", {}, {"exclude_winner_source": True}),
    ],
)
async def test_live_winner_invalidation_conditions_immediately_revive_loser(
    case_name: str,
    winner_overrides: dict,
    query_overrides: dict,
):
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser = await _seed_unit_stack(session, tag=f"l-{case_name}")
            winner = await _seed_unit_stack(session, tag=f"w-{case_name}", **winner_overrides)
            await _seed_kcr(
                session,
                res_id=f"res-{case_name}",
                winner=winner,
                loser=loser,
            )
            await session.commit()

            hidden_ids = [winner["document_id"]] if query_overrides.get("hide_winner_doc") else ()
            allowed_sources = (
                [loser["source_id"]] if query_overrides.get("exclude_winner_source") else None
            )

            superseded = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
                allowed_source_ids=allowed_sources,
                hidden_document_ids=hidden_ids,
            )
            assert superseded == {}, f"Expected loser revival for case={case_name}"

            exists_expr = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
                allowed_source_ids=allowed_sources,
                hidden_document_ids=hidden_ids,
            )
            visible = set((await session.scalars(select(SearchUnit.id).where(~exists_expr))).all())
            assert loser["unit_id"] in visible, f"Expected EXISTS revival for case={case_name}"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Temporal past_point(as_of) precision & NULL effective_valid_to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temporal_past_point_as_of_and_null_effective_valid_to():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser_dated = await _seed_unit_stack(session, tag="l-dated")
            winner_dated = await _seed_unit_stack(session, tag="w-dated")
            cutoff = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
            await _seed_kcr(
                session,
                res_id="res-dated",
                winner=winner_dated,
                loser=loser_dated,
                effective_valid_to=cutoff,
            )

            loser_null = await _seed_unit_stack(session, tag="l-null")
            winner_null = await _seed_unit_stack(
                session,
                tag="w-null",
                ver_valid_from=datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC),
            )
            await _seed_kcr(
                session,
                res_id="res-null",
                winner=winner_null,
                loser=loser_null,
                effective_valid_to=None,
            )
            await session.commit()

            # After cutoff (2025-07-01): BOTH suppressed.
            sup_after = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_dated["version_id"], loser_null["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=datetime(2025, 7, 1, 0, 0, 0, tzinfo=UTC),
                organization_id=ORG_ID,
            )
            assert set(sup_after.keys()) == {loser_dated["unit_id"], loser_null["unit_id"]}
            assert sup_after[loser_null["unit_id"]].effective_valid_to is None

            # Before cutoff (2025-03-15): dated loser stays visible; NULL-cutoff loser is suppressed.
            t_before = datetime(2025, 3, 15, 0, 0, 0, tzinfo=UTC)
            sup_before = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_dated["version_id"], loser_null["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=t_before,
                organization_id=ORG_ID,
            )
            assert set(sup_before.keys()) == {loser_null["unit_id"]}

            exists_before = build_active_superseded_unit_exists(
                SearchUnit.id,
                tenant_id=TENANT_ID,
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=t_before,
                organization_id=ORG_ID,
            )
            visible_before = set(
                (await session.scalars(select(SearchUnit.id).where(~exists_before))).all()
            )
            assert loser_dated["unit_id"] in visible_before
            assert loser_null["unit_id"] not in visible_before

            # Exact boundary with non-UTC offset (+07:00):
            # 2025-06-01T06:59:59+07:00 == 2025-05-31T23:59:59Z -> dated NOT suppressed.
            tz_plus7 = timezone(timedelta(hours=7))
            sup_1s_before = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_dated["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=datetime(2025, 6, 1, 6, 59, 59, tzinfo=tz_plus7),
                organization_id=ORG_ID,
            )
            assert sup_1s_before == {}

            # 2025-06-01T07:00:00+07:00 == 2025-06-01T00:00:00Z -> dated IS suppressed.
            sup_at_exact = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_dated["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=datetime(2025, 6, 1, 7, 0, 0, tzinfo=tz_plus7),
                organization_id=ORG_ID,
            )
            assert set(sup_at_exact.keys()) == {loser_dated["unit_id"]}

            # Before winner_null.valid_from (2025-01-15): even NULL effective_valid_to does not suppress!
            sup_pre_winner = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_null["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=datetime(2025, 1, 15, 0, 0, 0, tzinfo=UTC),
                organization_id=ORG_ID,
            )
            assert sup_pre_winner == {}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Deterministic tie-breaking across multiple active winners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_tie_breaking_multiple_active_winners():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            # Scenario A: different effective_valid_to -> earliest date wins even if ID sorts later.
            loser_a = await _seed_unit_stack(session, tag="ta-loser")
            w_a1 = await _seed_unit_stack(session, tag="ta-w1")
            w_a2 = await _seed_unit_stack(session, tag="ta-w2")
            t_later = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
            t_earlier = datetime(2025, 5, 1, 0, 0, 0, tzinfo=UTC)
            await _seed_kcr(
                session, res_id="res-001", winner=w_a1, loser=loser_a, effective_valid_to=t_later
            )
            await _seed_kcr(
                session, res_id="res-999", winner=w_a2, loser=loser_a, effective_valid_to=t_earlier
            )

            # Scenario B: NULL effective_valid_to beats any concrete date.
            loser_b = await _seed_unit_stack(session, tag="tb-loser")
            w_b1 = await _seed_unit_stack(session, tag="tb-w1")
            w_b2 = await _seed_unit_stack(session, tag="tb-w2")
            await _seed_kcr(
                session, res_id="res-000", winner=w_b1, loser=loser_b, effective_valid_to=t_earlier
            )
            await _seed_kcr(
                session, res_id="res-zzz", winner=w_b2, loser=loser_b, effective_valid_to=None
            )

            # Scenario C: identical effective_valid_to -> lexicographically smallest resolution_id wins.
            loser_c = await _seed_unit_stack(session, tag="tc-loser")
            w_c1 = await _seed_unit_stack(session, tag="tc-w1")
            w_c2 = await _seed_unit_stack(session, tag="tc-w2")
            await _seed_kcr(
                session, res_id="res-c-2", winner=w_c1, loser=loser_c, effective_valid_to=t_earlier
            )
            await _seed_kcr(
                session, res_id="res-c-1", winner=w_c2, loser=loser_c, effective_valid_to=t_earlier
            )
            await session.commit()

            sup = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[loser_a["version_id"], loser_b["version_id"], loser_c["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert sup[loser_a["unit_id"]].resolution_id == "res-999"
            assert sup[loser_a["unit_id"]].winner_unit_id == w_a2["unit_id"]

            assert sup[loser_b["unit_id"]].resolution_id == "res-zzz"
            assert sup[loser_b["unit_id"]].winner_unit_id == w_b2["unit_id"]
            assert sup[loser_b["unit_id"]].effective_valid_to is None

            assert sup[loser_c["unit_id"]].resolution_id == "res-c-1"
            assert sup[loser_c["unit_id"]].winner_unit_id == w_c2["unit_id"]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 6. load_active_conflict_unit_ids_for_scope (PENDING_REVIEW dual-side ACL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_active_conflict_unit_ids_for_scope_dual_side_acl():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            # Pair 1: Both in P_PUBLIC, PENDING_REVIEW -> both badged.
            u_a = await _seed_unit_stack(session, tag="ca", partition_id=P_PUBLIC)
            u_b = await _seed_unit_stack(session, tag="cb", partition_id=P_PUBLIC)
            await _seed_kcr(
                session,
                res_id="res-pend-pub",
                winner=u_a,
                loser=u_b,
                loser_partition=P_PUBLIC,
                winner_partition=P_PUBLIC,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
                conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
                effective_valid_to=None,
            )

            # Pair 2: Cross-partition (P_PUBLIC vs P_EXEC), PENDING_REVIEW.
            u_pub = await _seed_unit_stack(session, tag="cpub", partition_id=P_PUBLIC)
            u_exec = await _seed_unit_stack(session, tag="cexec", partition_id=P_EXEC)
            await _seed_kcr(
                session,
                res_id="res-pend-xpart",
                winner=u_exec,
                loser=u_pub,
                loser_partition=P_PUBLIC,
                winner_partition=P_EXEC,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
                conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
                effective_valid_to=None,
            )

            # Pair 3: Peer REVOKED -> must NOT badge live side.
            u_live = await _seed_unit_stack(session, tag="clive", partition_id=P_PUBLIC)
            u_rev = await _seed_unit_stack(
                session, tag="crev", partition_id=P_PUBLIC, ver_status="REVOKED"
            )
            await _seed_kcr(
                session,
                res_id="res-pend-rev",
                winner=u_live,
                loser=u_rev,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
                effective_valid_to=None,
            )

            # Pair 4: ACTIVE (not PENDING_REVIEW) -> NOT included in active conflict unit IDs.
            u_act_w = await _seed_unit_stack(session, tag="cactw", partition_id=P_PUBLIC)
            u_act_l = await _seed_unit_stack(session, tag="cactl", partition_id=P_PUBLIC)
            await _seed_kcr(
                session,
                res_id="res-act-not-pend",
                winner=u_act_w,
                loser=u_act_l,
                status=ConflictResolutionStatus.ACTIVE.value,
            )
            await session.commit()

            all_vids = [
                u_a["version_id"],
                u_b["version_id"],
                u_pub["version_id"],
                u_exec["version_id"],
                u_live["version_id"],
                u_rev["version_id"],
                u_act_w["version_id"],
                u_act_l["version_id"],
            ]

            # Public-only user: only Pair 1 badged; Pair 2 hidden (zero P_EXEC side-channel); Pair 3 & 4 excluded.
            pub_conflicts = await load_active_conflict_unit_ids_for_scope(
                session,
                version_ids=all_vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert pub_conflicts == frozenset({u_a["unit_id"], u_b["unit_id"]})

            # Exec user sees Pair 1 AND Pair 2; Pair 3 (revoked peer) & Pair 4 (ACTIVE) still excluded.
            exec_conflicts = await load_active_conflict_unit_ids_for_scope(
                session,
                version_ids=all_vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert exec_conflicts == frozenset(
                {u_a["unit_id"], u_b["unit_id"], u_pub["unit_id"], u_exec["unit_id"]}
            )

            # Scoped version_ids (only u_a's version): returns only u_a's unit (peer u_b is ACL-visible).
            narrow_vids = await load_active_conflict_unit_ids_for_scope(
                session,
                version_ids=[u_a["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert narrow_vids == frozenset({u_a["unit_id"]})

            # Scoped allowed_source_ids excluding peer u_b's source -> hides badge!
            narrow_src = await load_active_conflict_unit_ids_for_scope(
                session,
                version_ids=[u_a["version_id"], u_b["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
                allowed_source_ids=[u_a["source_id"]],
            )
            assert narrow_src == frozenset()

            # Hidden peer document -> hides badge!
            hidden_peer = await load_active_conflict_unit_ids_for_scope(
                session,
                version_ids=[u_a["version_id"], u_b["version_id"]],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
                hidden_document_ids=[u_b["document_id"]],
            )
            assert hidden_peer == frozenset()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 7. compute_scope_supersession_digest determinism, transitions & ACL split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_scope_supersession_digest_determinism_and_acl_isolation():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser_pub = await _seed_unit_stack(session, tag="dp-loser", partition_id=P_PUBLIC)
            winner_exec = await _seed_unit_stack(session, tag="dp-winner", partition_id=P_EXEC)
            await session.commit()

            vids = [loser_pub["version_id"], winner_exec["version_id"]]

            # Empty -> "none"
            d_empty = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert d_empty == "none"

            # Insert PENDING_REVIEW cross-partition row
            kcr = await _seed_kcr(
                session,
                res_id="res-digest-1",
                winner=winner_exec,
                loser=loser_pub,
                loser_partition=P_PUBLIC,
                winner_partition=P_EXEC,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
            )
            await session.commit()

            d_pending_pub = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            d_pending_exec = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert d_pending_pub == "none"
            assert re.fullmatch(r"[0-9a-f]{16}", d_pending_exec) is not None

            # Determinism across repeated calls
            d_pending_exec_2 = await compute_scope_supersession_digest(
                session,
                version_ids=list(reversed(vids)),
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_EXEC, P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert d_pending_exec_2 == d_pending_exec

            # Transition: PENDING_REVIEW -> ACTIVE
            kcr.status = ConflictResolutionStatus.ACTIVE.value
            await session.commit()

            d_active_pub = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            d_active_exec = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert d_active_pub == "none"
            assert re.fullmatch(r"[0-9a-f]{16}", d_active_exec) is not None
            assert d_active_exec != d_pending_exec

            # Changing effective_valid_to flips digest
            kcr.effective_valid_to = datetime(2025, 5, 1, 0, 0, 0, tzinfo=UTC)
            await session.commit()
            d_active_exec_shifted = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert re.fullmatch(r"[0-9a-f]{16}", d_active_exec_shifted) is not None
            assert d_active_exec_shifted != d_active_exec

            # Transition: ACTIVE -> DISMISSED_SCOPED -> back to "none"
            kcr.status = ConflictResolutionStatus.DISMISSED_SCOPED.value
            await session.commit()
            d_dismissed = await compute_scope_supersession_digest(
                session,
                version_ids=vids,
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[P_PUBLIC, P_EXEC],
                reference_time=REF_TIME,
                organization_id=ORG_ID,
            )
            assert d_dismissed == "none"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 8. PostgreSQL + SQLite SQL compilation (no alias clashes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_active_superseded_unit_exists_pg_and_sqlite_sql_compilation():
    exists_expr = build_active_superseded_unit_exists(
        SearchUnit.id,
        tenant_id=TENANT_ID,
        project_ids=[PROJECT_ID],
        allowed_partitions=[P_PUBLIC],
        reference_time=REF_TIME,
        organization_id=ORG_ID,
        allowed_source_ids=["src-1"],
        hidden_document_ids=["doc-hidden"],
    )

    # Simple outer query (MindMap unit_counts style)
    simple_stmt = select(SearchUnit.id).where(~exists_expr)

    # Full multi-table outer query (HandoverAssist ranked_subq style)
    exists_proj = build_active_superseded_unit_exists(
        SearchUnit.id,
        tenant_id=[TENANT_ID],
        project_ids=[PROJECT_ID],
        allowed_partitions=[P_PUBLIC],
        reference_time=REF_TIME,
        organization_id=ORG_ID,
    )
    joined_stmt = (
        select(SearchUnit.id, exists_proj.label("is_superseded"))
        .select_from(Document)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(SearchUnit, SearchUnit.document_version_id == DocumentVersion.id)
        .join(SourceProjectMapping, SourceProjectMapping.source_id == Document.source_id)
        .where(~exists_expr)
    )

    # Empty scope returns literal(False) and compiles cleanly when negated
    empty_exists = build_active_superseded_unit_exists(
        SearchUnit.id,
        tenant_id=TENANT_ID,
        project_ids=[],
        allowed_partitions=[P_PUBLIC],
        reference_time=REF_TIME,
    )
    empty_stmt = select(SearchUnit.id).where(~empty_exists)

    for dialect in (postgresql.dialect(), sqlite.dialect()):
        simple_sql = str(simple_stmt.compile(dialect=dialect))
        assert "EXISTS" in simple_sql.upper()
        assert "knowledge_conflict_resolutions" in simple_sql
        assert "document_versions" in simple_sql
        assert "documents" in simple_sql
        assert "source_project_mappings" in simple_sql
        assert "search_units" in simple_sql

        joined_sql = str(joined_stmt.compile(dialect=dialect))
        assert "EXISTS" in joined_sql.upper()
        assert "is_superseded" in joined_sql

        empty_sql = str(empty_stmt.compile(dialect=dialect))
        assert "knowledge_conflict_resolutions" not in empty_sql

    # Execute joined_stmt AND empty_stmt live against SQLite to verify zero alias clashes at runtime!
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            loser = await _seed_unit_stack(session, tag="comp-l", source_id="src-1")
            winner = await _seed_unit_stack(session, tag="comp-w", source_id="src-1")
            await _seed_kcr(session, res_id="res-comp", winner=winner, loser=loser)
            await session.commit()

            rows = (await session.execute(joined_stmt)).all()
            assert [(r.id, bool(r.is_superseded)) for r in rows] == [(winner["unit_id"], False)]

            # Also verify projection without ~exists_expr marks loser True and winner False
            proj_stmt = (
                select(SearchUnit.id, exists_proj.label("is_superseded"))
                .select_from(Document)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(SearchUnit, SearchUnit.document_version_id == DocumentVersion.id)
                .join(SourceProjectMapping, SourceProjectMapping.source_id == Document.source_id)
                .order_by(SearchUnit.id.asc())
            )
            proj_rows = (await session.execute(proj_stmt)).all()
            assert {r.id: bool(r.is_superseded) for r in proj_rows} == {
                loser["unit_id"]: True,
                winner["unit_id"]: False,
            }

            empty_rows = set((await session.scalars(empty_stmt)).all())
            assert empty_rows == {loser["unit_id"], winner["unit_id"]}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 9. Fail-closed ServiceUnavailableError on genuine SQLAlchemyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_error_fails_closed_with_service_unavailable():
    engine, factory = await _make_session_factory()
    try:
        async with factory() as session:
            # Drop knowledge_conflict_resolutions to trigger genuine OperationalError (SQLAlchemyError).
            await session.execute(text("DROP TABLE knowledge_conflict_resolutions"))
            await session.commit()

            with pytest.raises(ServiceUnavailableError):
                await load_superseded_unit_ids_for_scope(
                    session,
                    loser_version_ids=["ver-1"],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )

            with pytest.raises(ServiceUnavailableError):
                await load_active_conflict_unit_ids_for_scope(
                    session,
                    version_ids=["ver-1"],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )

            with pytest.raises(ServiceUnavailableError):
                await compute_scope_supersession_digest(
                    session,
                    version_ids=["ver-1"],
                    tenant_ids=[TENANT_ID],
                    project_ids=[PROJECT_ID],
                    allowed_partitions=[P_PUBLIC],
                    reference_time=REF_TIME,
                )
    finally:
        await engine.dispose()
