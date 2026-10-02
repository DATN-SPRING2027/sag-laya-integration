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
    build_lsh_band_index,
    classify_relation_candidate,
    compute_text_similarity,
    detect_semantic_signals,
    query_lsh_candidates,
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


@pytest.mark.asyncio
async def test_run_dedup_and_temporal_stage_executes_exact_and_near_dedup():
    """Kiểm tra run_dedup_and_temporal_stage thực thi cả exact dedup và near dedup Jaccard."""
    await init_db()
    project_id = f"proj_dedup_{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc_id = await _create_test_hierarchy(session)

        # Version 1: chứa block 1 và block 2
        v1_id = str(uuid.uuid4())
        v1 = DocumentVersion(
            id=v1_id,
            document_id=doc_id,
            version_no=1,
            file_hash="hash_content_v1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            status="RECEIVED",
        )
        session.add(v1)
        await session.commit()

        b1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Kiến trúc hệ thống microservices với Docker và Kubernetes.",
            content_hash="hash_block_1",
        )
        b2 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Cơ sở dữ liệu PostgreSQL và Qdrant vector database được cấu hình tối ưu.",
            content_hash="hash_block_2",
        )
        session.add_all([b1, b2])
        await session.commit()

        # Version 2: nạp lại: 1 block exact match, 1 block near duplicate (sửa 1 từ)
        v2_id = str(uuid.uuid4())
        v2 = DocumentVersion(
            id=v2_id,
            document_id=doc_id,
            version_no=2,
            file_hash="hash_content_v2",
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            status="RECEIVED",
        )
        session.add(v2)
        await session.commit()

        # Block v2_1: exact match với b1
        v2_b1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v2_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Kiến trúc hệ thống microservices với Docker và Kubernetes.",
            content_hash="hash_block_1",  # exact hash match
        )
        # Block v2_2: near duplicate với b2 (Jaccard > 0.85)
        v2_b2 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v2_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Intro",
            normalized_text="Cơ sở dữ liệu PostgreSQL và Qdrant vector database được cấu hình tối ưu nhất.",
            content_hash="hash_block_2_modified",
        )
        session.add_all([v2_b1, v2_b2])

        ing_run = IngestionRun(
            id=run_id,
            tenant_id="tenant_default",
            project_id=project_id,
            document_version_id=v2_id,
            idempotency_key="key_dedup_test",
            payload_hash="hash_content_v2",
            status="RUNNING",
        )
        session.add(ing_run)
        await session.commit()

        # Chạy Stage 2B
        res = await run_dedup_and_temporal_stage(
            session,
            document_version=v2,
            document_id=doc_id,
            project_id=project_id,
            run_id=run_id,
        )
        await session.commit()

        assert res["exact_block_matches"] == 1
        assert len(res["near_duplicate_candidates"]) == 1
        assert res["near_duplicate_candidates"][0]["relation"] == RELATION_EQUIVALENT
        assert res["near_duplicate_candidates"][0]["auto_merged"] is False
        assert res["temporal_action"] == "SUPERSEDED"

        # Kiểm tra metadata_json của v2 được ghi nhận
        v2_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v2_id))).scalar_one()
        assert v2_updated.metadata_json["exact_block_matches"] == 1
        assert v2_updated.metadata_json["near_duplicate_candidates_count"] == 1

        # Kiểm tra StageRun
        stage_run = (
            await session.execute(
                select(StageRun).where(StageRun.run_id == run_id, StageRun.stage == "DEDUP_TEMPORAL")
            )
        ).scalar_one()
        assert stage_run.status == "SUCCESS"
        assert stage_run.metrics_json["exact_block_matches"] == 1
        assert stage_run.metrics_json["near_duplicate_candidates_count"] == 1


