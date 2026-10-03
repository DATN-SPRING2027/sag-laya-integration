# Todo — [SAG][B2] / Thang

Baseline `0f73c1b`; branch `feat/Thang-sag-b2-query-planner-be-api`. Nguồn: [task spec](%5BSAG%5D%5BB2%5D.md), [research](research.md), [plan](plan.md). Checkbox implementation chỉ đánh dấu khi có evidence.

## Research đã hoàn thành

- [x] Đọc task spec, Phase 7/Checkpoint B, Workflow v1.1 và schema foundations.
- [x] Trace global Search/stream, canonical SearchUnit reader, tool/agent, ACL, readiness, fusion, context và citation click.
- [x] Xác nhận PR #15/#16 đã merge; phân biệt mock upload/manifest evidence với real services acceptance.
- [x] Lập bảng current contracts và gaps planner/snapshot/profile/graph/MMR/coverage/budget/trace.
- [x] Ghi proposal B1 handoff, thuật toán, file ownership, matrix regression, benchmark gates và rollback.

## P0 — Chốt consumer/provider contract

- [ ] DATN-37 xác nhận RoutingSnapshot/NodeProfile DTO và shared fixture; lưu reference xác nhận.
- [ ] Chốt profile scope proof khi client narrow Source hoặc mapping bị revoke; partial profile không được dùng như safe centroid.
- [ ] Chốt primary+secondary+bounded descendant membership, slot/version suffix và old-slot retention cho in-flight queries.
- [ ] Chốt consistent read/frozen run-stage-manifest identity/recheck intersection; ACL revision/expiry semantics nếu có.
- [ ] Chốt Phase 5 entity/graph adapter và canonical KnowledgeUnit→SearchUnit mapping, validity/provenance.
- [ ] Chốt model identity/calibration/config và current/historical version+timezone policy.
- [ ] Chốt gold corpus và recall/latency acceptance thresholds; không gọi Phase-0 defaults là benchmark đã agreed.
- [ ] Thống nhất diff shared files với owner trước edit; tách DB/index writer lane nếu contract cần storage change.

P0 cho phép planner/fixture/consumer global-fallback làm song song bằng proposed contract; integration và broader acceptance vẫn mở khi chưa có provider/benchmark evidence.

## P1 — Planner

- [x] Typed plan: primary/modifiers/version/features/reasons; deterministic precedence và explicit override compatibility.
- [ ] Exact identifier/UUID/hash/path/env/quote, date constraints, entity/relation/global features có benchmark fixture đa ngôn ngữ.
- [x] TEMPORAL composable; planner không tự biến cue thời gian thành filter/version policy. Temporal history semantics vẫn cần owner policy.
- [ ] MULTI_HOP relation-facet coverage và bounded graph adapter; hiện chỉ có structural no-evidence/missing-anchor escalation, adapter Phase 5 chưa tồn tại.
- [ ] CHAT high confidence skip và AMBIGUOUS/Laya failure giữ retrieval contract.
- [x] Six explicit modes, deterministic/reason-code fixtures và temporal composition có regression; long-query/graph benchmark vẫn cần kiểm tra.

## P2 — Snapshot và beam

- [x] Frozen typed request/group snapshot contract, exact per-Project scope fingerprint, tree slot/version/epoch, total profile cap và branch membership cap. Provider/consistent-read producer của DATN-37 chưa tích hợp nên query hiện fallback global nếu thiếu.
- [ ] Recheck intersect snapshot; revoke/retry/new-version races không resurrect hoặc thêm evidence.
- [x] ACL-safe profile scope validation và inaccessible/zero-count prune trước scoring/entropy/beam.
- [x] Normalized available signals; deterministic tie; zero/one/tie/broad entropy-margin behavior; depth/beam cap.
- [x] Branch filter slot membership + tree version ANDs exact base ACL ở cả dense/sparse; over-limit membership/provider payload falls back globally; producer payload/index provisioning không đổi.
- [x] Provider off/timeout/missing/malformed snapshot → authorized global retrieval với reason trace. Tree freshness/model-identity checks vẫn thuộc provider contract.

## P3 — Retrieval core và diversity

