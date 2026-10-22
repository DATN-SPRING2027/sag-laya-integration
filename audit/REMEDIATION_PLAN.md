# KẾ HOẠCH KHẮC PHỤC CHI TIẾT & ĐỐI CHIẾU KIẾN THỨC TIÊN TIẾN NHẤT (SOTA REMEDIATION PLAN)

**Dự án**: `sag-laya-integration` (`SAG/apps/api/sag_api`)  
**Mục tiêu**: Khắc phục triệt để 5 nhóm lỗi kiến trúc & ngữ nghĩa đã xác nhận bằng dữ liệu thật trên project `6ad61cb2e1f989c367a1eb46` (tài liệu `sag-metric-partition-universe.md` và lớp adapter `sag_api/sag/`), dựa trên các nghiên cứu và thực hành tiên tiến nhất giai đoạn 2024–2026 về **GraphRAG (HippoRAG 2, LightRAG, Microsoft GraphRAG)**, **Hybrid Dense-Sparse Retrieval (`Qwen3-Embedding-0.6B` + BM25 RRF)**, và **Multi-Schema Isolation trên SQLAlchemy 2.x / PostgreSQL**.

---

## PHẦN 1: TỔNG HỢP NGHIÊN CỨU KIẾN THỨC TIÊN TIẾN NHẤT (SOTA RESEARCH 2024–2026)

Để đảm bảo việc sửa lỗi không chỉ là "chữa cháy cục bộ" mà nâng tầm kiến trúc của dự án đúng với thiết kế **Knowledge Routing RAG**, dưới đây là đối chiếu giữa các công trình SOTA và từng vấn đề đã xác nhận trong codebase:

### 1.1 Trích xuất Đồ thị Tri thức từ Đoạn văn (Passage-Centric Knowledge Extraction — HippoRAG 2 & LightRAG)
- **Kiến thức SOTA**:
  - **HippoRAG (NeurIPS 2024) & HippoRAG 2 (2025)**: Thay vì tách rời đồ thị tri thức khỏi văn bản gốc, HippoRAG xây dựng đồ thị hai tầng gồm **Passage Nodes** (`KnowledgeUnit`) và **Phrase/Entity Nodes** (`KnowledgeEvidence`), nối với nhau qua cạnh *chứa đựng (containment)* và *đồng xuất hiện trong câu (sentence-scoped co-occurrence / OpenIE triples)*, sau đó chạy Personalized PageRank (PPR) hoặc phân cụm cộng đồng trên đồ thị lai.
  - **LightRAG (EMNLP/HKUDS 2024–2025)**: Chỉ ra rằng các cạnh nối giữa các thực thể chỉ có giá trị định tuyến khi chúng mang **ngữ cảnh cục bộ (local sentence context)** thay vì ghép cặp ngẫu nhiên toàn văn bản. Nếu ghép cặp mọi từ viết hoa trong cả đoạn văn theo thứ tự bảng chữ cái (`labels[0] --CO_OCCURS--> labels[1]`), đồ thị sẽ bị hiện tượng **"Hub/Alphabetical Bias"** (các token đứng đầu bảng chữ cái ASCII như `"0.75"`, `"2025-03-18"`, `"AI"` hút toàn bộ cạnh nối, trong khi các thực thể trung tâm đứng sau bảng chữ cái bị bỏ rơi).
  - **Ranh giới câu trong văn bản kỹ thuật/chính sách (Sentence Boundary Disambiguation - SBD)**: Trong tài liệu kỹ thuật/quản trị AI chứa nhiều số thập phân (`0.75`), số phiên bản (`v2.5`), và viết tắt (`e.g.`, `i.e.`, `U.S.`), việc tách câu bằng regex ngây thơ `[^.!?\n]+` sẽ phá hủy mệnh đề định lượng (như đã xảy ra với câu `"If the evidence score is below 0.75..."` bị cắt thành `"If the evidence score is below 0."`). Chuẩn công nghiệp yêu cầu sử dụng **Negative Lookbehind / Lookahead cho chữ số thập phân** (`(?<!\d)\.(?!\d)`) kết hợp bảo vệ từ viết tắt thông dụng.