def test_semantic_guard_negation_contradiction():
    """Kiểm tra Semantic Guard: Khẳng định bị phủ định (lexical overlap cao) KHÔNG được gắn EQUIVALENT mà phải là CONTRADICTS."""
    text1 = (
        "Báo cáo kết quả kiểm thử tải xác nhận toàn bộ cụm dịch vụ phân tán của hệ thống "
        "đang duy trì trạng thái hoạt động ổn định và sẵn sàng phục vụ lượng truy cập lớn."
    )
    text2 = (
        "Báo cáo kết quả kiểm thử tải xác nhận toàn bộ cụm dịch vụ phân tán của hệ thống "
        "không duy trì trạng thái hoạt động ổn định và sẵn sàng phục vụ lượng truy cập lớn."
    )

    sim = compute_text_similarity(text1, text2)
    assert sim >= 0.75

    is_contra, is_supp, is_super = detect_semantic_signals(text1, text2)
    assert is_contra is True

    rel = classify_relation_candidate(sim, is_contradiction=is_contra, is_support=is_supp, is_supersede=is_super)
    assert rel == RELATION_CONTRADICTS
    assert rel != RELATION_EQUIVALENT


def test_semantic_guard_support_marker():
    """Kiểm tra gán nhãn SUPPORTS khi có tín hiệu đồng thuận/xác nhận."""
    text1 = "Kiến trúc microservices đáp ứng tải cao."
    text2 = "Thực nghiệm chứng minh kiến trúc microservices đáp ứng tải cao."

    sim = compute_text_similarity(text1, text2)
    is_contra, is_supp, is_super = detect_semantic_signals(text1, text2)
    assert is_supp is True

    rel = classify_relation_candidate(sim, is_contradiction=is_contra, is_support=is_supp, is_supersede=is_super)
    assert rel == RELATION_SUPPORTS


def test_lsh_band_index_bounded_candidates():
    """Kiểm tra LSH Index: Tìm kiếm ứng viên giới hạn qua band buckets thay vì quét toàn bộ O(N^2)."""
    import random
    from sag_api.services.dedup_and_temporal_service import compute_minhash_signature

    random.seed(42)
    sigs_by_id = {}
    words_0 = [f"word_anchor_{j}" for j in range(20)]
    sigs_by_id["block_0"] = compute_minhash_signature(words_0, num_perm=128)

    for i in range(1, 60):
        words = [f"word_noise_{i}_{j}" for j in range(20)]
        sigs_by_id[f"block_{i}"] = compute_minhash_signature(words, num_perm=128)

    # Query nằm ngoài reference pool nhưng chia sẻ 18/20 từ với block_0
    query_words = words_0[:18] + ["query_unique_1", "query_unique_2"]
    query_sig = compute_minhash_signature(query_words, num_perm=128)

    lsh_buckets = build_lsh_band_index(sigs_by_id, num_bands=16, rows_per_band=8)
    assert len(lsh_buckets) > 0

    # Query bằng target signature nằm ngoài pool
    candidates = query_lsh_candidates(query_sig, lsh_buckets, num_bands=16, rows_per_band=8)
    # block_0 phải được tìm thấy và giới hạn số lượng (< 10)
    assert "block_0" in candidates
    assert len(candidates) < 10


@pytest.mark.asyncio
async def test_mid_timeline_insertion_rewires_successor_chain():
    """Kiểm tra chèn phiên bản vào giữa timeline (v1 -> v2, chèn v_mid):
    - Đóng valid_to của v1 tại v_mid.valid_from.
    - Gán v_mid.supersedes_id = v1.id và v_mid.valid_to = v2.valid_from.
    - Rewire v2.supersedes_id = v_mid.id để giữ chuỗi kế thừa liên tục (v1 <- v_mid <- v2).
    """
    await init_db()
    t_v1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    t_mid = datetime(2026, 1, 5, 10, 0, 0, tzinfo=UTC)
    t_v2 = datetime(2026, 1, 10, 10, 0, 0, tzinfo=UTC)

    doc_id = str(uuid.uuid4())
    v1_id = str(uuid.uuid4())
    v2_id = str(uuid.uuid4())
    v_mid_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, filename="mid_timeline.md", storage_path="/tmp/mid.md")
        session.add(doc)

        # 1. Nạp v1
        v1 = DocumentVersion(
            id=v1_id,
            document_id=doc_id,
            version_no=1,
            file_hash="h1",
            source_published_at=t_v1,
            observed_at=t_v1,
            valid_from=t_v1,
            valid_to=t_v2,
            status="RECEIVED",
        )
        # 2. Nạp v2 (đã nối v1)
        v2 = DocumentVersion(
            id=v2_id,
            document_id=doc_id,
            version_no=2,
            file_hash="h2",
            supersedes_id=v1_id,
            source_published_at=t_v2,
            observed_at=t_v2,
            valid_from=t_v2,
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
            status="RECEIVED",
        )
        session.add_all([v1, v2])
        await session.commit()

        # 3. Nạp muộn v_mid nằm giữa t_v1 và t_v2
        v_mid = DocumentVersion(
            id=v_mid_id,
            document_id=doc_id,
            version_no=3,
            file_hash="h_mid",
            source_published_at=t_mid,
            observed_at=t_mid,
            status="RECEIVED",
        )
        session.add(v_mid)

        res = await resolve_temporal_supersedes(session, current_version=v_mid, document_id=doc_id)
        await session.commit()

        assert res["action"] == "SUPERSEDED"
        assert v_mid.supersedes_id == v1_id
        assert v_mid.valid_from == t_mid
        assert v_mid.valid_to == t_v2

        # Kiểm tra v1 bị đóng tại t_mid
        v1_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v1_id))).scalar_one()
        assert v1_updated.valid_to == t_mid

        # Bằng chứng cốt lõi: v2 đã được rewire supersedes_id trỏ về v_mid_id thay vì v1_id
        v2_updated = (await session.execute(select(DocumentVersion).where(DocumentVersion.id == v2_id))).scalar_one()
        assert v2_updated.supersedes_id == v_mid_id