- [x] Bounded candidate unit IDs (hard cap 1,024) trước canonical hydration/rerank; branch-local/global escape hợp nhất trước final top-k.
- [x] Escape reserve từ đầu; authorization DB, provider, embedding, query paths và hydration dùng monotonic request deadline.
- [x] Fixture wrong accessible route (có local hit nhưng bỏ sót unit) và local timeout; global escape phục hồi unit trong authorized scope. Inaccessible profile được prune trước beam; delta/real-tree E2E còn thiếu.
- [x] Một vote mỗi unit/channel khi local+escape trùng; giữ RRF semantics và cross-scope rank interleave.
- [x] Canonical exact dedup giữ locator; near-duplicate content dedup có ngưỡng config và regression. Distinct-version/fact benchmark vẫn mở.
- [x] MMR deterministic và bảo toàn quoted/exact/identifier/path anchors; time/entity facet calibration chưa có entity extractor/gold benchmark.
- [x] EMPTY/WEAK/structurally-sufficient/UNKNOWN structural anchor states are distinct; semantic answerability remains `unknown` and no RRF confidence threshold is used. Entity/time facet calibration still lacks extraction/benchmark support.
- [ ] Phase 5 graph adapter có guard hop/node/time/ACL+provenance; absent adapter skip có trace.
- [x] Rerank bounded by `search_unit_candidate_limit`; khi remaining budget thấp thì dùng deterministic rank-interleave fallback và ghi reason. Rerank model/benchmark không thêm.
- [ ] Index failure/timeout không biến thành empty/no-answer; cancellation dọn in-flight tasks.

## P4 — API/tool/context/trace

- [x] Global Search/stream và search_context cùng planner/retrieval service; legacy `strategy` giữ nguyên, `retrieval_mode` là field additive.
- [x] Trace per-Project snapshot/slot+epoch/tree, requested/effective mode, reasons, selected accessible nodes, escape, blackhole và fallback.
- [ ] Stage latency/remaining budget/skip/error/partial status xuất hiện ở outcome/tool event.
- [ ] Không expose assertion/secret/raw exception/hidden node IDs, profile text hay unauthorized counts.
- [ ] Context dùng remaining token budget; citations chỉ từ final pack và click reauthorize đúng provenance.
- [ ] Giữ SSE event order, grounded fallback, no-answer và agent citation offset/history ACL regressions.

## P5 — Integration, review, evidence

- [x] Contract fixture tests planner/router/escape/snapshot ACL pass; kết quả runtime được ghi trong `researchtask.md` sau khi hoàn tất test.
- [x] Existing focused SearchUnit/RRF/Search/stream/tool/agent regression chạy; một test baseline agentic temporal-initial-tool bị loại riêng vì lệch Laya model routing ngoài scope, ghi kết quả chính xác trong `researchtask.md`.
- [ ] Integration với DATN-37 published output thật; mock tests không thay integration gate.
- [ ] Upload→verified manifest→tree-guided/global escape→API/tool→click với actual PG/Qdrant và trusted principals.
- [ ] Sáu modes đạt agreed benchmark; compare global-only baseline, record recovery/blackhole/leakage/latency.
- [x] Code review tìm finding; fix các vấn đề về candidate cap/fallback budget và bỏ formatting churn ngoài phạm vi.
- [x] Ruff changed Python files, diff check; final status/history/secrets/unrelated edits cần ghi sau vòng kiểm tra cuối.
- [x] Cập nhật researchtask + Phase 7 todo theo evidence; giữ B1/Checkpoint B gates mở.
- [ ] Commit/PR handoff nêu DB/config/security/risks/rollback; PR target main, merge do người có quyền.

## Acceptance cuối task

- [x] Six-mode deterministic planner có version/reasons và requested/effective semantics fixtures.
- [ ] Snapshot production nhất quán; consumer prune inaccessible; broad uncertain route; exact ACL vẫn nằm trên mọi Qdrant path. B1 provider chưa có.
- [ ] Route sai/inaccessible không làm recall về 0 trên authorized gold corpus theo benchmark đã thống nhất.
- [x] Fixture escape recovery kiểm tra wrong nonempty route, timeout và tree-provider unavailable; real tree off/lag/failure integration còn mở.
- [x] Fusion→dedup→MMR→coverage và bounded rerank fallback có fixture; graph adapter, benchmark và latency SLO còn mở.
- [ ] Citation/no-answer/secret safety không hồi quy.
- [ ] Threshold/corpus/provider integration evidence đủ để đóng phần B2; broader ROUTING_READY chỉ đóng khi cả B1/B2 gates đủ.
