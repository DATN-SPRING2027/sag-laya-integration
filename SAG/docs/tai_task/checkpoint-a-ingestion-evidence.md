# Báo Cáo Bằng Chứng Nghiệm Thu (Evidence Report)
## Checkpoint A — Ingestion Lane (SEARCH_READY End-to-End)

- **Ngày thực hiện:** 2026-10-02
- **Tài liệu kế hoạch:** [`checkpoint-a-ingestion-plan.md`](checkpoint-a-ingestion-plan.md)
- **Tài liệu phân tích:** [`checkpoint-a-ingestion-research.md`](checkpoint-a-ingestion-research.md)
- **Suite kiểm thử chính:** [`apps/api/tests/test_checkpoint_a_ingestion.py`](../../apps/api/tests/test_checkpoint_a_ingestion.py)

---

### 1. Đối chiếu Tiêu chuẩn Nghiệm thu (Acceptance Criteria Mapping)

| Yêu cầu chấp thuận (Acceptance Requirement) | Phương án xử lý trong mã nguồn | Kiểm thử kiểm chứng (Test Case) | Trạng thái |
| :--- | :--- | :--- | :---: |
| **Nối Upload $\rightarrow$ Canonical $\rightarrow$ Dedup $\rightarrow$ Index** | Pipeline tích hợp tại worker `_process_document_unlocked` và `run_search_indexing_stage`. | `test_checkpoint_a_e2e_upload_to_manifest_verified` | **PASSED** |
| **Chỉ đặt `SEARCH_READY` khi manifest nhất quán** | `search_index_service.py` đối soát count và sha256 checksum giữa PostgreSQL và Qdrant trước khi cập nhật `search_status = "SEARCH_READY"`. | `test_checkpoint_a_manifest_checksum_mismatch_fails_closed` | **PASSED** |
| **Cách ly làn tìm kiếm khỏi Knowledge Enrichment** | Đóng gói `schedule_universe_refresh` trong `try...except`, ghi log warning bằng sanitizer, không làm gián đoạn hoặc hạ cấp `SEARCH_READY`. | `test_checkpoint_a_universe_refresh_failure_does_not_downgrade_search_ready` | **PASSED** |
| **Enrichment bị tắt, trễ hoặc rỗng queue không chặn Search** | Hệ thống bỏ qua bước làm giàu tri thức nếu không có worker/queue nền, tài liệu vẫn đạt `SEARCH_READY` ngay sau khi index Qdrant hoàn tất. | `test_checkpoint_a_enrichment_disabled_or_lag_does_not_block_search` | **PASSED** |
| **Xử lý Parse Failure fail-closed** | Worker bắt ngoại lệ parse, đánh dấu `DocumentVersion.status = "FAILED"`, `IngestionRun.error_stage = "PARSE"`, không nạp điểm vào Qdrant. | `test_checkpoint_a_parse_failure_fails_closed` | **PASSED** |
| **Xử lý Indexing Failure fail-closed** | Gặp lỗi Qdrant/Embedder $\rightarrow$ `INDEX_FAILED`, rollback trạng thái transaction, dọn dẹp các điểm lỗi. | `test_checkpoint_a_indexing_failure_fails_closed` | **PASSED** |
| **Xử lý Empty Index fail-gracefully** | Tệp rỗng / không sinh được SearchUnit $\rightarrow$ dọn sạch điểm vector cũ, đánh dấu `FAILED` với mã lỗi `EMPTY_INDEX`, không gán `SEARCH_READY`. | `test_checkpoint_a_empty_index_fails_gracefully` | **PASSED** |
| **Tính lũy đẳng khi Retry & Reprocess** | Sử dụng UUIDv5 tất định cho Qdrant Point ID; reprocess xóa điểm cũ theo `document_version_id` và nạp lại chính xác mà không nhân bản SearchUnit. | `test_checkpoint_a_idempotent_retry_and_reprocess` | **PASSED** |
| **Lọc và che giấu bí mật (Zero Secret Leakage)** | Module `sag_api.core.sanitizer` tẩy rửa toàn bộ Bearer token, `sk-...`, `ak-...`, basic auth URL, và query parameters nhạy cảm (`api_key`, `password`, `token`, `access_token`, `client_secret`) trong error message / log / DB. | `test_checkpoint_a_zero_secret_leakage` | **PASSED** |
| **Khôi phục thảm họa (Disaster Recovery - SSOT)** | PostgreSQL là nguồn chân lý duy nhất (SSOT); hàm `rebuild_search_index_for_project` phục hồi nguyên trạng Qdrant vector index và verify manifest thành công khi xóa trắng Qdrant. | `test_checkpoint_a_disaster_recovery_rebuild` | **PASSED** |
| **Lỗi Extraction sau khi Index không hạ `SEARCH_READY`** | Đảm bảo khi Phase 2C đã xác minh `SEARCH_READY`, lỗi từ legacy extraction hoặc LLM enrichment phía sau không được phép hạ cấp `search_status` hoặc xóa `search_ready_at`, đồng thời giữ `Document.status = READY` để evidence service resolve locator chính xác. | `test_checkpoint_a_extraction_failure_after_indexing_does_not_downgrade_search_ready` | **PASSED** |
| **Kiểm thử E2E từ Upload API đến Manifest** | Chạy toàn bộ chu trình thực tế từ HTTP multipart upload API (`POST /api/v1/projects/{project_id}/documents/upload`) đến worker xử lý và xác minh Qdrant manifest. | `test_checkpoint_a_e2e_real_upload_api_to_manifest_verified` | **PASSED** |
| **Xử lý Whitespace Anchor Fallback & Locator** | Khi `source_anchor` chỉ chứa khoảng trắng (`"   "`), tự động fallback sang `block-<id[:8]>`, cập nhật DB và verify resolve thành công qua `resolve_traceable_evidence`. | `test_checkpoint_a_whitespace_source_anchor_fallback_and_locator_resolution` | **PASSED** |

