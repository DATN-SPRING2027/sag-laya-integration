"""Rebuildable session features and explicit message provenance, separate from raw chat."""

from __future__ import annotations

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, TimestampMixin


class SessionContextIndex(TimestampMixin, Base):
    __tablename__ = "session_context_indexes"
    __table_args__ = (
        UniqueConstraint("thread_id", "owner_key", "scope_fingerprint", "model_id", name="uq_session_context_scope"),
        CheckConstraint("state IN ('indexing','ready','stale')", name="ck_session_context_state"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), index=True)
    owner_key: Mapped[str] = mapped_column(String(64))
    scope_fingerprint: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(512))
    generation: Mapped[str] = mapped_column(String(64))
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="indexing")
    checkpoint: Mapped[dict] = mapped_column("checkpoint_json", JSON, default=dict)
    features: Mapped[dict] = mapped_column("features_json", JSON, default=dict)


class SessionMessageProvenance(TimestampMixin, Base):
    __tablename__ = "session_message_provenance"
    __table_args__ = (CheckConstraint("origin IN ('recorded','unknown')", name="ck_session_provenance_origin"),)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    partition_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ids: Mapped[list] = mapped_column("source_ids_json", JSON, default=list)
    dependencies: Mapped[list] = mapped_column("dependencies_json", JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="unknown")
