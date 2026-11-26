"""Cross-document unit-level conflict resolution overlay ORM model (Migration 0015)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, UTCDateTime


def _new_uuid() -> str:
    return str(uuid.uuid4())


def compute_conflict_pair_key(ver_a: str, unit_a: str, ver_b: str, unit_b: str) -> str:
    """Compute order-independent SHA-256 pair key for two (version_id, unit_id) anchors."""
    ordered = sorted([f"{ver_a}:{unit_a}", f"{ver_b}:{unit_b}"])
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()


class KnowledgeConflictResolution(Base):
    """Single source of truth for cross-document conflict inbox and query-time supersession overlay.

    Intentionally holds NO foreign key to ``search_units`` so ``DELETE FROM search_units``
    during retry/reprocess/rebuild preserves HITL resolutions and never fails under
    SQLite ``PRAGMA foreign_keys=ON``. Document FKs are exact ``VARCHAR(36)`` to match
    ``_ensure_document_schema_widths`` without startup lock churn.
    """

    __tablename__ = "knowledge_conflict_resolutions"
    __table_args__ = (
        UniqueConstraint("project_id", "pair_key", name="uq_kcr_project_pair"),
        CheckConstraint("winner_unit_id <> loser_unit_id", name="ck_kcr_distinct_units"),
        CheckConstraint(
            "winner_version_id <> loser_version_id",
            name="ck_kcr_distinct_versions",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_kcr_confidence_range",
        ),
        CheckConstraint(
            "status IN ("
            "'PENDING_REVIEW', 'ACTIVE', 'DISMISSED_SCOPED', "
            "'REVOKED_WINNER', 'NEEDS_REVALIDATION', 'SUPERSEDED_BY_UPDATE'"
            ")",
            name="ck_kcr_status",
        ),
        CheckConstraint(
            "resolution_mode IN ("
            "'AUTO_POLICY', 'MANUAL_HITL', 'UNRESOLVED', 'KEEP_BOTH_SCOPED'"
            ")",
            name="ck_kcr_resolution_mode",
        ),
        CheckConstraint(
            "conflict_subtype IN ("
            "'DIRECT_SUPERSEDES', 'HARD_CONTRADICTION', 'SCOPED_EXCEPTION', "
            "'DIFFERENT_PERSPECTIVE', 'SUPPORTS_OR_EQUIVALENT'"
            ")",
            name="ck_kcr_conflict_subtype",
        ),
        Index(
            "idx_kcr_project_status_partition",
            "tenant_id",
            "project_id",
            "status",
            "security_partition_id",
        ),
        Index(
            "idx_kcr_loser_unit_status",
            "project_id",
            "loser_unit_id",
            "status",
        ),
        Index(
            "idx_kcr_winner_version_status",
            "winner_version_id",
            "status",
        ),
        Index(
            "idx_kcr_loser_version_status",
            "loser_version_id",
            "status",
        ),
        Index("idx_kcr_winner_document", "winner_document_id"),
        Index("idx_kcr_loser_document", "loser_document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    security_partition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    winner_security_partition_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_key: Mapped[str] = mapped_column(String(64), nullable=False)

    winner_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    winner_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    winner_unit_id: Mapped[str] = mapped_column(String(36), nullable=False)
    winner_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    loser_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    loser_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    loser_unit_id: Mapped[str] = mapped_column(String(36), nullable=False)
    loser_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    conflict_subtype: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default="PENDING_REVIEW",
        server_default=text("'PENDING_REVIEW'"),
        nullable=False,
    )
    resolution_mode: Mapped[str] = mapped_column(
        String(32),
        default="UNRESOLVED",
        server_default=text("'UNRESOLVED'"),
        nullable=False,
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        server_default=text("0.0"),
        nullable=False,
    )
    cosine_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    scope_difference_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default=text("''"),
        nullable=False,
    )
    degraded_fallback: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    graph_edge_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_graph_edges.id", ondelete="SET NULL"),
        nullable=True,
    )
    evidence_json: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        server_default=text("'{}'"),
        nullable=False,
    )
    audit_history_json: Mapped[list] = mapped_column(
        JSON,
        default=list,
        server_default=text("'[]'"),
        nullable=False,
    )
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
