# Research task record — Checkpoint A

## Tài liệu

- Task gốc: [[SAG][Checkpoint A].md](%5BSAG%5D%5BCheckpoint%20A%5D.md).
- Research / implementation plan: [research.md](research.md).
- Todo và acceptance: [todo.md](todo.md).

## Tiến độ

- 2026-10-02: hoàn thành nghiên cứu baseline main `6035130`, ghi current flow/contracts/fields/algorithms/tests/gaps.
- Branch: `feat/Thang-checkpoint-a-search-ready-be-api` trong checkout `sag-laya-main-sync`.
- 2026-10-02: implementation và một lượt code review đã hoàn thành trên task branch.
- Không có commit, push hoặc PR trong lượt này.
- Chưa có xác nhận contract DATN-33/security hoặc kết quả staging; các phần đó vẫn là gate ngoài lane code.

## Implementation evidence — cập nhật sau khi thực hiện

- Commit(s): chưa tạo; working tree còn các thay đổi implementation trên task branch.
- Phạm vi files: API global search/stream, SearchUnit Qdrant reader, canonical evidence/citation/context, principal tenant/partition claims, agent `search_context`, tests và cập nhật plan/todo/task research. Không sửa ingestion/index producer hoặc frontend.
- Review finding đã sửa: lỗi SQLAlchemy ở scope lookup, candidate hydration và citation click có thể thoát thành exception thô vào tool trace. Canonical reader/citation đổi sang thông báo 503 đã làm sạch, log chỉ ghi exception type; Qdrant URL/request lỗi cũng được sanitize. Regression mô phỏng connection string có secret xác nhận không lộ.
- Checks: bộ suites retrieval/store/traceability, stream/agent, ACL/strategy và Phase 2C indexing/worker: **158 passed, 4 warnings**. Ruff trên 23 file Python: **All checks passed**. `git diff --check`: passed.
- Lệnh test đầy đủ:

  ```powershell
  $env:SAG_ENABLE_LAYA='false'
  $shimRoot=Join-Path $env:TEMP 'sag-checkpoint-a-testshim'
  $api=(Get-Location).Path
  $site=Join-Path $api '.venv\Lib\site-packages'
  $env:PYTHONPATH="$shimRoot;$api;$site;$site\win32;$site\win32\lib;$site\pythonwin;$site\pywin32_system32"
  .venv\Scripts\python.exe -S -m pytest -p litellm_test_shim tests/test_search_unit_store.py tests/test_search_unit_retrieval_service.py tests/test_retrieval_relevance.py tests/test_traceability.py tests/test_acl_runtime.py tests/test_search_strategy.py tests/test_search_stream.py tests/test_agentic.py tests/test_phase_2c_search_indexing.py tests/test_phase_2_worker_execution.py -q
  ```

- Môi trường kiểm chứng: `litellm==1.92.0` không cài được trên Windows host do thiếu MSVC `link.exe`; API lifespan dùng shim chỉ trong `%TEMP%`, không sửa dependency hay source. `SAG_ENABLE_LAYA=false` tránh test cố tải model Laya ngoài môi trường.
- Database/configuration/security impact: không thêm migration/schema/index hoặc đổi writer/config. Canonical evidence yêu cầu signed `tenantId` và `allowedPartitionIds`; principal assertion cũ thiếu claims này sẽ trả no-evidence/fail closed trên đường canonical. Không chạy migration/backfill/staging.
- Gaps còn mở: upload thật → worker → producer-written Qdrant → API; issuer/claim authority và Project Source mapping do owner xác nhận; embedding model identity; leakage/revoke test với principal thật; FE click/render split SearchUnit; answerability/entailment calibration. Danh sách chi tiết ở mục 13 của [research.md](research.md).
- Rollback / handoff: rollback bằng cách revert application branch; không xóa index hoặc mapping. Chưa push/merge.