---

### 2. Contract Payload cho nhánh Retrieval song song (PR #12 / PR #13)

Các trường payload được lưu trữ trong Qdrant Vector Payload để phục vụ truy vấn và trích dẫn ngược:

```json
{
  "project_id": "proj-uuid",
  "tenant_id": "tenant_default",
  "source_id": "src-uuid",
  "document_id": "doc-uuid",
  "document_version_id": "ver-uuid",
  "version_no": 1,
  "canonical_block_id": "blk-uuid",
  "block_from_id": "blk-start-uuid",
  "block_to_id": "blk-end-uuid",
  "source_anchor": "sec_intro_p1",
  "page_from": 1,
  "page_to": 1,
  "section_path": "Chapter 1 / Section A",
  "char_count": 250,
  "token_count": 65,
  "content_hash": "sha256-hex",
  "security_partition_id": "default",
  "valid_from_ts": 1727800000.0,
  "is_superseded": false
}
```

Các trường payload được đánh index lọc (`PAYLOAD_INDEX_FIELDS`):
- `project_id` (keyword)
- `tenant_id` (keyword)
- `security_partition_id` (keyword)
- `source_id` (keyword)
- `document_id` (keyword)
- `document_version_id` (keyword)
- `is_superseded` (bool)
- `valid_from_ts` (float)

---

### 3. Phản Hồi & Khắc Phục Các Finding Trong Code Review PR #16

- **Finding 1 [P1 - Isolation & Evidence Eligibility]:** 
  - Đã cập nhật outer exception handler trong `tasks.py` để bảo tồn `ver.search_status == "SEARCH_READY"` và `ver.search_ready_at` khi `engine_manager.process_document()` gặp lỗi.
  - Đồng thời thiết lập `doc.status = DocumentStatus.READY` nếu `is_search_ready` là `True` để thoả mãn điều kiện lọc `Document.status == READY` tại `evidence_service.py:180`, tránh việc kết quả tìm kiếm bị loại bỏ khỏi evidence pack.
  - Cập nhật test case `test_checkpoint_a_extraction_failure_after_indexing_does_not_downgrade_search_ready` xác thực `resolve_traceable_evidence` resolve locator thành công.
