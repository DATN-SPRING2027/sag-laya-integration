# Todo — SAG Knowledge Routing RAG

Nguồn chuẩn: [Workflow v1.1](../docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md). Thứ tự và phụ thuộc: [plan.md](plan.md). Các mục dưới đây là việc cần làm, chưa hàm ý trạng thái hiện tại của code.

## Phase 0 — Contracts & Foundations

- [x] Map flow upload, worker/job, search, query, schema, config và ACL hiện có; ghi rõ code tái sử dụng và gap.
- [x] Chốt Document/Version/SourceSnapshot/IngestionRun, stable ID, idempotency key, provenance và temporal fields.
- [x] Chốt stage/status/error contract và ý nghĩa riêng của SEARCH_READY, KNOWLEDGE_READY, FAILED.
- [x] Chốt Laya, query features/planner, retrieval trace, index/tree manifest và config version contracts.
- [x] Chốt tenant/project/security scope; rà migration, compatibility và rollback.
- [x] Evidence: [phase-0-contracts-and-foundations.md](../docs/phase-0-contracts-and-foundations.md).

## Phase 1 — Upload & Versioned Source

- [x] Giữ validation quyền, extension, MIME signature, size và policy trước khi xử lý file.
- [x] Stream/checksum source; xác nhận source identity và duplicate policy.
- [x] Tạo/liên kết Document, Version, SourceSnapshot, IngestionRun trong transaction.
- [x] Kiểm tra bốn trường hợp cùng hash/cùng identity, cùng hash/khác nguồn, hash mới/cùng identity, hash mới/nguồn mới.
- [x] Đảm bảo frontend retry dùng idempotency key, không tạo workflow trùng.
- [x] Ghi stage/status/error cho worker; xác nhận status API và UI phản ánh tiến trình/lỗi/retry.
- [x] Evidence: [test_phase_1_upload_and_versioning.py](../apps/api/tests/test_phase_1_upload_and_versioning.py) (34/34 passed).

## Phase 2A — Canonical Extraction

- [x] Xác nhận danh sách định dạng thực sự hỗ trợ và parser/fixtures cho từng định dạng.
- [x] Lưu canonical block type, ordinal, page range, section path, anchor và document version.
- [x] Kiểm tra normalization giữ bảng/code/punctuation/identifier và dấu vết boilerplate.
- [x] Xác nhận LLM không được dùng để sửa text mặc định.
- [x] Kiểm tra extraction output versioned/temp, retry và lỗi stage.
- [x] Evidence: [test_phase_2a_canonical_extraction.py](../apps/api/tests/test_phase_2a_canonical_extraction.py) (8/8 passed).

## Phase 2B — Dedup & Temporal

- [x] Kiểm tra file exact hash và block exact hash; reuse content vẫn giữ mọi provenance/evidence.
- [x] Kiểm tra near-duplicate tạo candidate cluster, có ngưỡng phù hợp loại dữ liệu.
- [x] Kiểm tra semantic similarity chỉ tạo candidate, không tự merge CONTRADICTS/SUPERSEDES.
- [x] Kiểm tra EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED và evidence mapping.
- [x] Kiểm tra published/observed/ingested time, validity, supersedes lineage và truy vấn lịch sử.
- [x] Xác nhận reprocess deterministic, retry an toàn và không mất lịch sử.
- [x] Evidence: [test_phase_2b_dedup_and_temporal.py](../apps/api/tests/test_phase_2b_dedup_and_temporal.py) (5/5 passed).

## Phase 2C — Search Index

- [x] Xác nhận Search Unit boundary theo heading/paragraph/table trước token window.
- [x] Giữ document/version, canonical block range, hash, page, section và security partition.
- [x] Tạo dense+sparse representation theo provider/model đã xác nhận; giữ riêng Search Unit và Knowledge Unit.
- [x] Tạo Qdrant payload indexes cho filter fields trước ingestion.
- [x] Upsert bằng stable point ID; kiểm tra reprocess không tạo point/vector trùng hoặc evidence cũ.
- [x] Lưu/verify index manifest; chỉ bật search readiness sau index nhất quán.
- [x] Xác nhận Qdrant có thể rebuild từ PostgreSQL/source artifacts.
- [x] Evidence: [test_phase_2c_search_indexing.py](../apps/api/tests/test_phase_2c_search_indexing.py) (4/4 passed).

