# Kế Hoạch Triển Khai: Hoàn Tất Ingestion Lane Cho Checkpoint A (SEARCH_READY End-to-End)

Tài liệu nghiên cứu kỹ thuật cơ sở: [`checkpoint-a-ingestion-research.md`](checkpoint-a-ingestion-research.md).
Nguồn chuẩn: [`tasks/plan.md`](../../tasks/plan.md) (Phase 2A–2C, Phase 5, Checkpoint A) và [`tasks/todo.md`](../../tasks/todo.md) (Checkpoint A).

Triển khai hoàn thiện đường ống nạp dữ liệu (Ingestion Lane) nối liền mạch từ upload tệp, trích xuất khối chuẩn tắc (Phase 2A), khử trùng lặp & chuỗi thời gian (Phase 2B) đến trích xuất SearchUnit & nạp chỉ mục Qdrant kèm đối soát manifest (Phase 2C), bảo đảm chỉ thiết lập `SEARCH_READY` khi dữ liệu và manifest nhất quán 100%, đồng thời cách ly hoàn toàn làn tìm kiếm khỏi các lỗi hoặc độ trễ từ các tiến trình làm giàu tri thức ngầm (knowledge enrichment/universe refresh).

---

## Scope

### In:
- **Chuẩn hóa giá trị trạng thái tìm kiếm:** Đồng bộ toàn diện giá trị `search_status = "SEARCH_READY"` trên toàn bộ các tầng (`search_index_service.py`, `rebuild_service.py`, `tasks.py`, `document_service.py`, `evidence_service.py`).
- **Gia cố phòng vệ cách ly nhánh phụ:** Đóng gói lời gọi `schedule_universe_refresh` và các worker/queue tri thức nền trong khối phòng vệ độc lập, bảo đảm không bao giờ ghi đè hoặc hạ cấp `SEARCH_READY` khi nhánh phụ gặp lỗi, bị tắt hoặc trễ tiến độ.
- **Bổ sung trường định vị (Locator) vào Qdrant Payload & Index:** Thêm `source_id`, `document_id`, `version_no`, `block_from_id`, `block_to_id`, `source_anchor` vào payload của `build_qdrant_payload`, đồng thời bổ sung `("source_id", "keyword")` và `("document_id", "keyword")` vào `PAYLOAD_INDEX_FIELDS`.
- **Lọc và che giấu bí mật toàn diện (Zero-Secret Leakage):** Xây dựng module `sag_api/core/sanitizer.py` với hàm `sanitize_error_message` tẩy rửa API keys (`sk-...`, `ak-...`, Bearer), query parameters nhạy cảm (`token`, `api-key`) và basic auth URL trong `IngestionRun.error_message`, `StageRun.error_message` và các dòng log của worker.
- **Xử lý dứt khoát tài liệu rỗng (Empty Index Handling):** Phân định rành mạch giữa manifest verified của tài liệu rỗng và trạng thái `search_status`, đánh dấu `FAILED` đồng bộ trên Document, Version, IngestionRun kèm mã lỗi `EMPTY_INDEX` khi tệp không có nội dung trích xuất.
- **Sửa lỗi nuốt thông tin lỗi trong Status API:** Điều chỉnh `get_document_version_status` trong `document_service.py` để luôn trả về chi tiết lỗi ngay cả khi `error_code` chưa được gán tường minh.
- **Bảo đảm tính lũy đẳng (Idempotent Retry) & Tái tạo (Disaster Recovery):** Khẳng định PostgreSQL là nguồn chân lý duy nhất (SSOT), hỗ trợ xóa sạch và dựng lại toàn bộ vector Qdrant từ bảng `search_units` thông qua `rebuild_service.py`.
- **Bộ kiểm thử hồi quy tích hợp Checkpoint A Ingestion:** Xây dựng suite kiểm thử `test_checkpoint_a_ingestion.py` kiểm chứng đủ 10 kịch bản chấp thuận (Acceptance Gates), đồng thời cập nhật các assertion liên quan trong `test_phase_2c_search_indexing.py`.

