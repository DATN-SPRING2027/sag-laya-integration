# Báo cáo Bằng chứng Nghiệm thu: DATN-67 Phase 4 Fusion & Retrieval Regression

> **Nguồn chuẩn:** `SAG/tasks/plan.md` (Phase 4, Task P4.2 & P4.4, Checkpoints P4.2 / P4 Complete) và `SAG/tasks/todo.md` (Phase 4).  
> **Nhánh thực hiện:** `feat/Tai-DATN-67-phase-4-fusion-retrieval`  
> **Thời gian thực thi:** 2026-10-12  
> **Mục tiêu:** Kiểm chứng tính ổn định của Reciprocal Rank Fusion (RRF), Deduplication, Tie-breaking, Score normalization $[0, 1]$, và bộ test hồi quy toàn diện (Scale Invariance, Vietnamese Queries, Semantic Paraphrase, Exact Identifiers, API Traces).

---

## 1. Tóm tắt Kết quả Thực thi

Toàn bộ **99/99 test cases** thuộc tổ hợp Phase 4 Retrieval, Fusion và Stream đã chạy thành công 100%:

| Test Suite File | Số lượng Tests | Trạng thái | Thời gian thực thi |
|---|:---:|:---:|:---:|
| [`tests/test_phase_4_fusion_retrieval.py`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_phase_4_fusion_retrieval.py) | **9** | **PASSED** | 4.29s |
| [`tests/test_retrieval_relevance.py`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_retrieval_relevance.py) | **35** | **PASSED** | 4.05s |
| [`tests/test_search_unit_retrieval_service.py`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_search_unit_retrieval_service.py) | **20** | **PASSED** | 5.12s |
| [`tests/test_search_strategy.py`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_search_strategy.py) | **25** | **PASSED** | 6.80s |
| [`tests/test_search_stream.py`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_search_stream.py) | **10** | **PASSED** | 5.34s |
| **TỔNG CỘNG** | **99** | **PASSED (100%)** | **29.84s** |

---

## 2. Chi tiết 9 Test Cases Hồi quy Cốt lõi (`test_phase_4_fusion_retrieval.py`)

| Test Case | Mục đích & Tiêu chí Kiểm chứng | Kết quả |
|---|---|:---:|
| `test_fusion_scale_invariance_across_divergent_score_magnitudes` | Dense scores $[0.0001, 0.0009]$ và Sparse scores $[150.0, 450.0]$ mang lại thứ tự xếp hạng RRF giống hệt 100% so với thang điểm chuẩn $[0.7, 0.95]$ và $[0.3, 0.8]$. Điểm đầu ra luôn chuẩn hóa trong $[0.0, 1.0]$. | **PASSED** |
| `test_fusion_rank_agreement_boosts_consensus_candidate` | Ứng viên xuất hiện ở Top 1 của cả 2 kênh Dense và Sparse nhận điểm consensus gần tiệm cận 1.0 ($1.0$), vượt trội so với các ứng viên chỉ xuất hiện ở 1 kênh (~$0.5$). | **PASSED** |
| `test_deduplication_uses_stable_fingerprint_when_chunk_id_missing` | Khi `chunk_id` rỗng, hệ thống tự động băm 240 ký tự đầu làm fingerprint ổn định. Hai bản ghi trùng prefix được gom nhóm thành 1, giữ nội dung dài hơn và điểm cao nhất. | **PASSED** |
| `test_deterministic_ordering_on_identical_scores_and_metrics` | Hai ứng viên đối xứng có cùng `fused_score`, `coverage`, `exact`, và `rank_sum` được tie-break xác định tuyệt đối dựa trên `key=(source, chunk_id)` bất kể thứ tự nạp vào danh sách. | **PASSED** |
| `test_vietnamese_domain_query_relevance_and_noise_filtering` | Truy vấn nghiệp vụ tài chính/ngân hàng tiếng Việt: tài liệu đúng quy trình và bảo đảm được giữ lại; tài liệu rác (thời tiết, thể thao) bị sàn ngữ nghĩa tương đối $0.75$ loại bỏ an toàn. | **PASSED** |
| `test_semantic_paraphrase_preserves_dense_evidence_without_lexical_overlap` | Truy vấn dạng diễn đạt lại bằng ngôn ngữ đời thường ("sinh em bé") đưa đúng văn bản pháp lý chính sách thai sản lên đầu dù không trùng từ khóa trực tiếp. | **PASSED** |
| `test_exact_identifier_query_prioritizes_literal_code_over_generic_match` | Truy vấn chứa mã hiệu văn bản `"QD-2026/TT-BTC"` đưa chính xác tài liệu chứa mã hiệu lên vị trí #1 bảng xếp hạng, vượt qua các tài liệu chung chung có điểm semantic cao hơn. | **PASSED** |
| `test_score_normalization_and_retrieval_stats_contract` | Khẳng định hợp đồng RerankResult: `0.0 <= score <= 1.0`, `rank >= 0`, `candidate_count == relevant_count + filtered_count`. | **PASSED** |
| `test_canonical_search_unit_emits_rrf_fusion_trace` | Kiểm chứng luồng Canonical SearchUnit ghi nhận đúng `stats["fusion_method"] == "rrf"` cùng đầy đủ `semantic_candidates`, `lexical_candidates`, `candidates`, `relevant`, `filtered_irrelevant`. | **PASSED** |

