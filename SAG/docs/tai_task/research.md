# Nghiên Cứu Chuyên Sâu: Thấu Hiểu Hệ Thống Và Kiến Trúc Cổng Kiểm Soát Tiếp Nhận Dữ Liệu (Ingress Gatekeeper) Trong SAG Knowledge Routing RAG

## Tóm Tắt Khoa Học (Executive Abstract)

Trong các hệ sinh thái Retrieval-Augmented Generation (RAG) quy mô doanh nghiệp, độ tin cậy và sự an toàn của toàn bộ đường ống dẫn dữ liệu (data ingestion pipeline) phụ thuộc hoàn toàn vào cửa ngõ tiếp nhận ban đầu (ingress layer). Một lỗ hổng kiểm soát quyền sẽ dẫn đến rò rỉ dữ liệu chéo giữa các tổ chức (cross-tenant leakage) hoặc vi phạm phân vùng bảo mật; trong khi một lỗ hổng thẩm định tệp sẽ tạo điều kiện cho các tệp độc hại hoặc giả mạo định dạng xâm nhập, gây lãng phí nghiêm trọng tài nguyên tính toán (CPU/GPU cho OCR, parser, embedding) và làm tắc nghẽn hàng đợi tác vụ nền (`JobQueue`). 

Nghiên cứu này đặt trọng tâm hàng đầu vào việc **thấu hiểu bản chất kiến trúc của hệ thống SAG**, vận dụng triệt để nguyên lý thiết kế tối giản, thực dụng (**Ponytail Philosophy: The best code is the code never written - Không phát sinh trừu tượng dư thừa, tái sử dụng tối đa mã nguồn hiện có, ưu tiên thư viện chuẩn stdlib**). Báo cáo rà soát tường tận **những gì dự án đã có**, mổ xẻ chính xác **những lỗ hổng dự án đang thiếu**, và đề xuất chi tiết **những cấu phần kỹ thuật cần bổ sung** trên Cổng tải lên chuẩn thức (`POST /api/v1/projects/{project_id}/documents/upload`). Nghiên cứu định hình mô hình Cổng Kiểm Soát Nhập Liệu Phòng Vệ Hai Trụ Cột (Dual-Pillar Ingress Gatekeeper), kết hợp song hành giữa **Kiểm soát quyền truy cập nguồn (Identity & Scope Authorization)** và **Kiểm định thực thể tệp (Payload & Magic Bytes Validation)**, làm tiền đề vững chắc cho việc thiết lập kế hoạch triển khai (plan) và nghiệm thu thực tế.

---

## 1. Thấu Hiểu Dự Án & Bối Cảnh Hệ Thống (Project Context & System Understanding)

### 1.1. Sứ Mệnh Và Kiến Trúc Tổng Thể Của SAG Knowledge Routing RAG
Hệ thống SAG (Semantic Aggregation & Generation) là nền tảng cốt lõi của đồ án tốt nghiệp, giải quyết bài toán truy xuất tri thức ngữ nghĩa thông minh và định tuyến câu hỏi dựa trên Cây Tri Thức (Knowledge Routing Tree). Quy trình nghiệp vụ của SAG được chia thành 12 giai đoạn chiến lược (từ Phase 0 đến Phase 11) được quy định chặt chẽ trong [SAG_Knowledge_Routing_RAG_Workflow_v1.1.md](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md) và [tasks/plan.md](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/tasks/plan.md):

* **Phase 0 — Contracts & Foundations:** Thiết lập các hợp đồng dữ liệu nền tảng, định danh ổn định (stable IDs), cơ chế khóa phân tán chống trùng lặp (`Idempotency-Key`), và ranh giới phân vùng bảo mật (`X-Continuum-Security-Partition`).
* **Phase 1 — Upload & Versioned Source (Phạm Vi Của Task):** Cửa ngõ tiếp nhận tệp, thẩm định quyền và tính hợp lệ của tệp, lưu trữ bất biến (content-addressed snapshot), ghi nhận phiên bản (`DocumentVersion`) và khởi tạo tiến trình (`IngestionRun`).
* **Phase 2A, 2B, 2C — Ingestion Pipeline:** Trích xuất khối chuẩn hóa (Canonical Extraction qua MinerU/MarkItDown), khử trùng lặp và tính toán thời gian (Dedup & Temporal), xây dựng chỉ mục vector (Search Indexing trên Qdrant).
* **Phase 3 & Phase 4 — Query Analyzer & Retrieval Engine:** Phân tích ý định người dùng (Laya Intent), định tuyến và hợp nhất kết quả tìm kiếm đa nguồn (RRF Fusion) có áp dụng kiểm soát quyền (Retrieval ACL Scope).

