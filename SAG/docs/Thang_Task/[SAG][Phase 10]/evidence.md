# Bằng chứng Thực nghiệm (Evidence): Checkpoint D & Phase 10 — Research Agent (Bounded Ingestion)

> **Tài liệu tham chiếu:** [researchtask.md](researchtask.md), `SAG/tasks/plan.md` (Phase 10, Checkpoint D — AGENT_READY), `SAG/tasks/todo.md`.  
> **Nhánh thực hiện:** `feat/Tai-checkpoint-d-research-agent`  
> **Ngày hoàn thành:** 2026-10-12

---

## 1. Tóm tắt Kết quả Thực hiện

Toàn bộ các yêu cầu của **Phase 10 — Research Agent** và **Checkpoint D — AGENT_READY** đã được triển khai đầy đủ, tuân thủ nguyên tắc Ponytail (không over-engineering, không xây dựng agent framework phức tạp, không crawl tự do, đưa dữ liệu quay lại trực tiếp qua normal ingestion pipeline).

### Trạng thái các mục Checkpoint D & Phase 10:
- [x] **Gap Detector → Question Generator → Research Planner → discovery/fetch**: Hoàn tất luồng tuần tự và tất định trong `sag_api/services/research_planner_service.py` và `sag_api/services/research_agent_service.py`.
- [x] **Mỗi task lưu gap/source/reason, policy, budget và trạng thái**: Bảng `research_tasks` lưu đầy đủ `gap_id`, `question_id`, `reason_codes_json`, `policy_json`, `budget_json`, `target_urls_json`, `checksum`, và `status`.
- [x] **Fetch đi qua normal ingestion → dedup/version → tree update**: Nội dung sau khi tải được chuyển tiếp vào `handle_document_upload` để tạo `IngestionRun` và `DocumentVersion`, sau đó kết nối vào Knowledge Tree thông qua `update_tree_incrementally`.
- [x] **Kiểm tra crawl/fetch limit, allowlist, idempotency, duplicate guard và điều kiện dừng**: Đã kiểm chứng trần `max_fetches`, bộ lọc domain allowlist, SSRF guard, SHA-256 duplicate guard scoped by partition, streaming limits, manual redirect revalidation, và dừng dứt khoát khi gặp nội dung trùng lặp hoặc không có thông tin mới.
- [x] **Research task bắt nguồn từ gap signal có căn cứ**: Mỗi task liên kết trực tiếp với một `KnowledgeGap` hợp lệ (`eligible=True`, có anchored evidence quotes và reason codes).
- [x] **Ingestion loop có giới hạn và dừng khi không có thông tin mới**: Xác nhận trạng thái `STOPPED_NO_NEW_INFO` khi nội dung trùng lặp hoặc rỗng, và `FAILED` khi kiệt quệ do lỗi mạng.

---

## 2. Kết quả Chạy Kiểm thử Tự động (Test Results)

Đã chạy toàn bộ bộ kiểm thử mới cùng với regression suite của Phase 9 và Checkpoint D trên môi trường ảo Python của dự án:

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.3, pluggy-1.6.0
rootdir: D:\DoAnTotnghiep\sag-laya-integration\SAG\apps\api
configfile: pyproject.toml
plugins: anyio-4.15.1, langsmith-0.8.5, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 16 items

tests/test_research_planner.py::test_normalize_url PASSED                [  6%]
tests/test_research_planner.py::test_is_url_allowed PASSED               [ 12%]
tests/test_research_planner.py::test_plan_research_task_determinism_and_budget_caps PASSED [ 18%]
tests/test_research_planner.py::test_detect_eligible_gaps_and_task_persistence PASSED [ 25%]
tests/test_research_planner.py::test_discover_candidates_from_question_and_provenance PASSED [ 31%]
tests/test_research_agent_checkpoint_d.py::test_traceability_gap_to_research_task PASSED [ 37%]
tests/test_research_agent_checkpoint_d.py::test_fetch_url_bounded_guards PASSED [ 43%]
tests/test_research_agent_checkpoint_d.py::test_redirect_revalidation_stops_on_disallowed_or_ssrf PASSED [ 50%]
tests/test_research_agent_checkpoint_d.py::test_streaming_page_limit_and_cumulative_task_cap PASSED [ 56%]
tests/test_research_agent_checkpoint_d.py::test_terminal_task_not_re_executed PASSED [ 62%]
tests/test_research_agent_checkpoint_d.py::test_exhausted_network_errors_set_failed_status PASSED [ 68%]
tests/test_research_agent_checkpoint_d.py::test_duplicate_check_scopes_by_security_partition PASSED [ 75%]
tests/test_research_agent_checkpoint_d.py::test_no_new_info_and_duplicate_guard_terminates PASSED [ 81%]
tests/test_research_agent_checkpoint_d.py::test_normal_ingestion_relay_success PASSED [ 87%]
tests/test_research_agent_checkpoint_d.py::test_e2e_full_research_flow_from_gap_to_tree_update PASSED [ 93%]
tests/test_research_agent_checkpoint_d.py::test_incremental_tree_update_with_research_unit PASSED [100%]

======================= 16 passed, 1 warning in 29.16s ========================
```
