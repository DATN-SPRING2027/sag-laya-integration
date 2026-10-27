import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.core.errors import ValidationError
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    KnowledgeJob,
    Source,
    SourceProjectMapping,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.jobs.knowledge import KnowledgeWorker
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.services import search_index_service
from sag_api.services.source_service import search_source_candidates


@pytest_asyncio.fixture
async def ingestion_db(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    file = tmp_path / "notes.md"
    file.write_text("# Upload policy\n\nThe upload size limit is 10 MiB per file.", encoding="utf-8")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    async with factory() as session:
        session.add(Source(id="source", name="Source", sag_source_config_id="config"))
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id="source",
                organization_id="tenant",
                project_id="project",
                state="CONFIRMED",
                confirmed_at=datetime.now(UTC),
                confirmed_by="user",
                approval_ref="test",
            )
        )
        session.add(
            Document(
                id="document",
                source_id="source",
                tenant_id="tenant",
                project_id="project",
                filename=file.name,
                storage_path=str(file),
                status=DocumentStatus.PENDING,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="version",
                document_id="document",
                version_no=1,
                file_hash=digest,
                metadata_json={"security_partition_id": "project"},
            )
        )
        await session.flush()
        session.add(
            IngestionRun(
                id="run",
                tenant_id="tenant",
                project_id="project",
                document_version_id="version",
                idempotency_key="key",
                payload_hash=digest,
                status="QUEUED",
            )
        )
        session.add(
            Job(
                id="job",
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.RUNNING,
                source_id="source",
                document_id="document",
                payload={"run_id": "run"},
            )
        )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
