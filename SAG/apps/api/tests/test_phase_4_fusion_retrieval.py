"""Phase 4 Fusion & Retrieval Regression Test Suite (DATN-67).

Verifies:
1. RRF rank fusion scale invariance across divergent dense/sparse score magnitudes.
2. Dual-retriever rank agreement consensus boost.
3. Deduplication with stable fingerprint fallback when chunk_id is absent.
4. Deterministic tie-breaking on identical scores/ranks across multiple runs.
5. Vietnamese domain query relevance and boilerplate/noise filtering.
6. Semantic paraphrase retrieval preserving dense evidence without lexical overlap.
7. Exact alphanumeric identifier priority over generic topical matches.
8. Score normalization strictly bounded in [0.0, 1.0] and contract stats integrity.
9. Canonical SearchUnit integration emitting RRF fusion trace.
"""

from __future__ import annotations

import pytest

from sag_api.sag import RetrievedSection
from sag_api.services.retrieval_service import rerank_sections


def _section(
    chunk_id: str,
    heading: str,
    content: str,
    score: float,
    *,
    source_config_id: str = "source-vn-1",
) -> RetrievedSection:
    return RetrievedSection(
        chunk_id=chunk_id,
        heading=heading,
        content=content,
        score=score,
        source_config_id=source_config_id,
    )


# ==============================================================================
# 1. Scale Invariance Across Divergent Score Magnitudes
# ==============================================================================
def test_fusion_scale_invariance_across_divergent_score_magnitudes():
    """RRF rank fusion must depend strictly on ranked order, not raw score scales."""
    query = "quy trình thanh toán"

    # Baseline scores: Dense in [0.7, 0.95], Lexical in [0.3, 0.8]
    semantic_base = [
        _section("doc_a", "Thanh toán A", "Quy trình thanh toán hợp đồng giai đoạn A", 0.95),
        _section("doc_b", "Thanh toán B", "Quy trình thanh toán tạm ứng giai đoạn B", 0.85),
        _section("doc_c", "Thanh toán C", "Quy trình đối soát thanh toán định kỳ C", 0.70),
    ]
    lexical_base = [
        _section("doc_b", "Thanh toán B", "Quy trình thanh toán tạm ứng giai đoạn B", 0.80),
        _section("doc_a", "Thanh toán A", "Quy trình thanh toán hợp đồng giai đoạn A", 0.60),
        _section("doc_c", "Thanh toán C", "Quy trình đối soát thanh toán định kỳ C", 0.30),
    ]

    baseline_result = rerank_sections(query, semantic_base, lexical=lexical_base, limit=10)

    # Extreme scaling: Dense in [0.0001, 0.0009], Lexical in [150.0, 450.0]
    semantic_extreme = [
        s.model_copy(update={"score": s.score * 0.001}) for s in semantic_base
    ]
    lexical_extreme = [
        s.model_copy(update={"score": s.score * 500.0}) for s in lexical_base
    ]

    scaled_result = rerank_sections(query, semantic_extreme, lexical=lexical_extreme, limit=10)

    # Rankings and selected order must be 100% identical
    assert [s.chunk_id for s in baseline_result.sections] == [s.chunk_id for s in scaled_result.sections]
    assert [s.chunk_id for s in scaled_result.sections] == ["doc_a", "doc_b", "doc_c"]

    # All output scores must remain normalized in [0.0, 1.0]
    for s in scaled_result.sections:
        assert 0.0 <= s.score <= 1.0
        assert s.score == pytest.approx(
            next(b.score for b in baseline_result.sections if b.chunk_id == s.chunk_id),
            abs=1e-5,
        )