- **Finding 2 [P1 - Sanitizer & Log Sinks]:** 
  - Mở rộng regex trong `sanitizer.py` nhận diện snake_case và kebab-case (`api_key`, `access_token`, `client_secret`, `password`, `secret`, `refresh_token`, `id_token`), dọn dẹp `_URL` thừa.
  - Tẩy rửa toàn bộ các log sink trực tiếp (`emb_exc`, `del_err`, `count_err`, `scroll_res.text`, `scroll_err`) bằng `sanitize_error_message`.
  - Mở rộng assertion trong `test_checkpoint_a_zero_secret_leakage` dùng `caplog` kiểm chứng không rò rỉ secret trong log.
- **Finding 3 [P2 - Locator Anchor & Whitespace Fallback]:** 
  - Cung cấp fallback tất định `s_anchor = (raw_anchor.strip() if raw_anchor and raw_anchor.strip() else f"block-{unit.block_from_id[:8]}")`. Xử lý trường hợp anchor chỉ chứa khoảng trắng `"   "`.
  - Đồng bộ fallback anchor ngược lại vào `CanonicalBlock.source_anchor` và flush vào database.
  - Thêm test case `test_checkpoint_a_whitespace_source_anchor_fallback_and_locator_resolution`.
- **Finding 4 [P2 - Observability]:** Thêm logic ghi nhận `StageRun(stage="INDEX_SEARCH", status="FAILED", ...)` trong khối xử lý lỗi của worker sau khi rollback; bổ sung assertion kiểm tra `StageRun` tồn tại trong DB sau lỗi.
- **Finding 5 [P2 - E2E Test]:** Bổ sung test case `test_checkpoint_a_e2e_real_upload_api_to_manifest_verified` kiểm chứng luồng liên thông từ HTTP Upload API đến manifest verified.

---

### 4. Nhật Ký Thực Thi Kiểm Thử (Test Execution Log)

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, langsmith-0.8.5, asyncio-1.4.0
asyncio: mode=Mode.AUTO

apps/api/tests/test_checkpoint_a_ingestion.py::test_checkpoint_a_e2e_upload_to_manifest_verified PASSED [  6%]
apps/api/tests/test_checkpoint_a_ingestion.py::test_checkpoint_a_universe_refresh_failure_does_not_downgrade_search_ready PASSED [ 12%]
apps/api/tests/test_checkpoint_a_enrichment_disabled_or_lag_does_not_block_search PASSED [ 18%]
apps/api/tests/test_checkpoint_a_parse_failure_fails_closed PASSED [ 25%]
apps/api/tests/test_checkpoint_a_indexing_failure_fails_closed PASSED [ 31%]
apps/api/tests/test_checkpoint_a_manifest_checksum_mismatch_fails_closed PASSED [ 37%]
apps/api/tests/test_checkpoint_a_empty_index_fails_gracefully PASSED [ 43%]
apps/api/tests/test_checkpoint_a_idempotent_retry_and_reprocess PASSED [ 50%]
apps/api/tests/test_checkpoint_a_zero_secret_leakage PASSED [ 56%]
apps/api/tests/test_checkpoint_a_disaster_recovery_rebuild PASSED [ 62%]
apps/api/tests/test_checkpoint_a_extraction_failure_after_indexing_does_not_downgrade_search_ready PASSED [ 68%]
apps/api/tests/test_checkpoint_a_e2e_real_upload_api_to_manifest_verified PASSED [ 75%]
apps/api/tests/test_checkpoint_a_whitespace_source_anchor_fallback_and_locator_resolution PASSED [ 81%]
apps/api/tests/test_checkpoint_a_reindex_demotes_readiness_before_deleting_points PASSED [ 87%]
apps/api/tests/test_checkpoint_a_chunk_count_without_manifest_does_not_promote_search_ready PASSED [ 93%]
apps/api/tests/test_checkpoint_a_legacy_ready_alias_rejected_without_manifest PASSED [100%]

======================== 16 passed, 1 warning in 3.42s ========================
```

Suite bổ trợ liên quan:
- `apps/api/tests/test_phase_2_worker_execution.py`: 6/6 passed
- `apps/api/tests/test_phase_2c_search_indexing.py`: 17/17 passed
- `apps/api/tests/test_traceability.py`: 2/2 passed
- `apps/api/tests/test_phase_1_upload_and_versioning.py`: 34/34 passed
- **Tổng cộng: 75/75 passed (100%)**
