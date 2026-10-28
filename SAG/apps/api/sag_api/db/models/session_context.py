"""Database models for SAG-ECCE Session Context Provenance & Decision Ledger (Chặng 2).

Aligned 1:1 with `migrations/0009_session_context_ecce.sql` and compatible with both
PostgreSQL 16 (`TIMESTAMPTZ`, `JSON`, `DOUBLE PRECISION`) and SQLite (`PRAGMA foreign_keys=ON`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, UTCDateTime

SESSION_OPEN_ENDED_VALID_TO = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

SESSION_UNIT_KINDS = ("TURN", "WINDOW", "EPISODE")
SESSION_EPISTEMIC_STATUSES = (
    "FACT",
    "PROPOSED",
    "CONSIDERING",
    "CONFIRMED",
    "SUPERSEDED",
    "NEGATED",
    "OPEN_ISSUE",
)
SESSION_LEDGER_KINDS = ("DECISION", "OPEN_TASK", "CONSTRAINT", "OPEN_ISSUE")
SESSION_GRAPH_NODE_TYPES = ("UNIT", "SENTENCE", "ENTITY", "EPISODE", "SOURCE")
SESSION_GRAPH_EDGE_TYPES = (
    "ENTITY_MENTION",
    "SENTENCE_IN_UNIT",
    "ADJACENT_IN_EPISODE",
    "SUPERSEDES",
    "ALIAS_CANDIDATE",
    "CITES_DOCUMENT_SOURCE",
)


def generate_session_unit_id(
    thread_id: str,
    unit_kind: str,
    ordinal: int,
    message_from_id: str,
) -> str:
    """Generate deterministic UUIDv5 primary key for a derived `SessionUnit`."""
    raw = f"sag:session_unit:{thread_id}:{unit_kind}:{ordinal}:{message_from_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def generate_session_unit_point_id(collection_name: str, session_unit_id: str) -> str:
    """Generate deterministic UUIDv5 Qdrant point ID for `session_units_{project_id}`."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:qdrant:{collection_name}:{session_unit_id}"))


