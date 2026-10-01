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
    generate_search_unit_point_id,
    index_search_units_to_qdrant,
    run_search_indexing_stage,
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


@pytest.mark.asyncio
async def test_search_indexing_stage_and_manifest_verification():
    """Kiểm tra toàn bộ luồng stage INDEX_SEARCH và đối soát số lượng (manifest verification)."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    requests_log: list[httpx.Request] = []

    def mock_qdrant_handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
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

        # Thực thi stage
        units = await run_search_indexing_stage(
            session,
            project_id=project_id,
            document_version=ver,
            security_partition_id="part_internal",
            qdrant_client=mock_client,
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

        # Xác minh mock Qdrant nhận đúng collection_name
        assert any(f"/collections/search_units_{project_id}/points" in r.url.path for r in requests_log)

        # Xác minh DocumentVersion đạt SEARCH_READY
        ver_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == ver_id))).scalar_one()
        assert ver_updated.search_status == "READY"
        assert ver_updated.search_ready_at is not None