## Phase 3 — Laya & Query Analyzer

- [x] Kiểm tra coarse intent CHAT/KNOWLEDGE/COMMAND/AMBIGUOUS và giữ query gốc/user scope.
- [x] Chỉ bỏ retrieval với CHAT confidence cao; low-confidence/AMBIGUOUS vẫn fallback.
- [x] Kiểm tra Laya lazy/singleton, timeout/init failure và fallback khi unavailable.
- [x] Trích deterministic exact terms, identifiers, paths, relation/temporal/global/multi-hop cues cho query trace.
- [x] Kiểm tra lỗi/nhãn Laya không xóa query hoặc context.

### Phase 3 review — API query flow

- [x] Tích hợp Laya và query analysis vào `/search`, `/search/stream` và source-scoped search flow.
- [x] Ghi `query_route` trace: coarse intent, confidence, requested/effective strategy, query analysis, scope và fallback reason.
- [x] Regression greeting, factual tiếng Việt, exact identifier, ambiguous/low-confidence và Laya error.
- [x] Giữ ngoài phạm vi Phase 3 này: retrieval fusion, evidence context, citation và ingestion/index lane.
- [x] Evidence: [phase-3-review-report.md](../docs/phase-3-review-report.md).

## Phase 4 — Retrieval Engine v1

- [ ] Chạy global hybrid search với scope/filter ACL, chưa phụ thuộc tree.
- [x] Global retrieval dùng RRF rank fusion; không cộng trực tiếp raw dense/sparse scores khác scale.
- [x] Relevance gate semantic-only bất biến khi retriever score được scale dương; có regression test.
- [x] Trước dense/lexical candidate generation, giao Project được ký với mapping CONFIRMED Project→Source; client Source IDs chỉ thu hẹp scope.
Operational ACL rollout/acceptance gates được theo dõi riêng trong [ACL evidence-path inventory](../docs/security/acl-evidence-path-inventory.md).
- [ ] Collapse exact/near duplicate; kiểm tra MMR giữ evidence đa dạng.
- [ ] Giới hạn candidate rerank và chỉ rerank khi còn latency budget.
- [ ] Build context theo coverage/diversity và token budget.
- [ ] Citation map được về source/version/block/page/anchor; kiểm tra no-answer khi evidence thiếu.
- [ ] Xác nhận LLM Settings độc lập Laya và chỉ nhận evidence pack.
- [ ] Ghi stage latency, requested/effective strategy và fallback trong trace.
- [ ] Tạo/chạy retrieval regression corpus theo Phụ lục E của workflow.

### P4 task proposal — Global retrieval, ACL và fusion