### 1.2 Dung hợp Tín hiệu Đa phương thức cho Cây Định tuyến Cộng đồng (Dense + Sparse + Entity Fusion cho Leiden CPM)
- **Kiến thức SOTA**:
  - **Microsoft GraphRAG & Constrained Hierarchical Leiden CPM**: Thuật toán Leiden với Constant Potts Model (`CPMVertexPartition`) phân hoạch các `KnowledgeUnit` thành cây định tuyến (`RoutingNode` / `TreeRoutingProfile`) dựa trên trọng số cạnh $w_{ij}$.
  - Trong kiến trúc của `sag_api` (`knowledge_service.py:29`), trọng số cạnh được thiết kế chuẩn xác theo công thức đa tín hiệu:
    $$w_{ij} = \frac{\sum_{k \in \mathcal{O}} \alpha_k \cdot s_k(i, j)}{\sum_{k \in \mathcal{O}} \alpha_k}, \quad \alpha = \{\text{semantic}: 0.30,\ \text{entity}: 0.25,\ \text{lexical}: 0.20,\ \text{structure}: 0.15,\ \text{temporal}: 0.05,\ \text{citation}: 0.05\}$$
  - **Lỗ hổng thực tế trong dự án so với lý thuyết**:
    1. Vì `features_json["dense"]` bị để rỗng `[]`, tín hiệu quan trọng nhất là $\text{semantic}$ ($\alpha = 0.30$) luôn bằng `None`, đồng thời `dense_medoid` trên mọi nút của cây định tuyến (`tree_routing_profiles.dense_medoid_json`) cũng bằng `None` — biến cây định tuyến đa phương thức thành cây thuần từ vựng (lexical-only).
    2. Khi nối hai đoạn văn $i, j$ mà một đoạn có thực thể còn đoạn kia không có thực thể (`ea != ∅, eb == ∅`), công thức hiện tại gán `entity = 0.0` (quan sát được, trọng số $0.25$) thay vì `None` (chưa có thông tin thực thể ở đoạn kia). Điều này phạt oan $38.5\%$ tổng trọng số của các đoạn văn liền kề trong cùng một mục (như `p-5` và `p-6`), làm đứt gãy đồ thị (`degree = 0`).
  - **Giải pháp tiên tiến & tiết kiệm chi phí nhất (Zero Extra Embedding API Cost)**: Vì mỗi `KnowledgeUnit` đều ánh xạ tới một `CanonicalBlock` nằm trong một `SearchUnit` đã được nhúng bằng `Qwen/Qwen3-Embedding-0.6B` (1024 chiều) trên Qdrant (hoặc tính trực tiếp qua embedder), ta có thể nạp vector `dense` vào `KnowledgeUnit.features_json["dense"]` để kích hoạt ngay lập tức cả `signals["semantic"]` (30%) lẫn `NodeProfile.dense_medoid` mà không làm thay đổi kiến trúc bảng.

### 1.3 Bảo toàn Ngôn ngữ Nguồn trong Trích xuất LLM Đa ngôn ngữ (Cross-Lingual Contamination Prevention)
- **Kiến thức SOTA**:
  - Các mô hình họ `Qwen3` (`Qwen/Qwen3-8B` đang dùng trong dự án) có trọng số huấn luyện song ngữ Anh–Trung rất mạnh. Khi prompt hệ thống (`zleap-sag` prompt template) được đặt ở ngôn ngữ `"zh"`, mô hình sẽ tự động dịch các thực thể và sự kiện của tài liệu tiếng Anh/tiếng Việt sang tiếng Trung (`"Evidence Gate"` $\rightarrow$ `"证据门控"`), gây hiện tượng **Cross-Lingual Modality Gap** khi truy vấn bằng tiếng Anh.
  - Thực hành chuẩn: **Tự động phát hiện ngôn ngữ của chunk/tài liệu (Lightweight Script/Language Detection)** trước khi gọi `DataEngine.extract` (nếu tỷ lệ ký tự CJK $< 10\%$, chuyển ngôn ngữ prompt sang `"en"`, hoặc thêm chỉ thị bảo toàn ngôn ngữ gốc của văn bản).

### 1.4 Cô lập Đa Schema trong SQLAlchemy 2.x + `asyncpg` (Multi-Schema Coherence)
- **Kiến thức SOTA**:
  - Khi ứng dụng chính (`sag_api`) hỗ trợ định tuyến schema qua tham số URL `?search_path=<schema_name>` (`sag_api/core/db.py:34-42`), mọi engine phụ trợ kết nối tới cùng PostgreSQL database (như `zleap-sag` `RelationalConfig`) bắt buộc phải kế thừa cùng `search_path` (qua `server_settings={"search_path": ...}` hoặc sự kiện `connect` trên `sync_engine`), và cột khóa liên kết giữa hai bên (`sources.sag_source_config_id` $\leftrightarrow$ `data_source.id`) không được dùng kiểu cố định độn khoảng trắng `CHAR(36)` khi độ dài chuỗi là 35 ký tự (`tp_<32_hex>`).