# ==============================================================================
# 2. Dual-Retriever Rank Agreement Consensus Boost
# ==============================================================================
def test_fusion_rank_agreement_boosts_consensus_candidate():
    """Candidates appearing at high rank in both channels must rank higher than single-channel."""
    query = "báo cáo tài chính thường niên"

    # doc_consensus is top 1 in both Dense and Sparse
    # doc_dense_only is top 2 in Dense only
    # doc_sparse_only is top 2 in Sparse only
    semantic = [
        _section("doc_consensus", "Báo cáo", "Báo cáo tài chính kiểm toán thường niên", 0.95),
        _section("doc_dense_only", "Thống kê", "Số liệu kế toán tổng hợp năm vừa qua", 0.85),
    ]
    lexical = [
        _section("doc_consensus", "Báo cáo", "Báo cáo tài chính kiểm toán thường niên", 0.90),
        _section("doc_sparse_only", "Báo cáo thường niên", "Bản công bố thông tin thường niên", 0.80),
    ]

    result = rerank_sections(query, semantic, lexical=lexical, limit=10)

    order = [s.chunk_id for s in result.sections]
    assert order[0] == "doc_consensus"

    consensus_section = result.sections[0]
    # Consensus candidate receives full dual RRF votes -> score close to 1.0
    assert consensus_section.score == pytest.approx(1.0, rel=1e-2)

    # Single-channel candidates have lower scores (~0.5)
    for section in result.sections[1:]:
        assert section.score < 0.7


# ==============================================================================
# 3. Deduplication with Stable Fingerprint Fallback
# ==============================================================================
def test_deduplication_uses_stable_fingerprint_when_chunk_id_missing():
    """When chunk_id is empty, deduplication must fall back to a deterministic content fingerprint."""
    query = "hướng dẫn bảo hiểm y tế"

    # Shared long text prefix >= 240 chars ensures identical fingerprint
    shared_prefix = (
        "Quy trình khám chữa bệnh bảo hiểm y tế đúng tuyến theo quy định hiện hành "
        "của bảo hiểm xã hội Việt Nam đối với các cơ sở y tế đủ điều kiện tiếp nhận bệnh nhân. "
    ) * 3

    duplicate_short = _section(
        "",
        "Bảo hiểm y tế",
        shared_prefix,
        0.75,
        source_config_id="src-bhxh",
    )
    duplicate_long = _section(
        "",
        "Bảo hiểm y tế",
        shared_prefix + " Người tham gia cần xuất trình thẻ căn cước công dân gắn chip khi đến khám.",
        0.90,
        source_config_id="src-bhxh",
    )
    other_section = _section(
        "unique_doc",
        "Bảo hiểm tai nạn",
        "Chế độ trợ cấp tai nạn lao động và bệnh nghề nghiệp tại doanh nghiệp.",
        0.80,
        source_config_id="src-bhxh",
    )

    result = rerank_sections(
        query,
        [duplicate_short, duplicate_long, other_section],
        limit=10,
    )

    # The duplicates must be merged into 1 entry
    assert len(result.sections) == 2
    merged_bhyt = next(s for s in result.sections if s.heading == "Bảo hiểm y tế")

    # Representative should prefer the longer, more informative content
    assert "Người tham gia cần xuất trình thẻ căn cước công dân" in merged_bhyt.content
    # Score should retain the maximum score among duplicates
    assert merged_bhyt.score > 0.0


# ==============================================================================
# 4. Deterministic Tie-Breaking Across Symmetrical Candidates
# ==============================================================================
def test_deterministic_ordering_on_identical_scores_and_metrics():
    """Candidates with equal fused score and metrics must tie-break deterministically by unique key."""
    query = "tra cứu tài liệu nội bộ"

    # Candidate A: rank 0 in dense, rank 1 in sparse
    # Candidate B: rank 1 in dense, rank 0 in sparse
    # Both receive identical RRF reciprocal rank sum: 1/(60+0+1) + 1/(60+1+1)
    # Both have coverage=2, exact=1, rank_sum=1
    candidate_a = _section("doc_a", "Mục A", "Nội dung quy chế làm việc mục A", 0.85)
    candidate_b = _section("doc_b", "Mục B", "Nội dung quy chế làm việc mục B", 0.85)

    # Order 1: dense=[A, B], sparse=[B, A]
    res1 = rerank_sections(
        query,
        [candidate_a, candidate_b],
        lexical=[candidate_b, candidate_a],
        limit=10,
    )

    # Order 2: dense=[B, A], sparse=[A, B]
    res2 = rerank_sections(
        query,
        [candidate_b, candidate_a],
        lexical=[candidate_a, candidate_b],
        limit=10,
    )

    # Both cases have identical metrics; tie-break by key ('source-vn-1', 'doc_a') < ('source-vn-1', 'doc_b')
    # so doc_a must consistently be ranked before doc_b in both runs.
    assert [s.chunk_id for s in res1.sections] == ["doc_a", "doc_b"]
    assert [s.chunk_id for s in res2.sections] == ["doc_a", "doc_b"]
    assert res1.sections[0].score == res2.sections[0].score


