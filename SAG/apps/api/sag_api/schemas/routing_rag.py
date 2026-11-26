"""Pydantic schemas for SAG Knowledge Routing RAG API (Phase 0 / Phase 1).

Complies with Section 9.1 and 9.2 of phase-0-contracts-and-foundations.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sag_api.enums import normalize_authority_tier, normalize_scope_tags

_SENTINEL_EFFECTIVE_CUTOFF = datetime(9999, 1, 1, 0, 0, 0, tzinfo=UTC)


class DocumentUploadResponse(BaseModel):
    document_id: str
    version_no: int
    version_id: str
    run_id: str
    file_hash: str
    status: str
    search_status: str
    knowledge_status: str
    is_duplicate: bool


class StageProgressItem(BaseModel):
    status: str
    duration_ms: float = 0.0


class DocumentVersionStatusResponse(BaseModel):
    document_id: str
    version_no: int
    status: str
    search_status: str
    knowledge_status: str
    search_ready: bool
    knowledge_ready: bool
    current_stage: str
    stage_progress: dict[str, StageProgressItem] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    progress: int = 0
    document_status: str | None = None
    parser_provider: str | None = None
    parser_status: str | None = None
    chunk_count: int = 0
    event_count: int = 0
    token_usage: int = 0


class ProjectDocumentVersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)


class ProjectDocumentReadinessBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[ProjectDocumentVersionRef] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_versions(self) -> ProjectDocumentReadinessBatchRequest:
        refs = [(item.document_id, item.version_no) for item in self.documents]
        if len(refs) != len(set(refs)):
            raise ValueError("Duplicate document version references are not allowed")
        return self


class ProjectDocumentReadinessItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=1)
    found: bool
    status: str = Field(max_length=32)
    search_status: str = Field(max_length=32)
    search_ready: bool
    document_status: str | None = Field(default=None, max_length=32)
    chunk_count: int = Field(ge=0)


class ProjectDocumentReadinessBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified_at: datetime
    items: list[ProjectDocumentReadinessItem] = Field(max_length=100)


class DocumentExtractedChunkItem(BaseModel):
    chunk_id: str
    ordinal: int
    section_path: str = ""
    block_type: str = "paragraph"
    page_from: int = 1
    page_to: int = 1
    token_count: int = 0
    content: str


class DocumentVersionChunksResponse(BaseModel):
    document_id: str
    version_no: int
    source_stage: Literal["SEARCH_UNITS", "CANONICAL_BLOCKS", "EMPTY"] = "EMPTY"
    total_chunks: int = 0
    total_tokens: int = 0
    items: list[DocumentExtractedChunkItem] = Field(default_factory=list)


class DocumentRevokeRequest(BaseModel):
    logical_source_id: str | None = None
    document_id: str | None = None
    reason: str | None = None


class DocumentRevokeResponse(BaseModel):
    status: Literal["REVOKED", "ALREADY_REVOKED", "NOT_FOUND"]
    document_id: str | None = None


class DocumentMetadataPatchRequest(BaseModel):
    """Strict tri-state partial update payload for DocumentVersion authority/temporal metadata."""

    model_config = ConfigDict(extra="forbid")

    version_no: int | None = Field(default=None, ge=1)
    authority_tier: int | None = Field(default=None, ge=1, le=4)
    effective_from: datetime | None = None
    scope_tags: list[str] | None = Field(default=None, max_length=16)

    @field_validator("version_no", mode="before")
    @classmethod
    def _reject_bool_version_no(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("version_no cannot be a boolean")
        return value

    @field_validator("authority_tier", mode="before")
    @classmethod
    def _validate_authority_tier(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("authority_tier cannot be a boolean")
        if isinstance(value, (int, str)):
            return normalize_authority_tier(value)
        raise ValueError("authority_tier must be an integer between 1 and 4")

    @field_validator("effective_from", mode="before")
    @classmethod
    def _reject_bool_effective_from(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("effective_from cannot be a boolean")
        return value

    @field_validator("effective_from", mode="after")
    @classmethod
    def _normalize_effective_from(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if dt >= _SENTINEL_EFFECTIVE_CUTOFF:
            raise ValueError("effective_from must be earlier than 9999-01-01T00:00:00Z")
        return dt

    @field_validator("scope_tags", mode="before")
    @classmethod
    def _validate_scope_tags(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("scope_tags must be a list of strings")
        if len(value) > 16:
            raise ValueError("scope_tags cannot exceed 16 tags")
        normalized = normalize_scope_tags(value)
        return normalized or None

    @model_validator(mode="after")
    def _require_metadata_field(self) -> DocumentMetadataPatchRequest:
        if not (self.model_fields_set - {"version_no"}):
            raise ValueError(
                "At least one metadata field (authority_tier, effective_from, scope_tags) must be provided"
            )
        return self


class DocumentMetadataPatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    version_id: str
    version_no: int
    authority_tier: int | None = None
    effective_from: datetime | None = None
    scope_tags: list[str] | None = None
    valid_from: datetime
    valid_to: datetime
    temporal_recomputed: bool = False
    qdrant_payload_synced: bool = False
    degraded: bool = False
    degraded_reason: str | None = None
