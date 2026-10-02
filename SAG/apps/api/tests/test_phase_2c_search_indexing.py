"""Unit and integration tests for Phase 2C: Search Indexing & Qdrant.

Tests:
1. Natural boundary chunking (heading, table).
2. SearchUnit fields and deterministic UUIDv5 point ID generation.
3. Pre-provisioned Tree Routing fields in Qdrant payload.
4. Per-Project Collection naming format: search_units_{project_id}.
5. Manifest verification between PostgreSQL SearchUnits and Qdrant points.
6. Idempotent retry cleanup of previous SearchUnits.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import uuid
import httpx
import pytest
from sqlalchemy import select

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    IngestionRun,
    SearchUnit,
    StageRun,
)
from sag_api.services.search_index_service import (
    build_qdrant_payload,
    build_search_units_from_blocks,
    compute_sparse_bm25_vector,
    generate_search_unit_point_id,
    index_search_units_to_qdrant,
    run_search_indexing_stage,
)
from sag_api.services.rebuild_service import (
    rebuild_search_index_for_project,
    rebuild_search_index_for_version,
)


def test_deterministic_uuidv5_point_id():
    """Kiểm tra point_id được sinh ổn định theo công thức UUIDv5 chuẩn."""
    collection = "search_units_proj_alpha"
    unit_id = "unit_12345"

    point_id_1 = generate_search_unit_point_id(collection, unit_id)
    point_id_2 = generate_search_unit_point_id(collection, unit_id)

    assert point_id_1 == point_id_2
    assert uuid.UUID(point_id_1).version == 5


def test_chunker_respects_natural_boundaries():
    """Kiểm tra chunker tách SearchUnit theo heading và table tự nhiên."""
    ver_id = str(uuid.uuid4())
    blocks = [
        CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="heading",
            page_from=1,
            page_to=1,
            section_path="Chương 1",
            normalized_text="Chương 1: Kiến trúc",
            content_hash="h0",
        ),
        CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Chương 1",
            normalized_text="Đoạn văn giải thích kiến trúc.",
            content_hash="h1",
        ),
        CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=2,
            block_type="table",
            page_from=1,
            page_to=1,
            section_path="Chương 1",
            normalized_text="| Cột 1 | Cột 2 |\n|---|---|\n| A | B |",
            content_hash="h2",
        ),
        CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=3,
            block_type="heading",
            page_from=2,
            page_to=2,
            section_path="Chương 2",
            normalized_text="Chương 2: Cài đặt",
            content_hash="h3",
        ),
    ]

    units = build_search_units_from_blocks(
        blocks,
        document_version_id=ver_id,
        security_partition_id="sec_part_1",
    )

    # Phải có ít nhất 3 search units do gặp table và heading mới
    assert len(units) >= 3
    assert units[0].block_from_id == blocks[0].id
    assert units[0].block_to_id == blocks[1].id
    assert units[0].security_partition_id == "sec_part_1"


def test_pre_provisioned_tree_routing_fields_in_payload():
    """Kiểm tra payload Qdrant có sẵn các trường Tree Routing (Phase 6/8) để tránh migrate schema sau này."""
    ver_id = str(uuid.uuid4())
    unit = SearchUnit(
        id=str(uuid.uuid4()),
        document_version_id=ver_id,
        block_from_id=str(uuid.uuid4()),
        block_to_id=str(uuid.uuid4()),
        security_partition_id="part_hr_confidential",
        content_hash="hash_content_sample",
        token_count=120,
        page_from=1,
        page_to=2,
        section_path="Section A > Sub B",
    )
    now = datetime.now(UTC)
    version = DocumentVersion(
        id=ver_id,
        document_id=str(uuid.uuid4()),
        version_no=1,
        file_hash="hash_file",
        valid_from=now,
        valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    )

    payload = build_qdrant_payload(unit, version=version)

    assert payload["_sag_id"] == unit.id
    assert payload["security_partition_id"] == "part_hr_confidential"
    assert payload["valid_from"] == now.isoformat()
    # 4 trường cốt lõi của Tree Routing
    assert "primary_node_a" in payload and payload["primary_node_a"] is None
    assert "primary_node_b" in payload and payload["primary_node_b"] is None
    assert "tree_version_a" in payload and payload["tree_version_a"] is None
    assert "tree_version_b" in payload and payload["tree_version_b"] is None


class MockEmbedder:
    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        return [[0.2, 0.4, 0.6] for _ in texts]


@pytest.mark.asyncio
async def test_search_indexing_stage_and_manifest_verification():
    """Kiểm tra toàn bộ luồng stage INDEX_SEARCH với real embedding và đối soát số lượng (manifest verification)."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    requests_log: list[httpx.Request] = []
    stored_points: list[dict[str, Any]] = []

    def mock_qdrant_handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
        url_path = request.url.path
        if url_path.endswith("/points/count"):
            return httpx.Response(200, json={"result": {"count": len(stored_points)}})
        if url_path.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": stored_points, "next_page_offset": None}})
        if request.method == "PUT" and "/points" in url_path:
            import json
            body = json.loads(request.content.decode("utf-8"))
            stored_points.extend(body.get("points", []))
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
        if request.method == "GET":
            return httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"content_vector": {"size": 3}}}}}})
        return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_qdrant_handler),
        base_url="http://localhost:6333",
    )

    async with SessionLocal() as session:
        # Tạo Document & DocumentVersion
        doc = Document(id=doc_id, source_id=None, filename="arch.md", storage_path="/tmp/arch.md")
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_arch",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add(ver)
        await session.commit()

        # Tạo 2 canonical blocks
        b1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="heading",
            page_from=1,
            page_to=1,
            section_path="Title",
            normalized_text="Hệ Thống Phân Tán",
            content_hash="h1",
        )
        b2 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Title",
            normalized_text="Mô hình Master-Worker được áp dụng.",
            content_hash="h2",
        )
        session.add_all([b1, b2])

        ingestion_run = IngestionRun(
            id=run_id,
            tenant_id="tenant_default",
            project_id=project_id,
            document_version_id=ver_id,
            idempotency_key="key_search_idx",
            payload_hash="hash_arch",
            status="RUNNING",
        )
        session.add(ingestion_run)
        await session.commit()

        # Thực thi stage với mock_client và real MockEmbedder
        mock_embedder = MockEmbedder()
        units = await run_search_indexing_stage(
            session,
            project_id=project_id,
            document_version=ver,
            security_partition_id="part_internal",
            qdrant_client=mock_client,
            embedder=mock_embedder,
            run_id=run_id,
        )
        await session.commit()

        assert len(units) >= 1

        # Xác minh StageRun
        stage_run = (
            await session.execute(
                select(StageRun).where(StageRun.run_id == run_id, StageRun.stage == "INDEX_SEARCH")
            )
        ).scalar_one()

        assert stage_run.status == "SUCCESS"
        assert stage_run.metrics_json["collection_name"] == f"search_units_{project_id}"
        assert stage_run.metrics_json["manifest_verified"] is True
        assert stage_run.metrics_json["qdrant_indexed_count"] == len(units)

        # Xác minh mock Qdrant nhận đúng collection_name và vector embedding thật
        points_requests = [
            r for r in requests_log
            if f"/collections/search_units_{project_id}/points" in r.url.path and r.method == "PUT"
        ]
        assert len(points_requests) >= 1
        body = json.loads(points_requests[0].content)
        assert "points" in body
        assert body["points"][0]["vector"]["content_vector"] == [0.2, 0.4, 0.6]

        # Xác minh DocumentVersion đạt SEARCH_READY
        ver_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status == "SEARCH_READY"
        assert ver_updated.search_ready_at is not None


