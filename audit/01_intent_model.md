# 01 — Mô Hình Mục Đích & Bất Biến Kiến Trúc (Intent Model)

Ngày cập nhật: 2026-10-21.

## 1. Mục đích cốt lõi của hệ thống (1–3 câu)

Hệ thống **SAG (SQL-Retrieval & Structured Knowledge Routing Augmented Generation)** cung cấp nền tảng quản trị tri thức đa-tenant/đa-project (`Continuum`), cho phép nạp tài liệu bất biến theo phiên bản (`DocumentVersion` / `SourceSnapshot`), phân rã thành các khối chuẩn hóa (`CanonicalBlock`), lập chỉ mục lai (`SearchUnit` trên Qdrant + `SourceChunk`/`SourceEvent` của `zleap-sag`), trích xuất đơn vị tri thức có bằng chứng neo ký tự (`KnowledgeUnit` & `KnowledgeEvidence` bậc `E0`/`E1`/`E2`), và dựng đồ thị + cây định tuyến phân hoạch bảo mật (`KnowledgeGraphBuild` & `TreeManifest` dùng Leiden CPM) để truy xuất câu trả lời chính xác, truy nguyên được nguồn gốc và chống rò rỉ chéo giữa các dự án.

---

## 2. Các Use Cases chính & Chất lượng kỳ vọng

1. **Nạp & Quản lý phiên bản tài liệu bất biến (Immutable Versioned Ingestion)**:
   - Mỗi lần tải tài liệu lên một `(tenant_id, project_id, security_partition_id)` phải lưu bản chụp nội dung theo địa chỉ băm SHA-256 (`snapshots/{hash[:2]}/{hash}/{filename}`), đảm bảo tính toàn vẹn tuyệt đối từ file vật lý $\rightarrow$ `SourceSnapshot` $\rightarrow$ `DocumentVersion` $\rightarrow$ `CanonicalBlock` $\rightarrow$ `SearchUnit` $\rightarrow$ `KnowledgeUnit`.
2. **Trích xuất Tri thức có Bằng chứng Neo Ký tự (Evidence-Anchored Knowledge Extraction — E0/E1/E2 & Engine Events)**:
   - Từ mỗi đoạn văn bản (`CanonicalBlock`), hệ thống phải trích xuất chính xác các thực thể (`entity`), câu khẳng định (`claim`), quan hệ (`relation`), và sự kiện (`SourceEvent`), bảo toàn ngôn ngữ gốc của tài liệu và ngữ nghĩa câu để phục vụ xây dựng đồ thị tri thức (`KnowledgeGraphBuild`), đánh giá chất lượng tri thức (`KnowledgeQualityAssessment`), và sinh câu hỏi bù đắp khoảng trống tri thức (`KnowledgeGapQuestion`).
3. **Định tuyến Truy vấn phân cấp & Tìm kiếm Lai (Hierarchical Tree Routing & Hybrid Retrieval)**:
   - Xây dựng đồ thị `KnowledgeUnitEdge` (kết hợp 6 tín hiệu: `semantic`, `lexical`, `entity`, `structure`, `temporal`, `citation`) và phân hoạch cây Leiden (`TreeManifest` / `TreeRoutingProfile`) để định tuyến truy vấn vào đúng cụm tri thức trước khi truy xuất vector + BM25 (`SearchUnit` / `SourceChunk`).
4. **Cách ly Đa Tenant / Project / Security Partition (Fail-Closed Multi-Tenant Isolation)**:
   - Tuyệt đối không để dữ liệu, bảng DB, hoặc vector của project/partition này rò rỉ hoặc trộn lẫn không kiểm soát với project/partition khác, và không để tài liệu chưa hoàn tất (`PAUSED` / `FAILED` / `DELETING`) làm nhiễm bẩn kết quả tìm kiếm.

---

## 3. Hệ thống PHẢI ĐÚNG ở đâu và ĐƯỢC PHÉP SAI ở đâu?

- **PHẢI ĐÚNG (Zero-Tolerance)**:
  1. **Toàn vẹn Checksum & Truy nguyên nguồn (Provenance & Checksum)**: Mọi `KnowledgeUnit`, `KnowledgeEvidence`, `CanonicalBlock`, `SourceSnapshot` phải khớp SHA-256 và tọa độ ký tự (`unit.text[start:end] == quote`). (Tài liệu kiểm thử `sag-metric-partition-universe.md` quy định rõ: mọi câu trả lời thực tế phải trích dẫn `source identifier`, `source version`, `page or chunk`, và đạt `evidence score >= 0.75`).
  2. **Cách ly Schema & Security Partition**: Dữ liệu giữa các schema PostgreSQL và giữa các `security_partition_id` phải nhất quán và tách biệt.
  3. **Tính đúng đắn ngữ nghĩa của Thực thể, Claim và Quan hệ**: Không được cắt gãy câu chứa số thập phân (như `0.75`) thành claim sai sự thật, không được ghép cặp thực thể vô nghĩa theo bảng chữ cái làm sai lệch đồ thị, và không được dịch cưỡng bức tài liệu tiếng Anh sang sự kiện tiếng Trung.
