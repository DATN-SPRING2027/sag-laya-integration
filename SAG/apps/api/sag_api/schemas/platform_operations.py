"""Strict output schemas for service-only platform AI operations telemetry."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlatformRuntimeProfileOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_configured: bool
    llm_provider: str | None = None
    llm_model: str | None = None
    retrieval_model: str | None = None
    vector_store: str | None = None
    parser_provider: str | None = None
    worker_concurrency: int = Field(ge=1)


class PlatformJobSummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    paused: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)


class PlatformCorpusSummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: int = Field(ge=0)
    processing: int = Field(ge=0)
    paused: int = Field(ge=0)
    failed: int = Field(ge=0)


class PlatformAiOperationsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "degraded"]
    db_ready: bool
    knowledge_runtime_ready: bool
    qdrant_ready: bool | None
    runtime: PlatformRuntimeProfileOut
    jobs: PlatformJobSummaryOut
    corpus: PlatformCorpusSummaryOut
