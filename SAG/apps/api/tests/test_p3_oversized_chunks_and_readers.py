"""TDD tests for Milestone P3 Packet 2: oversized SearchUnit slicing, snapshots, and all 6 readers (S2-01)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zleap.sag.pipeline import SourceDescriptor, SourceType

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.db.models.routing_rag import CanonicalBlock, SearchUnit
from sag_api.enums import DocumentStatus
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.schemas.handover_assist import HandoverAssistSourceInput
from sag_api.services import (
    handover_assist_service,
    mind_map_service,
    search_index_service,
    search_unit_retrieval_service,
)
from sag_api.services.document_service import get_document_version_chunks


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:  # noqa: ANN001
        connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
async def p3_reader_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_oversized_prose_text() -> str:
    sentences = [
        f"Sentence {idx:02d} explains distinct shard partition behavior for alpha_{idx:02d} "
        f"with latency budget {10 + idx}ms across zone_{idx % 4}."
        for idx in range(1, 31)
    ]
    return " ".join(sentences)


def _make_oversized_table_text() -> str:
    rows = [
        "| Region | Service | Quota |",
        "| --- | --- | --- |",
    ]
    for idx in range(1, 25):
        rows.append(f"| apac-{idx:02d} | service-node-{idx:02d} | {100 + idx} GiB |")
    return "\n".join(rows)


async def _seed_version_with_blocks(
    session,
    *,
    doc_id: str = "doc-p3-1",
    ver_id: str = "ver-p3-1",
    project_id: str = "proj-p3",
    tenant_id: str = "tenant-p3",
    partition_id: str = "proj-p3:default",
    logical_source_id: str = "continuum:project_document:aaaaaaaaaaaaaaaaaaaaaaaa",
    blocks_spec: list[tuple[str, str, str]],
    max_tokens_per_unit: int = 80,
) -> tuple[list[CanonicalBlock], list[SearchUnit]]:
    now = datetime.now(UTC)
    session.add(Source(id=f"src-{doc_id}", name="Test Source", sag_source_config_id=f"cfg-{doc_id}"))
    await session.flush()
    session.add(
        Document(
            id=doc_id,
            source_id=f"src-{doc_id}",
            tenant_id=tenant_id,
            project_id=project_id,
            logical_source_id=logical_source_id,
            filename="oversized.md",
            storage_path="local://oversized.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
    )
    await session.flush()
    session.add(
        DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash=f"fh-{ver_id}",
            valid_from=now,
            status="READY",
            search_status="SEARCH_READY",
            knowledge_status="NOT_STARTED",
        )
    )
    await session.flush()

    blocks: list[CanonicalBlock] = []
    for ordinal, (block_type, section_path, norm_text) in enumerate(blocks_spec):
        blk = CanonicalBlock(
            id=f"blk-{ver_id}-{ordinal}",
            document_version_id=ver_id,
            ordinal=ordinal,
            block_type=block_type,
            page_from=1,
            page_to=1,
            section_path=section_path,
            source_anchor=f"anchor-{ordinal}",
            normalized_text=norm_text,
            content_hash=hashlib.sha256(norm_text.encode("utf-8")).hexdigest(),
        )
        session.add(blk)
        blocks.append(blk)
    await session.flush()

    units = search_index_service.build_search_units_from_blocks(
        blocks,
        document_version_id=ver_id,
        security_partition_id=partition_id,
        max_tokens_per_unit=max_tokens_per_unit,
    )
    for unit in units:
        session.add(unit)
    await session.commit()
    return blocks, units


@pytest.mark.asyncio
async def test_oversized_prose_and_table_slices_and_chunks_order(p3_reader_db):
    prose_text = _make_oversized_prose_text()
    table_text = _make_oversized_table_text()
    async with p3_reader_db() as session:
        _blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[
                ("paragraph", "Overview", prose_text),
                ("table", "Quotas", table_text),
            ],
            max_tokens_per_unit=64,
        )
        prose_units = [u for u in units if u.section_path == "Overview"]
        table_units = [u for u in units if u.section_path == "Quotas"]
        assert len(prose_units) >= 3
        assert len(table_units) >= 3

        # Monotonic char_start and non-empty slices across oversized sub-chunks
        for grp in (prose_units, table_units):
            starts = [u.char_start for u in grp]
            assert all(s is not None for s in starts)
            assert starts == sorted(starts)
            assert len(set(starts)) == len(starts)

        # Table sub-chunks 1+ carry repeated headers via text_snapshot
        assert table_units[0].text_snapshot is None
        assert all(u.text_snapshot is not None for u in table_units[1:])

        res = await get_document_version_chunks(
            session,
            project_id="proj-p3",
            document_id="doc-p3-1",
            version_no=1,
        )
        assert res.source_stage == "SEARCH_UNITS"
        assert res.total_chunks == len(units)

        returned_contents = [item.content for item in res.items]
        # No full-block duplication!
        assert len(set(returned_contents)) == len(returned_contents)
        assert all(c != prose_text for c in returned_contents)
        assert all(c != table_text for c in returned_contents)

        # Exact reading order & SHA-256 parity with SearchUnit.content_hash
        for item, expected_unit in zip(res.items, units, strict=True):
            assert item.chunk_id == str(expected_unit.id)
            assert hashlib.sha256(item.content.encode("utf-8")).hexdigest() == expected_unit.content_hash
            if expected_unit.section_path == "Quotas":
                assert "| Region | Service | Quota |" in item.content


@pytest.mark.asyncio
async def test_hydrate_scope_knowledge_index_preserves_oversized_split_units(p3_reader_db):
    prose_text = _make_oversized_prose_text()
    table_text = _make_oversized_table_text()
    async with p3_reader_db() as session:
        _blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[
                ("paragraph", "Overview", prose_text),
                ("table", "Quotas", table_text),
            ],
            max_tokens_per_unit=64,
        )
        source = await session.get(Source, "src-doc-p3-1")
        assert source is not None
        ver = await session.get(DocumentVersion, "ver-p3-1")
        assert ver is not None

        k_index = search_unit_retrieval_service._ScopeKnowledgeIndex(
            ku_by_version={},
            ku_by_id={},
            edges_by_ku={},
            ent_exact_versions={},
            hydrated_su_by_version={},
        )
        current_versions = {
            "ver-p3-1": search_unit_retrieval_service._ReadyVersion(
                document_version_id="ver-p3-1",
                source_id=source.id,
                project_id="proj-p3",
                tenant_id="tenant-p3",
                partition_id="proj-p3:default",
                source=source,
            )
        }
        await search_unit_retrieval_service._ensure_versions_hydrated(
            session,
            k_index,
            ["ver-p3-1"],
            current_versions,
            [source],
        )
        hydrated = k_index.hydrated_su_by_version.get("ver-p3-1", [])
        assert len(hydrated) == len(units)
        hydrated_by_id = {sec.search_unit_id: sec for sec, _s, _e in hydrated}
        for unit in units:
            assert unit.id in hydrated_by_id
            sec = hydrated_by_id[unit.id]
            assert hashlib.sha256(sec.content.encode("utf-8")).hexdigest() == unit.content_hash


@pytest.mark.asyncio
async def test_reuse_canonical_search_units_preserves_order_and_slices(p3_reader_db):
    prose_text = _make_oversized_prose_text()
    async with p3_reader_db() as session:
        _blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[("paragraph", "Overview", prose_text)],
            max_tokens_per_unit=64,
        )
        assert len(units) >= 3

    captured_chunk_sets = []

    async def fake_index(chunk_set_input, _descriptor, _opts):
        captured_chunk_sets.append(chunk_set_input)
        return SimpleNamespace(
            source_id="sag-src-1",
            chunk_ids=[c.client_key for c in chunk_set_input.chunks],
            generation_id="gen-1",
            chunk_version=chunk_set_input.chunk_version,
            source_version=chunk_set_input.source_version,
        )

    fake_engine = SimpleNamespace(
        resources=SimpleNamespace(relational=SimpleNamespace(session_factory=lambda: p3_reader_db)),
        _index=fake_index,
        _components=object(),
    )
    proc = IncrementalDocumentProcessor(
        fake_engine,
        "cfg-doc-p3-1",
        max_concurrency=1,
        document_version_id="ver-p3-1",
    )
    current = ProcessCheckpoint(source_id="cfg-doc-p3-1")

    async def noop_checkpoint(_cp):
        return None

    reused = await proc._ingest_from_canonical_search_units(
        current=current,
        descriptor=SourceDescriptor(
            data_source_id="cfg-doc-p3-1",
            source_type=SourceType.TEXT,
            title="oversized.md",
        ),
        on_checkpoint=noop_checkpoint,
    )
    assert reused is True
    assert len(captured_chunk_sets) == 1
    chunks = captured_chunk_sets[0].chunks
    assert len(chunks) == len(units)
    for chunk, expected_unit in zip(chunks, units, strict=True):
        assert chunk.metadata["search_unit_id"] == str(expected_unit.id)
        assert hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() == expected_unit.content_hash


@pytest.mark.asyncio
async def test_index_search_units_to_qdrant_rehydrated_fallback_uses_slices(p3_reader_db, monkeypatch):
    table_text = _make_oversized_table_text()
    async with p3_reader_db() as session:
        blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[("table", "Quotas", table_text)],
            max_tokens_per_unit=64,
        )
        assert len(units) >= 3
        ver = await session.get(DocumentVersion, "ver-p3-1")
        assert ver is not None

    # Re-load SearchUnits from a fresh DB session so ephemeral _text_content does not exist
    async with p3_reader_db() as fresh_session:
        rehydrated_units = list(
            (
                await fresh_session.scalars(
                    select(SearchUnit).where(SearchUnit.document_version_id == "ver-p3-1")
                )
            ).all()
        )
        assert all(not hasattr(u, "_text_content") for u in rehydrated_units)

    upserted_points: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/collections/" in request.url.path:
            vectors_cfg = {"content_vector": {"size": 4}, "bm25_sparse": {}}
            return httpx.Response(
                200,
                json={"result": {"config": {"params": {"vectors": vectors_cfg}}}},
            )
        if request.method == "PUT" and request.url.path.endswith("/points"):
            body = json.loads(request.content.decode("utf-8"))
            upserted_points.extend(body.get("points", []))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    class FakeEmbedder:
        model = "test-emb"

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    qdrant_client = httpx.AsyncClient(base_url="http://qdrant.local", transport=httpx.MockTransport(handler))
    try:
        count = await search_index_service.index_search_units_to_qdrant(
            qdrant_client,
            project_id="proj-p3",
            units=rehydrated_units,
            version=ver,
            embedder=FakeEmbedder(),
            blocks_by_id={b.id: b for b in blocks},
        )
        assert count == len(rehydrated_units)
        by_id = {
            pt["payload"]["search_unit_id"]: pt["payload"]
            for pt in upserted_points
            if not pt.get("payload", {}).get("_collection_meta")
        }
        for u in rehydrated_units:
            payload = by_id[str(u.id)]
            assert hashlib.sha256(payload["content"].encode("utf-8")).hexdigest() == u.content_hash
    finally:
        await qdrant_client.aclose()


@pytest.mark.asyncio
async def test_handover_and_mind_map_readers_resolve_oversized_slices(p3_reader_db):
    # Ensure block text is longer than _MAX_UNIT_CHARS (1600) so later slices sit past 1600 chars
    long_prose = " ".join(
        f"Clause {idx:03d} specifies distinct operational threshold {500 + idx} for cluster node_{idx:03d}."
        for idx in range(1, 55)
    )
    assert len(long_prose) > 3200
    async with p3_reader_db() as session:
        _blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[("paragraph", "Operations", long_prose)],
            max_tokens_per_unit=80,
        )
        assert len(units) >= 4
        assert units[-1].char_start is not None and units[-1].char_start > 1600

        principal = VerifiedPrincipal(
            subject="user-1",
            organization_id="tenant-p3",
            allowed_project_ids=frozenset({"proj-p3"}),
            issuer="test",
            key_id="k1",
            token_id="t1",
            issued_at=1,
            expires_at=9999999999,
            tenant_id="tenant-p3",
            allowed_partition_ids=frozenset({"proj-p3:default"}),
        )

        # 1. handover_assist_service._load_indexed_units
        handover_map = await handover_assist_service._load_indexed_units(
            session,
            principal=principal,
            project_id="proj-p3",
            sources=[
                HandoverAssistSourceInput(
                    kind="PROJECT_DOCUMENT",
                    id="aaaaaaaaaaaaaaaaaaaaaaaa",
                    version=1,
                    title="Oversized Doc",
                    logicalSourceId="continuum:project_document:aaaaaaaaaaaaaaaaaaaaaaaa",
                )
            ],
        )
        handover_items = handover_map[("continuum:project_document:aaaaaaaaaaaaaaaaaaaaaaaa", 1)]
        assert len(handover_items) == len(units)
        for (_kind, loc_val, _heading, text), expected_unit in zip(handover_items, units, strict=True):
            assert loc_val == str(expected_unit.id)
            assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected_unit.content_hash

        # 2. mind_map_service._select_evidence_catalog
        ready_doc = mind_map_service._ReadyDocumentScope(
            sag_document_id="doc-p3-1",
            public_document_id="aaaaaaaaaaaaaaaaaaaaaaaa",
            document_name="oversized.md",
            source_id="src-doc-p3-1",
            source_name="Test Source",
            document_version_id="ver-p3-1",
            version_no=1,
            manifest_fingerprint="fp-1",
            total_unit_count=len(units),
            updated_sort_key=1.0,
            project_id="proj-p3",
        )
        catalog, _coverage, _outline = await mind_map_service._select_evidence_catalog(
            session,
            principal=principal,
            ready_docs=[ready_doc],
            pending_count=0,
            topic=None,
        )
        assert len(catalog) == len(units)
        for ev, expected_unit in zip(catalog, units, strict=True):
            assert ev.unit.unit_id == str(expected_unit.id)
            assert hashlib.sha256(ev.unit.content.encode("utf-8")).hexdigest() == expected_unit.content_hash


@pytest.mark.asyncio
async def test_oversized_whitespace_normalized_prose_persists_text_snapshot(p3_reader_db):
    # Double spaces cause `" ".join(cur_spans)` to differ from exact slice, requiring text_snapshot
    norm_text = "   ".join(
        f"Clause {idx:02d} covers distinct compliance rule_{idx:02d} with weight {idx}."
        for idx in range(1, 28)
    )
    async with p3_reader_db() as session:
        _blocks, units = await _seed_version_with_blocks(
            session,
            blocks_spec=[("paragraph", "Compliance", norm_text)],
            max_tokens_per_unit=64,
        )
        assert len(units) >= 3
        starts = [u.char_start for u in units]
        assert all(s is not None for s in starts)
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)
        assert all(u.text_snapshot is not None for u in units)

        res = await get_document_version_chunks(
            session,
            project_id="proj-p3",
            document_id="doc-p3-1",
            version_no=1,
        )
        assert res.total_chunks == len(units)
        for item, expected_unit in zip(res.items, units, strict=True):
            assert item.chunk_id == str(expected_unit.id)
            assert hashlib.sha256(item.content.encode("utf-8")).hexdigest() == expected_unit.content_hash