- **ĐƯỢC PHÉP SAI (Acceptable Degradation)**:
  1. Khi tìm kiếm chính xác (`multi` / graph) bị quá thời gian (`timeout`), hệ thống được phép hạ cấp (`fallback_used = True`) về tìm kiếm vector/lexical miễn là vẫn giữ nguyên bộ lọc ACL/partition.
  2. Ở bậc trích xuất tĩnh `E0`/`E1` (trước khi chạy LLM `E2`), độ phủ thực thể có thể thấp hơn LLM nhưng **không được sinh dữ liệu rác/sai sự thật** hoặc làm liệt cơ chế chọn mẫu lên bậc `E2` (`importance >= 0.5`).

---

## 4. Bất biến Kiến trúc (Architectural Invariants — Dùng làm chuẩn quy chiếu ở các Phase sau)

- **INV-1 (Schema & Store Coherence)**: Mọi bảng quan hệ của ứng dụng SAG và của engine `zleap-sag` cùng phục vụ một project/deployment phải đọc/ghi trên schema nhất quán và dùng định dạng khóa ngoại (`source_config_id` / `data_source.id`) khớp nhau không lệch khoảng trắng.
- **INV-2 (Snapshot & Checksum Chain of Custody)**: Chuỗi băm từ file vật lý trên đĩa $\rightarrow$ `source_snapshots.checksum_sha256` $\rightarrow$ `document_versions.file_hash` $\rightarrow$ `canonical_blocks.content_hash` $\rightarrow$ `knowledge_units.content_hash` & `checksum` phải khớp 100%.
- **INV-3 (Semantic Fidelity of E0/E1 Knowledge Extraction)**: Bộ tách câu, bộ nhận diện thực thể và bộ trích xuất quan hệ `E0`/`E1` phải bảo toàn ranh giới câu (đặc biệt với số thập phân, ngày tháng, phiên bản), nhận diện được thực thể miền (Title-Case / thuật ngữ kỹ thuật), và chỉ tạo quan hệ `CO_OCCURS` có cơ sở ngữ cảnh câu thay vì ghép cặp chữ cái tùy tiện.
- **INV-4 (Graph & Routing Signal Completeness)**: Đồ thị `KnowledgeGraphBuild` và hồ sơ định tuyến `TreeRoutingProfile` phải có đầy đủ các tín hiệu trọng số chính (`semantic` dense vector 30%, `entity` overlap 25%, `lexical` 20%) như thiết kế trong `SIGNAL_WEIGHTS`.
- **INV-5 (Language-Preserving Engine Extraction)**: Quá trình trích xuất sự kiện/thực thể bằng LLM trong `zleap-sag` phải giữ nguyên ngôn ngữ của tài liệu nguồn (hoặc tự động phát hiện ngôn ngữ) thay vì ép cứng tiếng Trung (`sag_language = "zh"`).
- **INV-6 (Lifecycle & Readiness Hygiene across Stores)**: Chỉ các tài liệu ở trạng thái hoàn tất hợp lệ (`READY`) mới được tham gia vào tập chỉ mục phục vụ truy xuất hoặc phải có bộ lọc trạng thái đồng bộ giữa `documents`, `search_units` (Qdrant), và `source_chunks` (Qdrant); khi tài liệu chuyển sang `READY`, mọi trường lỗi tạm thời (`error_layer`, `error_stage`) phải được xóa sạch.

---

## 5. Giả định đã được Xác minh bằng Dữ liệu Thật (Cập nhật từ Phase 3)

- Giả định ở phiên khảo sát sơ bộ rằng *"project `6ad61cb2e1f989c367a1eb46` không có trong PostgreSQL"* đã được **bác bỏ hoàn toàn bằng dữ liệu thật**: project này nằm trong schema riêng `sag_metric_e2e_67a1eb46` trên cùng instance PostgreSQL `localhost:5432/sag`, trong khi engine `zleap-sag` lại ghi nhầm vào schema mặc định `"sag"` do lỗi cấu hình trong `sag_api/sag/config_builder.py:66-77`.