async def test_canonical_ingestion_never_calls_legacy_extractor_by_default(ingestion_db, monkeypatch, verified):
    monkeypatch.setattr(tasks.settings, "document_event_extraction_enabled", False)

    async def index(session, *, document_version, **kwargs):
        document_version.search_status = "SEARCH_READY" if verified else "INDEX_FAILED"
        document_version.search_ready_at = datetime.now(UTC) if verified else None
        return [SimpleNamespace(id="canonical-unit")]

    monkeypatch.setattr(search_index_service, "run_search_indexing_stage", index)
    manager = SimpleNamespace(
        get_sag_embedding=AsyncMock(return_value=None),
        process_document=AsyncMock(side_effect=AssertionError("Unexpected legacy LLM extraction")),
    )
    async with ingestion_db() as session:
        job = await session.get(Job, "job")
        if verified:
            await tasks._process_document_unlocked(session, job, engine_manager=manager)
            await session.refresh(await session.get(Document, "document"))
            assert (await session.get(Document, "document")).status == DocumentStatus.READY
            assert (await session.get(IngestionRun, "run")).status == "SUCCEEDED"
            assert (await session.get(DocumentVersion, "version")).knowledge_status == "NOT_STARTED"
            assert (await session.scalars(select(CanonicalBlock))).all()
        else:
            with pytest.raises(RuntimeError, match="manifest"):
                await tasks._process_document_unlocked(session, job, engine_manager=manager)
            assert (await session.get(Document, "document")).status == DocumentStatus.FAILED
        manager.process_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_foundation_can_start_from_search_ready_before_legacy_document_completion(ingestion_db):
    async with ingestion_db() as session:
        doc = await session.get(Document, "document")
        doc.status = DocumentStatus.EXTRACTING
        version = await session.get(DocumentVersion, "version")
        version.search_status, version.search_ready_at = "SEARCH_READY", datetime.now(UTC)
        text = "The upload size limit is 10 MiB."
        session.add(
            CanonicalBlock(
                id="block",
                document_version_id="version",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Upload",
                normalized_text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                source_anchor="upload",
            )
        )
        await session.commit()
    worker = KnowledgeWorker(ingestion_db, Settings())
    await worker.discover()
    async with ingestion_db() as session:
        job = await session.scalar(select(KnowledgeJob))
        assert job is not None and job.kind == "FOUNDATION" and job.status == "QUEUED"
        assert (await session.get(Document, "document")).status == DocumentStatus.EXTRACTING


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_legacy_processor_requires_explicit_event_extraction_opt_in(enabled):
    processor = IncrementalDocumentProcessor(SimpleNamespace(), "config", max_concurrency=1, extract_events=enabled)
    processor._sample_language = AsyncMock()
    processor._chunk_set_ref = lambda checkpoint: SimpleNamespace(chunk_ids=checkpoint.chunk_ids)
    processor._extract = AsyncMock(return_value=(False, SimpleNamespace(event_ids=["event"], event_count=1)))
    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(source_id="source", chunk_ids=["chunk"]),
        on_checkpoint=AsyncMock(),
        should_pause=AsyncMock(return_value=False),
    )
    assert outcome.event_count == (1 if enabled else 0)
    assert processor._extract.await_count == int(enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_limit", [0, 2])
async def test_source_search_never_silently_omits_authorized_sources(ingestion_db, monkeypatch, configured_limit):
    monkeypatch.setattr(tasks.settings, "search_source_candidate_limit", configured_limit)
    async with ingestion_db() as session:
        for index in range(18):
            source_id = f"extra-{index}"
            session.add(Source(id=source_id, name=source_id, sag_source_config_id=source_id))
            await session.flush()
            session.add(
                SourceProjectMapping(
                    source_id=source_id,
                    organization_id="tenant",
                    project_id="project" if index < 17 else "other",
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="user",
                    approval_ref="test",
                )
            )
        await session.commit()
        principal = SimpleNamespace(allowed_project_ids={"project"}, organization_id="tenant")
        if configured_limit:
            with pytest.raises(ValidationError, match="budget"):
                await search_source_candidates(session, principal=principal, include_project_sources=True)
        else:
            result = await search_source_candidates(session, principal=principal, include_project_sources=True)
            assert len(result) == 18
            assert "extra-17" not in {source.id for source in result}


@pytest.mark.asyncio
async def test_canonical_embedding_reuses_sdk_client_without_starting_event_storage(monkeypatch):
    import zleap.sag.core.ai.factory as factory

    client = SimpleNamespace(
        model="test",
        base_url="https://example.invalid",
        dimensions=1024,
        max_tokens=8192,
        generate=AsyncMock(),
        batch_generate=AsyncMock(),
        close=AsyncMock(),
    )
    create = AsyncMock(return_value=client)
    monkeypatch.setattr(factory, "create_embedding_client", create)
    manager = EngineManager(Settings())
    manager._slot = AsyncMock(side_effect=AssertionError("Legacy storage must not start for canonical embeddings"))
    first = await manager.get_sag_embedding("source-a")
    second = await manager.get_sag_embedding("source-b")
    assert first is second
    create.assert_awaited_once()
    manager._slot.assert_not_awaited()
    await manager.aclose_all()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("prune_fails", [False, True])
@pytest.mark.parametrize("missing_index", [False, True])
async def test_verified_new_version_publishes_even_when_old_point_cleanup_fails(ingestion_db, monkeypatch, prune_fails, missing_index):
    import json

    import httpx

    from sag_api.services import dedup_and_temporal_service as temporal

    points = []
    deleted = []
    created_indexes = []

    async def index(_client, *, units, **kwargs):
        points.extend(
            {
                "id": search_index_service.generate_search_unit_point_id("search_units_project", unit.id),
                "payload": {"content_hash": unit.content_hash},
            }
            for unit in units
        )
        return len(units)

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/index"):
            created_indexes.append(body["field_name"])
            return httpx.Response(200, json={"result": {}})
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": len(points)}})
        if request.url.path.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": points}})
        match = body["filter"]["must"][0]["match"]
        if missing_index and match.get("value") == "version" and not created_indexes:
            return httpx.Response(400, text='Index required but not found for "document_version_id"')
        if match.get("any") == ["old-version"]:
            deleted.append(match["any"])
            if prune_fails:
                return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"result": {}})

    monkeypatch.setattr(search_index_service, "index_search_units_to_qdrant", index)
    monkeypatch.setattr(
        temporal,
        "resolve_temporal_supersedes",
        AsyncMock(
            return_value={
                "action": "SUPERSEDED",
                "supersedes_id": "old-version",
            }
        ),
    )
    async with ingestion_db() as session:
        session.add(
            CanonicalBlock(
                id="block",
                document_version_id="version",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Upload",
                normalized_text="Upload policy is 10 MiB.",
                content_hash=hashlib.sha256(b"Upload policy is 10 MiB.").hexdigest(),
                source_anchor="upload",
            )
        )
        await session.flush()
        version = await session.get(DocumentVersion, "version")
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            units = await search_index_service.run_search_indexing_stage(
                session,
                project_id="project",
                document_version=version,
                security_partition_id="project",
                tenant_id="tenant",
                qdrant_client=client,
            )
        assert units
        assert version.search_status == "SEARCH_READY"
        assert deleted == [["old-version"]]
        assert created_indexes == (["document_version_id"] if missing_index else [])
