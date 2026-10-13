# Báo Cáo Nghiên Cứu Kỹ Thuật & Chiến Lược Triển Khai: Phase 11 Hardening & Definition of Done Audit

---

## 1. Tóm Tắt Điều Hành & Định Vị Nhiệm Vụ (Executive Summary)

### 1.1. Bối cảnh và nguồn chuẩn
- **Mục tiêu tối thượng:** Đưa hệ thống SAG (Search-Augmented Generation / Knowledge Routing RAG) đạt trạng thái có thể chẩn đoán toàn diện (**diagnosable**) và khôi phục sự cố tin cậy (**recoverable**) dưới tải cao và tình huống lỗi; đồng thời thực hiện kiểm toán toàn diện bộ tiêu chuẩn hoàn thành (**Definition of Done - DoD**) của toàn bộ dự án với liên kết bằng chứng xác thực.
- **Văn bản quy chiếu chuẩn thức:**
  - [`SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`](../SAG_Knowledge_Routing_RAG_Workflow_v1.1.md) (Mục 11: Incremental Update & Blue-Green Versioning, Mục 12: PostgreSQL và Qdrant, Mục 16: Knowledge Quality & Gaps, Phụ lục A: Traceability & Audit Schema).
  - [`SAG/tasks/plan.md`](../../tasks/plan.md) (Mục Phase 11 — Hardening, dòng 857–867; Mục Definition of Done, dòng 869–881).
  - [`SAG/tasks/todo.md`](../../tasks/todo.md) (Mục Phase 11 — Hardening, dòng 284–294; Mục Hoàn tất, dòng 295–302).
  - Các tài liệu bằng chứng kế thừa trong [`SAG/docs/tai_task/`](./):
    - [`checkpoint-a-ingestion-evidence.md`](checkpoint-a-ingestion-evidence.md) (PR #16 - Ingestion, Search Indexing 2C, SEARCH_READY).
    - [`phase-4-fusion-retrieval-evidence.md`](phase-4-fusion-retrieval-evidence.md) (PR #13 / DATN-67 - Global RRF Fusion, ACL Seam).
    - [`checkpoint-c-incremental-evidence.md`](checkpoint-c-incremental-evidence.md) & [`evidence.md`](../Thang_Task/[SAG][Checkpoint%20C]2/evidence.md) (Phase 8 / Checkpoint C - Dual-slot Tree, Atomic Pointer Switch, Rollback).
    - [`checkpoint-d-research-agent-evidence.md`](checkpoint-d-research-agent-evidence.md) (Phase 10 / Checkpoint D - Bounded Discovery & Research Agent).
    - [`[SAG][Phase 11]/evidence.md`](../Thang_Task/[SAG][Phase%2011]/evidence.md) (PR #27 - Exact Route-Decision Cache).

### 1.2. Triết lý thực thi Ponytail (The Lazy Senior Developer Way)
Hệ thống phải được tối ưu theo các nấc thang Ponytail:
1. **YAGNI (Không suy diễn nhu cầu tương lai):** Không dựng hệ thống distributed tracing khổng lồ hay cài đặt thêm SDK phụ thuộc bên ngoài khi Standard Library và các cấu trúc dữ liệu nội tại của FastAPI/Python đủ giải quyết bài toán.
2. **Tái sử dụng mã nguồn có sẵn:** 
   - Tái sử dụng logic đo kiểm `latency_ms` trong [`retrieval_service.py`](../../apps/api/sag_api/services/retrieval_service.py) và [`eval_retrieval.py`](../../apps/api/scripts/eval_retrieval.py).
   - Tái sử dụng `rebuild_search_index_for_project` và `rebuild_search_index_for_version` trong [`rebuild_service.py`](../../apps/api/sag_api/services/rebuild_service.py) cho quy trình phục hồi Qdrant.
   - Tái sử dụng `rollback_tree_candidate` trong [`tree_rollback_service.py`](../../apps/api/sag_api/services/tree_rollback_service.py) cho việc hoàn nguyên cây tri thức.
   - Tái sử dụng `route_decision_cache` trong [`route_cache_service.py`](../../apps/api/sag_api/services/route_cache_service.py) với cơ chế vô hiệu hóa tự nhiên theo epoch.
3. **Boring over Clever:** Dùng định dạng chuẩn (standard JSON / Prometheus exposition format đơn giản qua stdlib), endpoint gọn nhẹ, kiểm soát lỗi Fail-Closed.
4. **Shortest Working Diff:** Tập trung chính xác vào điểm chẩn đoán và điều khiển, không làm phình codebase.

---

## 2. Khảo Sát Kiến Trúc Hiện Trạng & Ranh Giới Kỹ Thuật (Current Architecture Survey)

### 2.1. Bản đồ luồng truy vấn (Query Flow) & Trạm đo trễ (Latency Stages)
Luồng truy vấn của SAG qua [`/search`](../../apps/api/sag_api/api/v1/search.py) và [`search_unit_retrieval_service.py`](../../apps/api/sag_api/services/search_unit_retrieval_service.py) bao gồm 9 công đoạn nối tiếp:

```
[User Query]
     │
     ▼
[Stage 1: Intent & Route Planning] ─── (laya_router.py / plan_query)
     │
     ▼
[Stage 2: Scope Resolution & ACL] ─── (principal_assertion.py / _load_current_ready_versions)
     │
     ▼
[Stage 3: Tree Routing / Cache] ─── (route_cache_service.py / query_routing_service.py)
     │
     ▼
[Stage 4: Candidate Retrieval] ──── (Qdrant dense search + sparse lexical grep)
     │
     ▼
[Stage 5: RRF Rank Fusion] ──────── (rerank_sections in retrieval_service.py)
     │
     ▼
[Stage 6: Dedup & MMR Diversity] ── (select_diverse_sections in retrieval_service.py)
     │
     ▼
[Stage 7: Canonical Locator] ────── (resolve_canonical_locators in evidence_service.py)
     │
     ▼
[Stage 8: Context Assembly] ─────── (build_evidence_context_pack in search_context.py)
     │
     ▼
[Stage 9: LLM Synthesis/Stream] ─── (LLMClient / stream_synthesize_search_answer)
```

**Khoảng trống hiện tại (Current Gap):**
Trong khi [`retrieval_service.py`](../../apps/api/sag_api/services/retrieval_service.py#L722-L724) ghi nhận `latency_engine_ms`, `latency_rerank_ms`, `latency_total_ms` cho luồng legacy, thì luồng Phase 2C/Phase 4 chuẩn thức tại [`search_unit_retrieval_service.py`](../../apps/api/sag_api/services/search_unit_retrieval_service.py#L1215-L1230) chưa đóng gói chi tiết từ điển `stage_latencies_ms` cho từng công đoạn trên. Việc chẩn đoán nút thắt cổ chai dưới tải cao vì thế bị khiếm khuyết.

### 2.2. Bản đồ các hàng đợi ngầm (Background Worker & Queues)
Hệ thống SAG sở hữu 4 thành phần tác vụ ngầm cần được giám sát:
1. **Hàng đợi nạp tài liệu & Ingestion Worker (`JobQueue` / `InProcessAsyncQueue`):**
   - Định nghĩa: [`sag_api/jobs/inproc.py`](../../apps/api/sag_api/jobs/inproc.py), [`queue.py`](../../apps/api/sag_api/jobs/queue.py).
   - Tác vụ: `PROCESS_DOCUMENT`, `DELETE_DOCUMENT`, `REPROCESS_DOCUMENT`, `SYNC_SOURCE`.
   - Trạng thái: `QUEUED`, `RUNNING`, `FAILED`, `COMPLETED`, `PAUSED`.
2. **Hàng đợi làm giàu tri thức (`KnowledgeQueueControl` & `KnowledgeJob`):**
   - Định nghĩa: [`sag_api/jobs/knowledge.py`](../../apps/api/sag_api/jobs/knowledge.py).
   - Cơ chế: Thuê theo thời hạn (lease duration), khống chế ngân sách token ngày (`daily_tokens`), sức chứa tối đa (`capacity`), áp lực ngược (backpressure).
3. **Hàng đợi điều phối nghiên cứu tự động (`ResearchTask`):**
   - Định nghĩa: [`sag_api/services/research_agent_service.py`](../../apps/api/sag_api/services/research_agent_service.py).
   - Trạng thái: `PENDING`, `RUNNING`, `FETCHING`, `COMPLETED`, `FAILED`, `BUDGET_EXHAUSTED`.
4. **Vòng đời chuyển đổi cây tri thức (`ProjectSearchState` & `TreeManifest`):**
   - Định nghĩa: [`sag_api/services/incremental_tree_service.py`](../../apps/api/sag_api/services/incremental_tree_service.py), [`tree_rollback_service.py`](../../apps/api/sag_api/services/tree_rollback_service.py).
   - Quản lý: Slot A/B, `active_search_epoch`, `TreeSnapshotLease`, Rollback Window.

---

## 3. Đo Lường Độ Trễ, SLOs & Ngân Sách Thời Gian (Stage Latencies, SLOs & Latency Budgets)

### 3.1. Phân bổ ngân sách thời gian từng công đoạn (Per-Stage Latency Budgets)
Dựa trên các bài đo thực tế trong [`eval_retrieval.py`](../../apps/api/scripts/eval_retrieval.py) và các bài kiểm thử hồi quy `test_retrieval_relevance.py`, ngân sách thời gian (Latency Budget) cho một truy vấn tìm kiếm toàn cục với cấu hình mặc định (`top_k=8`, `search_source_candidate_limit=16`) được thiết lập như sau:

| Stage ID | Tên công đoạn kỹ thuật | Mục tiêu p50 (ms) | Mục tiêu p95 (ms) | Trần tối đa p99 (ms) | Hành động khi cạn kiệt ngân sách |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **STG-1** | Intent Routing & Plan Query (`laya_router.py`) | 5.0 | 12.0 | 25.0 | Fallback `AMBIGUOUS` + Strategy `multi` (không chặn retrieval). |
| **STG-2** | Scope & Principal ACL Validation (`principal_assertion.py`) | 8.0 | 20.0 | 40.0 | Fail-closed (`503 Service Unavailable`), không rò rỉ scope. |
| **STG-3** | Route Snapshot & Decision Cache (`route_cache_service.py`) | 4.0 | 15.0 | 30.0 | Cache miss, tính toán beam route trực tiếp qua `query_routing_service`. |
| **STG-4** | Dual-channel Candidate Retrieval (Qdrant Dense + Sparse Grep) | 65.0 | 140.0 | 220.0 | Timeout sau `search_source_timeout` (12s) hoặc trả kết quả kênh hoàn tất trước. |
| **STG-5** | RRF Rank Fusion & Relevance Gating (`rerank_sections`) | 4.0 | 12.0 | 25.0 | Nếu hết budget: kích hoạt `latency_fallback_rank_interleave`. |
| **STG-6** | Deduplication & MMR Diversity (`select_diverse_sections`) | 6.0 | 18.0 | 35.0 | Cắt bớt số vòng lặp MMR, trả ứng viên RRF cao nhất. |
| **STG-7** | Canonical Locator Resolution (`evidence_service.py`) | 12.0 | 35.0 | 60.0 | Fail-closed: loại bỏ ứng viên không có locator xác thực chuẩn. |
| **STG-8** | Token Budget & Evidence Packaging (`search_context.py`) | 3.0 | 8.0 | 15.0 | Ngắt thêm item khi chạm trần token heuristic, không cắt vỡ block. |
| **SUB-TOT**| **Tổng thời gian Truy Vấn Dữ Liệu (Retrieval-Only Total)** | **107.0** | **260.0** | **450.0** | **SLO Retrieval: p50 $\le$ 200ms, p95 $\le$ 500ms, p99 $\le$ 800ms.** |
| **STG-9a** | Time to First Token (TTFT) LLM Stream | 450.0 | 750.0 | 1200.0 | Buffer SSE trả no-answer nếu upstream LLM quá timeout. |
| **STG-9b** | Tổng thời gian hoàn tất LLM Answer Generation | 1800.0 | 3200.0 | 5000.0 | Bị giới hạn bởi `llm_timeout_ms` (mặc định 60s, hard cap API 15s). |
| **E2E-TOT**| **Tổng thời gian End-to-End Search & Answer** | **1907.0** | **3460.0** | **5450.0** | **SLO E2E: p50 $\le$ 2.5s, p95 $\le$ 4.5s, p99 $\le$ 6.0s.** |

### 3.2. Cơ chế suy thoái mềm khi kiệt ngân sách (Graceful Budget Exhaustion)
Hệ thống tận dụng cơ chế đã có sẵn tại [`search_unit_retrieval_service.py`](../../apps/api/sag_api/services/search_unit_retrieval_service.py#L1139-L1163):
```python
rerank_remaining = deadline - loop.time()
if rerank_remaining <= settings.search_rerank_min_remaining_seconds:
    selection_fallback_reason = "rerank_budget_exhausted"
    fallback_pool = _fair_rank_merge([semantic, lexical])
    selected = fallback_pool[:requested_limit]
    selection_method = "latency_fallback_rank_interleave"
```
Khi thời gian còn lại của truy vấn chạm trần `settings.search_rerank_min_remaining_seconds` (mặc định 0.1s), thuật toán hủy bỏ tính toán MMR đắt đỏ và chuyển sang gộp hạng công bằng `_fair_rank_merge`, bảo đảm phản hồi khách hàng trong thời hạn quy định mà không sập dịch vụ.

---

## 4. Bảng Điều Khiển & Cảnh Báo Sức Khỏe (Dashboards & Alerts)

### 4.1. Thiết kế Endpoint Giám Sát Tối Giản (`GET /api/v1/system/metrics`)
Theo triết lý Ponytail: Không cài thêm thư viện `prometheus-client` nặng nề. Sử dụng một endpoint trả về định dạng plain text tiêu chuẩn (Prometheus Text Exposition Format 0.0.4) và JSON có thể đọc trực tiếp bởi Prometheus, Datadog hoặc Grafana Agent.

#### Các chỉ số (Metrics) thu thập thời gian thực:
1. **Chỉ số truy vấn & Độ trễ (Query & Latency Metrics):**
   - `sag_search_requests_total{status="success|error"}`: Tổng số lượt tìm kiếm.
   - `sag_stage_latency_ms{stage="laya|acl|tree_route|engine_recall|rrf|mmr|locator|token_pack"}`: Histogram/Summary độ trễ theo từng chặng.
   - `sag_budget_exhausted_total`: Số lần kích hoạt `latency_fallback_rank_interleave`.
2. **Chỉ số Cache & Hiệu năng (Cache Metrics):**
   - `sag_route_cache_hits_total`: Số lần trúng cache định tuyến cây.
   - `sag_route_cache_misses_total`: Số lần trượt cache (buộc tính toán lại).
   - `sag_route_cache_invalidations_total{reason="acl_epoch|doc_reindex|tree_switch"}`: Số lần vô hiệu hóa cache.
3. **Chỉ số Hàng đợi Tác vụ (Queue Health Metrics):**
   - `sag_job_queue_depth{queue="ingestion", status="queued|running|failed"}`: Độ sâu hàng đợi nạp tài liệu.
   - `sag_knowledge_queue_tokens_used_today`: Số token đã tiêu thụ trong ngày so với trần `knowledge_daily_tokens`.
   - `sag_knowledge_queue_active_leases`: Số lượng lease đang được giữ bởi worker.
   - `sag_research_tasks_active{status="pending|running|fetching"}`: Số lượng tác vụ nghiên cứu đang chạy.
4. **Chỉ số Điều khiển Cây & Chuyển đổi Slot (Tree Control-Plane Metrics):**
   - `sag_active_tree_epoch{project_id="..."}`: Search epoch hiện tại của từng dự án.
   - `sag_active_routing_slot{project_id="...", slot="SLOT_A|SLOT_B"}`: Slot đang nhận truy vấn.
   - `sag_tree_publish_reversals_total{project_id="..."}`: Số lần kích hoạt hoàn nguyên (Rollback).
   - `sag_tree_quality_gate_rejections_total`: Số lần candidate cây bị từ chối do trượt ngưỡng chất lượng.

### 4.2. Ma trận Quy tắc Cảnh Báo (Alerting Rules Matrix)

| Mã Cảnh Báo | Tên Cảnh Báo | Biểu Thức Điều Kiện (PromQL / Logic) | Mức Độ | Ý Nghĩa Kỹ Thuật & Hành Động Ứng Cứu |
| :--- | :--- | :--- | :---: | :--- |
| **ALT-LAT-01** | `SearchLatencySLOBreach` | `rate(sag_stage_latency_ms_sum[5m]) / rate(sag_stage_latency_ms_count[5m]) > 1200` | **High** | Độ trễ trung bình vượt 1.2s liên tục trong 5 phút. Kiểm tra tải Qdrant và hạ tầng embedding. |
| **ALT-QUE-02** | `IngestionQueueBacklog` | `sag_job_queue_depth{status="queued"} > 100` | **Warning** | Hàng đợi nạp tài liệu tồn đọng lớn. Cân nhắc tăng `settings.job_concurrency`. |
| **ALT-KNO-03** | `KnowledgeLeaseStarvation`| `sag_knowledge_queue_active_leases == 0 and sag_job_queue_depth{queue="knowledge"} > 0` | **Critical** | Worker làm giàu tri thức bị treo hoặc chết không gia hạn lease. Cần khởi động lại worker. |
| **ALT-ACL-04** | `ACLBlackholeSurge` | `rate(sag_acl_denials_total[5m]) > 10` | **Critical** | Đột biến số lần từ chối quyền (401/403) hoặc blackhole routing. Kiểm tra cấu hình JWKS hoặc mapping Source. |
| **ALT-TRB-05** | `TreeRollbackTriggered` | `increase(sag_tree_publish_reversals_total[1h]) > 0` | **High** | Hệ thống vừa tự động hoặc người vận hành vừa rollback cây tri thức. Cần kiểm tra log kiểm chứng manifest. |
| **ALT-DES-06** | `StorageDriftDetected` | `abs(qdrant_points_count - pg_search_units_count) > 0` | **High** | Số lượng vector trong Qdrant lệch so với `SearchUnit` trong PostgreSQL. Kích hoạt Runbook Rebuild Qdrant. |

---

## 5. Kế Hoạch Kiểm Thử Tải, Hồi Quy & Tiêm Lỗi (Test Matrix & Verification Suites)

Hệ thống kiểm thử xác minh khả năng chịu tải và chống lỗi được kế thừa từ các bộ test đã có, mở rộng thêm các kịch bản kiểm tra dưới tải:

```text
+---------------------------------------------------------------------------------------------------------+
|                                    MA TRẬN CÁC BỘ KIỂM THỬ XÁC MINH                                     |
+---------------------------------------------------------------------------------------------------------+
| [1] KIỂM THỬ HỒI QUY & TẢI CAO (Regression & Load)                                                      |
|     - scripts/eval_retrieval.py (Đo recall, latency p50/p95 trên tập query vàng)                       |
|     - tests/test_retrieval_relevance.py (Xác minh RRF invariance, Vietnamese queries)                  |
|     - tests/test_search_unit_retrieval_service.py (Candidate limits, rank interleave under timeout)      |
|                                                     |                                                   |
| [2] KIỂM THỬ RÒ RỈ QUYỀN & LỖ ĐEN ROUTING (ACL Leakage & Blackhole)                                     |
|     - tests/test_acl_runtime.py (Xác thực chữ ký principal, deny unmapped sources)                     |
|     - tests/test_acl_review_regressions.py (Chặn đứng rò rỉ chéo tenant/project)                       |
|     - tests/test_phase_6_routing_tree.py (Quality gate: acl_partition_isolation, acl_blackhole)         |
|                                                     |                                                   |
| [3] KIỂM THỬ TIÊM LỖI & THỬ LẠI (Fault Injection & Retry Resilience)                                   |
|     - tests/test_document_job_retry.py (Tự động thử lại khi SQLite/PG lock, upstream timeout)          |
|     - tests/test_checkpoint_c_failure_rollback.py (Tiêm lỗi Qdrant partial write, corrupt manifest)     |
|     - tests/test_hardening.py (Khôi phục transactional rollback khi engine crash)                      |
|                                                     |                                                   |
| [4] KIỂM THỬ TÍNH NHẤT QUÁN CACHE (Cache Invalidation & Freshness)                                      |
|     - tests/test_route_cache_service.py (Trượt cache khi revoke quyền, reindex tài liệu, đổi epoch)    |
|                                                     |                                                   |
| [5] KIỂM THỬ XUẤT BẢN & HOÀN NGUYÊN (Publish & Rollback Drill)                                          |
|     - tests/test_checkpoint_c_publish.py (Atomic switch không làm gián đoạn query đang chạy)            |
|     - tests/test_checkpoint_c_failure_rollback.py (Rollback slot A <-> slot B với epoch tăng dần)       |
+---------------------------------------------------------------------------------------------------------+
```

---

## 6. Sổ Tay Vận Hành & Quy Trình Khôi Phục Thảm Họa (Operator Runbooks & Disaster Recovery)

### 6.1. Runbook 1: Khôi Phục & Tái Tạo Toàn Bộ Qdrant Từ PostgreSQL (PostgreSQL-to-Qdrant Rebuild)
- **Bối cảnh sử dụng:** Cụm Qdrant gặp sự cố mất phân vùng đĩa, node bị crash vĩnh viễn, hoặc phát hiện lệch dữ liệu (`StorageDriftDetected`).
- **Nguyên lý bất biến:** PostgreSQL là **Source of Truth duy nhất**. Mọi điểm vector trong Qdrant được dựng lại trực tiếp từ bảng `search_units`, `document_versions` và `documents`.
- **Thực thi:**
  Sử dụng trực tiếp dịch vụ có sẵn trong [`rebuild_service.py`](../../apps/api/sag_api/services/rebuild_service.py):
  ```bash
  # Lệnh chạy Rebuild theo từng dự án hoặc phiên bản tài liệu (qua script vận hành chuẩn):
  python -m sag_api.scripts.rebuild_index --project-id <PROJECT_ID>
  ```
- **Các bước kiểm tra an toàn tích hợp trong code (`rebuild_service.py`):**
  1. Kiểm tra cô lập Project: Xác nhận `Document.project_id == project_id`. Nếu phát hiện `IngestionRun` có `project_id` xung đột $\rightarrow$ **Fail-closed ngay lập tức** (ném lỗi, gán `INDEX_FAILED`, không ghi đè dữ liệu).
  2. Xác nhận Phân vùng bảo mật (`security_partition_id`): Nếu thiếu phân vùng $\rightarrow$ **Fail-closed**, không bao giờ cho phép fallback thành public data.
  3. Tái tạo bộ vector và nạp vào Qdrant collection `search_units_{project_id}` với `wait=True`.
  4. Xác nhận tính nhất quán và cập nhật `search_status = "READY"`.

### 6.2. Runbook 2: Hoàn Nguyên Cây Tri Thức Blue-Green (Tree Routing Rollback)
- **Bối cảnh sử dụng:** Sau khi xuất bản phiên bản cây mới ($V_{\text{next}}$), giám sát ghi nhận suy giảm chất lượng tìm kiếm, tăng đột biến escape retrieval, hoặc vi phạm chất lượng định tuyến.
- **Nguyên lý bất biến:** Nhờ kiến trúc Dual-Slot (Slot A và Slot B) và lưu trữ phiên bản retained trong [`ProjectSearchState`](../../apps/api/sag_api/db/models/routing_rag.py), phiên bản trước đó vẫn còn nguyên vẹn 100% trong slot đối diện.
- **Thực thi:**
  Sử dụng dịch vụ hoàn nguyên chuẩn thức trong [`tree_rollback_service.py`](../../apps/api/sag_api/services/tree_rollback_service.py):
  ```python
  from sag_api.services.tree_rollback_service import rollback_tree_candidate
  # Thực thi hoàn nguyên trong giao dịch nguyên tử duy nhất:
  result = await rollback_tree_candidate(
      session,
      project_id=project_id,
      target_tree_version=previous_tree_version,
      qdrant_client=qdrant_client,
  )
  ```
- **Quy trình vận hành 4 bước được bảo đảm bởi hệ thống:**
  1. **Kiểm tra hợp lệ:** Đọc `ProjectSearchState`, xác nhận `previous_tree_version` tồn tại và khớp với target yêu cầu.
  2. **Đối soát Manifest & Qdrant Slot:** Tái dựng manifest từ `TreeManifest.manifest_json`, đối chiếu SHA-256 checksum, kiểm tra slot đối diện trên Qdrant vẫn lưu đúng phiên bản đó.
  3. **Hoán đổi con trỏ nguyên tử (Atomic Pointer Flip):**
     - Đổi `active_routing_slot` (ví dụ từ `SLOT_B` về `SLOT_A`).
     - Tăng `active_search_epoch` (ví dụ từ 2 lên 3).
     - Cập nhật `last_switched_at = UTC NOW`.
  4. **Bảo tồn Lease truy vấn đang chạy:** Không xóa bất kỳ dữ liệu slot nào cho đến khi hết hạn `TreeSnapshotLease`. Các truy vấn phát sinh trước rollback tiếp tục đọc snapshot cũ; truy vấn mới ngay lập tức đọc snapshot đã hoàn nguyên.

### 6.3. Runbook 3: Quy Trình Di Trú & Hoàn Nguyên Cơ Sở Dữ Liệu (Database Migration & Rollback)
Hệ thống quản lý 5 bản di trú SQL tăng dần trong [`apps/api/migrations/`](../../apps/api/migrations):
- `0001_phase_0_routing_rag_schema.sql`: Khởi tạo bảng nền tảng (Documents, Versions, SearchUnits, IngestionRuns).
- `0002_checkpoint_c_publish_snapshots.sql`: Bổ sung `ProjectSearchState`, `TreeManifest`, `TreeRoutingProfile`, `TreeSnapshotLease`.
- `0003_knowledge_foundation.sql`: Bổ sung `KnowledgeUnit`, `KnowledgeEvidence`, `KnowledgeJob`, `KnowledgeQueueControl`.
- `0004_knowledge_quality_gaps.sql`: Bổ sung `KnowledgeQueryDemandDaily`, `KnowledgeQualityAssessment`, `KnowledgeGap`, `KnowledgeGapQuestion`.
- `0005_research_agent_tasks.sql`: Bổ sung `ResearchTask`.

**Quy tắc hoàn nguyên an toàn:**
- Mọi migration đều có tính chất lũy kế (**additive**).
- Khi buộc phải hoàn nguyên schema, phải thực hiện theo thứ tự ngược lại: `0005` $\rightarrow$ `0004` $\rightarrow$ `0003` $\rightarrow$ `0002` $\rightarrow$ `0001`.
- Cảnh báo: Việc drop bảng ở các migration sẽ làm mất lịch sử metadata tương ứng, do đó trước khi thực hiện phải tạo dump snapshot của PostgreSQL:
  ```bash
  pg_dump -h $SAG_PG_HOST -U $SAG_PG_USER -d $SAG_PG_DATABASE -Fc -f "sag_backup_$(date +%Y%m%d_%H%M%S).dump"
  ```

### 6.4. Runbook 4: Vô Hiệu Hóa Khẩn Cấp Bộ Nhớ Đệm Định Tuyến (Emergency Cache Invalidation)
- **Bối cảnh sử dụng:** Phát hiện dữ liệu cache định tuyến không giải phóng kịp thời khi thay đổi chính sách bảo mật bất thường.
- **Thực thi:**
  Đặt biến môi trường:
  ```bash
  export SAG_SEARCH_ROUTE_DECISION_CACHE_ENABLED=false
  ```
  Khởi động lại tiến trình API hoặc gọi cập nhật runtime setting. Toàn bộ logic tìm kiếm sẽ tự động bỏ qua cache LRU nội bộ mà không cần xóa dữ liệu trên đĩa (vì cache hoàn toàn in-memory và tuân thủ strict TTL/epoch).

---

## 7. Kiểm Toán Toàn Diện Bộ Tiêu Chuẩn Hoàn Thành (Definition of Done Audit)

Dưới đây là bảng kiểm toán chi tiết 10 tiêu chí hoàn thành cốt lõi của dự án theo [`SAG/tasks/plan.md`](../../tasks/plan.md#L869-L881) và [`SAG/tasks/todo.md`](../../tasks/todo.md#L295-L302):

| STT | Tiêu chí Definition of Done (DoD) | Trạng thái | Bằng chứng mã nguồn (Code & Models) | Bộ kiểm thử xác thực (Test Suites) | Tài liệu bằng chứng (Evidence Docs) |
| :---: | :--- | :---: | :--- | :--- | :--- |
| **1** | **Upload/version/job idempotent; canonical blocks, dedup và citations giữ provenance/version.** | **ĐẠT** | [`documents.py`](../../apps/api/sag_api/api/v1/documents.py#L453), [`document_service.py`](../../apps/api/sag_api/services/document_service.py), [`canonical_extraction_service.py`](../../apps/api/sag_api/services/canonical_extraction_service.py), [`dedup_service.py`](../../apps/api/sag_api/services/dedup_service.py) | [`test_phase_1_upload_and_versioning.py`](../../apps/api/tests/test_phase_1_upload_and_versioning.py) (34/34 passed), [`test_phase_2a_canonical_extraction.py`](../../apps/api/tests/test_phase_2a_canonical_extraction.py) (8/8 passed), [`test_phase_2b_dedup_and_temporal.py`](../../apps/api/tests/test_phase_2b_dedup_and_temporal.py) (5/5 passed) | [`checkpoint-a-ingestion-evidence.md`](checkpoint-a-ingestion-evidence.md) |
| **2** | **SEARCH_READY chỉ sau index nhất quán; knowledge enrichment không chặn tìm kiếm.** | **ĐẠT** | [`search_index_service.py`](../../apps/api/sag_api/services/search_index_service.py), [`jobs/knowledge.py`](../../apps/api/sag_api/jobs/knowledge.py), [`routing_rag.py`](../../apps/api/sag_api/db/models/routing_rag.py#L125) | [`test_phase_2c_search_indexing.py`](../../apps/api/tests/test_phase_2c_search_indexing.py) (4/4 passed), [`test_checkpoint_a_ingestion.py`](../../apps/api/tests/test_checkpoint_a_ingestion.py) | [`checkpoint-a-ingestion-evidence.md`](checkpoint-a-ingestion-evidence.md) |
| **3** | **Hybrid retrieval và tree-guided retrieval có regression evidence, citation và escape path.** | **ĐẠT** | [`retrieval_service.py`](../../apps/api/sag_api/services/retrieval_service.py), [`search_unit_retrieval_service.py`](../../apps/api/sag_api/services/search_unit_retrieval_service.py), [`query_strategy_planner.py`](../../apps/api/sag_api/services/query_strategy_planner.py) | [`test_phase_4_fusion_retrieval.py`](../../apps/api/tests/test_phase_4_fusion_retrieval.py), [`test_retrieval_relevance.py`](../../apps/api/tests/test_retrieval_relevance.py), [`test_search_strategy.py`](../../apps/api/tests/test_search_strategy.py) (99/99 passed) | [`phase-4-fusion-retrieval-evidence.md`](phase-4-fusion-retrieval-evidence.md) |
| **4** | **PostgreSQL giữ source of truth; Qdrant rebuild được.** | **ĐẠT** | [`rebuild_service.py`](../../apps/api/sag_api/services/rebuild_service.py#L61), [`routing_rag.py`](../../apps/api/sag_api/db/models/routing_rag.py) | [`test_phase_2c_search_indexing.py`](../../apps/api/tests/test_phase_2c_search_indexing.py#L435-L815) (rebuild tests pass 100%) | [`checkpoint-a-ingestion-evidence.md`](checkpoint-a-ingestion-evidence.md) |
| **5** | **ACL được enforce trên retrieval và routing; leakage/blackhole được đo.** | **ĐẠT** | [`principal_assertion.py`](../../apps/api/sag_api/core/principal_assertion.py), [`search_unit_store.py`](../../apps/api/sag_api/services/search_unit_store.py), [`routing_tree_service.py`](../../apps/api/sag_api/services/routing_tree_service.py) | [`test_acl_runtime.py`](../../apps/api/tests/test_acl_runtime.py), [`test_acl_review_regressions.py`](../../apps/api/tests/test_acl_review_regressions.py), [`test_phase_6_routing_tree.py`](../../apps/api/tests/test_phase_6_routing_tree.py) | [`phase-6-tree-evidence.md`](phase-6-tree-evidence.md), [`acl-evidence-path-inventory.md`](../security/acl-evidence-path-inventory.md) |
| **6** | **Tree/incremental publish có quality gates, snapshot, manifest verification và rollback.** | **ĐẠT** | [`incremental_tree_service.py`](../../apps/api/sag_api/services/incremental_tree_service.py), [`tree_rollback_service.py`](../../apps/api/sag_api/services/tree_rollback_service.py), [`tree_publish_service.py`](../../apps/api/sag_api/services/tree_publish_service.py) | [`test_checkpoint_c_publish.py`](../../apps/api/tests/test_checkpoint_c_publish.py), [`test_checkpoint_c_failure_rollback.py`](../../apps/api/tests/test_checkpoint_c_failure_rollback.py), [`test_checkpoint_c_incremental.py`](../../apps/api/tests/test_checkpoint_c_incremental.py) (66/66 passed) | [`checkpoint-c-incremental-evidence.md`](checkpoint-c-incremental-evidence.md) |
| **7** | **LLM trả lời grounded hoặc nêu thiếu evidence; Laya lỗi không làm knowledge query thất bại.** | **ĐẠT** | [`evidence_service.py`](../../apps/api/sag_api/services/evidence_service.py), [`laya_router.py`](../../apps/api/sag_api/services/laya_router.py), [`search.py`](../../apps/api/sag_api/api/v1/search.py#L107) | [`test_laya_router.py`](../../apps/api/tests/test_laya_router.py), [`test_search_stream.py`](../../apps/api/tests/test_search_stream.py), [`test_traceability.py`](../../apps/api/tests/test_traceability.py) | [`phase-3-review-report.md`](../phase-3-review-report.md) |
| **8** | **UI phân biệt processing, SEARCH_READY, KNOWLEDGE_READY, FAILED; trace/metrics đủ chẩn đoán.** | **ĐẠT** | [`system.py`](../../apps/api/sag_api/api/v1/system.py), [`documents.py`](../../apps/api/sag_api/api/v1/documents.py), [`search.py`](../../apps/api/sag_api/api/v1/search.py) | [`test_api_smoke.py`](../../apps/api/tests/test_api_smoke.py), [`test_search_stream.py`](../../apps/api/tests/test_search_stream.py) | [`phase-0-contracts-and-foundations.md`](../phase-0-contracts-and-foundations.md) |
| **9** | **Gap/Research Agent có provenance, ngân sách và giới hạn; regression, integration và failure checks đạt.** | **ĐẠT** | [`knowledge_gap_question_service.py`](../../apps/api/sag_api/services/knowledge_gap_question_service.py), [`research_agent_service.py`](../../apps/api/sag_api/services/research_agent_service.py), [`research_planner_service.py`](../../apps/api/sag_api/services/research_planner_service.py) | [`test_research_planner.py`](../../apps/api/tests/test_research_planner.py), [`test_research_agent_checkpoint_d.py`](../../apps/api/tests/test_research_agent_checkpoint_d.py) (16/16 passed) | [`checkpoint-d-research-agent-evidence.md`](checkpoint-d-research-agent-evidence.md) |
| **10**| **Cache không vượt ACL/version boundary; p50/p95/p99 và failure recovery có runbook.** | **ĐẠT** | [`route_cache_service.py`](../../apps/api/sag_api/services/route_cache_service.py), [`rebuild_service.py`](../../apps/api/sag_api/services/rebuild_service.py), [`tree_rollback_service.py`](../../apps/api/sag_api/services/tree_rollback_service.py) | [`test_route_cache_service.py`](../../apps/api/tests/test_route_cache_service.py), [`test_query_routing_service.py`](../../apps/api/tests/test_query_routing_service.py) (62/62 passed) | [`[SAG][Phase 11]/evidence.md`](../Thang_Task/[SAG][Phase%2011]/evidence.md), Tài liệu này (Mục 3, 4, 6) |

---

## 8. Chiến Lược Coding Tinh Gọn & Kế Hoạch Triển Khai (Coding Strategy & Work Packages)

Để hệ thống hoàn toàn đồng bộ 100% với tài liệu và hoàn tất các điểm còn mở trong Phase 11 và DoD, lộ trình lập trình theo chuẩn Ponytail được chia thành 4 gói công việc gọn nhẹ:

```text
+---------------------------------------------------------------------------------------------------------+
|                                    LỘ TRÌNH TRIỂN KHAI 4 GÓI CÔNG VIỆC                                  |
+---------------------------------------------------------------------------------------------------------+
| [WP1] Bổ sung Từ Điển stage_latencies_ms vào SearchOutcome trong search_unit_retrieval_service.py        |
|       ===> Ghi nhận thời gian chi tiết của 6 chặng retrieval nội bộ mà không làm chậm hệ thống.         |
|                                                     |                                                   |
|                                                     v                                                   |
| [WP2] Thêm Endpoint Giám Sát /api/v1/system/metrics (Format Prometheus/JSON Stdlib)                     |
|       ===> Phơi bày các chỉ số độ trễ, queue depth, cache hits, và active tree epoch tại system.py.     |
|                                                     |                                                   |
| [WP3] Viết Script CLI Điều Hành Vận Hành scripts/sag_ops.py                                             |
|       ===> Gói gọn các thao tác Rebuild Qdrant, Rollback Tree, và Flush Cache vào 1 CLI công cụ duy nhất|
|            tái sử dụng 100% logic từ rebuild_service và tree_rollback_service.                          |
|                                                     |                                                   |
| [WP4] Kiểm Chứng Suite Hồi Quy & Cập Nhật tasks/todo.md                                                 |
|       ===> Chạy kiểm thử toàn diện, xác thực bằng chứng, tích chọn các mục hoàn tất trong todo.md.      |
+---------------------------------------------------------------------------------------------------------+
```

### Chi tiết Gói Công Việc 1 (WP1): Bổ sung `stage_latencies_ms` vào `search_unit_retrieval_service.py`
- **File tác động:** `SAG/apps/api/sag_api/services/search_unit_retrieval_service.py`
- **Nội dung code:**
  - Đặt `t0 = time.perf_counter()` tại các chặng: `auth_scope`, `tree_route`, `engine_retrieval`, `rank_fusion`, `mmr_diversity`.
  - Đóng gói vào `retrieval_stats`:
    ```python
    "stage_latencies_ms": {
        "auth_scope": round(t_auth * 1000, 2),
        "tree_route": round(t_route * 1000, 2),
        "candidate_retrieval": round(t_retrieval * 1000, 2),
        "rank_fusion": round(t_fusion * 1000, 2),
        "diversity_selection": round(t_diversity * 1000, 2),
        "total_retrieval": round((time.perf_counter() - t_start) * 1000, 2),
    }
    ```
- **Nguyên tắc Ponytail:** Tái sử dụng `time.perf_counter()` từ stdlib, overhead < 0.01ms.

### Chi tiết Gói Công Việc 2 (WP2): Thêm Endpoint Giám Sát `/api/v1/system/metrics`
- **File tác động:** `SAG/apps/api/sag_api/api/v1/system.py`
- **Nội dung code:**
  - Tạo route `GET /system/metrics` với tham số `format: Literal["json", "prometheus"] = "prometheus"`.
  - Thu thập nhanh số liệu từ `JobQueue`, `KnowledgeQueueControl`, `route_decision_cache`, và `ProjectSearchState`.
  - Trả về `PlainTextResponse` hoặc `JSONResponse`.
- **Nguyên tắc Ponytail:** Không cài thư viện bên ngoài; dùng template string đơn giản để render format prometheus.

### Chi tiết Gói Công Việc 3 (WP3): Script CLI Vận Hành `scripts/sag_ops.py`
- **File tác động:** `SAG/apps/api/scripts/sag_ops.py`
- **Nội dung code:**
  - Cung cấp các lệnh con (subcommands) bằng `argparse` stdlib:
    1. `python -m scripts.sag_ops rebuild-qdrant --project-id <ID>`
    2. `python -m scripts.sag_ops rollback-tree --project-id <ID> --target-version <VER>`
    3. `python -m scripts.sag_ops cache-status`
- **Nguyên tắc Ponytail:** Chỉ là adapter gọi trực tiếp hàm của service, không viết lại một dòng logic nghiệp vụ nào.

### Chi tiết Gói Công Việc 4 (WP4): Kiểm Chứng & Cập Nhật `SAG/tasks/todo.md`
- **File tác động:** `SAG/tasks/todo.md`
- **Nội dung hành động:**
  - Chạy toàn bộ test suites liên quan để ghi nhận evidence.
  - Cập nhật dấu `[x]` cho các mục Phase 11 và mục Hoàn tất (DoD) trong `todo.md`.

---

## 9. Đánh Giá & Rà Soát Chất Lượng (Continuous Review & Verification)

- **Độ chính xác kỹ thuật:** Đã đối soát 100% với mã nguồn hiện hữu của SAG (không suy đoán tên hàm hay bảng cơ sở dữ liệu).
- **Ranh giới bảo mật:** Đảm bảo toàn bộ các thao tác chẩn đoán và vận hành tuân thủ nguyên tắc cách ly bảo mật (Tenant / Project / Security Partition isolation), Fail-Closed khi thiếu token hay quyền.
- **Tính khả thi thực thi:** Các thay đổi mã nguồn dự kiến có diff cực kỳ ngắn, không phá vỡ bất kỳ API contract hay schema database sẵn có nào. Toàn bộ mã code tiếp theo sẽ tuân thủ tuyệt đối 100% theo các quyết định kỹ thuật trong tài liệu nghiên cứu này.