```text
+-------------------------------------------------------------------------------+
|                 Client Application / Continuum Gateway                        |
|             (POST /api/v1/projects/{project_id}/documents/upload)             |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|                     PHASE 1: INGRESS GATEKEEPER BOUNDARY                      |
|                                                                               |
|   [TRỤ CỘT 1: Quyền Truy Cập (Auth Scope)]                                    |
|   - Xác thực JWT Token qua VerifiedPrincipal                                  |
|   - Project Membership Boundary (has_project_access)                          |
|   - Phân vùng bảo mật Data Isolation (has_partition_access)                   |
|   - Chống mạo danh (Anti-impersonation: X-Continuum-User-Id)                  |
|   - Cách ly người thuê (Tenant Isolation: X-Continuum-Tenant-Id)              |
|   ===> Ném HTTP 401 / 403 Forbidden (Dừng ngay nếu vi phạm quyền)            |
|                                       |                                       |
|                                       v (Nếu có quyền hợp lệ)                 |
|   [TRỤ CỘT 2: Kiểm Định Tệp (Payload & Magic Bytes Gatekeeper)]               |
|   - Phần mở rộng trong 15 định dạng cấu hình settings.allowed_upload_exts     |
|   - Đọc luồng phân đoạn 64KB (Bounded Stream) chống tràn RAM                  |
|   - Thẩm định chữ ký nhị phân Magic Bytes (%PDF-, PK.., OLE2..)               |
|   - Quét phát hiện null-byte (\x00) & ô nhiễm nhị phân trong tệp văn bản     |
|   ===> Ném HTTP 422 Unprocessable Entity (Dừng ngay nếu tệp sai/độc hại)     |
+---------------------------------------+---------------------------------------+
                                        | (Vượt qua cả 2 trụ cột)
                                        v
+-------------------------------------------------------------------------------+
|                    STORAGE & TRANSACTION LAYER                                |
|   - Lưu trữ bất biến SourceSnapshot (content-addressed theo SHA-256)          |
|   - Tạo Document, DocumentVersion, IngestionRun trong Transaction             |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|              DOWNSTREAM PIPELINE (Strictly Excluded from Task)                |
|   - JobQueue Worker Pool dispatch                                             |
|   - Phase 2A: MinerU (Scanned PDF OCR) / MarkItDown Canonical Parsing         |
|   - Phase 2B: Content Dedup & Temporal Lineage                                |
|   - Phase 2C: Qdrant Dense & Sparse Vector Indexing                           |
+-------------------------------------------------------------------------------+
```

### 1.2. Phân Định Ranh Giới Nghiệp Vụ Cốt Lõi (Core Boundary Separation)
Để định hướng công việc chính xác và không bị phân tán nguồn lực, ba ranh giới sau được xác lập dứt khoát:

