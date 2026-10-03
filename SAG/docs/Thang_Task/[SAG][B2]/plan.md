# Implementation plan and status — [SAG][B2]

Baseline `0f73c1b`; branch `feat/Thang-sag-b2-query-planner-be-api`. Phân tích và lý do thiết kế: [research.md](research.md).

## Status sau implementation slice

Đã triển khai consumer-side planner/routing, ACL-preserving branch filters, global escape, bounded candidate hydration/rerank (hard cap 1,024 SearchUnits) và profile/snapshot payload (8,192 profiles/query) limits, MMR/coverage trace và API/stream/tool wiring. Provider tree snapshot của DATN-37 chưa có trong checkout này; thiếu provider, timeout, payload over-limit hoặc snapshot sai thì consumer dùng canonical global SearchUnit path trong authorized scope. Phase 5 graph adapter và benchmark/gold corpus chưa có, nên chưa đủ điều kiện đóng Checkpoint B/ROUTING_READY.

Actual files gồm `services/query_strategy_planner.py`, `services/query_routing_service.py`, `services/query_analysis.py`, `services/search_unit_retrieval_service.py`, `services/retrieval_service.py`, `sag/search_unit_store.py`, API/schema/tool/agent integrations và regression tests. Candidate cap và deadline reserve dùng settings có validation; không có database/index-writer/migration change.

Một quyết định triển khai khác với plan ban đầu: với route hợp lệ, global escape được chạy đồng thời trong reserved budget thay vì chờ detector “weak”, vì semantic answerability chưa calibrated. Đây duy trì authorized recall khi tree route sai nhưng tốn thêm global query; benchmark/SLO cần chốt trước production rollout.

## 1. Contract và fixture trước

Tạo runtime DTO/fixture đề xuất cho planner, per-Project RoutingSnapshot và ACL-safe NodeProfile. Chốt với DATN-37 các field, scope proof, primary/secondary/descendant membership, slot retention và provider failure semantics. Model/graph signals có thể absent; consumer phải global escape được.

**Files dự kiến:** `sag_api/schemas/query_routing.py` (DTO mới nếu chưa có module phù hợp khi triển khai), `tests/fixtures/routing_snapshot.py`, `tests/test_query_planner.py`. Đây là tên dự kiến, không file đã tồn tại. DB models/store/publish không thuộc slice này.

**Acceptance:** immutable typed DTO, invalid/mixed scope bị từ chối; fixtures cover two Projects/partitions, narrow Sources, wrong route, delta unassigned, stale slot. “Contract agreed” chỉ đánh dấu khi có owner confirmation/evidence.

## 2. Planner deterministic

Reuse `query_analysis.py`; bổ sung exact/date/entity-related feature cần thiết theo fixture, tránh ảnh hưởng lexical scoring ngoài scope. Planner mới trong `services/query_strategy_planner.py` trả primary+modifiers/version/reasons. Rule precedence exact→relational→global→local; temporal composable; multi-hop escalation có căn cứ. Coarse intent guard và legacy mode enum giữ compatibility.

**Files dự kiến:** `services/query_strategy_planner.py`, `services/query_analysis.py`, `tests/test_query_planner.py`, `tests/test_query_analysis.py`.

**Acceptance:** sáu modes có deterministic cases; exact+temporal/relational+temporal; không auto MULTI_HOP vì query dài; unresolved time/entity có reason. Không tạo confidence giả.

## 3. Request snapshot và ACL boundary

Read frozen effective source grants, ready versions+current verified run/stage/manifest, per-Project tree state trong consistent read transaction; release connection trước network. Recheck intersect snapshot để revocation/retry loại candidate mà không thêm version mới. Provider unavailable/stale → tree disabled cho query, canonical global path tiếp tục.

**Files dự kiến:** `services/query_routing_service.py` (consumer/orchestration), `services/search_unit_retrieval_service.py`, contract tests snapshot/ACL. Reuse Source resolver; không viết principal verifier mới.

**Acceptance:** race version/slot/retry/revoke không mix epoch hay leak; no long transaction khi SSE/LLM; no raw assertions trong trace.

## 4. Beam router và branch filter

Consume B1 profile, prune inaccessible trước beam; score normalized available signals, stable ties; entropy+margin giữ broad branch; caps children/depth/leaf expansion. Translate pinned primary+secondary leaf membership AND exact base ACL cho cả dense/sparse. Unsupported descendant/profile/filter contract → escape có reason.

**Files dự kiến:** `services/query_routing_service.py`, `sag/search_unit_store.py`, `tests/test_tree_routing.py`, `tests/test_search_unit_store.py`.

**Acceptance:** zero-score/one-child/tie/missing signals an toàn; secondary membership và per-Project slot đúng; forbidden nodes không vào trace. Không sửa producer payload/index provisioning trong slice consumer.

## 5. Local+escape core, budget và candidate pipeline

Tách candidate acquisition+canonical hydration khỏi final selection vừa đủ để hai paths hợp nhất trước top-k. Reserve escape từ đầu; local và escape có deadline/candidate/concurrency caps. Reuse embedding chỉ trong compatible model identity. Một ranking/channel/scope, tránh double-count local+escape. Fuse→authorized content dedup→MMR bảo vệ required facets→coverage.