def generate_session_decision_id(session_unit_id: str, topic_slot_key: str) -> str:
    """Generate deterministic UUIDv5 primary key for a `SessionDecisionLedger` entry."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:session_decision:{session_unit_id}:{topic_slot_key}"))


def generate_session_graph_edge_id(
    source_unit_id: str,
    source_node_key: str,
    target_node_key: str,
    edge_type: str,
) -> str:
    """Generate deterministic UUIDv5 primary key for a provenance-anchored `SessionGraphEdge`."""
    raw = f"sag:session_edge:{source_unit_id}:{source_node_key}:{target_node_key}:{edge_type}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


class SessionUnit(Base):
    """Derived multi-granularity retrieval unit (`TURN | WINDOW | EPISODE`) linked to raw `messages`."""

    __tablename__ = "session_units"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "unit_kind",
            "ordinal",
            name="uq_session_units_thread_kind_ordinal",
        ),
        CheckConstraint("length(trim(tenant_id)) > 0", name="ck_session_units_tenant_id"),
        CheckConstraint("length(trim(project_id)) > 0", name="ck_session_units_project_id"),
        CheckConstraint(
            "unit_kind IN ('TURN', 'WINDOW', 'EPISODE')",
            name="ck_session_units_unit_kind",
        ),
        CheckConstraint("ordinal >= 0", name="ck_session_units_ordinal_nonnegative"),
        CheckConstraint("length(trim(episode_id)) > 0", name="ck_session_units_episode_id"),
        CheckConstraint(
            "episode_ordinal >= 0",
            name="ck_session_units_episode_ordinal_nonnegative",
        ),
        CheckConstraint(
            "epistemic_status IN ('FACT', 'PROPOSED', 'CONSIDERING', 'CONFIRMED', "
            "'SUPERSEDED', 'NEGATED', 'OPEN_ISSUE')",
            name="ck_session_units_epistemic_status",
        ),
        CheckConstraint("doc_word_len >= 0", name="ck_session_units_doc_word_len_nonnegative"),
        CheckConstraint("token_count >= 0", name="ck_session_units_token_count_nonnegative"),
        CheckConstraint("length(content_hash) = 64", name="ck_session_units_content_hash_len"),
        CheckConstraint(
            "length(trim(embedding_model_signature)) > 0",
            name="ck_session_units_embedding_sig",
        ),
        CheckConstraint("valid_to >= valid_from", name="ck_session_units_valid_window"),
        Index(
            "ix_session_units_project_thread",
            "tenant_id",
            "project_id",
            "thread_id",
            "unit_kind",
            "ordinal",
        ),
        Index("ix_session_units_thread_episode", "thread_id", "episode_id", "ordinal"),
        Index("ix_session_units_message_from", "message_from_id"),
        Index("ix_session_units_message_to", "message_to_id"),
        Index(
            "ix_session_units_temporal",
            "thread_id",
            "epistemic_status",
            "valid_from",
            "valid_to",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_kind: Mapped[str] = mapped_column(
        String(16),
        default="TURN",
        server_default="TURN",
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False)
    episode_ordinal: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_from_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_to_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    epistemic_status: Mapped[str] = mapped_column(
        String(24),
        default="FACT",
        server_default="FACT",
        nullable=False,
    )
    topic_slot_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_anaphoric_or_short: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    security_partition_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    cited_source_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    entity_mentions_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    sentences_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    term_freqs_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    doc_word_len: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    token_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model_signature: Mapped[str] = mapped_column(
        String(128),
        default="deterministic-ngram-v1",
        server_default="deterministic-ngram-v1",
        nullable=False,
    )
    qdrant_point_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    valid_to: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: SESSION_OPEN_ENDED_VALID_TO,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SessionDecisionLedger(Base):
    """Bi-temporal working & decision state ledger (`[valid_from, valid_to)`) with `SUPERSEDES` lineage."""

    __tablename__ = "session_decision_ledger"
    __table_args__ = (
        UniqueConstraint(
            "session_unit_id",
            "topic_slot_key",
            name="uq_session_decision_unit_slot",
        ),
        CheckConstraint("length(trim(tenant_id)) > 0", name="ck_session_decision_tenant_id"),
        CheckConstraint("length(trim(project_id)) > 0", name="ck_session_decision_project_id"),
        CheckConstraint(
            "ledger_kind IN ('DECISION', 'OPEN_TASK', 'CONSTRAINT', 'OPEN_ISSUE')",
            name="ck_session_decision_ledger_kind",
        ),
        CheckConstraint(
            "length(trim(topic_slot_key)) > 0",
            name="ck_session_decision_topic_slot_key",
        ),
        CheckConstraint(
            "turn_ordinal >= 0",
            name="ck_session_decision_turn_ordinal_nonnegative",
        ),
        CheckConstraint(
            "epistemic_status IN ('FACT', 'PROPOSED', 'CONSIDERING', 'CONFIRMED', "
            "'SUPERSEDED', 'NEGATED', 'OPEN_ISSUE')",
            name="ck_session_decision_epistemic_status",
        ),
        CheckConstraint("valid_to >= valid_from", name="ck_session_decision_valid_window"),
        Index(
            "ix_session_decision_thread_slot_status",
            "thread_id",
            "topic_slot_key",
            "epistemic_status",
            "turn_ordinal",
        ),
        Index(
            "ix_session_decision_project_status",
            "tenant_id",
            "project_id",
            "thread_id",
            "epistemic_status",
        ),
        Index("ix_session_decision_supersedes", "supersedes_id"),
        Index("ix_session_decision_superseded_by", "superseded_by_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_unit_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("session_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    ledger_kind: Mapped[str] = mapped_column(
        String(24),
        default="DECISION",
        server_default="DECISION",
        nullable=False,
    )
    topic_slot_key: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    epistemic_status: Mapped[str] = mapped_column(String(24), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    entity_keys_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    superseded_entity_keys_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    companion_unit_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("session_decision_ledger.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("session_decision_ledger.id", ondelete="SET NULL"),
        nullable=True,
    )
    supersedes_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    security_partition_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    cited_source_ids_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    valid_to: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=lambda: SESSION_OPEN_ENDED_VALID_TO,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SessionGraphEdge(Base):
    """Provenance-anchored graph edge (`Entity ↔ Sentence ↔ Unit`, `ADJACENT`, `SUPERSEDES`, `ALIAS`)."""

    __tablename__ = "session_graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_unit_id",
            "source_node_key",
            "target_node_key",
            "edge_type",
            name="uq_session_graph_edges_provenance",
        ),
        CheckConstraint("length(trim(tenant_id)) > 0", name="ck_session_graph_edges_tenant_id"),
        CheckConstraint("length(trim(project_id)) > 0", name="ck_session_graph_edges_project_id"),
        CheckConstraint(
            "source_node_type IN ('UNIT', 'SENTENCE', 'ENTITY', 'EPISODE', 'SOURCE')",
            name="ck_session_graph_edges_source_node_type",
        ),
        CheckConstraint(
            "target_node_type IN ('UNIT', 'SENTENCE', 'ENTITY', 'EPISODE', 'SOURCE')",
            name="ck_session_graph_edges_target_node_type",
        ),
        CheckConstraint(
            "length(trim(source_node_key)) > 0",
            name="ck_session_graph_edges_source_node_key",
        ),
        CheckConstraint(
            "length(trim(target_node_key)) > 0",
            name="ck_session_graph_edges_target_node_key",
        ),
        CheckConstraint(
            "edge_type IN ('ENTITY_MENTION', 'SENTENCE_IN_UNIT', 'ADJACENT_IN_EPISODE', "
            "'SUPERSEDES', 'ALIAS_CANDIDATE', 'CITES_DOCUMENT_SOURCE')",
            name="ck_session_graph_edges_edge_type",
        ),
        CheckConstraint("weight >= 0.0", name="ck_session_graph_edges_weight_nonnegative"),
        Index("ix_session_graph_edges_thread_type", "thread_id", "edge_type"),
        Index("ix_session_graph_edges_source_node", "thread_id", "source_node_key"),
        Index("ix_session_graph_edges_target_node", "thread_id", "target_node_key"),
        Index("ix_session_graph_edges_source_unit", "source_unit_id"),
        Index("ix_session_graph_edges_target_unit", "target_unit_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(256), nullable=False)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False)
    thread_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_unit_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("session_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_unit_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("session_units.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_node_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_node_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_node_key: Mapped[str] = mapped_column(String(256), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        server_default="1.0",
        nullable=False,
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.now(),
        nullable=False,
    )