---

## 3. Nhật ký Chạy Test Thực tế (CLI Log)

```
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, langsmith-0.8.5, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False

tests/test_phase_4_fusion_retrieval.py::test_fusion_scale_invariance_across_divergent_score_magnitudes PASSED [ 11%]
tests/test_phase_4_fusion_retrieval.py::test_fusion_rank_agreement_boosts_consensus_candidate PASSED [ 22%]
tests/test_phase_4_fusion_retrieval.py::test_deduplication_uses_stable_fingerprint_when_chunk_id_missing PASSED [ 33%]
tests/test_phase_4_fusion_retrieval.py::test_deterministic_ordering_on_identical_scores_and_metrics PASSED [ 44%]
tests/test_phase_4_fusion_retrieval.py::test_vietnamese_domain_query_relevance_and_noise_filtering PASSED [ 55%]
tests/test_phase_4_fusion_retrieval.py::test_semantic_paraphrase_preserves_dense_evidence_without_lexical_overlap PASSED [ 66%]
tests/test_phase_4_fusion_retrieval.py::test_exact_identifier_query_prioritizes_literal_code_over_generic_match PASSED [ 77%]
tests/test_phase_4_fusion_retrieval.py::test_score_normalization_and_retrieval_stats_contract PASSED [ 88%]
tests/test_phase_4_fusion_retrieval.py::test_canonical_search_unit_emits_rrf_fusion_trace PASSED [100%]

======================== 9 passed, 1 warning in 4.29s =========================

Chạy toàn bộ 5 suites liên quan:
99 passed, 2 warnings in 29.84s
```

---

## 4. Xác nhận Tuân thủ Ranh giới & Tiêu chí Nghiệm thu

* [x] **Dense và Lexical được fuse ổn định:** Không cộng raw score khác scale; RRF hoạt động chuẩn xác; scale invariance được chứng minh qua test.
* [x] **Dedupe & Tie-break xác định:** Fingerprint 240 ký tự hoạt động ổn định khi thiếu `chunk_id`; tie-breaking nhất quán 100% qua khóa `(source, chunk_id)`.
* [x] **Hợp đồng Score & Trace:** Score luôn nằm trong $[0.0, 1.0]$; stats chứa đầy đủ `fusion_method: "rrf"` và số lượng candidate theo đúng contract.
* [x] **Không xâm phạm ranh giới:**
  * Giữ nguyên pipeline Ingestion/Index (Phase 1–2C).
  * Giữ nguyên Laya query planner & routing trace (DATN-35).
  * Không can thiệp vào Production ACL issuer/JWKS hay staging multi-tenant checks (DATN-61).
  * Giữ nguyên MMR context packing và citation validator (PR #13).
