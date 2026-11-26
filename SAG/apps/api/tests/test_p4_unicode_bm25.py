"""TDD tests for Milestone P4 Commit 3: Unified Unicode BM25 tokenizer (S2-07, S3-06)."""

from __future__ import annotations

import hashlib
import unicodedata

from sag_api.sag.search_unit_store import _SPARSE_QUERY_STOPWORDS, build_sparse_query_vector
from sag_api.services.search_index_service import COMMON_STOPWORDS, compute_sparse_bm25_vector
from sag_api.services.session_context.substrate import tokenize_bm25_terms


def _bucket(token: str) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000


def test_bm25_tokenizer_module_and_stopwords_reexport():
    from sag_api.core.bm25_tokenizer import BM25_STEM_WEIGHT
    from sag_api.core.bm25_tokenizer import COMMON_STOPWORDS as CORE_STOPS

    assert COMMON_STOPWORDS is CORE_STOPS
    assert _SPARSE_QUERY_STOPWORDS is CORE_STOPS
    assert BM25_STEM_WEIGHT == 0.55


def test_bm25_index_query_session_symmetry_and_nfc_nfd():
    from sag_api.core.bm25_tokenizer import tokenize_bm25_tokens

    nfc_text = "Điều 15 tại tầng 2 của hệ thống tìm kiếm"
    nfd_text = unicodedata.normalize("NFD", nfc_text)
    assert nfc_text != nfd_text

    res_nfc = tokenize_bm25_tokens(nfc_text)
    res_nfd = tokenize_bm25_tokens(nfd_text)
    assert res_nfc.tokens == res_nfd.tokens
    assert res_nfc.expanded_tokens == res_nfd.expanded_tokens

    assert compute_sparse_bm25_vector(nfc_text) == compute_sparse_bm25_vector(nfd_text)
    assert build_sparse_query_vector(nfc_text) == build_sparse_query_vector(nfd_text)
    assert tokenize_bm25_terms(nfc_text) == tokenize_bm25_terms(nfd_text)

    # Stopwords ("tại", "của") are filtered uniformly across index, query, and session
    session_freqs, _ = tokenize_bm25_terms(nfc_text)
    assert "tại" not in session_freqs
    assert "của" not in session_freqs
    assert "điều" in session_freqs
    assert compute_sparse_bm25_vector("tìm kiếm hệ thống") == compute_sparse_bm25_vector(
        "tìm kiếm của hệ thống là gì"
    )


def test_bm25_glued_unicode_alnum_splitting():
    for glued, expected_parts in [
        ("điều15", ("điều", "15")),
        ("tầng2", ("tầng", "2")),
        ("v2", ("v", "2")),
    ]:
        idx_vec = compute_sparse_bm25_vector(glued)
        qry_vec = build_sparse_query_vector(glued)
        sess_freqs, _ = tokenize_bm25_terms(glued)

        for part in expected_parts:
            assert _bucket(part) in idx_vec["indices"], f"{part} missing in index for {glued}"
            assert _bucket(part) in qry_vec["indices"], f"{part} missing in query for {glued}"
            assert part in sess_freqs, f"{part} missing in session for {glued}"


def test_bm25_drops_thousands_comma_and_rd_rewrite():
    # 1,500 must NOT become 1500 (protects Vietnamese/EU decimal 1,500 = 1.5)
    dec_idx = compute_sparse_bm25_vector("Hệ số là 1,500")
    dec_qry = build_sparse_query_vector("1,500")
    dec_sess, _ = tokenize_bm25_terms("Hệ số là 1,500")
    assert _bucket("1500") not in dec_idx["indices"]
    assert _bucket("1500") not in dec_qry["indices"]
    assert "1500" not in dec_sess
    assert _bucket("500") in dec_idx["indices"]
    assert _bucket("500") in dec_qry["indices"]

    # r&d must NOT rewrite to rd
    rd_idx = compute_sparse_bm25_vector("R&D budget")
    rd_qry = build_sparse_query_vector("R&D")
    rd_sess, _ = tokenize_bm25_terms("R&D budget")
    assert _bucket("rd") not in rd_idx["indices"]
    assert _bucket("rd") not in rd_qry["indices"]
    assert "rd" not in rd_sess


def test_bm25_unified_stem_weight_and_cjk_bigrams():
    idx_vec = compute_sparse_bm25_vector("configurations")
    qry_vec = build_sparse_query_vector("configurations")
    assert idx_vec["indices"] == qry_vec["indices"]

    stem_bucket = _bucket("configuration")
    full_bucket = _bucket("configurations")
    qry_map = dict(zip(qry_vec["indices"], qry_vec["values"], strict=True))
    assert qry_map[stem_bucket] == round(qry_map[full_bucket] * 0.55, 4)

    # CJK bigrams work identically in session too
    sess_cjk, _ = tokenize_bm25_terms("個人情報")
    assert {"個人", "人情", "情報"} <= set(sess_cjk)
