"""Regression tests for Milestone P1 Commit 1: raw cosine floor & threshold alignment (S4-01/02/04)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1 import dify as dify_api
from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
)
from sag_api.enums import DocumentStatus
from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.schemas.dify import DifyRetrievalRequest, DifyRetrievalSetting
from sag_api.services import search_unit_retrieval_service as retrieval
from sag_api.services.retrieval_plan import group_sections_by_document
from sag_api.services.retrieval_service import rerank_sections
from sag_api.services.search_index_service import generate_search_unit_point_id


def _make_principal() -> VerifiedPrincipal:
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
        allowed_partition_ids=frozenset({"partition-a"}),
    )


async def _setup_canonical_db(monkeypatch, unit_specs: list[tuple[str, str, str]]):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(retrieval, "SessionLocal", factory)

    async with factory() as session:
        source = Source(id="source-a", name="Project A", sag_source_config_id="config-a")
        session.add(source)
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id="source-a",
                organization_id="tenant-a",
                project_id="project-a",
                state="CONFIRMED",
                confirmed_at=datetime.now(UTC),
                confirmed_by="user",
                approval_ref="test",
            )
        )
        doc = Document(
            id="doc-a",
            source_id="source-a",
            tenant_id="tenant-a",
            project_id="project-a",
            filename="policy.md",
            storage_path="policy.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
        session.add(doc)
        await session.flush()
        now = datetime.now(UTC)
        ver = DocumentVersion(
            id="version-a",
            document_id="doc-a",
            version_no=1,
            file_hash="abc123",
            status=DocumentStatus.READY,
            search_status="SEARCH_READY",
            search_ready_at=now,
            metadata_json={"security_partition_id": "partition-a"},
        )
        session.add(ver)
        await session.flush()
        checksum = "a" * 64
        session.add(
            IngestionRun(
                id="run-a",
                document_version_id="version-a",
                project_id="project-a",
                tenant_id="tenant-a",
                idempotency_key="idem-a",
                payload_hash="payload-a",
                status="COMPLETED",
                started_at=now,
            )
        )
        await session.flush()
        session.add(
            StageRun(
                id="stage-a",
                run_id="run-a",
                stage="INDEX_SEARCH",
                status="SUCCESS",
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": "search_units_project-a",
                    "search_unit_count": len(unit_specs),
                    "pg_count": len(unit_specs),
                    "qdrant_count": len(unit_specs),
                    "qdrant_indexed_count": len(unit_specs),
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        await session.flush()
        for idx, (unit_id, section_path, content) in enumerate(unit_specs):
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            block_id = f"block-{idx}"
            session.add(
                CanonicalBlock(
                    id=block_id,
                    document_version_id="version-a",
                    ordinal=idx,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path=section_path,
                    source_anchor=f"p1-b{idx}",
                    normalized_text=content,
                    content_hash=digest,
                )
            )
            await session.flush()
            session.add(
                SearchUnit(
                    id=unit_id,
                    document_version_id="version-a",
                    block_from_id=block_id,
                    block_to_id=block_id,
                    security_partition_id="partition-a",
                    content_hash=digest,
                    token_count=max(1, len(content.split())),
                    page_from=1,
                    page_to=1,
                    section_path=section_path,
                )
            )
        await session.commit()
    return engine, source


def _make_qdrant_point(unit_id: str, content: str, score: float) -> dict:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": generate_search_unit_point_id("search_units_project-a", unit_id),
        "score": score,
        "payload": {
            "search_unit_id": unit_id,
            "document_version_id": "version-a",
            "project_id": "project-a",
            "tenant_id": "tenant-a",
            "security_partition_id": "partition-a",
            "content_hash": digest,
            "content": content,
        },
    }


@pytest.mark.asyncio
async def test_ood_low_raw_dense_score_rejected_despite_max_score_normalization(monkeypatch):
    """S4-01: OOD query with raw Qdrant cosine=0.05 must NOT pass after max-score normalization."""
    unit_id = "unit-ood"
    content = "Internal deployment architecture uses Kubernetes and PostgreSQL."
    db_engine, source = await _setup_canonical_db(
        monkeypatch, [(unit_id, "Architecture", content)]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": 1}})
        if request.url.path.endswith("/points/query/batch"):
            return httpx.Response(
                200,
                json={"result": [{"points": [_make_qdrant_point(unit_id, content, 0.05)]}, {"points": []}]},
            )
        raise AssertionError(f"Unexpected Qdrant call: {request.method} {request.url.path}")

    monkeypatch.setattr(retrieval, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await retrieval._retrieve_search_unit_sections(
                engine,
                [source],
                "What is the recipe for chocolate cake?",
                principal=_make_principal(),
                top_k=5,
                candidate_limit=8,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
        assert outcome.sections == []
    finally:
        await db_engine.dispose()


def test_rerank_sections_keeps_lexical_match_even_when_raw_dense_below_floor():
    """S4-01: Lexical match survives even when raw_dense_score is below search_min_dense_score."""
    sec = RetrievedSection(
        chunk_id="c-lex",
        heading="Error Code Reference",
        content="Error code ERR-42 indicates an invalid token signature.",
        score=1.0,
        raw_dense_score=0.10,
        rank=0,
        source_id="src-1",
    )
    result = rerank_sections("ERR-42 invalid token", [sec], lexical=[sec], limit=5)
    assert [s.chunk_id for s in result.sections] == ["c-lex"]


@pytest.mark.asyncio
async def test_latency_fallback_filters_boilerplate_superseded_and_low_raw_dense_and_marks_degraded(monkeypatch):
    """S4-04: Latency fallback must still filter boilerplate, superseded, low cosine, and mark degraded."""
    specs = [
        ("unit-bp", "Footer", "All Rights Reserved. Confidential Disclaimer."),
        ("unit-sup", "[DEPRECATED] Old Policy", "Old authentication steps."),
        ("unit-ood", "Unrelated Cooking", "Boil pasta for eight minutes with salt."),
        ("unit-good", "Token Rotation", "Rotate API signing keys every 90 days."),
    ]
    db_engine, source = await _setup_canonical_db(monkeypatch, specs)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": 4}})
        if request.url.path.endswith("/points/query/batch"):
            dense_points = [
                _make_qdrant_point("unit-bp", specs[0][2], 0.90),
                _make_qdrant_point("unit-sup", specs[1][2], 0.85),
                _make_qdrant_point("unit-ood", specs[2][2], 0.05),
                _make_qdrant_point("unit-good", specs[3][2], 0.75),
            ]
            return httpx.Response(
                200,
                json={"result": [{"points": dense_points}, {"points": []}]},
            )
        raise AssertionError(f"Unexpected Qdrant call: {request.method} {request.url.path}")

    monkeypatch.setattr(retrieval, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(retrieval, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr(settings, "search_rerank_min_remaining_seconds", 999.0)
    try:
        async with httpx.AsyncClient(
            base_url="https://qdrant.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await retrieval._retrieve_search_unit_sections(
                engine,
                [source],
                "API key rotation schedule",
                principal=_make_principal(),
                top_k=5,
                candidate_limit=8,
                deadline=asyncio.get_running_loop().time() + 10.0,
            )
        kept_ids = [s.chunk_id for s in outcome.sections]
        assert kept_ids == ["unit-good"]
        assert outcome.stats.get("degraded") is True
        assert outcome.stats.get("degraded_reason") == "rerank_budget_exhausted"
    finally:
        await db_engine.dispose()


@pytest.mark.asyncio
async def test_dify_retrieval_compares_score_threshold_on_raw_dense_score(monkeypatch):
    """S4-02: Dify score_threshold must compare raw_dense_score, not inflated hybrid_score."""
    source = SimpleNamespace(id="source-a", name="Project A", sag_source_config_id="config-a")
    monkeypatch.setattr(dify_api, "get_authorized_source", AsyncMock(return_value=source))
    monkeypatch.setattr(dify_api, "versioned_source_ids", AsyncMock(return_value={"source-a"}))

    weak_sec = RetrievedSection(
        chunk_id="c-weak",
        search_unit_id="c-weak",
        document_id="doc-1",
        heading="Weak",
        content="Weak match inflated by hybrid RRF",
        score=0.68,
        raw_dense_score=0.20,
        source_id="source-a",
    )
    strong_sec = RetrievedSection(
        chunk_id="c-strong",
        search_unit_id="c-strong",
        document_id="doc-2",
        heading="Strong",
        content="Strong match",
        score=0.72,
        raw_dense_score=0.60,
        source_id="source-a",
    )
    monkeypatch.setattr(
        retrieval,
        "retrieve_search_unit_sections",
        AsyncMock(return_value=SearchOutcome(query="q", sections=[weak_sec, strong_sec], stats={})),
    )

    req = DifyRetrievalRequest(
        knowledge_id="source-a",
        query="q",
        retrieval_setting=DifyRetrievalSetting(top_k=5, score_threshold=0.50),
    )
    resp = await dify_api.retrieval(
        body=req,
        _authorized=None,
        principal=_make_principal(),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
    )
    assert [r.metadata["chunk_id"] for r in resp.records] == ["c-strong"]
    assert resp.records[0].score == pytest.approx(0.60)


def test_group_sections_by_document_enforces_absolute_floor_on_top_item():
    """S4-02: group_sections_by_document must enforce absolute_floor even on representatives[0]."""
    weak_candidates = [
        RetrievedSection(
            chunk_id="c1",
            document_id="doc-1",
            heading="H1",
            content="Weak noise 1",
            score=0.12,
            rank=0,
        ),
        RetrievedSection(
            chunk_id="c2",
            document_id="doc-2",
            heading="H2",
            content="Weak noise 2",
            score=0.08,
            rank=1,
        ),
    ]
    assert group_sections_by_document(weak_candidates, limit=10) == []
