# Task research record — [SAG][B2]

## Task và trạng thái

- Owner: Thang/KeyT. Ngày: 2026-10-03.
- Task: Query planner, tree-guided retrieval & escape; Phase 7 / Checkpoint B.
- Spec nguyên gốc: [task spec](%5BSAG%5D%5BB2%5D.md).
- Code baseline: `0f73c1b`, branch `feat/Thang-sag-b2-query-planner-be-api`, checkout `F:\LEARN KÌ 8\ĐATN\sag-laya-main-after-16`.
- Final implementation commit: `9e99e9710c20f9796072598a39aa8ee0bfdd3e02`; review PR: [#17](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/17), Draft → `main`.
- Thư mục người dùng: `F:\LEARN KÌ 8\ĐATN\sag-laya-integration\SAG\docs\Thang_Task\[SAG][B2]`; tài liệu cũng nằm trên task branch để tracking.
- Trạng thái: **đã implement consumer-side B2 trên task branch; chưa đủ evidence để đóng Checkpoint B/ROUTING_READY; B1 contract/provider và benchmark chưa được owner xác nhận**.

## Tài liệu để thực hiện

| Tài liệu | Nội dung |
|---|---|
| [research.md](research.md) | Current flow, field/function contracts đã xác minh, ACL/provenance/score/token semantics, snapshot/profile proposal, algorithms, test matrix, scope/gaps. |
| [plan.md](plan.md) | Lát implementation đã làm, quyết định global escape song song, gaps và rollback. |
| [todo.md](todo.md) | Checklist code đã có evidence và các gate integration/benchmark còn mở. |

Đã implement planner `qsp-v1` với mode override additive, deterministic primary/modifiers/reason codes; typed frozen snapshot/profile consumer contract và ACL-safe beam; slot/version membership filter luôn đi cùng project/tenant/partition/version filters; global escape chạy trong reserved budget; pool candidate có cap trước canonical hydration/rerank; RRF→canonical verification→near-duplicate collapse→MMR→coverage; rerank hết budget thì rank-interleave fallback. Global search, stream và `search_context` chia sẻ planner/retrieval và trace.

Global escape được chạy đồng thời với branch-local khi tree route hợp lệ vì hiện chưa có calibrated signal để chứng minh evidence đủ; điều này tốn thêm một query nhưng không làm giảm recall khi route sai. Đây cần benchmark latency/cost trước khi bật tree provider production. Global route vẫn là fallback khi provider absent/timeout/malformed.

## Evidence research và kiểm chứng

- Baseline `0f73c1b`, task branch mới, không có thay đổi không liên quan trước implementation.
- Đọc plan/todo Phase 7, Workflow v1.1 §11/13, Checkpoint A/SearchUnit reader, ready manifest, tree-slot payload contract, provider/profile models và callers.
- Trace `_build_query_route`, `_prepare_global_search`, `SearchContextTool.invoke`, `retrieve_search_unit_sections`, ready-attempt checks, Qdrant filters/hydration, RRF, evidence pack và agent ToolResult.
- Không thấy runtime `get_routing_snapshot` provider trong checkout; B2 tiêu thụ typed contract và falls back global. Producer/publish lane không bị sửa.
- Chạy focused suite (planner, routing, SearchUnit store/retrieval, relevance, API/stream/agent): **147 passed, 1 deselected**. Deselect là test temporal-initial-tool riêng được chạy độc lập và fail: `_initial_tool_choice("最近 ChatGPT 有哪些更新？")` nhận `none` thay `get_time` do Laya routing. Logic P3 này không đổi trong B2; chưa có baseline A/B run với model deterministic để gán failure chắc chắn cho code hay model output.
- Checkpoint A SearchUnit payload/index contract + agent-history ACL + settings: **74 passed** (final combined run).
- Ruff trên toàn bộ Python files đã đổi: **All checks passed**. `git diff --check`: **passed**.
- Vòng code review theo correctness/readability/architecture/security/performance tìm thêm hai finding trong B2: explicit `MULTI_HOP` override không đặt `multi_hop_eligible`/reason nhất quán; MMR near-duplicate signature bỏ mọi nội dung sau 4.096 ký tự đầu. Đã sửa cùng regression cho override và evidence dài có phần kết luận khác nhau.
- Các finding implementation đã xử lý trong các vòng review trước gồm candidate pool cap, aggregate profile/membership bounds, rerank budget fallback, stats fallback chưa khởi tạo, wrong-route nonempty fixture và phân biệt structural coverage unknown/sufficient.
- Sau vòng review cuối: planner/router/retrieval/store/API/stream/agent suites **147 passed, 1 deselected**; Phase 2C indexing + agent-history ACL + settings **74 passed**; Ruff và `git diff --check` passed. Deselected baseline Laya temporal-initial-tool test đã chạy riêng trước đó và fail ở P3 routing ngoài phạm vi; không có thay đổi routing behavior ở B2.

## Gaps trước nghiệm thu

DATN-37 owner-confirmed snapshot/profile DTO, actual provider, consistent read/epoch and slot retention; Phase 5 graph/entity adapter; real Checkpoint A trusted-principal/PG/Qdrant integration; embedding/profile model identity; temporal history policy; calibrated semantic answerability; agreed gold corpus/recall/latency thresholds. Current tests are contract fixtures and do not certify tree producer, leakage resistance against real tenant principals, six-mode routing recall, or production SLO. `ROUTING_READY` remains open.

## Files, checks và handoff

- Backend: `SAG/apps/api`; task branch `feat/Thang-sag-b2-query-planner-be-api`; baseline commit `0f73c1b`. Source changes are in `services/query_strategy_planner.py`, `services/query_routing_service.py`, `query_analysis.py`, `retrieval_service.py`, `search_unit_retrieval_service.py`, `sag/search_unit_store.py`, API/schema/tool/agent integration and focused tests.
- Config: additive, validated `SAG_SEARCH_UNIT_CANDIDATE_LIMIT` (hard max 1,024), `SAG_SEARCH_RERANK_MIN_REMAINING_SECONDS`, `SAG_SEARCH_TREE_PROFILE_LIMIT` (max 16,384 profiles/query), `SAG_SEARCH_TREE_ESCAPE_RESERVE_SECONDS`, `SAG_SEARCH_TREE_SNAPSHOT_TIMEOUT`, `SAG_SEARCH_MMR_DIVERSITY_WEIGHT`, and `SAG_SEARCH_NEAR_DUPLICATE_SIMILARITY` settings. No secret/default credential added.
- Database/index/ingestion impact: **none**; no schema, migration, writer, or worker changed.
- Security: Qdrant branch predicates only narrow the existing authorized filters; canonical authorization/readiness/hydration remains mandatory; provider and escape failures are sanitized. Snapshot fingerprint is scope matching, not an authentication signature: the eventual provider remains a trusted owner boundary and must prove ACL-scoped profiles.
- Rollback: remove/disable B2 consumer routing to return to canonical global-only SearchUnit retrieval; retain ACL/readiness/citation guards. Do not roll back Checkpoint A data or mappings.
- Final handoff: commit `9e99e9710c20f9796072598a39aa8ee0bfdd3e02` pushed on `feat/Thang-sag-b2-query-planner-be-api`; [PR #17](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/17) is open as Draft against `main`. No merge was performed; integration and benchmark gates remain open.