@pytest.mark.asyncio
async def test_search_readiness_fails_without_qdrant_client():
    """Kiểm tra nếu không có qdrant_client thì không bao giờ được đánh dấu READY (fail-closed)."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, source_id=None, filename="fail_ready.md", storage_path="/tmp/f.md")
        session.add(doc)
        await session.commit()

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_f",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add(ver)
        await session.commit()

        b1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Nội dung cần index nhưng không có Qdrant client.",
            content_hash="hf1",
        )
        session.add(b1)

        ingestion_run = IngestionRun(
            id=run_id,
            tenant_id="tenant_default",
            project_id=project_id,
            document_version_id=ver_id,
            idempotency_key="key_fail_qdrant",
            payload_hash="hash_f",
            status="RUNNING",
        )
        session.add(ingestion_run)
        await session.commit()

        # Không truyền qdrant_client -> Bắt buộc ném lỗi và search_status là INDEX_FAILED
        with pytest.raises(RuntimeError) as exc_info:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id="part_internal",
                qdrant_client=None,
                run_id=run_id,
            )

        assert "Search indexing failed" in str(exc_info.value)
        await session.commit()

        # Xác minh DocumentVersion KHÔNG được READY
        ver_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status == "INDEX_FAILED"
        assert ver_updated.search_ready_at is None


def test_sparse_bm25_weighting_common_vs_rare_terms():
    """Kiểm tra trọng số BM25: từ hiếm/đặc trưng có trọng số lớn hơn nhiều so với stopwords."""
    text_short = "Kiến trúc microservices kubernetes là một giải pháp trong hệ thống"
    vec = compute_sparse_bm25_vector(text_short, avg_doc_len=10.0)

    assert len(vec["indices"]) > 0
    assert len(vec["values"]) == len(vec["indices"])

    # Token "kubernetes" hoặc "microservices" là content words (hiếm) -> giá trị BM25 cao
    # Token "là", "trong", "một" là stopwords -> giá trị BM25 bị nén thấp (~0.1)
    import hashlib
    stopword_idx = int(hashlib.md5("trong".encode("utf-8")).hexdigest()[:8], 16) % 1000000
    rare_idx = int(hashlib.md5("kubernetes".encode("utf-8")).hexdigest()[:8], 16) % 1000000

    idx_map = dict(zip(vec["indices"], vec["values"]))
    assert rare_idx in idx_map
    assert stopword_idx in idx_map
    assert idx_map[rare_idx] > idx_map[stopword_idx] * 5.0

    # Kiểm tra length normalization: văn bản quá dài bị giảm trọng số
    text_long = " ".join(["từ"] * 100 + ["kubernetes"])
    vec_long = compute_sparse_bm25_vector(text_long, avg_doc_len=10.0)
    idx_long_map = dict(zip(vec_long["indices"], vec_long["values"]))
    assert idx_long_map[rare_idx] < idx_map[rare_idx]


@pytest.mark.asyncio
async def test_empty_units_with_stale_qdrant_points_fails_ready():
    """Kiểm tra hồi quy: Khi units rỗng nhưng Qdrant vẫn còn point rác (delete thất bại), gate phải gắn INDEX_FAILED."""
    await init_db()
    project_id = f"proj_empty_{uuid.uuid4().hex[:8]}"
    ver_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="empty.md", storage_path="/tmp/empty.md", project_id=project_id)
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_empty",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add_all([doc, ver])
        await session.commit()

        # Mock Qdrant client giả định delete failed và vẫn còn 2 điểm trên Qdrant
        def fake_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if request.method == "POST" and "/points/delete" in url_str:
                return httpx.Response(200, json={"status": "ok"})
            if request.method == "POST" and "/points/count" in url_str:
                # Còn 2 điểm sót lại trên Qdrant
                return httpx.Response(200, json={"result": {"count": 2}})
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(fake_handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock-qdrant:6333") as client:
            with pytest.raises(RuntimeError):
                await run_search_indexing_stage(
                    session,
                    project_id=project_id,
                    document_version=ver,
                    security_partition_id="part_internal",
                    qdrant_client=client,
                )

        ver_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status == "INDEX_FAILED"
        assert ver_updated.search_ready_at is None


@pytest.mark.asyncio
async def test_rebuild_service_two_project_isolation():
    """Kiểm tra Rebuild Service: Rebuild project A tuyệt đối không nạp DocumentVersion của project B."""
    await init_db()
    proj_a = f"proj_iso_a_{uuid.uuid4().hex[:8]}"
    proj_b = f"proj_iso_b_{uuid.uuid4().hex[:8]}"

    doc_a_id = str(uuid.uuid4())
    doc_b_id = str(uuid.uuid4())
    ver_a_id = str(uuid.uuid4())
    ver_b_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc_a = Document(id=doc_a_id, filename="a.md", storage_path="/tmp/a.md", project_id=proj_a)
        doc_b = Document(id=doc_b_id, filename="b.md", storage_path="/tmp/b.md", project_id=proj_b)
        ver_a = DocumentVersion(
            id=ver_a_id,
            document_id=doc_a_id,
            version_no=1,
            file_hash="hash_a",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "sec_part_a"},
        )
        ver_b = DocumentVersion(
            id=ver_b_id,
            document_id=doc_b_id,
            version_no=1,
            file_hash="hash_b",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "sec_part_b"},
        )
        session.add_all([doc_a, doc_b, ver_a, ver_b])
        await session.commit()

        # Tạo CanonicalBlock và SearchUnit cho cả 2
        b_a = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_a_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="A",
            normalized_text="Nội dung thuộc về Project A",
            content_hash="h_a",
        )
        su_a = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=ver_a_id,
            security_partition_id="sec_part_a",
            block_from_id=b_a.id,
            block_to_id=b_a.id,
            page_from=1,
            page_to=1,
            section_path="A",
            token_count=10,
            content_hash="h_a",
        )
        b_b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_b_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="B",
            normalized_text="Nội dung thuộc về Project B",
            content_hash="h_b",
        )
        su_b = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=ver_b_id,
            security_partition_id="sec_part_b",
            block_from_id=b_b.id,
            block_to_id=b_b.id,
            page_from=1,
            page_to=1,
            section_path="B",
            token_count=10,
            content_hash="h_b",
        )
        session.add_all([b_a, su_a, b_b, su_b])
        await session.commit()

        # Mock Qdrant client ghi nhận các point upserted
        upserted_points = []

        def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if request.method == "PUT" and "/points" in url_str:
                body = json.loads(request.content)
                upserted_points.extend(body.get("points", []))
                return httpx.Response(200, json={"status": "ok"})
            if request.method == "POST" and "/points/count" in url_str:
                return httpx.Response(200, json={"result": {"count": len(upserted_points)}})
            if request.method == "POST" and "/points/scroll" in url_str:
                return httpx.Response(200, json={"result": {"points": upserted_points}})
            return httpx.Response(200, json={"status": "ok"})

        mock_embedder = [0.1] * 8
        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock-qdrant:6333") as client:
            res_rebuild = await rebuild_search_index_for_project(
                session,
                project_id=proj_a,
                qdrant_client=client,
                embedder=lambda t: mock_embedder,
            )

        assert res_rebuild["project_id"] == proj_a
        assert res_rebuild["rebuilt_versions_count"] == 1
        # Bằng chứng cách ly: Tất cả các point nạp vào search_units_proj_a đều thuộc về ver_a_id, KHÔNG CÓ ver_b_id
        for pt in upserted_points:
            assert pt["payload"]["document_version_id"] == ver_a_id
            assert pt["payload"]["document_version_id"] != ver_b_id


@pytest.mark.asyncio
async def test_rebuild_service_unmapped_partition_fails_closed():
    """Kiểm tra Rebuild Service: DocumentVersion không có security_partition_id phải fail closed, không fallback public."""
    await init_db()
    project_id = f"proj_unmapped_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="unmapped.md", storage_path="/tmp/u.md", project_id=project_id)
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_u",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={},  # Thiếu security_partition_id
        )
        session.add_all([doc, ver])
        await session.commit()

        b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Không có partition",
            content_hash="h_u",
        )
        # SearchUnit cũng không có hoặc rỗng
        su = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            security_partition_id="",  # Rỗng / unmapped
            block_from_id=b.id,
            block_to_id=b.id,
            page_from=1,
            page_to=1,
            section_path="Intro",
            token_count=5,
            content_hash="h_u",
        )
        session.add_all([b, su])
        await session.commit()

        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"}))
        async with httpx.AsyncClient(transport=transport, base_url="http://mock-qdrant:6333") as client:
            with pytest.raises(ValueError) as exc_info:
                await rebuild_search_index_for_project(
                    session,
                    project_id=project_id,
                    qdrant_client=client,
                    embedder=lambda t: [0.1] * 8,
                )

            assert "unmapped security_partition_id" in str(exc_info.value)

        ver_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status == "INDEX_FAILED"


@pytest.mark.asyncio
async def test_manifest_fails_closed_when_checksum_mismatch_or_scroll_error():
    """Kiểm tra manifest gate fail-closed (#1): Nếu scroll lỗi hoặc checksum không khớp, fail closed."""
    await init_db()
    project_id = f"proj_fc_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, project_id=project_id, filename="fc.md", storage_path="/tmp/fc.md")
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="h_fc",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add_all([doc, ver])
        await session.commit()

        b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Main",
            normalized_text="Nội dung kiểm tra fail-closed khi scroll lỗi.",
            content_hash="h_block_fc",
        )
        session.add(b)
        await session.commit()

        # Mock handler nơi scroll API trả về lỗi 500
        def broken_scroll_handler(request: httpx.Request) -> httpx.Response:
            url_path = request.url.path
            if url_path.endswith("/points/count"):
                return httpx.Response(200, json={"result": {"count": 1}})
            if url_path.endswith("/points/scroll"):
                return httpx.Response(500, json={"error": "Scroll internal error"})
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(broken_scroll_handler),
            base_url="http://localhost:6333",
        ) as broken_client:
            with pytest.raises(RuntimeError) as exc_info:
                await run_search_indexing_stage(
                    session,
                    project_id=project_id,
                    document_version=ver,
                    security_partition_id="part_1",
                    qdrant_client=broken_client,
                    embedder=lambda t: [0.1, 0.2, 0.3],
                )

            assert "manifest verification mismatch" in str(exc_info.value)

        await session.refresh(ver)
        assert ver.search_status == "INDEX_FAILED"
        assert ver.search_ready_at is None