- [x] **Quyết định ACL:** BE/Continuum là authority; SAG nhận signed principal assertion đã xác minh và mapping allowed Project→Source. Project→Source là boundary hiện tại; client `source_ids` chỉ narrowing filter. Runtime verifier/resolver đã được triển khai; trust cấu hình và mapping production vẫn thuộc acceptance vận hành.
- [x] Thêm seam `SearchACLScope.authorized_source_ids`; explicit request được giao với scope trước khi truy vấn Source. Không gửi `source_ids` thì chọn candidate chỉ trong tập authorized, vẫn theo candidate limit hiện có.
- [x] Dense và lexical nhận cùng candidate Source list trước candidate generation/top-k; giữ logical-delete/reprocess prefilter hiện tại.
- [x] Fail closed khi scope thiếu (`503` trên request/`error` event trên SSE); scope rỗng hoặc request giao rỗng trả 0 evidence và không gọi retriever.
- [x] Wire signed assertion verifier, Project→Source mapping resolver và unmapped-source denial; kiểm tra bằng contract/runtime tests. Owner-approved backfill script có sẵn, production approval/run được theo dõi riêng.
- [x] Fuse semantic + lexical bằng RRF, không cộng raw score khác scale; score đầu ra chuẩn hóa về `[0, 1]` và ghi `fusion_method`/candidate counts.
- [x] Dedupe candidate theo source/chunk và dùng tie-break xác định; giữ max semantic score, representative nội dung ổn định/dài hơn và ưu tiên exact lexical trước expansion-only match.
- [x] Chuẩn hóa semantic-only relevance gate theo tỷ lệ so với score cao nhất trong cùng candidate set; test xác nhận scale `0.95/0.8` và `0.00095/0.0008` giữ cùng kết quả.
- [x] Relevance gate xét tín hiệu lexical và semantic theo từng candidate; lexical hit của candidate khác không chặn dense evidence.
- [x] Mô tả Search/Dify `score` là normalized RRF rank score; ghi rõ Dify `score_threshold` áp dụng trên thang này.
- [x] Global `/search` và `/search/stream` bỏ event/graph retrieval trong P4; response giữ graph arrays rỗng. Source-scoped P3 path không đổi.
- [x] Relevance/search strategy regressions: `test_retrieval_relevance.py`, `test_search_strategy.py`, `test_search_stream.py` pass.
- [x] ACL seam regressions: implicit scope, requested∩authorized, unauthorized/empty request, dense/lexical cùng scope, empty authorization, missing scope fail-closed trên `/search` và `/search/stream`; test dùng fake scope.
- [x] Không dùng Knowledge Graph/Tree trong global P4 retrieval; không sửa ingestion/index lane hoặc shared contract/config.
- [ ] Propagate Source provenance qua các lượt assistant phụ thuộc history; kiểm tra revoke khi lượt sau tóm tắt evidence cũ nhưng không có citation/tool riêng. Bộ lọc hiện chỉ xác minh provenance được ghi trên từng message.

### P4 evidence context / citation / no-answer — implementation status (2026-10-01)

