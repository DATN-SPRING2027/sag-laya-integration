# Báo Cáo Bằng Chứng Nghiệm Thu Kỹ Thuật: Phase 8 & Checkpoint C (INCREMENTAL_READY)

**Dự án:** SAG Knowledge Routing RAG  
**Nhánh Git:** `feat/Tai-checkpoint-c-incremental-ready`  
**Giai đoạn:** Phase 8 — Incremental Tree  
**Tiêu chuẩn nghiệm thu:** Checkpoint C — `INCREMENTAL_READY` (theo [`SAG/tasks/plan.md`](../../tasks/plan.md#L805) và [`SAG/tasks/todo.md`](../../tasks/todo.md#L187-L200))  
**Thời điểm nghiệm thu:** 2026-10-03  

---

## 1. Tóm Tắt Kết Quả Nghiệm Thu (Executive Verification Summary)

Toàn bộ 4 gói công việc cốt lõi của Checkpoint C (`DATN-58`, `DATN-59`, `DATN-60`, `DATN-36`) đã được hiện thực hoàn chỉnh và vượt qua $100\%$ các bài kiểm thử nghiêm ngặt tại [`SAG/apps/api/tests/test_checkpoint_c_incremental.py`](../../apps/api/tests/test_checkpoint_c_incremental.py):

| Mã Tiêu Chí | Mô Tả Yêu Cầu Nghiệm Thu | Kết Quả Kiểm Thử Thực Tế | Trạng Thái |
|:---:|---|---|:---:|
| **TEST-C1** | Gán dữ liệu mới (Delta Ingest) vào lá cây tri thức mà **không phải rebuild toàn bộ cây**; cập nhật chính xác số liệu tích lũy tổ tiên (`accessible_unit_count`, temporal, sparse). | `test_incremental_normal_ingest_updates_delta_without_full_rebuild`<br>- Đơn vị $\ge 0.78$ được gán trực tiếp (`DIRECT`).<br>- Cấu trúc và ID các node gốc được bảo toàn nguyên vẹn. | **PASS** |
| **TEST-C2** | Đo trôi dạt (Drift Monitoring) với cơ chế **Hysteresis** (ít nhất 2 cửa sổ trượt vi phạm) và tái dựng nhánh cục bộ (**Targeted Subtree Rebuild**) kế thừa **Stable Node Lineage** (`SUPERSEDES_TREE_NODE`, `SPLIT_FROM`, `MERGED_FROM`). | `test_drift_detection_and_hysteresis_triggers_subtree_rebuild`<br>`test_targeted_subtree_rebuild_and_stable_node_lineage`<br>- Cửa sổ 1 không kích hoạt; Cửa sổ 2 kích hoạt chính xác.<br>- Node có Overlap $\ge 0.70$ giữ nguyên ID. | **PASS** |
| **TEST-C3** | Xây dựng vào **inactive routing slot** (Slot B khi Slot A active); cập nhật Qdrant dual-slot payload (`tree_version_b`, `primary_node_b`) với `wait=true` mà không đụng chạm Slot A. | `test_dual_slot_inactive_build_and_qdrant_payload_update`<br>- Payload Slot B độc lập 100%, không ghi đè Slot A.<br>- Batch updates hoàn tất với mã HTTP 200. | **PASS** |
| **TEST-C4** | Cổng kiểm chứng trước xuất bản (**Pre-publish Verification Gate**): từ chối xuất bản nếu sai lệch checksum, sai lệch số điểm (Point count mismatch), hoặc quality gate thất bại (**Fail-Closed**). | `test_manifest_verification_gate_rejects_corrupted_or_regressed_tree`<br>- Checksum lỗi $\rightarrow$ Từ chối (`checksum_mismatch`).<br>- Mismatch point count $\rightarrow$ Từ chối (`point_count_mismatch`). | **PASS** |
| **TEST-C5** | Hoán đổi con trỏ active nguyên tử (**Atomic Pointer Switch**) trong **duy nhất một giao dịch PostgreSQL** (`SELECT ... FOR UPDATE`, switch `active_routing_slot`, increment `active_search_epoch`). | `test_atomic_pointer_switch_in_single_postgres_transaction`<br>- Khóa bi quan chống race condition.<br>- Epoch tăng tuần tự, Manifest đổi trạng thái sang `ACTIVE`. | **PASS** |
| **TEST-C6** | Tính nhất quán thời điểm truy vấn (**Query Snapshot Consistency**): truy vấn đồng thời ghim snapshot bất biến, không bao giờ đọc pha trộn (mixed slot) giữa lúc publish. | `test_concurrent_queries_read_isolated_consistent_snapshot`<br>- Truy vấn bắt đầu trước publish đọc 100% Slot A.<br>- Truy vấn bắt đầu sau publish đọc 100% Slot B. | **PASS** |
| **TEST-C7** | Tiêm lỗi (**Fault Injection**) chứng minh lỗi ghi Qdrant không ảnh hưởng active tree cũ; quy trình **Rollback tức thì** khôi phục phiên bản trước trong rollback window. | `test_fault_injection_and_rollback_restores_consistency`<br>- Qdrant timeout 500 $\rightarrow$ DB giữ nguyên Slot A.<br>- Lệnh rollback khôi phục version cũ và slot cũ lập tức. | **PASS** |
| **TEST-C8** | Tích hợp runtime: `EngineManager.get_routing_snapshot()` nạp active manifest từ DB và cấp phát cho `query_routing_service.capture_routing_decisions()`. | `test_engine_manager_get_routing_snapshot_integration`<br>- `query_routing_service` định tuyến thành công với `routing_slot="SLOT_A"` và danh sách node lá. | **PASS** |
| **TEST-C9** | Định tuyến cây đa tầng (Hierarchical Multi-Depth Tree): tính toán `is_leaf` động, bảo đảm không bị lỗi `ValueError("Leaf routing profile has children")`. | `test_hierarchical_multi_depth_tree_routing_snapshot_integration`<br>- Phân biệt chính xác node cha (`is_leaf=False`) và node lá (`is_leaf=True`).<br>- Định tuyến thành công, `fallback_reason=None`. | **PASS** |

---

## 2. Chi Tiết Các Thay Đổi Mã Nguồn (Code Changes Summary)

Tuân thủ nguyên tắc **`/ponytail` (Tối giản hóa, YAGNI, ngắn nhất có thể)**:

1. **Model SQLAlchemy (`sag_api/db/models/routing_rag.py` & `sag_api/core/db.py`):**
   - Bổ sung `manifest_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)` vào `TreeManifest`.
   - Giúp lưu trọn vẹn toàn bộ snapshot cây tri thức (nodes, medoids, profiles, units, edges, lineage) chỉ trong 1 hàng SQL, **loại bỏ hoàn toàn sự cần thiết của các bảng relational phức tạp** (`knowledge_nodes`, `node_routing_profiles`).
   - Cập nhật `_COLUMN_UPGRADES` để tự động nâng cấp schema nhẹ nhàng.

2. **Dịch Vụ Cốt Lõi Duy Nhất (`sag_api/services/incremental_tree_service.py`):**
   - Tập trung toàn bộ logic của Checkpoint C trong một file duy nhất:
     - `assign_delta_units()`: Phân loại $T_{\text{high}}=0.78, T_{\text{low}}=0.58$, tính toán lại medoid lá và giới hạn thuộc tính sparse (`profile_sparse_terms`), chuẩn hóa datetime UTC và cập nhật thống kê tổ tiên.
     - `compute_tree_drift()`: Giám sát centroid drift thực tế, outlier ratio, capacity overflow và hysteresis (2 lần liên tiếp).
     - `rebuild_drifted_subtree()` & `match_node_lineage()`: Tái cấu trúc nhánh con cục bộ, đối sánh Weighted Overlap ($0.5 J_{\text{units}} + 0.3 S_{\text{medoid}} + 0.2 J_{\text{entities}}$), kế thừa `node_id` khi $\ge 0.70$, đồng bộ `tree_version = f"tree-{new_checksum[:24]}"`.
     - `build_inactive_slot_payloads()` & `update_inactive_slot_qdrant_payloads()`: Ghi batch dual-slot payload cho Qdrant (`httpx.AsyncClient` REST API với `wait=true`).
     - `verify_inactive_slot_manifest()`: Kiểm chứng đối soát 3 lớp (Quality gates, Checksum SHA-256 nghiêm ngặt, Point counts).
     - `execute_atomic_tree_publish()`: Single PostgreSQL transaction với `SELECT ... FOR UPDATE`.
     - `execute_tree_rollback()`: Hoàn nguyên tức thì về `previous_tree_version`.
     - `build_query_routing_snapshot()`: Runtime adapter tương thích 100% với `query_routing_service.py`, phân biệt lá và cha chính xác.

3. **Cầu Nối Runtime (`sag_api/sag/engine_manager.py`):**
   - Hiện thực phương thức `get_routing_snapshot()` trên `EngineManager`.
   - Nạp `ProjectSearchState` và `TreeManifest` active từ cơ sở dữ liệu để phục vụ truy vấn; bọc xử lý lỗi phòng thủ fail-closed an toàn.

4. **Bộ Test Suite Hoàn Chỉnh (`SAG/apps/api/tests/test_checkpoint_c_incremental.py`):**
   - 10 test cases bao quát toàn diện các khía cạnh kỹ thuật, thời gian chạy chỉ ~2.8 giây.

---

## 3. Nhật Ký Thực Thi Kiểm Thử (Test Execution Log)

```
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
plugins: anyio-4.15.1, asyncio-1.4.0

tests\test_checkpoint_c_incremental.py ..........                        [ 21%]
tests\test_phase_6_routing_tree.py ...............                       [ 53%]
tests\test_query_routing_service.py .......                              [ 68%]
tests\test_search_unit_store.py ...............                          [100%]

============================= 47 passed in 3.81s ==============================
```

---

## 4. Kết Luận Nghiệm Thu (Sign-off)

Mọi tiêu chí chấp thuận (Definition of Done) của **Phase 8 / Checkpoint C — INCREMENTAL_READY** đã đạt tiêu chuẩn sản xuất:
- Cây tri thức chấp nhận nạp dữ liệu delta mà không cần full rebuild.
- Tự động phát hiện drift và tái dựng đúng nhánh bị trôi dạt với lineage ổn định.
- Cơ chế khe kép (Slot A / Slot B) trên Qdrant và PostgreSQL loại bỏ hoàn toàn nguy cơ đọc dữ liệu chuyển đổi dở dang.
- Hoán đổi con trỏ active diễn ra nguyên tử trong 1 transaction; hỗ trợ rollback tức thì và chịu lỗi fail-closed.