# ==============================================================================
# 5. Vietnamese Domain Query Relevance & Noise Filtering
# ==============================================================================
def test_vietnamese_domain_query_relevance_and_noise_filtering():
    """Vietnamese banking domain query filters irrelevant noise via relative semantic floor."""
    query = "doanh nghiệp vay vốn tài chính thương mại"

    relevant_approval = _section(
        "appr_1",
        "Hồ sơ cấp tín dụng doanh nghiệp",
        "Doanh nghiệp nộp báo cáo tài chính để thẩm định vay vốn thương mại.",
        0.92,
    )
    relevant_collateral = _section(
        "appr_2",
        "Biện pháp bảo đảm tài sản",
        "Định giá tài sản thế chấp cho các khoản vay của doanh nghiệp.",
        0.82,
    )
    unrelated_sports = _section(
        "noise_sports",
        "Thể thao thế giới",
        "Kết quả thi đấu bóng đá cúp quốc gia cuối tuần.",
        0.20,
    )
    unrelated_weather = _section(
        "noise_weather",
        "Thời tiết biển",
        "Dự báo thời tiết vùng biển có gió cấp năm sóng cao hai mét.",
        0.15,
    )

    result = rerank_sections(
        query,
        [relevant_approval, relevant_collateral, unrelated_sports, unrelated_weather],
        limit=10,
    )

    selected_ids = [s.chunk_id for s in result.sections]
    assert "appr_1" in selected_ids
    assert "appr_2" in selected_ids
    assert "noise_sports" not in selected_ids
    assert "noise_weather" not in selected_ids
    assert result.filtered_count == 2


# ==============================================================================
# 6. Semantic Paraphrase Preserves Dense Evidence Without Lexical Overlap
# ==============================================================================
def test_semantic_paraphrase_preserves_dense_evidence_without_lexical_overlap():
    """Dense retriever correctly identifies relevant Vietnamese text even with zero lexical overlap."""
    # Vernacular query
    query = "chính sách hỗ trợ người lao động khi sinh nở"

    # Formal legal text with high dense score
    dense_evidence = _section(
        "maternity_law",
        "Chế độ thai sản",
        "Lao động nữ sinh con được hưởng bảo hiểm xã hội 6 tháng lương.",
        0.95,
    )
    # Generic document with low dense score below the 0.75 relative floor
    generic_doc = _section(
        "weather_doc",
        "Thời tiết biển",
        "Dự báo thời tiết vùng biển có gió cấp năm sóng cao hai mét.",
        0.20,
    )

    result = rerank_sections(
        query,
        [dense_evidence, generic_doc],
        limit=10,
    )

    assert [s.chunk_id for s in result.sections] == ["maternity_law"]
    assert result.sections[0].chunk_id == "maternity_law"


