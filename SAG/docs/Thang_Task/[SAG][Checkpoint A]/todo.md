# Todo — Checkpoint A / Thang

Nguồn phân tích: [research.md](research.md). Baseline `6035130`; branch `feat/Thang-checkpoint-a-search-ready-be-api`.

## Đã thực hiện ở giai đoạn research

- [x] Đọc task spec, plan/todo chuẩn và trace producer → Search/tool → pack → citation click.
- [x] Lập bảng payload/PG fields, phân biệt point ID / SearchUnit ID / legacy chunk ID.
- [x] Trace ACL, hai principal contracts, ready status, token budget, fusion/relevance và enrichment dependencies.
- [x] Ghi phương án implementation, matrix regression, file ownership, gaps và acceptance.
- [x] Implement reader/context/citation/no-answer trên task branch sau khi Thang yêu cầu.

## P0 — Contract và shared ownership

- [ ] Ghi xác nhận DATN-33: search-ready độc lập enrichment; READY/SEARCH_READY, document lifecycle, zero units, current verified attempt, retry invalidation.
- [ ] Ghi quyết định BE/security: trusted org→tenant và partition authority; mapping CONFIRMED cho upload Project Source. Không tự cấp quyền từ config/request.
- [ ] Chốt collection/model identity và nhiều Source/project; compatible cross-project channel ranks.
- [ ] Chốt sparse query tokenizer/hash/weights với producer IDF; không sửa encoder producer trong reader lane.
- [ ] Chốt current/historical version và click policy; exact split unit content/parent anchor limitation.
- [ ] Thống nhất shared files với owner trước edit; owner worker/index giữ lane riêng.

P0 không cấm làm adapter/DTO/test scaffolding song song, nhưng các policy phụ thuộc contract và acceptance phải chờ quyết định thật.

## P1 — Reader và scope

- [x] Thêm read-only SearchUnit Qdrant adapter: dense/sparse named queries, exact point read, parsing và sanitized errors.
- [x] Thêm service scope + verified current SearchReady versions từ PG, query filters trước top-k ở cả channel, fan-out bounded bởi candidate limit/concurrency hiện có.
- [x] Hydrate theo batch, verify source/version/unit/tenant/project/partition/hash, block endpoints và locator.
- [x] Recheck mapping/lifecycle/current manifest sau Qdrant và trước evidence pack; click reauthorize; không cache grants.
- [x] Canonical selector chỉ thêm Project Sources đã CONFIRMED vào đường canonical; không đổi default legacy selector hay tự tạo mapping.
- [x] Reuse RRF/dedupe/relevance semantics, rank interleave ổn định, không cộng raw scores hay dùng RRF làm confidence.
- [x] Index error/retry bounded; canonical path không fallback legacy/graph và không biến lỗi thành empty.

## P2 — API/tool/context/citation

- [x] Wire `/search` và `/search/stream` vào reader chung; giữ routing CHAT/ambiguous và SSE contract.
- [x] Wire global `search_context` vào reader chung, bỏ event/graph/tree khỏi đường evidence.
- [x] Bổ sung additive explicit unit/block range/section provenance ở DTO/schema/tool/citations.
- [x] Pack theo budget/token estimator hiện có; citations chỉ map evidence trong pack.
- [x] EMPTY/WEAK no-answer; SUFFICIENT structural-only, answerability gap ghi bên dưới.
- [x] Citation click resolve exact canonical unit dưới Source/tenant/partition scope, verify hash và manifest; mismatch/unready/revoke fail closed.
- [x] Giữ legacy route cho ID legacy thật; FE response giữ `chunk_id` alias và bổ sung canonical locator. FE click/render với split unit vẫn cần nghiệm thu.
- [x] Sanitize API/index errors; traces không chứa assertion, key, private point payload.

## P3 — Regression và evidence

- [ ] Vertical upload→worker→verified producer-written index→Search/SSE/tool; không fake ready/legacy matching hits.
- [x] Mock Qdrant transport khẳng định dense/sparse filter trước limit; real-instance smoke còn mở.
- [x] ACL narrowing, wrong project/source, missing tenant/partition fail-closed và mapping CONFIRMED gate có regressions; cross-org/revocation staging còn mở.
- [x] Failed/unready current status, retry manifest invalidation và deleting/reprocess barrier được kiểm tra; producer zero-unit/retry E2E còn owner gate.
- [x] RRF stability/exact identifier/positive-scale relevance gates được regression; cross-model compatibility và calibrated negative semantic corpus còn gap.
- [x] Existing token overhead/CJK/history/output reserve/zero-budget và hash/range trace tests chạy.
- [x] Citation provenance và exact click service+route; wrong project/unready fail closed. FE navigation/render và staging revoke còn mở.
- [x] Greeting/factual/exact/ambiguous/no evidence; `search_context` test fail nếu đụng enrichment graph.
- [x] Transient retry, exhausted/malformed response, sanitized errors có regression; real missing collection/provider deployment smoke còn mở.
- [x] Bộ suites retrieval/store/traceability, stream/agent, ACL/strategy và Phase 2C indexing/worker chạy lại sau review (**161 passed, 4 warnings**); xem lệnh và shim limitation trong `researchtask.md`.

## P4 — Handoff

- [x] Hoàn thành code review và follow-up review PR #15; sửa readiness `DocumentVersion`, Qdrant client pooling, channel candidate grouping và exception chaining; regression xác nhận.
- [x] Cập nhật nguồn chuẩn `SAG/tasks/plan.md` và `todo.md` với trạng thái implementation cùng các gate còn mở.
- [x] Điền final results vào researchtask.md: branch/commit state, checks, finding/fix, remaining gaps, DB/config/security impact và rollback.
- [ ] Giữ gap tokenizer/answerability/anchor offset/model/producer/external smoke còn mở nếu chưa giải quyết.
- [x] Commit trên task branch và mở PR #15 vào main; follow-up review fix sẽ tiếp tục trên cùng PR.

## Definition of done

Checklist acceptance ở research.md là gate của lane này. Implementation, focused regression, review và task evidence đã hoàn thành trên branch; commit/PR handoff, real upload-index-provider smoke và owner contract gates còn mở. Checklist toàn Checkpoint A chưa hoàn tất.
