[SAG][B2] Query planner, tree-guided retrieval & escape

Nguồn chuẩn: sag-laya-integration/SAG/tasks/plan.md (Phase 7, Checkpoint B) và tasks/todo.md (Phase 7, Checkpoint B).

Phụ thuộc và chạy song song

Cùng chờ Checkpoint A (DATN-33/DATN-34) và Phase 5 Knowledge Units & Graph. Chạy song song với B1 (DATN-37) bằng RoutingSnapshot/NodeProfile contract và mock fixture đã thống nhất sớm; tích hợp end-to-end sau khi tree output sẵn sàng.

Phạm vi

Hoàn thiện deterministic QueryStrategyPlanner: map query features sang primary strategy + modifiers (EXACT, LOCAL_FACTUAL, ENTITY_RELATIONAL, TEMPORAL, GLOBAL_TOPIC, MULTI_HOP); lưu planner version và reason codes.

Chụp snapshot nhất quán tree/search/ACL cho mỗi query; prune node inaccessible trước beam, route bằng ACL-safe profiles và giữ broad branch khi entropy/margin chưa quyết định.

Chạy branch-local hybrid retrieval cùng global escape theo budget. Route rỗng, sai, inaccessible hoặc coverage yếu phải thử global escape trong authorized scope. Giữ fusion → dedup → MMR → coverage; graph expansion/rerank bounded và có thể bỏ khi hết latency budget.

Trace tree version/selected nodes, requested/effective strategy, reason codes, fallback và blackhole.

Acceptance

Regression Exact, Local Factual, Entity Relational, Temporal, Global Topic và Multi-hop đạt ngưỡng benchmark đã thống nhất.

Route sai hoặc inaccessible không làm recall về 0; global escape phục hồi evidence được phép mà không rò rỉ ACL.

Trace cho biết snapshot, strategy, reason, fallback/blackhole; latency exhaustion có fallback phù hợp.

Test planner/escape trên contract fixture trước, rồi regression end-to-end với output DATN-37; cập nhật evidence và todo Phase 7.

Ranh giới
Lane này sở hữu planner và tree-guided retrieval/escape, không xây topology/profile/quality gates của B1 và không làm incremental update/publish/rollback của Checkpoint C (DATN-36).