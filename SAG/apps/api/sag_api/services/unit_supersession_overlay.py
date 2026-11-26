"""Shared ACL-aware read-only overlay for unit-level conflict supersessions.

Single source of truth consumed identically by Retrieval, MindMap, and HandoverAssist.
Every read path enforces live winner (and, for PENDING_REVIEW conflicts, dual-side)
ACL, partition, SourceProjectMapping, Document, DocumentVersion, and SearchUnit gates
so cross-partition or revoked/deleted winners NEVER create side-channels or ghost
suppression.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.logging import get_logger
from sag_api.db.models import (
    Document,
    DocumentVersion,
    Job,
    KnowledgeConflictResolution,
    SearchUnit,
    SourceProjectMapping,
)
from sag_api.enums import ConflictResolutionStatus, DocumentStatus, JobStatus, JobType

log = get_logger("unit_supersession_overlay")

_READY_SEARCH_STATUSES: tuple[str, ...] = ("READY", "SEARCH_READY")
_BLOCKED_DOCUMENT_STATUSES: tuple[DocumentStatus, ...] = (
    DocumentStatus.DELETING,
    DocumentStatus.DELETE_FAILED,
    DocumentStatus.PAUSING,
    DocumentStatus.PAUSED,
    DocumentStatus.FAILED,
)
_MIN_UTC = datetime.min.replace(tzinfo=UTC)

IdCollection = Sequence[str] | set[str] | frozenset[str]


@dataclass(frozen=True, slots=True)
class SupersededUnitInfo:
    """Resolved active unit-level supersession visible to the current caller scope."""

    loser_unit_id: str
    winner_unit_id: str
    winner_version_id: str
    winner_document_id: str
    effective_valid_to: datetime | None
    resolution_id: str


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


def _normalize_id_list(values: str | IdCollection) -> list[str]:
    if isinstance(values, str):
        return [values] if values else []
    return sorted({value for value in values if isinstance(value, str) and value})


def _tie_break_key(info: SupersededUnitInfo) -> tuple[datetime, str]:
    return (info.effective_valid_to or _MIN_UTC, info.resolution_id)


def _build_live_side_predicates(
    *,
    ver_alias: Any,
    doc_alias: Any,
    spm_alias: Any,
    kcr_document_id_col: Any,
    kcr_unit_id_col: Any,
    kcr_partition_col: Any,
    kcr_tenant_col: Any,
    kcr_project_col: Any,
    partition_ids: list[str],
    ref_utc: datetime,
    organization_id: str | None,
    allowed_source_ids: list[str] | None,
    hidden_document_ids: list[str],
    exclude_in_flight_control_jobs: bool = False,
) -> list[ColumnElement[bool]]:
    """Build live ACL, temporal, mapping, partition, and SearchUnit gates for one side."""
    partition_expr = ver_alias.metadata_json["security_partition_id"].as_string()
    unit_alias = aliased(SearchUnit)
    unit_exists = (
        select(unit_alias.id)
        .where(
            unit_alias.id == kcr_unit_id_col,
            unit_alias.document_version_id == ver_alias.id,
            unit_alias.security_partition_id.in_(partition_ids),
            unit_alias.security_partition_id == partition_expr,
        )
        .exists()
    )

    predicates: list[ColumnElement[bool]] = [
        ver_alias.status != "REVOKED",
        ver_alias.search_status.in_(_READY_SEARCH_STATUSES),
        ver_alias.search_ready_at.is_not(None),
        ver_alias.valid_from <= ref_utc,
        ver_alias.valid_to > ref_utc,
        doc_alias.id == kcr_document_id_col,
        doc_alias.tenant_id == kcr_tenant_col,
        doc_alias.project_id == kcr_project_col,
        doc_alias.is_active.is_(True),
        doc_alias.status.notin_(_BLOCKED_DOCUMENT_STATUSES),
        spm_alias.project_id == doc_alias.project_id,
        spm_alias.state == "CONFIRMED",
        partition_expr == kcr_partition_col,
        partition_expr.in_(partition_ids),
        unit_exists,
    ]
    if hidden_document_ids:
        predicates.append(doc_alias.id.notin_(hidden_document_ids))
    if exclude_in_flight_control_jobs:
        job_alias = aliased(Job)
        in_flight_control_doc_ids = select(job_alias.document_id).where(
            job_alias.document_id.is_not(None),
            job_alias.type.in_((JobType.DELETE_DOCUMENT, JobType.REPROCESS_DOCUMENT)),
            job_alias.status.in_((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)),
        )
        predicates.append(doc_alias.id.notin_(in_flight_control_doc_ids))
    if allowed_source_ids is not None:
        predicates.append(doc_alias.source_id.in_(allowed_source_ids))
    if organization_id is not None:
        predicates.append(spm_alias.organization_id == organization_id)
    return predicates


async def load_superseded_unit_ids_for_scope(
    session: AsyncSession,
    *,
    loser_version_ids: IdCollection,
    tenant_ids: IdCollection,
    project_ids: IdCollection,
    allowed_partitions: IdCollection,
    reference_time: datetime,
    organization_id: str | None = None,
    allowed_source_ids: IdCollection | None = None,
    hidden_document_ids: IdCollection = (),
) -> dict[str, SupersededUnitInfo]:
    """Load ACL-verified ACTIVE unit supersessions keyed by ``loser_unit_id``."""
    norm_loser_versions = _normalize_id_list(loser_version_ids)
    norm_tenants = _normalize_id_list(tenant_ids)
    norm_projects = _normalize_id_list(project_ids)
    norm_partitions = _normalize_id_list(allowed_partitions)
    if not norm_loser_versions or not norm_tenants or not norm_projects or not norm_partitions:
        return {}

    norm_sources: list[str] | None = None
    if allowed_source_ids is not None:
        norm_sources = _normalize_id_list(allowed_source_ids)
        if not norm_sources:
            return {}
    norm_hidden = _normalize_id_list(hidden_document_ids)
    ref_utc = _as_utc(reference_time)

    kcr = aliased(KnowledgeConflictResolution)
    winner_ver = aliased(DocumentVersion)
    winner_doc = aliased(Document)
    winner_spm = aliased(SourceProjectMapping)

    stmt = (
        select(
            kcr.id.label("resolution_id"),
            kcr.loser_unit_id.label("loser_unit_id"),
            kcr.winner_unit_id.label("winner_unit_id"),
            kcr.winner_version_id.label("winner_version_id"),
            kcr.winner_document_id.label("winner_document_id"),
            kcr.effective_valid_to.label("effective_valid_to"),
        )
        .select_from(kcr)
        .join(winner_ver, winner_ver.id == kcr.winner_version_id)
        .join(winner_doc, winner_doc.id == winner_ver.document_id)
        .join(winner_spm, winner_spm.source_id == winner_doc.source_id)
        .where(
            kcr.loser_version_id.in_(norm_loser_versions),
            kcr.status == ConflictResolutionStatus.ACTIVE.value,
            or_(kcr.effective_valid_to.is_(None), kcr.effective_valid_to <= ref_utc),
            kcr.tenant_id.in_(norm_tenants),
            kcr.project_id.in_(norm_projects),
            kcr.security_partition_id.in_(norm_partitions),
            kcr.winner_security_partition_id.in_(norm_partitions),
            *_build_live_side_predicates(
                ver_alias=winner_ver,
                doc_alias=winner_doc,
                spm_alias=winner_spm,
                kcr_document_id_col=kcr.winner_document_id,
                kcr_unit_id_col=kcr.winner_unit_id,
                kcr_partition_col=kcr.winner_security_partition_id,
                kcr_tenant_col=kcr.tenant_id,
                kcr_project_col=kcr.project_id,
                partition_ids=norm_partitions,
                ref_utc=ref_utc,
                organization_id=organization_id,
                allowed_source_ids=norm_sources,
                hidden_document_ids=norm_hidden,
            ),
        )
        .order_by(kcr.id.asc())
    )

    try:
        rows = (await session.execute(stmt)).all()
    except SQLAlchemyError as error:
        log.warning(
            "unit supersession overlay lookup unavailable error_type=%s",
            type(error).__name__,
        )
        raise ServiceUnavailableError("Unit supersession overlay lookup is unavailable") from error

    superseded: dict[str, SupersededUnitInfo] = {}
    for row in rows:
        info = SupersededUnitInfo(
            loser_unit_id=str(row.loser_unit_id),
            winner_unit_id=str(row.winner_unit_id),
            winner_version_id=str(row.winner_version_id),
            winner_document_id=str(row.winner_document_id),
            effective_valid_to=_as_utc_optional(row.effective_valid_to),
            resolution_id=str(row.resolution_id),
        )
        existing = superseded.get(info.loser_unit_id)
        if existing is None or _tie_break_key(info) < _tie_break_key(existing):
            superseded[info.loser_unit_id] = info
    return superseded


async def load_active_conflict_unit_ids_for_scope(
    session: AsyncSession,
    *,
    version_ids: IdCollection,
    tenant_ids: IdCollection,
    project_ids: IdCollection,
    allowed_partitions: IdCollection,
    reference_time: datetime,
    organization_id: str | None = None,
    allowed_source_ids: IdCollection | None = None,
    hidden_document_ids: IdCollection = (),
) -> frozenset[str]:
    """Load unit IDs in ``version_ids`` participating in dual-ACL-visible PENDING_REVIEW conflicts."""
    norm_versions = _normalize_id_list(version_ids)
    norm_tenants = _normalize_id_list(tenant_ids)
    norm_projects = _normalize_id_list(project_ids)
    norm_partitions = _normalize_id_list(allowed_partitions)
    if not norm_versions or not norm_tenants or not norm_projects or not norm_partitions:
        return frozenset()

    norm_sources: list[str] | None = None
    if allowed_source_ids is not None:
        norm_sources = _normalize_id_list(allowed_source_ids)
        if not norm_sources:
            return frozenset()
    norm_hidden = _normalize_id_list(hidden_document_ids)
    ref_utc = _as_utc(reference_time)

    kcr = aliased(KnowledgeConflictResolution)
    winner_ver = aliased(DocumentVersion)
    winner_doc = aliased(Document)
    winner_spm = aliased(SourceProjectMapping)
    loser_ver = aliased(DocumentVersion)
    loser_doc = aliased(Document)
    loser_spm = aliased(SourceProjectMapping)

    stmt = (
        select(
            kcr.winner_version_id.label("winner_version_id"),
            kcr.winner_unit_id.label("winner_unit_id"),
            kcr.loser_version_id.label("loser_version_id"),
            kcr.loser_unit_id.label("loser_unit_id"),
        )
        .select_from(kcr)
        .join(winner_ver, winner_ver.id == kcr.winner_version_id)
        .join(winner_doc, winner_doc.id == winner_ver.document_id)
        .join(winner_spm, winner_spm.source_id == winner_doc.source_id)
        .join(loser_ver, loser_ver.id == kcr.loser_version_id)
        .join(loser_doc, loser_doc.id == loser_ver.document_id)
        .join(loser_spm, loser_spm.source_id == loser_doc.source_id)
        .where(
            kcr.status == ConflictResolutionStatus.PENDING_REVIEW.value,
            or_(
                kcr.loser_version_id.in_(norm_versions),
                kcr.winner_version_id.in_(norm_versions),
            ),
            kcr.tenant_id.in_(norm_tenants),
            kcr.project_id.in_(norm_projects),
            kcr.security_partition_id.in_(norm_partitions),
            kcr.winner_security_partition_id.in_(norm_partitions),
            *_build_live_side_predicates(
                ver_alias=winner_ver,
                doc_alias=winner_doc,
                spm_alias=winner_spm,
                kcr_document_id_col=kcr.winner_document_id,
                kcr_unit_id_col=kcr.winner_unit_id,
                kcr_partition_col=kcr.winner_security_partition_id,
                kcr_tenant_col=kcr.tenant_id,
                kcr_project_col=kcr.project_id,
                partition_ids=norm_partitions,
                ref_utc=ref_utc,
                organization_id=organization_id,
                allowed_source_ids=norm_sources,
                hidden_document_ids=norm_hidden,
            ),
            *_build_live_side_predicates(
                ver_alias=loser_ver,
                doc_alias=loser_doc,
                spm_alias=loser_spm,
                kcr_document_id_col=kcr.loser_document_id,
                kcr_unit_id_col=kcr.loser_unit_id,
                kcr_partition_col=kcr.security_partition_id,
                kcr_tenant_col=kcr.tenant_id,
                kcr_project_col=kcr.project_id,
                partition_ids=norm_partitions,
                ref_utc=ref_utc,
                organization_id=organization_id,
                allowed_source_ids=norm_sources,
                hidden_document_ids=norm_hidden,
            ),
        )
    )

    try:
        rows = (await session.execute(stmt)).all()
    except SQLAlchemyError as error:
        log.warning(
            "active conflict overlay lookup unavailable error_type=%s",
            type(error).__name__,
        )
        raise ServiceUnavailableError("Active conflict overlay lookup is unavailable") from error

    version_id_set = frozenset(norm_versions)
    unit_ids: set[str] = set()
    for row in rows:
        if str(row.winner_version_id) in version_id_set:
            unit_ids.add(str(row.winner_unit_id))
        if str(row.loser_version_id) in version_id_set:
            unit_ids.add(str(row.loser_unit_id))
    return frozenset(unit_ids)


def build_active_superseded_unit_exists(
    search_unit_id_col: ColumnElement[str],
    *,
    tenant_id: str | IdCollection,
    project_ids: IdCollection,
    allowed_partitions: IdCollection,
    reference_time: datetime,
    organization_id: str | None = None,
    allowed_source_ids: IdCollection | None = None,
    hidden_document_ids: IdCollection = (),
    exclude_in_flight_control_jobs: bool = False,
) -> ColumnElement[bool]:
    """Build a correlated SQL EXISTS clause verifying an ACL-visible ACTIVE supersession."""
    norm_tenants = _normalize_id_list(tenant_id)
    norm_projects = _normalize_id_list(project_ids)
    norm_partitions = _normalize_id_list(allowed_partitions)
    if not norm_tenants or not norm_projects or not norm_partitions:
        return literal(False)

    norm_sources: list[str] | None = None
    if allowed_source_ids is not None:
        norm_sources = _normalize_id_list(allowed_source_ids)
        if not norm_sources:
            return literal(False)
    norm_hidden = _normalize_id_list(hidden_document_ids)
    ref_utc = _as_utc(reference_time)

    kcr = aliased(KnowledgeConflictResolution)
    winner_ver = aliased(DocumentVersion)
    winner_doc = aliased(Document)
    winner_spm = aliased(SourceProjectMapping)

    return (
        select(literal(1))
        .select_from(kcr)
        .join(winner_ver, winner_ver.id == kcr.winner_version_id)
        .join(winner_doc, winner_doc.id == winner_ver.document_id)
        .join(winner_spm, winner_spm.source_id == winner_doc.source_id)
        .where(
            kcr.loser_unit_id == search_unit_id_col,
            kcr.status == ConflictResolutionStatus.ACTIVE.value,
            or_(kcr.effective_valid_to.is_(None), kcr.effective_valid_to <= ref_utc),
            kcr.tenant_id.in_(norm_tenants),
            kcr.project_id.in_(norm_projects),
            kcr.security_partition_id.in_(norm_partitions),
            kcr.winner_security_partition_id.in_(norm_partitions),
            *_build_live_side_predicates(
                ver_alias=winner_ver,
                doc_alias=winner_doc,
                spm_alias=winner_spm,
                kcr_document_id_col=kcr.winner_document_id,
                kcr_unit_id_col=kcr.winner_unit_id,
                kcr_partition_col=kcr.winner_security_partition_id,
                kcr_tenant_col=kcr.tenant_id,
                kcr_project_col=kcr.project_id,
                partition_ids=norm_partitions,
                ref_utc=ref_utc,
                organization_id=organization_id,
                allowed_source_ids=norm_sources,
                hidden_document_ids=norm_hidden,
                exclude_in_flight_control_jobs=exclude_in_flight_control_jobs,
            ),
        )
        .exists()
    )


async def compute_scope_supersession_digest(
    session: AsyncSession,
    *,
    version_ids: IdCollection,
    tenant_ids: IdCollection,
    project_ids: IdCollection,
    allowed_partitions: IdCollection,
    reference_time: datetime,
    organization_id: str | None = None,
    allowed_source_ids: IdCollection | None = None,
    hidden_document_ids: IdCollection = (),
) -> str:
    """Compute a deterministic 16-char hex digest (or ``'none'``) for cache/fingerprint keying."""
    superseded = await load_superseded_unit_ids_for_scope(
        session,
        loser_version_ids=version_ids,
        tenant_ids=tenant_ids,
        project_ids=project_ids,
        allowed_partitions=allowed_partitions,
        reference_time=reference_time,
        organization_id=organization_id,
        allowed_source_ids=allowed_source_ids,
        hidden_document_ids=hidden_document_ids,
    )
    conflict_unit_ids = await load_active_conflict_unit_ids_for_scope(
        session,
        version_ids=version_ids,
        tenant_ids=tenant_ids,
        project_ids=project_ids,
        allowed_partitions=allowed_partitions,
        reference_time=reference_time,
        organization_id=organization_id,
        allowed_source_ids=allowed_source_ids,
        hidden_document_ids=hidden_document_ids,
    )
    if not superseded and not conflict_unit_ids:
        return "none"

    canonical_payload = {
        "superseded": [
            [
                info.loser_unit_id,
                info.winner_unit_id,
                info.winner_version_id,
                info.winner_document_id,
                info.effective_valid_to.isoformat() if info.effective_valid_to is not None else "",
                info.resolution_id,
            ]
            for info in sorted(
                superseded.values(),
                key=lambda item: (item.loser_unit_id, item.resolution_id, item.winner_unit_id),
            )
        ],
        "conflicts": sorted(conflict_unit_ids),
    }
    raw = json.dumps(canonical_payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "SupersededUnitInfo",
    "build_active_superseded_unit_exists",
    "compute_scope_supersession_digest",
    "load_active_conflict_unit_ids_for_scope",
    "load_superseded_unit_ids_for_scope",
]