---

## PHẦN 2: KẾ HOẠCH TRIỂN KHAI CHI TIẾT TỪNG TASK (ATOMIC IMPLEMENTATION PLAN)

### Phạm vi (Scope)
- **Trong phạm vi (In-Scope)**:
  - **Task 1 (`P0`)**: Nâng cấp bộ trích xuất `E0`/`E1` và công thức tín hiệu đồ thị trong `sag_api/services/knowledge_service.py` (sửa tách câu số thập phân `0.75`, mở rộng `_ENTITY_RE` bắt thực thể Title-Case & định lượng, sửa `E1` `CO_OCCURS` theo phạm vi câu, sửa phạt oan `entity = 0.0` khi một vế rỗng).
  - **Task 2 (`P0`)**: Đồng bộ `search_path` PostgreSQL giữa `sag_api` và `zleap-sag` trong `sag_api/sag/config_builder.py` & `sag_api/sag/engine_manager.py`, và xử lý triệt để lỗi độn khoảng trắng `CHAR(36)` của `data_source.id`.
  - **Task 3 (`P1`)**: Tự động phát hiện ngôn ngữ tài liệu (tránh ép cứng `sag_language = "zh"` cho tài liệu tiếng Anh/Việt) trong `sag_api/sag/incremental_processor.py` & `sag_api/core/config.py`.
  - **Task 4 (`P1`)**: Bơm vector `dense` (1024-d) vào `KnowledgeUnit.features_json["dense"]` trong `sag_api/jobs/knowledge.py` & `sag_api/services/knowledge_service.py` để kích hoạt tín hiệu `semantic` (`0.30`) và `dense_medoid`.
  - **Task 5 (`P1/P2`)**: Vệ sinh trạng thái `error_layer` / `error_stage` khi tài liệu đạt `READY` (`sag_api/jobs/tasks.py`, `sag_api/services/document_service.py`) và dọn dẹp / lọc các điểm Qdrant của phiên bản `SUPERSEDED` / `EXTRACTION_FAILED`.
- **Ngoài phạm vi (Out-of-Scope)**:
  - Không thay đổi schema API công khai (breaking API changes) của `sag_api`.
  - Không thay thế thư viện `zleap-sag` bằng framework bên thứ ba.

---

### Task 1 (P0): Sửa bộ trích xuất `E0`/`E1` và tín hiệu đồ thị trong `knowledge_service.py`

**Mục tiêu**: Đảm bảo 100% câu chứa số thập phân (`0.75`) không bị cắt gãy, nhận diện đầy đủ thực thể miền Title-Case (`Northwind Labs`, `Evidence Gate`, `Evidence Ledger`, `Access Control Service`, `Governance Team`, `50 answers`, `5 percent`), sinh quan hệ `E1` `CO_OCCURS` chính xác trong phạm vi từng câu theo thứ tự xuất hiện, và không phạt `entity = 0.0` khi một đoạn văn không có thực thể.

**Files cần sửa**:
- Modify: `SAG/apps/api/sag_api/services/knowledge_service.py:52-91, 105-189, 518-522`
- Test: `SAG/apps/api/tests/test_knowledge_service_e0_e1.py`

**Chi tiết thay đổi code đề xuất**:

1. **Cập nhật `_ENTITY_RE`, `_SENTENCE_RE`, và `terms()` (`knowledge_service.py:52-91`)**:
```python
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:[^.!?\n]|\b\d+\.\d+\b|\b(?:v|ver|no|fig|eq|dr|mr|mrs|ms|vs|etc|e\.g|i\.e)\.)+(?:[.!?](?!\d)|$)",
    re.IGNORECASE,
)
_LEADING_STOP_ENTITIES = frozenset({
    "A", "An", "And", "As", "At", "Be", "Before", "After", "By", "Each", "Every",
    "For", "From", "If", "In", "Into", "It", "Its", "No", "Not", "Of", "On", "Or",
    "Some", "Such", "That", "The", "Their", "Then", "There", "These", "This",
    "Those", "To", "Under", "When", "Where", "While", "With", "Without",
})
_ENTITY_RE = re.compile(
    r"(?P<url>https?://\S+)"
    r"|(?P<email>\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b)"
    r"|(?P<ipv4>\b(?:\d{1,3}\.){3}\d{1,3}\b)"
    r"|(?P<uuid>\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b)"
    r"|(?P<date>\b\d{4}-\d{2}-\d{2}\b)"
    r"|(?P<hash>\bSHA-(?:1|224|256|384|512)\b)"
    r"|(?P<quantity>\b\d+(?:\.\d+)?\s*(?:percent|%|answers|citations|days|hours|seconds|ms|bytes|kb|mb|gb|tb)\b)"
    r"|(?P<version>\bv?\d+\.\d+(?:\.\d+)*\b)"
    r"|(?P<title_phrase>\b[A-Z][A-Za-z0-9_-]+(?:\s+[A-Z][A-Za-z0-9_-]+){1,4}\b)"
    r"|(?P<code_token>\b(?:[A-Z]{2,}[A-Z0-9_-]*|[A-Za-z_][A-Za-z0-9_]*::[A-Za-z0-9_:]+|/[A-Za-z0-9._/-]{2,})\b)"
)
```
Trong vòng lặp khớp `_ENTITY_RE` (`knowledge_service.py:113-127`), nếu nhóm khớp là `title_phrase` và từ đầu tiên nằm trong `_LEADING_STOP_ENTITIES` (ví dụ `"The Evidence Ledger"` hoặc `"Before Retrieval"`), tự động cắt bỏ hư từ đầu câu (`"The "` $\rightarrow$ `"Evidence Ledger"`) và cập nhật lại `start = match.start() + offset` để bảo toàn tuyệt đối bất biến `unit.text[start:end] == quote`!

2. **Sửa tách câu `claim` và sinh quan hệ `E1` `CO_OCCURS` theo phạm vi câu (`knowledge_service.py:128-167`)**:
- Thay `re.finditer(r"[^.!?\n]+(?:[.!?]|$)", text)` bằng `_SENTENCE_SPLIT_RE.finditer(text)`.
- Thay vì lấy `labels = sorted(...)` trên toàn đoạn rồi chỉ ghép `labels[0]` với `labels[1]`, duyệt qua từng câu đã tách: tìm các thực thể có span `[e_start, e_end]` nằm trong phạm vi câu `[s_start, s_end]` theo đúng thứ tự xuất hiện và tạo các quan hệ `CO_OCCURS` giữa các thực thể kế cận trong cùng câu đó (neo `quote`, `start`, `end` vào đúng câu chứa cặp thực thể đó, tuân thủ giới hạn `config.max_evidence_per_unit`).

3. **Sửa công thức tín hiệu `entity` trong `graph_payload` (`knowledge_service.py:518-520`)**:
```python
ea, eb = set(af["entities"]), set(bf["entities"])
if ea and eb:
    weights = {e: math.log(1 + len(rows) / entity_df[e]) for e in sorted(ea | eb)}
    entity = math.fsum(weights[e] for e in sorted(ea & eb)) / math.fsum(weights.values())
else:
    entity = None
```
Khi một trong hai đoạn văn không chứa thực thể nào (`not ea or not eb`), đặt `entity = None` (không quan sát được) thay vì `0.0`, giúp các đoạn văn liền kề trong cùng một mục không bị phạt oan 25% trọng số.

---

### Task 2 (P0): Đồng bộ PostgreSQL Schema (`search_path`) cho `zleap-sag` và xử lý khoảng trắng `CHAR(36)`

**Mục tiêu**: Xóa bỏ tình trạng "Split-Brain Schema" giữa `sag_api` và `zleap-sag` khi `DATABASE_URL` có `?search_path=...`, và ngăn lỗi lệch khoảng trắng của `sag.data_source.id`.

**Files cần sửa**:
- Modify: `SAG/apps/api/sag_api/sag/config_builder.py:66-77`
- Modify: `SAG/apps/api/sag_api/sag/engine_manager.py:563-636, 658-675`
- Test: `SAG/apps/api/tests/test_sag_schema_alignment.py`

**Chi tiết thay đổi code đề xuất**:
1. Trong `sag_api/sag/engine_manager.py`:
   - Viết hàm helper `_extract_pg_search_path(database_url: str) -> str | None` (tương tự `sag_api/core/db.py:34-41`).
   - Ngay khi khởi tạo `DataEngine` trong `_slot()` (`engine_manager.py:658-670`), trước khi gọi `await self._ensure_engine_schema(engine)` và `await engine.start()`, nếu `search_path` khác `None`, gắn listener `"connect"` lên `engine.resources.relational._engine.sync_engine` (hoặc truyền `server_settings={"search_path": search_path}`) để mọi kết nối của `zleap-sag` luôn dùng đúng schema của `sag_api`.
