"""Pydantic schemas for SAG Knowledge Routing RAG API (Phase 0 / Phase 1).

Complies with Section 9.1 and 9.2 of phase-0-contracts-and-foundations.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentObjectIngestRequest(BaseModel):
    source_url: str = Field(..., min_length=8, max_length=4096)
    original_filename: str = Field(..., min_length=1, max_length=512)
    content_type: str = Field(default="application/octet-stream", max_length=256)
    expected_sha256: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$")
    expected_size_bytes: int = Field(..., ge=1)
    logical_source_id: str | None = Field(default=None, max_length=512)
    source_published_at: datetime | None = None


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


class DocumentReprocessRequest(BaseModel):
    source_url: str | None = Field(default=None, min_length=8, max_length=4096)
    original_filename: str | None = Field(default=None, min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=256)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    expected_size_bytes: int | None = Field(default=None, ge=1)
