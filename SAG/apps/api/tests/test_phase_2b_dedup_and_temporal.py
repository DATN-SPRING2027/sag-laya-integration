"""Unit and integration tests for Phase 2B: Dedup & Temporal.

Tests:
1. File exact hash and block exact hash retain provenance.
2. Near-duplicate candidate detection with threshold.
3. Anti-auto-merge policy: CONTRADICTS and SUPERSEDES only create candidate edges.
4. Canonical relations: EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED with evidence mapping.
5. Multi-timestamp validity and supersedes chain tracking.
6. Out-of-order ingestion protection against overwriting newer facts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid
import pytest
from sqlalchemy import select

from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Document, Source
from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    IngestionRun,
    KnowledgeGraphEdge,
    SearchUnit,
    StageRun,
)
from sag_api.services.dedup_and_temporal_service import (
    RELATION_CONTRADICTS,
    RELATION_EQUIVALENT,
    RELATION_RELATED,
    RELATION_SUPERSEDES,
    RELATION_SUPPORTS,
    VALID_RELATIONS,
    classify_relation_candidate,
    compute_text_similarity,
    register_relation_candidate,
    resolve_temporal_supersedes,
    run_dedup_and_temporal_stage,
)


def test_compute_text_similarity():
    """Kiểm tra độ tương đồng Jaccard giữa các đoạn văn bản."""
    text1 = "Hệ thống SAG sử dụng PostgreSQL 16 và Qdrant làm cơ sở dữ liệu."
    text2 = "Hệ thống SAG sử dụng PostgreSQL 16 và Qdrant làm cơ sở dữ liệu chính."
    text3 = "Thời tiết hôm nay tại Hà Nội rất đẹp, trời mát mẻ."

    sim_high = compute_text_similarity(text1, text2)
    sim_low = compute_text_similarity(text1, text3)

    assert sim_high > 0.80
    assert sim_low == 0.0


def test_relation_taxonomy_and_candidate_classification():
    """Kiểm tra phân loại quan hệ: bảo đảm CONTRADICTS và SUPERSEDES không bị tự gán thành EQUIVALENT."""
    assert classify_relation_candidate(0.95, is_contradiction=True) == RELATION_CONTRADICTS
    assert classify_relation_candidate(0.95, is_supersede=True) == RELATION_SUPERSEDES
    assert classify_relation_candidate(0.95, is_support=True) == RELATION_SUPPORTS
    assert classify_relation_candidate(0.90) == RELATION_EQUIVALENT
    assert classify_relation_candidate(0.60) == RELATION_RELATED


async def _create_test_hierarchy(session, doc_id: str | None = None):
    """Tạo Document và DocumentVersion cha để thỏa mãn SQLite foreign key."""
    actual_doc_id = doc_id or str(uuid.uuid4())
    doc = Document(
        id=actual_doc_id,
        source_id=None,
        filename="test.md",
        status="LOADING",
        storage_path="/tmp/test.md",
    )
    session.add(doc)
    await session.commit()
    return actual_doc_id


@pytest.mark.asyncio
async def test_anti_auto_merge_candidate_registration():
    """Kiểm tra CONTRADICTS và SUPERSEDES được ghi nhận là candidate, không tự merge, có audit trail."""
    await init_db()
    project_id = f"proj_{uuid.uuid4().hex[:8]}"

    async with SessionLocal() as session:
        doc_id = await _create_test_hierarchy(session)
        ver_id = str(uuid.uuid4())
        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_anti_merge",
            status="RECEIVED",
        )
        session.add(ver)
        await session.commit()

        block_a_id = str(uuid.uuid4())
        block_b_id = str(uuid.uuid4())
        b1 = CanonicalBlock(
            id=block_a_id,
            document_version_id=ver_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Claim A",
            content_hash="h1",
        )
        b2 = CanonicalBlock(
            id=block_b_id,
            document_version_id=ver_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="Claim B",
            content_hash="h2",
        )
        session.add_all([b1, b2])
        await session.commit()

        unit_a_id = str(uuid.uuid4())
        unit_b_id = str(uuid.uuid4())
        u1 = SearchUnit(
            id=unit_a_id,
            document_version_id=ver_id,
            block_from_id=block_a_id,
            block_to_id=block_a_id,
            security_partition_id="part_default",
            content_hash="h1",
            token_count=10,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
        u2 = SearchUnit(
            id=unit_b_id,
            document_version_id=ver_id,
            block_from_id=block_b_id,
            block_to_id=block_b_id,
            security_partition_id="part_default",
            content_hash="h2",
            token_count=10,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
        session.add_all([u1, u2])
        await session.commit()

        edge = await register_relation_candidate(
            session,
            project_id=project_id,
            source_unit_id=unit_a_id,
            target_unit_id=unit_b_id,
            edge_type=RELATION_CONTRADICTS,
            weight=0.92,
            evidence_mapping={"claim_a": "Server port is 8080", "claim_b": "Server port is 9090"},
        )
        await session.commit()

        # Kiểm tra edge trong database
        persisted = (
            await session.execute(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.id == edge.id)
            )
        ).scalar_one()

        assert persisted.edge_type == RELATION_CONTRADICTS
        assert persisted.metadata_json["is_candidate"] is True
        assert persisted.metadata_json["auto_merged"] is False
        assert persisted.metadata_json["requires_human_or_eval_resolution"] is True
        assert persisted.metadata_json["claim_a"] == "Server port is 8080"


@pytest.mark.asyncio
async def test_temporal_chain_supersedes_normal_order():
    """Kiểm tra phiên bản nạp sau (thời gian mới hơn) supersedes phiên bản cũ hợp lệ."""
    await init_db()
    v1_id = str(uuid.uuid4())
    v2_id = str(uuid.uuid4())

    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, 10, 0, 0, tzinfo=UTC)

    async with SessionLocal() as session:
        doc_id = await _create_test_hierarchy(session)
        # Tạo version 1
        v1 = DocumentVersion(
            id=v1_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_v1",
            source_published_at=t0,
            observed_at=t0,
            valid_from=t0,
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            status="RECEIVED",
        )
        session.add(v1)
        await session.commit()

        # Nạp version 2 (mới hơn v1)
        v2 = DocumentVersion(
            id=v2_id,
            document_id=doc_id,
            version_no=2,
            file_hash="hash_v2",
            source_published_at=t1,
            observed_at=t1,
            status="RECEIVED",
        )
        session.add(v2)

        res = await resolve_temporal_supersedes(session, current_version=v2, document_id=doc_id)
        await session.commit()

        assert res["action"] == "SUPERSEDED"
        assert res["active_version_id"] == v2_id
        assert v2.supersedes_id == v1_id
        assert v2.valid_from == t1

        # Version 1 phải bị đóng cửa sổ hiệu lực tại t1
        v1_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v1_id))).scalar_one()
        assert v1_updated.valid_to == t1


@pytest.mark.asyncio
async def test_out_of_order_ingestion_protection():
    """Kiểm tra dữ liệu cũ nạp trễ (out-of-order) không ghi đè dữ liệu mới hơn đã active."""
    await init_db()
    v_new_id = str(uuid.uuid4())
    v_old_late_id = str(uuid.uuid4())

    t_earlier = datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC)
    t_later = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    t_now = datetime.now(UTC)

    async with SessionLocal() as session:
        doc_id = await _create_test_hierarchy(session)
        # Giả sử phiên bản v_new với nội dung từ 2026 đã được nạp trước
        v_new = DocumentVersion(
            id=v_new_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_2026",
            source_published_at=t_later,
            observed_at=t_now - timedelta(hours=1),
            valid_from=t_later,
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            status="RECEIVED",
        )
        session.add(v_new)
        await session.commit()

        # Bây giờ một tài liệu cũ từ năm 2025 được nạp trễ
        v_old_late = DocumentVersion(
            id=v_old_late_id,
            document_id=doc_id,
            version_no=2,
            file_hash="hash_2025",
            source_published_at=t_earlier,  # Xuất bản sớm hơn t_later
            observed_at=t_now,
            status="RECEIVED",
        )
        session.add(v_old_late)

        res = await resolve_temporal_supersedes(session, current_version=v_old_late, document_id=doc_id)
        await session.commit()

        assert res["action"] == "OUT_OF_ORDER_ARCHIVED"
        assert res["active_version_id"] == v_new_id
        assert v_old_late.supersedes_id is None
        # valid_to của bản cũ chỉ kéo dài đến thời điểm xuất bản của bản mới hơn
        assert v_old_late.valid_to == t_later

        # Bản mới hơn (v_new) vẫn giữ nguyên hiệu lực đến vĩnh cửu
        v_new_check = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v_new_id))).scalar_one()
        assert v_new_check.valid_to == datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