def test_oversized_canonical_block_chunked_within_token_limit():
    """Kiểm tra oversized block (#14): Block 1200 từ được tách thành các units <= 512 tokens."""
    ver_id = str(uuid.uuid4())
    words = [f"token_{i}" for i in range(1200)]
    long_text = " ".join(words)

    long_block = CanonicalBlock(
        id=str(uuid.uuid4()),
        document_version_id=ver_id,
        ordinal=0,
        block_type="paragraph",
        page_from=3,
        page_to=4,
        section_path="DeepSection > Sub",
        normalized_text=long_text,
        content_hash="hash_long",
    )

    units = build_search_units_from_blocks(
        [long_block],
        document_version_id=ver_id,
        security_partition_id="sec_p1",
        max_tokens_per_unit=512,
    )

    assert len(units) == 3
    assert units[0].token_count == 512
    assert units[1].token_count == 512
    assert units[2].token_count == 176
    for u in units:
        assert u.token_count <= 512
        assert u.block_from_id == long_block.id
        assert u.block_to_id == long_block.id
        assert u.section_path == "DeepSection > Sub"
        assert u.page_from == 3
        assert u.page_to == 4


@pytest.mark.asyncio
async def test_rebuild_service_with_custom_dim_and_conflicting_project():
    """Kiểm tra rebuild với embedder 1024-dim (#9) và phát hiện conflict IngestionRun project_id (#2)."""
    await init_db()
    proj_a = f"proj_a_{uuid.uuid4().hex[:8]}"
    proj_b = f"proj_b_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        # Document thuộc proj_a
        doc = Document(id=doc_id, project_id=proj_a, tenant_id="t_cust", filename="conf.md", storage_path="/tmp/c.md")
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hc",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            metadata_json={"security_partition_id": "sec_part_a"},
        )
        session.add_all([doc, ver])
        await session.commit()

        b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Valid text",
            content_hash="hb",
        )
        su = SearchUnit(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            security_partition_id="sec_part_a",
            block_from_id=b.id,
            block_to_id=b.id,
            page_from=1,
            page_to=1,
            section_path="Root",
            token_count=2,
            content_hash="hb",
        )
        # Giả lập IngestionRun có project_id = proj_b (xung đột dữ liệu với doc.project_id = proj_a)
        ir_conflict = IngestionRun(
            id=str(uuid.uuid4()),
            tenant_id="t_cust",
            project_id=proj_b,  # Conflict!
            document_version_id=ver_id,
            idempotency_key="key_conflict",
            payload_hash="hc",
            status="QUEUED",
        )
        session.add_all([b, su, ir_conflict])
        await session.commit()

        stored: list[dict[str, Any]] = []

        def mock_qdrant(req: httpx.Request) -> httpx.Response:
            url = req.url.path
            if url.endswith("/points/count"):
                return httpx.Response(200, json={"result": {"count": len(stored)}})
            if url.endswith("/points/scroll"):
                return httpx.Response(200, json={"result": {"points": stored, "next_page_offset": None}})
            if req.method == "PUT" and "/points" in url:
                import json
                body = json.loads(req.content.decode("utf-8"))
                stored.extend(body.get("points", []))
                return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
            return httpx.Response(200, json={"result": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(mock_qdrant),
            base_url="http://localhost:6333",
        ) as client:
            # Rebuild project_a phải phát hiện IngestionRun có project_id proj_b và fail closed
            with pytest.raises(ValueError) as exc_info:
                await rebuild_search_index_for_project(
                    session,
                    project_id=proj_a,
                    qdrant_client=client,
                    embedder=lambda t: [0.1] * 1024,
                )

            assert "conflicting IngestionRun project_id" in str(exc_info.value)
            assert "rebuild failed closed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tenant_id_isolation_in_payload():
    """Kiểm tra tenant_id (#6) được truyền đúng từ Document/IngestionRun vào Qdrant payload."""
    await init_db()
    project_id = f"proj_ten_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, project_id=project_id, tenant_id="tenant_enterprise_xyz", filename="t.md", storage_path="/tmp/t.md")
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="ht",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add_all([doc, ver])
        await session.commit()

        b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Multi-tenant test content",
            content_hash="h_tenant_b",
        )
        session.add(b)
        await session.commit()

        stored_points: list[dict[str, Any]] = []

        def mock_qdrant(req: httpx.Request) -> httpx.Response:
            url = req.url.path
            if url.endswith("/points/count"):
                return httpx.Response(200, json={"result": {"count": len(stored_points)}})
            if url.endswith("/points/scroll"):
                return httpx.Response(200, json={"result": {"points": stored_points, "next_page_offset": None}})
            if req.method == "PUT" and "/points" in url:
                import json
                body = json.loads(req.content.decode("utf-8"))
                stored_points.extend(body.get("points", []))
                return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
            return httpx.Response(200, json={"result": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(mock_qdrant),
            base_url="http://localhost:6333",
        ) as client:
            await run_search_indexing_stage(
                session,
                project_id=project_id,
                document_version=ver,
                security_partition_id="part_custom",
                qdrant_client=client,
                embedder=lambda t: [0.1, 0.2, 0.3],
            )

        assert len(stored_points) == 1
        assert stored_points[0]["payload"]["tenant_id"] == "tenant_enterprise_xyz"


@pytest.mark.asyncio
async def test_delete_document_points_and_reconcile_orphans():
    """Kiểm tra helper xóa points theo versions và dọn dẹp orphan points trong Qdrant (#8)."""
    await init_db()
    project_id = f"proj_clean_{uuid.uuid4().hex[:8]}"

    qdrant_state: list[dict[str, Any]] = [
        {"id": "pt_1", "payload": {"search_unit_id": "su_1", "document_version_id": "v1"}},
        {"id": "pt_2", "payload": {"search_unit_id": "su_2", "document_version_id": "v1"}},
        {"id": "pt_orphan", "payload": {"search_unit_id": "su_nonexistent", "document_version_id": "v_deleted"}},
    ]

    def mock_qdrant(req: httpx.Request) -> httpx.Response:
        url = req.url.path
        if url.endswith("/points/scroll"):
            return httpx.Response(200, json={"result": {"points": qdrant_state, "next_page_offset": None}})
        if url.endswith("/points/delete"):
            import json
            body = json.loads(req.content.decode("utf-8"))
            if "points" in body:
                to_delete = set(body["points"])
                qdrant_state[:] = [p for p in qdrant_state if p["id"] not in to_delete]
            elif "filter" in body:
                qdrant_state.clear()
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
        return httpx.Response(200, json={"result": True})

    async with SessionLocal() as session:
        # Trong PostgreSQL chỉ có su_1 và su_2
        doc_id = str(uuid.uuid4())
        doc = Document(id=doc_id, project_id=project_id, filename="d.md", storage_path="/tmp/d.md")
        ver = DocumentVersion(
            id="v1",
            document_id=doc_id,
            version_no=1,
            file_hash="h1",
            valid_from=datetime.now(UTC),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        session.add_all([doc, ver])
        await session.commit()

        cb1 = CanonicalBlock(
            id="b1",
            document_version_id="v1",
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Block 1",
            content_hash="h1",
        )
        cb2 = CanonicalBlock(
            id="b2",
            document_version_id="v1",
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Block 2",
            content_hash="h2",
        )
        session.add_all([cb1, cb2])
        await session.commit()

        su1 = SearchUnit(
            id="su_1",
            document_version_id="v1",
            security_partition_id="part_1",
            block_from_id="b1",
            block_to_id="b1",
            page_from=1,
            page_to=1,
            section_path="Root",
            token_count=1,
            content_hash="h1",
        )
        su2 = SearchUnit(
            id="su_2",
            document_version_id="v1",
            security_partition_id="part_1",
            block_from_id="b2",
            block_to_id="b2",
            page_from=1,
            page_to=1,
            section_path="Root",
            token_count=1,
            content_hash="h2",
        )
        session.add_all([su1, su2])
        await session.commit()

        from sag_api.services.search_index_service import (
            delete_document_points_from_qdrant,
            reconcile_orphan_search_units,
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_qdrant), base_url="http://localhost:6333") as client:
            # 1. Dọn dẹp orphan points
            rec_result = await reconcile_orphan_search_units(session, client, project_id=project_id)
            assert rec_result["orphans_deleted"] == 1
            assert len(qdrant_state) == 2
            assert all(p["id"] != "pt_orphan" for p in qdrant_state)

            # 2. Xóa points của document version v1
            del_count = await delete_document_points_from_qdrant(client, project_id=project_id, document_version_ids=["v1"])
            assert del_count == 1
            assert len(qdrant_state) == 0


def test_sparse_bm25_indices_strictly_unique_and_sorted():
    """Kiểm tra indices của sparse vector luôn duy nhất và sắp xếp tăng dần kể cả khi có collision."""
    from sag_api.services.search_index_service import compute_sparse_bm25_vector
    text = "Hệ thống SAG phân tích dữ liệu phân tán với cơ chế phục hồi thảm họa dữ liệu tự động."
    res = compute_sparse_bm25_vector(text)
    indices = res["indices"]
    values = res["values"]
    assert len(indices) == len(set(indices)), "Sparse indices must be strictly unique for Qdrant compatibility"
    assert indices == sorted(indices), "Sparse indices must be sorted ascending"
    assert len(indices) == len(values)


@pytest.mark.asyncio
async def test_rebuild_service_missing_or_mismatched_project_fails_closed():
    """Kiểm tra Document không có project_id hoặc project_id lệch bị fail-closed."""
    from sag_api.services.rebuild_service import rebuild_search_index_for_version
    await init_db()
    async with SessionLocal() as session:
        # 1. Document không có project_id
        doc_no_proj = Document(
            id=f"doc_no_p_{uuid.uuid4().hex[:8]}",
            source_id=None,
            filename="test1.md",
            storage_path="/tmp/test.md",
            project_id=None,
        )
        ver_no_proj = DocumentVersion(
            id=f"ver_no_p_{uuid.uuid4().hex[:8]}",
            document_id=doc_no_proj.id,
            version_no=1,
            file_hash="hash_no_p",
            search_status="PENDING",
        )
        session.add_all([doc_no_proj, ver_no_proj])
        await session.commit()

        with pytest.raises(ValueError, match="must have confirmed project_id matching"):
            await rebuild_search_index_for_version(
                session,
                document_version_id=ver_no_proj.id,
                project_id="proj_target",
                security_partition_id="part_sec",
                qdrant_client=None,
                embedder=None,
            )

        # 2. Document có project_id lệch với caller
        doc_mismatch = Document(
            id=f"doc_mismatch_{uuid.uuid4().hex[:8]}",
            source_id=None,
            filename="test2.md",
            storage_path="/tmp/test2.md",
            project_id="proj_other",
        )
        ver_mismatch = DocumentVersion(
            id=f"ver_mismatch_{uuid.uuid4().hex[:8]}",
            document_id=doc_mismatch.id,
            version_no=1,
            file_hash="hash_mismatch",
            search_status="PENDING",
        )
        session.add_all([doc_mismatch, ver_mismatch])
        await session.commit()

        with pytest.raises(ValueError, match="must have confirmed project_id matching"):
            await rebuild_search_index_for_version(
                session,
                document_version_id=ver_mismatch.id,
                project_id="proj_target",
                security_partition_id="part_sec",
                qdrant_client=None,
                embedder=None,
            )


@pytest.mark.asyncio
async def test_empty_units_without_qdrant_client_fails_closed():
    """Kiểm tra khi units rỗng và không có Qdrant client, stage phải fail closed (INDEX_FAILED)."""
    await init_db()
    async with SessionLocal() as session:
        doc = Document(
            id=f"doc_empty_{uuid.uuid4().hex[:8]}",
            source_id=None,
            filename="test_empty.md",
            storage_path="/tmp/test_empty.md",
            project_id="proj_empty",
        )
        ver = DocumentVersion(
            id=f"ver_empty_{uuid.uuid4().hex[:8]}",
            document_id=doc.id,
            version_no=1,
            file_hash="hash_empty",
            search_status="PENDING",
        )
        session.add_all([doc, ver])
        await session.commit()

        # Gọi run_search_indexing_stage với qdrant_client=None và không có blocks/units
        with pytest.raises(RuntimeError, match="manifest verification mismatch"):
            await run_search_indexing_stage(
                session,
                project_id="proj_empty",
                document_version=ver,
                security_partition_id="part_sec",
                qdrant_client=None,
                embedder=None,
            )
        await session.refresh(ver)
        assert ver.search_status == "INDEX_FAILED"



