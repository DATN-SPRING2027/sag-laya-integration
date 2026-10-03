# Báo Cáo Nghiên Cứu Kỹ Thuật Chuyên Sâu: Phase 8 & Checkpoint C (INCREMENTAL_READY)

---

## 1. Tóm Tắt Điều Hành & Định Vị Nhiệm Vụ (Executive Summary)

### 1.1. Bối cảnh và nguồn chuẩn
- **Nguồn chuẩn:**
  - [`SAG/tasks/plan.md`](../../tasks/plan.md) (Phase 8: Incremental Tree, Checkpoint C: INCREMENTAL_READY).
  - [`SAG/tasks/todo.md`](../../tasks/todo.md) (Mục Phase 8 và Checkpoint C — INCREMENTAL_READY).
  - [`SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`](../SAG_Knowledge_Routing_RAG_Workflow_v1.1.md) (Mục 10: Xây dựng Knowledge Routing Tree, Mục 11: Incremental Update, Tree Stability và Blue-Green Versioning, Mục 12: PostgreSQL và Qdrant, Phụ lục A, B, C).
  - [`phase-0-contracts-and-foundations.md`](../phase-0-contracts-and-foundations.md) (Pillar 2: Schema DDL & Lifecycle).
  - [`phase-6-tree-evidence.md`](phase-6-tree-evidence.md) (Bằng chứng triển khai Phase 6 Routing Tree Builder trên PR #18).
  - [`[SAG][B2].md`](../Thang_Task/[SAG][B2]/[SAG][B2].md) và [`researchtask.md`](../Thang_Task/[SAG][B2]/researchtask.md) (Tích hợp B2 Query Planner, Tree-guided Retrieval & Escape trên PR #17).

### 1.2. Thứ tự phụ thuộc và ranh giới giao diện
- **Trình tự tích hợp hệ thống:** A (SEARCH_READY) $\rightarrow$ B (ROUTING_READY / DATN-35) $\rightarrow$ C (INCREMENTAL_READY).
- **Kế thừa và phụ thuộc:**
  - Task này kế thừa toàn bộ kết quả của Checkpoint A (đã đóng trong PR #15 / PR #16) và Checkpoint B (Phase 6 Tree Builder trong PR #18, B2 Query Planner & Tree Retrieval trong PR #17).
  - Task này **không làm lại** phạm vi của DATN-35 (ROUTING_READY baseline) hay ingestion/search readiness của Checkpoint A.
  - Phụ thuộc vào dữ liệu, knowledge pipeline và persisted routing tree từ DATN-62 để thực hiện nghiệm thu end-to-end.
- **Phân rã các Work Packages của Checkpoint C:**
  1. `DATN-58`: Gán dữ liệu mới vào base + delta tree trong luồng ingest thường; cập nhật node/ancestor statistics và drift signals mà không yêu cầu full tree rebuild.
  2. `DATN-59`: Tái dựng cây cục bộ (Targeted Subtree Rebuild) khi tín hiệu drift hoặc quality gate kích hoạt; duy trì đối sánh cụm ổn định, giữ vững node ID và lineage (`SPLIT_FROM`, `MERGED_FROM`, `SUPERSEDES_TREE_NODE`).
  3. `DATN-60`: Xây dựng vào inactive routing slot (Slot A / Slot B) trên snapshot/version độc lập; cập nhật Qdrant dual-slot payload đồng bộ với manifest; kiểm chứng exact counts, checksum và quality gates trước khi hoán đổi active pointer.
  4. `DATN-36`: Đảm bảo tính nhất quán dữ liệu truy vấn (Query Snapshot Consistency): mỗi truy vấn đọc trọn vẹn một snapshot tree/search/ACL trong lúc publish, không trộn slot cũ/mới; kiểm thử tiêm lỗi (Fault Injection) chứng minh active tree cũ tiếp tục phục vụ thông suốt và quy trình hoàn nguyên (Rollback) khôi phục trạng thái đồng nhất giữa PostgreSQL và Qdrant.

---

## 2. Khảo Sát Kiến Trúc Hiện Trạng Repo (Current Codebase Architecture Survey)

Qua khảo sát toàn diện mã nguồn trong repository `sag-laya-integration/SAG`, hiện trạng của các cấu phần liên quan đến Phase 8 và Checkpoint C được xác định như sau:

### 2.1. Cấu phần Phase 6 Routing Tree Builder (`sag_api/services/routing_tree_service.py`)
- **Khả năng hiện tại:**
  - Đã triển khai thuật toán phân cụm có ràng buộc `constrained-hierarchical-leiden-cpm-v1` với thư viện `igraph==0.11.9` và `leidenalg==0.10.2`.
  - Định nghĩa các cấu trúc dữ liệu bất biến: `KnowledgeUnitInput`, `KnowledgeEdgeInput`, `TreeBuildConfig`, `NodeProfile`, `RoutingNode`, `RoutingSnapshot`.
  - Đảm bảo tính tất định (deterministic) thông qua SHA-256 hash của content/config/edges, tạo ra `tree_version = f"tree-{checksum[:24]}"`.
  - Cách ly tuyệt đối theo `security_partition_id`: không có cạnh nối hoặc profile nào vượt ranh giới bảo mật.
  - Bổ sung 10 quality gates: `minimum_units`, `cohesion`, `edge_cut`, `giant_ratio`, `routing_recall`, `acl_partition_isolation`, `acl_blackhole`, `tree_integrity`, `cluster_constraints`, `partition_profiles`.
- **Hạn chế đối với Checkpoint C:**
  - Hàm `build_routing_snapshot()` là một **pure in-memory builder** chạy từ đầu (from scratch) trên toàn bộ danh sách `units` và `edges` được truyền vào.
  - Chưa hỗ trợ cơ chế gán vi sai (delta assignment) cho Knowledge Units mới.
  - Chưa có hàm giám sát độ trôi (drift monitoring) hay phát hiện outlier.
  - Chưa hỗ trợ rebuild từng nhánh con (subtree rebuild) mà chỉ rebuild toàn bộ cây.
  - Chưa có cơ chế đối sánh cụm cũ $\leftrightarrow$ mới (node lineage matching) để kế thừa `node_id`.
  - Chưa lưu trữ thực thể cây xuống cơ sở dữ liệu PostgreSQL.

### 2.2. Cấu phần Query Routing & Strategy Planner (`query_routing_service.py`, `query_strategy_planner.py`)
- **Khả năng hiện tại:**
  - `QueryStrategyPlanner` phân tích truy vấn theo 6 chế độ: `EXACT`, `LOCAL_FACTUAL`, `ENTITY_RELATIONAL`, `TEMPORAL`, `GLOBAL_TOPIC`, `MULTI_HOP`.
  - `query_routing_service.py` nhận `GroupRoutingSnapshot` với các trường: `tree_version`, `routing_slot` (`SLOT_A` hoặc `SLOT_B`), `search_epoch`, `manifest_status` (`"ACTIVE"`), `manifest_checksum` (64 ký tự hex), `manifest_verified` (`True`).
  - Hàm `route_snapshot()` thực hiện beam routing dựa trên entropy/margin, chọn ra danh sách `membership_node_ids` lá để định tuyến.
  - Hàm `capture_routing_decisions()` gọi `engine_manager.get_routing_snapshot()`. Nếu provider trả `None` hoặc lỗi, router chuyển sang `reason_code="TREE_PROVIDER_UNAVAILABLE"` hoặc `fallback_reason="routing_snapshot_unavailable"`.
- **Hạn chế đối với Checkpoint C:**
  - `EngineManager` trong `sag_api/sag/engine_manager.py` **chưa hiện thực hàm `get_routing_snapshot()`**. Hiện tại các test đang phải mock phương thức này trên `engine_manager`.
  - Chưa có kết nối từ bảng trạng thái điều khiển PostgreSQL `ProjectSearchState` lên tầng runtime để cấp phát `RoutingSnapshot` thực tế cho truy vấn.

### 2.3. Cấu phần Qdrant Store & Search Filter (`search_unit_store.py`, `search_unit_retrieval_service.py`)
- **Khả năng hiện tại:**
  - `search_unit_store.py` đã hiện thực hàm `build_search_filter()` với giao thức dual-slot chính xác:
    ```python
    slot = "a" if routing_slot == "SLOT_A" else "b"
    filter_body["must"].append({"key": f"tree_version_{slot}", "match": {"value": tree_version}})
    filter_body["should"] = [
        {"key": f"primary_node_{slot}", "match": {"any": nodes}},
        {"key": f"secondary_node_ids_{slot}", "match": {"any": nodes}},
    ]
    ```
  - `search_unit_retrieval_service.py` điều phối song song nhánh định tuyến cây cục bộ (`branch_local`) và nhánh dự phòng toàn cục (`global_escape`) với ngân sách latency riêng.
- **Hạn chế đối với Checkpoint C:**
  - Điểm dữ liệu trong Qdrant khi nạp mới qua `search_index_service.py` đang gán mặc định:
    `"primary_node_a": None`, `"secondary_node_ids_a": []`, `"tree_version_a": None`, `"primary_node_b": None`, `"secondary_node_ids_b": []`, `"tree_version_b": None`.
  - Chưa có worker hoặc service nào cập nhật các trường routing này khi cây tri thức được build hoặc cập nhật delta.

### 2.4. Cấu phần Cơ sở dữ liệu PostgreSQL (`sag_api/db/models/routing_rag.py`)
- **Khả năng hiện tại:**
  - Đã có sẵn 2 model điều khiển chính (Control-Plane Models):
    1. `ProjectSearchState`:
       - `project_id` (PK, varchar 64)
       - `slot_a_tree_version` (varchar 64)
       - `slot_b_tree_version` (varchar 64)
       - `active_routing_slot` (varchar 16, default `"SLOT_A"`)
       - `active_tree_version` (varchar 64)
       - `previous_tree_version` (varchar 64)
       - `active_search_epoch` (BigInteger, default 1)
       - `last_switched_at` (UTCDateTime)
    2. `TreeManifest`:
       - `tree_version` (PK, varchar 64)
       - `project_id` (varchar 64)
       - `config_version` (varchar 32)
       - `node_count` (int), `leaf_count` (int), `max_leaf_size` (int)
       - `giant_ratio` (float), `routing_recall_at_k` (float), `escape_win_rate` (float), `acl_blackhole_rate` (float)
       - `status` (varchar 32, default `"INACTIVE"`)
       - `checksum` (varchar 64)
       - `created_at` (UTCDateTime)
- **Hạn chế đối với Checkpoint C:**
  - Chưa có service quản lý vòng đời của `ProjectSearchState` (tạo mới, truy vấn, switch slot nguyên tử, rollback).
  - Bảng `TreeManifest` trong schema Phase 0 cần lưu trữ đầy đủ chuỗi JSON manifest chuẩn (`manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)`), giúp tái tạo toàn bộ snapshot cây tri thức (nodes, medoids, profiles, units, lineage) mà không cần bùng nổ các bảng relational phức tạp (`knowledge_nodes`, `node_routing_profiles`), tuân thủ triệt để nguyên tắc YAGNI và tối giản hóa cơ sở dữ liệu.

---

## 3. Phân Tích Lỗ Hổng & Khoảng Trống Kỹ Thuật (Gap Analysis)

Để đạt được tiêu chuẩn nghiệm thu Checkpoint C theo [Workflow v1.1 Mục 11](../SAG_Knowledge_Routing_RAG_Workflow_v1.1.md#11-incremental-update-tree-stability-và-blue-green-versioning) và [plan.md](../../tasks/plan.md), cần khắc phục 6 khoảng trống kỹ thuật cốt lõi:

| Mã Gap | Mô tả khoảng trống kỹ thuật | Hiện trạng trong Repo | Yêu cầu chuẩn của Checkpoint C |
|---|---|---|---|
| **GAP-C1** | **Delta Assignment không có full rebuild** | Ingest tài liệu chỉ tạo `SearchUnit`, gọi `build_routing_snapshot` phải rebuild 100% từ đầu. | So sánh ANN vector của Knowledge Unit mới với các prototype lá; gán trực tiếp nếu score $\ge T_{\text{high}}$ (0.78), đưa vào borderline nếu $T_{\text{low}} \le \text{score} < T_{\text{high}}$, đánh dấu outlier nếu $< T_{\text{low}}$ (0.58). Cập nhật thống kê tổ tiên mà không đổi cấu trúc cây. |
| **GAP-C2** | **Thiếu cơ chế đo Drift & Hysteresis Trigger** | Chưa có module tính toán biến thiên centroid, tỷ lệ outlier, độ suy giảm cohesion. | Theo dõi 8 tín hiệu drift chuẩn; sử dụng cửa sổ trượt (hysteresis window, ví dụ 2/3 lần vượt ngưỡng) để kích hoạt tái tạo cây, tránh rung lắc (flapping) do nhiễu dữ liệu đột biến. |
| **GAP-C3** | **Tái dựng cây cục bộ (Subtree Rebuild) & Stable Lineage** | Chỉ có hàm partition toàn cây; nếu rebuild sẽ sinh lại toàn bộ `node_id` ngẫu nhiên/hash mới. | Chỉ cắt và phân vùng lại nhánh con bị drift; đối sánh cụm cũ/mới qua Weighted Overlap (Jaccard thành viên + Cosine Medoid + Entity Overlap); bảo toàn `node_id` nếu overlap $\ge 0.70$; ghi nhận vết phả hệ (`SPLIT_FROM`, `MERGED_FROM`, `SUPERSEDES_TREE_NODE`). |
| **GAP-C4** | **Dual-Slot Build & Batch Update Qdrant Point Payload** | Qdrant payload chỉ có các trường rỗng; chưa có cơ chế xác định slot nhàn rỗi (inactive slot). | Đọc `active_routing_slot` từ PostgreSQL (`SLOT_A` $\rightarrow$ inactive là `SLOT_B` và ngược lại). Build phiên bản $V_{\text{next}}$ vào inactive slot. Ghi batch vào Qdrant với `wait=true` mà không đụng chạm đến slot đang phục vụ. |
| **GAP-C5** | **Kiểm chứng Manifest, Checksum & Quality Gate nghiêm ngặt** | Chưa có bước đối soát chéo giữa DB và Qdrant cho Tree Manifest trước khi publish. | Kiểm tra khớp 100% số điểm được cập nhật, khớp SHA-256 checksum manifest, vượt qua các cổng chất lượng (Recall không suy giảm quá mức cho phép, giant ratio không vượt trần). Nếu thất bại, đánh dấu `REJECTED`, không đổi active pointer. |
| **GAP-C6** | **Chuyển đổi nguyên tử (Atomic Switch), Snapshot Consistency & Rollback** | Chưa có giao dịch DB chuyển pointer; query runtime chưa snapshot cứng version/slot. | Một PostgreSQL transaction duy nhất chuyển `active_routing_slot` và tăng `active_search_epoch`. Mỗi query chụp bất biến `SearchSnapshot` đầu request. Tiêm lỗi chứng minh active tree cũ không gián đoạn; rollback khôi phục trạng thái chuẩn trong rollback window. |

---

## 4. Thiết Kế Kỹ Thuật Chi Tiết Cho Từng Work Package

```
+---------------------------------------------------------------------------------------------+
|                                    CHECKPOINT C ARCHITECTURE                                 |
|                                                                                             |
|   +-----------------------+           +-----------------------+                             |
|   | Normal Ingestion Flow |           |   Periodic / Event    |                             |
|   | (New Knowledge Units) |           |   Drift Monitor       |                             |
|   +-----------+-----------+           +-----------+-----------+                             |
|               |                                   |                                         |
|               v                                   v                                         |
|   +-----------------------+           +-----------------------+                             |
|   | DATN-58: Base + Delta |           | DATN-59: Targeted     |                             |
|   | Assignment (T_high/   |---------->| Subtree Rebuild &     |                             |
|   | T_low, Node/Ancestor  |  Drift    | Stable Lineage        |                             |
|   | Stats Update)         |  Trigger  | (Jaccard + Medoids)   |                             |
|   +-----------------------+           +-----------+-----------+                             |
|                                                   |                                         |
|                                                   v                                         |
|                                       +-----------------------+                             |
|                                       | DATN-60: Dual-Slot    |                             |
|                                       | Inactive Slot Build   |                             |
|                                       | (Slot A / Slot B)     |                             |
|                                       +-----------+-----------+                             |
|                                                   |                                         |
|                               +-------------------+-------------------+                     |
|                               |                                       |                     |
|                               v                                       v                     |
|                   +-----------------------+               +-----------------------+         |
|                   | PostgreSQL Staging    |               | Qdrant Inactive Slot  |         |
|                   | (Manifest, Status=    |               | Batch Payload Update  |         |
|                   | "INACTIVE")           |               | (primary_node_b, ...) |         |
|                   +-----------+-----------+               +-----------+-----------+         |
|                               |                                       |                     |
|                               +-------------------+-------------------+                     |
|                                                   |                                         |
|                                                   v                                         |
|                                       +-----------------------+                             |
|                                       | Manifest & Quality    |                             |
|                                       | Verification Gate     |                             |
|                                       +-----------+-----------+                             |
|                                                   |                                         |
|                               +-------------------+-------------------+                     |
|                               | Passed                                | Failed              |
|                               v                                       v                     |
|                   +-----------------------+               +-----------------------+         |
|                   | DATN-36: Single PG    |               | Mark Manifest         |         |
|                   | Transaction Switch    |               | REJECTED; Retain Old  |         |
|                   | (Active Slot Switch)  |               | Active Tree (Fail-    |         |
|                   +-----------+-----------+               | Closed)               |         |
|                               |                           +-----------------------+         |
|                               v                                                             |
|                   +-----------------------+                                                 |
|                   | Consistent Query Read |                                                 |
|                   | (SearchSnapshot) &    |                                                 |
|                   | Rollback Support      |                                                 |
|                   +-----------------------+                                                 |
+---------------------------------------------------------------------------------------------+
```

### 4.1. DATN-58: Base + Delta Tree Ingestion & Drift Monitoring
- **Mục tiêu:** Cho phép dữ liệu tài liệu nạp vào hằng ngày được gán vào cây tri thức hiện có mà không phải chịu chi phí tái dựng toàn bộ cây (Avoid Full Rebuild).
- **Thuật toán Gán Vi Sai (Incremental Delta Assignment):**
  1. Với mỗi Knowledge Unit mới $u \in \Delta$:
     - Tìm tập hợp các node lá (leaf nodes) thuộc cùng `tenant_id`, `project_id`, và `security_partition_id`.
     - Tính toán độ tương đồng Cosine giữa vector đặc trưng dense $u.\text{dense}$ và vector đại diện medoid của từng node lá $n$:
       $$S(u, n) = \text{cosine}(u.\text{dense}, n.\text{profile}.\text{dense\_medoid})$$
     - Tìm node lá có độ tương đồng lớn nhất $n^* = \arg\max_n S(u, n)$ và score cực đại $S^* = S(u, n^*)$.
  2. Phân loại theo ngưỡng (Section 11.1 Workflow v1.1):
     - **Direct Attach ($S^* \ge T_{\text{high}} = 0.78$):** Gán $u$ trực tiếp vào $n^*$.
     - **Borderline Queue ($T_{\text{low}} \le S^* < T_{\text{high}}$, với $T_{\text{low}} = 0.58$):** Gán tạm thời vào $n^*$, đồng thời đánh dấu cờ `is_borderline=True` để theo dõi tái cấu trúc cục bộ.
     - **Outlier Candidate ($S^* < T_{\text{low}} = 0.58$):** Gán vào vùng đệm ngoại lai (outlier partition/bucket) của security partition đó.
  3. Cập nhật thống kê tích lũy (Incremental Statistics Update):
     - Tăng `accessible_unit_count` của $n^*$ và tất cả các node tổ tiên (ancestors) lên gốc.
     - Hợp nhất tập thực thể (entities) và cập nhật khoảng thời gian hiệu lực `[temporal_from, temporal_to]`.
     - Cộng dồn trọng số các từ khóa thưa (sparse signature terms).
     - **Lưu ý:** Việc cập nhật này chỉ áp dụng cho tầng thống kê vi sai (delta statistics); không làm biến dạng active snapshot đang phục vụ truy vấn cho tới khi hoàn tất chu kỳ xuất bản.
- **Giám Sát Độ Trôi (Drift Monitoring) & Hysteresis:**
  - Tính toán định kỳ hoặc sau mỗi batch nạp các chỉ số trôi:
    1. *Centroid Drift:* $D_{\text{centroid}}(n) = 1.0 - \text{cosine}(\mu_{\text{old}}, \mu_{\text{new}})$. Kích hoạt khi vượt quá $\theta_{\text{centroid}} = 0.15$.
    2. *Outlier Ratio:* $R_{\text{outlier}} = \frac{|\text{outlier units}|}{|\text{total new units}|}$. Kích hoạt khi $R_{\text{outlier}} > 0.20$.
    3. *Capacity Overflow:* Kích hoạt khi số đơn vị trong node lá $|n.\text{unit\_ids}| > N_{\text{max}}$ (mặc định 32).
    4. *Cohesion Drop:* Độ suy giảm tỷ lệ cạnh nội bộ của node lá vượt quá $25\%$.
  - *Cơ chế Hysteresis:* Cần ít nhất 2 cửa sổ kiểm tra liên tiếp vượt ngưỡng cảnh báo để kích hoạt lệnh Subtree Rebuild, ngăn chặn tình trạng "chớp nháy" (flapping) do một vài điểm dữ liệu nhiễu đột ngột.

---

### 4.2. DATN-59: Targeted Subtree Rebuild & Stable Node Lineage
- **Mục tiêu:** Khi một nhánh con bị trôi dạt quá mức hoặc vượt quá dung lượng ràng buộc, hệ thống chỉ tái dựng lại đúng nhánh con đó (Subtree Rebuild), đồng thời bảo toàn định danh node (`node_id`) và lịch sử tiến hóa (lineage) để không làm vỡ các khóa định tuyến và cache phụ thuộc.
- **Quy trình Tái Dựng Nhánh Con Cục Bộ:**
  1. Xác định đỉnh gốc của nhánh con bị ảnh hưởng ($N_{\text{sub}}$): là node thấp nhất trong phân cấp bao trùm toàn bộ các node lá bị drift hoặc vi phạm ràng buộc dung lượng.
  2. Thu thập toàn bộ các Knowledge Units và Knowledge Edges nội bộ thuộc phạm vi của $N_{\text{sub}}$.
  3. Áp dụng thuật toán Constrained Leiden Partitioning (`_partition` trong `routing_tree_service.py`) độc lập trên tập con này, tuân thủ các ràng buộc:
     - `min_cluster_size` (2), `target_cluster_size` (8), `max_cluster_size` (32), `max_children` (8).
  4. Tạo ra tập các cụm mới $C_{\text{new}} = \{c_1, c_2, \dots, c_m\}$.
- **Thuật toán Đối Sánh Cụm Ổn Định & Kế Thừa Lineage (Stable Node Lineage):**
  - Giữa các node cũ $O \in \text{Children}(N_{\text{sub}})$ và các cụm mới $c \in C_{\text{new}}$, tính toán chỉ số tương đồng tổng hợp:
    $$\text{Overlap}(O, c) = w_j \cdot J(O.\text{units}, c.\text{units}) + w_m \cdot \text{cosine}(O.\text{medoid}, c.\text{medoid}) + w_e \cdot J(O.\text{entities}, c.\text{entities})$$
    (với trọng số mặc định $w_j = 0.5$, $w_m = 0.3$, $w_e = 0.2$).
  - Đối sánh tham lam theo thứ tự Overlap giảm dần:
    - Nếu $\text{Overlap}(O, c) \ge \text{NODE\_ID\_INHERIT\_THRESHOLD} = 0.70$:
      Cụm mới $c$ kế thừa chính xác `node_id = O.node_id`. Ghi nhận quan hệ: `SUPERSEDES_TREE_NODE(old=O.node_id, new=c.node_id)`.
    - Nếu một node cũ $O$ phân rã thành nhiều cụm mới:
      Các cụm mới nhận `node_id` mới ổn định (hash từ ID cha và tập thành viên), ghi nhận quan hệ: `SPLIT_FROM(parent_node=O.node_id)`.
    - Nếu nhiều node cũ gộp lại thành một cụm mới:
      Ghi nhận quan hệ: `MERGED_FROM(source_nodes=[O_1.node_id, O_2.node_id, ...])`.
  - Cập nhật phiên bản hiệu lực: `valid_from_tree_version = V_next`, `valid_to_tree_version = None` cho cụm mới; đóng phiên bản cũ với `valid_to_tree_version = V_next`.

---

### 4.3. DATN-60: Dual-Slot Inactive Build, Qdrant Dual-Slot Payload & Manifest Verification
- **Mục tiêu:** Xóa bỏ nguy cơ gián đoạn hoặc đọc phải trạng thái dữ liệu dở dang (half-migrated data) trong Qdrant bằng kiến trúc khe kép (Dual Routing Slot A/B).
- **Nguyên lý Khe Kép (Dual-Slot Blue-Green Architecture):**
  - Bảng điều khiển PostgreSQL `ProjectSearchState` lưu trữ:
    `active_routing_slot` $\in \{\text{"SLOT\_A"}, \text{"SLOT\_B"}\}$.
  - Khi `SLOT_A` đang là active:
    - Mọi truy vấn hiện hành lọc theo `tree_version_a`, `primary_node_a`, `secondary_node_ids_a`.
    - Tiến trình build phiên bản cây mới $V_{\text{next}}$ sẽ chọn **inactive slot là `SLOT_B`**.
  - Không bao giờ ghi đè lên slot đang active.
- **Quy trình Cập Nhật Vector Payload Trên Qdrant:**
  1. Sau khi sinh xong snapshot cây $V_{\text{next}}$ (gồm cấu trúc phân cấp, profile medoid, và ánh xạ unit $\rightarrow$ node), trích xuất ánh xạ node thành viên cho từng Search Unit:
     - `primary_node`: node lá chứa unit.
     - `secondary_node_ids`: các node lân cận có cạnh đồ thị mạnh vượt ngưỡng secondary membership.
  2. Gom các điểm thành từng batch (ví dụ 250–500 điểm) và thực hiện gọi API Qdrant `/collections/{collection}/points/payload?wait=true`:
     ```json
     {
       "points": ["point-id-1", "point-id-2"],
       "payload": {
         "primary_node_b": "node-target-leaf-id",
         "secondary_node_ids_b": ["node-neighbor-id"],
         "tree_version_b": "tree-62e9206b0b..."
       }
     }
     ```
  3. Lập chỉ mục lọc: Đảm bảo các trường `primary_node_a`, `primary_node_b`, `tree_version_a`, `tree_version_b` đã được đánh index kiểu `keyword` trong Qdrant collection (như đã định nghĩa tại `PAYLOAD_INDEX_FIELDS`).
- **Quy trình Kiểm Chứng Manifest Trước Xuất Bản (Pre-publish Verification):**
  - Trước khi cho phép hoán đổi con trỏ, hệ thống phải chạy cổng kiểm chứng toàn diện:
    1. *Exact Count Verification:* Kiểm tra số lượng điểm trong Qdrant có mang `tree_version_b == V_next` phải khớp chính xác $100\%$ với số lượng Search Units được ghi nhận trong `manifest["unit_count"]` (tính toán từ `manifest_json`) và số lượng node khớp với `TreeManifest.node_count`.
    2. *Checksum Verification:* Tính toán lại SHA-256 canonical checksum của manifest snapshot $V_{\text{next}}$ và đối chiếu với giá trị checksum đã lưu trữ.
    3. *Quality Gates Verification:*
       - Cohesion $\ge \text{min\_cohesion}$
       - Edge cut $\le \text{max\_edge\_cut}$
       - Giant ratio $\le \text{max\_giant\_ratio}$
       - Routing recall regression: $\text{Recall}(V_{\text{next}}) \ge \text{Recall}(V_{\text{active}}) - \text{MAX\_RECALL\_REGRESSION}$
       - ACL Partition Isolation: $100\%$ (không có rò rỉ chéo partition).
  - *Chính sách Thất bại (Fail-Closed):* Nếu bất kỳ tiêu chí nào không đạt, đánh dấu `TreeManifest.status = "REJECTED"`, hủy bỏ tiến trình xuất bản. Slot active cũ và cây cũ tiếp tục vận hành bình thường, không có bất kỳ tác động tiêu cực nào tới hệ thống.

---

### 4.4. DATN-36: Atomic Publish, Query Snapshot Consistency, Fault Injection & Rollback
- **Mục tiêu:** Chuyển đổi trạng thái hệ thống sang phiên bản cây mới trong một thao tác nguyên tử không thể chia cắt; đảm bảo truy vấn đồng thời đọc dữ liệu nhất quán; cung cấp khả năng hoàn nguyên lập tức khi phát hiện hồi quy.
- **1. Hoán Đổi Con Trỏ Nguyên Tử (Atomic Pointer Switch):**
  - Thực thi trong **một giao dịch PostgreSQL (Single ACID Transaction)** duy nhất:
    ```sql
    BEGIN;
    -- 1. Khóa bi quan bản ghi trạng thái của project để chống xung đột xuất bản đồng thời
    SELECT * FROM project_search_state WHERE project_id = :project_id FOR UPDATE;

    -- 2. Cập nhật con trỏ active sang inactive slot vừa hoàn tất kiểm chứng
    UPDATE project_search_state
    SET 
        previous_tree_version = active_tree_version,
        active_tree_version = :v_next,
        active_routing_slot = :inactive_slot, -- e.g. 'SLOT_B'
        active_search_epoch = active_search_epoch + 1,
        slot_b_tree_version = :v_next,
        last_switched_at = NOW()
    WHERE project_id = :project_id;

    -- 3. Cập nhật trạng thái manifest trong DB
    UPDATE tree_manifests SET status = 'ACTIVE' WHERE tree_version = :v_next;
    UPDATE tree_manifests SET status = 'INACTIVE' WHERE tree_version = :v_old;

    COMMIT;
    ```
- **2. Tính Nhất Quán Tại Thời Điểm Truy Vấn (Query-Time Snapshot Consistency):**
  - Mọi yêu cầu tìm kiếm khi bắt đầu được ghim trạng thái snapshot bất biến thông qua `GroupRoutingSnapshot` và `RouteDecision`:
    - `query_routing_service.py` yêu cầu `snapshot` chứa `routing_slot` (`"SLOT_A"` hoặc `"SLOT_B"`), `tree_version`, và `search_epoch`.
    - Sau khi beam routing hoàn tất, `RouteDecision` ghim chặt `routing_slot` và `tree_version` truyền xuống `search_unit_retrieval_service.py`.
    - `search_unit_retrieval_service.py` chuyển thẳng `routing_slot` và `tree_version` này vào `search_unit_store.py:build_search_filter()`.
  - Trong suốt quá trình thực thi của truy vấn (từ feature planning, beam routing, đến hybrid candidate retrieval trên Qdrant và context packaging), truy vấn **chỉ sử dụng các giá trị slot/version đã ghim này**.
  - Dù cho một tiến trình xuất bản cây khác hoàn tất việc đổi active pointer giữa chừng trong PostgreSQL, truy vấn đang chạy vẫn an toàn tuyệt đối vì nó tiếp tục đọc đúng slot và version đã ghim, không bao giờ nhìn thấy trạng thái pha trộn (mixed slot).
- **3. Khả Năng Chịu Lỗi & Tiêm Lỗi (Fault Injection Resilience):**
  - *Tiêm lỗi mạng/Qdrant timeout trong quá trình ghi payload vào inactive slot:*
    - Active slot cũ không bị thay đổi.
    - Transaction trên PostgreSQL bị rollback hoặc hủy bỏ; manifest $V_{\text{next}}$ bị đánh dấu `FAILED` hoặc xóa bỏ.
    - Truy vấn của người dùng không hề bị ảnh hưởng.
  - *Tiêm lỗi Checksum / Count Mismatch:*
    - Cổng kiểm soát phát hiện bất nhất giữa PostgreSQL count và Qdrant point count; từ chối kích hoạt chuyển slot; trả mã cảnh báo kiểm toán.
- **4. Cơ Chế Hoàn Nguyên (Rollback Protocol):**
  - Nếu sau khi chuyển đổi sang $V_{\text{next}}$, hệ thống giám sát phát hiện lỗi logic hoặc tỷ lệ escape bất thường (quality regression in production):
  - Kích hoạt quy trình rollback bằng một transaction đảo ngược:
    ```python
    await rollback_active_tree(session, project_id)
    ```
  - Khôi phục `active_tree_version = previous_tree_version`, chuyển `active_routing_slot` về slot đối ứng trước đó, và tăng `active_search_epoch`.
  - Do slot cũ vẫn được giữ nguyên vẹn trong rollback window (chưa bị ghi đè), việc rollback diễn ra tức thì trong vài mili-giây mà không cần tái lập chỉ mục lại Qdrant.

---

## 5. Thiết Kế Module & Kiến Trúc Mã Nguồn Đề Xuất (Implementation Plan)

Tuân thủ nguyên tắc **Ponytail (Fewest files, zero-bloat, YAGNI)**, toàn bộ logic nghiệp vụ, quản lý trạng thái cơ sở dữ liệu và adapter snapshot được gom gọn gàng trong **duy nhất một service**:

### 5.1. Module cốt lõi: `sag_api/services/incremental_tree_service.py`
- Chứa toàn bộ nghiệp vụ của Checkpoint C:
  - **Quản lý trạng thái DB:**
    - `get_or_create_project_state(session, project_id) -> ProjectSearchState`: Khởi tạo hoặc lấy trạng thái điều khiển slot A/B.
    - `save_tree_manifest(session, snapshot, status="INACTIVE") -> TreeManifest`: Lưu manifest và toàn bộ `manifest_json`.
    - `execute_atomic_tree_publish(session, project_id, snapshot, slot) -> ProjectSearchState`: Khóa bi quan `SELECT ... FOR UPDATE`, switch active slot và increment epoch nguyên tử trong 1 transaction.
    - `execute_tree_rollback(session, project_id) -> ProjectSearchState`: Hoàn nguyên an toàn về `previous_tree_version`.
  - **Thuật toán Cây & Drift (DATN-58, DATN-59):**
    - `assign_delta_units(base_snapshot, new_units, config) -> DeltaAssignmentResult`: Phân bổ Knowledge Units mới vào các node lá theo $T_{\text{high}}=0.78 / T_{\text{low}}=0.58$, cập nhật thống kê tổ tiên, borderline và outlier.
    - `compute_tree_drift(base_snapshot, delta_result) -> DriftReport`: Đánh giá centroid drift, outlier ratio, capacity overflow và hysteresis window (2/3 lần liên tiếp).
    - `rebuild_drifted_subtree(base_snapshot, drift_report, edges, config) -> SubtreeRebuildResult`: Tái phân vùng cục bộ nhánh trôi dạt bằng thuật toán Leiden có ràng buộc.
    - `match_node_lineage(old_nodes, new_nodes, threshold=0.70) -> LineageMapping`: Đối sánh Weighted Overlap ($w_j=0.5, w_m=0.3, w_e=0.2$), giữ vững `node_id` và ghi nhận phả hệ (`SPLIT_FROM`, `MERGED_FROM`, `SUPERSEDES_TREE_NODE`).
  - **Tương tác Qdrant Dual-Slot (DATN-60):**
    - `build_inactive_slot_payloads(snapshot, slot, project_id) -> list[dict]`: Chuẩn bị payload dual-slot theo cụm node lá.
    - `update_inactive_slot_qdrant_payloads(qdrant_client, collection_name, payloads) -> bool`: Gọi `httpx.AsyncClient` REST API ghi payload với `wait=true`.
    - `verify_inactive_slot_manifest(session, qdrant_client, project_id, snapshot, slot) -> bool`: Kiểm chứng đối soát số lượng điểm, checksum và quality gates.
  - **Runtime Query Adapter:**
    - `build_query_routing_snapshot(state, manifest_data, scopes) -> query_routing_service.RoutingSnapshot`: Chuyển đổi dữ liệu cây đã lưu và active slot thành request snapshot mà `query_routing_service.py` yêu cầu.

### 5.2. Mở rộng `EngineManager` trong `sag_api/sag/engine_manager.py`
- Cung cấp phương thức bất đồng bộ chuẩn:
  ```python
  async def get_routing_snapshot(
      self,
      *,
      query: str,
      scopes: list[dict[str, object]],
      planner: dict[str, object],
  ) -> query_routing_service.RoutingSnapshot | None:
  ```
- Nạp `ProjectSearchState` và `TreeManifest` hiện hành, gọi adapter `build_query_routing_snapshot()` để trả về snapshot hợp lệ.

### 5.3. Tích hợp Luồng Ingestion Thường (`tasks.py`)
- Trong pipeline nạp tài liệu sau khi Phase 2C hoàn tất:
  - Nếu project đã có active tree: gọi `assign_delta_units()` để cập nhật base tree mà không làm chậm luồng chính.
  - Nếu drift monitor kích hoạt: đưa tác vụ rebuild subtree và publish sang inactive slot một cách bất đồng bộ.

---

## 6. Kế Hoạch Kiểm Chứng & Tiêu Chí Nghiệm Thu (Acceptance & Verification Plan)

Suite kiểm thử toàn diện sẽ được thiết lập tại [`SAG/apps/api/tests/test_checkpoint_c_incremental.py`](../../apps/api/tests/test_checkpoint_c_incremental.py) với 7 nhóm kịch bản kiểm thử tương ứng với đầy đủ tiêu chí chấp thuận của Checkpoint C:

| Mã Kiểm Thử | Tên Kịch Bản Kiểm Thử | Mục Tiêu & Ràng Buộc Kiểm Chứng | Kết Quả Mong Đợi |
|---|---|---|---|
| **TEST-C1** | `test_incremental_normal_ingest_updates_delta_without_full_rebuild` | Nạp một batch tài liệu/Knowledge Units mới khi cây đã tồn tại. Đảm bảo cấu trúc phân cấp gốc không bị phá vỡ, các unit được gán vào đúng lá theo prototype similarity ($T_{\text{high}}/T_{\text{low}}$), số lượng accessible units được cập nhật đúng. | Không có lời gọi nào tới Leiden algorithm phân vùng lại toàn cây; base tree vẫn giữ nguyên ID; unit mới được gán chính xác. |
| **TEST-C2** | `test_drift_detection_triggers_targeted_subtree_rebuild_and_stable_lineage` | Bơm dữ liệu gây trôi dạt cục bộ (Centroid drift $> 0.15$ hoặc dung lượng lá vượt quá 32). Kiểm tra cơ chế hysteresis kích hoạt tái dựng duy nhất nhánh con đó. | Chỉ nhánh con bị ảnh hưởng được tái phân vùng; các node có Overlap $\ge 0.70$ kế thừa chính xác `node_id` cũ; các node chia tách ghi nhận đúng quan hệ `SPLIT_FROM` trong lineage. |
| **TEST-C3** | `test_dual_slot_inactive_build_and_qdrant_payload_update` | Kiểm tra quá trình sinh phiên bản mới trên inactive slot (ví dụ: Slot A đang active thì build vào Slot B). Kiểm tra các điểm trong Qdrant nhận đúng `primary_node_b`, `tree_version_b` mà `primary_node_a`, `tree_version_a` không hề bị thay đổi. | Slot A giữ nguyên vẹn; Slot B được cập nhật đầy đủ thông tin routing với `wait=true`. |
| **TEST-C4** | `test_manifest_verification_gate_rejects_corrupted_or_regressed_tree` | Giả lập trường hợp Qdrant cập nhật thiếu điểm (Point count mismatch), sai lệch Checksum, hoặc routing recall sụt giảm quá ngưỡng cho phép. | Quá trình publish dừng lại ngay lập tức (fail-closed); manifest được đánh dấu `REJECTED`; con trỏ active trong `ProjectSearchState` giữ nguyên slot cũ. |
| **TEST-C5** | `test_atomic_pointer_switch_in_single_postgres_transaction` | Thực thi lệnh chuyển con trỏ sau khi manifest đã verified. Kiểm tra trong DB: `active_routing_slot` đổi sang slot mới, `active_tree_version` trỏ tới phiên bản mới, `active_search_epoch` tăng lên 1 đơn vị, `previous_tree_version` lưu lại version cũ. | Thao tác diễn ra nguyên tử; trạng thái `TreeManifest` đổi từ `INACTIVE` sang `ACTIVE`. |
| **TEST-C6** | `test_concurrent_queries_read_isolated_consistent_snapshot_during_publish` | Chạy đồng thời các luồng truy vấn song song với một luồng thực hiện atomic publish. | Các truy vấn bắt đầu trước thời điểm switch đọc trọn vẹn $100\%$ snapshot cũ; các truy vấn bắt đầu sau thời điểm switch đọc trọn vẹn $100\%$ snapshot mới; không có truy vấn nào đọc phải trạng thái lai ghép (mixed slot). |
| **TEST-C7** | `test_fault_injection_and_rollback_restores_consistency` | 1. Tiêm lỗi mạng trong lúc ghi Qdrant payload: active tree cũ tiếp tục phục vụ không gián đoạn.<br>2. Thực hiện lệnh rollback sau khi đã publish: con trỏ active chuyển ngược về previous version và previous slot trong PostgreSQL; truy vấn ngay lập tức đọc lại cây cũ thành công. | Hệ thống phục hồi toàn vẹn trạng thái nhất quán giữa PostgreSQL và Qdrant mà không mất mát dữ liệu. |

---

## 7. Đánh Giá Rủi Ro & Chiến Lược Giảm Thiểu (Risk & Mitigation Matrix)

| **Rủi ro kỹ thuật** | **Mức độ** | **Tác động tiềm tàng** | **Biện pháp giảm thiểu & Thiết kế phòng ngừa** |
|---|:---:|---|---|
| **Lệch pha (Desynchronization) giữa PostgreSQL và Qdrant** | **CAO** | Qdrant trỏ node của Version B nhưng PostgreSQL lại đang trỏ Version A, dẫn đến truy vấn trả về 0 kết quả (blackhole). | Kiến trúc Dual-Slot: Qdrant lưu song song payload của cả 2 slot (`_a` và `_b`). Chỉ chuyển đổi con trỏ active trên PostgreSQL sau khi Qdrant đã xác nhận ghi thành công $100\%$ batch points và verify count/checksum khớp hoàn toàn. |
| **Xung đột ghi đè slot (Race Condition) khi nhiều worker cùng publish** | **TRUNG BÌNH** | Hai worker cùng cố gắng build và publish vào inactive slot cùng lúc làm hỏng dữ liệu payload. | Sử dụng cơ chế khóa bi quan `SELECT ... FOR UPDATE` trên bảng `ProjectSearchState` tại PostgreSQL, đảm bảo chỉ một tiến trình xuất bản được phép thao tác tại một thời điểm cho mỗi project. |
| **Tái cấu trúc cây làm thay đổi `node_id` làm mất tính ổn định định tuyến** | **TRUNG BÌNH** | Node ID thay đổi liên tục khiến cache routing bị vô hiệu hóa và mất khả năng theo dõi tiến hóa tri thức. | Giải thuật Weighted Overlap Lineage Matching với ngưỡng kế thừa khắt khe ($\ge 0.70$), bảo tồn định danh cụm cũ cho các cụm có độ tương đồng cao và lưu trữ rõ ràng phả hệ `SPLIT_FROM` / `MERGED_FROM`. |
| **Suy giảm chất lượng định tuyến sau khi gán vi sai (Routing Regression)** | **TRUNG BÌNH** | Việc gán nhiều unit delta vào các node lá có thể làm loãng tâm cụm (centroid dilution) và giảm độ chính xác routing. | Cổng kiểm soát chất lượng (Quality Gate) tính toán lại `routing_recall_at_k` trên tập benchmark trước khi xuất bản; nếu độ suy giảm vượt quá `MAX_RECALL_REGRESSION` thì từ chối kích hoạt và kích hoạt subtree rebuild. |
| **Tắc nghẽn tài nguyên do Subtree Rebuild liên tục (Thrashing/Flapping)** | **THẤP** | Dữ liệu nạp liên tục kích hoạt liên tiếp các lệnh rebuild làm nghẽn CPU. | Cơ chế trễ (Hysteresis): yêu cầu điều kiện drift phải duy trì qua nhiều batch nạp trước khi kích hoạt rebuild; đặt giới hạn tần suất tối thiểu (cooldown period) giữa các lần tái dựng nhánh. |

---

## 8. Kết Luận & Các Bước Tiếp Theo (Conclusion & Next Steps)

Bản nghiên cứu này đã thiết lập một nền tảng kỹ thuật và kiến trúc hoàn chỉnh, chặt chẽ cho việc triển khai **Phase 8 / Checkpoint C — INCREMENTAL_READY** trên repository `sag-laya-integration`:
1. **Chi nhánh công việc:** Đã tạo và chuyển sang nhánh `feat/Tai-checkpoint-c-incremental-ready`.
2. **Kế hoạch triển khai:** Tiến hành tuần tự theo 4 work packages chuẩn:
   $$\text{DATN-58} \longrightarrow \text{DATN-59} \longrightarrow \text{DATN-60} \longrightarrow \text{DATN-36}$$
3. **Các file cần hoàn thiện tiếp theo:**
   - Tạo kế hoạch triển khai chi tiết: `SAG/docs/tai_task/checkpoint-c-incremental-plan.md`.
   - Tạo module dịch vụ duy nhất (chuẩn `/ponytail`): `incremental_tree_service.py`.
   - Tích hợp `EngineManager.get_routing_snapshot()` và kết nối luồng ingest.
   - Viết bộ suite kiểm thử hoàn chỉnh `test_checkpoint_c_incremental.py` (10 test cases) và ghi nhận báo cáo bằng chứng `checkpoint-c-incremental-evidence.md`.
   - Cập nhật checklist tương ứng trong `SAG/tasks/todo.md`.
