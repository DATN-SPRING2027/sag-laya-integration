# Kế Hoạch Triển Khai Kỹ Thuật (Implementation Plan)
## Phase 8 & Checkpoint C: INCREMENTAL_READY
**Nhánh Git:** `feat/Tai-checkpoint-c-incremental-ready`  
**Triết lý kiến trúc:** Tuân thủ triệt để `/ponytail` (Tối giản, YAGNI, shortest working diff, 1 module duy nhất) và `/codebase-audit-pre-push` (không mã thừa, fail-closed an toàn, kiểm chứng 100%).

---

## 1. Mục Tiêu & Định Vị Các Gói Công Việc (Work Packages)

Theo chuẩn [`SAG/tasks/plan.md`](../../tasks/plan.md) (dòng 795–806) và [`SAG/tasks/todo.md`](../../tasks/todo.md) (dòng 187–200):

1. **DATN-58 (Incremental Delta Ingest & Drift Monitoring):**
   - Phân bổ `KnowledgeUnitInput` mới vào cây tri thức hiện có dựa trên độ tương đồng Cosine với prototype lá:
     - Direct attach: $S^* \ge T_{\text{high}} = 0.78$.
     - Borderline: $T_{\text{low}} = 0.58 \le S^* < T_{\text{high}}$.
     - Outlier candidate: $S^* < T_{\text{low}} = 0.58$.
   - Cập nhật số liệu tích lũy của node và tổ tiên (`accessible_unit_count`, temporal range, sparse term sums) mà không rebuild toàn bộ cây.
   - Giám sát 8 tín hiệu drift chuẩn với cơ chế Hysteresis (cần ít nhất 2 cửa sổ trượt liên tiếp vượt ngưỡng để kích hoạt rebuild).

2. **DATN-59 (Targeted Subtree Rebuild & Stable Node Lineage):**
   - Khi tín hiệu drift kích hoạt: chỉ cắt và tái phân vùng cục bộ nhánh con bị trôi dạt ($N_{\text{sub}}$) bằng thuật toán Leiden có ràng buộc (`constrained-hierarchical-leiden-cpm-v1`).
   - Đối sánh cụm cũ $\leftrightarrow$ cụm mới qua Weighted Overlap ($w_j=0.5, w_m=0.3, w_e=0.2$):
     - $\text{Overlap} \ge 0.70$: Giữ vững `node_id` cũ, gán quan hệ `SUPERSEDES_TREE_NODE`.
     - Phân rã: Gán quan hệ `SPLIT_FROM`.
     - Gộp cụm: Gán quan hệ `MERGED_FROM`.

3. **DATN-60 (Inactive Dual-Slot Build & Qdrant Payload Update):**
   - Đọc `active_routing_slot` từ `ProjectSearchState` (nếu Slot A đang active thì build vào Slot B và ngược lại).
   - Chuẩn bị payload batch cho inactive slot gồm `tree_version_{b}`, `primary_node_{b}`, `secondary_node_ids_{b}`.
   - Ghi batch vào Qdrant bằng `httpx.AsyncClient` REST API với `wait=true` mà không ảnh hưởng tới slot active đang phục vụ.
   - Chạy cổng kiểm chứng nghiêm ngặt (Pre-publish Verification Gate): khớp $100\%$ điểm trong Qdrant, khớp SHA-256 canonical checksum, vượt qua 10 quality gates. Nếu không đạt $\rightarrow$ `status = 'REJECTED'`, fail-closed an toàn.

4. **DATN-36 (Atomic Publish, Query Snapshot Consistency, Fault Injection & Rollback):**
   - Chuyển đổi con trỏ active trong **một giao dịch PostgreSQL (Single ACID Transaction)** duy nhất:
     `SELECT ... FOR UPDATE` trên `project_search_state`, đổi `active_routing_slot`, trỏ `active_tree_version = V_next`, tăng `active_search_epoch`.
   - Đảm bảo tính nhất quán của truy vấn: truy vấn ghim slot/version bất biến trong `RouteDecision` và `GroupRoutingSnapshot`, không bao giờ đọc mixed slot trong lúc publish.
   - Tiêm lỗi (Fault Injection): lỗi ghi Qdrant không làm hỏng active slot cũ; rollback tức thì khôi phục phiên bản trước đó trong rollback window.

---

## 2. Thiết Kế Cấu Trúc Mã Nguồn (Ponytail-Aligned Architecture)

Tối giản số file và loại bỏ code trùng lặp:

```
SAG/apps/api/
├── sag_api/
│   ├── db/models/routing_rag.py           <-- Bổ sung manifest_json vào TreeManifest
│   ├── services/incremental_tree_service.py <-- MODULE CỐT LÕI (toàn bộ logic Checkpoint C)
│   └── sag/engine_manager.py              <-- Hiện thực get_routing_snapshot()
└── tests/
    └── test_checkpoint_c_incremental.py   <-- Bộ test suite toàn diện TEST-C1..TEST-C7
```

---

## 3. Kế Hoạch Triển Khai Từng Bước (Phased Execution Steps)

### Bước 1: Chuẩn hóa Model & Schema (`TreeManifest.manifest_json`)
- Cập nhật model SQLAlchemy `TreeManifest` trong `sag_api/db/models/routing_rag.py`:
  - Thêm `manifest_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)`
- Cập nhật `_COLUMN_UPGRADES` trong `sag_api/core/db.py`:
  - Thêm `"tree_manifests": {"manifest_json": "JSON"}` để tương thích cả SQLite lẫn PostgreSQL.