### Out:
- **Không làm Global Retrieval, Fusion hay RRF Reranking:** Thuộc phạm vi của nhánh Retrieval song song (Phase 4 / PR #12).
- **Không làm Evidence Context Fitting, Token Budgeting hay Citation UI:** Thuộc phạm vi của PR #13 (`evidence_service.py` / `SearchPanel`).
- **Không làm trích xuất tri thức nâng cao E1/E2 hay phân cụm Tree (Leiden Algorithm):** Thuộc phạm vi Phase 5 và Phase 6.
- **Không thay đổi cấu trúc bảng cơ sở dữ liệu (Database Schema DDL) hoặc tạo migration mới khi chưa thống nhất.**

---

## Action Items

- [x] **Step 1: Tạo module làm sạch bí mật tập trung (`sag_api/core/sanitizer.py`)**
  - Tạo file [`apps/api/sag_api/core/sanitizer.py`](../../apps/api/sag_api/core/sanitizer.py) cung cấp hàm `sanitize_error_message(value: object) -> str`.
  - Cấu hình các biểu thức chính quy (regex) xử lý:
    - Bearer tokens: `(?i)bearer\s+\S+` $\rightarrow$ `Bearer [REDACTED]`.
    - API keys: `(?i)\b(?:sk|ak)-[a-z0-9._-]{6,}\b` $\rightarrow$ `[REDACTED]`.
    - Sensitive query parameters: `(?i)([?&](?:token|key|signature|credential|authorization|api-key|x-amz-[^=]+)=)[^&#\s]+` $\rightarrow$ `\1[REDACTED]`.
    - Basic auth credentials trong URL: `(?i)(https?://)([^:]+:[^@]+@)` $\rightarrow$ `\1[REDACTED]@`.
  - Giới hạn độ dài thông điệp tối đa 500 ký tự để chống tràn log và database.

- [x] **Step 2: Cập nhật Payload Contract và chuẩn hóa trạng thái trong `search_index_service.py`**
  - Mở rộng mảng `PAYLOAD_INDEX_FIELDS`: Thêm `("source_id", "keyword")` và `("document_id", "keyword")`.
  - Nâng cấp hàm `build_qdrant_payload`:
    - Nhận thêm các tham số tùy chọn: `source_id: str | None = None`, `document_id: str | None = None`, `version_no: int | None = None`, `block_from_id: str | None = None`, `block_to_id: str | None = None`, `source_anchor: str | None = None`.
    - Đóng gói đầy đủ các trường trên vào dictionary payload trả về.
  - Cập nhật hàm `index_search_units_to_qdrant`: Truyền các trường metadata từ `version`, `unit`, `doc`, và `blocks_by_id` vào `build_qdrant_payload`.
  - Chuẩn hóa `run_search_indexing_stage`:
    - Đổi `document_version.search_status = "READY"` thành `"SEARCH_READY"`.
    - Xử lý khi `len(units) == 0`: Xác minh Qdrant có 0 điểm, nhưng gán `document_version.search_status = "FAILED"`, `document_version.search_ready_at = None`, và ném ngoại lệ có kiểm soát `RuntimeError("EMPTY_INDEX: Tài liệu không có nội dung văn bản để lập chỉ mục tìm kiếm")`.
    - Áp dụng `sanitize_error_message` vào `StageRun.error_message`.

- [x] **Step 3: Đồng bộ trạng thái và Payload trong `rebuild_service.py`**
  - Trong `rebuild_search_index_for_version` và `rebuild_search_index_for_project`:
    - Xác nhận các SearchUnit được nạp lại mang đầy đủ payload mới (`source_id`, `document_id`, `source_anchor`...).
    - Xác nhận `ver.search_status` được duy trì là `"SEARCH_READY"` sau khi rebuild thành công.
    - Áp dụng `sanitize_error_message` khi ghi log lỗi rebuild.

- [x] **Step 4: Cách ly Worker phụ và gia cố `tasks.py`**
  - Trong hàm `_process_document_unlocked`:
    - **Cách ly Universe Refresh:** Đóng gói lời gọi `schedule_universe_refresh` (dòng 660–669) trong khối `try...except Exception as uni_err:` riêng biệt. Ghi log cảnh báo bằng `sanitize_error_message(uni_err)`, không re-raise ngoại lệ ra ngoài.
    - **Đánh giá Readiness chính xác:** Tại dòng 639–646, kiểm tra trạng thái dựa trên kết quả Phase 2C (`doc_ver.search_status == "SEARCH_READY"` và có SearchUnits). Không để `outcome.chunk_count` của engine cũ ghi đè sai lệch trạng thái của Phase 2.
    - **Xử lý lỗi toàn diện & chống rò rỉ:**
      - Áp dụng `sanitize_error_message` cho `ing_run.error_message`, `doc.error`, và `public_message` ở tất cả các nhánh (không chỉ nhánh parse).
      - Luôn gán `ing_run.error_code` khi thất bại: `err_code or f"{err_layer.value}_{err_stage.value}_FAILED"`.

- [x] **Step 5: Khắc phục hiển thị lỗi trong Status API (`document_service.py`)**
  - Chỉnh sửa dòng 1285 trong hàm `get_document_version_status`:
    - Đổi `if latest_run.error_code:` thành `if latest_run.error_message or latest_run.error_code:`.
    - Cung cấp `code = latest_run.error_code or f"{latest_run.error_layer or 'API'}_{latest_run.error_stage or 'INGEST'}_FAILED"`.
    - Đảm bảo client luôn nhận được cấu trúc `error: {layer, stage, code, message}` khi tài liệu ở trạng thái `FAILED`.

- [x] **Step 6: Cập nhật các Assertions trong Test Suites hiện hữu**
  - Trong [`tests/test_phase_2c_search_indexing.py`](../../apps/api/tests/test_phase_2c_search_indexing.py#L287):
    - Đổi `assert ver_updated.search_status == "READY"` thành `assert ver_updated.search_status == "SEARCH_READY"`.
  - Trong [`tests/test_phase_2_worker_execution.py`](../../apps/api/tests/test_phase_2_worker_execution.py#L337):
    - Khẳng định chặt chẽ: `assert ver_updated.search_status == "SEARCH_READY"`.

- [x] **Step 7: Xây dựng Test Suite Tích Hợp Checkpoint A (`test_checkpoint_a_ingestion.py`)**
  - Tạo mới file [`apps/api/tests/test_checkpoint_a_ingestion.py`](../../apps/api/tests/test_checkpoint_a_ingestion.py) bao phủ đủ 10 ca kiểm thử:
    1. `test_checkpoint_a_e2e_upload_to_manifest_verified`: Luồng thành công đầy đủ upload $\rightarrow$ parse $\rightarrow$ dedup $\rightarrow$ index $\rightarrow$ manifest verified $\rightarrow$ `SEARCH_READY`.
    2. `test_checkpoint_a_universe_refresh_failure_does_not_downgrade_search_ready`: Mock `schedule_universe_refresh` ném ngoại lệ $\rightarrow$ `ver.search_status` vẫn giữ nguyên là `"SEARCH_READY"`.
    3. `test_checkpoint_a_enrichment_disabled_or_lag_does_not_block_search`: Không có job_queue / queue rỗng $\rightarrow$ `SEARCH_READY` đạt được ngay lập tức.
    4. `test_checkpoint_a_parse_failure_fails_closed`: Tệp lỗi cú pháp $\rightarrow$ `FAILED`, `error_stage = PARSE`, không có điểm Qdrant.
    5. `test_checkpoint_a_indexing_failure_fails_closed`: Mock Qdrant/Embedder chết $\rightarrow$ `INDEX_FAILED`, rollback transactional.
    6. `test_checkpoint_a_manifest_checksum_mismatch_fails_closed`: Giả lập sai lệch checksum Qdrant $\rightarrow$ `INDEX_FAILED`.
    7. `test_checkpoint_a_empty_index_fails_gracefully`: Tệp chỉ có khoảng trắng $\rightarrow$ dọn sạch Qdrant, đánh dấu `FAILED` với mã `EMPTY_INDEX`, không bật `SEARCH_READY`.
    8. `test_checkpoint_a_idempotent_retry_and_reprocess`: Reprocess tài liệu $\rightarrow$ xóa point cũ, nạp point mới, cùng UUIDv5, không nhân bản SearchUnit.
    9. `test_checkpoint_a_zero_secret_leakage`: Cố tình gây lỗi với URL chứa `api-key=secret123` và token `sk-live-abc` $\rightarrow$ kiểm tra DB và log không chứa chuỗi nhạy cảm.
    10. `test_checkpoint_a_disaster_recovery_rebuild`: Xóa trắng Qdrant collection $\rightarrow$ chạy `rebuild_search_index_for_project` phục hồi đầy đủ điểm và manifest verified từ PostgreSQL.

- [x] **Step 8: Thực thi kiểm thử hồi quy & Kiểm tra chất lượng mã nguồn**
  - Chạy toàn bộ các test suites liên quan bằng `pytest`:
    - `tests/test_checkpoint_a_ingestion.py` (10/10 passed)
    - `tests/test_phase_2_worker_execution.py` (6/6 passed)
    - `tests/test_phase_2c_search_indexing.py` (17/17 passed)
    - `tests/test_phase_1_upload_and_versioning.py` (34/34 passed)
    - `tests/test_traceability.py` (2/2 passed)
    - Tổng cộng: **69/69 tests passed**
  - Chạy `py_compile` xác nhận không có lỗi cú pháp.

- [x] **Step 9: Cập nhật tài liệu tiến độ & Bằng chứng nghiệm thu**
  - Cập nhật các mục thuộc Checkpoint A Ingestion trong [`tasks/todo.md`](../../tasks/todo.md).
  - Soạn tài liệu bằng chứng nghiệm thu (Evidence Report).

---

## Validation

- **Lệnh thực thi kiểm thử:**
  ```powershell
  $env:PYTHONPATH="."
  python -m pytest tests/test_checkpoint_a_ingestion.py tests/test_phase_2_worker_execution.py tests/test_phase_2c_search_indexing.py tests/test_traceability.py -v
  ```
- **Tiêu chí nghiệm thu (Acceptance Criteria):**
  1. 100% test cases trong `test_checkpoint_a_ingestion.py` pass.
  2. `SEARCH_READY` chỉ xuất hiện khi manifest count và checksum giữa PostgreSQL và Qdrant trùng khớp 100%.
  3. Lỗi nhân tạo tại `schedule_universe_refresh` (ném `RuntimeError("Redis connection refused")`) chứng minh `DocumentVersion.search_status` vẫn giữ nguyên là `"SEARCH_READY"`.
  4. Lỗi nhân tạo chứa API token `sk-proj-super-secret-key-12345` chứng minh trường `error_message` trong database hiển thị `[REDACTED]` và không chứa chuỗi nhạy cảm.
  5. API `/status` trả về thông tin lỗi đầy đủ khi tài liệu bị lỗi (không trả về `error: null`).

---

## Technical Decisions (Resolved)

1. **Xử lý tài liệu rỗng (Empty Index Resolution):**
   - Tài liệu tải lên không sinh được SearchUnit nào sẽ bị đánh dấu `status = "FAILED"`, `search_status = "FAILED"`, `search_ready_at = None`, kèm mã lỗi `EMPTY_INDEX`. Hệ thống dọn sạch vector cũ trong Qdrant và kiểm chứng `qdrant_count == 0`.
2. **Cơ chế Rebuild không cần khóa toàn cục (Lock-Free Rebuild):**
   - Quá trình rebuild cho phép chạy trực tiếp trên các collection hiện hữu nhờ tính lũy đẳng cấp point (UUIDv5 tất định) và xóa nguyên tử theo `document_version_id`. Không cần khóa toàn cục bảng hoặc collection.
