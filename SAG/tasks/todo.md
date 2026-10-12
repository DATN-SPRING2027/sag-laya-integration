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
- [x] Collapse exact/near duplicate; kiểm tra MMR giữ evidence đa dạng (Đã kiểm chứng tại `retrieval_service.py:dedup_and_diversify_evidence_mmr`, 4 regression tests trong `test_retrieval_relevance.py:175-260` từ PR #13 / Checkpoint B2).
- [x] Giới hạn candidate rerank và chỉ rerank khi còn latency budget (Đã kiểm chứng tại `search_unit_retrieval_service.py:_rerank_with_budget`, config `SAG_SEARCH_RERANK_MIN_REMAINING_SECONDS` và candidate cap trong `test_search_unit_retrieval_service.py`).
- [x] Build context theo coverage/diversity và token budget (Đã kiểm chứng tại `search_context.py:build_evidence_context_pack`, token window calculation trong `test_retrieval_relevance.py` và Checkpoint A lane).
- [x] Citation map được về source/version/block/page/anchor; kiểm tra no-answer khi evidence thiếu (Đã kiểm chứng tại `SearchCitationValidator` trong `citation_service.py`, fail-closed no-answer trong `test_search_unit_retrieval_service.py` và PR #13).
- [x] Xác nhận LLM Settings độc lập Laya và chỉ nhận evidence pack (Đã kiểm chứng tại `core/config.py`, pipeline prompt tách biệt Laya query analysis và chỉ nhận formatted SearchUnit evidence pack).
- [x] Ghi stage latency, requested/effective strategy và fallback trong trace (Đã kiểm chứng tại `SearchTraceContext`, trường `stage_latencies_ms`, `query_route` trace và verified trong `test_search_strategy.py`, `test_search_stream.py`).
- [x] Tạo/chạy retrieval regression corpus: bổ sung test suite `test_phase_4_fusion_retrieval.py` kiểm chứng scale invariance, Vietnamese domain queries, semantic paraphrase, exact identifiers và deterministic ordering; xem [phase-4-fusion-retrieval-evidence.md](../docs/tai_task/phase-4-fusion-retrieval-evidence.md).

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
- [x] Relevance/search strategy regressions: `test_retrieval_relevance.py`, `test_search_strategy.py`, `test_search_stream.py`, `test_phase_4_fusion_retrieval.py` pass (99/99 passed).
- [x] ACL seam regressions: implicit scope, requested∩authorized, unauthorized/empty request, dense/lexical cùng scope, empty authorization, missing scope fail-closed trên `/search` và `/search/stream`; test dùng fake scope.
- [x] Không dùng Knowledge Graph/Tree trong global P4 retrieval; không sửa ingestion/index lane hoặc shared contract/config.
- [ ] Propagate Source provenance qua các lượt assistant phụ thuộc history; kiểm tra revoke khi lượt sau tóm tắt evidence cũ nhưng không có citation/tool riêng. Bộ lọc hiện chỉ xác minh provenance được ghi trên từng message.

**Implementation status (2026-10-12, DATN-67):** Toàn bộ 99/99 tests của tổ hợp Phase 4 Fusion & Retrieval đạt 100% passed (`test_phase_4_fusion_retrieval.py`, `test_retrieval_relevance.py`, `test_search_unit_retrieval_service.py`, `test_search_strategy.py`, `test_search_stream.py`). Đã kiểm chứng tính bất biến thang đo (scale invariance), consensus boost, fallback fingerprint khi thiếu chunk_id, tie-breaking xác định và các kịch bản tiếng Việt chuyên ngành. Xem chi tiết tại [phase-4-fusion-retrieval-evidence.md](../docs/tai_task/phase-4-fusion-retrieval-evidence.md).

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
- [ ] Owner DATN-61 cùng BE/security/ingestion xác nhận principal tenant/partition, Project Source mapping, readiness/retry và embedding-model identity contract; assertion cũ thiếu tenant/partition sẽ fail closed. DATN-33 (ingestion/readiness) và DATN-34 (retrieval/citation) đã Done nhưng không thay thế xác nhận production identity/mapping.
- [ ] Staging với principal/data thật xác nhận leakage, revoke, enrichment off/lag/failure và navigation FE tới split SearchUnit.
- [ ] Calibrate answerability/entailment và exact split-unit offset; structural/exact-anchor gate chưa phải semantic confidence.

**Implementation status (2026-10-02):** reader/context/citation code và regressions đã làm trên task branch; PR review follow-up sửa readiness contract (`search_status=READY/SEARCH_READY`), reuse pooled Qdrant client, candidate grouping thừa và HTTP exception chaining. Relevant checks đạt **161 passed, 4 warnings**; Ruff và `git diff --check` pass. Xem [evidence và limitations](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20A%5D/researchtask.md). Không đánh dấu Checkpoint A toàn hệ thống hoàn tất: upload-to-real-index/provider/staging và owner contract vẫn mở.

**Research follow-up (2026-10-06, DATN-61):** Xem [Checkpoint A production E2E research](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20A%5D2/researchtask.md). Local code xác nhận Project upload tự tạo Project Source nhưng không tạo `CONFIRMED` SourceProjectMapping; search cần mapping này cùng assertion có org/Project/tenant/partition hợp lệ. Test upload hiện vẫn dùng `MockQdrantStorage`/`FakeIngestionEngine`; local Docker không có SAG PostgreSQL/Qdrant runtime đang chạy và task chưa có staging profile, principal hoặc corpus được chỉ định. Vì vậy các mục production E2E, owner contract, post-revoke leakage và browser split-SearchUnit vẫn **chưa nghiệm thu**; không suy rộng fixture tests thành production evidence.

**Local validation follow-up (2026-10-06):** Trên nhánh mới từ `e2de5e7`, API focused suite đạt **170 passed** và FE focused suite đạt **39 passed**; Ruff đạt trên ba test file SearchContext/ACL/agent đã sửa. Full API suite chưa chạy lại trên base mới. Lần full suite trên base cũ `64ec5b4` có **903 passed, 25 failed, 1 skipped, 8 errors, 108 warnings**; không có lỗi trong các test SearchContext đã cập nhật. Chi tiết và giới hạn attribution nằm trong [researchtask.md](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20A%5D2/researchtask.md). PR #23 là bước chuẩn bị local, **không đóng DATN-61 và không phải production acceptance**. Các kết quả local dùng fixture/test doubles; staging/production gates vẫn mở và chưa xác nhận principal, Project→Source mapping hoặc embedding identity.

**Owner follow-up (kiểm tra 2026-10-11; yêu cầu đăng 2026-10-06):** Đã đăng [yêu cầu xác nhận contract và staging trên DATN-61](https://trankimthang0207.atlassian.net/browse/DATN-61?focusedCommentId=10107). Jira hiện ở trạng thái In Review với hai comment, chưa có owner response; chưa nhận được quyết định, profile/corpus hay quyền staging. Giữ các mục production chưa hoàn tất.

**Review follow-up (2026-10-11):** Đổi tên upload/worker regression để nêu rõ dùng `MockQdrantStorage` và `FakeIngestionEngine`; test mới pass **1/1**. Cập nhật research và evidence để phân biệt local regression với production acceptance. Các mục upload-to-real-Qdrant, post-revoke ACL và browser acceptance vẫn mở.

## Phase 5 — Knowledge Units & Graph

- [x] Xây Knowledge Unit ổn định theo canonical passage/version/scope, tách khỏi Search Unit; rebuild/reparse giữ ID, provenance, validity và checksum; temporal validity/lineage thay đổi sẽ invalidate và rebuild evidence.
- [x] E0 deterministic; E1 lightweight domain patterns có giới hạn; chọn E2 cho semantic relation hoặc low-confidence/high-importance. Entity/claim/relation là evidence candidates, không tự nâng thành fact.
- [x] Queue PostgreSQL async độc lập: token/day budget theo tenant, concurrency toàn worker, capacity/max-age backpressure, lease recovery, timeout và bounded retries.
- [x] Gắn exact block/quote/span, source/version/snapshot, extractor version, confidence và validity cho entity, alias, claim, relation; từ chối E2 thiếu anchor hoặc input stale.
- [ ] Sinh graph candidates từ semantic, lexical, entity, structure, citation và temporal signals. *(PR #24 hoàn tất lexical, entity, structure, citation, temporal và missing-signal renormalization; semantic dense representation cho Knowledge Unit được để mở làm follow-up khi tích hợp Knowledge embedder).*
- [x] Calibration [0,1], BM25 q05/q95 theo partition, version config/weights và undirected degree cap; persist/verify graph manifest và edge rows.
- [x] Xác nhận lỗi/queue lag không chặn hoặc hạ SEARCH_READY.
- [x] Rebuild từ PostgreSQL canonical/source artifacts; replay response E2 đã validate sau khi mất derived store, không gọi lại model hoặc tính thêm budget; operator retry qua CLI.
- [x] Regression review: E2 chưa cấu hình/disabled backlog không chặn FOUNDATION; completion thắng lease recovery không bị claim/token charge lặp; old lease không ghi đè success/failure; rebuild sửa text/scope/ordinal bị hỏng từ canonical input.
- [ ] Calibrate specialized E1 NER/RE và E2 provider thật trên corpus nghiệp vụ mở rộng khi có model/provider và gold labels được duyệt; hiện dùng E1 patterns và E2 validated fake-provider regressions.
- [x] Evidence: [knowledge-foundation-evidence.md](../docs/tai_task/knowledge-foundation-evidence.md), [corpus results](../docs/tai_task/knowledge-corpus-results.json) và [test_knowledge_foundation.py](../apps/api/tests/test_knowledge_foundation.py).

## Phase 6 — Knowledge Routing Tree

- [x] Contract `routing-snapshot.v1` nhận Knowledge Unit/edge fixture độc lập SearchUnit; build topology riêng theo tenant/project và security partition.
- [x] Dùng deterministic Constrained Hierarchical Leiden CPM với seed theo membership, max community size, giant guard và balanced fallback; giữ bounds/repair/stop criteria trong config version.
- [x] Tạo node profile dense medoid, sparse terms, entities, temporal range và accessible-unit count; ID node ổn định theo tenant/project/partition/membership.
- [x] Ghi manifest checksum theo input/config/edges/lineage, metrics cohesion, giant ratio, child-size entropy, edge cut, depth, routing recall và từng quality gate.
- [x] Regression synthetic xác nhận deterministic rebuild, failed quality gate bị REJECTED và profile/edge không vượt security partition.
- [x] Fixture contract sẵn sàng cho B2 planner/escape phát triển mock-first: [test_phase_6_routing_tree.py](../apps/api/tests/test_phase_6_routing_tree.py) và [phase-6-tree-evidence.md](../docs/phase-6-tree-evidence.md).
- [x] Tích hợp producer Knowledge Unit/Graph Phase 5 với builder hiện có; persist versioned node/profile/manifest/lineage trong PostgreSQL dưới `knowledge-candidate.v1`, trạng thái INACTIVE hoặc REJECTED; verify input/config/edge/profile/lineage checksums.
- [x] Benchmark từ 5 tài liệu repo thật và 13 query có gold source anchors: 402 units, 2.379 edges, recall@10 = 12/13 (92,3%) ≥ 80%; ghi quality thresholds, calibration và từng query. Hai tiến trình với PYTHONHASHSEED khác nhau tạo cùng candidate checksum.
- [x] PostgreSQL/runtime regressions cho tenant/project/partition/source isolation, source revocation, checksum corruption và failed quality/ACL gates; candidate build không đổi active pointer.
- [ ] DATN-58–60: nối candidate vào incremental publish/active-slot switching, SearchUnit membership và rollback E2E. Phần này ngoài scope knowledge foundation; không đánh dấu ROUTING_READY/INCREMENTAL_READY.
- [ ] Mở rộng benchmark với held-out corpus/query nghiệp vụ, signed principal và staging leakage/revocation trên dữ liệu thật trước rollout.
- [x] Evidence producer/persistence/corpus: [knowledge-foundation-evidence.md](../docs/tai_task/knowledge-foundation-evidence.md). Corpus này đại diện kiến trúc/ingestion/security của repo, không thay nghiệm thu provider/Qdrant/production ACL.

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

Implementation slice B2 và các phụ thuộc Phase 5 là lịch sử riêng trong `SAG/docs/Thang_Task/[SAG][B2]/todo.md`. DATN-35 / `ROUTING_READY` được cung cấp là dependency đã hoàn tất; PR này không revalidate integration evidence.

## Checkpoint B — ROUTING_READY

- [x] Tree ổn định và routing recall đạt ngưỡng benchmark. *(DATN-35 / `ROUTING_READY` được xác nhận hoàn tất theo task dependency; không revalidate trong PR này.)*
- [x] Escape retrieval cứu được query route sai/inaccessible. *(DATN-35 / `ROUTING_READY` được xác nhận hoàn tất theo task dependency; không revalidate trong PR này.)*
- [x] Tree không publish khi quality/ACL blackhole gate thất bại. *(DATN-35 / `ROUTING_READY` được xác nhận hoàn tất theo task dependency; không revalidate trong PR này.)*

## Phase 8 — Incremental Tree

- [x] Unit/service contract gán base + delta, cập nhật node/ancestor và drift signals; retry idempotent.
- [x] Service contract chọn subtree/partition cần rebuild, giữ stable lineage và tích lũy quality gates qua mọi subtree trong batch.
- [x] Build inactive routing slot và cập nhật Qdrant dual-slot payload.
- [x] Verify manifest/checksum/quality trước khi đổi active pointer.
- [x] Query trong lúc publish đọc một snapshot nhất quán.
- [x] Inject lỗi subtree build, Qdrant partial batch, manifest/count/checksum/quality gate và pointer switch; lỗi trước switch giữ active tree phục vụ.
- [x] Rollback xác minh Qdrant slot rồi đổi active manifest/pointer/epoch cùng transaction PostgreSQL; retry cùng `target_tree_version` không toggle pointer hoặc tăng epoch.
- [x] Giữ request snapshot/lease xuyên publish và rollback; slot có query đang dùng không bị ghi đè.

**Implementation evidence (2026-10-05):** các mục trên có regression local bằng SQLite + `httpx.MockTransport`; focused suite `test_checkpoint_c_publish.py`, `test_checkpoint_c_failure_rollback.py`, `test_checkpoint_c_incremental.py`, `test_search_unit_retrieval_service.py` và `test_query_routing_service.py` đạt **64 passed**. Chi tiết từng failure, kết quả và giới hạn môi trường: [Checkpoint C failure/rollback evidence](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20C%5D2/evidence.md). Rollback giữ version trước trong slot đối diện tới lần publish kế tiếp reserve slot đó; retry cùng target đã active giữ nguyên epoch; khi reserve slot cũ, PostgreSQL bỏ tham chiếu trước khi Qdrant bị sửa để partial write không thể được nhận nhầm là snapshot hợp lệ.

**Review follow-up (2026-10-06):** validate `source_routing_profiles` khi rehydrate manifest; rollback dọn lease hết hạn theo project bằng PostgreSQL time nhưng giữ lease còn hiệu lực; regression malformed-manifest và lease cleanup. Năm module focused đạt **66 passed**, Ruff, `compileall` và `git diff --check` đều đạt. Kiểm tra profile trong atomic switch vẫn chặn thay đổi xảy ra trong lúc xác minh Qdrant. Producer runtime DATN-58/59 vẫn mở.

**Giới hạn acceptance:** chưa có live PostgreSQL/Qdrant; DATN-58/59 producer chưa nối vào `publish_tree_candidate` ở task base. Các checkbox Phase 8 thể hiện code/fixture coverage, chưa xác nhận triển khai runtime `INCREMENTAL_READY`. DATN-35 / `ROUTING_READY` được xác nhận hoàn tất theo task dependency; Checkpoint B không được kiểm thử lại trong task này.

**Update/subtree service contract (2026-10-06):** `update_tree_incrementally` và `coordinate_ingest_delta` được kiểm thử trực tiếp trên `KnowledgeUnitInput`; chưa có production caller từ normal ingestion. Xem [Phase 8 incremental update evidence](../docs/tai_task/phase-8-incremental-update-evidence.md).

## Checkpoint C — INCREMENTAL_READY

- [x] Incremental service contract xử lý delta không full tree rebuild; unit regressions bao phủ drift/subtree, lineage và quality-gate accumulation.
- [x] Failure paths, retained-slot rollback và request snapshot qua publish/rollback có focused regression như bằng chứng Phase 8 ở trên.
- [ ] Nối production producer DATN-58/59 vào publisher với mapping KnowledgeUnit→SearchUnit/source/version/partition đã được xác nhận, rồi kiểm chứng live PostgreSQL/Qdrant trước khi ghi nhận runtime `INCREMENTAL_READY`. Hiện publisher và rollback chưa có production caller; không tạo endpoint/mapping thay thế khi dependency contract còn thiếu.

## Phase 9 — Knowledge Quality & Gap

- [x] Thêm frozen input/output schemas và config có version cho metric definitions, normalization, scoring weights, thresholds, window và as_of.
- [x] Thêm deterministic metrics theo tree node/topic: anchored-evidence coverage, source diversity, freshness decay, contradiction-candidate rate, query demand, uncalibrated uncertainty proxy và demand growth.
- [x] Thêm fixtures chứng minh cùng input/config/as_of cho kết quả và checksum ổn định; thay evidence/freshness/conflict candidates/demand làm priority đổi đúng chiều.
- [x] Thêm additive migration/model cho demand daily aggregates, assessment snapshots, scored gaps và generated questions; giữ metrics, input/config checksums, reason codes, evidence IDs/locators.
- [x] Ghi daily query-demand counts từ route decision đã kiểm tra ACL; không lưu raw query hoặc message text.
- [x] Coi bucket demand thiếu ở bất kỳ cửa sổ nào là unknown; không suy ra zero từ bucket vắng.
- [x] Collector chỉ đọc current KnowledgeUnits/evidence/tree members trong cùng tenant/project/partition/version; manifest/profile checksum hoặc ACL lineage không khớp thì fail closed.
- [x] Assessment snapshot giữ đầy đủ scorer input và xác thực input checksum cùng evidence lineage trước khi persist.
- [x] Chỉ tạo question sau scoring và khi gap eligible; question phải trỏ đến gap persisted và giữ supporting evidence/reason linkage.
- [x] Phrase-only LLM không được sửa score/scope/evidence hoặc gọi research tools; no eligible gap thì không gọi LLM.
- [x] Kiểm tra tenant/project/partition scope, revoked/current source, incomplete freshness/demand, LLM error/invalid output và assessment/question persistence idempotency.
- [x] Chạy focused suite và Ruff trên toàn bộ file Python thuộc Phase 9; full repository/API outcomes ghi bên dưới. API repo không khai báo typecheck/build command riêng.
- [x] Cập nhật Phase 9 evidence và giới hạn production acceptance sau implementation/verification.

**Implementation evidence (2026-10-12):**

- Focused command từ `SAG/apps/api`: `python -m pytest -q tests/test_knowledge_quality_service.py tests/test_knowledge_quality_persistence.py tests/test_knowledge_query_demand.py tests/test_search_unit_retrieval_service.py tests/test_query_routing_service.py tests/test_knowledge_foundation.py` → **77 passed, 2 skipped** sau review fixes.
- Ruff trên các file Phase 9 đã đổi (`knowledge_quality_service.py`, `knowledge_quality_persistence.py`, `knowledge_quality_repository.py`, `knowledge_gap_question_service.py`, model/init, retrieval service và 3 test file) → **All checks passed**.
- `ruff check sag_api/ sag_agent/ tests/` theo `SAG/CONTRIBUTING.md` → **162 lỗi lint** trên phạm vi repository, gồm nhiều file ngoài task; không dọn refactor/lint ngoài phạm vi.
- Full API `python -m pytest -q` → **955 passed, 3 skipped, 23 failed** trong 450.57s. Lượt chạy bắt đầu trước thay đổi code Phase 9 cuối; code cuối đã được kiểm tra lại bằng focused suite bên trên. Failure nằm ở các test ngoài Phase 9: ask_stream, document_cleanup_coordinator, document_parsing, document_resume, dsh_integration, experience, hardening, octx_runtime, quick_model_setup và units. Cần phân loại riêng trước khi xem toàn API là green.
- Chạy `0004_knowledge_quality_gaps.sql` trên SQLite in-memory → **pass**, tạo đủ 4 bảng. Không có PostgreSQL/Qdrant live validation.
- Migration 0004 chỉ thêm bảng/index/constraint; không sửa 0001–0003. Rollback sau khi consumer dừng phải drop theo thứ tự question → gap → assessment → demand; thao tác xóa dữ liệu Phase 9.
- DATN-62 và Checkpoint B ROUTING_READY đã Done; DATN-67 Phase 4 vẫn In Progress. Query demand production chưa có lịch sử trước rollout, và fixture/local checks không chứng minh production acceptance.
- Review fixes: numeric-only demand snapshot validation chặn raw query lách qua field aggregate; snapshot giữ scorer input đầy đủ và xác nhận checksum/lineage; collector/question loại source mapping không còn CONFIRMED; thiếu demand bucket/window là unknown; question output phải giữ exact evidence phrase. Metric/config và prompt/generator đã bump version tương ứng; question guard chỉ xác nhận lexical anchor, không chứng minh semantic entailment.

**PR #26 review follow-up (2026-10-12):**

- [x] Dùng lại `knowledge_service.checksum` trong scoring, persistence và question; xóa ba bản cài đặt checksum trùng lặp.
- [x] Chuyển demand write khỏi response path bằng task nền có strong reference và completion callback; giữ ACL fingerprint, 100 ms timeout, fail-soft logging và không lưu raw query. Ghi demand vẫn best-effort, có thể mất khi process bị dừng cưỡng bức.
- [x] Ghi rõ cold-start: demand/growth chỉ biết khi node có ít nhất một bucket trong cả cửa sổ hiện tại và cửa sổ liền trước; trước đó priority/question vẫn fail-closed.
- [x] Sửa tên thư mục evidence thành `docs/Thang_Task/[SAG][Phase 9]/` và cập nhật liên kết trong plan.
- Implementation commit: `c5ca0946fd83f13f3cbd153b2d49dbb854f5d63b` (`fix(sag): address Phase 9 review feedback`), thuộc PR #26.
- Ruff trên 5 file Python đổi trong follow-up → **All checks passed**; Python 3.11 `py_compile` cùng 5 file → **pass**.
- Chưa chạy được pytest sau follow-up: runner mặc định thiếu `zleap`; `uv run --frozen --extra dev --python 3.11 ...` dừng khi build `litellm` vì máy thiếu MSVC `link.exe`, trước khi test collection. Reviewer đã báo 26/26 Phase 9 và 27/27 routing/retrieval trước các thay đổi follow-up này; không coi đó là kiểm tra lại cho commit follow-up.

## Phase 10 — Research Agent

- [x] Kiểm tra Gap Detector → Question Generator → Research Planner → discovery/fetch.
- [x] Mỗi task lưu gap/source/reason, policy, budget và trạng thái.
- [x] Fetch đi qua normal ingestion → dedup/version → tree update.
- [x] Kiểm tra crawl/fetch limit, allowlist, idempotency, duplicate guard và điều kiện dừng.

## Checkpoint D — AGENT_READY

- [x] Research task bắt nguồn từ gap signal có căn cứ.
- [x] Ingestion loop có giới hạn và dừng khi không có thông tin mới.

**Implementation status (2026-10-12, Phase 10 & Checkpoint D):**
- Đã hoàn tất toàn bộ 16/16 tests trong `test_research_planner.py` và `test_research_agent_checkpoint_d.py` (100% passed).
- Bổ sung manual redirect revalidation (tối đa 5 hops, recheck allowlist và SSRF tại mỗi hop), streaming response với `max_bytes_per_page`, cumulative task cap `max_total_bytes`, terminal task idempotency, failure classification, duplicate check scoped by security partition, và automatic bounded discovery từ gap/question.
- Dữ liệu fetch chuyển tiếp thành công sang normal ingestion `handle_document_upload` và cập nhật cây tri thức `update_tree_incrementally`.
- Chi tiết xem tại [researchtask.md](../docs/Thang_Task/[SAG][Phase%2010]/researchtask.md) và [evidence.md](../docs/Thang_Task/[SAG][Phase%2010]/evidence.md).

## Phase 11 — Hardening

- [x] Thêm exact route-decision cache key có scope/principal ACL fingerprint, SourceProjectMapping generation, current INDEX_SEARCH run/stage/manifest, search/tree/config/model/planner versions, query-profile inputs và filters; phân quyền và snapshot/lease mới vẫn chạy trước mỗi lần tra cache.
- [x] Route-decision cache mặc định tắt; không có answer/evidence cache hoặc semantic query reuse. Semantic cache chưa được triển khai vì chưa có route-equivalence benchmark và enablement gate được owner thống nhất.
- [x] Fixture bật cache để kiểm tra miss sau mapping revoke/regrant, reindex cùng document version, publish/rollback search epoch; key fixture kiểm tra tenant/project/partition, filter và tree config; cache hit gắn snapshot ID hiện tại, fallback không được lưu.
- [ ] Đo p50/p95/p99 theo stage và end-to-end; đặt SLO/latency budget theo benchmark môi trường.
- [ ] Chạy load/latency và live PostgreSQL/Qdrant ACL revoke/leakage/blackhole, retry/failure, publish/rollback verification.
- [ ] Hoàn thiện dashboards/alerts và migration/rollback/rebuild runbook.

**Evidence (2026-10-12):** focused cache/routing/retrieval/tree suite đạt **62 passed**; Checkpoint C publish/failure/rollback/incremental đạt **46 passed**. Full API suite bị chặn khi collect hai module phụ thuộc `litellm`; ACL/revoke/publish/rollback group có 58 passed và 6 lỗi khởi động vì thiếu package này (bản khóa cần MSVC linker). Ruff, format check cho cache/routing/test files, `compileall` và `git diff --check` đạt. Không dùng kết quả fixture để đóng staging/runtime acceptance. Semantic precision chưa đo; cache semantic vẫn không được triển khai/bật. Chi tiết: [Phase 11 evidence](../docs/Thang_Task/%5BSAG%5D%5BPhase%2011%5D/evidence.md).

## Hoàn tất

- [ ] Mọi mục Definition of Done ở [plan.md](plan.md) có evidence.
- [ ] PostgreSQL là source of truth; Qdrant rebuild được.
- [ ] UI phân biệt processing, SEARCH_READY, KNOWLEDGE_READY và FAILED.
- [ ] Query grounded hoặc no-answer đúng; citations và fallback trace truy nguyên được.
- [ ] Regression, integration và các failure checks quan trọng đạt.
