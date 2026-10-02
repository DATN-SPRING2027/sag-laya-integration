# Kế Hoạch Triển Khai Task: Kiểm Tra Giới Hạn Và Loại File Đầu Vào (Ingress File Validation Gatekeeper)

---

## 1. Thông Tin Chung Và Định Vị Task (Metadata & Task Mapping)

* **Tên công việc:** Kiểm tra giới hạn/loại file đầu vào và từ chối file không hợp lệ trước khi tạo pipeline job.
* **Người phụ trách (Owner):** Phan Tài (phan tai).
* **Triết lý thực thi (Ponytail):** Tối giản, thực dụng, không over-engineering. Ưu tiên Python Standard Library, tái sử dụng tối đa hàm sẵn có, diff ngắn gọn và hiệu quả cao nhất.
* **Định vị trong tài liệu chuẩn:**
  * **[tasks/todo.md](../../tasks/todo.md#L16):** Phase 1 — Upload & Versioned Source:
    > `- [x] Giữ validation quyền, extension, MIME signature, size và policy trước khi xử lý file.`
  * **[tasks/plan.md](../../tasks/plan.md#L262):** Phase 1 — Upload & Versioned Source:
    > `1. Giữ validation quyền, extension, MIME signature, size và policy trước xử lý nặng.`
    > Cổng ra: `... upload sai bị từ chối trước pipeline; ...`
* **Tài liệu nghiên cứu cơ sở:** [research.md](research.md).

---

## 2. Phạm Vi Công Việc Và Ranh Giới Kỹ Thuật (Scope & Boundaries)

### 2.1. Trong Phạm Vi (In-Scope)
1. **Trọng tâm điểm cuối (Endpoint Focus):** Tập trung tuyệt đối vào **Cổng Mới chuẩn thức** [`POST /api/v1/projects/{project_id}/documents/upload`](../../apps/api/sag_api/api/v1/documents.py#L453). Cổng cũ chỉ cần duy trì lớp guard cơ bản chống crash.
2. **Bảo toàn ranh giới quyền (Auth Scope Boundary — HTTP 401/403):** Duy trì và kiểm chứng 4 lớp bảo vệ: Token qua `VerifiedPrincipal`, quyền dự án (`has_project_access`), phân vùng bảo mật (`has_partition_access`), và chống mạo danh User (`X-Continuum-User-Id`).
3. **Bảo vệ chống tràn RAM (Bounded Chunk Streaming — HTTP 422):** Sử dụng hàm [`_read_upload_file_bounded`](../../apps/api/sag_api/api/v1/documents.py#L85) đọc theo từng khối 64KB, hủy luồng ngay khi tệp rỗng hoặc vượt trần `max_upload_size_bytes` / `settings.max_upload_mb`.
4. **Xác thực chữ ký nhị phân tối giản (Pure Stdlib Magic Bytes — HTTP 422):**
   * Tái sử dụng bảng 4 nhánh sẵn có trong [`_verify_mime_signature`](../../apps/api/sag_api/api/v1/documents.py#L110) (đã bao phủ 15 định dạng trong `settings.allowed_upload_exts`).
   * Bổ sung kiểm tra quét phát hiện null-byte (`\x00`) trên toàn bộ payload (`b"\x00" in file_bytes`) đối với nhóm văn bản thuần (`.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.html`).
   * Không phát sinh parser phân tích cú pháp suy diễn (không parse đóng mở thẻ HTML hay dấu ngoặc JSON tại cổng Ingress).
5. **Ranh giới lưu tệp (File Storage Boundary):**
   * Tái sử dụng luồng lưu ảnh chụp bất biến [`_save_snapshot_file`](../../apps/api/sag_api/services/document_service.py#L846) sẵn có cho tệp hợp lệ.
   * **Nguyên tắc cốt tử:** Tệp sai/không hợp lệ phải bị từ chối **TRƯỚC KHI** gọi `_save_snapshot_file` (Fail-Fast: không ghi một byte nào xuống đĩa, không tạo `Document` hay `Job` trong cơ sở dữ liệu).
6. **Chuẩn hóa tầng dịch vụ (`document_service.py`):** Tinh gọn hàm `_check_upload_file`, xóa bỏ ký tự phân cách tiếng Trung `"、"`, tránh nhân đôi logic kiểm tra đã được thực hiện ở tầng API endpoint.

### 2.2. Ngoài Phạm Vi (Out-of-Scope)
* **Không làm trích xuất hay OCR tài liệu:** Việc sử dụng **MinerU cho Scanned PDF** và **Microsoft MarkItDown cho Office** đã có sẵn trong module `sag_api/parsing/` và thuộc về **Phase 2A (Canonical Extraction)** do Worker chạy ngầm, không chạy ở luồng HTTP upload.
* **Không làm phân tích cú pháp nội dung sâu (Deep Content Parsing):** Khâu Ingress không giải mã toàn bộ bảng mã UTF-8 hay kiểm tra cấu trúc cú pháp JSON/HTML (nhiệm vụ này thuộc về các parser chuyên biệt ở Phase 2A).
* **Không làm hỗ trợ định dạng bảng kiểm kê (DATN-25):** Thuộc Phase 2A.
* **Không can thiệp luồng khử trùng lặp (Dedup) và thời gian (Temporal):** Thuộc Phase 2B.
* **Không can thiệp luồng chỉ mục (Search Index) và Qdrant:** Thuộc Phase 2C.
* **Không sửa đổi cơ chế truy vấn/tìm kiếm (Query/Retrieval) của KeyT.**
* **Không thay đổi cấu trúc bảng cơ sở dữ liệu (schema) hoặc chia sẻ cấu hình chung nếu chưa thống nhất.**

---

## 3. Các Tệp Mã Nguồn Tác Động & Kiểm Chứng (Target Files)

| Phân Loại | Đường Dẫn Tệp | Vai Trò & Trách Nhiệm Kỹ Thuật |
| :--- | :--- | :--- |
| **Điểm cuối API** | [apps/api/sag_api/api/v1/documents.py](../../apps/api/sag_api/api/v1/documents.py) | Bổ sung kiểm tra null-byte (`b"\x00" in file_bytes`) vào nhánh văn bản thuần của `_verify_mime_signature` và áp dụng cho cả legacy route. |
| **Tầng dịch vụ** | [apps/api/sag_api/services/document_service.py](../../apps/api/sag_api/services/document_service.py) | Chuẩn hóa `_check_upload_file` (sửa dấu phân cách `"、"` thành `", "`, tránh duplicate validation). |
| **Kiểm thử tích hợp & Ingress** | [apps/api/tests/test_phase_1_upload_and_versioning.py](../../apps/api/tests/test_phase_1_upload_and_versioning.py) | Bổ sung test cases (null-byte toàn payload, fake docx, extension cấm, legacy route); xác minh toàn bộ 34 tests pass 100%. |
| **Tài liệu theo dõi** | [tasks/todo.md](../../tasks/todo.md) | Cập nhật kết quả nghiệm thu và ghi nhận gap thực tế vào mục tương ứng của Phase 1. |

---

## 4. Kế Hoạch Triển Khai Tinh Gọn (Ponytail Work Packages)

```text
+---------------------------------------------------------------------------------------+
|                      LỘ TRÌNH TRIỂN KHAI THEO 4 GÓI CÔNG VIỆC                         |
+---------------------------------------------------------------------------------------+
| [WP1] Thắt Chặt Quét Null-Byte Cho Nhánh Văn Bản Thuần                                |
|       ===> Thêm kiểm tra b"\x00" in file_bytes trong _verify_mime_signature           |
|                                           |                                           |
|                                           v                                           |
| [WP2] Chuẩn Hóa Tầng Dịch Vụ document_service                                         |
|       ===> Sửa dấu phẩy "、" thành ", " trong _check_upload_file, tránh lặp validation|
|                                           |                                           |
|                                           v                                           |
| [WP3] Bổ Sung Các Test Case Còn Thiếu Trong test_phase_1_upload_and_versioning.py     |
|       ===> Tái sử dụng test có sẵn; bổ sung test đuôi cấm, fake DOCX, và null-byte    |
|                                           |                                           |
|                                           v                                           |
| [WP4] Kiểm Chứng Hồi Quy Suite Phase 1 & Nghiệm Thu                                   |
|       ===> Đảm bảo toàn bộ suite (34 tests) pass 100%                                 |
+---------------------------------------------------------------------------------------+
```

### Gói Công Việc 1 (WP1): Thắt Chặt Quét Null-Byte Cho Nhánh Văn Bản Thuần
* **Mục tiêu:** Giữ nguyên cấu trúc 4 nhánh định dạng hiện có của `_verify_mime_signature`, chỉ bổ sung phát hiện ô nhiễm nhị phân bằng thư viện chuẩn (stdlib slicing).
* **Nhiệm vụ cụ thể:**
  1. Giữ nguyên kiểm tra cho PDF (`%PDF-`), ZIP/Office/EPUB (`PK\x03\x04`), XLS (OLE2).
  2. Bổ sung điều kiện `b"\x00" in file_bytes` vào nhánh tệp văn bản thuần (`.txt`, `.md`, `.markdown`, `.text`, `.csv`, `.tsv`, `.json`, `.html`, `.htm`).
  3. Chuẩn hóa mã lỗi ném ra: `ValidationError` kèm `layer=ErrorLayer.CLIENT`, `stage=ErrorStage.UPLOAD`, HTTP 422.

### Gói Công Việc 2 (WP2): Chuẩn Hóa Tầng Dịch Vụ
* **Mục tiêu:** Dọn dẹp nợ kỹ thuật nhỏ tại `document_service._check_upload_file`.
* **Nhiệm vụ cụ thể:**
  1. Sửa ký tự phân cách tiếng Trung `"、"` thành `", "` trong thông báo lỗi danh sách phần mở rộng hợp lệ.
  2. Giữ nguyên luồng gọi `_save_snapshot_file` đối với tệp hợp lệ để tạo `DocumentVersion` và kích hoạt `IngestionRun`.

### Gói Công Việc 3 (WP3): Bổ Sung Các Test Case Còn Thiếu
* **Mục tiêu:** Hoàn thiện ma trận kiểm thử tại đúng vị trí [test_phase_1_upload_and_versioning.py](../../apps/api/tests/test_phase_1_upload_and_versioning.py), kế thừa các test case đã có.
* **Nhiệm vụ cụ thể:**
  1. Tận dụng 4 test case đã pass sẵn: Empty file (`L210`), Oversized file (`L227`), Fake PDF (`L721`), Valid upload (`L252`).
  2. Bổ sung test case từ chối phần mở rộng không được phép (`.exe`, `.sh`) $\rightarrow$ 422.
  3. Bổ sung test case từ chối tệp văn bản chứa null-byte `\x00` (kể cả sau 8KB) $\rightarrow$ 422.
  4. Bổ sung test case từ chối tệp Word/DOCX giả mạo (thiếu chữ ký `PK`) $\rightarrow$ 422.
  5. Bổ sung test case cổng legacy `/sources/{source_id}/documents` cũng chặn MIME không hợp lệ $\rightarrow$ 422.

### Gói Công Việc 4 (WP4): Kiểm Chứng Hồi Quy & Cập Nhật Nghiệm Thu
* **Mục tiêu:** Đảm bảo không phát sinh hồi quy trên toàn bộ hệ thống Phase 1.
* **Nhiệm vụ cụ thể:**
  1. Chạy toàn bộ test suite `test_phase_1_upload_and_versioning.py` (khẳng định 34 test đều pass 100%).
  2. Cập nhật ghi chú tiến độ trong [tasks/todo.md](../../tasks/todo.md) mục Phase 1.

---

## 5. Ma Trận Kịch Bản Kiểm Thử (Test Verification Matrix)

| Mã Ca Kiểm Thử | Tên Kịch Bản | Đầu Vào Thử Nghiệm | Kỳ Vọng Kết Quả | Trạng Thái Nghiệm Thu |
| :--- | :--- | :--- | :--- | :--- |
| **TC-VAL-01** | Tệp rỗng (0 bytes) | `empty.txt` (0 bytes) | Bị từ chối với 422; không tạo Snapshot/Job. | **Passed** (`test_upload_empty_file_fails_with_validation_error`) |
| **TC-VAL-02** | Tệp vượt ngưỡng kích thước | `large.pdf` (vượt `max_upload_mb`) | Stream bị hủy ngay lập tức; ném 422. | **Passed** (`test_upload_oversized_file_fails_with_validation_error`) |
| **TC-VAL-03** | Phần mở rộng không được phép | `script.sh`, `app.exe` | Bị từ chối với 422 kèm danh sách đuôi hợp lệ. | **Passed** (`test_upload_disallowed_extension_rejected`) |
| **TC-VAL-04** | Giả mạo PDF bằng binary PE | `fake.pdf` (nội dung plain text / `MZ`) | Bị từ chối 422 do thiếu chữ ký `%PDF-`. | **Passed** (`test_mime_signature_mismatch_rejected`) |
| **TC-VAL-05** | Giả mạo DOCX/XLSX bằng text thô | `report.docx` (text thuần thiếu `PK`) | Bị từ chối 422 do thiếu chữ ký container ZIP. | **Passed** (`test_mime_signature_docx_without_zip_rejected`) |
| **TC-VAL-06** | Giả mạo Text bằng ảnh/binary | `data.txt` (bắt đầu bằng `\x89PNG...`) | Bị từ chối 422 do phát hiện header nhị phân. | **Passed** (`test_mime_signature_text_with_png_image_rejected`) |
| **TC-VAL-07** | Tệp văn bản chứa null-byte | `notes.md` (chứa byte `\x00` đầu hoặc sâu sau 8KB) | Bị từ chối 422 do phát hiện null-byte ô nhiễm. | **Passed** (`test_mime_signature_text_with_null_byte_rejected`, `test_mime_signature_text_with_null_byte_after_8kb_rejected`) |
| **TC-VAL-08** | Tệp hợp lệ chuẩn thức | `sample.pdf` chuẩn, `doc.docx` chuẩn | Chấp nhận 201, tạo snapshot, kích hoạt run. | **Passed** (`test_upload_fresh_document_succeeds_and_creates_records`) |

---

## 6. Tiêu Chí Hoàn Thành (Definition of Done - DoD)

1. **Chặn Đứng Tệp Sai Trước Pipeline:** 100% tệp sai kích thước, sai phần mở rộng, hoặc giả mạo chữ ký nhị phân bị từ chối với mã lỗi 422 trước khi lưu tệp hoặc tạo Job.
2. **Không Rác Ổ Đĩa & Cơ Sở Dữ Liệu:** Tệp bị từ chối không để lại bất kỳ dữ liệu nào trong thư mục `snapshots/` hoặc các bảng `Document`, `SourceSnapshot`, `Job` (đã kiểm chứng tự động qua `test_rejected_upload_leaves_no_disk_or_db_garbage`).
3. **Bảo Tồn Ranh Giới Quyền & Luồng Hợp Lệ:** Kiểm soát quyền (Token 401, Project 403, Partition 403) tại Cổng Mới tiếp tục vận hành chuẩn xác; tệp hợp lệ được lưu snapshot và đẩy Job cho downstream bình thường.
4. **Bộ Kiểm Thử Đạt Chuẩn:** Toàn bộ 34 bài kiểm thử của `test_phase_1_upload_and_versioning.py` đạt kết quả 34/34 (100% Passed).
5. **Cập Nhật Tiến Độ Minh Bạch:** Tài liệu [tasks/todo.md](../../tasks/todo.md) phản ánh trung thực kết quả kiểm chứng trong Phase 1 (34/34 passed).
