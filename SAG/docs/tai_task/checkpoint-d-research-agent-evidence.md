# Bằng chứng Thực nghiệm (Evidence): Checkpoint D & Phase 10 — Research Agent (Bounded Ingestion)

> **Tài liệu tham chiếu:** [checkpoint-d-research-agent-research.md](checkpoint-d-research-agent-research.md), `SAG/tasks/plan.md` (Phase 10, Checkpoint D — AGENT_READY), `SAG/tasks/todo.md`.  
> **Nhánh thực hiện:** `feat/Tai-checkpoint-d-research-agent`  
> **Ngày hoàn thành:** 2026-10-12

---

## 1. Tóm tắt Kết quả Thực hiện

Toàn bộ các yêu cầu của **Phase 10 — Research Agent** và **Checkpoint D — AGENT_READY** đã được triển khai đầy đủ, tuân thủ nguyên tắc Ponytail (không over-engineering, không xây dựng agent framework phức tạp, không crawl tự do, đưa dữ liệu quay lại trực tiếp qua normal ingestion pipeline).

### Trạng thái các mục Checkpoint D & Phase 10:
- [x] **Gap Detector → Question Generator → Research Planner → discovery/fetch**: Hoàn tất luồng tuần tự và tất định trong `sag_api/services/research_planner_service.py` và `sag_api/services/research_agent_service.py`.
- [x] **Mỗi task lưu gap/source/reason, policy, budget và trạng thái**: Migration `0005_research_agent_tasks.sql` bổ sung bảng `research_tasks` lưu đầy đủ `gap_id`, `question_id`, `reason_codes_json`, `policy_json`, `budget_json`, `target_urls_json`, `checksum`, và `status`.
- [x] **Fetch đi qua normal ingestion → dedup/version → tree update**: Nội dung sau khi tải được chuyển tiếp vào `handle_document_upload` để tạo `IngestionRun` và `DocumentVersion`, sau đó kết nối vào Knowledge Tree thông qua `update_tree_incrementally`.
- [x] **Kiểm tra crawl/fetch limit, allowlist, idempotency, duplicate guard và điều kiện dừng**: Đã kiểm chứng trần `max_fetches`, bộ lọc domain allowlist, SSRF guard, SHA-256 duplicate guard, và dừng dứt khoát khi gặp nội dung trùng lặp hoặc không có thông tin mới.
- [x] **Research task bắt nguồn từ gap signal có căn cứ**: Mỗi task liên kết trực tiếp với một `KnowledgeGap` hợp lệ (`eligible=True`, có anchored evidence quotes và reason codes).
- [x] **Ingestion loop có giới hạn và dừng khi không có thông tin mới**: Xác nhận trạng thái `STOPPED_NO_NEW_INFO` khi nội dung trùng lặp hoặc rỗng, triệt tiêu hoàn toàn nguy cơ crawl loop.

---

## 2. Kết quả Chạy Kiểm thử Tự động (Test Results)

Đã chạy toàn bộ bộ kiểm thử mới cùng với regression suite của Phase 9 trên môi trường ảo Python của dự án:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 33 items

tests\test_knowledge_quality_service.py ........                         [ 24%]
tests\test_knowledge_quality_persistence.py ................             [ 72%]
tests\test_research_planner.py ....                                      [ 84%]
tests\test_research_agent_checkpoint_d.py .....                          [100%]

============================= 33 passed in 39.17s =============================
```

### Chi tiết các ca kiểm thử Checkpoint D (`test_research_agent_checkpoint_d.py`):

1. **`test_traceability_gap_to_research_task`**:
   - Xác nhận task lưu đúng `gap_id`, `question_id`, `node_id`, `reason_codes`.
   - Xác nhận candidate URLs ngoài allowlist bị loại bỏ; các URLs được duyệt nằm trong domain policy (`wikipedia.org`, `arxiv.org`).

2. **`test_fetch_url_bounded_guards`**:
   - Kiểm tra domain không nằm trong danh sách được phép: bị từ chối với thông báo lỗi rõ ràng.
   - Kiểm tra SSRF (địa chỉ IP nội bộ, loopback 127.0.0.1): bị chặn dứt khoát trước khi tạo kết nối mạng.

3. **`test_no_new_info_and_duplicate_guard_terminates`**:
   - Giả lập trường hợp các trang web trả về nội dung trùng lặp với `IngestionRun` đã tồn tại trong hệ thống.
   - Xác nhận: Hệ thống phát hiện duplicate, không tải trùng vào database, dừng sớm và chuyển trạng thái task thành `STOPPED_NO_NEW_INFO` mà không phát sinh thêm bất kỳ lượt crawl nào.

4. **`test_normal_ingestion_relay_success`**:
   - Giả lập tải về tài liệu mới hợp lệ.
   - Chuyển tiếp thành công qua `handle_document_upload`, tạo `IngestionRun` và `DocumentVersion`.
   - Trạng thái task chuyển thành `COMPLETED`, lưu trữ các ID phiên bản và run ID vào `ingestion_run_ids_json`.

5. **`test_incremental_tree_update_with_research_unit`**:
   - Khởi tạo base tree manifest với 6 units.
   - Bổ sung unit tri thức mới thu thập từ research task qua `apply_incremental_tree_update_for_research`.
   - Xác nhận: Cập nhật delta thành công (`rebuilt_node_ids == ()`), không kích hoạt full rebuild (`drift.trigger_rebuild is False`), và số lượng unit trong manifest tăng lên 7.

---

## 3. Các File Đã Tạo & Bổ Sung

1. **Migration CSDL (Additive)**:
   - [SAG/apps/api/migrations/0005_research_agent_tasks.sql](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/migrations/0005_research_agent_tasks.sql)
2. **Models ORM**:
   - [SAG/apps/api/sag_api/db/models/research.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/db/models/research.py)
   - Export trong [SAG/apps/api/sag_api/db/models/__init__.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/db/models/__init__.py)
3. **Logic Dịch vụ Nghiệp vụ**:
   - [SAG/apps/api/sag_api/services/research_planner_service.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/research_planner_service.py)
   - [SAG/apps/api/sag_api/services/research_agent_service.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/sag_api/services/research_agent_service.py)
4. **Bộ Kiểm thử Tự động**:
   - [SAG/apps/api/tests/test_research_planner.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_research_planner.py)
   - [SAG/apps/api/tests/test_research_agent_checkpoint_d.py](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/apps/api/tests/test_research_agent_checkpoint_d.py)
5. **Tài liệu Nghiên cứu & Bằng chứng**:
   - [SAG/docs/tai_task/checkpoint-d-research-agent-research.md](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/docs/tai_task/checkpoint-d-research-agent-research.md)
   - [SAG/docs/tai_task/checkpoint-d-research-agent-evidence.md](file:///d:/DoAnTotnghiep/sag-laya-integration/SAG/docs/tai_task/checkpoint-d-research-agent-evidence.md)