- [x] Ghi chính xác retrieval-result fields và actual source/semantics trong [implementation plan](plan.md#existing-contracts).
- [x] Resolver gắn locator bằng exact SearchUnit/chunk ID, authorized SAG Source scope, active/READY Document, SEARCH_READY version và CanonicalBlock start/end cùng version; missing locator fail closed.
- [x] Search API và Agent chỉ pack whole traceable evidence nằm trong token estimate budget; reserve dùng `llm_max_tokens`, window dùng `llm_context_window`, Agent tính actual runtime messages/tool schemas mỗi turn.
- [x] Citation output có SAG source/document/version/chunk/page/anchor; validator chỉ giữ IDs của evidence đã render và được phép.
- [x] Search stream buffer raw model text tới citation validation; Agent ẩn answer deltas khi local grounding bắt buộc hoặc `search_context` đã chạy, còn direct/chat turns giữ contract; terminal gate loại output local không có provenance claim.
- [x] Regression có greeting, factual positive fixture, exact identifier, ambiguous giữ routing contract, empty evidence, missing locator, provenance ACL boundary, Agent citation/context fitting.
- [ ] Chờ PR #14 (Phase 2 Canonical/Search Index) merge, rồi cùng owner ingestion/index (phan tai) xác nhận corpus thật populate `SearchUnit`, chunk IDs khớp và locator truy xuất được trước khi đưa PR #13 ra khỏi Draft; legacy `SourceChunk` không map được thì sẽ no-answer cho tới khi có deliverable mapping/reindex được thống nhất.
- [ ] Calibrate answerability/claim coverage/entailment riêng; RRF và relevance score không phải confidence, nên hiện tại chỉ phát hiện structural weak và exact-anchor miss.
- [ ] Chạy end-to-end upload → extraction → index → retrieval → answer trên corpus có locator thật, cùng provider/model tokenizer/context-window verification.
- [ ] Consumer nghiệm thu stream single canonical delta/time-to-first-answer và kiểm tra full API suite; hiện có một agent routing baseline failure được ghi ở kiểm chứng của plan.

**Trạng thái:** P4 retrieval/fusion/ACL runtime code có contract/runtime tests để review; task evidence pack có code và focused regressions, nhưng còn các gap được đánh dấu ở trên. Production ACL rollout acceptance vẫn **BLOCKED** cho tới khi có signer/JWKS thật, mapping/backfill được duyệt và staging leakage/revocation tests đạt. Đây là cổng vận hành ACL; **P1** trong PR notes chỉ Priority 1 runtime ACL, không phải Phase 1 — Upload & Versioned Source. Chi tiết acceptance nằm trong [ACL evidence-path inventory](../docs/security/acl-evidence-path-inventory.md).

## Checkpoint A — SEARCH_READY end-to-end

### Ingestion / index lane (PR #16)

- [x] Upload → canonical extraction (2A) → dedup (2B) → SearchUnit/Qdrant indexing (2C) → manifest verified → `SEARCH_READY` end-to-end.
- [x] Search lane isolation: lỗi, trễ hoặc tắt knowledge enrichment/universe không chặn hoặc hạ cấp `SEARCH_READY`.
- [x] Resilience & Zero Secret Leakage: parse/index/manifest failure, empty index fail-gracefully (`EMPTY_INDEX`), retry idempotent và sanitizer cho error/log/DB.
- [x] Locator payload: `project_id`, `source_id`, `document_id`, `document_version_id`, `version_no`, `page_from`, `page_to`, `section_path`, `block_from_id`, `block_to_id`, `source_anchor`, `valid_from_ts`.
- [x] Evidence: [test_checkpoint_a_ingestion.py](../apps/api/tests/test_checkpoint_a_ingestion.py), [checkpoint-a-ingestion-plan.md](../docs/tai_task/checkpoint-a-ingestion-plan.md) và [checkpoint-a-ingestion-evidence.md](../docs/tai_task/checkpoint-a-ingestion-evidence.md). Xem evidence report để biết từng case và kết quả; PR head hiện tại đã bổ sung regression cho các review finding.

### Retrieval / context / citation lane

- [x] Global `/search` và `/search/stream` đọc Phase 2C SearchUnit theo Project; không gọi Knowledge Tree/event retrieval.
- [x] Candidate Sources bị giới hạn bởi CONFIRMED mapping và signed org/Project scope; Qdrant dense/sparse áp filter project/tenant/partition/version trước top-k; `source_ids` chỉ thu hẹp.
- [x] Xác minh current attempt/manifest, SEARCH_READY và version validity; retry/unready/deleting/reprocessing không dùng stale point.
- [x] Dense + sparse dùng RRF rank fusion; query nhiều scope được rank-interleave ổn định, không cộng raw score hay dùng RRF làm confidence.
- [x] `search_context` dùng cùng reader, token pack và citation numbering; enrichment/event/graph không thuộc read path.
- [x] Citation giữ source/document/version/SearchUnit/block range/page/section/anchor; canonical click đọc exact Qdrant point, xác minh hash và reauthorize Source/tenant/partition.
- [x] Empty/weak evidence trả no-answer; exact ID cần anchor coverage; index error/retry có bound và response không lộ exception/secret.
- [ ] Upload thật → worker Phase 2C → producer manifest → `/search`, `/search/stream`, `search_context` trên Qdrant thật; hiện có fixture vertical cho reader, chưa phải staging/provider E2E.
- [ ] Owner DATN-33/BE/security xác nhận principal tenant/partition, Project Source mapping, readiness/retry và embedding-model identity contract; assertion cũ thiếu tenant/partition sẽ fail closed.
- [ ] Staging với principal/data thật xác nhận leakage, revoke, enrichment off/lag/failure và navigation FE tới split SearchUnit.
- [ ] Calibrate answerability/entailment và exact split-unit offset; structural/exact-anchor gate chưa phải semantic confidence.

**Implementation status (2026-10-02):** reader/context/citation code và regressions đã làm trên task branch; PR review follow-up sửa readiness contract (`search_status=READY/SEARCH_READY`), reuse pooled Qdrant client, candidate grouping thừa và HTTP exception chaining. Relevant checks đạt **161 passed, 4 warnings**; Ruff và `git diff --check` pass. Xem [evidence và limitations](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20A%5D/researchtask.md). Không đánh dấu Checkpoint A toàn hệ thống hoàn tất: upload-to-real-index/provider/staging và owner contract vẫn mở.

## Phase 5 — Knowledge Units & Graph

- [ ] Xây Knowledge Unit ổn định, tách khỏi Search Unit.
- [ ] Kiểm tra E0 deterministic, E1 model extraction và điều kiện đưa việc sang E2.
- [ ] Đưa E2 vào queue async có token/day budget, concurrency, backpressure và retry độc lập.
- [ ] Gắn evidence/provenance/confidence/validity cho entity, alias, claim, relation.
- [ ] Sinh graph candidates từ semantic, lexical, entity, structure, citation và temporal signals.
- [ ] Kiểm tra missing-signal renormalization, calibration [0,1], version config/quantile và sparse degree cap.
- [x] Xác nhận lỗi/queue lag không chặn hoặc hạ SEARCH_READY.

## Phase 6 — Knowledge Routing Tree

- [x] Contract `routing-snapshot.v1` nhận Knowledge Unit/edge fixture độc lập SearchUnit; build topology riêng theo tenant/project và security partition.
- [x] Dùng deterministic Constrained Hierarchical Leiden CPM với seed theo membership, max community size, giant guard và balanced fallback; giữ bounds/repair/stop criteria trong config version.
- [x] Tạo node profile dense medoid, sparse terms, entities, temporal range và accessible-unit count; ID node ổn định theo tenant/project/partition/membership.
- [x] Ghi manifest checksum theo input/config/edges/lineage, metrics cohesion, giant ratio, child-size entropy, edge cut, depth, routing recall và từng quality gate.
- [x] Regression synthetic xác nhận deterministic rebuild, failed quality gate bị REJECTED và profile/edge không vượt security partition.
- [x] Fixture contract sẵn sàng cho B2 planner/escape phát triển mock-first: [test_phase_6_routing_tree.py](../apps/api/tests/test_phase_6_routing_tree.py) và [phase-6-tree-evidence.md](../docs/phase-6-tree-evidence.md).
- [ ] Tích hợp producer Knowledge Unit/Graph Phase 5 và persist node/profile/manifest/lineage; chỉ cập nhật active tree sau publish transaction. Phase 5 hiện chưa có Knowledge Unit store/builder trong repo, nên Phase 6 chỉ tạo candidate snapshot thuần và không đổi active pointer.
- [ ] Chạy benchmark routing recall trên corpus/query thật, hiệu chỉnh quality thresholds; fixture hiện là synthetic contract evidence.
- [ ] Hoàn tất end-to-end publish/failed-build-active-tree regression và ACL leakage kiểm tra trên DB/runtime.

## Phase 7 — Tree-guided Retrieval

- [x] Implement planner `qsp-v1`: deterministic primary strategy/modifiers, explicit mode override và reason codes; regression fixtures cover sáu mode.
- [ ] So sánh sáu mode với global-only trên gold corpus và ngưỡng benchmark đã thống nhất; xác nhận routing recall/latency.
- [ ] Chụp một snapshot tree/search/ACL cho mỗi query.
- [x] Consumer prune profile sai scope/không accessible trước beam; tree filter chỉ được AND thêm vào Project/tenant/partition/version ACL. Provider B1 thật chưa nối.
- [x] Beam route deterministic; entropy/margin giữ broad route khi tín hiệu chưa quyết định.
- [x] Fixture kiểm tra branch-local + authorized global escape cho wrong route và local timeout; real B1 tree output còn chờ.
- [x] Giữ RRF→canonical verification→dedup/MMR→coverage; giới hạn candidate hydration/rerank toàn truy vấn. Graph adapter chưa có nên skip an toàn và trace rõ.
- [x] Snapshot/local/escape budget exhaustion có global/rank-interleave fallback phù hợp; chưa benchmark latency trên hạ tầng thật.
- [x] Trace planner/snapshot/nodes/version/reason/escape/coverage/blackhole và selection fallback.
- [ ] Kiểm tra ACL leakage và routing blackhole bằng principal có quyền khác nhau.

Implementation slice B2 đã có fixture test (xem `SAG/docs/Thang_Task/[SAG][B2]/todo.md`); các mục cần DATN-37/Phase 5, principal thật, gold corpus và benchmark vẫn mở. Không đánh dấu `ROUTING_READY` khi chưa có integration evidence.

## Checkpoint B — ROUTING_READY

- [ ] Tree ổn định và routing recall đạt ngưỡng benchmark.
- [ ] Escape retrieval cứu được query route sai/inaccessible.
- [ ] Tree không publish khi quality/ACL blackhole gate thất bại.

## Phase 8 — Incremental Tree

- [x] Gán dữ liệu mới vào base + delta; cập nhật node/ancestor và drift signals.
- [x] Chỉ rebuild subtree khi drift/quality gate yêu cầu; giữ stable node lineage.
- [x] Build inactive routing slot và cập nhật Qdrant dual-slot payload.
- [x] Verify manifest/checksum/quality trước khi đổi active pointer.
- [x] Query trong lúc publish đọc một snapshot nhất quán.
- [x] Inject/kiểm tra lỗi build và publish; active tree cũ vẫn phục vụ và rollback được.

**Implementation slice (2026-10-04):** task branch `feat/Thang-checkpoint-c-blue-green-be-api-db` thêm blue-green publisher, durable query-slot lease, indexed Source/version/partition profile snapshot và focused SQLite/mock regressions (37 tests liên quan pass sau follow-up review). Xem [Checkpoint C evidence và giới hạn](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20C%5D/evidence.md). Các checkbox Phase 8/Checkpoint C vẫn mở: DATN-58 producer chưa nối, PostgreSQL advisory-lock/migration và Qdrant thật chưa được kiểm chứng; Checkpoint B vẫn chưa hoàn tất trên task base.

## Checkpoint C — INCREMENTAL_READY

- [x] Ingest bình thường không đòi full tree rebuild.
- [x] Drift, subtree rebuild, publish, concurrent query và rollback đã kiểm chứng.

## Phase 9 — Knowledge Quality & Gap

- [ ] Tính coverage, source diversity, freshness, contradiction, query demand, uncertainty và growth.
- [ ] Gap priority có công thức/config version và evidence/reason truy nguyên được.
- [ ] Xác nhận metrics/scoring có cấu trúc chạy trước LLM question generation.
- [ ] LLM chỉ diễn đạt gap thành câu hỏi; không tự mở research task vô điều kiện.

## Phase 10 — Research Agent

- [ ] Kiểm tra Gap Detector → Question Generator → Research Planner → discovery/fetch.
- [ ] Mỗi task lưu gap/source/reason, policy, budget và trạng thái.
- [ ] Fetch đi qua normal ingestion → dedup/version → tree update.
- [ ] Kiểm tra crawl/fetch limit, allowlist, idempotency, duplicate guard và điều kiện dừng.

## Checkpoint D — AGENT_READY

- [ ] Research task bắt nguồn từ gap signal có căn cứ.
- [ ] Ingestion loop có giới hạn và dừng khi không có thông tin mới.

## Phase 11 — Hardening

- [ ] Cache keys chứa scope/ACL fingerprint/epoch, search/tree/model/planner/config version và filters cần thiết.
- [ ] Answer cache tắt mặc định; semantic route cache chỉ bật sau khi đo precision và ACL isolation.
- [ ] Đo p50/p95/p99 theo stage và end-to-end; đặt SLO/latency budget theo benchmark môi trường.
- [ ] Chạy regression/load, ACL leakage/blackhole, retry/failure, cache invalidation, publish/rollback checks.
- [ ] Hoàn thiện dashboards/alerts và migration/rollback/rebuild runbook.

## Hoàn tất

- [ ] Mọi mục Definition of Done ở [plan.md](plan.md) có evidence.
- [ ] PostgreSQL là source of truth; Qdrant rebuild được.
- [ ] UI phân biệt processing, SEARCH_READY, KNOWLEDGE_READY và FAILED.
- [ ] Query grounded hoặc no-answer đúng; citations và fallback trace truy nguyên được.
- [ ] Regression, integration và các failure checks quan trọng đạt.
