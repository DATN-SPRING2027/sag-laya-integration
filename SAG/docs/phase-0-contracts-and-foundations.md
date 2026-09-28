# Đặc Tả Kiến Trúc & Hợp Đồng Dữ Liệu Nền Tảng (Phase 0 — Contracts & Foundations)
## SAG Knowledge Routing RAG • Baseline Research & Implementation Contracts

> **Jira Ticket**: [DATN-23](https://trankimthang0207.atlassian.net/browse/DATN-23)  
> **Người thực hiện**: Phan Thành Tài (Tài - DE190491)  
> **Tài liệu tham chiếu chuẩn**: [SAG_Knowledge_Routing_RAG_Workflow_v1.1.md](SAG_Knowledge_Routing_RAG_Workflow_v1.1.md)  
> **Kế hoạch & Checklist tác nghiệp**: [plan.md](../tasks/plan.md) | [todo.md](../tasks/todo.md)  
> **Repository**: `DATN-SPRING2027/sag-laya-integration`  
> **Trạng thái**: Draft / In-Review (Phase 0 Foundation Baseline)  

---

## 1. Chuẩn Phân Loại Bằng Chứng & Phân Định Trách Nhiệm (Truth Grading)

Tài liệu này tuân thủ chuẩn phân loại bằng chứng độc lập, không chấp nhận mô tả phỏng đoán:
- `[FACT / VERIFIED]`: Đã đối chiếu và kiểm chứng trực tiếp từ mã nguồn thực tế tại repository `sag-laya-integration` và các file cấu hình Docker/Compose.
- `[IMPLEMENTED]`: Logic nghiệp vụ đã được code hoàn chỉnh, có test suite đi kèm và đang chạy thực tế.
- `[PARTIAL]`: Đã có hạ tầng nền tảng (ORM model, adapter, enum hoặc route) nhưng luồng nghiệp vụ end-to-end chưa hoàn chỉnh.
- `[GAP]`: Yêu cầu bắt buộc trong đặc tả `Workflow v1.1` nhưng hiện tại codebase hoàn toàn chưa có.
- `[DESIGN / PROPOSED]`: Hợp đồng kiến trúc chuẩn hóa được đề xuất trong tài liệu này cho Phase 0 để các Phase 1–11 tuân theo.
- `[DECISION REQUIRED]`: Điểm rẽ kiến trúc hoặc chính sách vận hành cần sự thống nhất của Leader/Nhóm.

---

## 2. Bối Cảnh Nghiệp Vụ & Ranh Giới Hệ Thống

### 2.1. Sự Dịch Chuyển Từ Classic RAG Sang Knowledge Routing RAG
Mô hình Classic RAG truyền thống ("naive chunking $\rightarrow$ flat embedding $\rightarrow$ top-K vector search") bộc lộ các điểm yếu nghiêm trọng trong môi trường doanh nghiệp:
1. Chi phí tìm kiếm cao và thiếu ổn định (latency p95 dao động lớn do phải rerank hàng trăm chunk).
2. Dễ đứt gãy ngữ cảnh khi tài liệu có cấu trúc phân cấp phức tạp (Heading hierarchy, bảng biểu, liên kết thực thể).
3. Thiếu khả năng kiểm soát độ trôi (topic drift) và lịch sử phiên bản tài liệu.

Kiến trúc **Knowledge Routing RAG** định hình lại toàn bộ quy trình:
- **Ingestion Lane (Đắt một lần)**: Chuẩn hóa canonical blocks, kiểm soát duplicate/temporal, tạo Search Units phục vụ hybrid search, trích xuất Knowledge Units đa tín hiệu và xây dựng Cây Tri Thức (**Knowledge Routing Tree**).
- **Query Lane (Rẻ nhiều lần)**: Truy vấn trước hết đi qua bộ phân loại Coarse Intent (Laya Local), trích xuất đặc trưng câu hỏi (Deterministic Features), định tuyến qua cây tri thức để thu hẹp không gian tìm kiếm về cụm module cục bộ (Branch-Local Hybrid Search), và chỉ mở rộng sang đồ thị hoặc gọi LLM khi cần thiết.

### 2.2. Ranh Giới Sở Hữu Dữ Liệu (Data Ownership Boundary)
Theo kết luận tại [data-ownership-and-storage.md](data-ownership-and-storage.md):
- **Continuum Backend (`DATN-BE` / MongoDB)**: Là Source of Truth cho toàn bộ danh tính nghiệp vụ: Users, Organizations, Projects, Teams, Memberships, Roles và Permissions.
- **SAG (`sag-laya-integration` / PostgreSQL + Qdrant)**:
  - **PostgreSQL 16**: Là **Source of Truth** cho toàn bộ metadata tri thức: Documents, Versions, Snapshots, Ingestion Runs, Canonical Blocks, Graph Edges, Tree Manifests và Audit Lineage.
  - **Qdrant**: Là **Search Accelerator** (lưu dense + sparse vectors và payload filter). Mọi collection trong Qdrant phải có khả năng tái tạo (rebuildable) hoàn toàn từ PostgreSQL và file gốc.
  - SAG không tự quản lý user/project toàn cục. SAG nhận các assertion header đã được xác thực từ BE (`X-Continuum-Project-Id`, `X-Continuum-Security-Partitions`).

---

## 3. Khảo Sát Hiện Trạng Codebase & Ma Trận Phân Tích Gap (Pillar 1)

### 3.1. Đối Chiếu Từng Module Mã Nguồn Thực Tế

| Module / File trong `apps/api/sag_api/` | Hiện trạng thực tế (`[FACT]`) | Yêu cầu mục tiêu trong `Workflow v1.1` | Đánh giá & Khoảng cách (`[GAP]`) |
| :--- | :--- | :--- | :--- |
| `db/models/document.py` | Bảng `documents` phẳng (`id`, `source_id`, `filename`, `storage_path`, `status`, `chunk_count`, `progress`). Không có versioning, không hash. | Cần tách thực thể: `Document` (logical entity) và `DocumentVersion` (immutable revision có `file_hash`, `supersedes_id`, temporal timestamps). | `[GAP]` Rất lớn. Đang ghi đè trạng thái tài liệu khi upload lại; không lưu lịch sử thay đổi phiên bản. |
| `db/models/job.py` | Bảng `jobs` (`id`, `type`, `status`, `source_id`, `document_id`, `progress`, `payload_json`, `error`). | Cần `IngestionRun` gắn `idempotency_key`, kèm bảng con `stage_runs` lưu chi tiết độ trễ, logs và kết quả từng stage. | `[PARTIAL]` Đã có polling job cơ bản nhưng thiếu idempotency key ở mức DB và không lưu chi tiết các stage trung gian. |
| `enums.py` | Enum `DocumentStatus`: `PENDING`, `LOADING`, `EXTRACTING`, `PAUSING`, `PAUSED`, `READY`, `FAILED`. | Tách biệt hai trạng thái capability: `SEARCH_READY` (xong search index) và `KNOWLEDGE_READY` (xong tree & graph). | `[GAP]` Nghiêm trọng. Hiện tại chỉ có `READY`. Khi enrichment (extract/graph/tree) lỗi hoặc chậm thì tài liệu không thể tìm kiếm được. |
| `api/v1/documents.py` | Route `POST /sources/{id}/documents` ghi file đĩa với UUID random (`new_id()`), tạo record `Document` và đẩy `Job`. | Phải stream tính SHA-256 trước khi ghi đĩa; kiểm tra duplicate policy; tạo `SourceSnapshot` và `DocumentVersion` trong 1 transaction. | `[GAP]` Không có idempotency key từ client; không có kiểm tra duplicate theo hash; tạo file rác nếu client retry. |
| `sag/qdrant_store.py` | Adapter Qdrant qua HTTP, hỗ trợ vector query và filter cơ bản (`eq`, `in`, `range`). Collection cấu hình theo string. | Hỗ trợ Sparse + Dense representation; payload schema lưu `security_partition_id`, `valid_from/to`; index manifest verification. | `[PARTIAL]` Adapter kết nối tốt, nhưng thiếu payload schema chuẩn cho multi-tenancy và chưa có sparse vector support. |
| `services/laya_router.py` | Đã triển khai singleton cache, route coarse intent `CHAT`, `KNOWLEDGE`, `COMMAND`, `AMBIGUOUS`. Ngưỡng CHAT $\ge 0.65$. | Hợp đồng trả về ổn định, tích hợp vào Query Strategy Planner để điều phối các chế độ truy vấn. | `[IMPLEMENTED]` Đã hoàn thành xuất sắc trong Phase 3 PR #7. Cần bảo tồn hợp đồng này. |
| `services/query_analysis.py` | Đã có regex trích xuất `exact_terms`, `identifier_terms`, `path_terms`, `temporal_cues`, `relation_cues`, `global_cues`. | Là đầu vào trực tiếp cho Query Strategy Planner để sinh ra `QueryPlan` kèm reason codes. | `[IMPLEMENTED]` Đã có parser cơ bản. Cần chuẩn hóa thành Pydantic Schema `QueryFeatures`. |
| `db/models/universe.py` | Bảng `universe_overviews`, `universe_partitions` phục vụ 3D visual projection. | Dữ liệu cây tri thức phục vụ định tuyến truy vấn (`knowledge_nodes`, `node_routing_profiles`, `tree_manifests`). | `[GAP]` Bảng universe phục vụ hiển thị 3D Three.js, chưa phải là Knowledge Routing Tree có ràng buộc thuật toán Leiden. |

---

## 4. Hợp Đồng Dữ Liệu & Thực Thể Cơ Sở Dữ Liệu (Pillar 2)

### 4.1. Cấu Trúc Thực Thể Mới Trên PostgreSQL 16
Hệ thống Phase 0 chuẩn hóa mô hình dữ liệu quan hệ, chuyển từ kiến trúc phẳng sang kiến trúc đa tầng kế thừa bất biến:

```
┌────────────────────────────────────────────────────────────────────────┐
│                              documents                                 │
│  - id: UUID (Primary Key, Stable)                                      │
│  - tenant_id: VARCHAR(64) (Multi-tenant partition)                     │
│  - project_id: VARCHAR(64) (Continuum Project Scope)                   │
│  - owner_id: VARCHAR(64) (Continuum User ID)                           │
│  - logical_source_id: VARCHAR(128) (Tên logic hoặc path nguồn)         │
│  - created_at: TIMESTAMPTZ                                             │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │ 1
                                   │
                                   │ N
┌──────────────────────────────────▼─────────────────────────────────────┐
│                          document_versions                             │
│  - id: UUID (Primary Key, Stable UUIDv5)                               │
│  - document_id: UUID (FK -> documents.id)                              │
│  - version_no: INTEGER (1, 2, 3...)                                    │
│  - file_hash: VARCHAR(64) (SHA-256 checksum)                           │
│  - supersedes_id: UUID (FK -> document_versions.id, Nullable)          │
│  - source_published_at: TIMESTAMPTZ (Ngày phát hành gốc)               │
│  - observed_at: TIMESTAMPTZ (Thời điểm hệ thống phát hiện)             │
│  - ingested_at: TIMESTAMPTZ (Thời điểm nạp hoàn tất)                  │
│  - valid_from: TIMESTAMPTZ (Thời điểm bắt đầu có hiệu lực)             │
│  - valid_to: TIMESTAMPTZ (Thời điểm hết hiệu lực, Default: 9999-12-31) │
│  - status: VARCHAR(32) (RECEIVED, PARSED, SEARCH_READY...)             │
│  - search_status: VARCHAR(32) (PENDING, INDEXING, SEARCH_READY, FAILED)│
│  - knowledge_status: VARCHAR(32) (NOT_STARTED, ..., FAILED_RETRYABLE)  │
│  - search_ready_at: TIMESTAMPTZ                                        │
│  - knowledge_ready_at: TIMESTAMPTZ                                     │
└──────────────────┬──────────────────────────────────┬──────────────────┘
                   │ 1                                │ 1
                   │                                  │
                   │ 1                                │ N
┌──────────────────▼───────────────┐  ┌───────────────▼──────────────────┐
│         source_snapshots         │  │          ingestion_runs          │
│  - id: UUID (Primary Key)        │  │  - id: UUID (Primary Key)        │
│  - document_version_id: UUID (FK)│  │  - tenant_id: VARCHAR(64)        │
│  - storage_uri: VARCHAR(1024)    │  │  - project_id: VARCHAR(64)       │
│  - original_filename: VARCHAR    │  │  - document_version_id: UUID (FK)│
│  - mime_type: VARCHAR(128)       │  │  - idempotency_key: VARCHAR(64)  │
│  - byte_size: BIGINT             │  │  - current_stage: VARCHAR(32)    │
│  - checksum_sha256: VARCHAR(64)  │  │  - status: VARCHAR(32)           │
│  - created_at: TIMESTAMPTZ       │  │  - attempt_count: INTEGER        │
└──────────────────────────────────┘  │  - error_layer: VARCHAR(32)      │
                                      │  - error_code: VARCHAR(64)       │
                                      │  - started_at, completed_at      │
                                      └──────────────────┬───────────────┘
                                                         │ 1
                                                         │ N
                                      ┌──────────────────▼───────────────┐
                                      │            stage_runs            │
                                      │  - id: UUID (Primary Key)        │
                                      │  - run_id: UUID (FK)             │
                                      │  - stage: VARCHAR(32)            │
                                      │  - status: VARCHAR(32)           │
                                      │  - duration_ms: FLOAT            │
                                      │  - metrics_json: JSONB           │
                                      │  - error_message: TEXT           │
                                      └──────────────────────────────────┘
```

### 4.2. Mã DDL Chi Tiết (PostgreSQL 16 DDL Scripts)

```sql
-- 1. Bảng logical document
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(64) NOT NULL,
    logical_source_id VARCHAR(128) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_documents_tenant_project_logical_source UNIQUE (tenant_id, project_id, logical_source_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_project ON documents (tenant_id, project_id);

-- 2. Bảng phiên bản tài liệu
CREATE TABLE IF NOT EXISTS document_versions (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    supersedes_id UUID REFERENCES document_versions(id) ON DELETE SET NULL,
    source_published_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ingested_at TIMESTAMPTZ,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMPTZ NOT NULL DEFAULT '9999-12-31 23:59:59+00',
    status VARCHAR(32) NOT NULL DEFAULT 'RECEIVED',
    search_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    knowledge_status VARCHAR(32) NOT NULL DEFAULT 'NOT_STARTED',
    search_ready_at TIMESTAMPTZ,
    knowledge_ready_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_document_versions_doc_ver UNIQUE (document_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_hash ON document_versions (file_hash);
CREATE INDEX IF NOT EXISTS idx_document_versions_temporal ON document_versions (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_document_versions_search_status ON document_versions (search_status);
CREATE INDEX IF NOT EXISTS idx_document_versions_knowledge_status ON document_versions (knowledge_status);

-- 3. Bảng snapshot file gốc
CREATE TABLE IF NOT EXISTS source_snapshots (
    id UUID PRIMARY KEY,
    document_version_id UUID NOT NULL UNIQUE REFERENCES document_versions(id) ON DELETE CASCADE,
    storage_uri VARCHAR(1024) NOT NULL,
    original_filename VARCHAR(512) NOT NULL,
    mime_type VARCHAR(128) NOT NULL,
    byte_size BIGINT NOT NULL,
    checksum_sha256 VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 4. Bảng vòng chạy nạp tài liệu (Ingestion Run)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id UUID PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    document_version_id UUID NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(64) NOT NULL,
    current_stage VARCHAR(32) NOT NULL DEFAULT 'RECEIVE',
    status VARCHAR(32) NOT NULL DEFAULT 'QUEUED',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_layer VARCHAR(32),
    error_stage VARCHAR(32),
    error_code VARCHAR(64),
    error_message TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ingestion_runs_tenant_project_idempotency UNIQUE (tenant_id, project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_tenant_project ON ingestion_runs (tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status ON ingestion_runs (status);

-- 5. Bảng chi tiết từng stage thực thi
CREATE TABLE IF NOT EXISTS stage_runs (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    stage VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    duration_ms FLOAT NOT NULL DEFAULT 0.0,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_stage_runs_run_stage ON stage_runs (run_id, stage);

-- 6. Bảng khối văn bản chuẩn hóa (Canonical Blocks)
CREATE TABLE IF NOT EXISTS canonical_blocks (
    id UUID PRIMARY KEY,
    document_version_id UUID NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    block_type VARCHAR(32) NOT NULL, -- paragraph, heading, table, code, list
    page_from INTEGER NOT NULL,
    page_to INTEGER NOT NULL,
    section_path VARCHAR(512) NOT NULL, -- "1.1 > Kiến trúc > Storage"
    source_anchor VARCHAR(256),
    normalized_text TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_canonical_blocks_ordinal UNIQUE (document_version_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_canonical_blocks_hash ON canonical_blocks (content_hash);

-- 7. Bảng đơn vị tìm kiếm (Search Units)
CREATE TABLE IF NOT EXISTS search_units (
    id UUID PRIMARY KEY,
    document_version_id UUID NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    block_from_id UUID NOT NULL REFERENCES canonical_blocks(id),
    block_to_id UUID NOT NULL REFERENCES canonical_blocks(id),
    security_partition_id VARCHAR(64) NOT NULL DEFAULT 'public',
    content_hash VARCHAR(64) NOT NULL,
    token_count INTEGER NOT NULL,
    page_from INTEGER NOT NULL,
    page_to INTEGER NOT NULL,
    section_path VARCHAR(512) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_search_units_version ON search_units (document_version_id);
CREATE INDEX IF NOT EXISTS idx_search_units_security ON search_units (security_partition_id);

-- 8. Bảng quản lý trạng thái search của project (Blue-Green dual pointer)
CREATE TABLE IF NOT EXISTS project_search_state (
    project_id VARCHAR(64) PRIMARY KEY,
    slot_a_tree_version VARCHAR(64),
    slot_b_tree_version VARCHAR(64),
    active_routing_slot VARCHAR(16) NOT NULL DEFAULT 'SLOT_A', -- SLOT_A hoặc SLOT_B
    active_tree_version VARCHAR(64),
    previous_tree_version VARCHAR(64),
    active_search_epoch BIGINT NOT NULL DEFAULT 1,
    last_switched_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 9. Bảng Tree Manifest kiểm soát chất lượng cây tri thức
CREATE TABLE IF NOT EXISTS tree_manifests (
    tree_version VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(64) NOT NULL,
    config_version VARCHAR(32) NOT NULL,
    node_count INTEGER NOT NULL,
    leaf_count INTEGER NOT NULL,
    max_leaf_size INTEGER NOT NULL,
    giant_ratio FLOAT NOT NULL,
    routing_recall_at_k FLOAT NOT NULL,
    escape_win_rate FLOAT NOT NULL,
    acl_blackhole_rate FLOAT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'INACTIVE', -- ACTIVE, INACTIVE, REJECTED
    checksum VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tree_manifests_project ON tree_manifests (project_id, status);
```

### 4.3. Công Thức Sinh Định Danh Bất Biến (Deterministic Stable ID Formulas)
Mọi ID thực thể phải được sinh theo công thức tất định để đảm bảo khả năng tái tạo:
1. **Document ID**:
   $$\text{doc\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:doc:\{tenant\_id\}:\{project\_id\}:\{logical\_source\_id\}"})$$
2. **Document Version ID**:
   $$\text{version\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:ver:\{doc\_id\}:\{version\_no\}"})$$
3. **Canonical Block ID**:
   $$\text{block\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:block:\{version\_id\}:\{ordinal\}:\{content\_hash\}"})$$
4. **Search Unit ID & Qdrant Point ID**:
   $$\text{unit\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:unit:\{version\_id\}:\{ordinal\}"})$$
   $$\text{point\_id} = \text{UUIDv5}(\text{NAMESPACE\_URL}, f\text{"sag:qdrant:search\_units:\{unit\_id\}"})$$
5. **Idempotency Key Engine**:
   $$\text{idempotency\_key} = \text{SHA-256}(f\text{"\{tenant\_id\}:\{project\_id\}:\{file\_hash\}:\{client\_token\}"})$$

---

## 5. Ngữ Nghĩa Trạng Thái Tách Rời: Search Ready vs. Knowledge Ready (Pillar 3)

### 5.1. Mô Hình State Machine Hai Làn (Two-Lane Pipeline Machine)

```
                       [HTTP Upload]
                             │
                             ▼
                        ┌──────────┐
                        │ RECEIVED │
                        └────┬─────┘
                             │ (Validate MIME, Hash, Snapshot)
                             ▼
                        ┌──────────┐
                        │  PARSED  │
                        └────┬─────┘
                             │ (Extract Canonical Blocks)
                             ▼
                        ┌──────────┐
                        │ DEDUPED  │
                        └────┬─────┘
                             │ (Exact/Near Dedup, Link Supersedes)
                             ▼
                        ┌──────────┐
                        │ INDEXING │
                        └────┬─────┘
                             │ (Qdrant Dense/Sparse + Verify Manifest)
                             ▼
                  =========================
                  ★ STATUS: SEARCH_READY ★  <── [CỔNG KIỂM TRA A: NGƯỜI DÙNG TÌM KIẾM ĐƯỢC]
                  =========================
                             │
            ┌────────────────┴────────────────┐
            ▼ (Nhánh Async Background)        ▼ (Phục vụ User ngay)
    ┌──────────────────────┐            [Global Hybrid Retrieval]
    │ KNOWLEDGE_EXTRACTING │
    └──────────┬───────────┘
               │ (E0/E1 + selective E2 LLM)
               ▼
    ┌──────────────────────┐
    │    GRAPH_BUILDING    │
    └──────────┬───────────┘
               │ (Calibrated Multi-signal edges)
               ▼
    ┌──────────────────────┐
    │    TREE_ASSIGNING    │
    └──────────┬───────────┘
               │ (Constrained Leiden + Manifest Gate)
               ▼
    ============================
    ★ STATUS: KNOWLEDGE_READY ★ <── [CỔNG KIỂM TRA B: TREE ROUTING SẴN SÀNG]
    ============================
```

### 5.2. Nguyên Tắc Cách Ly Sự Cố (Failure Isolation Principles)
1. **Nguyên tắc Độc lập Năng lực**: Làn Ingestion đến `SEARCH_READY` chỉ thực thi các tác vụ deterministic (Parse $\rightarrow$ Chunk $\rightarrow$ Hash $\rightarrow$ Embed $\rightarrow$ Qdrant Upsert). Không có LLM phụ thuộc ở làn này. Cột `search_status` phản ánh trạng thái làn tìm kiếm độc lập (`PENDING` $\rightarrow$ `INDEXING` $\rightarrow$ `SEARCH_READY` hoặc `FAILED`).
2. **Không Khóa Chức Năng**: Khi nhánh làm giàu tri thức gặp lỗi (LLM quá tải, timeout, Leiden graph không hội tụ), `search_status` của `document_versions` **vẫn giữ nguyên `SEARCH_READY`**, chỉ chuyển cờ `knowledge_status = FAILED_RETRYABLE` (hoặc `FAILED_FATAL`). Cột tổng quan `status` vẫn báo hiệu tài liệu đã sẵn sàng phục vụ tìm kiếm với chế độ Hybrid Fallback, không làm gián đoạn trải nghiệm người dùng.
3. **Fallback Tuyệt Đối**: Bất kỳ khi nào Knowledge Tree bị lỗi hoặc chưa sẵn sàng (`knowledge_status != 'KNOWLEDGE_READY'`), mọi truy vấn tự động kích hoạt **Global Hybrid Retrieval** để đảm bảo người dùng luôn nhận được câu trả lời và trích dẫn chuẩn xác.

---

## 6. Chuẩn Hóa Danh Mục Lỗi (Pillar 3 — Error Taxonomy)

Mở rộng cấu trúc từ `apps/api/sag_api/core/error_taxonomy.py`:

### 6.1. Ma Trận ErrorLayer & ErrorStage Mới
- **`ErrorLayer`**:
  - `CLIENT`: Lỗi client gửi sai định dạng, file quá lớn, header thiếu.
  - `API`: Lỗi validation, thiếu tenant/project scope.
  - `ENGINE`: Lỗi parser MinerU/MarkItDown, cắt block hỏng.
  - `LLM`: Lỗi LLM Gateway timeout, rate limit (chỉ ở bước E2 trích xuất nâng cao hoặc trả lời cuối).
  - `STORE`: Lỗi PostgreSQL transaction, Qdrant cluster unreachable.
  - `LAYA`: Lỗi inference mô hình cục bộ hoặc out-of-memory.
  - `ROUTING`: Lỗi giải thuật Leiden, vi phạm ràng buộc chất lượng cây.

- **`ErrorStage`**:
  - `RECEIVE` $\rightarrow$ `PARSE` $\rightarrow$ `DEDUP` $\rightarrow$ `SEARCH_INDEX` $\rightarrow$ `KNOWLEDGE_EXTRACT` $\rightarrow$ `GRAPH_LINK` $\rightarrow$ `TREE_BUILD` $\rightarrow$ `ROUTING` $\rightarrow$ `RETRIEVE` $\rightarrow$ `SYNTHESIZE`.

### 6.2. Cấu Trúc Khung Lỗi Chuẩn Hóa (Standard Error Envelope)

```python
class ErrorDetail(BaseModel):
    error_layer: Literal["client", "api", "engine", "llm", "store", "laya", "routing"]
    error_stage: Literal[
        "receive", "parse", "dedup", "search_index", 
        "knowledge_extract", "graph_link", "tree_build", "routing", "retrieve", "synthesize"
    ]
    error_code: str
    message: str
    retryable: bool
    attempt_count: int
    context: dict[str, Any] = Field(default_factory=dict)
```

---

## 7. Hợp Đồng Định Tuyến, Chiến Lược & Manifests (Pillar 4)

### 7.1. Hợp Đồng Laya Coarse Intent (Preserved Contract)
Khớp chính xác với triển khai đã kiểm chứng tại `apps/api/sag_api/services/laya_router.py`:

```python
class LayaIntentResult(BaseModel):
    coarse_intent: Literal["CHAT", "KNOWLEDGE", "COMMAND", "AMBIGUOUS"]
    confidence: float
    is_fallback: bool
    fallback_reason: str | None
    model_name: str = "multilingual"

# Quy tắc chuyển tiếp:
# IF coarse_intent == "CHAT" AND confidence >= 0.65 THEN:
#     need_retrieval = False
# ELSE:
#     need_retrieval = True (Luôn giữ nguyên query gốc và project scope)
```

### 7.2. Hợp Đồng Đặc Trưng Truy Vấn Tất Định (Deterministic Query Features)
Khớp với `apps/api/sag_api/services/query_analysis.py`:

```python
class QueryFeaturesContract(BaseModel):
    raw_query: str
    normalized_query: str
    exact_terms: list[str]       # Từ trong ngoặc kép: "CTM-92841"
    identifier_terms: list[str]  # Mã định danh: ERR_TIMEOUT, HTTP_404, UUID
    path_terms: list[str]        # Đường dẫn: src/auth/login.ts
    temporal_cues: list[str]     # Tín hiệu thời gian: trước, sau, hôm qua, v1.0
    relation_cues: list[str]     # Tín hiệu quan hệ: nguyên nhân, phụ thuộc, dẫn đến
    global_cues: list[str]       # Tín hiệu toàn cảnh: tổng quan, toàn bộ, kiến trúc
    multi_hop: bool              # Suy luận đa bước
```

### 7.3. Hợp Đồng Query Strategy Planner
Bộ điều phối chiến lược truy vấn tạo ra kế hoạch tìm kiếm:

```python
class RetrievalStrategy(str, Enum):
    DIRECT_ANSWER = "DIRECT_ANSWER"           # CHAT tự trả lời không cần tài liệu
    EXACT_LOOKUP = "EXACT_LOOKUP"             # Tìm chính xác theo mã/đường dẫn
    LOCAL_FACTUAL = "LOCAL_FACTUAL"           # Tìm kiếm sự kiện cục bộ trong phân cụm
    TEMPORAL = "TEMPORAL"                     # Lọc theo cửa sổ thời gian
    ENTITY_RELATIONAL = "ENTITY_RELATIONAL"   # Mở rộng đồ thị 1 bước nhảy
    GLOBAL_TOPIC = "GLOBAL_TOPIC"             # Quét các node mức cao của cây
    MULTI_HOP = "MULTI_HOP"                   # Khám phá đa nhánh cây tri thức

class QueryPlanContract(BaseModel):
    planner_version: str = "1.1.0"
    requested_strategy: RetrievalStrategy
    effective_strategy: RetrievalStrategy
    reason_codes: list[str]
    selected_node_ids: list[str] = Field(default_factory=list)
    escape_budget_ratio: float = 0.2          # 20% ngân sách dành cho global search chống blackhole
    max_candidates: int = 50
    rerank_enabled: bool = True
```

### 7.4. Hợp Đồng Truy Vết Tìm Kiếm (Retrieval Trace Contract)
Đảm bảo khả năng quan sát chi tiết từng mili-giây:

```python
class RetrievalTraceContract(BaseModel):
    trace_id: str
    query_analysis_ms: float
    laya_ms: float
    routing_ms: float
    branch_dense_ms: float
    branch_sparse_ms: float
    escape_search_ms: float
    fusion_ms: float
    dedup_mmr_ms: float
    graph_expand_ms: float = 0.0
    rerank_ms: float = 0.0
    context_build_ms: float
    total_latency_ms: float
    tree_version: str | None
    selected_nodes: list[str]
    strategy_used: RetrievalStrategy
    fallback_used: bool
    fallback_reason: str | None
```

### 7.5. Hợp Đồng Kiểm Soát Chất Lượng Cây (Tree Quality Manifest Contract)
Quy định ngưỡng nghiệm thu trước khi chuyển trạng thái cây sang `ACTIVE`:
- **`giant_ratio`** $\le 0.30$: Không cho phép cụm khổng lồ chiếm quá 30% tổng số đơn vị tri thức.
- **`routing_recall_at_k`** $\ge 0.90$ (với $k=5$): Đảm bảo tỷ lệ định tuyến trúng cụm liên quan đạt tối thiểu 90%.
- **`escape_win_rate`** $\le 0.15$: Tỷ lệ global escape search chiến thắng branch local search không vượt quá 15% (nếu vượt quá chứng tỏ cây bị phân cụm kém).
- **`acl_blackhole_rate`** $= 0.0$: Tuyệt đối không cho phép định tuyến vào nhánh mà người dùng không có quyền truy cập.

---

## 8. Bảo Mật Đa Người Thuê & Ranh Giới Qdrant (Pillar 5)

### 8.1. Cấu Hình Collection & HNSW Index Qdrant
- **Collection Name**: `search_units_{project_id}`
- **Vector Params**:
  - `dense`: size = 1024 (`Qwen3-Embedding-0.6B`), Distance = `Cosine`
  - `sparse`: (tích hợp qua BM25/SPLADE modifier)
- **HNSW Parameters**:
  - `m = 16`: Số liên kết trên mỗi node đồ thị
  - `ef_construct = 100`: Độ chính xác khi dựng chỉ mục
  - `on_disk = True`: Tối ưu RAM, lưu vector index trên ổ đĩa SSD

### 8.2. Payload Schema & Cơ Chế Pre-Filtering Bắt Buộc

```json
{
  "tenant_id": "tenant_continuum_default",
  "project_id": "proj_12345",
  "security_partition_id": "team_backend",
  "document_version_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "search_unit_id": "e3b0c442-98fc-1c14-9afb-f4c8996fb924",
  "content_hash": "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e",
  "valid_from": 1727481600,
  "valid_to": 253402300799,
  "page_from": 1,
  "page_to": 3,
  "section_path": "Architecture > Storage > PostgreSQL"
}
```

**Quy tắc lọc trước (Pre-filtering)**:
Mọi truy vấn Cosine Similarity trên Qdrant **bắt buộc** truyền bộ lọc `must` bao gồm cả `tenant_id`, `project_id` và `security_partition_id`:
```json
{
  "must": [
    { "key": "tenant_id", "match": { "value": "tenant_continuum_default" } },
    { "key": "project_id", "match": { "value": "proj_12345" } },
    { "key": "security_partition_id", "match": { "any": ["public", "team_backend"] } }
  ]
}
```
*Lưu ý an ninh: Tuyệt đối không sử dụng post-filtering sau khi LLM đã nhận chunk. Điều này ngăn chặn việc rò rỉ dữ liệu mật vào prompt context.*

---

## 9. Hợp Đồng Giao Tiếp API Mới (REST API Endpoints Specification)

### 9.1. Upload Tài Liệu Có Idempotency & Versioning
- **Endpoint**: `POST /api/v1/projects/{project_id}/documents/upload`
- **Headers**:
  - `X-Continuum-User-Id`: ID người thực hiện
  - `Idempotency-Key`: Khóa chống lặp (UUID hoặc client token)
  - `X-Continuum-Security-Partition`: Phân vùng bảo mật (`public`, `team_backend`...)
- **Form Data**: `file` (multipart binary), `source_published_at` (optional ISO timestamp)
- **Thuật toán xử lý Idempotency & Pre-allocation Lookup**:
  1. Khi nhận request kèm header `Idempotency-Key`, server tính toán / xác định `idempotency_key = SHA-256(tenant_id:project_id:file_hash:client_token)` theo công thức tại Section 4.
  2. Server thực hiện truy vấn bảng `ingestion_runs` theo cặp khóa `(tenant_id, project_id, idempotency_key)` **trước khi tạo bất kỳ bản ghi `document_version` mới nào**.
  3. **Nếu bản ghi đã tồn tại**:
     - Lấy thông tin `document_version_id` và `id` của run đó từ database.
     - Trả về ngay lập tức mã HTTP 200 OK với cờ `"is_duplicate": true`, không cấp phát version mới và không trigger lại background pipeline.
  4. **Nếu chưa tồn tại**:
     - Mở transaction database: xác định logical document (hoặc tạo mới), tính toán `version_no` tiếp theo, chèn `document_versions` (với `search_status = 'PENDING'`, `knowledge_status = 'NOT_STARTED'`), chèn `source_snapshots`, và tạo `ingestion_runs` với `(tenant_id, project_id, idempotency_key)`.
     - Đưa job vào Celery / background worker queue.
     - Trả về mã HTTP 201 Created với `"is_duplicate": false`.
- **Response 201 Created**:
  ```json
  {
    "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "version_no": 1,
    "version_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "run_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "file_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "status": "RECEIVED",
    "search_status": "PENDING",
    "knowledge_status": "NOT_STARTED",
    "is_duplicate": false
  }
  ```

### 9.2. Truy Vấn Tiến Độ & Readiness Trạng Thái
- **Endpoint**: `GET /api/v1/projects/{project_id}/documents/{document_id}/versions/{version_no}/status`
- **Response 200 OK**:
  ```json
  {
    "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "version_no": 1,
    "status": "SEARCH_READY",
    "search_status": "SEARCH_READY",
    "knowledge_status": "EXTRACTING",
    "search_ready": true,
    "knowledge_ready": false,
    "current_stage": "KNOWLEDGE_EXTRACT",
    "stage_progress": {
      "receive": {"status": "SUCCESS", "duration_ms": 12.4},
      "parse": {"status": "SUCCESS", "duration_ms": 1420.0},
      "dedup": {"status": "SUCCESS", "duration_ms": 85.3},
      "search_index": {"status": "SUCCESS", "duration_ms": 310.2},
      "knowledge_extract": {"status": "RUNNING", "duration_ms": 12500.0}
    },
    "error": null
  }
  ```

### 9.3. Endpoint Tìm Kiếm Đa Chế Độ Tích Hợp Laya & Escape Search
- **Endpoint**: `POST /api/v1/projects/{project_id}/search`
- **Request Body**:
  ```json
  {
    "query": "Mã cấu hình cache Redis của dịch vụ auth là gì?",
    "requested_strategy": "AUTO",
    "top_k": 5
  }
  ```
- **Response 200 OK**:
  ```json
  {
    "answer": "Theo tài liệu kiến trúc, mã cấu hình cache Redis là CTM-92841.",
    "citations": [
      {
        "citation_id": "cite_1",
        "document_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "version_no": 1,
        "page_number": 2,
        "section_path": "Architecture > Cache Configuration",
        "snippet": "...sử dụng biến REDIS_AUTH_KEY=CTM-92841..."
      }
    ],
    "trace": {
      "laya_intent": "KNOWLEDGE",
      "laya_confidence": 0.94,
      "strategy_used": "EXACT_LOOKUP",
      "fallback_used": false,
      "total_latency_ms": 284.5
    }
  }
  ```

---

## 10. Chiến Lược Rollback & Kế Hoạch Chuyển Đổi (Migration & Rollback Runbook)

### 10.1. Chiến Lược Tương Thích Ngược (Backward Compatibility)
1. **Bảo toàn dữ liệu cũ**: Bảng `documents` cũ được ánh xạ sang mô hình mới bằng cách gán `version_no = 1`, `logical_source_id = filename`, tính hash từ file trên đĩa để nạp vào `document_versions`.
2. **Không phá vỡ API client hiện hành**: Route cũ `/sources/{id}/documents` được chuyển thành wrapper gọi nội bộ vào service phiên bản mới.

### 10.2. Quy Trình Rollback Khẩn Cấp Dưới 100ms (Emergency Rollback)
1. **Rollback Cây Tri Thức (Active Pointer & Version Switch)**:
   - Nếu cây phiên bản mới ở `SLOT_B` gây suy giảm Routing Recall hoặc dính lỗi logic, API thực hiện câu lệnh duy nhất hoán đổi slot và chuyển con trỏ `active_tree_version` về `previous_tree_version`:
     ```sql
     UPDATE project_search_state 
     SET active_routing_slot = CASE WHEN active_routing_slot = 'SLOT_B' THEN 'SLOT_A' ELSE 'SLOT_B' END,
         active_tree_version = previous_tree_version,
         previous_tree_version = active_tree_version,
         active_search_epoch = active_search_epoch + 1,
         last_switched_at = CURRENT_TIMESTAMP 
     WHERE project_id = :project_id;
     ```
   - Thời gian thực thi: $< 5\text{ms}$. Toàn bộ các worker tìm kiếm và online retrieval tự động nhận diện `active_tree_version` cũ và `active_search_epoch` mới mà không cần re-index.
2. **Rollback Qdrant Point Batch**:
   - Khi một `IngestionRun` thất bại ở stage `SEARCH_INDEX`, toàn bộ vector rác được dọn dẹp bằng bộ lọc điểm:
     ```python
     await qdrant_client.delete(
         collection_name=f"search_units_{project_id}",
         points_selector=Filter(
             must=[FieldCondition(key="document_version_id", match=MatchValue(value=str(version_id)))]
         )
     )
     ```

---

## 11. Cổng Nghiệm Thu Phase 0 (Definition of Done & Verification)

Theo [plan.md](../tasks/plan.md), Phase 0 đạt cổng nghiệm thu khi thỏa mãn toàn bộ các tiêu chí:

- [x] **Tiêu chí 1: Khảo sát & Gap Matrix hoàn tất**: Đã đối chiếu toàn diện hiện trạng code (`db/models/`, `api/v1/`, `services/`, `jobs/`) với đặc tả `Workflow v1.1`.
- [x] **Tiêu chí 2: Thực thể & Định danh chốt chuẩn**: Đã ban hành cấu trúc DDL PostgreSQL 16 chi tiết cho 9 bảng (`documents`, `document_versions`, `source_snapshots`, `ingestion_runs`, `stage_runs`, `canonical_blocks`, `search_units`, `project_search_state`, `tree_manifests`) và công thức sinh UUIDv5.
- [x] **Tiêu chí 3: Ngữ nghĩa Readiness tách rời**: Đã quy định ranh giới độc lập giữa `SEARCH_READY` (Hybrid retrieval khả dụng) và `KNOWLEDGE_READY` (Tree enrichment). Sự cố tại nhánh Knowledge không hạ Search capability.
- [x] **Tiêu chí 4: Hợp đồng Query & Manifests**: Chuẩn hóa xong Pydantic contract cho Laya coarse-intent, deterministic features, Query Strategy Planner, Retrieval Trace và Tree Manifest quality gates.
- [x] **Tiêu chí 5: Phân vùng bảo mật & Rollback**: Thiết lập cơ chế Qdrant pre-filtering chống rò rỉ dữ liệu và cơ chế chuyển đổi Dual-Slot A/B $< 100\text{ms}$.
