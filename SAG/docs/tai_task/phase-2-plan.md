# Kế Hoạch Triển Khai Chi Tiết: Phase 2 — Canonical Extraction, Dedup & Temporal, Search Index

---

## 1. Thông Tin Chung & Định Vị Kế Hoạch (Executive Overview & Task Mapping)

* **Tên kế hoạch:** Xây dựng đường ống trích xuất khối chuẩn hóa, khử trùng lặp đa tầng, mô hình thời gian và chỉ mục tìm kiếm Qdrant đạt chuẩn `SEARCH_READY`.
* **Phân đoạn kiến trúc:** Phase 2A (Canonical Extraction) $\rightarrow$ Phase 2B (Dedup & Temporal) $\rightarrow$ Phase 2C (Search Index).
* **Người phụ trách (Owner):** Phan Tài (phan tai).
* **Triết lý thực thi (Ponytail Philosophy):**
  * *Tối giản, thực dụng, kiểm thử hướng đối tượng (TDD).*
  * *PostgreSQL là nguồn chân lý duy nhất (Single Source of Truth).* Qdrant đóng vai trò chỉ mục tăng tốc và có thể tái tạo 100% từ cơ sở dữ liệu và artifact lưu trữ.
  * *Tách biệt năng lực dứt khoát:* Làn tìm kiếm deterministic đạt `SEARCH_READY` trước, không phụ thuộc vào LLM hoặc tiến trình làm giàu tri thức ngầm (`KNOWLEDGE_READY`).
