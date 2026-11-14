"""Aggregate safe runtime, ingestion job, and corpus telemetry for Platform operators."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.db.models import Document, Job
from sag_api.enums import DocumentStatus, JobStatus
from sag_api.schemas.platform_operations import (
    PlatformAiOperationsOut,
    PlatformCorpusSummaryOut,
    PlatformJobSummaryOut,
    PlatformRuntimeProfileOut,
)

_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/+-]{1,128}$")
_FORBIDDEN_IDENTIFIER_RE = re.compile(
    r"(api_?key|secret|token|password|credential|bearer|://)",
    re.IGNORECASE,
)


def _safe_identifier(value: str | None, *, max_len: int = 128) -> str | None:
    """Project only bounded non-URL, non-secret configuration identifiers."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > max_len
        or not _SAFE_IDENTIFIER_RE.fullmatch(cleaned)
        or _FORBIDDEN_IDENTIFIER_RE.search(cleaned)
    ):
        return None
    return cleaned


async def read_platform_operations(
    session: AsyncSession,
    *,
    knowledge_runtime_ready: bool,
    qdrant_ready: bool | None,
) -> PlatformAiOperationsOut:
    """Return global deployment status, runtime identifiers, and enum counts without tenant data."""
    job_rows = (
        await session.execute(select(Job.status, func.count()).group_by(Job.status))
    ).all()
    job_counts = {status: int(count) for status, count in job_rows}

    corpus_rows = (
        await session.execute(
            select(Document.status, func.count())
            .where(Document.is_active.is_(True))
            .group_by(Document.status)
        )
    ).all()
    corpus_counts = {status: int(count) for status, count in corpus_rows}

    processing_count = (
        corpus_counts.get(DocumentStatus.PENDING, 0)
        + corpus_counts.get(DocumentStatus.LOADING, 0)
        + corpus_counts.get(DocumentStatus.EXTRACTING, 0)
        + corpus_counts.get(DocumentStatus.PAUSING, 0)
    )
    failed_corpus_count = corpus_counts.get(DocumentStatus.FAILED, 0) + corpus_counts.get(
        DocumentStatus.DELETE_FAILED, 0
    )

    overall_status = (
        "ready" if knowledge_runtime_ready and qdrant_ready is not False else "degraded"
    )

    return PlatformAiOperationsOut(
        status=overall_status,
        db_ready=True,
        knowledge_runtime_ready=knowledge_runtime_ready,
        qdrant_ready=qdrant_ready,
        runtime=PlatformRuntimeProfileOut(
            llm_configured=settings.llm_configured,
            llm_provider=_safe_identifier(settings.llm_provider),
            llm_model=_safe_identifier(settings.llm_model),
            retrieval_model=_safe_identifier(settings.embedding_model),
            vector_store=_safe_identifier(settings.sag_vector_provider),
            parser_provider=_safe_identifier(settings.effective_document_parser),
            worker_concurrency=max(1, int(settings.job_concurrency)),
        ),
        jobs=PlatformJobSummaryOut(
            queued=job_counts.get(JobStatus.QUEUED, 0),
            running=job_counts.get(JobStatus.RUNNING, 0),
            paused=job_counts.get(JobStatus.PAUSED, 0),
            succeeded=job_counts.get(JobStatus.SUCCEEDED, 0),
            failed=job_counts.get(JobStatus.FAILED, 0),
        ),
        corpus=PlatformCorpusSummaryOut(
            ready=corpus_counts.get(DocumentStatus.READY, 0),
            processing=processing_count,
            paused=corpus_counts.get(DocumentStatus.PAUSED, 0),
            failed=failed_corpus_count,
        ),
    )
