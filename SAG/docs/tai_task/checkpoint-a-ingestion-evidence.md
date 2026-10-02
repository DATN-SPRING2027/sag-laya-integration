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
| **Lọc và che giấu bí mật (Zero Secret Leakage)** | Module `sag_api.core.sanitizer` tẩy rửa toàn bộ Bearer token, `sk-...`, `ak-...`, basic auth URL, và query parameters nhạy cảm trong error message / log / DB. | `test_checkpoint_a_zero_secret_leakage` | **PASSED** |
| **Khôi phục thảm họa (Disaster Recovery - SSOT)** | PostgreSQL là nguồn chân lý duy nhất (SSOT); hàm `rebuild_search_index_for_project` phục hồi nguyên trạng Qdrant vector index và verify manifest thành công khi xóa trắng Qdrant. | `test_checkpoint_a_disaster_recovery_rebuild` | **PASSED** |

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

### 3. Nhật Ký Thực Thi Kiểm Thử (Test Execution Log)

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, langsmith-0.8.5, asyncio-1.4.0
asyncio: mode=Mode.AUTO

apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_e2e_upload_to_manifest_verified PASSED [ 10%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_universe_refresh_failure_does_not_downgrade_search_ready PASSED [ 20%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_enrichment_disabled_or_lag_does_not_block_search PASSED [ 30%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_parse_failure_fails_closed PASSED [ 40%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_indexing_failure_fails_closed PASSED [ 50%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_manifest_checksum_mismatch_fails_closed PASSED [ 60%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_empty_index_fails_gracefully PASSED [ 70%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_idempotent_retry_and_reprocess PASSED [ 80%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_zero_secret_leakage PASSED [ 90%]
apps\api\tests\test_checkpoint_a_ingestion.py::test_checkpoint_a_disaster_recovery_rebuild PASSED [100%]

======================= 10 passed, 1 warning in 11.23s =======================
```

Suite bổ trợ liên quan:
- `apps/api/tests/test_phase_2_worker_execution.py`: 6/6 passed
- `apps/api/tests/test_phase_2c_search_indexing.py`: 17/17 passed
- `apps/api/tests/test_traceability.py`: 2/2 passed
- `apps/api/tests/test_phase_1_upload_and_versioning.py`: 34/34 passed