# ==============================================================================
# 7. Exact Alphanumeric Identifier Query Prioritizes Literal Code Match
# ==============================================================================
def test_exact_identifier_query_prioritizes_literal_code_over_generic_match():
    """Exact administrative decision code (e.g. QD-2026/TT-BTC) must be ranked above generic text."""
    query = "QD-2026/TT-BTC"

    exact_doc = _section(
        "exact_code",
        "Thông tư hướng dẫn",
        "Căn cứ quyết định QD-2026/TT-BTC của Bộ Tài Chính ban hành quy chuẩn kế toán mới.",
        0.80,
    )
    generic_doc = _section(
        "generic_tax",
        "Chính sách tài chính 2026",
        "Bộ Tài Chính thông báo các quy định tài chính và thuế quan mới trong năm 2026.",
        0.95,
    )

    # Lexical retriever matches the exact token for exact_doc
    lexical_hits = [exact_doc]

    result = rerank_sections(
        query,
        [generic_doc, exact_doc],
        lexical=lexical_hits,
        exact_lexical_keys={("source-vn-1", "exact_code")},
        limit=10,
    )

    # exact_doc must be ranked #1 due to exact key match priority
    assert result.sections[0].chunk_id == "exact_code"


# ==============================================================================
# 8. Score Normalization and Retrieval Stats Contract
# ==============================================================================
def test_score_normalization_and_retrieval_stats_contract():
    """RerankResult must adhere to contract: score in [0.0, 1.0], stats counts valid."""
    query = "hợp đồng dịch vụ tư vấn"

    candidates = [
        _section("doc_1", "Hợp đồng 1", "Điều khoản thanh toán hợp đồng dịch vụ tư vấn.", 0.90),
        _section("doc_2", "Hợp đồng 2", "Điều khoản chấm dứt hợp đồng dịch vụ tư vấn.", 0.80),
        _section("noise", "Thời tiết", "Thời tiết ngày mai có nắng nhẹ không mưa.", 0.10),
    ]

    result = rerank_sections(query, candidates, limit=5)

    assert result.candidate_count == 3
    assert result.relevant_count == 2
    assert result.filtered_count == 1
    assert result.candidate_count == result.relevant_count + result.filtered_count

    for sec in result.sections:
        assert isinstance(sec.score, float)
        assert 0.0 <= sec.score <= 1.0
        assert isinstance(sec.rank, int)
        assert sec.rank >= 0


