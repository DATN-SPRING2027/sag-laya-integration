"""TDD tests for Milestone P4 Commit 2: Shared NFC normalize_text, tiktoken estimate_tokens, and sentence splitter."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.identity import VerifiedPrincipal as LocalVerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.db.models.routing_rag import CanonicalBlock
from sag_api.enums import DocumentStatus
from sag_api.sag.dto import RetrievedSection
from sag_api.sag.octx_snapshot import _slug
from sag_api.services import (
    dedup_and_temporal_service,
    knowledge_gap_question_service,
    mind_map_service,
    retrieval_service,
    search_unit_retrieval_service,
    tree_query_snapshot_service,
)
from sag_api.services.document_service import get_document_version_chunks
from sag_api.services.session_context import extractor as sc_extractor
from sag_api.services.session_context.models import EntityMention, IndexedTurn
from sag_api.services.session_context.substrate import IncrementalSessionIndex


def test_normalize_text_nfc_and_preserve_code_newlines():
    from sag_api.core.text_utils import normalize_text

    nfd = unicodedata.normalize("NFD", "Tiếng Việt\r\n  dòng   hai\x00\tba\r")
    nfc_expected = unicodedata.normalize("NFC", "Tiếng Việt\ndòng hai ba")
    assert normalize_text(nfd) == nfc_expected

    code_nfd = unicodedata.normalize("NFD", "def hàm():\r\n    x = 'Tiếng Việt'\t# c\r\n    return x\r\n")
    code_norm = normalize_text(code_nfd, preserve_code=True)
    assert "\r" not in code_norm
    assert unicodedata.is_normalized("NFC", code_norm)
    assert "    x = 'Tiếng Việt'\t# c" in code_norm

    unstripped = normalize_text("  hello   world  ", strip=False)
    assert unstripped == " hello world "


def test_octx_slug_maps_vietnamese_d_stroke():
    from sag_api.core.text_utils import slugify_ascii

    assert slugify_ascii("Đường dẫn Hà Nội - Điều 15") == "duong-dan-ha-noi-dieu-15"
    assert _slug("Đề án chuyển đổi số") == "de-an-chuyen-doi-so"


def test_estimate_tokens_tiktoken_disallowed_special_and_offline_fallback():
    from sag_api.core import text_utils, token_usage
    from sag_api.generation.prompt import estimate_tokens as prompt_estimate_tokens
    from sag_api.parsing.canonical import estimate_unicode_tokens

    special_text = "Hello <|endoftext|> Tiếng Việt"
    tok_count = text_utils.estimate_tokens(special_text)
    assert tok_count > 0
    assert prompt_estimate_tokens(special_text) == tok_count
    assert estimate_unicode_tokens(special_text) == tok_count
    assert token_usage.estimate_text_tokens([special_text, ""]) == tok_count
    assert text_utils.estimate_tokens("") == 0

    text_utils._tiktoken_encoding.cache_clear()
    try:
        with patch("tiktoken.get_encoding", side_effect=RuntimeError("offline cold cache")):
            vi = "Quy định chi tiết thi hành một số điều của Luật."
            est = text_utils.estimate_tokens(vi)
            expected = max(1, (len(vi.encode()) + 2) // 3)
            assert est == expected

            cjk_vi = "你好Tiếng"
            est_cjk = text_utils.estimate_tokens(cjk_vi)
            assert est_cjk == 2 + (len("Tiếng".encode()) + 2) // 3
    finally:
        text_utils._tiktoken_encoding.cache_clear()


def test_sentence_and_clause_splitter_preserves_numbers_urls_times_abbreviations_and_newlines():
    from sag_api.core.text_utils import split_clauses_multilingual, split_sentences_multilingual
    from sag_api.parsing.canonical import segment_prose_by_information_cohesion

    sample = (
        "Tại TP. HCM lúc 14:30 ngày 12/10, ông Mr. An và Dr. Bình xem https://example.com/v1.0?a=1.5.\n"
        "Điều 1. Giá trị hợp đồng là 1,000,000 đồng hoặc 1,500 USD theo tỉ lệ 1:2 (phiên bản v1.0).\n"
        "Khoản 2. Áp dụng hệ số 3.14 cho Prof. Minh và Mrs. Hoa."
    )
    sents = split_sentences_multilingual(sample)
    assert len(sents) == 3
    assert "TP. HCM" in sents[0]
    assert "14:30" in sents[0]
    assert "Mr. An" in sents[0]
    assert "Dr. Bình" in sents[0]
    assert "https://example.com/v1.0?a=1.5" in sents[0]
    assert sents[1].startswith("Điều 1. Giá trị")
    assert "1,000,000" in sents[1]
    assert "1,500" in sents[1]
    assert "1:2" in sents[1]
    assert "v1.0" in sents[1]
    assert sents[2].startswith("Khoản 2. Áp dụng")
    assert "3.14" in sents[2]
    assert "Prof. Minh" in sents[2]
    assert "Mrs. Hoa" in sents[2]

    clause_input = "Giá là 1,000,000 đồng, phí là 1,500 USD: lúc 14:30 theo tỉ lệ 1:2 và link https://a.b/c:8080"
    clauses = split_clauses_multilingual(clause_input)
    assert any("1,000,000" in c for c in clauses)
    assert any("1,500" in c for c in clauses)
    assert any("14:30" in c and "1:2" in c and "https://a.b/c:8080" in c for c in clauses)

    # segment_prose_by_information_cohesion preserves newlines
    long_lines = "\n".join(
        f"Dòng {i}: Quy trình kiểm thử v1.{i} tại TP. HCM."
        for i in range(1, 25)
    )
    segs = segment_prose_by_information_cohesion(long_lines, max_block_tokens=120, min_block_tokens=48)
    assert len(segs) >= 2
    assert any("\n" in seg for seg in segs)

    # _best_excerpt preserves v1.0 and TP. HCM
    sec = RetrievedSection(
        chunk_id="c1",
        heading="Release",
        content="See v1.0 at TP. HCM for 3.14 benchmark. Another unrelated sentence here.",
        score=0.9,
    )
    excerpt = retrieval_service._best_excerpt("TP. HCM v1.0 benchmark", sec, limit=200)
    assert "v1.0" in excerpt and "TP. HCM" in excerpt and "3.14" in excerpt

    # session_context extractor split_sentences_with_spans preserves Điều 1. & TP.
    sc_spans = sc_extractor.split_sentences_with_spans(
        "Điều 1. Triển khai tại TP. HCM lúc 14:30. Bước hai là kiểm tra."
    )
    assert len(sc_spans) == 2
    assert "TP. HCM" in sc_spans[0][2]
    assert sc_spans[0][2].startswith("Điều 1.")


def test_nfc_nfd_equivalence_across_services_and_mind_map_cicd():
    nfc = "Tiếng Việt Quản lý Điều 15"
    nfd = unicodedata.normalize("NFD", nfc)

    assert dedup_and_temporal_service.tokenize_for_minhash(nfc) == dedup_and_temporal_service.tokenize_for_minhash(nfd)
    assert dedup_and_temporal_service._tokenize_for_diff(nfc) == dedup_and_temporal_service._tokenize_for_diff(nfd)

    profile = {
        "sparse": [["tiếng", 0.5], ["việt", 0.5], ["điều", 0.4]],
        "entities": [nfc],
    }
    s_nfc = tree_query_snapshot_service._query_signal_scores(nfc, profile)
    s_nfd = tree_query_snapshot_service._query_signal_scores(nfd, profile)
    assert s_nfc == s_nfd
    assert s_nfd["sparse"] > 0.0 and s_nfd["entity"] > 0.0

    # Mind map: NFC/NFD + CI/CD not split on '/'
    assert mind_map_service._topic_terms(nfc) == mind_map_service._topic_terms(nfd)
    assert mind_map_service._score_text_against_topic(
        topic=nfd,
        terms=mind_map_service._topic_terms(nfd),
        filename=f"{nfc}.md",
        section_path=nfc,
        content=nfc,
    ) == mind_map_service._score_text_against_topic(
        topic=nfc,
        terms=mind_map_service._topic_terms(nfc),
        filename=f"{nfc}.md",
        section_path=nfc,
        content=nfc,
    )
    assert mind_map_service._split_section_segments("DevOps > CI/CD Pipeline | Rollout") == [
        "DevOps",
        "CI/CD Pipeline",
        "Rollout",
    ]

    # Knowledge gap anchor check with NFD question vs NFC quote
    q_nfd = unicodedata.normalize("NFD", "Quy trình quản lý điều 15 như thế nào?")
    knowledge_gap_question_service._validate_evidence_anchor(
        q_nfd,
        [{"quote": "Tài liệu mô tả quy trình quản lý điều 15 rất rõ."}],
    )

    # Session substrate _register_turn_entities NFC vs NFD surface count
    sub = IncrementalSessionIndex("sess-1")
    turn = IndexedTurn(
        turn_id="t1",
        thread_id="sess-1",
        ordinal=0,
        role="user",
        content=f"Chúng tôi dùng {nfc} và {nfc}.",
        created_at_ts=1.0,
        created_at_iso="2026-01-01T00:00:00Z",
        token_count=10,
        content_hash="h1",
        episode_id="ep1",
        window_id="w1",
        dense_vector=(1.0,),
        sparse_bm25={},
        content_terms=frozenset(),
        entities=(EntityMention(key="entity:ql", surface=nfd),),
        sentences=(),
    )
    sub._register_turn_entities(turn)
    assert sub.entity_to_turn_counts["entity:ql"]["t1"] == 2

    # search_unit_retrieval_service: Unicode artifact code & API upload filename NFC
    from sag_api.api.v1.documents import _upload_filename

    assert search_unit_retrieval_service._ARTIFACT_CODE_RE.search("Biểu MẪU-102 ban hành") is not None
    nfd_fname = unicodedata.normalize("NFD", "Báo cáo Tiếng Việt.pdf")
    assert unicodedata.is_normalized("NFC", _upload_filename(f"C:\\docs\\{nfd_fname}"))
    assert _upload_filename(f"C:\\docs\\{nfd_fname}") == unicodedata.normalize("NFC", "Báo cáo Tiếng Việt.pdf")


@pytest.mark.asyncio
async def test_document_chunks_fallback_uses_tiktoken_not_whitespace_split():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            session.add(Source(id="s1", name="S", sag_source_config_id="cfg1"))
            await session.flush()
            session.add(
                Document(
                    id="d1",
                    source_id="s1",
                    tenant_id="t1",
                    project_id="p1",
                    filename="vi.md",
                    storage_path="/tmp/vi.md",
                    status=DocumentStatus.READY,
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id="v1",
                    document_id="d1",
                    version_no=1,
                    file_hash="h1",
                    valid_from=now,
                    status="READY",
                    search_status="PENDING",
                    metadata_json={"security_partition_id": "part1"},
                )
            )
            await session.flush()
            vi_text = "Triển khai hệ thống tìm kiếm tiếng Việt chuẩn Unicode NFC."
            session.add(
                CanonicalBlock(
                    id="b1",
                    document_version_id="v1",
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Intro",
                    normalized_text=vi_text,
                    content_hash="ch1",
                )
            )
            await session.commit()

            principal = LocalVerifiedPrincipal(
                user_id="u1",
                tenant_id="t1",
                allowed_projects=frozenset({"p1"}),
                allowed_partitions=frozenset({"part1"}),
            )
            from sag_api.core.text_utils import estimate_tokens

            res = await get_document_version_chunks(
                session,
                project_id="p1",
                document_id="d1",
                version_no=1,
                principal=principal,
            )
            assert res.source_stage == "CANONICAL_BLOCKS"
            assert res.items[0].token_count == estimate_tokens(vi_text)
            assert res.items[0].token_count > len(vi_text.split())
    finally:
        await engine.dispose()