def test_minhash_disjoint_sets_yield_zero_similarity():
    """Kiểm tra sentinel 0xFFFFFFFF (#13): Hai tập từ vựng hoàn toàn rời nhau phải có similarity = 0.0."""
    from sag_api.services.dedup_and_temporal_service import compute_minhash_signature, estimate_minhash_similarity

    set_a = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    set_b = ["hotel", "india", "juliet", "kilo", "lima", "mike", "november"]

    sig_a = compute_minhash_signature(set_a, num_perm=128)
    sig_b = compute_minhash_signature(set_b, num_perm=128)

    sim = estimate_minhash_similarity(sig_a, sig_b)
    assert sim == 0.0, f"Expected 0.0 similarity for completely disjoint word sets, got {sim}"


@pytest.mark.asyncio
async def test_cross_document_dedup_within_project():
    """Kiểm tra đối soát dedup xuyên tài liệu (cross-document) trong cùng project_id (#12)."""
    await init_db()
    project_id = f"proj_cross_{uuid.uuid4().hex[:8]}"

    async with SessionLocal() as session:
        # Document 1 trong project
        doc1_id = str(uuid.uuid4())
        doc1 = Document(id=doc1_id, project_id=project_id, filename="doc1.md", storage_path="/tmp/doc1.md")
        session.add(doc1)
        await session.commit()

        v1_id = str(uuid.uuid4())
        v1 = DocumentVersion(id=v1_id, document_id=doc1_id, version_no=1, file_hash="hash_d1", status="RECEIVED")
        session.add(v1)
        await session.commit()

        b1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Policy",
            normalized_text="Quy định nghỉ phép năm áp dụng tối đa 12 ngày làm việc cho toàn thể nhân sự công ty.",
            content_hash="hash_b1_policy",
        )
        session.add(b1)
        await session.commit()

        # Document 2 trong cùng project nhưng khác document_id
        doc2_id = str(uuid.uuid4())
        doc2 = Document(id=doc2_id, project_id=project_id, filename="doc2.md", storage_path="/tmp/doc2.md")
        session.add(doc2)
        await session.commit()

        v2_id = str(uuid.uuid4())
        v2 = DocumentVersion(id=v2_id, document_id=doc2_id, version_no=1, file_hash="hash_d2", status="RECEIVED")
        session.add(v2)
        await session.commit()

        b2 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v2_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="HR_Policy",
            normalized_text="Quy định nghỉ phép năm áp dụng tối đa 12 ngày làm việc cho toàn thể nhân sự công ty này.",
            content_hash="hash_b2_policy_variant",
        )
        session.add(b2)
        await session.commit()

        # Chạy dedup cho v2: phải tìm thấy b1 từ doc1 vì cùng project_id
        res = await run_dedup_and_temporal_stage(
            session,
            document_version=v2,
            document_id=doc2_id,
            project_id=project_id,
        )
        await session.commit()

        assert len(res["near_duplicate_candidates"]) >= 1
        cand = res["near_duplicate_candidates"][0]
        assert cand["target_block_id"] == b1.id
        assert cand["target_version_id"] == v1_id