2. Trong `_ensure_engine_schema(self, engine: DataEngine)` (`engine_manager.py:563-582`):
   - Sau `await engine.init_schema()`, trên PostgreSQL thực thi câu lệnh idempotent chuyển cột `data_source.id` và các cột `data_source_id` từ `CHAR(36)` sang `VARCHAR(64)` (hoặc `RTRIM` các giá trị cũ bị độn khoảng trắng):
     ```sql
     ALTER TABLE data_source ALTER COLUMN id TYPE VARCHAR(64) USING RTRIM(id);
     ```
     Điều này giúp khóa `tp_<32_hex>` (35 ký tự) khớp 100% giữa `sources.sag_source_config_id` và `data_source.id`.

---

### Task 3 (P1): Tự động nhận diện ngôn ngữ văn bản cho `zleap-sag` (`incremental_processor.py`)

**Mục tiêu**: Ngăn chặn việc tài liệu tiếng Anh/tiếng Việt bị `zleap-sag` dịch sang sự kiện và thực thể tiếng Trung do `sag_language` mặc định là `"zh"`.

**Files cần sửa**:
- Modify: `SAG/apps/api/sag_api/core/config.py:161`
- Modify: `SAG/apps/api/sag_api/sag/incremental_processor.py:65-75, 290-330`
- Test: `SAG/apps/api/tests/test_incremental_processor_language.py`

**Chi tiết thay đổi code đề xuất**:
1. Trong `sag_api/core/config.py:161`:
   - Cho phép `sag_language: str = "auto"` (hoặc giữ tương thích cấu hình hiện tại nhưng nếu không cấu hình rõ trong env thì tự động phát hiện theo nội dung tài liệu).
2. Trong `sag_api/sag/incremental_processor.py`:
   - Thêm hàm `_detect_document_language(text: str, configured: str) -> str`:
     ```python
     def _detect_document_language(text: str, configured: str | None) -> str:
         cfg = (configured or "").strip().lower()
         if cfg and cfg not in {"auto", "zh"}:
             return cfg
         sample = text[:4000]
         if not sample.strip():
             return cfg or "en"
         cjk_count = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
         alpha_count = sum(1 for ch in sample if ch.isalpha())
         if alpha_count > 0 and (cjk_count / alpha_count) >= 0.15:
             return "zh"
         return "en"
     ```
   - Gọi `_apply_language_to_prompt_manager(detected_lang)` trước khi chạy `DataEngine.extract` trên các chunks của tài liệu.

---

### Task 4 (P1): Bổ sung Vector `dense` cho `KnowledgeUnit` để kích hoạt `semantic` (30%) và `dense_medoid`

**Mục tiêu**: Khắc phục việc `KnowledgeUnit.features_json["dense"]` luôn bằng `[]` khiến tín hiệu `semantic` (`0.30`) trong `knowledge_unit_edges` và `dense_medoid_json` trong `tree_routing_profiles` luôn bằng `null`.

**Files cần sửa**:
- Modify: `SAG/apps/api/sag_api/services/knowledge_service.py:287-360`
- Modify: `SAG/apps/api/sag_api/jobs/knowledge.py:458-467`
- Test: `SAG/apps/api/tests/test_knowledge_dense_features.py`

**Chi tiết thay đổi code đề xuất**:
- Trong `rebuild_knowledge_version` (`knowledge_service.py:287-360`), cho phép nhận tham số tùy chọn `dense_by_block_id: Mapping[str, Sequence[float]] | None = None` (hoặc `embedder`).
- Khi `KnowledgeWorker` chạy job `FOUNDATION` (`jobs/knowledge.py:458-463`), nếu có `embedder` (hoặc lấy từ vector của `SearchUnit` chứa `block.id` trên Qdrant), điền vector chuẩn hóa vào `features["dense"]` trước khi tính `unit_checksum`. Khi `features["dense"]` có dữ liệu, hàm `_cosine` (`knowledge_service.py:400`) và `_profile` (`routing_tree_service.py:178`) sẽ tự động tính `signals["semantic"]` và `dense_medoid` mà không cần sửa cấu trúc bảng!

---

### Task 5 (P1/P2): Vệ sinh trạng thái tài liệu (`error_layer`/`error_stage`) & Ngăn rò rỉ dữ liệu `PAUSED` / `SUPERSEDED`