# ==============================================================================
# 9. Canonical SearchUnit Integration Emits RRF Fusion Trace
# ==============================================================================
@pytest.mark.asyncio
async def test_canonical_search_unit_emits_rrf_fusion_trace():
    """Canonical search unit retrieval path must record fusion_method='rrf' and candidate stats."""
    import hashlib
    import uuid
    from datetime import UTC, datetime, timedelta

    import httpx

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.principal_assertion import VerifiedPrincipal
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
    from sag_api.services import search_unit_retrieval_service
    from sag_api.services.search_index_service import generate_search_unit_point_id

    await init_db()
    suffix = uuid.uuid4().hex
    project_id = f"project-{suffix}"
    source_id = f"source-{suffix}"
    source_config_id = f"config-{suffix}"
    tenant_id = f"tenant-{suffix}"
    partition_id = f"partition-{suffix}"
    document_id = f"doc-{suffix}"
    version_id = f"ver-{suffix}"
    block_id = f"block-{suffix}"
    unit_id = f"unit-{suffix}"
    run_id = f"run-{suffix}"
    content = "Quy trình đối soát và giải ngân hợp đồng tín dụng."
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = datetime.now(UTC)
    manifest_checksum = hashlib.sha256(f"manifest-{suffix}".encode()).hexdigest()

    async with SessionLocal() as session:
        source = Source(
            id=source_id,
            name="Tài liệu tín dụng",
            sag_source_config_id=source_config_id,
            config={},
        )
        session.add(source)
        await session.flush()
        session.add(
            SourceProjectMapping(
                source_id=source_id,
                organization_id=f"org-{suffix}",
                project_id=project_id,
                state="CONFIRMED",
                confirmed_at=now,
                confirmed_by="test-owner",
                approval_ref="test-approval",
            )
        )
        await session.flush()
        session.add(
            Document(
                id=document_id,
                source_id=source_id,
                tenant_id=tenant_id,
                project_id=project_id,
                filename="quy_trinh_tin_dung.md",
                content_type="text/markdown",
                size_bytes=len(content),
                storage_path=f"/tmp/{document_id}",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=1,
                file_hash=hashlib.sha256(b"upload").hexdigest(),
                status="RECEIVED",
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={"security_partition_id": partition_id},
            )
        )
        await session.flush()
        session.add(
            IngestionRun(
                id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                document_version_id=version_id,
                idempotency_key=suffix,
                payload_hash=hashlib.sha256(b"payload").hexdigest(),
                status="SUCCEEDED",
                started_at=now - timedelta(seconds=2),
                completed_at=now,
                created_at=now - timedelta(seconds=2),
            )
        )
        await session.flush()
        session.add(
            CanonicalBlock(
                id=block_id,
                document_version_id=version_id,
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Quy trình > Giải ngân",
                source_anchor="anchor-giai-ngan",
                normalized_text=content,
                content_hash=content_hash,
            )
        )
        session.add(
            SearchUnit(
                id=unit_id,
                document_version_id=version_id,
                block_from_id=block_id,
                block_to_id=block_id,
                security_partition_id=partition_id,
                content_hash=content_hash,
                token_count=10,
                page_from=1,
                page_to=1,
                section_path="Quy trình > Giải ngân",
            )
        )
        session.add(
            StageRun(
                id=f"stage-{suffix}",
                run_id=run_id,
                stage="INDEX_SEARCH",
                status="SUCCESS",
                duration_ms=1,
                created_at=now,
                metrics_json={
                    "manifest_verified": True,
                    "collection_name": f"search_units_{project_id}",
                    "search_unit_count": 1,
                    "pg_count": 1,
                    "qdrant_count": 1,
                    "qdrant_indexed_count": 1,
                    "manifest_checksum": manifest_checksum,
                    "qdrant_checksum": manifest_checksum,
                },
            )
        )
        await session.commit()
        source = await session.get(Source, source_id)

    principal = VerifiedPrincipal(
        subject="user-p4-test",
        organization_id=f"org-{suffix}",
        allowed_project_ids=frozenset({project_id}),
        issuer="https://issuer.invalid",
        key_id="test-key",
        token_id=f"token-{suffix}",
        issued_at=1,
        expires_at=2,
        tenant_id=tenant_id,
        allowed_partition_ids=frozenset({partition_id}),
    )

    point_id = generate_search_unit_point_id(f"search_units_{project_id}", unit_id)

    async def qdrant_reply(request: httpx.Request) -> httpx.Response:
        point_payload = {
            "search_unit_id": unit_id,
            "document_version_id": version_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "security_partition_id": partition_id,
            "content_hash": content_hash,
            "content": content,
        }
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"result": {"id": point_id, "score": 0.0, "payload": point_payload}},
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": point_id,
                            "score": 0.85,
                            "payload": point_payload,
                        }
                    ]
                }
            },
        )

    qdrant_client = httpx.AsyncClient(
        transport=httpx.MockTransport(qdrant_reply),
        base_url="http://qdrant-test",
    )

    class Embedder:
        async def generate(self, _query: str):
            return [0.1, 0.9]

    class SearchEngines:
        async def get_sag_embedding(self, *_args):
            return Embedder()

        async def get_search_unit_qdrant_client(self):
            return qdrant_client

        async def get_routing_snapshot(self, **_kwargs):
            return None

    search_engines = SearchEngines()

    outcome = await search_unit_retrieval_service.retrieve_search_unit_sections(
        search_engines,
        [source],
        "quy trình giải ngân",
        principal=principal,
        top_k=5,
    )

    assert outcome.stats["canonical_index"] is True
    assert outcome.stats["fusion_method"] == "rrf"
    assert outcome.stats["semantic_candidates"] == 1
    assert outcome.stats["lexical_candidates"] == 1
    assert outcome.stats["candidates"] == 1
    assert outcome.stats["relevant"] == 1
    assert outcome.stats["filtered_irrelevant"] == 0
    assert len(outcome.sections) == 1
    assert outcome.sections[0].chunk_id == unit_id
    assert 0.0 <= outcome.sections[0].score <= 1.0