**Files dự kiến:** `services/search_unit_retrieval_service.py`, `services/query_routing_service.py`, `services/retrieval_service.py`, `tests/test_tree_retrieval_escape.py`, `tests/test_retrieval_relevance.py`.

**Acceptance:** wrong route nonempty và empty đều được global core cứu gold authorized evidence; local timeout không ăn escape reserve; trace phân biệt empty với index error; score scale invariant, duplicate channel không boost. MMR/threshold config là benchmark parameters, không hard-coded confidence.

## 6. Coverage, bounded graph và rerank

Structural/required facets coverage reuse exact-anchor logic và canonical locator checks. UNKNOWN→escape; relation evidence+missing coverage mới bounded graph/multi-hop nếu Phase 5 adapter có thật. Mỗi expanded unit quay về canonical verification. Optional reranker absent/too late thì skip có reason. Temporal history phải có owner policy và retained verified-version selector; nếu chưa có, giữ gap rõ ràng.

**Files dự kiến:** `services/query_routing_service.py`, `services/evidence_service.py` chỉ khi cần dùng chung facet checks, `tests/test_tree_retrieval_escape.py`. Không xây graph extraction hoặc cài neural package mới.

**Acceptance:** graph target inaccessible/current-attempt invalid bị loại; expansion không dùng escape reserve; context/citation chỉ từ final evidence; semantic answerability chưa calibrated được ghi explicit.

## 7. API, stream và tool integration

API/global stream và canonical `SearchContextTool` cùng gọi service B2. `_build_query_route` dùng planner service thay vì thực hiện domain mapping trong route. Legacy `strategy` giữ type; additive requested mode/trace nếu được chốt. Wire `stats.routing` qua ToolResult và agent tool event cho success/empty/weak/error; giữ citation buffering, no-answer và cancellation contracts.

**Files dự kiến:** `api/v1/search.py`, `schemas/search.py`, `tools/builtin.py`, `services/agent_service.py`, integration tests `test_search_strategy.py`, `test_search_stream.py`, `test_agent_tools.py`/`test_agentic.py`. Chia API và tool thành commit riêng nếu diff lớn; shared diff cần thống nhất owner.

**Acceptance:** API/tool không diverge về planner/scope/effective execution; CHAT/AMBIGUOUS regression; không legacy graph fallback; citations truy đúng version/unit/page/anchor.

## 8. DATN-37 integration, benchmark và handoff

Replace fixture provider bằng actual B1 output; producer upload→verified index→published tree→search/stream/tool→click. Benchmark versioned corpus/principals/gold/model/config, compare global-only baseline cho sáu modes; routing recall, escape recovery, ACL leakage/blackhole và latency. Chốt thresholds trước khi đánh dấu Checkpoint B. Update researchtask và Phase 7 todo **chỉ** phần có evidence, giữ broader gates open.

**Validation đã chạy, từ `SAG/apps/api`:** dùng Python/Ruff của checkout `sag-laya-integration` vì task checkout không có `.venv`; không cài dependency hay sửa checkout đó.

```powershell
& 'F:\LEARN KÌ 8\ĐATN\sag-laya-integration\SAG\apps\api\.venv\Scripts\python.exe' -m pytest tests/test_query_strategy_planner.py tests/test_query_routing_service.py tests/test_search_unit_store.py tests/test_retrieval_relevance.py tests/test_search_unit_retrieval_service.py tests/test_search_stream.py tests/test_agentic.py tests/test_search_strategy.py -q -k 'not test_initial_tool_policy_anchors_time_and_preserves_clarification'
& 'F:\LEARN KÌ 8\ĐATN\sag-laya-integration\SAG\apps\api\.venv\Scripts\python.exe' -m pytest tests/test_phase_2c_search_indexing.py tests/test_agent_history_acl.py -q
& 'F:\LEARN KÌ 8\ĐATN\sag-laya-integration\SAG\apps\api\.venv\Scripts\python.exe' -m ruff check <changed-python-files>
git diff --check
```

Combined focused suite: **146 passed, 1 deselected**; separate Checkpoint A index payload and agent-history ACL tests: **33 passed**. Test temporal-initial-tool riêng được chạy và fail ở P3/Laya behavior ghi trong `researchtask.md`. Ruff và diff check passed. Real services/benchmark chưa thể chạy vì B1 provider, trusted staging principals và gold corpus chưa sẵn sàng. Không yêu cầu frontend build cho deliverable backend/docs không đổi FE.

## 9. Impact và rollback dự kiến

- B2 reader/planner không cần migration; snapshot DTO không là DB schema. Không đổi producer payload, collection/index, DB model hay worker.
- Retrieval config mới được validate qua typed settings; production tuning vẫn cần benchmark/SLO, và config defaults chưa được coi là calibrated.
- Branch membership luôn được kết hợp với exact Project/tenant/partition/version ACL filters. Thiếu hoặc lỗi provider thì consumer giữ global ACL-scoped retrieval.
- Rollback runtime về canonical global-only Checkpoint A path; giữ ACL/readiness/citation guard. Không drop mappings/tree/SearchUnit data hoặc sửa worker khi rollback B2.
- Rủi ro chính: scope profile an toàn, slot retention, current/historical policy, model compatibility, deadline starvation và benchmark chưa agreed. Gate fixtures có thể làm trước; gate ROUTING_READY phải chờ thực integration evidence.