1. **Chuẩn Hóa Điểm Cuối (Endpoint Choice): Cổng Mới Là Trọng Tâm Duy Nhất:**
   Cổng mới [`POST /api/v1/projects/{project_id}/documents/upload`](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/api/v1/documents.py#L453) là điểm cuối chuẩn thức phục vụ kiến trúc Knowledge Routing RAG (Phase 1). Cổng cũ (`/sources/{source_id}/documents`) là di sản single-user cũ, chỉ cần được gắn guard cơ bản chống crash, không đầu tư kiến trúc vào cổng cũ.
2. **Xử Lý Scanned PDF Bằng MinerU & MarkItDown: Thuộc Về Phase 2A (Hạ Nguồn):**
   * Hệ thống **đã tích hợp đầy đủ** Microsoft MarkItDown (`markitdown.MarkItDown`) và MinerU (`MinerUClient`) trong module [sag_api/parsing/](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/parsing).
   * Cơ chế đã định tuyến rõ: Tệp `.pdf` được ưu tiên xử lý qua **MinerU** để thực hiện bóc tách bố cục tinh tế, trích xuất bảng biểu và OCR scanned PDF; nếu MinerU gặp sự cố sẽ fallback về MarkItDown.
   * **Ranh giới:** Tác vụ OCR nặng này được thực thi ngầm bởi Worker trong **Phase 2A (Canonical Extraction)**. Khâu tiếp nhận (Phase 1) **tuyệt đối không parse tệp PDF trực tiếp trong luồng HTTP upload** để tránh gây nghẽn kết nối và quá thời gian chờ (timeout) của client.
3. **Ranh Giới Lưu Tệp (File Storage Boundary):**
   * Hệ thống **đã có sẵn** cơ chế lưu trữ ảnh chụp bất biến theo mã băm SHA-256 ([_save_snapshot_file](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/document_service.py#L846)) tại thư mục `.data/engine/snapshots/{hash[:2]}/{hash}/{filename}`.
   * Task này **kế thừa 100% luồng lưu trữ snapshot hiện hữu**, không xây dựng hệ thống lưu trữ mới. Trọng tâm của task là: **Tệp không hợp lệ phải bị từ chối NGAY TRƯỚC KHI lưu tệp (Fail-Fast Memory Abort), không được ghi bất kỳ byte rác nào xuống đĩa hay cơ sở dữ liệu.**

---

## 2. Khảo Sát Hiện Trạng: Dự Án Đang Có Gì? (Current Implementation Audit)

Khảo sát mã nguồn thực tế tại [documents.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/api/v1/documents.py), [document_service.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/document_service.py), và [parsing/service.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/parsing/service.py) cho thấy dự án đã sở hữu các khối chức năng nền tảng:

* **Tầng xác thực quyền tại Cổng Dự Án:** [documents.py:L472-L512](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/api/v1/documents.py#L472-L512) đã có xác thực JWT Token qua `VerifiedPrincipal`, kiểm tra Project, kiểm tra Security Partition (`X-Continuum-Security-Partition`), kiểm tra chống mạo danh User, và kiểm tra Tenant.
* **Đọc luồng giới hạn dung lượng:** Hàm `_read_upload_file_bounded` đã đọc theo từng khối 64KB, hủy luồng và ném `ValidationError (422)` nếu tệp rỗng hoặc vượt trần kích thước cấu hình.
* **Xác thực chữ ký nhị phân sơ bộ:** Hàm `_verify_mime_signature` đã có kiểm tra magic bytes cho `.pdf` (`%PDF-`), OpenXML (`PK\x03\x04`), `.xls` (OLE2), và cấm 4 tiền tố binary (`MZ`, `ELF`, `PNG`, `JPEG`) đối với text.
* **Danh mục định dạng cấu hình:** [config.py:L86-L102](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/core/config.py#L86-L102) quy định rõ danh sách trắng 15 định dạng: `.md`, `.markdown`, `.txt`, `.text`, `.pdf`, `.docx`, `.pptx`, `.xls`, `.xlsx`, `.csv`, `.tsv`, `.html`, `.htm`, `.json`, `.epub`.
* **Bộ trích xuất hạ nguồn (Phase 2A):** Module [sag_api/parsing/](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/parsing) đã tích hợp sẵn **MinerU** cho PDF và **Microsoft MarkItDown** cho các định dạng văn phòng/văn bản.

---

## 3. Phân Tích Khoảng Trống: Dự Án Đang Thiếu Gì? (Gap Analysis)

Dựa trên tiêu chuẩn tối giản và hiệu quả cao, hệ thống hiện còn tồn tại **4 khoảng trống kỹ thuật thực tế** cần khắc phục:

```text
+---------------------------------------------------------------------------------------+
|                              4 KHOẢNG TRỐNG KỸ THUẬT                                  |
+-------------------------------------------------------------+-------------------------+
| [G1] Bảng Magic Bytes chưa bao phủ 15 định dạng cấu hình    | ==> Tệp sai định dạng   |
|      - Thiếu kiểm tra CSV, TSV, JSON, HTML, EPUB            |     lọt vào Ingestion   |
+-------------------------------------------------------------+-------------------------+
| [G2] Thiếu cơ chế quét Null-Byte (\x00) trong tệp văn bản   | ==> Tệp nhị phân trá    |
|      - Chỉ cấm 4 tiền tố header, dễ lọt mã nhị phân rác     |     hình gây lỗi parser |
+-------------------------------------------------------------+-------------------------+
| [G3] Phân mảnh logic thẩm định giữa API và DocumentService  | ==> Bất nhất thông báo  |
|      - document_service._check_upload_file dùng dấu "、"     |     lỗi & taxonomy      |
+-------------------------------------------------------------+-------------------------+
| [G4] Thiếu Test Suite chuyên biệt cho Ingress Gatekeeper    | ==> Chưa có kiểm chứng  |
|      - test_document_parsing.py chỉ test sau khi đã parse   |     tự động từ chối sớm |
+-------------------------------------------------------------+-------------------------+
```

1. **Khoảng Trống 1: Bảng chữ ký nhị phân chưa bao phủ 10 định dạng còn lại:** Hàm `_verify_mime_signature` mới chỉ kiểm tra 5 định dạng (`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.xls`). Các định dạng như `.csv`, `.tsv`, `.json`, `.html`, `.epub` chưa có bộ quy tắc xác thực tính hợp lệ nội dung.
2. **Khoảng Trống 2: Nguy cơ tệp nhị phân trá hình tệp văn bản:** Các định dạng text (`.txt`, `.csv`, `.json`, `.md`) hiện chỉ dùng danh sách đen 4 chữ ký (`MZ`, `ELF`, `PNG`, `JPEG`). Nếu kẻ tấn công hoặc người dùng nạp tệp nhị phân khác chứa các byte không in được hoặc null-byte (`\x00`), tệp vẫn lọt qua vào đường ống trích xuất.
3. **Khoảng Trống 3: Phân mảnh tại tầng dịch vụ ([document_service.py:L831](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/document_service.py#L831)):** Hàm `_check_upload_file` định nghĩa riêng biệt không gọi MIME check, thông báo lỗi dùng dấu phẩy tiếng Trung `"、"`, và chưa gắn nhãn kiến trúc `ErrorLayer.CLIENT`, `ErrorStage.UPLOAD`.
4. **Khoảng Trống 4: Thiếu kiểm thử tự động cho khâu Ingress:** Trong tệp [test_document_parsing.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_document_parsing.py), các bài test hiện tại tập trung kiểm thử kết quả trích xuất của MinerU và MarkItDown ở hạ nguồn, chưa có các ca kiểm thử từ chối tệp độc hại/giả mạo định dạng ở cửa ngõ tiếp nhận.

---

## 4. Giải Pháp Kỹ Thuật Đề Xuất Theo Triết Lý Tối Giản (Ponytail Architecture)

Áp dụng nấc thang **Ponytail Ladder**:
* *Rung 1 (YAGNI):* Không viết thêm microservice, không thêm bảng DB mới, không thay đổi schema cơ sở dữ liệu.
* *Rung 2 (Tái sử dụng):* Tái sử dụng `_read_upload_file_bounded`, `_save_snapshot_file`, `error_taxonomy.py`.
* *Rung 3 (Stdlib):* Dùng hoàn toàn thư viện chuẩn Python (`struct`, `io`, slicing byte). Không cài thêm thư viện liên kết C nặng nề (`libmagic`).
* *Rung 4 (Diff tối thiểu):* Tập trung tinh chỉnh trực tiếp tại [documents.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/api/v1/documents.py) và [document_service.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/document_service.py).

```text
+---------------------------------------------------------------------------------------+
|                       KIẾN TRÚC THẨM ĐỊNH TỐI GIẢN (STDLIB)                           |
+---------------------------------------------------------------------------------------+
|  [Input Stream]                                                                       |
|         │                                                                             |
|         ▼                                                                             |
|  1. _check_extension(filename): Khớp phần mở rộng trong settings.allowed_upload_exts  |
|         │                                                                             |
|         ▼                                                                             |
|  2. _read_upload_file_bounded(file, max_bytes): Đọc luồng 64KB, abort nếu > limit    |
|         │                                                                             |
|         ▼                                                                             |
|  3. _verify_mime_signature(filename, file_bytes):                                     |
|     ├── PDF: file_bytes[:5] == b"%PDF-"                                               |
|     ├── Office/EPUB: file_bytes[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}   |
|     ├── XLS: file_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"                   |
|     └── Text/CSV/JSON/HTML:                                                           |
|         ├── Không chứa byte nhị phân cấm (MZ, ELF, PNG, JPG, GIF)                     |
|         └── Quét b"\x00" trong 8KB đầu tiên (Không cho phép null-byte)                |
|         │                                                                             |
|         ▼                                                                             |
|  ===> HỢP LỆ: Chuyển tiếp sang _save_snapshot_file & tạo IngestionRun                 |
|  ===> BẤT THƯỜNG: Ném ValidationError(layer=CLIENT, stage=UPLOAD, code=422)           |
+---------------------------------------------------------------------------------------+
```

### Bảng Quy Chuẩn Xác Thực 15 Định Dạng (Pure Python Stdlib)

| Nhóm Định Dạng | Đuôi Tệp | Quy Tắc Chữ Ký Nhị Phân (Magic Bytes) | Điều Kiện An Toàn Nội Dung |
| :--- | :--- | :--- | :--- |
| **PDF** | `.pdf` | `file_bytes.startswith(b"%PDF-")` | Độ dài tệp tối thiểu $\ge 8$ bytes. |
| **OpenXML / EPUB** | `.docx`, `.pptx`, `.xlsx`, `.epub` | Bắt đầu bằng chữ ký ZIP container: `b"PK\x03\x04"`, `b"PK\x05\x06"`, hoặc `b"PK\x07\x08"`. | Khước từ ZIP rỗng (dưới 30 bytes). |
| **Bảng tính cũ** | `.xls` | Bắt đầu bằng OLE2: `b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"`. | Khước từ tệp text đổi đuôi `.xls`. |
| **Văn bản thuần** | `.txt`, `.md`, `.markdown`, `.text` | Phủ định: Không chứa chữ ký `b"MZ"`, `b"\x7fELF"`, `b"\x89PNG"`, `b"\xff\xd8\xff"`. | Khẳng định: `b"\x00" not in file_bytes[:8192]` và giải mã được qua UTF-8/ASCII. |
| **Dữ liệu bảng** | `.csv`, `.tsv` | Tương tự văn bản thuần (không chứa chữ ký binary/ảnh). | Quét 8KB đầu không chứa null byte `\x00`. |
| **Dữ liệu cấu trúc** | `.json` | Không chứa chữ ký nhị phân; loại bỏ khoảng trắng đầu/cuối. | Ký tự đầu không rỗng là `{` hoặc `[` và kết thúc bằng `}` hoặc `]`. |
| **Siêu văn bản** | `.html`, `.htm` | Không chứa chữ ký nhị phân; cho phép UTF-8 BOM. | Chứa thẻ đánh dấu (`<html`, `<!doctype`, `<head`, `<body`) trong 2KB đầu. |

---

## 5. Kết Luận Khoa Học & Cầu Nối Triển Khai (Conclusion)

1. **Hiểu Đúng Dự Án:** Cổng nạp chuẩn thức là Cổng Mới (`POST /projects/{project_id}/documents/upload`). Toàn bộ cơ chế bảo vệ quyền (Trụ cột 1) và bảo vệ tệp (Trụ cột 2) tập trung tối ưu tại đây.
2. **Hiểu Đúng Ranh Giới:**
   * MinerU xử lý Scanned PDF và MarkItDown xử lý Office thuộc về **Phase 2A (Canonical Extraction)** của Worker hạ nguồn. Cổng Ingress chỉ đảm bảo dữ liệu đầu vào sạch, không parse tài liệu nặng tại cổng HTTP.
   * Khâu lưu file kế thừa hoàn toàn `_save_snapshot_file` hiện có. Trọng tâm là tệp lỗi bị từ chối trước khi lưu.
3. **Thực Thi Tinh Gọn (Ponytail):** Triển khai trực tiếp bằng thư viện chuẩn của Python, ngắn gọn, chắc chắn, không phát sinh nợ kỹ thuật hay phụ thuộc cồng kềnh.