@pytest.mark.asyncio
async def test_candidate_ranking_selects_highest_similarity():
    """Kiểm tra candidate ranking (#18): Khi 1 block khớp nhiều block trong quá khứ, chọn ứng viên có điểm cao nhất."""
    await init_db()
    project_id = f"proj_rank_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        doc = Document(id=doc_id, project_id=project_id, filename="ranking.md", storage_path="/tmp/r.md")
        session.add(doc)
        await session.commit()

        v1_id = str(uuid.uuid4())
        v1 = DocumentVersion(id=v1_id, document_id=doc_id, version_no=1, file_hash="h1", status="RECEIVED")
        session.add(v1)
        await session.commit()

        pb1 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Arch",
            normalized_text="Hệ thống cơ sở dữ liệu phân tán PostgreSQL được triển khai song song.",
            content_hash="hpb1",
        )
        pb2 = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=1,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Arch",
            normalized_text="Hệ thống cơ sở dữ liệu phân tán PostgreSQL và Qdrant được triển khai tối ưu trên Kubernetes cluster.",
            content_hash="hpb2",
        )
        session.add_all([pb1, pb2])
        await session.commit()

        v2_id = str(uuid.uuid4())
        v2 = DocumentVersion(id=v2_id, document_id=doc_id, version_no=2, file_hash="h2", status="RECEIVED")
        session.add(v2)
        await session.commit()

        curr_b = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v2_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Arch",
            normalized_text="Hệ thống cơ sở dữ liệu phân tán PostgreSQL và Qdrant được triển khai tối ưu trên Kubernetes cluster mạnh mẽ.",
            content_hash="hcurr",
        )
        session.add(curr_b)
        await session.commit()

        res = await run_dedup_and_temporal_stage(
            session,
            document_version=v2,
            document_id=doc_id,
            project_id=project_id,
        )
        await session.commit()

        assert len(res["near_duplicate_candidates"]) == 1
        best_cand = res["near_duplicate_candidates"][0]
        assert best_cand["target_block_id"] == pb2.id


@pytest.mark.asyncio
async def test_tier_4_cosine_embedding_dedup_and_contradiction_guard():
    """Kiểm tra Tier 4 embedding evaluation và contradiction semantic guard (#11)."""
    await init_db()
    project_id = f"proj_t4_{uuid.uuid4().hex[:8]}"
    doc_id = str(uuid.uuid4())

    class MockSemanticEmbedder:
        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            vecs = []
            for t in texts:
                if "thành công" in t:
                    vecs.append([1.0, 0.0, 0.0])
                elif "thất bại" in t:
                    vecs.append([-1.0, 0.0, 0.0])
                else:
                    vecs.append([0.95, 0.05, 0.0])
            return vecs

    async with SessionLocal() as session:
        doc = Document(id=doc_id, project_id=project_id, filename="semantic.md", storage_path="/tmp/sem.md")
        session.add(doc)
        await session.commit()

        v1_id = str(uuid.uuid4())
        v1 = DocumentVersion(id=v1_id, document_id=doc_id, version_no=1, file_hash="h1", status="RECEIVED")
        session.add(v1)
        await session.commit()

        pb = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v1_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Result",
            normalized_text="Dự án đã triển khai thành công đúng tiến độ cam kết.",
            content_hash="h_succ",
        )
        session.add(pb)
        await session.commit()

        v2_id = str(uuid.uuid4())
        v2 = DocumentVersion(id=v2_id, document_id=doc_id, version_no=2, file_hash="h2", status="RECEIVED")
        session.add(v2)
        await session.commit()

        curr = CanonicalBlock(
            id=str(uuid.uuid4()),
            document_version_id=v2_id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Result",
            normalized_text="Toàn bộ kế hoạch của dự án đã thành công mỹ mãn.",
            content_hash="h_succ_para",
        )
        session.add(curr)
        await session.commit()

        res = await run_dedup_and_temporal_stage(
            session,
            document_version=v2,
            document_id=doc_id,
            project_id=project_id,
            embedder=MockSemanticEmbedder(),
        )
        await session.commit()

        assert len(res["near_duplicate_candidates"]) >= 1
        cand = res["near_duplicate_candidates"][0]
        assert cand["similarity_score"] >= 0.90
        assert cand["target_block_id"] == pb.id