* **Định vị trong tài liệu chuẩn:**
  * **[tasks/todo.md](../../tasks/todo.md#L24-L49):** Bao phủ toàn bộ 18 tiêu chí của Phase 2A, Phase 2B và Phase 2C.
  * **[tasks/plan.md](../../tasks/plan.md#L270-L304):** Bám sát các phụ thuộc, yêu cầu kỹ thuật và cổng nghiệm thu của Phase 2.
  * **[SAG_Knowledge_Routing_RAG_Workflow_v1.1.md](../SAG_Knowledge_Routing_RAG_Workflow_v1.1.md):** Khớp với Mục 6 (Canonical Extraction), Mục 7 (Deduplication & Temporal), Mục 8 (Search Unit & Index).
  * **[phase-0-contracts-and-foundations.md](../phase-0-contracts-and-foundations.md):** Tuân thủ DDL PostgreSQL 16 (Pillar 2) và công thức sinh UUIDv5 tất định.

---

## 2. Kiến Trúc Luồng Dữ Liệu & Chuyển Đổi Trạng Thái (Data Flow Architecture)

```text
+---------------------------------------------------------------------------------------------------+
|                        PHASE 1: INGRESS & SNAPSHOT (ĐÃ NGHIỆM THU)                                |
|  - SourceSnapshot (content-addressed SHA-256) / DocumentVersion (v1, v2...)                       |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2A: CANONICAL EXTRACTION                                      |
|  [Parser Router]       ==> PDF (MinerU -> MarkItDown), Office (MarkItDown), Text/Data (Native)    |
|  [Block Extractor]     ==> Phân đoạn: heading, paragraph, table, code, list, caption              |
|  [Normalizer Engine]   ==> Unicode NFC, giữ nguyên định dạng bảng, thụt lề code & identifier      |
|  [Boilerplate Detector]==> Gắn nhãn audit header/footer lặp lại (is_boilerplate=True)            |
|  [Persistence Staging] ==> Ghi transactional vào bảng canonical_blocks (UUIDv5 block_id)          |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2B: DEDUP & TEMPORAL LINEAGE                                  |
|  [Tier 1 & 2 Dedup]    ==> File hash & Block hash; reuse content nhưng giữ riêng provenance       |
|  [Tier 3 Near-Dedup]   ==> MinHash (128 hash) / LSH; ngưỡng 0.85 (text) và 0.95 (code/bảng)       |
|  [Tier 4 Semantic Guard]==> Cosine >= 0.90 chỉ sinh candidate; CẤM auto-merge CONTRADICTS         |
|  [Relation Tagger]     ==> Gán nhãn: EQUIVALENT / SUPPORTS / CONTRADICTS / SUPERSEDES / RELATED   |
|  [Bi-temporal Lineage] ==> valid_from, valid_to, supersedes_id, phục vụ truy vấn lịch sử         |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
|                               PHASE 2C: SEARCH UNIT & QDRANT INDEXING                             |
|  [Structural Chunker]  ==> Ưu tiên boundary heading/paragraph/table trước token window            |
|  [SearchUnit Binder]   ==> Ghi bảng search_units (block_range, token_count, security_partition)   |
|  [Dual Representation] ==> Vector dày (Dense Embedding) + Vector thưa (Sparse Lexical BM25)      |
|  [Qdrant Pre-indexing] ==> Khởi tạo payload indexes cho filter fields (project, partition, time) |
|  [Stable Upsert]       ==> Nạp Qdrant qua UUIDv5 point_id, reprocess idempotent không sinh rác     |
|  [Manifest Gatekeeper] ==> Verify số lượng vector khớp 100% với search_units                     |
+-------------------------------------------------+-------------------------------------------------+
                                                  |
                                                  v (Nhất quán 100%)
                        =======================================================
                        ★  TRẠNG THÁI DOCUMENT_VERSION: SEARCH_READY        ★
                        =======================================================
                                                  │
                                                  ▼ (Chạy ngầm bất đồng bộ)
                        [Phase 5: Knowledge Extraction & Tree -> KNOWLEDGE_READY]
```

---

## 3. Bảng Ma Trận Ánh Xạ 18 Tiêu Chí Nghiệm Thu (Requirements Traceability Matrix)

| STT | Tiêu Chí Todo.md | Mục Tiêu Kỹ Thuật | Cấu Phần Thực Thi | Vị Trí Mã Nguồn Dự Kiến |
| :--- | :--- | :--- | :--- | :--- |
| **2A.1** | Xác nhận danh sách định dạng & parser/fixtures | 15 định dạng chuẩn; phân bổ đúng MinerU / MarkItDown / Native; bộ test fixtures ổn định. | Matrix Parser & Test Fixtures | `sag_api/parsing/router.py`<br>`tests/fixtures/canonical/` |
| **2A.2** | Lưu canonical block type, ordinal, page range, section path, anchor | Lưu đầy đủ vào bảng `canonical_blocks` theo DDL Phase 0 với ID sinh bằng UUIDv5. | Block Model & Persistence | `sag_api/services/canonical_service.py`<br>`sag_api/db/models/routing_rag.py` |
| **2A.3** | Kiểm tra normalization giữ bảng/code/punctuation/identifier | Chuẩn hóa Unicode NFC/NFKC; không làm gãy format markdown table, thụt dòng code; giữ identifier, URL, mã lỗi. | Normalizer Engine | `sag_api/parsing/normalizer.py` |
| **2A.4** | Xác nhận LLM không được dùng để sửa text mặc định | Tuyệt đối không gọi LLM để rewrite hoặc edit văn bản; deterministic 100%. | Strict Zero-LLM Guard | `sag_api/parsing/normalizer.py` |
| **2A.5** | Kiểm tra extraction output versioned/temp, retry và lỗi stage | Output ghi vào staging transaction trước khi commit; cập nhật `StageRun`; rollback an toàn khi lỗi. | Staging & StageRun Tracker | `sag_api/jobs/tasks.py`<br>`sag_api/services/canonical_service.py` |
| **2B.1** | Kiểm tra file exact hash & block exact hash; giữ provenance | Trùng file hash thì link snapshot; trùng block hash thì reuse content nhưng giữ riêng provenance/evidence. | Exact Dedup & Evidence Linker | `sag_api/services/dedup_service.py` |
| **2B.2** | Near-duplicate tạo candidate cluster có ngưỡng theo loại dữ liệu | MinHash (128 hash) / SimHash + Jaccard; ngưỡng 0.85 (văn bản), 0.95 (code/bảng). | Near-duplicate LSH Engine | `sag_api/services/dedup_service.py` |
| **2B.3** | Semantic similarity chỉ tạo candidate, không tự merge CONTRADICTS | Cosine $\ge 0.90$ chỉ sinh liên kết candidate; cấm tự động gộp các câu phủ định/mâu thuẫn. | Semantic Candidate Guard | `sag_api/services/dedup_service.py` |
| **2B.4** | Kiểm tra EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED | Phân loại chính xác 5 quan hệ ngữ nghĩa; ghi nhận vào evidence mapping. | Relation Classification Model | `sag_api/services/dedup_service.py` |
| **2B.5** | Published/observed/ingested time, validity, supersedes lineage | Ghi nhận bi-temporal model; `valid_from` đến `valid_to`; liên kết `supersedes_id` chuỗi phiên bản. | Temporal Lineage Manager | `sag_api/services/temporal_service.py` |
| **2B.6** | Reprocess deterministic, retry an toàn, không mất lịch sử | Reprocess sinh cùng ID và hash; không xóa các version lịch sử; cập nhật hiệu lực `valid_to`. | Deterministic Reprocess | `sag_api/services/temporal_service.py` |
| **2C.1** | Search Unit boundary theo heading/paragraph/table trước token window | Tôn trọng cấu trúc văn bản; không cắt ngang giữa bảng hoặc đoạn code ngắn; áp token window sau. | Structural Chunker | `sag_api/services/search_unit_service.py` |
| **2C.2** | Giữ doc/version, block range, hash, page, section, security partition | Lưu đầy đủ thuộc tính vào bảng `search_units`; bắt buộc có `security_partition_id`. | SearchUnit Model Binder | `sag_api/services/search_unit_service.py` |
| **2C.3** | Dense + sparse representation; tách riêng Search Unit & Knowledge Unit | Vector dày (dense embedding) + vector thưa (BM25 lexical); không nhầm lẫn với Knowledge Unit. | Dual Embedding & Indexing | `sag_api/services/search_index_service.py` |
| **2C.4** | Tạo Qdrant payload indexes cho filter fields trước ingestion | Khởi tạo payload index trên Qdrant (`project_id`, `security_partition_id`, `valid_from`, v.v.). | Qdrant Schema & Index Init | `sag_api/sag/qdrant_store.py` |
| **2C.5** | Upsert bằng stable point ID; reprocess không tạo point trùng | Point ID sinh qua UUIDv5 theo Search Unit ID; upsert idempotent, không sinh rác vector cũ. | Stable Point ID Upserter | `sag_api/services/search_index_service.py` |
| **2C.6** | Lưu/verify index manifest; chỉ bật search readiness sau nhất quán | Kiểm đếm vector count, checksum giữa PG và Qdrant; chỉ set `SEARCH_READY` khi khớp 100%. | Manifest Gatekeeper | `sag_api/services/search_index_service.py` |
| **2C.7** | Xác nhận Qdrant có thể rebuild từ PostgreSQL/source artifacts | Cung cấp hàm/CLI rebuild toàn bộ vector Qdrant từ bảng `search_units` trong PostgreSQL. | Qdrant Rebuild Service | `sag_api/services/rebuild_service.py` |

---

## 4. Kế Hoạch Chi Tiết Từng Phân Đoạn (Technical Specifications)

### 4.1. Phase 2A — Canonical Extraction

#### 4.1.1. Ma Trận Bộ Phân Tích (Parser Matrix) & Test Fixtures
Hệ thống hỗ trợ chính xác 15 định dạng trong `settings.allowed_upload_exts`:
1. **Nhóm PDF (`.pdf`):**
   * *Ưu tiên:* `MinerUClient` xử lý nhận diện bố cục, trích xuất bảng 2 chiều, công thức và OCR scanned PDF.
   * *Dự phòng (Fallback):* `markitdown.MarkItDown` khi MinerU không khả dụng hoặc lỗi cấu hình.
2. **Nhóm Văn Phòng (`.docx`, `.pptx`, `.xlsx`, `.xls`):**
   * Sử dụng `markitdown.MarkItDown` cục bộ, trích xuất cấu trúc slide, văn bản và bảng biểu nguyên bản.
3. **Nhóm Văn Bản & Dữ Liệu (`.txt`, `.text`, `.md`, `.markdown`, `.csv`, `.tsv`, `.json`, `.html`, `.htm`, `.epub`):**
   * Dùng parser chuyên biệt hoặc MarkItDown, bảo toàn ký tự phân cách cột (CSV/TSV), cây cấu trúc JSON và cấu trúc DOM HTML.
4. **Bộ Test Fixtures:** Tạo thư mục `apps/api/tests/fixtures/canonical/` chứa tệp mẫu đại diện cho cả 15 định dạng, bao gồm: bảng phức tạp, khối mã lệnh, tiêu đề đa cấp, và văn bản chứa header/footer lặp lại.

#### 4.1.2. Mô Hình Dữ Liệu `CanonicalBlock` & Định Danh Tất Định
Bảng `canonical_blocks` trong cơ sở dữ liệu lưu trữ các khối văn bản chuẩn hóa. Mọi khối văn bản phải có ID sinh theo công thức tất định:
$$\text{block\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:block:\{version\_id\}:\{ordinal\}:\{content\_hash\}"})$$
* **Thuộc tính bắt buộc:**
  * `document_version_id`: Khóa ngoại trỏ về `document_versions.id`.
  * `ordinal`: Thứ tự xuất hiện tuyến tính trong tài liệu ($0, 1, 2, \dots$).
  * `block_type`: Thuộc tập hợp `{'heading', 'paragraph', 'table', 'code', 'list', 'caption'}`.
  * `page_from`, `page_to`: Vị trí trang gốc (1-based). Đối với tài liệu không phân trang (như Markdown/HTML), mặc định là 1.
  * `section_path`: Chuỗi phân cấp ngữ cảnh (ví dụ: `"Chương 1 > 1.1 Cơ sở lý thuyết > Bảng dữ liệu"`).
  * `source_anchor`: Định danh neo nguồn (ví dụ: `#sec-1-1`, `page-3-tbl-1`).
  * `normalized_text`: Nội dung văn bản sau chuẩn hóa.
  * `content_hash`: SHA-256 của `normalized_text`.

#### 4.1.3. Quy Tắc Chuẩn Hóa Văn Bản (Normalization Rules)
* **Unicode Normalization:** Sử dụng `unicodedata.normalize('NFC', text)` để chuẩn hóa bảng mã tiếng Việt dựng sẵn và các ký tự đặc thù.
* **Bảo tồn cấu trúc bảng:** Tuyệt đối không xóa bỏ các ký tự định dạng cột như dấu gạch đứng `|`, dấu gạch ngang phân cách `-|-` trong Markdown Table.
* **Bảo tồn khối mã lệnh (Code Blocks):** Giữ nguyên thụt đầu dòng (indentation 2 spaces / 4 spaces), ký tự ngoặc nhọn, dấu chấm phẩy, không gom nhiều dòng code thành một dòng.
* **Bảo toàn định danh & dấu câu:** Không xóa các ký tự đặc biệt trong mã định danh như `snake_case`, `camelCase`, URL, UUID, đường dẫn tệp, mã lỗi (ví dụ: `ERR_STORAGE_404`).
* **Nhận diện và đánh dấu Boilerplate:** Phát hiện tiêu đề trang (header) và chân trang (footer) lặp lại theo mẫu (ví dụ: `"Trang \d+ / \d+"`, tên tài liệu lặp lại ở đầu mỗi trang). Không âm thầm xóa bỏ mà gắn cờ `is_boilerplate=True` trong metadata để phục vụ kiểm toán (audit trail).

#### 4.1.4. Rào Chắn Tuyệt Đối Không Dùng LLM
* Khâu Canonical Extraction là **100% deterministic**. Tuyệt đối không gọi mô hình ngôn ngữ lớn (LLM) để "chỉnh sửa câu chữ", "sửa lỗi chính tả", hay "viết lại tóm tắt".
* Tránh hoàn toàn hiện tượng ảo giác (hallucination) làm sai lệch dữ liệu gốc của tài liệu kỹ thuật.

#### 4.1.5. Lưu Trữ Tạm (Staging), Theo Dõi Stage & Cơ Chế Phục Hồi Lỗi
* Toàn bộ danh sách `CanonicalBlock` của một lượt xử lý được ghi tạm vào danh sách trong bộ nhớ/staging database transaction.
* Chỉ khi toàn bộ tệp được phân tích cú pháp thành công, transaction mới được `commit` xuống bảng `canonical_blocks`.
* Tiến trình được ghi nhận thông qua bản ghi `StageRun`:
  * `stage = 'PARSE'` $\rightarrow$ `status = 'RUNNING'` $\rightarrow$ `status = 'SUCCESS'`.
  * Nếu xảy ra lỗi: ghi nhận `status = 'FAILED'`, gắn nhãn `error_layer = ErrorLayer.ENGINE`, `error_stage = ErrorStage.PARSE`, lưu chi tiết `error_message`.
  * Hỗ trợ worker retry an toàn: nếu một lượt chạy lại (retry) diễn ra, hệ thống xóa bỏ các block tạm chưa hoàn tất của version đó trước khi ghi mới.

---

### 4.2. Phase 2B — Deduplication & Temporal Versioning

#### 4.2.1. Bốn Tầng Khử Trùng Lặp (4-Tier Dedup Architecture)
1. **Tầng 1 — File Exact Hash:** So sánh SHA-256 toàn bộ tệp (`checksum_sha256`). Nếu trùng hoàn toàn trong cùng một Logical Source, tái sử dụng `DocumentVersion` hiện có mà không chạy lại pipeline.
2. **Tầng 2 — Block Exact Hash & Cô Lập Phân Vùng Quyền (Security Partition Isolation):**
   * So sánh `content_hash` của từng `CanonicalBlock`. Nếu một khối văn bản đã tồn tại từ trước ở tài liệu khác hoặc phiên bản trước, nội dung được tái sử dụng nhưng **vẫn tạo bản ghi liên kết provenance/evidence riêng** cho phiên bản mới, đảm bảo trích dẫn (citation) trỏ chính xác về tài liệu đang truy vấn.
   * **Nguyên tắc cô lập quyền tuyệt đối khi Dedup:** Dù hai tài liệu ở hai phân vùng khác nhau (ví dụ: Doc A thuộc `security_partition_id='public'`, Doc B thuộc `security_partition_id='confidential'`) có đoạn văn bản giống hệt nhau, chúng **chỉ chia sẻ content hash ở tầng lưu trữ PostgreSQL**. Khi sang tầng `SearchUnit` và `Qdrant`, mỗi tài liệu/phiên bản **bắt buộc tạo Search Unit và Point ID riêng biệt** mang đúng `security_partition_id` và `project_id` của tài liệu đó. Tuyệt đối không chia sẻ chung vector point trên Qdrant giữa các phân vùng bảo mật khác nhau để triệt tiêu nguy cơ rò rỉ thông tin (ACL Leakage).
3. **Tầng 3 — Near-duplicate Candidate Clustering:**
   * Sử dụng kỹ thuật Shingling (k-shingles, $k=5$) kết hợp MinHash (128 hàm băm) và Locality Sensitive Hashing (LSH) hoặc tính chỉ số Jaccard trực tiếp.
   * *Ngưỡng tương đồng (Similarity Threshold):*
     * Văn bản tự sự (narrative text): Jaccard $\ge 0.85$.
     * Mã lệnh và bảng biểu (code/tables): Jaccard $\ge 0.95$ (để tránh nhận nhầm các dòng code có cấu trúc tương tự).
   * Tạo cụm ứng viên (Candidate Clusters), lưu liên kết ứng viên vào bảng metadata để phục vụ rà soát.
4. **Tầng 4 — Semantic Similarity Candidates (Cấm Tự Động Merge):**
   * Tính toán độ tương đồng Cosine trên không gian vector embedding.
   * Nếu Cosine Similarity $\ge 0.90$, hệ thống chỉ đánh dấu là **ứng viên ngữ nghĩa (Semantic Candidate)**.
   * **Quy tắc bất biến:** Tuyệt đối không tự động gộp (auto-merge) các phát biểu đối lập hoặc phiên bản thay thế. Hai câu có embedding rất gần nhau có thể mang ý nghĩa trái ngược hoàn toàn (ví dụ: *"Hệ thống hỗ trợ Redis"* và *"Hệ thống không hỗ trợ Redis"*).

#### 4.2.2. Phân Loại 5 Quan Hệ Ngữ Nghĩa (Semantic Relations)
Mọi cặp block hoặc claim tương đồng trong cụm ứng viên phải được phân loại thành 1 trong 5 nhãn quan hệ:
* `EQUIVALENT`: Cùng một nội dung ngữ nghĩa được diễn đạt bằng các cách khác nhau.
* `SUPPORTS`: Bằng chứng mới bổ trợ, củng cố tính xác thực cho tuyên bố đã có.
* `CONTRADICTS`: Hai nội dung mâu thuẫn trực tiếp về số liệu, logic hoặc chính sách.
* `SUPERSEDES`: Phiên bản mới trực tiếp thay thế hoặc bãi bỏ thông tin ở phiên bản cũ.
* `RELATED`: Có liên quan về chủ đề nhưng độc lập về mặt sự kiện, không thể gộp.

#### 4.2.3. Mô Hình Thời Gian Hai Trục (Bi-temporal Model) & Truy Vấn Lịch Sử
Bảng `document_versions` quản lý dòng thời gian qua các trường chuyên biệt:
* `source_published_at`: Thời điểm nguồn phát hành bản gốc (lấy từ metadata của tệp hoặc cấu hình).
* `observed_at`: Thời điểm SAG tiếp nhận và nhìn thấy tệp.
* `ingested_at`: Thời điểm hoàn thành ingestion vào cơ sở dữ liệu.
* `valid_from`: Thời điểm phiên bản bắt đầu có hiệu lực (mặc định bằng `observed_at`).
* `valid_to`: Thời điểm hết hiệu lực (mặc định `9999-12-31 23:59:59 UTC`).
* `supersedes_id`: Con trỏ liên kết trực tiếp tới ID của phiên bản cũ mà phiên bản này thay thế.
* **Nguyên tắc bảo toàn lịch sử:** Khi phiên bản $V_{k+1}$ tải lên thay thế $V_k$, hệ thống cập nhật `valid_to` của $V_k$ bằng thời điểm `observed_at` của $V_{k+1}$, đồng thời gán $V_{k+1}.\text{supersedes\_id} = V_k.\text{id}$. Không bao giờ xóa đè dữ liệu cũ. Các truy vấn có điều kiện thời gian (Point-in-time Query: *"Chính sách tại ngày 01/01/2026 là gì?"*) sẽ lọc theo khoảng `valid_from <= T <= valid_to` để trả về chính xác tri thức lịch sử.

#### 4.2.4. Tính Tất Định Khi Xử Lý Lại (Deterministic Reprocessing)
* Khi cần chạy lại tiến trình (reprocess) cho một `document_version_id`, hệ thống sử dụng cùng thuật toán và tham số băm.
* Quá trình retry đảm bảo tính lũy đòn (idempotent), không sinh thêm các bản ghi duplicate giả mạo và không làm sai lệch liên kết `supersedes_id` đã thiết lập.

---

### 4.3. Phase 2C — Search Indexing & Search Readiness

#### 4.3.1. Ranh Giới Đơn Vị Tìm Kiếm (Search Unit Boundary)
Search Unit là đơn vị cơ sở được index lên vector store để phục vụ truy xuất (retrieval-optimized).
* **Nguyên tắc phân đoạn:** Ưu tiên tuyệt đối ranh giới cấu trúc văn bản: **Heading $\rightarrow$ Paragraph $\rightarrow$ Table** trước khi áp đặt cửa sổ giới hạn token (Token Window).
* Một bảng biểu hoặc một khối mã lệnh nếu có kích thước nhỏ hơn trần `chunk_max_tokens` (mặc định 1.000 tokens) thì phải được giữ nguyên vẹn trong 1 `SearchUnit`, tuyệt đối không cắt đôi bảng ở giữa các hàng.
* Chỉ khi một đoạn văn bản hoặc bảng vượt quá `chunk_max_tokens`, hệ thống mới phân đoạn theo ranh giới dòng hoặc câu kết hợp overlap nhỏ ($\le 10\%$).

#### 4.3.2. Cấu Trúc Bảng `search_units`
$$\text{unit\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:unit:\{version\_id\}:\{ordinal\}"})$$
* **Trường dữ liệu bắt buộc:**
  * `id`: Khóa chính UUIDv5.
  * `document_version_id`: Khóa ngoại liên kết `document_versions.id`.
  * `block_from_id`: ID của `CanonicalBlock` bắt đầu.
  * `block_to_id`: ID của `CanonicalBlock` kết thúc.
  * `security_partition_id`: Phân vùng bảo mật kế thừa từ Ingress (ví dụ: `'public'`, `'team_backend'`). Cấm rỗng hoặc null.
  * `content_hash`: SHA-256 của toàn bộ nội dung trong Search Unit.
  * `token_count`: Số lượng token tính theo tokenizer chuẩn.
  * `page_from`, `page_to`: Khoảng trang bao phủ.
  * `section_path`: Phân cấp tiêu đề của khối.

#### 4.3.3. Biểu Diễn Kép: Dense Vector & Sparse Lexical (Kèm Cơ Chế Fallback)
* **Dense Representation:**
  * Sinh embedding vector đa chiều (ví dụ: 1536 chiều với text-embedding-3-small hoặc 1024 chiều với BGE-m3) thông qua cấu hình `EmbeddingProvider` trong `Settings`.
  * Tối ưu hóa cho tìm kiếm tương đồng ngữ nghĩa (semantic / paraphrase retrieval).
* **Sparse Representation (Chiến Lược Kép & Fallback An Toàn):**
  * *Nhánh 1 (Native Qdrant Sparse):* Nếu cụm Qdrant hỗ trợ named sparse vectors (Qdrant v1.7+), hệ thống nạp các cặp `(indices, values)` biểu diễn tần suất từ khóa BM25/lexical vào trường sparse vector của point.
  * *Nhánh 2 (PostgreSQL Lexical Fallback):* Nếu cấu hình Qdrant chỉ hỗ trợ dense vector đơn thuần, phần sparse lexical retrieval được đảm nhiệm bởi PostgreSQL Full-Text Search / `grep_chunks()` kết hợp thuật toán RRF Fusion đã thiết lập ở Phase 4. Đảm bảo hệ thống vận hành ổn định trên mọi môi trường mà không bị phụ thuộc cứng vào extension của Qdrant.
* **Tách biệt rạch ròi:** `SearchUnit` phục vụ truy xuất thông tin (retrieval); trong khi `KnowledgeUnit` (ở Phase 5) phục vụ phân cụm đồ thị tri thức (reasoning). Hai thực thể này được lưu trữ độc lập.

#### 4.3.4. Thiết Kế Payload Qdrant & Khởi Tạo Trước Trường Cây (Pre-provisioned Tree Fields)
Trước khi nạp dữ liệu (ingestion), Adapter Qdrant phải đảm bảo bộ sưu tập (collection) đã được khởi tạo sẵn các **Payload Index** tương ứng với các trường lọc.
Đặc biệt, để sẵn sàng cho Phase 6 và Phase 8 (Knowledge Routing Tree & Dual-slot Rebuild) mà **không phải migrate lại schema Qdrant sau này**, payload của mỗi point tại Phase 2C được quy chuẩn đầy đủ theo Phụ lục B của Workflow v1.1:

```json
{
  "point_id": "UUIDv5-point-id",
  "tenant_id": "tenant_continuum_default",
  "project_id": "proj_12345",
  "security_partition_id": "team_backend",
  "document_id": "doc_abcde",
  "document_version_id": "ver_67890",
  "search_unit_id": "unit_54321",
  "content_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",

  "primary_node_a": null,
  "secondary_node_ids_a": [],
  "tree_version_a": null,

  "primary_node_b": null,
  "secondary_node_ids_b": [],
  "tree_version_b": null,

  "page_from": 1,
  "page_to": 2,
  "valid_from": 1727800000,
  "valid_to": 253402300799
}
```

* **Danh sách Payload Index bắt buộc tạo trước khi nạp:**
  1. `tenant_id` (keyword)
  2. `project_id` (keyword)
  3. `security_partition_id` (keyword)
  4. `document_version_id` (keyword)
  5. `valid_from` (integer)
  6. `valid_to` (integer)
  7. `primary_node_a` (keyword) — *sẵn sàng cho Tree Slot A*
  8. `primary_node_b` (keyword) — *sẵn sàng cho Tree Slot B*

* **Mục đích:** Khi người dùng truy vấn ở Phase 4 (Global Hybrid) hay Phase 7 (Tree Routing), Qdrant luôn lọc cứng (`Pre-filtering`) theo quyền (`tenant_id`, `project_id`, `security_partition_id`) và thời gian hiệu lực (`valid_from <= T <= valid_to`) trước khi tính toán khoảng cách vector.

#### 4.3.5. Nạp Qdrant Bằng Stable Point ID (Idempotent Upsert)
* Điểm vector (vector point) trong Qdrant sử dụng định danh tất định:
  $$\text{point\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:qdrant:search\_units:\{unit\_id\}"})$$
* Việc nạp vector sử dụng phương thức `upsert`. Khi reprocess cùng một tài liệu, các điểm vector cũ được ghi đè chính xác theo `point_id`, không sinh thêm vector rác và không tạo ra kết quả trùng lặp khi truy vấn.

#### 4.3.6. Index Manifest & Cổng Mở Khóa `SEARCH_READY`
* Sau khi toàn bộ các vector của phiên bản tài liệu được nạp vào Qdrant, hệ thống tạo bản ghi **Index Manifest**:
  * `total_units_count`: Số lượng Search Unit trong PostgreSQL.
  * `total_vectors_count`: Số lượng vector được xác nhận lưu trữ trong Qdrant.
  * `collection_name`: Tên bộ sưu tập Qdrant.
  * `manifest_checksum`: Mã băm SHA-256 tổng hợp của danh sách `(point_id, content_hash)`.
* **Cổng Kiểm Tra Nghiêm Ngặt:**
  * Hệ thống thực hiện đối soát: Nếu `total_vectors_count == total_units_count` và các điều kiện nhất quán đạt yêu cầu $\rightarrow$ Cập nhật `document_versions.search_status = 'SEARCH_READY'`, `document_versions.status = 'SEARCH_READY'`, ghi nhận thời điểm `search_ready_at`.
  * Nếu phát hiện sai lệch: Chuyển `search_status = 'FAILED'`, không mở cổng tìm kiếm cho phiên bản này.

#### 4.3.7. Khả Năng Tái Tạo Chỉ Mục Qdrant Từ PostgreSQL (Index Rebuildability)
* Qdrant không phải là nơi lưu trữ gốc. Toàn bộ nội dung văn bản, metadata, phân vùng bảo mật và khoảng thời gian hiệu lực đều nằm trọn vẹn trong PostgreSQL (`canonical_blocks` và `search_units`).
* Xây dựng module dịch vụ `RebuildService`: Cho phép đọc lại toàn bộ `search_units` từ PostgreSQL, tính toán lại vector và tái tạo toàn bộ collection trong Qdrant khi cần di chuyển hạ tầng hoặc phục hồi sau thảm họa.

#### 4.3.8. Mô Hình Kiến Trúc Cụm Qdrant: Shared Cluster vs. Per-Project Collection
* **Quy chuẩn hạ tầng (Cluster Topology):** Hệ thống triển khai **1 Cụm Qdrant Dùng Chung (Shared Qdrant Cluster)** theo cấu hình `sag_qdrant_url` trong `Settings`. Không triển khai mỗi Project một cụm Qdrant độc lập nhằm tránh lãng phí tài nguyên RAM/CPU, overhead mạng và phức tạp hóa quản lý connection pool.
* **Quy chuẩn bộ sưu tập (Collection Partitioning):**
  * Áp dụng mô hình **Per-Project Collection**: $\text{collection\_name} = f\text{"search\_units\_\{project\_id\}"}$ theo đúng chuẩn DDL và kịch bản Rollback tại [phase-0-contracts-and-foundations.md](../phase-0-contracts-and-foundations.md#L732).
  * *Lợi ích:* 
    1. Đảm bảo cô lập vật lý cấp Collection giữa các Project: Không có bất kỳ nguy cơ nào truy vấn nhầm sang Project khác kể cả khi ứng dụng gặp lỗi logic ở lớp filter.
    2. Dễ dàng bảo trì, dọn dẹp hoặc xóa toàn bộ một Project bằng một lệnh `DELETE /collections/search_units_{project_id}` mà không gây phân mảnh hay locking trên dữ liệu của Project khác.
    3. HNSW index của từng Project được xây dựng độc lập, giữ độ chính xác cao và không bị loãng vector space.

---

## 5. Lộ Trình Triển Khai 6 Gói Công Việc (Work Packages Breakdown)

```text
+-----------------------------------------------------------------------------------------------+
|                           LỘ TRÌNH TRIỂN KHAI 6 GÓI CÔNG VIỆC                                  |
+-----------------------------------------------------------------------------------------------+
| [WP1] Parser Router, Normalizer & Fixtures Suite (Phase 2A)                                   |
|       ===> Chuẩn hóa 15 định dạng, Unicode NFC, bảo tồn bảng/code/ID, test fixtures           |
|                                           |                                                   |
|                                           v                                                   |
| [WP2] Canonical Block Extractor, Persistence & Staging (Phase 2A)                             |
|       ===> Sinh UUIDv5 block_id, lưu canonical_blocks, StageRun & tracking lỗi                 |
|                                           |                                                   |
|                                           v                                                   |
| [WP3] 4-Tier Deduplication & Candidate Clustering Engine (Phase 2B)                           |
|       ===> File/Block hash, MinHash/LSH near-dup, Semantic candidate guard, 5 quan hệ         |
|                                           |                                                   |
|                                           v                                                   |
| [WP4] Bi-temporal Lineage & Reprocess State Manager (Phase 2B)                                |
|       ===> valid_from/to, supersedes_id, point-in-time history query, deterministic reprocess |
|                                           |                                                   |
|                                           v                                                   |
| [WP5] Search Unit Structural Chunker & Qdrant Dense/Sparse Store (Phase 2C)                   |
|       ===> Chunker theo heading/table, dual vector, Qdrant payload index, UUIDv5 point_id     |
|                                           |                                                   |
|                                           v                                                   |
| [WP6] Index Manifest Verification, SEARCH_READY Gate & Rebuild Service (Phase 2C)             |
|       ===> Verify count/checksum, mở khóa SEARCH_READY, CLI/Service rebuild từ PostgreSQL     |
+-----------------------------------------------------------------------------------------------+
```

### Gói Công Việc 1 (WP1): Parser Router, Normalizer & Fixtures Suite (Phase 2A)
* **Nhiệm vụ:**
  1. Xây dựng module điều hướng parser `sag_api/parsing/router.py`, định tuyến chuẩn xác 15 định dạng: PDF (MinerU $\rightarrow$ Fallback MarkItDown), Office (MarkItDown), Text/CSV/JSON/HTML/EPUB (Native/MarkItDown).
  2. Xây dựng module chuẩn hóa văn bản `sag_api/parsing/normalizer.py`: thực thi Unicode NFC, giữ nguyên cấu trúc Markdown Table, giữ thụt lề code block, giữ nguyên dấu chấm câu và ký hiệu kỹ thuật (`_`, `-`, `/`, UUID).
  3. Xây dựng bộ phát hiện boilerplate (header/footer lặp lại) và gắn nhãn audit.
  4. Tạo bộ test fixtures mẫu tại `apps/api/tests/fixtures/canonical/` đại diện cho 15 định dạng.

### Gói Công Việc 2 (WP2): Canonical Block Extractor, Persistence & Staging (Phase 2A)
* **Nhiệm vụ:**
  1. Xây dựng `sag_api/services/canonical_service.py` trích xuất danh sách khối văn bản có cấu trúc: `heading`, `paragraph`, `table`, `code`, `list`, `caption`.
  2. Áp dụng công thức sinh định danh tất định `block_id = UUIDv5(...)`.
  3. Lưu trữ có transactional staging: ghi dữ liệu vào bảng `canonical_blocks`, cập nhật `StageRun` với `stage='PARSE'`, đo đạc `duration_ms` và số lượng blocks tạo ra.
  4. Đảm bảo worker retry an toàn và ném lỗi chuẩn hóa theo `ErrorLayer.ENGINE` và `ErrorStage.PARSE`.

### Gói Công Việc 3 (WP3): 4-Tier Deduplication & Candidate Clustering Engine (Phase 2B)
* **Nhiệm vụ:**
  1. Xây dựng `sag_api/services/dedup_service.py` quản lý 4 tầng dedup:
     * Tầng 1: File exact hash match.
     * Tầng 2: Block exact hash match với liên kết đa bằng chứng (multi-evidence retention).
     * Tầng 3: Near-duplicate bằng k-shingling và MinHash/LSH, áp dụng ngưỡng 0.85 cho text và 0.95 cho code/bảng.
     * Tầng 4: Semantic similarity candidate linking (Cosine $\ge 0.90$), cấm auto-merge.
  2. Xây dựng logic phân loại 5 quan hệ ngữ nghĩa (`EQUIVALENT`, `SUPPORTS`, `CONTRADICTS`, `SUPERSEDES`, `RELATED`) và lưu vào cấu trúc evidence mapping.

### Gói Công Việc 4 (WP4): Bi-temporal Lineage & Reprocess State Manager (Phase 2B)
* **Nhiệm vụ:**
  1. Xây dựng `sag_api/services/temporal_service.py` quản lý các mốc thời gian: `source_published_at`, `observed_at`, `ingested_at`, `valid_from`, `valid_to`.
  2. Xử lý logic chuỗi phiên bản: gán `supersedes_id` và khép khoảng thời gian hiệu lực `valid_to` của phiên bản bị thay thế.
  3. Cung cấp API/hàm truy vấn lịch sử tri thức theo thời điểm (Point-in-time retrieval).
  4. Đảm bảo reprocess chạy tất định, không làm đứt gãy lịch sử và không làm mất dữ liệu kiểm toán.

### Gói Công Việc 5 (WP5): Search Unit Structural Chunker & Qdrant Dense/Sparse Store (Phase 2C)
* **Nhiệm vụ:**
  1. Xây dựng `sag_api/services/search_unit_service.py`: gom khối `CanonicalBlock` thành `SearchUnit` theo ranh giới Heading, Paragraph, Table trước khi áp trần `chunk_max_tokens`.
  2. Lưu bản ghi vào bảng `search_units` kèm `security_partition_id` bắt buộc.
  3. Mở rộng `sag_api/sag/qdrant_store.py`:
     * Áp dụng mô hình collection riêng cho từng project: $\text{collection\_name} = f\text{"search\_units\_\{project\_id\}"}$.
     * Khởi tạo Payload Indexes cho các trường lọc: `tenant_id`, `project_id`, `security_partition_id`, `document_version_id`, `valid_from`, `valid_to`, `primary_node_a`, `primary_node_b`.
     * Hỗ trợ nạp cả vector dày (dense) và vector thưa (sparse/lexical).
     * Sinh `point_id` theo công thức `UUIDv5` từ `unit_id`.

### Gói Công Việc 6 (WP6): Index Manifest Verification, SEARCH_READY Gate & Rebuild Service (Phase 2C)
* **Nhiệm vụ:**
  1. Xây dựng `sag_api/services/search_index_service.py`: tính toán Index Manifest, kiểm tra đối soát số lượng vector trong Qdrant và Search Unit trong PostgreSQL.
  2. Triển khai Cổng Mở Khóa `SEARCH_READY`: chỉ chuyển `document_versions.search_status = 'SEARCH_READY'` và `status = 'SEARCH_READY'` khi manifest hoàn toàn nhất quán.
  3. Xây dựng `sag_api/services/rebuild_service.py`: hỗ trợ dựng lại toàn bộ chỉ mục Qdrant từ cơ sở dữ liệu PostgreSQL và source snapshot.
  4. Tích hợp toàn bộ luồng vào worker pipeline trong `sag_api/jobs/tasks.py`.

---

## 6. Chiến Lược Kiểm Thử Hồi Quy & Cổng Nghiệm Thu (Testing & Verification Strategy)

### 6.1. Danh Sách Tệp Kiểm Thử Chuyên Biệt
Để đảm bảo tính độc lập và dễ theo dõi, 3 tệp kiểm thử tự động mới sẽ được thiết lập tại `apps/api/tests/`:
1. `tests/test_phase_2a_canonical_extraction.py`:
   * Kiểm thử bóc tách 15 định dạng file từ fixtures.
   * Kiểm thử bảo tồn cấu trúc bảng Markdown, thụt lề code block, và giữ nguyên định danh kỹ thuật.
   * Kiểm thử phát hiện và đánh dấu boilerplate.
   * Kiểm thử tính tất định của `block_id` (UUIDv5) và cơ chế lưu trữ theo transaction.
2. `tests/test_phase_2b_dedup_and_temporal.py`:
   * Kiểm thử tái sử dụng block trùng lặp mà vẫn giữ đầy đủ provenance/evidence.
   * Kiểm thử gom cụm near-duplicate bằng MinHash/LSH với ngưỡng khác nhau cho text và code.
   * Kiểm thử cấm tự động merge khi gặp `CONTRADICTS` hoặc `SUPERSEDES`.
   * Kiểm thử cập nhật `valid_to` khi có phiên bản mới thay thế (`supersedes_id`) và truy vấn Point-in-time.
   * Kiểm thử tính tất định khi reprocess.
3. `tests/test_phase_2c_search_indexing.py`:
   * Kiểm thử phân đoạn Search Unit theo cấu trúc Heading/Table, không cắt đôi bảng.
   * Kiểm thử Qdrant payload schema được pre-index đầy đủ trước khi nạp.
   * Kiểm thử sinh `point_id` tất định (UUIDv5) và nạp lặp lại (reprocess) không sinh duplicate vector.
   * Kiểm thử Index Manifest verifier: từ chối `SEARCH_READY` nếu thiếu vector, kích hoạt `SEARCH_READY` khi nhất quán.
   * Kiểm thử rebuild toàn bộ Qdrant collection từ bảng `search_units` trong PostgreSQL.

### 6.2. Cổng Nghiệm Thu (Definition of Done)
* Toàn bộ 18 tiêu chí trong `tasks/todo.md` (Phase 2A, 2B, 2C) được đánh dấu hoàn thành kèm bằng chứng kiểm thử tự động (100% test pass).
* Không có bất kỳ phụ thuộc LLM nào trong khâu Ingestion đưa tài liệu lên trạng thái `SEARCH_READY`.
* Mọi thực thể sinh ra (`CanonicalBlock`, `SearchUnit`, Qdrant `point_id`) đều có thể tái tạo tất định từ khóa định danh và nội dung (UUIDv5).
* Người dùng có thể thực hiện tìm kiếm tài liệu ngay khi tài liệu đạt `SEARCH_READY`, trong khi nhánh tri thức nâng cao (Knowledge Tree) tiếp tục chạy ngầm mà không gây khóa hệ thống.

---

## 7. Ánh Xạ Trực Tiếp & Kế Hoạch Đóng Khớp 3 Task Jira (Jira Alignment & Gap Closure)

Nhằm đảm bảo kế hoạch khớp 100% với phân công nhiệm vụ thực tế của **Owner: Phan Tài** trên hệ thống Jira, bản kế hoạch bổ sung các cam kết và các điểm kết nối (wire-up) cụ thể như sau:

### 7.1. Bảng Đối Chiếu Chi Tiết 3 Task Jira

| Mã Jira | Phạm Vi & Mục Tiêu | Files Phụ Trách | Tiêu Chí Hoàn Thành & Bằng Chứng | Điểm Kết Nối Xử Lý (Action Items) |
| :--- | :--- | :--- | :--- | :--- |
| **Jira Task 1** | **Phase 2B–2C: Exact dedup/upsert & Search Readiness**<br>Không mở rộng sang retrieval/ranking. | `apps/api/sag_api/sag/engine_manager.py`<br>`sag_api/db/models/document.py`<br>`sag_api/jobs/tasks.py`<br>`sag_api/services/search_index_service.py` | - Reprocess/upsert không để lại vector/evidence cũ hoặc trùng.<br>- Search chỉ báo ready sau khi manifest index được xác minh 100%.<br>- Todo ghi nhận rõ phần hoàn tất và gap. | 1. Cập nhật `document_version.search_status = 'READY'` và `document_version.search_ready_at = func.now()` chỉ sau khi `manifest_verified == True`.<br>2. Nối `run_search_indexing_stage()` vào pipeline worker trong `tasks.py`. |
| **Jira Task 2** | **Ingestion Retry / Reprocess & Truy Nguyên Lỗi Stage**<br>Tránh tạo Document/Job trùng khi chạy lại; giữ lỗi đủ truy nguyên theo stage. | `apps/api/sag_api/jobs/tasks.py`<br>`sag_api/services/document_service.py`<br>`sag_api/db/models/document.py` | - Retry/reprocess lặp lại không nhân bản bản ghi.<br>- Trạng thái/lỗi mỗi stage quan sát được minh bạch.<br>- Cập nhật Todo / Ingestion report. | 1. Đảm bảo cập nhật `doc.error_layer` (`parse`/`engine`/`store`) và `doc.error_stage` (`parse`/`dedup`/`index`) trên bảng `documents` khi bất kỳ stage nào gặp lỗi.<br>2. Bảo toàn tính idempotent thông qua `idempotency_key` và cơ chế dọn dẹp SearchUnit cũ. |
| **Jira Task 3** | **Phase 2A: Parser Output sang Canonical Blocks**<br>Bảo toàn cấu trúc bảng, code, identifier, thứ tự và anchor ổn định. | `apps/api/sag_api/parsing/service.py`<br>`sag_api/parsing/canonical.py`<br>`sag_api/services/canonical_service.py` | - Fixtures định dạng xác nhận block/order/anchor ổn định.<br>- Cấu trúc quan trọng được giữ 100%.<br>- Todo cập nhật kết quả/gap Phase 2A. | 1. Tích hợp hàm chuyển đổi `extract_canonical_blocks()` trực tiếp vào `sag_api/parsing/service.py`.<br>2. Nhận output Markdown từ MinERU/MarkItDown và tự động bóc tách thành Canonical Blocks chuẩn hóa. |

### 7.2. Trình Tự Thực Thi Đóng Khớp (Execution Steps - Ponytail Style)

```text
+-------------------------------------------------------------------------------------------------------+
| BƯỚC 1: TÍCH HỢP CANONICAL EXTRACTION VÀO PARSING SERVICE (JIRA TASK 3)                               |
| - Mở rộng sag_api/parsing/service.py: import canonical extractor.                                     |
| - Sau khi parser parse ra text/markdown, gọi extract_canonical_blocks() tạo ra CanonicalBlock objects. |
+---------------------------------------------------+---------------------------------------------------+
                                                    |
                                                    v
+-------------------------------------------------------------------------------------------------------+
| BƯỚC 2: CẬP NHẬT SEARCH READINESS & ATTRIBUTION LỖI TRONG WORKER (JIRA TASK 1 & 2)                    |
| - Trong sag_api/services/search_index_service.py:                                                     |
|     * Khi manifest khớp: document_version.search_status = 'READY', search_ready_at = now.           |
|     * Khi manifest lệch: document_version.search_status = 'INDEX_FAILED'.                             |
| - Trong sag_api/jobs/tasks.py:                                                                        |
|     * Bổ sung try/except gán doc.error_layer và doc.error_stage tương ứng với từng stage.             |
|     * Gọi run_dedup_and_temporal_stage() và run_search_indexing_stage() theo thứ tự.                  |
+---------------------------------------------------+---------------------------------------------------+
                                                    |
                                                    v
+-------------------------------------------------------------------------------------------------------+
| BƯỚC 3: KIỂM CHỨNG TOÀN DIỆN & CẬP NHẬT TODO EVIDENCE                                                 |
| - Chạy test suites chuyên biệt: test_phase_2a, test_phase_2b, test_phase_2c.                          |
| - Kiểm chứng không phá vỡ các test liên quan: test_document_job_retry.py.                             |
| - Cập nhật bảng kiểm soát và báo cáo nghiệm thu hoàn chỉnh.                                           |
+-------------------------------------------------------------------------------------------------------+
```

---

## 8. Bổ Sung & Đóng Khớp Đợt Review Mới Nhất PR #14 (PR #14 Re-Review Closure)

Bản cập nhật này giải quyết toàn bộ 6 phát hiện trọng yếu từ lượt review mới nhất của Reviewer (`KeyT9999`) trên PR #14:

1. **[P1] Thứ tự dọn dẹp retry & Qdrant points cleanup (`sag_api/services/canonical_service.py` & `search_index_service.py`):**
   - Đảo ngược thứ tự xóa: Xóa `SearchUnit` trước khi xóa `CanonicalBlock` trên DB để tránh lỗi khóa ngoại FK.
   - Thêm bước xóa points cũ trên Qdrant qua API `POST /collections/{collection}/points/delete` có filter `document_version_id` trước khi nạp lại.
   - Sinh `SearchUnit.id` tất định qua UUIDv5 `UUIDv5("sag:unit:{doc_ver_id}:{ordinal}")` và `point_id` qua `UUIDv5("sag:qdrant:{collection}:{unit_id}")`.
2. **[P1] Qdrant API Key Header (`sag_api/jobs/tasks.py`):**
   - Truyền `api-key` header từ cấu hình `settings.sag_qdrant_api_key` vào `httpx.AsyncClient`.
3. **[P1] Manifest Gatekeeper đối soát Qdrant points count (`sag_api/services/search_index_service.py`):**
   - Không chỉ đếm PostgreSQL `search_units`, mà còn gọi trực tiếp `POST /collections/{collection}/points/count` với filter `document_version_id`.
   - Đối chiếu số lượng chính xác 100% giữa PostgreSQL và Qdrant trước khi gắn trạng thái `SEARCH_READY`.
4. **[P1] Near-Dedup MinHash/LSH, separate thresholds & Evidence Recording (`sag_api/services/dedup_and_temporal_service.py`):**
   - Áp dụng MinHash 128 hash permutations cho n-gram shingles.
   - Phân tách ngưỡng: 0.85 cho văn bản thông thường, 0.95 cho code và bảng dữ liệu.
   - Phân loại quan hệ ngữ nghĩa 5 nhãn (`EQUIVALENT`, `SUPPORTS`, `CONTRADICTS`, `SUPERSEDES`, `RELATED`) và cấm tự động merge (`auto_merged = False`).
   - Ghi nhận chi tiết evidence mapping vào `document_version.metadata_json` và `StageRun.metrics_json`.
5. **[P1] Dual Representation (Dense + BM25 Sparse), Payload Indexes & Rebuild Service (`sag_api/services/search_index_service.py` & `rebuild_service.py`):**
   - Khởi tạo 8 payload indexes (`tenant_id`, `project_id`, `security_partition_id`, `document_version_id`, `valid_from`, `valid_to`, `primary_node_a`, `primary_node_b`).
   - Dual vector: `content_vector` (dense) + `bm25_sparse` (lexical sparse term frequency).
   - Module `rebuild_service.py` hỗ trợ tái tạo toàn bộ vector collection từ nguồn chân lý duy nhất PostgreSQL.
6. **[P2] Bảo vệ Bi-Temporal không chồng lấn khi nạp lệch thứ tự (`sag_api/services/dedup_and_temporal_service.py`):**
   - Tìm kiếm predecessor còn active qua khoảng thời gian thực tế `valid_from <= cur_start < valid_to` thay vì chỉ sắp xếp số phiên bản `version_no`.
   - Chuẩn hóa UTC an toàn cho mọi timestamp và khép kín khoảng thời gian, loại bỏ hoàn toàn hiện tượng chồng lấn hiệu lực.

