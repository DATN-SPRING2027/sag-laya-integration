"""Strict Pydantic v2 schemas for the Conflict Inbox & HITL Resolution REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
)


class ConflictSideOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_name: str
    version_id: str
    version_no: int | None = None
    unit_id: str
    section_path: str = ""
    page_from: int | None = None
    page_to: int | None = None
    text_snapshot: str = ""
    authority_tier: int | None = None
    effective_from: datetime | None = None
    security_partition_id: str


class ConflictItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    project_id: str
    security_partition_id: str
    winner_security_partition_id: str
    pair_key: str
    conflict_subtype: ConflictSubtype
    status: ConflictResolutionStatus
    resolution_mode: ConflictResolutionMode
    confidence: float = Field(ge=0.0, le=1.0)
    cosine_similarity: float | None = None
    effective_valid_to: datetime | None = None
    scope_difference_summary: str | None = None
    reason: str = ""
    degraded_fallback: bool = False
    graph_edge_id: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    audit_history: list[dict[str, Any]] = Field(default_factory=list)
    side_a: ConflictSideOut
    side_b: ConflictSideOut


class ConflictListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ConflictItemOut]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class ConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["WINNER_B", "WINNER_A", "KEEP_BOTH_SCOPED"]
    reason: str = Field(default="", max_length=2000)


class ConflictReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class ConflictActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str
    project_id: str
    security_partition_id: str
    winner_security_partition_id: str
    pair_key: str
    conflict_subtype: ConflictSubtype
    status: ConflictResolutionStatus
    resolution_mode: ConflictResolutionMode
    winner_document_id: str
    winner_version_id: str
    winner_unit_id: str
    loser_document_id: str
    loser_version_id: str
    loser_unit_id: str
    effective_valid_to: datetime | None = None
    reason: str = ""
    degraded_fallback: bool = False
    graph_edge_id: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    audit_history: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "ConflictActionResponse",
    "ConflictItemOut",
    "ConflictListResponse",
    "ConflictReopenRequest",
    "ConflictResolveRequest",
    "ConflictSideOut",
]