**Mục tiêu**:
1. Xóa sạch `error_layer = None` và `error_stage = None` khi tài liệu hoàn tất (`READY`) hoặc được `resume`/`reprocess`.
2. Loại bỏ các tài liệu đang `PAUSED` khỏi tập kết quả tìm kiếm hoạt động và xóa các điểm Qdrant của phiên bản cũ khi bị `SUPERSEDED`.

**Files cần sửa**:
- Modify: `SAG/apps/api/sag_api/jobs/tasks.py:259-262, 672-681`
- Modify: `SAG/apps/api/sag_api/services/document_service.py:259-263, 600`
- Modify: `SAG/apps/api/sag_api/services/search_unit_retrieval_service.py:277-280`
- Modify: `SAG/apps/api/sag_api/services/dedup_and_temporal_service.py` & `search_index_service.py:764-771`
- Test: `SAG/apps/api/tests/test_document_hygiene_and_visibility.py`

**Chi tiết thay đổi code đề xuất**:
1. Tại `jobs/tasks.py:259-262` và `672-681` (cũng như `document_service.py:259-263, 600`), bổ sung `error_layer=None, error_stage=None` vào câu lệnh `update(Document).values(...)` khi xóa `error=None`.
2. Tại `search_unit_retrieval_service.py:278`, siết điều kiện lọc `Document.status == DocumentStatus.READY` (hoặc loại trừ thêm `DocumentStatus.PAUSED`, `DocumentStatus.PAUSING`, `DocumentStatus.FAILED`) để các tài liệu đang tạm dừng (`PAUSED`) không làm nhiễm bẩn 96% chỉ mục tìm kiếm của project.
3. Tại `search_index_service.py:764-771`, sau khi `resolve_temporal_supersedes` đánh dấu các phiên bản cũ là `SUPERSEDED`, gọi `delete_document_points_from_qdrant` cho các `superseded_version_ids` để tránh rác vector trên Qdrant.

---

## PHẦN 3: QUY TRÌNH KIỂM CHỨNG & REBUILD DỮ LIỆU SAU KHI FIX (VALIDATION CHECKLIST)

Sau khi áp dụng 5 Task trên và chạy bộ unit test (`pytest`), thực hiện quy trình xác nhận trên dữ liệu thật của project `6ad61cb2e1f989c367a1eb46`:

- [ ] **Bước 1 (Unit & Regression Tests)**: Chạy `pytest` cho toàn bộ test mới và bộ test hiện có của `knowledge_service`, `routing_tree_service`, `search_unit_retrieval_service`, và `engine_manager`.
- [ ] **Bước 2 (Rebuild Knowledge & Reprocess Document cho `sag-metric-partition-universe.md`)**:
  - Chạy lại `rebuild_knowledge_version` cho `document_version_id = '9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2'` và `rebuild_project_universe_tree` cho project `6ad61cb2e1f989c367a1eb46`.
- [ ] **Bước 3 (Truy vấn SQL Xác minh Độc lập)**:
  1. Kiểm tra không còn `claim` nào bị cắt tại `0.`:
     ```sql
     SELECT id, payload_json->>'quote' AS quote
     FROM "sag_metric_e2e_67a1eb46".knowledge_evidence
     WHERE kind = 'claim' AND payload_json->>'quote' LIKE '%below 0.';
     -- Kỳ vọng: 0 dòng (trước khi sửa: 1 dòng 'ac6ec2da-...')
     ```
  2. Kiểm tra cả 5 `knowledge_units` đều có thực thể miền và không còn quan hệ `("0.75", "CO_OCCURS", "2025-03-18")`:
     ```sql
     SELECT ordinal, features_json->'entities' AS entities, features_json->>'importance' AS importance
     FROM "sag_metric_e2e_67a1eb46".knowledge_units
     WHERE document_version_id = '9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2' AND is_current = TRUE
     ORDER BY ordinal;
     -- Kỳ vọng: cả 5/5 dòng đều có jsonb_array_length(entities) >= 1
     ```
  3. Kiểm tra không còn tài liệu `READY` nào mang `error_layer` / `error_stage` khác `NULL`:
     ```sql
     SELECT id, status, error, error_layer, error_stage
     FROM "sag_metric_e2e_67a1eb46".documents
     WHERE status = 'READY' AND (error_layer IS NOT NULL OR error_stage IS NOT NULL);
     -- Kỳ vọng: 0 dòng
     ```
  4. Kiểm tra các sự kiện của `sag-metric-partition-universe.md` sau khi reprocess được trích xuất bằng tiếng Anh thay vì tiếng Trung.
