"""Document domain logic: upload persistence -> registration -> enqueue processing."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.core.identity import (
    compute_payload_hash,
    generate_doc_id,
    generate_version_id,
    normalize_idempotency_key,
)
from sag_api.db.base import new_id
from sag_api.db.models import Document, Job, Source
from sag_api.db.models.routing_rag import (
    DocumentVersion,
    IngestionRun,
    SourceSnapshot,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import JobQueue
from sag_api.jobs.scheduling import DELETE_PRIORITY, RESUME_PRIORITY, set_scheduler
from sag_api.schemas.routing_rag import (
    DocumentUploadResponse,
    DocumentVersionStatusResponse,
    StageProgressItem,
)
from sag_api.services.source_operation_service import touch_source_revision


async def _enqueue_persisted_job(job_queue: JobQueue, job_id: str) -> None:
    """Dispatch a committed job with queue-level retry supervision when available."""
    durable = getattr(job_queue, "enqueue_durably", None)
    if callable(durable):
        await durable(job_id)
        return
    await job_queue.enqueue(job_id)


async def list_documents(session: AsyncSession, source_id: str) -> list[Document]:
    rows = await session.execute(
        select(Document)
        .where(
            Document.source_id == source_id,
            Document.is_active.is_(True),
            Document.status.not_in([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
        )
        .order_by(Document.created_at.desc())
    )
    return list(rows.scalars().all())


async def get_document(session: AsyncSession, source: Source, document_id: str) -> Document:
    doc = await session.get(Document, document_id)
    if doc is None or doc.source_id != source.id or not doc.is_active:
        raise NotFoundError("Document not found")
    return doc


async def get_public_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
) -> Document:
    """Resolve a document for public reads after the logical-delete barrier."""
    document = await get_document(session, source, document_id)
    if document.status in {
        DocumentStatus.DELETING,
        DocumentStatus.DELETE_FAILED,
    }:
        raise NotFoundError("Document not found")
    return document


async def create_document_from_upload(
    session: AsyncSession,
    source: Source,
    *,
    filename: str,
    content_type: str,
    data: bytes,
    upload_dir: str,
    job_queue: JobQueue,
) -> tuple[Document, Job]:
    doc_id = new_id()
    safe_name = os.path.basename(filename) or "upload"
    dest_dir = os.path.join(upload_dir, source.id)
    os.makedirs(dest_dir, exist_ok=True)
    storage_path = os.path.join(dest_dir, f"{doc_id}_{safe_name}")
    with open(storage_path, "wb") as f:
        f.write(data)

    document = Document(
        id=doc_id,
        tenant_id="default",
        project_id=source.id,
        owner_id="system",
        logical_source_id=safe_name,
        source_id=source.id,
        filename=safe_name,
        content_type=content_type or "application/octet-stream",
        size_bytes=len(data),
        storage_path=storage_path,
        status=DocumentStatus.PENDING,
    )
    session.add(document)
    await session.execute(update(Source).where(Source.id == source.id).values(document_count=Source.document_count + 1))
    job = Job(
        type=JobType.PROCESS_DOCUMENT,
        source_id=source.id,
        document_id=doc_id,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    await touch_source_revision(session, source.id)
    await session.commit()
    await session.refresh(document)
    await session.refresh(job)

    await _enqueue_persisted_job(job_queue, job.id)
    return document, job


def _format_messages(messages: list[dict]) -> str:
    lines = ["# Messages", ""]
    for m in messages:
        who = m.get("author") or m.get("role") or "Message"
        ts = f" ({m['ts']})" if m.get("ts") else ""
        lines.append(f"**{who}**{ts}: {m.get('text') or ''}")
    return "\n\n".join(lines)


async def ingest_content(
    session: AsyncSession,
    source: Source,
    *,
    text: str | None = None,
    title: str | None = None,
    messages: list[dict] | None = None,
    upload_dir: str,
    job_queue: JobQueue,
) -> Document:
    """Unified ingestion: normalize text or a batch of messages into a document -> reuse ingest/extract pipeline (continuous ingestion)."""
    from sag_api.core.errors import ValidationError

    if messages:
        content = _format_messages(messages)
        filename = f"{title or f'messages-{len(messages)}'}.md"
    elif text:
        content = (f"# {title}\n\n" if title else "") + text
        filename = f"{title or 'text'}.md"
    else:
        raise ValidationError("Please provide text or messages")

    document, _job = await create_document_from_upload(
        session,
        source,
        filename=filename,
        content_type="text/markdown",
        data=content.encode("utf-8"),
        upload_dir=upload_dir,
        job_queue=job_queue,
    )
    return document


async def reprocess_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue,
) -> Job:
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("Document is deleting or deletion failed, cannot reprocess")
    latest = await session.scalar(select(Job).where(Job.document_id == document.id).order_by(Job.created_at.desc()))
    if latest is not None and latest.status in {
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.PAUSED,
    }:
        return latest
    restart_from_scratch = document.status == DocumentStatus.READY
    retrying_cleanup = bool(
        latest is not None and latest.type == JobType.REPROCESS_DOCUMENT and latest.status == JobStatus.FAILED
    )
    requires_maintenance = restart_from_scratch or retrying_cleanup
    original_status = document.status
    derived_source_ids: set[str] = set()
    if restart_from_scratch:
        derived_source_ids = {
            value
            for value in [
                document.sag_source_id,
                *[
                    _checkpoint_source_id(candidate.payload)
                    for candidate in (await session.scalars(select(Job).where(Job.document_id == document.id))).all()
                ],
            ]
            if value
        }

    values: dict = {
        "status": DocumentStatus.PENDING,
        "error": None,
    }
    if restart_from_scratch:
        values.update(
            progress=0,
            chunk_count=0,
            event_count=0,
            token_usage=0,
            sag_source_id=None,
            parser_provider=None,
            mineru_provider=None,
            mineru_model=None,
            parser_status=None,
            fallback_from=None,
            fallback_reason=None,
        )
    claimed = await session.execute(
        update(Document)
        .where(Document.id == document.id, Document.status == original_status)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        existing = await session.scalar(
            select(Job)
            .where(
                Job.document_id == document_id,
                Job.type.in_([JobType.PROCESS_DOCUMENT, JobType.REPROCESS_DOCUMENT]),
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED]),
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing
        raise ConflictError("Document state has changed, please refresh and retry")
    await session.refresh(document)
    if restart_from_scratch:
        await _refresh_source_counts(session, source)
    payload = dict(latest.payload or {}) if latest is not None and not restart_from_scratch else {}
    payload.pop("pause_requested", None)
    payload.pop("resume_requested", None)
    if restart_from_scratch:
        payload = set_scheduler(
            {
                "target_document_id": document.id,
                "derived_source_ids": sorted(derived_source_ids),
            },
            priority=DELETE_PRIORITY,
        )
    elif retrying_cleanup:
        payload = set_scheduler(payload, priority=DELETE_PRIORITY, blocked_reason=None)
    job = Job(
        type=(JobType.REPROCESS_DOCUMENT if requires_maintenance else JobType.PROCESS_DOCUMENT),
        source_id=source.id,
        document_id=document.id,
        status=JobStatus.QUEUED,
        # If previous failure already created a MinerU task, reprocessing should continue polling instead of charging again.
        payload=payload,
    )
    session.add(job)
    await touch_source_revision(session, source.id)
    await session.commit()
    await session.refresh(job)
    if requires_maintenance:
        job_queue.begin_source_maintenance(source.id, job.id)
    await _enqueue_persisted_job(job_queue, job.id)
    return job


def _checkpoint_source_id(payload: dict | None) -> str | None:
    checkpoint = (payload or {}).get("process_checkpoint")
    value = checkpoint.get("source_id") if isinstance(checkpoint, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _document_derived_source_ids(
    session: AsyncSession,
    document: Document,
) -> set[str]:
    """Collect every engine article id ever associated with one document."""
    values = {document.sag_source_id} if document.sag_source_id else set()
    jobs = (await session.scalars(select(Job).where(Job.document_id == document.id))).all()
    for candidate in jobs:
        payload = candidate.payload or {}
        checkpoint_id = _checkpoint_source_id(payload)
        if checkpoint_id:
            values.add(checkpoint_id)
        values.update(
            value.strip() for value in payload.get("derived_source_ids", []) if isinstance(value, str) and value.strip()
        )
    return values


def _source_document_filter(source_id: str):
    return (
        Document.source_id == source_id,
        Document.is_active.is_(True),
        Document.status.not_in([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
    )


def _source_sum_clause(source_id: str, column):
    """Sum chunk and event counts only for READY documents."""
    return (
        select(
            func.coalesce(
                func.sum(
                    case(
                        (Document.status == DocumentStatus.READY, column),
                        else_=0,
                    )
                ),
                0,
            )
        )
        .where(*_source_document_filter(source_id))
        .scalar_subquery()
    )


async def _refresh_source_counts(session: AsyncSession, source: Source) -> None:
    """Atomically recalculate source aggregate counts.

    Deletion tasks and document-level changes (upload/delete, etc.) can be committed concurrently;
    use a single UPDATE at the database layer to serialize count updates, avoiding lost updates from interleaved SELECT-then-assign.
    """
    document_count = (
        select(func.count(Document.id))
        .where(*_source_document_filter(source.id))
        .scalar_subquery()
    )
    await session.execute(
        update(Source)
        .where(Source.id == source.id)
        .values(
            document_count=document_count,
            chunk_count=_source_sum_clause(source.id, Document.chunk_count),
            event_count=_source_sum_clause(source.id, Document.event_count),
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(
        source,
        attribute_names=["document_count", "chunk_count", "event_count"],
    )


async def _commit_document_job_transition(
    session: AsyncSession,
    document: Document,
    job: Job,
    *,
    expected_document_status: DocumentStatus,
    document_values: dict,
    expected_job_status: JobStatus,
    job_values: dict,
) -> bool:
    """Atomically claim one document control transition and its process job."""
    claimed_document = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.status == expected_document_status,
        )
        .values(**document_values)
        .execution_options(synchronize_session=False)
    )
    if claimed_document.rowcount != 1:
        await session.rollback()
        return False

    claimed_job = await session.execute(
        update(Job)
        .where(
            Job.id == job.id,
            Job.document_id == document.id,
            Job.type == JobType.PROCESS_DOCUMENT,
            Job.status == expected_job_status,
        )
        .values(**job_values)
        .execution_options(synchronize_session=False)
    )
    if claimed_job.rowcount != 1:
        await session.rollback()
        return False

    await session.commit()
    await session.refresh(document)
    await session.refresh(job)
    return True


async def _raise_document_control_conflict(
    session: AsyncSession,
    document_id: str,
    *,
    action: str,
    fallback: str,
) -> None:
    """Report the winning concurrent transition after a failed control CAS."""
    await session.rollback()
    current = await session.get(Document, document_id, populate_existing=True)
    if current is None:
        raise ConflictError("Document has been deleted, please refresh and retry")
    if current.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError(f"Document is deleting or deletion failed, cannot {action}")
    raise ConflictError(fallback)


async def pause_document(session: AsyncSession, source: Source, document_id: str) -> Job:
    """Cooperative pause: complete and save checkpoint for in-flight chunks, then stop picking up new chunks."""
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("Document is deleting or deletion failed, cannot stop extraction")
    if document.status not in {
        DocumentStatus.PENDING,
        DocumentStatus.LOADING,
        DocumentStatus.EXTRACTING,
    }:
        raise ConflictError("Extraction task has completed or state has changed, cannot stop")
    document_record_id = document.id
    expected_document_status = document.status
    job = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document_record_id,
            Job.type == JobType.PROCESS_DOCUMENT,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if job is None:
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="stop extraction",
            fallback="The current document has no stoppable extraction task",
        )

    if job.status == JobStatus.QUEUED:
        if await _commit_document_job_transition(
            session,
            document,
            job,
            expected_document_status=expected_document_status,
            document_values={"status": DocumentStatus.PAUSED},
            expected_job_status=JobStatus.QUEUED,
            job_values={"status": JobStatus.PAUSED},
        ):
            return job
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="stop extraction",
            fallback="Document or extraction task state has changed, please refresh and retry",
        )

    if job.status != JobStatus.RUNNING:
        raise ConflictError("Extraction task has ended, cannot stop")
    if await _commit_document_job_transition(
        session,
        document,
        job,
        expected_document_status=expected_document_status,
        document_values={"status": DocumentStatus.PAUSING},
        expected_job_status=JobStatus.RUNNING,
        job_values={"status": JobStatus.PAUSED},
    ):
        return job
    await _raise_document_control_conflict(
        session,
        document_record_id,
        action="stop extraction",
        fallback="Document or extraction task state has changed, please refresh and retry",
    )


async def resume_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue,
) -> Job:
    """Re-enqueue paused task as-is; processor will skip chunks already completed in checkpoint."""
    document = await get_document(session, source, document_id)
    if document.status in {DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED}:
        raise ConflictError("Document is deleting or deletion failed, cannot resume")
    if document.status != DocumentStatus.PAUSED:
        raise ConflictError("The current document is not paused, cannot resume")
    document_record_id = document.id
    job = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document_record_id,
            Job.type == JobType.PROCESS_DOCUMENT,
            Job.status == JobStatus.PAUSED,
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if job is None:
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="resume",
            fallback="The current document has no resumable paused task",
        )

    payload = dict(job.payload or {})
    checkpoint = payload.get("process_checkpoint")
    is_legacy_checkpoint = (
        isinstance(checkpoint, dict)
        and bool(checkpoint.get("chunk_ids"))
        and not checkpoint.get("chunk_version")
    )
    if is_legacy_checkpoint:
        if not os.path.isfile(document.storage_path):
            raise ConflictError("Legacy extraction checkpoint cannot be restored and original file no longer exists; please re-upload original file")
        payload.pop("process_checkpoint", None)
    payload.pop("pause_requested", None)
    payload["resume_requested"] = True
    resumed_payload = set_scheduler(
        payload,
        priority=RESUME_PRIORITY,
        blocked_reason=None,
    )
    resumed_status = DocumentStatus.EXTRACTING if payload.get("process_checkpoint") else DocumentStatus.PENDING
    if not await _commit_document_job_transition(
        session,
        document,
        job,
        expected_document_status=DocumentStatus.PAUSED,
        document_values={"status": resumed_status, "error": None},
        expected_job_status=JobStatus.PAUSED,
        job_values={
            "payload": resumed_payload,
            "status": JobStatus.QUEUED,
            "started_at": None,
            "finished_at": None,
            "error": None,
        },
    ):
        await _raise_document_control_conflict(
            session,
            document_record_id,
            action="resume",
            fallback="Document or extraction task state has changed, please refresh and retry",
        )
    await _enqueue_persisted_job(job_queue, job.id)
    return job


async def delete_document(
    session: AsyncSession,
    source: Source,
    document_id: str,
    *,
    job_queue: JobQueue | None = None,
) -> Job:
    document = await get_document(session, source, document_id)
    source_record_id = source.id
    derived_source_ids = await _document_derived_source_ids(session, document)
    existing = await session.scalar(
        select(Job)
        .where(
            Job.document_id == document.id,
            Job.type == JobType.DELETE_DOCUMENT,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    active_jobs = list(
        (
            await session.scalars(
                select(Job).where(
                    Job.document_id == document.id,
                    Job.type == JobType.PROCESS_DOCUMENT,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            )
        ).all()
    )
    active_reprocess_jobs = list(
        (
            await session.scalars(
                select(Job).where(
                    Job.document_id == document.id,
                    Job.type == JobType.REPROCESS_DOCUMENT,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            )
        ).all()
    )

    # A document whose processor has never started cannot have written any
    # engine-side records. Delete it inside this request so it is never held up
    # by unrelated long-running ingestion or cleanup work.
    metadata_only = (
        document.status == DocumentStatus.PENDING
        and not document.sag_source_id
        and not active_reprocess_jobs
        and all(
            active_job.status == JobStatus.QUEUED
            and active_job.started_at is None
            and active_job.attempts == 0
            and not _checkpoint_source_id(active_job.payload)
            for active_job in active_jobs
        )
    )
    if metadata_only:
        path = document.storage_path
        target_document_id = document.id
        candidate_job_ids = [candidate.id for candidate in active_jobs]
        if candidate_job_ids:
            fenced_jobs = await session.execute(
                update(Job)
                .where(
                    Job.id.in_(candidate_job_ids),
                    Job.document_id == target_document_id,
                    Job.type == JobType.PROCESS_DOCUMENT,
                    Job.status == JobStatus.QUEUED,
                    Job.started_at.is_(None),
                    Job.attempts == 0,
                )
                .values(status=JobStatus.PAUSED)
                .execution_options(synchronize_session=False)
            )
            if fenced_jobs.rowcount != len(candidate_job_ids):
                # A worker claimed the processor after the optimistic read.
                # Roll back any partial fence and use cooperative deletion.
                await session.rollback()
                completed_jobs = list(
                    (
                        await session.scalars(
                            select(Job)
                            .where(
                                Job.source_id == source_record_id,
                                Job.type == JobType.DELETE_DOCUMENT,
                                Job.status == JobStatus.SUCCEEDED,
                            )
                            .order_by(Job.created_at.desc(), Job.id.desc())
                            .limit(100)
                        )
                    ).all()
                )
                completed = next(
                    (
                        candidate
                        for candidate in completed_jobs
                        if (candidate.payload or {}).get("target_document_id") == target_document_id
                    ),
                    None,
                )
                if completed is not None:
                    return completed
                current_source = await session.get(
                    Source,
                    source_record_id,
                    populate_existing=True,
                )
                if current_source is None:
                    raise NotFoundError("Source not found")
                return await delete_document(
                    session,
                    current_source,
                    target_document_id,
                    job_queue=job_queue,
                )
        claimed = await session.execute(
            update(Document)
            .where(
                Document.id == target_document_id,
                Document.status == DocumentStatus.PENDING,
                Document.sag_source_id.is_(None),
            )
            .values(status=DocumentStatus.DELETING, error=None)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            completed_jobs = list(
                (
                    await session.scalars(
                        select(Job)
                        .where(
                            Job.source_id == source_record_id,
                            Job.type == JobType.DELETE_DOCUMENT,
                            Job.status == JobStatus.SUCCEEDED,
                        )
                        .order_by(Job.created_at.desc(), Job.id.desc())
                        .limit(100)
                    )
                ).all()
            )
            completed = next(
                (
                    candidate
                    for candidate in completed_jobs
                    if (candidate.payload or {}).get("target_document_id") == target_document_id
                ),
                None,
            )
            if completed is not None:
                return completed
            raise ConflictError("Document state has changed, please refresh and retry")
        completed_at = datetime.now(UTC)
        completed = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=None,
            status=JobStatus.SUCCEEDED,
            progress=1.0,
            attempts=1,
            payload=set_scheduler(
                {"target_document_id": target_document_id, "cleanup_mode": "metadata_only"},
                priority=DELETE_PRIORITY,
            ),
            started_at=completed_at,
            finished_at=completed_at,
        )
        session.add(completed)
        await session.delete(document)
        await session.flush()
        await _refresh_source_counts(session, source)
        await touch_source_revision(session, source.id)
        await session.commit()
        await session.refresh(completed)
        if path:
            from sag_api.parsing.service import parsed_sidecar_paths

            for candidate in [path, *parsed_sidecar_paths(path)]:
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except OSError:
                    pass
        return completed

    if existing is None:
        original_status = document.status
        claimed = await session.execute(
            update(Document)
            .where(Document.id == document.id, Document.status == original_status)
            .values(status=DocumentStatus.DELETING, error=None)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            existing = await session.scalar(
                select(Job)
                .where(
                    Job.document_id == document_id,
                    Job.type == JobType.DELETE_DOCUMENT,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
            if existing is None:
                raise ConflictError("Document state has changed, please refresh and retry")
            if job_queue is not None:
                job_queue.begin_source_maintenance(source_record_id, existing.id)
                await _enqueue_persisted_job(job_queue, existing.id)
            return existing
        await session.refresh(document)

    for job in active_jobs:
        job.payload = {**(job.payload or {}), "pause_requested": True}
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.PAUSED
    document.status = DocumentStatus.DELETING
    document.error = None
    if existing is not None:
        await _refresh_source_counts(session, source)
        await session.commit()
        await session.refresh(existing)
        if job_queue is not None:
            job_queue.begin_source_maintenance(source.id, existing.id)
            await _enqueue_persisted_job(job_queue, existing.id)
        return existing

    delete_job = Job(
        type=JobType.DELETE_DOCUMENT,
        source_id=source.id,
        document_id=document.id,
        status=JobStatus.QUEUED,
        payload=set_scheduler(
            {
                "target_document_id": document.id,
                "derived_source_ids": sorted(derived_source_ids),
            },
            priority=DELETE_PRIORITY,
        ),
    )
    session.add(delete_job)
    await _refresh_source_counts(session, source)
    await touch_source_revision(session, source.id)
    await session.commit()
    await session.refresh(delete_job)
    if job_queue is not None:
        job_queue.begin_source_maintenance(source.id, delete_job.id)
        await _enqueue_persisted_job(job_queue, delete_job.id)
    return delete_job


# ============================================================================
# Knowledge Routing RAG: Multi-Tenant Project Document Upload & Versioning
# ============================================================================


def _check_upload_file(filename: str, file_bytes: bytes) -> None:
    """Validate file content, size, and allowed extensions."""
    if not file_bytes:
        raise ValidationError("Uploaded file cannot be empty")
    max_size = getattr(settings, "max_upload_size_bytes", 50 * 1024 * 1024)
    if len(file_bytes) > max_size:
        raise ValidationError(f"File size exceeds maximum allowed ({max_size} bytes)")
    allowed = settings.allowed_upload_exts
    if allowed:
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in allowed:
            pretty = "、".join(sorted(e.lstrip(".") for e in allowed))
            raise ValidationError(f"Unsupported file extension. Allowed: {pretty}")


def _save_snapshot_file(version_id: str, original_filename: str, file_bytes: bytes) -> str:
    """Persist raw file bytes to snapshot storage on disk."""
    base_dir = Path(settings.effective_data_dir) / "snapshots" / version_id
    base_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(original_filename).name or "source.bin"
    target_path = base_dir / safe_name
    target_path.write_bytes(file_bytes)
    return str(target_path.resolve())


async def handle_document_upload(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    owner_id: str,
    security_partition_id: str,
    client_token: str,
    file_bytes: bytes,
    original_filename: str,
    content_type: str = "application/octet-stream",
    logical_source_id: str | None = None,
    source_published_at: datetime | None = None,
) -> DocumentUploadResponse:
    """Process an upload request with strict ACL partition, idempotency, and versioning.

    Handles concurrent retries with race-condition recovery.
    """
    if not security_partition_id or not security_partition_id.strip():
        raise ValidationError(
            "X-Continuum-Security-Partition header is required to enforce data isolation"
        )
    if not client_token or not client_token.strip():
        raise ValidationError("Idempotency-Key header is required for upload")

    _check_upload_file(original_filename, file_bytes)

    payload_hash = compute_payload_hash(file_bytes)
    idempotency_key = normalize_idempotency_key(client_token)
    logical_id = (logical_source_id or original_filename).strip()

    # Step 1: Pre-allocation lookup by (tenant_id, project_id, idempotency_key)
    lookup_stmt = select(IngestionRun).where(
        IngestionRun.tenant_id == tenant_id,
        IngestionRun.project_id == project_id,
        IngestionRun.idempotency_key == idempotency_key,
    )
    existing_run = (await session.execute(lookup_stmt)).scalar_one_or_none()

    if existing_run:
        # Check payload match
        if existing_run.payload_hash != payload_hash:
            raise ConflictError(
                f"Idempotency key is already bound to a different file payload hash. "
                f"Reusing an idempotency key across different file contents is forbidden. "
                f"(incoming={payload_hash}, existing={existing_run.payload_hash})",
                layer=ErrorLayer.CLIENT,
                stage=ErrorStage.UPLOAD,
                code="IDEMPOTENCY_PAYLOAD_MISMATCH",
                retryable=False,
            )
        doc_ver = await session.get(DocumentVersion, existing_run.document_version_id)
        return DocumentUploadResponse(
            document_id=doc_ver.document_id if doc_ver else "",
            version_no=doc_ver.version_no if doc_ver else 1,
            version_id=existing_run.document_version_id,
            run_id=existing_run.id,
            file_hash=payload_hash,
            status=doc_ver.status if doc_ver else "RECEIVED",
            search_status=doc_ver.search_status if doc_ver else "PENDING",
            knowledge_status=doc_ver.knowledge_status if doc_ver else "NOT_STARTED",
            is_duplicate=True,
        )

    # Step 2: Attempt creation in transaction with savepoint to recover race condition
    try:
        async with session.begin_nested():
            # Check or create logical document
            doc_stmt = select(Document).where(
                Document.tenant_id == tenant_id,
                Document.project_id == project_id,
                Document.logical_source_id == logical_id,
            )
            doc = (await session.execute(doc_stmt)).scalar_one_or_none()

            if not doc:
                doc_id = generate_doc_id(tenant_id, project_id, logical_id)
                doc = Document(
                    id=doc_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    owner_id=owner_id,
                    logical_source_id=logical_id,
                    filename=original_filename,
                    content_type=content_type,
                    size_bytes=len(file_bytes),
                    storage_path="",
                )
                session.add(doc)
                await session.flush()
                version_no = 1
                supersedes_id = None
            else:
                # Find latest version for this document
                v_stmt = (
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == doc.id)
                    .order_by(DocumentVersion.version_no.desc())
                    .limit(1)
                )
                latest_ver = (await session.execute(v_stmt)).scalar_one_or_none()
                if latest_ver and latest_ver.file_hash == payload_hash:
                    # Same hash and same identity -> reuse existing version number
                    version_no = latest_ver.version_no
                    supersedes_id = latest_ver.supersedes_id
                else:
                    version_no = (latest_ver.version_no + 1) if latest_ver else 1
                    supersedes_id = latest_ver.id if latest_ver else None

            version_id = generate_version_id(doc.id, version_no)

            existing_ver = await session.get(DocumentVersion, version_id)
            if not existing_ver:
                storage_path = _save_snapshot_file(version_id, original_filename, file_bytes)
                doc.storage_path = storage_path

                doc_version = DocumentVersion(
                    id=version_id,
                    document_id=doc.id,
                    version_no=version_no,
                    file_hash=payload_hash,
                    supersedes_id=supersedes_id,
                    source_published_at=source_published_at,
                    status="RECEIVED",
                    search_status="PENDING",
                    knowledge_status="NOT_STARTED",
                    metadata_json={"security_partition_id": security_partition_id},
                )
                session.add(doc_version)
                await session.flush()

                snapshot = SourceSnapshot(
                    id=str(uuid.uuid4()),
                    document_version_id=version_id,
                    storage_uri=storage_path,
                    original_filename=original_filename,
                    mime_type=content_type,
                    byte_size=len(file_bytes),
                    checksum_sha256=payload_hash,
                )
                session.add(snapshot)
            else:
                doc_version = existing_ver

            run_id = str(uuid.uuid4())
            run = IngestionRun(
                id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                document_version_id=doc_version.id,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                current_stage="RECEIVE",
                status="QUEUED",
            )
            session.add(run)

            stage_run = StageRun(
                id=str(uuid.uuid4()),
                run_id=run_id,
                stage="receive",
                status="SUCCESS",
                duration_ms=10.0,
                metrics_json={"byte_size": len(file_bytes)},
            )
            session.add(stage_run)
            await session.flush()

        await session.commit()
        return DocumentUploadResponse(
            document_id=doc.id,
            version_no=doc_version.version_no,
            version_id=doc_version.id,
            run_id=run.id,
            file_hash=payload_hash,
            status=doc_version.status,
            search_status=doc_version.search_status,
            knowledge_status=doc_version.knowledge_status,
            is_duplicate=False,
        )
    except IntegrityError:
        # Race condition recovery: concurrent insert with same (tenant_id, project_id, idempotency_key)
        await session.rollback()
        stmt = select(IngestionRun).where(
            IngestionRun.tenant_id == tenant_id,
            IngestionRun.project_id == project_id,
            IngestionRun.idempotency_key == idempotency_key,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            if existing.payload_hash != payload_hash:
                raise ConflictError(
                    f"Idempotency key is already bound to a different file payload hash. "
                    f"Reusing an idempotency key across different file contents is forbidden. "
                    f"(incoming={payload_hash}, existing={existing.payload_hash})",
                    layer=ErrorLayer.CLIENT,
                    stage=ErrorStage.UPLOAD,
                    code="IDEMPOTENCY_PAYLOAD_MISMATCH",
                    retryable=False,
                )
            doc_ver = await session.get(DocumentVersion, existing.document_version_id)
            return DocumentUploadResponse(
                document_id=doc_ver.document_id if doc_ver else "",
                version_no=doc_ver.version_no if doc_ver else 1,
                version_id=existing.document_version_id,
                run_id=existing.id,
                file_hash=payload_hash,
                status=doc_ver.status if doc_ver else "RECEIVED",
                search_status=doc_ver.search_status if doc_ver else "PENDING",
                knowledge_status=doc_ver.knowledge_status if doc_ver else "NOT_STARTED",
                is_duplicate=True,
            )
        raise


async def get_document_version_status(
    session: AsyncSession,
    *,
    project_id: str,
    document_id: str,
    version_no: int,
) -> DocumentVersionStatusResponse:
    """Retrieve fine-grained readiness and stage execution progress for a document version."""
    stmt = (
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.project_id == project_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_no == version_no,
        )
    )
    doc_ver = (await session.execute(stmt)).scalar_one_or_none()
    if not doc_ver:
        raise NotFoundError(
            f"Document version {document_id}/v{version_no} not found in project {project_id}"
        )

    run_stmt = (
        select(IngestionRun)
        .where(IngestionRun.document_version_id == doc_ver.id)
        .order_by(IngestionRun.created_at.desc())
        .limit(1)
    )
    latest_run = (await session.execute(run_stmt)).scalar_one_or_none()

    stage_progress: dict[str, StageProgressItem] = {}
    err_info = None
    current_stage = "UNKNOWN"

    if latest_run:
        current_stage = latest_run.current_stage
        sr_stmt = (
            select(StageRun)
            .where(StageRun.run_id == latest_run.id)
            .order_by(StageRun.created_at.asc())
        )
        stages = (await session.execute(sr_stmt)).scalars().all()
        for s in stages:
            stage_progress[s.stage] = StageProgressItem(
                status=s.status, duration_ms=s.duration_ms
            )
        if latest_run.error_code:
            err_info = {
                "layer": latest_run.error_layer,
                "stage": latest_run.error_stage,
                "code": latest_run.error_code,
                "message": latest_run.error_message,
            }

    return DocumentVersionStatusResponse(
        document_id=doc_ver.document_id,
        version_no=doc_ver.version_no,
        status=doc_ver.status,
        search_status=doc_ver.search_status,
        knowledge_status=doc_ver.knowledge_status,
        search_ready=(doc_ver.search_status == "SEARCH_READY"),
        knowledge_ready=(doc_ver.knowledge_status == "KNOWLEDGE_READY"),
        current_stage=current_stage,
        stage_progress=stage_progress,
        error=err_info,
    )