### Bước 2: Hiện thực Module Cốt Lõi `incremental_tree_service.py`
Xây dựng các hàm thành phần:
1. **Database Control Plane:**
   - `get_or_create_project_state(session, project_id) -> ProjectSearchState`
   - `save_tree_manifest(session, snapshot, status) -> TreeManifest`
   - `execute_atomic_tree_publish(session, project_id, snapshot, target_slot) -> ProjectSearchState`
   - `execute_tree_rollback(session, project_id) -> ProjectSearchState`
2. **Delta Assignment (DATN-58):**
   - `assign_delta_units(base_snapshot, new_units, config) -> DeltaAssignmentResult`
   - Tính toán Cosine similarity giữa $u.\text{dense}$ và leaf prototype `dense_medoid`.
   - Phân loại: Direct attach ($\ge 0.78$), Borderline ($0.58 \le S < 0.78$), Outlier ($< 0.58$).
   - Cập nhật thống kê tổ tiên: `accessible_unit_count`, temporal range, accumulated sparse signatures.
3. **Drift Monitoring & Hysteresis (DATN-58):**
   - `compute_tree_drift(base_snapshot, delta_result, history_window) -> DriftReport`
   - Giám sát: Centroid drift ($> 0.15$), Outlier ratio ($> 0.20$), Capacity overflow ($> N_{\text{max}}$).
   - Hysteresis: cần ít nhất 2 lần vi phạm liên tiếp trong cửa sổ trượt để kích hoạt rebuild.
4. **Targeted Subtree Rebuild & Stable Lineage (DATN-59):**
   - `rebuild_drifted_subtree(base_snapshot, affected_node_id, edges, config) -> RoutingSnapshot`
   - Cắt nhánh cục bộ và phân vùng lại bằng constrained Leiden algorithm (`routing_tree_service._partition`).
   - `match_node_lineage(old_nodes, new_nodes, threshold=0.70) -> tuple[dict, list]`
   - Tính Weighted Overlap: $0.5 \cdot \text{Jaccard} + 0.3 \cdot \text{CosineMedoid} + 0.2 \cdot \text{EntityJaccard}$.
   - Kế thừa `node_id` nếu overlap $\ge 0.70$ (`SUPERSEDES_TREE_NODE`); ghi nhận `SPLIT_FROM`, `MERGED_FROM`.
5. **Qdrant Dual-Slot & Verification (DATN-60):**
   - `build_inactive_slot_payloads(snapshot, slot, project_id) -> list[dict]`
   - Gom điểm theo từng node lá để tối ưu số lần gọi API Qdrant.
   - `update_inactive_slot_qdrant_payloads(qdrant_client, collection_name, payloads) -> bool`
   - `verify_inactive_slot_manifest(session, qdrant_client, collection_name, project_id, snapshot, slot) -> bool`
   - Kiểm tra khớp $100\%$ số điểm trong Qdrant có mang `tree_version_{slot} == V_next`, khớp SHA-256 checksum manifest, vượt qua các cổng chất lượng.
6. **Query Routing Adapter:**
   - `build_query_routing_snapshot(state, manifest_record, scopes) -> query_routing_service.RoutingSnapshot`
   - Nạp `manifest_json`, trích xuất `roots` và `profiles`, đóng gói thành `GroupRoutingSnapshot` phù hợp với schema của `query_routing_service.py`.

### Bước 3: Tích Hợp Vào `EngineManager`
- Mở rộng `sag_api/sag/engine_manager.py`:
  - Hiện thực phương thức `get_routing_snapshot(query, scopes, planner)`.
  - Truy vấn `ProjectSearchState` và `TreeManifest` active từ DB.
  - Sử dụng adapter `build_query_routing_snapshot()` để trả về request snapshot hợp lệ cho `query_routing_service.py`.

### Bước 4: Viết Bộ Test Toàn Diện `tests/test_checkpoint_c_incremental.py`
Bao quát 7 kịch bản chấp thuận:
- **TEST-C1:** Delta assignment không làm thay đổi cấu trúc cây gốc, cập nhật đúng counts và ancestor statistics.
- **TEST-C2:** Drift monitor phát hiện trôi dạt và kích hoạt targeted subtree rebuild với stable node lineage matching.
- **TEST-C3:** Inactive slot build ghi đúng `tree_version_b`, `primary_node_b` vào Qdrant mà không chạm Slot A.
- **TEST-C4:** Verification gate phát hiện sai lệch số điểm / checksum / quality và từ chối xuất bản (fail-closed, `status='REJECTED'`).
- **TEST-C5:** Chuyển đổi con trỏ active nguyên tử trong 1 transaction PostgreSQL duy nhất.
- **TEST-C6:** Kiểm tra tính độc lập và nhất quán snapshot khi truy vấn song song trong lúc chuyển đổi active slot.
- **TEST-C7:** Tiêm lỗi mạng Qdrant (active tree không đổi) và quy trình Rollback khôi phục tức thì về previous version.

### Bước 5: Kiểm Thử Toàn Bộ Hệ Thống & Cập Nhật Tài Liệu
- Chạy toàn bộ test suites (`test_phase_6_routing_tree.py`, `test_query_routing_service.py`, `test_checkpoint_c_incremental.py`).
- Cập nhật checklist trong [`SAG/tasks/todo.md`](../../tasks/todo.md).
- Lập báo cáo bằng chứng tại `SAG/docs/tai_task/checkpoint-c-incremental-evidence.md`.
