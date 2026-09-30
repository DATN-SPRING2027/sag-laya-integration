"""Pydantic schemas for SAG Knowledge Routing RAG API (Phase 0 / Phase 1).

Complies with Section 9.1 and 9.2 of phase-0-contracts-and-foundations.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
