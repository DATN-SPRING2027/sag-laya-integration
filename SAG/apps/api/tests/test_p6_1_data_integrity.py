"""TDD regression tests for Batch P6-1 Packet 1: Reconcile race, Superseded 503, Boilerplate & Tables."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    SearchUnit,
    Source,
    SourceProjectMapping,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs import tasks
from sag_api.parsing.canonical import (
    _html_table_to_markdown,
    compute_content_hash,
    is_boilerplate_text,
)
from sag_api.services import search_index_service, search_unit_retrieval_service


@pytest_asyncio.fixture
async def p6_1_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_preserves_inflight_indexing_and_superseded_active_versions(p6_1_db):
    now = datetime.now(UTC)
    async with p6_1_db() as session:
        session.add(Source(id="src-p6", name="S", sag_source_config_id="cfg-p6"))
        session.add_all(
            [
                Document(
                    id="doc-active",
                    source_id="src-p6",
                    tenant_id="t1",
                    project_id="proj-p6",
                    filename="a.md",
                    storage_path="a.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc-inflight",
                    source_id="src-p6",
                    tenant_id="t1",
                    project_id="proj-p6",
                    filename="inflight.md",
                    storage_path="inflight.md",
                    status=DocumentStatus.EXTRACTING,
                    is_active=True,
                ),
                Document(
                    id="doc-inactive",
                    source_id="src-p6",
                    tenant_id="t1",
                    project_id="proj-p6",
                    filename="inactive.md",
                    storage_path="inactive.md",
                    status=DocumentStatus.READY,
                    is_active=False,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DocumentVersion(
                    id="ver-ready",
                    document_id="doc-active",
                    version_no=2,
                    file_hash="fh-2",
                    valid_from=now - timedelta(hours=1),
                    valid_to=now + timedelta(days=1),
                    status="READY",
                    search_status="SEARCH_READY",
                ),
                DocumentVersion(
                    id="ver-superseded",
                    document_id="doc-active",
                    version_no=1,
                    file_hash="fh-1",
                    valid_from=now - timedelta(days=2),
                    valid_to=now - timedelta(minutes=1),
                    status="READY",
                    search_status="SEARCH_READY",
                ),
                DocumentVersion(
                    id="ver-indexing",
                    document_id="doc-inflight",
                    version_no=1,
                    file_hash="fh-idx",
                    valid_from=now - timedelta(minutes=1),
                    valid_to=now + timedelta(days=1),
                    status="PROCESSING",
                    search_status="INDEXING",
                ),
                DocumentVersion(
                    id="ver-uncommitted-su",
                    document_id="doc-inflight",
                    version_no=2,
                    file_hash="fh-idx2",
                    valid_from=now - timedelta(minutes=1),
                    valid_to=now + timedelta(days=1),
                    status="PROCESSING",
                    search_status="INDEXING",
                ),
                DocumentVersion(
                    id="ver-failed",
                    document_id="doc-active",
                    version_no=3,
                    file_hash="fh-3",
                    valid_from=now,
                    valid_to=now + timedelta(days=1),
                    status="FAILED",
                    search_status="INDEX_FAILED",
                ),
                DocumentVersion(
                    id="ver-inactive",
                    document_id="doc-inactive",
                    version_no=1,
                    file_hash="fh-inact",
                    valid_from=now - timedelta(hours=1),
                    valid_to=now + timedelta(days=1),
                    status="READY",
                    search_status="SEARCH_READY",
                ),
            ]
        )
        await session.flush()

        for suffix, vid in (
            ("ready", "ver-ready"),
            ("sup", "ver-superseded"),
            ("idx", "ver-indexing"),
            ("fail", "ver-failed"),
            ("inact", "ver-inactive"),
        ):
            digest = hashlib.sha256(suffix.encode()).hexdigest()
            session.add(
                CanonicalBlock(
                    id=f"blk-{suffix}",
                    document_version_id=vid,
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="S",
                    normalized_text=suffix,
                    content_hash=digest,
                )
            )
            session.add(
                SearchUnit(
                    id=f"su-{suffix}",
                    document_version_id=vid,
                    block_from_id=f"blk-{suffix}",
                    block_to_id=f"blk-{suffix}",
                    security_partition_id="proj-p6",
                    content_hash=digest,
                    token_count=1,
                    page_from=1,
                    page_to=1,
                    section_path="S",
                )
            )
        await session.commit()

        meta_id = search_index_service.collection_meta_point_id("search_units_proj-p6")
        qdrant_points = [
            {"id": meta_id, "payload": {"_collection_meta": True, "payload_only": True}},
            {"id": "pt-ready", "payload": {"search_unit_id": "su-ready", "document_version_id": "ver-ready"}},
            {"id": "pt-sup", "payload": {"search_unit_id": "su-sup", "document_version_id": "ver-superseded"}},
            {"id": "pt-idx", "payload": {"search_unit_id": "su-idx", "document_version_id": "ver-indexing"}},
            # Simulates MVCC window where Qdrant point exists before SearchUnit row commits
            {
                "id": "pt-uncommitted",
                "payload": {"search_unit_id": "su-uncommitted", "document_version_id": "ver-uncommitted-su"},
            },
            {"id": "pt-fail", "payload": {"search_unit_id": "su-fail", "document_version_id": "ver-failed"}},
            {"id": "pt-inact", "payload": {"search_unit_id": "su-inact", "document_version_id": "ver-inactive"}},
            {"id": "pt-missing", "payload": {"search_unit_id": "su-gone", "document_version_id": "ver-gone"}},
        ]
        deleted_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/points/scroll"):
                return httpx.Response(200, json={"result": {"points": qdrant_points, "next_page_offset": None}})
            if request.url.path.endswith("/points/delete"):
                body = json.loads(request.content.decode("utf-8"))
                deleted_ids.extend(body.get("points", []))
                return httpx.Response(200, json={"result": {"status": "completed"}})
            return httpx.Response(200, json={"result": {}})

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            stats = await search_index_service.reconcile_orphan_search_units(
                session, q_client, project_id="proj-p6"
            )

        assert set(deleted_ids) == {"pt-fail", "pt-inact", "pt-missing"}
        assert "pt-idx" not in deleted_ids
        assert "pt-uncommitted" not in deleted_ids
        assert "pt-sup" not in deleted_ids
        assert "pt-ready" not in deleted_ids
        assert meta_id not in deleted_ids
        assert stats["orphans_deleted"] == 3


@pytest.mark.asyncio
async def test_reconcile_orphan_search_units_raises_on_non_2xx_scroll(p6_1_db):
    async with p6_1_db() as session:
        async with httpx.AsyncClient(
            base_url="http://qdrant.local",
            transport=httpx.MockTransport(lambda _req: httpx.Response(500, text="Internal Error")),
        ) as q_client:
            with pytest.raises(RuntimeError, match="HTTP 500"):
                await search_index_service.reconcile_orphan_search_units(
                    session, q_client, project_id="proj-err"
                )


@pytest.mark.asyncio
async def test_reconcile_search_index_discovers_emptied_project_and_uses_long_timeout(p6_1_db):
    observed_timeouts: list[float] = []
    deleted_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ext_timeout = request.extensions.get("timeout") or {}
        read_t = ext_timeout.get("read")
        if read_t is not None:
            observed_timeouts.append(float(read_t))
        if request.method == "GET" and request.url.path == "/collections":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "collections": [
                            {"name": "search_units_proj-emptied"},
                            {"name": "session_units_other"},
                        ]
                    }
                },
            )
        if request.url.path == "/collections/search_units_proj-emptied/points/scroll":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": "pt-orphan-emptied",
                                "payload": {
                                    "search_unit_id": "su-deleted",
                                    "document_version_id": "ver-deleted",
                                },
                            }
                        ],
                        "next_page_offset": None,
                    }
                },
            )
        if request.url.path == "/collections/search_units_proj-emptied/points/delete":
            body = json.loads(request.content.decode("utf-8"))
            deleted_ids.extend(body.get("points", []))
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    # Simulate the 8-second search client returned by EngineManager
    search_q_client = httpx.AsyncClient(
        base_url="http://qdrant.local",
        timeout=httpx.Timeout(8.0, connect=4.0),
        transport=httpx.MockTransport(handler),
    )
    mgr = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=search_q_client))

    try:
        async with p6_1_db() as session:
            # Zero Document rows in DB: project was emptied
            job = Job(
                id="job-rec-emptied",
                type=JobType.RECONCILE_SEARCH_INDEX,
                status=JobStatus.RUNNING,
                payload={},
            )
            session.add(job)
            await session.commit()

            await tasks.reconcile_search_index(session, job, engine_manager=mgr)

            assert deleted_ids == ["pt-orphan-emptied"]
            assert (job.payload or {}).get("orphans_deleted") == 1
            assert "proj-emptied" in ((job.payload or {}).get("projects") or {})
            assert observed_timeouts and all(t >= 30.0 for t in observed_timeouts)
            # Must NOT mutate the shared 8s search client timeout
            assert search_q_client.timeout.read == 8.0
    finally:
        await search_q_client.aclose()


@pytest.mark.asyncio
async def test_superseded_retrieval_third_revalidation_does_not_503(p6_1_db, monkeypatch):
    """End-to-end check that 3rd _load_current_ready_versions passes include_superseded=allows_superseded."""
    now = datetime.now(UTC)
    async with p6_1_db() as session:
        src = Source(id="src-sup3", name="Policy Src", sag_source_config_id="cfg-sup3")
        session.add(src)
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="map-sup3",
                source_id="src-sup3",
                organization_id="org-1",
                project_id="p-sup3",
                state="CONFIRMED",
                mapping_version=1,
                confirmed_at=now,
                confirmed_by="u1",
                approval_ref="ref-1",
            )
        )
        session.add(
            Document(
                id="doc-sup3",
                source_id="src-sup3",
                tenant_id="org-1",
                project_id="p-sup3",
                filename="policy.md",
                storage_path="/tmp/policy.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-old-3",
                document_id="doc-sup3",
                version_no=1,
                file_hash="fh-old-3",
                valid_from=now - timedelta(days=20),
                valid_to=now - timedelta(days=1),
                status="READY",
                search_status="SEARCH_READY",
                search_ready_at=now - timedelta(days=20),
                metadata_json={"security_partition_id": "part-1"},
            )
        )
        await session.flush()
        old_text = "Previous retention policy before 2025 was 30 days."
        old_hash = compute_content_hash(old_text)
        session.add(
            CanonicalBlock(
                id="blk-old-3",
                document_version_id="ver-old-3",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Retention Policy",
                source_anchor="p-0",
                normalized_text=old_text,
                content_hash=old_hash,
                is_boilerplate=False,
            )
        )
        await session.flush()
        session.add(
            SearchUnit(
                id="su-old-3",
                document_version_id="ver-old-3",
                block_from_id="blk-old-3",
                block_to_id="blk-old-3",
                security_partition_id="part-1",
                content_hash=old_hash,
                token_count=10,
                page_from=1,
                page_to=1,
                section_path="Retention Policy",
            )
        )
        run = IngestionRun(
            id="run-old-3",
            document_version_id="ver-old-3",
            tenant_id="org-1",
            project_id="p-sup3",
            idempotency_key="idem-old-3",
            payload_hash="ph-old-3",
            status="COMPLETED",
            started_at=now - timedelta(days=20),
        )
        session.add(run)
        await session.flush()
        point_id = search_index_service.generate_search_unit_point_id("search_units_p-sup3", "su-old-3")
        checksum = hashlib.sha256(f"{point_id}:{old_hash}".encode()).hexdigest()
        session.add(
            StageRun(
                id="sr-old-3",
                run_id="run-old-3",
                stage="INDEX_SEARCH",
                status="SUCCESS",
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": "search_units_p-sup3",
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                    "manifest_checksum": checksum,
                    "qdrant_checksum": checksum,
                },
            )
        )
        await session.commit()

    def qdrant_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/query/batch"):
            body = json.loads(request.content.decode("utf-8"))
            n = len(body.get("searches", []))
            pt = {
                "id": point_id,
                "score": 0.91,
                "payload": {
                    "search_unit_id": "su-old-3",
                    "document_version_id": "ver-old-3",
                    "document_id": "doc-sup3",
                    "source_id": "src-sup3",
                    "project_id": "p-sup3",
                    "tenant_id": "org-1",
                    "security_partition_id": "part-1",
                    "content_hash": old_hash,
                    "content": old_text,
                },
            }
            return httpx.Response(200, json={"result": [{"points": [pt]} for _ in range(n)]})
        return httpx.Response(200, json={"result": {"points": []}})

    class FakeEmb:
        model = "Qwen/Qwen3-Embedding-0.6B"

        async def generate(self, _text: str) -> list[float]:
            return [0.5, 0.5, 0.5, 0.5]

    q_client = httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(qdrant_handler)
    )
    mgr = SimpleNamespace(
        get_search_unit_qdrant_client=AsyncMock(return_value=q_client),
        get_sag_embedding=AsyncMock(return_value=FakeEmb()),
        get_routing_snapshot=AsyncMock(return_value=None),
    )
    principal = VerifiedPrincipal(
        subject="u1",
        organization_id="org-1",
        issuer="https://issuer.test",
        key_id="k1",
        token_id="tok-3",
        issued_at=1000,
        expires_at=9999999999,
        tenant_id="org-1",
        allowed_project_ids=frozenset({"p-sup3"}),
        allowed_partition_ids=frozenset({"part-1"}),
    )

    monkeypatch.setattr(search_unit_retrieval_service, "SessionLocal", p6_1_db)
    try:
        query_text = "What was the previous retention policy before 2025?"
        plan = search_unit_retrieval_service.plan_query(
            query_text, requested_top_k=5, temporal_mode="change_lineage"
        )
        outcome = await search_unit_retrieval_service.retrieve_search_unit_sections(
            mgr,
            [src],
            query_text,
            principal=principal,
            top_k=5,
            query_strategy_plan=plan,
        )
        assert len(outcome.sections) == 1
        assert outcome.sections[0].document_version_id == "ver-old-3"
        assert outcome.sections[0].is_superseded is True
    finally:
        await q_client.aclose()


@pytest.mark.asyncio
async def test_boilerplate_skip_vs_hydrate_and_qdrant_rebuild_match_hash(p6_1_db):
    now = datetime.now(UTC)
    b0_text = "Đoạn nội dung trang 1 về điều khoản hợp đồng lao động."
    b1_text = "Trang 2"
    b2_text = "Đoạn nội dung trang 2 tiếp nối điều khoản hợp đồng lao động."

    async with p6_1_db() as session:
        src = Source(id="src-bp-hyd", name="S", sag_source_config_id="cfg-bp-hyd")
        session.add(src)
        await session.flush()
        session.add(
            Document(
                id="doc-bp-hyd",
                source_id="src-bp-hyd",
                tenant_id="t1",
                project_id="proj-bp-hyd",
                filename="contract.md",
                storage_path="/tmp/contract.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        ver = DocumentVersion(
            id="ver-bp-hyd",
            document_id="doc-bp-hyd",
            version_no=1,
            file_hash="fh-bp-hyd",
            valid_from=now - timedelta(hours=1),
            status="READY",
            search_status="SEARCH_READY",
        )
        session.add(ver)
        await session.flush()
        b0 = CanonicalBlock(
            id="b0",
            document_version_id="ver-bp-hyd",
            ordinal=0,
            block_type="paragraph",
            normalized_text=b0_text,
            page_from=1,
            page_to=1,
            section_path="Hợp đồng",
            source_anchor="p-0",
            content_hash=compute_content_hash(b0_text),
            is_boilerplate=False,
        )
        b1 = CanonicalBlock(
            id="b1",
            document_version_id="ver-bp-hyd",
            ordinal=1,
            block_type="paragraph",
            normalized_text=b1_text,
            page_from=2,
            page_to=2,
            section_path="Hợp đồng",
            source_anchor="p-1",
            content_hash=compute_content_hash(b1_text),
            is_boilerplate=True,
        )
        b2 = CanonicalBlock(
            id="b2",
            document_version_id="ver-bp-hyd",
            ordinal=2,
            block_type="paragraph",
            normalized_text=b2_text,
            page_from=2,
            page_to=2,
            section_path="Hợp đồng",
            source_anchor="p-2",
            content_hash=compute_content_hash(b2_text),
            is_boilerplate=False,
        )
        session.add_all([b0, b1, b2])
        await session.flush()

        units = search_index_service.build_search_units_from_blocks(
            [b0, b1, b2],
            document_version_id="ver-bp-hyd",
            security_partition_id="part-1",
        )
        assert len(units) == 1
        unit = units[0]
        assert unit.block_from_id == "b0"
        assert unit.block_to_id == "b2"

        # 1. resolve_search_unit_text must ignore boilerplate blocks in span
        resolved = search_index_service.resolve_search_unit_text(unit, [b0, b1, b2])
        assert hashlib.sha256(resolved.encode("utf-8")).hexdigest() == unit.content_hash
        assert "Trang 2" not in resolved

        session.add(unit)
        await session.commit()

        # 2. _ensure_versions_hydrated must NOT drop unit due to SHA-256 mismatch
        k_index = search_unit_retrieval_service._ScopeKnowledgeIndex(
            ku_by_version={},
            ku_by_id={},
            edges_by_ku={},
            ent_exact_versions={},
            hydrated_su_by_version={},
        )
        current_versions = {
            "ver-bp-hyd": search_unit_retrieval_service._ReadyVersion(
                document_version_id="ver-bp-hyd",
                source_id=src.id,
                project_id="proj-bp-hyd",
                tenant_id="t1",
                partition_id="part-1",
                source=src,
            )
        }
        await search_unit_retrieval_service._ensure_versions_hydrated(
            session, k_index, ["ver-bp-hyd"], current_versions, [src]
        )
        hydrated = k_index.hydrated_su_by_version.get("ver-bp-hyd", [])
        assert len(hydrated) == 1
        assert hydrated[0][0].search_unit_id == unit.id
        assert "Trang 2" not in hydrated[0][0].content

        # 3. index_search_units_to_qdrant rehydrated fallback must exclude boilerplate block
        rehydrated_unit = await session.get(SearchUnit, unit.id)
        assert rehydrated_unit is not None
        if hasattr(rehydrated_unit, "_text_content"):
            delattr(rehydrated_unit, "_text_content")
        if hasattr(rehydrated_unit, "_body_block_ids"):
            delattr(rehydrated_unit, "_body_block_ids")

        upserted: list[dict] = []

        def q_handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/collections/" in request.url.path and "/points/" not in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "config": {
                                "params": {
                                    "vectors": {"content_vector": {"size": 4}},
                                    "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                                }
                            },
                            "payload_schema": {},
                        }
                    },
                )
            if request.method == "GET" and "/points/" in request.url.path:
                return httpx.Response(404, json={})
            if request.method == "PUT" and request.url.path.endswith("/points"):
                body = json.loads(request.content.decode("utf-8"))
                upserted.extend(body.get("points", []))
            return httpx.Response(200, json={"result": {"status": "completed"}})

        class FakeEmb:
            model = "test-emb"

            async def batch_generate(self, texts: list[str]) -> list[list[float]]:
                return [[0.25, 0.25, 0.25, 0.25] for _ in texts]

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(q_handler)
        ) as q_client:
            await search_index_service.index_search_units_to_qdrant(
                q_client,
                project_id="proj-bp-hyd",
                units=[rehydrated_unit],
                version=ver,
                embedder=FakeEmb(),
                blocks_by_id={"b0": b0, "b1": b1, "b2": b2},
                block_vectors={"b0": [1.0, 0.0, 0.0, 0.0], "b2": [0.0, 1.0, 0.0, 0.0]},
            )
        su_pts = [p for p in upserted if not p.get("payload", {}).get("_collection_meta")]
        assert len(su_pts) == 1
        assert hashlib.sha256(su_pts[0]["payload"]["content"].encode("utf-8")).hexdigest() == unit.content_hash
        assert "Trang 2" not in su_pts[0]["payload"]["content"]


def test_boilerplate_patterns_preserve_technical_labels():
    for label in ("P0", "P1", "P2", "PG16", "TR1", "3 / 4", "Điều 5"):
        assert is_boilerplate_text(label) is False, f"False positive boilerplate on {label!r}"

    for marker in ("Trang 5", "Page 3 / 10", "Page 2 of 10", "p. 5", "p.5", "pg. 12", "tr. 3", "- 5 -", "— 12 —"):
        assert is_boilerplate_text(marker) is True, f"Missed boilerplate on {marker!r}"


def test_oversized_header_only_table_emits_unit():
    hdr_tbl_text = (
        "| "
        + " | ".join(f"Cột tiêu đề số {i}" for i in range(40))
        + " |\n| "
        + " | ".join("---" for _ in range(40))
        + " |"
    )
    hdr_blk = CanonicalBlock(
        id="hdr-1",
        document_version_id="ver-1",
        ordinal=0,
        block_type="table",
        normalized_text=hdr_tbl_text,
        page_from=1,
        page_to=1,
        section_path="Bảng",
        content_hash=compute_content_hash(hdr_tbl_text),
        is_boilerplate=False,
    )
    units = search_index_service.build_search_units_from_blocks(
        [hdr_blk],
        document_version_id="ver-1",
        security_partition_id="public",
        max_tokens_per_unit=100,
    )
    assert len(units) == 1
    assert units[0].content_hash == compute_content_hash(hdr_tbl_text)
    assert search_index_service.resolve_search_unit_text(units[0], [hdr_blk]) == hdr_tbl_text


def test_html_table_to_markdown_honors_colspan_and_rowspan():
    html_tbl = (
        "<table>"
        "<tr><th>Kỳ</th><th colspan='2'>Năm 2025</th><th>Ghi chú</th></tr>"
        "<tr><td rowspan='2'>Q1</td><td>Doanh thu</td><td>100 tỷ</td><td>OK</td></tr>"
        "<tr><td>Chi phí</td><td>40 tỷ</td><td>OK</td></tr>"
        "</table>"
    )
    md = _html_table_to_markdown(html_tbl)
    lines = md.splitlines()
    assert len(lines) == 4  # header, sep, row1, row2
    cells = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in lines]
    assert all(len(row) == 4 for row in cells)
    assert cells[0][0] == "Kỳ"
    assert cells[0][1] == "Năm 2025"
    assert cells[0][3] == "Ghi chú"
    assert cells[2] == ["Q1", "Doanh thu", "100 tỷ", "OK"]
    assert cells[3] == ["Q1", "Chi phí", "40 tỷ", "OK"]

    # Ensure td colspan does not duplicate numeric values and huge rowspan is clamped
    html_span_num = (
        "<table>"
        "<tr><th>Item</th><th>A</th><th>B</th></tr>"
        "<tr><td rowspan='99'>Total</td><td colspan='2'>500 tỷ</td></tr>"
        "</table>"
    )
    md2 = _html_table_to_markdown(html_span_num)
    lines2 = md2.splitlines()
    assert len(lines2) == 3  # clamped to real rows (no phantom rows!)
    row_total = [c.strip() for c in lines2[2].strip().strip("|").split("|")]
    assert row_total == ["Total", "500 tỷ", ""]
