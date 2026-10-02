# Báo Cáo Nghiên Cứu Kỹ Thuật Chuyên Sâu: Hoàn Tất Ingestion Lane Cho Checkpoint A (SEARCH_READY End-to-End)

---

## 1. Tóm Tắt Điều Hành & Định Vị Nhiệm Vụ (Executive Summary)

### 1.1. Bối cảnh và nguồn chuẩn
- **Nguồn chuẩn:** 
  - [`sag-laya-integration/SAG/tasks/plan.md`](../../tasks/plan.md) (Phase 2A–2C, Phase 5, Checkpoint A).
  - [`sag-laya-integration/SAG/tasks/todo.md`](../../tasks/todo.md) (Checkpoint A).
  - [`SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`](../SAG_Knowledge_Routing_RAG_Workflow_v1.1.md) (Mục 6, 7, 8 và Phụ lục A/B/E).
  - [`phase-0-contracts-and-foundations.md`](../phase-0-contracts-and-foundations.md) (Pillar 1: Lifecycle, Pillar 2: Schema).
- **Phân đoạn thực thi:** Hoàn thiện và gia cố toàn bộ làn nạp dữ liệu (**Ingestion Lane**) từ khâu upload tệp, phân tích khối chuẩn tắc (Phase 2A), khử trùng lặp & chuỗi thời gian (Phase 2B), trích xuất SearchUnit & nạp vector Qdrant kèm kiểm chứng manifest (Phase 2C), cho tới khi đạt trạng thái chuẩn thức `SEARCH_READY`.
- **Ranh giới giao diện:** Tác vụ này chạy song song với nhánh **Retrieval Lane (Phase 4 / PR #13)**. Ingestion Lane chịu trách nhiệm toàn vẹn dữ liệu (PostgreSQL + Qdrant) và công bố contract chuẩn; không can thiệp vào tầng global retrieval fusion, evidence context assembly hay giao diện người dùng trích dẫn (citation UI).

### 1.2. Mục tiêu kỹ thuật cốt lõi
1. **Nối thông chuỗi Ingestion:** Upload $\rightarrow$ Canonical Extraction $\rightarrow$ Dedup & Temporal $\rightarrow$ SearchUnit Chunking $\rightarrow$ Dual Vector Embedding (Dense + Sparse BM25) $\rightarrow$ Qdrant Indexing $\rightarrow$ Manifest Verification $\rightarrow$ Đạt `SEARCH_READY`.
2. **Bảo vệ tính bất biến của `SEARCH_READY`:** Trạng thái sẵn sàng tìm kiếm chỉ được xác lập sau khi manifest giữa PostgreSQL và Qdrant đã khớp 100%. Các worker/queue phụ ngầm (E2 enrichment, Knowledge Graph, Universe Overview rebuild) tuyệt đối không được phép hạ cấp, trì hoãn hoặc ghi đè trạng thái `SEARCH_READY`.
3. **Chống rò rỉ bí mật (Zero-Secret Leakage):** Bịt kín các đường dẫn ghi log, thông báo lỗi trong `IngestionRun.error_message`, `StageRun.error_message`, và `Document.error` để không lộ API key, token truy cập hoặc đường dẫn URL nội bộ nhạy cảm.
4. **Xử lý toàn diện các ca biên:** Parse failure, indexing failure, manifest count/checksum mismatch, tài liệu rỗng (empty index), và cơ chế thử lại lũy kế (idempotent retry).
5. **Chốt cứng Payload & Status Contract:** Thống nhất giao thức trao đổi dữ liệu với nhánh Retrieval để đảm bảo `resolve_exact_evidence_locators` truy xuất chính xác từng khối trích dẫn mà không bị `no-answer`.

---

## 2. Phân Tích Kiến Trúc Hiện Trạng & Phát Hiện Lỗ Hổng Chi Tiết (Root Cause & Gap Analysis)

Qua khảo sát chi tiết mã nguồn tại [`sag_api/jobs/tasks.py`](../../apps/api/sag_api/jobs/tasks.py), [`sag_api/services/search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py), [`sag_api/services/document_service.py`](../../apps/api/sag_api/services/document_service.py), [`sag_api/services/evidence_service.py`](../../apps/api/sag_api/services/evidence_service.py) và các file test liên quan, chúng tôi xác định 8 lỗ hổng/gap kỹ thuật cụ thể:

### Gap 1: Worker phụ hạ cấp `SEARCH_READY` thành `FAILED` khi gặp ngoại lệ
- **Vị trí mã nguồn:** [`sag_api/jobs/tasks.py`](../../apps/api/sag_api/jobs/tasks.py#L660-L669):
  ```python
  if job_queue is not None:
      from sag_api.services.universe_service import schedule_universe_refresh
      await schedule_universe_refresh(
          session, job_queue, source_id=source.id, reason="document_processed"
      )
  ```
- **Cơ chế lỗi:** Lời gọi này nằm bên trong khối `try...except Exception as e:` bao quanh toàn bộ hàm `_process_document_unlocked`. Dù bước index trước đó đã commit `status = DocumentStatus.READY`, `ver.status = "SEARCH_READY"`, và `ver.search_status = "SEARCH_READY"`, nhưng nếu `schedule_universe_refresh` ném ngoại lệ (ví dụ: User không tồn tại, Redis/queue ngắt kết nối, lỗi DB lock), luồng thực thi sẽ nhảy vào khối `except Exception as e:` ở dòng 527.
- **Hệ quả phá hủy:** Tại dòng 553 và 584, worker cập nhật lại `Document.status = DocumentStatus.FAILED` và `ver.status = "FAILED"`. Tài liệu bị hỏng trạng thái readiness tìm kiếm một cách sai lệch, vi phạm nguyên tắc cách ly làn tìm kiếm khỏi nhánh tri thức.
- **Giải pháp:** Bọc `schedule_universe_refresh` trong khối `try...except Exception as queue_exc:` riêng biệt, chỉ ghi log cảnh báo (`log.warning`), không ném tiếp ngoại lệ ra ngoài và không làm ảnh hưởng đến commit thành công của `SEARCH_READY`.

---

### Gap 2: Sự bất nhất giá trị trạng thái (`"READY"` vs `"SEARCH_READY"`)
- **Vị trí mã nguồn:**
  1. [`sag_api/services/search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py#L709):
     ```python
     if manifest_verified:
         document_version.search_status = "READY"
     ```
  2. [`sag_api/jobs/tasks.py`](../../apps/api/sag_api/jobs/tasks.py#L641):
     ```python
     ver.status = "SEARCH_READY"
     ver.search_status = "SEARCH_READY"
     ```
  3. [`sag_api/services/evidence_service.py`](../../apps/api/sag_api/services/evidence_service.py#L181):
     ```python
     DocumentVersion.search_status == "SEARCH_READY"
     ```
  4. [`tests/test_phase_2c_search_indexing.py`](../../apps/api/tests/test_phase_2c_search_indexing.py#L287):
     ```python
     assert ver_updated.search_status == "READY"
     ```
- **Hệ quả:** Khi hàm `run_search_indexing_stage` chạy độc lập hoặc qua `rebuild_service.py`, nó đặt giá trị `"READY"`. Tuy nhiên, câu truy vấn SQL của `evidence_service.py` dùng phép lọc cứng `DocumentVersion.search_status == "SEARCH_READY"`. Do đó, các tài liệu này sẽ bị coi là chưa sẵn sàng tìm kiếm, dẫn đến `no-answer` giả tạo.
- **Giải pháp:** Đồng bộ hóa giá trị chuẩn `SEARCH_READY` trên toàn bộ mã nguồn: `search_index_service.py`, `rebuild_service.py`, `tasks.py`, `document_service.py`, và cập nhật assertion tương ứng trong `test_phase_2c_search_indexing.py`.

---

### Gap 3: Rò rỉ bí mật trong `error_message`, `StageRun` và Logs
- **Vị trí mã nguồn:**
  - [`sag_api/jobs/tasks.py`](../../apps/api/sag_api/jobs/tasks.py#L466, #L495, #L538, #L582):
    Dòng 495: `ing_run.error_message = str(pipe_err)` (chuyển đổi raw exception thành chuỗi, không qua lọc).
    Dòng 538: `public_message = message` (chỉ lọc qua `_redact_parser_reason` nếu `parser_failed` là True; nếu lỗi xảy ra ở khâu DEDUP, EMBED, hoặc INDEX thì `public_message` giữ nguyên chuỗi lỗi gốc).
  - [`sag_api/services/search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py#L726):
    `error_message=indexing_error` được lưu trực tiếp vào `StageRun.error_message`.
- **Rủi ro an ninh:** Khi `httpx` gặp lỗi kết nối tới Qdrant hoặc dịch vụ Embedding bên thứ ba mang URL hoặc header chứa `api-key`, `Authorization: Bearer sk-...`, chuỗi nhạy cảm sẽ bị lưu thẳng vào DB và trả về qua API tra cứu trạng thái public.
- **Giải pháp:** Xây dựng module chuẩn `sag_api/core/sanitizer.py` chứa hàm `sanitize_error_message(value: object) -> str` để tẩy rửa toàn diện API keys, bearer tokens, URL credentials, và query parameters bí mật trước khi lưu trữ hoặc ghi log.

---

### Gap 4: Xung đột đếm chunk giữa Legacy Engine và Phase 2 Index
- **Vị trí mã nguồn:** [`sag_api/jobs/tasks.py`](../../apps/api/sag_api/jobs/tasks.py#L640):
  ```python
  if outcome.chunk_count > 0:
      ver.status = "SEARCH_READY"
      ver.search_status = "SEARCH_READY"
      ver.search_ready_at = datetime.now(UTC)
  else:
      ver.status = "FAILED"
      ver.search_status = "FAILED"
  ```
- **Bản chất vấn đề:** `outcome.chunk_count` là số chunk sinh ra từ `engine_manager.process_document` (zleap engine cũ), không phản ánh số lượng `SearchUnit` thực tế được sinh ra và nạp vào Qdrant trong Phase 2C (`len(units)`).
- **Hệ quả:** Nếu Phase 2 đã index thành công 10 SearchUnits và manifest verified, nhưng `engine_manager.process_document` trả về 0 chunk (hoặc bị mock/bỏ qua), dòng 645 sẽ ghi đè `ver.status = "FAILED"`, phá hủy kết quả Phase 2.
- **Giải pháp:** Tiêu chí đánh giá `SEARCH_READY` phải căn cứ vào kết quả kiểm chứng của Phase 2 (`doc_ver.search_status == "SEARCH_READY"` và `stage_run.metrics_json["search_unit_count"] > 0`).

---

### Gap 5: Lệch pha trạng thái khi tài liệu rỗng (Empty Index Inconsistency)
- **Vị trí mã nguồn:**
  - [`sag_api/services/search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py#L694):
    Khi `not units`, nếu `qdrant_count == 0` thì `manifest_verified = True`, và dịch vụ đánh dấu `search_status = "READY"`, `search_ready_at = datetime.now(UTC)`.
  - Tuy nhiên, trong [`tasks.py`](../../apps/api/sag_api/jobs/tasks.py#L609-L646):
    `Document.status = DocumentStatus.READY`
    `IngestionRun.status = "SUCCEEDED"`
    Nhưng dòng 645 lại ép: `ver.status = "FAILED"`, `ver.search_status = "FAILED"`.
- **Hệ quả:** Dữ liệu bị phân mảnh trạng thái trầm trọng: Document báo READY, Run báo SUCCEEDED, nhưng Version lại báo FAILED và vẫn còn `search_ready_at`!
- **Giải pháp:** Định nghĩa rõ ràng trạng thái cho Empty Index:
  - Khi một tài liệu hợp lệ về file nhưng trích xuất ra 0 blocks/0 units: Dọn sạch vector Qdrant, xác nhận 0 points.
  - Đánh dấu đồng bộ: `Document.status = DocumentStatus.FAILED`, `doc_ver.status = "FAILED"`, `doc_ver.search_status = "FAILED"`, `doc_ver.search_ready_at = None`, `ing_run.status = "FAILED"`, `ing_run.error_code = "EMPTY_INDEX"`, `ing_run.error_message = "Tài liệu không có nội dung văn bản để lập chỉ mục tìm kiếm"`.

---

### Gap 6: Lỗi nuốt thông tin lỗi trong API tra cứu trạng thái (`document_service.py`)
- **Vị trí mã nguồn:** [`sag_api/services/document_service.py`](../../apps/api/sag_api/services/document_service.py#L1285-L1291):
  ```python
  if latest_run.error_code:
      err_info = {
          "layer": latest_run.error_layer,
          "stage": latest_run.error_stage,
          "code": latest_run.error_code,
          "message": latest_run.error_message,
      }
  ```
- **Hệ quả:** Khi worker gặp lỗi nạp Phase 2 (dòng 491-496 trong `tasks.py`), worker chỉ gán `error_layer`, `error_stage`, và `error_message`, nhưng **không gán** `error_code`. Do đó `latest_run.error_code` là `None`, dẫn đến điều kiện `if latest_run.error_code:` bị False! Endpoint GET `/status` trả về `"error": null` cho client dù trạng thái là `FAILED`.
- **Giải pháp:** Sửa điều kiện thành `if latest_run.error_message or latest_run.error_code:`, đồng thời trong `tasks.py` luôn gán `error_code` mặc định khi xảy ra lỗi.

---

### Gap 7: Thiếu trường liên kết Locator trong Qdrant Payload cho Retrieval
- **Hiện trạng mã nguồn:**
  `build_qdrant_payload` trong [`search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py#L278-L316) chỉ lưu:
  `_sag_id`, `search_unit_id`, `tenant_id`, `project_id`, `document_version_id`, `security_partition_id`, `valid_from_ts`, `valid_to_ts`, `token_count`, `section_path`, `page_from`, `page_to`, `content_hash`, `content`.
- **Điểm thiếu cho nhánh Retrieval:**
  1. `source_id`: Retrieval cần để giao với `authorized_source_ids`.
  2. `document_id`: Retrieval cần để định danh tài liệu cha.
  3. `version_no`: Cần cho citation display locator.
  4. `block_from_id` và `block_to_id`: Cần cho block range tracking.
  5. `source_anchor`: Anchor của canonical block bắt đầu.
- **Giải pháp:** Mở rộng `build_qdrant_payload` nhận thêm các trường trên với giá trị mặc định an toàn để không làm gãy các lời gọi kiểm thử hiện có.

---

### Gap 8: Thiếu cấu hình chỉ mục lọc trong `PAYLOAD_INDEX_FIELDS`
- **Hiện trạng mã nguồn:** [`search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py#L108-L117):
  Mảng `PAYLOAD_INDEX_FIELDS` hiện chỉ có: `tenant_id`, `project_id`, `security_partition_id`, `document_version_id`, `valid_from_ts`, `valid_to_ts`, `primary_node_a`, `primary_node_b`.
- **Vấn đề:** Khi Retrieval thực hiện hybrid candidate retrieval có lọc theo `source_id` hoặc `document_id`, Qdrant sẽ phải quét toàn bộ collection (full scan) nếu thiếu payload index kiểu `keyword`.
- **Giải pháp:** Bổ sung `("source_id", "keyword")` và `("document_id", "keyword")` vào `PAYLOAD_INDEX_FIELDS`.

---

## 3. Bản Đặc Tả Hợp Đồng Payload & Status Cho Retrieval Song Song

Ingestion Lane đóng vai trò nhà sản xuất (Producer), cam kết duy trì chuẩn dữ liệu sau cho Retrieval Lane (Consumer):

### 3.1. Hợp đồng Payload Điểm Vector Qdrant (`search_units_{project_id}`)

```json
{
  "id": "UUIDv5 (Point ID duy nhất sinh từ collection_name và search_unit_id)",
  "payload": {
    "_sag_id": "UUIDv5 (SearchUnit.id)",
    "search_unit_id": "UUIDv5 (SearchUnit.id)",
    "tenant_id": "string",
    "project_id": "string",
    "source_id": "string (khớp với Document.source_id)",
    "document_id": "string (khớp với Document.id)",
    "document_version_id": "string (khớp với DocumentVersion.id)",
    "version_no": 1,
    "security_partition_id": "string",
    "valid_from": "2026-10-02T00:00:00Z",
    "valid_to": "9999-12-31T23:59:59Z",
    "valid_from_ts": 1790899200,
    "valid_to_ts": 253402300799,
    "token_count": 142,
    "page_from": 1,
    "page_to": 2,
    "section_path": "Chương 1 > Kiến trúc hệ thống",
    "block_from_id": "UUIDv5 (CanonicalBlock bắt đầu)",
    "block_to_id": "UUIDv5 (CanonicalBlock kết thúc)",
    "source_anchor": "block-0",
    "content_hash": "sha256_hash",
    "content": "Nội dung văn bản chuẩn tắc...",
    "primary_node_a": null,
    "secondary_node_ids_a": [],
    "tree_version_a": null,
    "primary_node_b": null,
    "secondary_node_ids_b": [],
    "tree_version_b": null
  },
  "vector": {
    "content_vector": [0.012, -0.045, "... 1536 chiều ..."],
    "bm25_sparse": {
      "indices": [1042, 5821, 99401],
      "values": [1.42, 0.85, 2.11]
    }
  }
}
```

### 3.2. Hợp đồng Truy Vấn Truy Nguyên Tuyệt Đối (Exact Evidence Locator Contract)

Căn cứ vào `resolve_exact_evidence_locators` trong [`evidence_service.py`](../../apps/api/sag_api/services/evidence_service.py#L157-L184), Ingestion Lane bảo đảm các điều kiện sau luôn được thỏa mãn trong PostgreSQL:
1. `SearchUnit.id == chunk_id`.
2. `SearchUnit.document_version_id == DocumentVersion.id`.
3. `DocumentVersion.document_id == Document.id`.
4. `SearchUnit.block_from_id == CanonicalBlock.id`.
5. `Document.status == DocumentStatus.READY`.
6. `Document.is_active == True`.
7. `DocumentVersion.search_status == "SEARCH_READY"`.
8. `DocumentVersion.search_ready_at IS NOT NULL`.
9. `CanonicalBlock.source_anchor` là chuỗi không rỗng (`len(anchor.strip()) > 0`).
10. `SearchUnit.page_from >= 1` và `SearchUnit.page_to >= SearchUnit.page_from`.

---

## 4. Kiến Trúc Khắc Phục Thảm Họa & Tính Lũy Đẳng (SSOT & Idempotency)

1. **PostgreSQL là Single Source of Truth:**
   - Mọi khối văn bản gốc lưu tại `canonical_blocks`.
   - Mọi phân đoạn tìm kiếm, hash, và token lưu tại `search_units`.
   - Hàm `rebuild_search_index_for_project` có thể tái tạo 100% Qdrant points mà không cần tệp tải lên gốc.
2. **Idempotent Retry:**
   - Point ID trong Qdrant sinh bằng UUIDv5 cố định (`generate_search_unit_point_id`).
   - Trước khi upsert mới, luôn thực hiện lệnh xóa theo filter `document_version_id` trên cả PostgreSQL và Qdrant (`wait=true`).
   - Reprocess cùng một version không sinh thêm vector rác hoặc nhân bản SearchUnit.

---

## 5. Kết Luận
Báo cáo nghiên cứu đã bóc tách chính xác toàn bộ 8 điểm nghẽn và rủi ro trong codebase hiện tại. Kế hoạch hành động cụ thể đã được hiệu chỉnh tương ứng trong [`checkpoint-a-ingestion-plan.md`](checkpoint-a-ingestion-plan.md) để làm chuẩn mực tuyệt đối trước khi coding.
