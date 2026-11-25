"""Regression tests for Milestone P1 Commit 3: evidence DB fail-closed & versioned ACL (S5-06, S4-06)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sag_api.core.db as core_db
from sag_api.api.v1 import knowledge as knowledge_api
from sag_api.core.errors import NotFoundError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    Source,
    SourceProjectMapping,
)
from sag_api.enums import DocumentStatus
from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.services import evidence_service, search_unit_retrieval_service
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import GetEntityTool


def _principal(partitions: frozenset[str] = frozenset({"partition-a"})) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="test-user",
        organization_id="tenant-a",
        allowed_project_ids=frozenset({"project-a"}),
        issuer="test-issuer",
        key_id="test-key",
        token_id="test-token",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-a",
        allowed_partition_ids=partitions,
    )


@pytest.mark.asyncio
async def test_resolve_traceable_evidence_fails_closed_on_sqlalchemy_error(monkeypatch):
    """S5-06: SQLAlchemyError during locator lookup must return [] instead of unverified sections."""
    unverified = RetrievedSection(
        chunk_id="legacy-1",
        heading="Unverified",
        content="Potentially stale or unauthorized text.",
        score=0.9,
        source_id="src-1",
        source_config_id="cfg-1",
        canonical_evidence_verified=False,
    )
    src = SimpleNamespace(id="src-1", name="Source 1", sag_source_config_id="cfg-1")

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            raise SQLAlchemyError("db unavailable")

    monkeypatch.setattr(core_db, "SessionLocal", lambda: FailingSession())
    resolved = await evidence_service.resolve_traceable_evidence([unverified], [src])
    assert resolved == []


@pytest.mark.asyncio
async def test_get_entity_tool_enforces_versioned_source_acl_and_returns_citations(monkeypatch):
    """S4-06 / S5-12: GetEntityTool must enforce versioned_source_ids ACL and return citations."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_cm():
        async with factory() as s:
            yield s

    monkeypatch.setattr(core_db, "SessionLocal", session_cm)

    async with factory() as s:
        src = Source(id="src-1", name="Project A", sag_source_config_id="cfg-1")
        s.add(src)
        await s.flush()
        doc = Document(
            id="doc-1",
            source_id="src-1",
            tenant_id="tenant-a",
            project_id="project-a",
            filename="team.md",
            storage_path="team.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
        s.add(doc)
        await s.flush()
        s.add(
            DocumentVersion(
                id="ver-1",
                document_id="doc-1",
                version_no=1,
                file_hash="hash-1",
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=datetime.now(UTC),
                metadata_json={"security_partition_id": "partition-a"},
            )
        )
        await s.commit()

    verified_sec = RetrievedSection(
        chunk_id="su-1",
        search_unit_id="su-1",
        block_from_id="b-1",
        block_to_id="b-1",
        section_path="Team > Owners",
        heading="Owners",
        content="Alice leads the security engineering team.",
        score=0.88,
        raw_dense_score=0.81,
        rank=0,
        source_id="src-1",
        source_config_id="cfg-1",
        canonical_evidence_verified=True,
        document_id="doc-1",
        document_version_id="ver-1",
        document_name="team.md",
        version_no=1,
        page_from=1,
        page_to=1,
        anchor="p1-b1",
    )
    mock_retrieve = AsyncMock(
        return_value=SearchOutcome(query="Alice", sections=[verified_sec], stats={})
    )
    monkeypatch.setattr(search_unit_retrieval_service, "retrieve_search_unit_sections", mock_retrieve)

    mock_engine = SimpleNamespace(
        list_entities=AsyncMock(side_effect=AssertionError("legacy list_entities must not be called")),
        entity_context=AsyncMock(side_effect=AssertionError("legacy entity_context must not be called")),
    )
    ctx = ToolContext(
        engine_manager=mock_engine,
        sources=[src],
        principal=_principal(),
        citation_offset=2,
    )
    try:
        res = await GetEntityTool().invoke({"name": "Alice"}, ctx)
        assert mock_retrieve.await_count == 1
        assert len(res.citations) == 1
        assert res.citations[0]["n"] == 3
        assert res.citations[0]["source_id"] == "src-1"
        assert "Alice leads the security engineering team." in res.content

        # Missing principal fails closed on versioned source
        ctx_no_principal = ToolContext(engine_manager=mock_engine, sources=[src], principal=None)
        res_denied = await GetEntityTool().invoke({"name": "Alice"}, ctx_no_principal)
        assert res_denied.citations == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_outline_enforces_versioned_source_acl_and_reads_canonical_blocks(monkeypatch):
    """S4-06: /outline must read CanonicalBlock for versioned sources and enforce partition ACL."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    async with factory() as s:
        src = Source(id="src-1", name="Project A", sag_source_config_id="cfg-1")
        s.add(src)
        await s.flush()
        s.add(
            SourceProjectMapping(
                source_id="src-1",
                organization_id="tenant-a",
                project_id="project-a",
                state="CONFIRMED",
                confirmed_at=now,
                confirmed_by="user",
                approval_ref="test",
            )
        )
        doc = Document(
            id="doc-1",
            source_id="src-1",
            tenant_id="tenant-a",
            project_id="project-a",
            filename="arch.md",
            storage_path="arch.md",
            status=DocumentStatus.READY,
            sag_source_id=None,
            is_active=True,
        )
        s.add(doc)
        await s.flush()
        s.add(
            DocumentVersion(
                id="ver-1",
                document_id="doc-1",
                version_no=1,
                file_hash="hash-arch",
                status=DocumentStatus.READY,
                search_status="SEARCH_READY",
                search_ready_at=now,
                valid_from=now - timedelta(minutes=1),
                valid_to=now + timedelta(days=365),
                metadata_json={"security_partition_id": "partition-a"},
            )
        )
        await s.flush()
        s.add(
            CanonicalBlock(
                id="blk-1",
                document_version_id="ver-1",
                ordinal=0,
                block_type="heading",
                page_from=1,
                page_to=1,
                section_path="1. Overview",
                source_anchor="p1-b0",
                normalized_text="1. Overview",
                content_hash="h1",
            )
        )
        s.add(
            CanonicalBlock(
                id="blk-2",
                document_version_id="ver-1",
                ordinal=1,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="1. Overview",
                source_anchor="p1-b1",
                normalized_text="System overview body.",
                content_hash="h2",
            )
        )
        s.add(
            CanonicalBlock(
                id="blk-3",
                document_version_id="ver-1",
                ordinal=2,
                block_type="heading",
                page_from=2,
                page_to=2,
                section_path="2. Deployment",
                source_anchor="p2-b2",
                normalized_text="2. Deployment",
                content_hash="h3",
            )
        )
        await s.commit()

    monkeypatch.setattr(knowledge_api, "get_source", AsyncMock(return_value=src))
    monkeypatch.setattr(knowledge_api, "get_public_document", AsyncMock(return_value=doc))
    mock_engine = SimpleNamespace(
        list_chunk_headings=AsyncMock(side_effect=AssertionError("legacy list_chunk_headings must not be called"))
    )

    try:
        async with factory() as session:
            out = await knowledge_api.outline(
                source_id="src-1",
                document_id="doc-1",
                _user=SimpleNamespace(id="u-1"),
                principal=_principal(frozenset({"partition-a"})),
                session=session,
                engine_manager=mock_engine,
            )
            assert out.document_id == "doc-1"
            assert [item.heading for item in out.outline] == ["1. Overview", "2. Deployment"]

            # Unauthorized partition must fail closed with NotFoundError
            with pytest.raises(NotFoundError):
                await knowledge_api.outline(
                    source_id="src-1",
                    document_id="doc-1",
                    _user=SimpleNamespace(id="u-1"),
                    principal=_principal(frozenset({"partition-other"})),
                    session=session,
                    engine_manager=mock_engine,
                )
    finally:
        await engine.dispose()
