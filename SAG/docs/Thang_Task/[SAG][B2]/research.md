# Research — [SAG][B2] Query planner, tree-guided retrieval & escape

Ngày nghiên cứu: **2026-10-03**, owner **Thang/KeyT**. Đây là phân tích code và thiết kế đề xuất trước implementation; chưa có code B2 hay kết quả test B2.

## 1. Baseline và nguồn chứng cứ

- Code đọc tại `F:\LEARN KÌ 8\ĐATN\sag-laya-main-after-16`, branch `feat/Thang-sag-b2-query-planner-be-api`, commit `0f73c1b`, bằng `origin/main` khi tạo branch.
- Thư mục task do Thang chỉ định: `F:\LEARN KÌ 8\ĐATN\sag-laya-integration\SAG\docs\Thang_Task\[SAG][B2]`. Checkout này còn nhiều thay đổi ACL chưa commit trên branch cũ; **không dùng code dirty đó làm baseline B2**. Tài liệu B2 được đồng bộ sang task branch để sau này đưa vào PR.
- Nguồn chuẩn: `SAG/tasks/plan.md`, Phase 7/Checkpoint B; `SAG/tasks/todo.md`, Phase 7/Checkpoint B; `SAG/docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`, §11.4, §13, §18–19.
- Schema nền: `SAG/docs/phase-0-contracts-and-foundations.md`; code thực tế được ưu tiên khi xác định field đã tồn tại.
- GitHub xác nhận [PR #15](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/15) và [PR #16](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/16) đã merge. Trong các remote refs và 12 PR gần nhất đọc được chưa thấy output B1/DATN-37 hoặc Phase 5 được tích hợp. Điều này không xác nhận trạng thái công việc chưa push của owner khác.
- Baseline code có thể tra cố định tại [commit 0f73c1b](https://github.com/DATN-SPRING2027/sag-laya-integration/tree/0f73c1b).

**Phân biệt evidence:** PR #15 ghi lịch sử focused checks `161 passed, 4 warnings`; PR #16 có upload API → worker → manifest tests. `tests/test_checkpoint_a_ingestion.py` dùng `MockQdrantStorage`, `httpx.MockTransport` và `FakeIngestionEngine`, nên tên “real upload API” xác nhận HTTP/worker flow với dependency mock, không chứng minh real Qdrant/staging. Không chạy lại test trong task research này và không đánh dấu Checkpoint A/B vận hành đã đạt.

## 2. Current flow và callers

| Đường vào | Flow thực tế ở baseline | Ý nghĩa đối với B2 |
|---|---|---|
| Global `/search`, `/search/stream` | `api/v1/search.py::_prepare_global_search` → `_build_query_route` → `source_service.search_source_candidates` → `search_unit_retrieval_service.retrieve_search_unit_sections` → synthesis/evidence pack | Cùng một preparation path; đặt orchestration B2 trong service để hai endpoint đồng nhất. |
| Query routing API | `_build_query_route` đọc `analyze_query`, gọi Laya qua `asyncio.to_thread`, chỉ CHAT confidence cao mới skip; AMBIGUOUS/error vẫn retrieve | Giữ coarse intent contract. Chuyển feature→mode sang planner deterministic; Laya không quyết định sáu mode. |
| `search_context` | `tools/builtin.py::SearchContextTool.invoke` gọi trực tiếp `retrieve_search_unit_sections` với `ctx.principal`, `ctx.sources`; pack bằng `build_tool_evidence_pack` | Tool hiện không đi qua `_build_query_route`; phải dùng chung planner/orchestrator với API. Mỗi invocation có snapshot mới và không mở rộng `ctx.sources`. |
| Agent | `services/agent_service.py` lựa chọn tool, tính remaining context budget, nhận citations và ToolResult; nếu thiếu `_graph` có thể gọi legacy graph helper | Tool canonical hiện đặt `_graph=SourceGraphInfo()` để tránh enrichment ngoài lane. B2 cần chuyển trace sang tool event rõ ràng; không tự kích hoạt legacy graph. |
| Source-scoped search | `api/v1/search.py` gọi `retrieve_relevant_sections`, event recall và legacy engine | Không tự thay toàn bộ legacy/P3 path. Entry point B2 đợt này: global API và canonical tool; adapter source-scoped nếu cần phải có regression riêng. |
| Citation click | `search_unit_retrieval_service.get_search_unit_citation` được source chunk read route gọi, reauthorize và verify exact point | Tree/graph chỉ tìm candidate; evidence cuối vẫn qua canonical hydration/click guard này. |

### Planner hiện có gì, còn thiếu gì?

`services/query_analysis.py::QueryFeatures` là dataclass frozen: `exact_terms`, `identifier_terms`, `path_terms`, `temporal_cues`, `relation_cues`, `global_cues`, `multi_hop`. `extract_query_features` dùng regex/cue lists; `analyze_query` thêm lexical lookup/scoring terms, cap và optional Jieba cho Chinese.

Hiện **không có** lớp/hàm `QueryStrategyPlanner`, linked entity resolver cho planner, date interval parser hoặc planner version. `_build_query_route` ghi feature reason codes nhưng strategy vẫn là `vector/multi/multi_es_fast` lấy từ request/config/Laya. Global SearchUnit reader **không nhận strategy** và luôn query dense+sparse; trace requested/effective strategy có thể mô tả legacy mode chứ chưa phản ánh pipeline canonical.

Feature gaps cần fixture trước khi sửa: UUID/hash/IP/port và explicit dates chưa được bóc tách đầy đủ; temporal cues hiện match substring; `multi_hop` là cue heuristic, không phải bằng chứng quan hệ. Không đưa độ dài query hoặc boolean cue này thành lệnh graph expansion. Lexical normalizer chỉ giữ Latin ASCII/digits/CJK, vì vậy cần regression tiếng Việt có dấu và exact identifier punctuation để tránh sai feature/coverage; không refactor toàn bộ tokenizer trong B2.

## 3. Search, ACL, provenance và score contracts đã có

| Contract | Source thực tế | B2 phải giữ |
|---|---|---|
| Principal | `core/principal_assertion.py::VerifiedPrincipal`: frozen subject/org/project grants/issuer/key ID/token ID/issued+expires, `tenant_id`, `allowed_partition_ids` | Server verified, fail closed. Không lấy grant từ query/client/tree. Không đưa assertion/token ID vào trace public. |
| Source scope | `source_service._authorized_source_statement`, `search_source_candidates`: mapping `CONFIRMED`, org/project match; requested Source chỉ narrowing | Default source candidate cap 16, explicit cap theo config; escape chỉ trong effective candidate Sources. Ghi scope truncation để benchmark không nhầm thành lỗi tree. |
| Ready versions | `_load_current_ready_versions_checked`: active Document, delete/reprocess visibility, tenant/project/partition match, current validity, `search_status in READY/SEARCH_READY`, ready timestamp | Không đòi lifecycle `DocumentVersion.status=SEARCH_READY`; không đòi knowledge ready. |
| Verified attempt | Latest IngestionRun + latest `INDEX_SEARCH` StageRun `SUCCESS`, stage không cũ hơn run start; manifest verified, đúng collection, count/checksum nhất quán | Retry/reprocess invalidation trước và sau index query. Snapshot cần giữ run/stage/manifest identity thay vì chỉ version ID. |
| Candidate grouping | `_groups`: `(project, source, tenant, partition)` và ready version IDs | Fan-out/concurrency bounded; không so raw score giữa Projects/model spaces. |
| Qdrant ACL trước top-k | `sag/search_unit_store.py::build_search_filter`: project + tenant + partition + authorized document version IDs | Branch condition chỉ thêm vào `must`, không thay ACL filter. Dense và sparse đều dùng cùng base scope. Version IDs được lấy qua authorized Sources. |
| Hybrid | `_query_group`: `content_vector` embedding, `bm25_sparse` query; `build_sparse_query_vector` dùng lowercase Unicode word tokens/MD5 bucket/frequency; collection IDF | Không đổi producer tokenizer/IDF/index lane. Model identity chưa có đầy đủ trong snapshot/manifest: cần owner contract. |
| Validation | Sau Qdrant, recheck ready versions/mapping rồi batch hydrate SearchUnit + block range; verify IDs, project/tenant/partition/version, content SHA256, point UUID | Mọi branch/escape/graph candidate phải đi qua cùng guard; point payload không là nguồn cấp quyền. |
| Fusion | `retrieval_service.rerank_sections`: normalized RRF, K=60, relative semantic floor 0.68 và lexical relevance gate; `_fair_rank_merge` rank-interleaves groups | RRF là rank score, không là calibrated confidence/coverage. Tên `rerank_sections` hiện là fusion+filter, không neural reranker. |
| Dedup hiện có | `_section_key` ưu tiên `(source config, chunk ID)`; rankings unique theo key | Chưa collapse exact content hash across units, near-duplicates hay MMR thật ở canonical path. B2 cần thêm sau fusion, giữ locator representative hợp lệ. |
| Context/citation | `services/evidence_service.py`, `generation/prompt.py::build_citations`, `retrieval_service._validated_answer`, `agent_service._finalize_answer_citations`; canonical SearchUnit→DocumentVersion→block range/page/section/anchor | Summary/profile/graph edge không được biến thành cited evidence. Citation chỉ trỏ evidence thực sự trong final pack. |

`build_evidence_pack` dùng `settings.llm_context_window`, reserved output và estimate của rendered messages; `estimate_tokens` CJK≈1/char, phần còn lại≈1/4 chars. Tool dùng remaining budget do agent host cung cấp. Đây là estimator, không model tokenizer chính xác; B2 tái sử dụng và ghi gap, không đặt token budget cứng mới.

Citation validation hiện có hai lớp cần giữ: `_validated_answer` kiểm tra có citation number và nằm trong range của pack; canonical hydration/locator/click checks xác minh provenance/authorization. Number hợp lệ không chứng minh claim được evidence entail. B2 không tuyên bố citation syntax validator là calibrated answerability hoặc entailment validator.

## 4. Tree/graph hiện trạng và B1 handoff

`db/models/routing_rag.py` đã có `ProjectSearchState` với active slot/tree version/search epoch và `TreeManifest` với counts/quality/checksum/status. Chưa thấy runtime reader/provider cho hai model, tree node membership tables, `RoutingSnapshot` hay `NodeProfile` trong baseline. Universe graph/overview là tính năng khác; không dùng làm routing tree giả.

SearchUnit payload pre-provisioned: `primary_node_a`, `secondary_node_ids_a`, `tree_version_a` và bộ tương ứng `_b`; initial primary/version `None`, secondary `[]`. `PAYLOAD_INDEX_FIELDS` mới có primary node indexes, chưa có secondary/tree-version indexes. Không có ancestor path. B2 không tự backfill/update membership hoặc tạo index; B1/C sở hữu publish/store, thay đổi DB/index phải tách deliverable với owner.

`KnowledgeGraphEdge` hiện nối SearchUnit IDs theo Project; `dedup_and_temporal_service` ghi candidate relation edges, metadata có `is_candidate`, calibrated weight hiện gán từ weight đầu vào. Không đủ để khẳng định Phase 5 KnowledgeUnit graph/calibration hoàn chỉnh. B2 chỉ bật graph adapter khi Phase 5 cung cấp provenance/validity/ACL endpoints; chưa có adapter thì skip có trace và giữ hybrid escape.

### Contract đề xuất cần thống nhất với B1/DATN-37

Các field dưới đây là **DTO runtime đề xuất**, không là DB columns đã có hay contract owner đã duyệt.

| DTO | Field/semantics bắt buộc |
|---|---|
| `RoutingSnapshot` cấp request | snapshot ID, captured_at/effective query time, scope fingerprint, immutable effective Sources/Projects/tenant/partitions, frozen ready version+run+stage+manifest identities, planner/retrieval config version, per-Project tree state. |
| Per-Project tree state | ProjectSearchState active slot `SLOT_A/SLOT_B`, tree version, search epoch; TreeManifest checksum/status/config version; payload suffix mapping explicit. Không giả sử một tree version/epoch dùng cho mọi Project. |
| `NodeProfile` | project/tenant/tree version/node/parent/depth, security partition và profile scope identity, accessible-unit count, dense centroid/medoids + embedding model identity, sparse signature + tokenizer/config identity, optional entity/time signals và bounded prior. |
| Profile scope proof | Profile chỉ chứa dữ liệu được phép với caller; counts/membership tính trên effective Source/version scope. Partition profile trộn Source bị revoke/ngoài grant không thể an toàn chỉ bằng kiểm tra count>0. Nếu không tạo được safe profile cho phần scope đang có quyền, bỏ profile đó và escape. |
| Membership/filter translation | Provider trả bounded descendant leaf IDs từ pinned version cho broad internal branch; query `(primary_node_<slot> in leaves OR secondary_node_ids_<slot> intersects leaves)` **AND** `tree_version_<slot> == pinned_version` **AND** base ACL. Nếu không cung cấp được descendant filter bounded, dùng escape, không bỏ filter tree/ACL để giả broad. |
| Provider lifecycle | Profiles/version immutable; publish chỉ trả snapshot đủ manifest verification. Quy định thời gian giữ old slot/version cho in-flight query; stale/missing/mismatch phải fail tree path và global escape. |
| Graph adapter Phase 5 | Seed SearchUnit/KnowledgeUnit mapping, project+partition+source+version constraints, relation validity/provenance, hop/node/latency caps, output canonical unit IDs. |

Mock fixture đầu tiên phải có ít nhất hai Projects, hai partitions, Source narrowing, node mixed access, wrong route, secondary membership, unassigned delta units, slot/version switch và missing profile signals. Fixture tự viết trong B2 là contract proposal; chỉ ghi “agreed” khi có evidence xác nhận của owner B1.

## 5. Quyết định thiết kế B2

### 5.1. Planner deterministic, primary + modifiers

1. Reuse feature extractor; chỉ bổ sung feature cần cho fixture còn thiếu. Không gọi LLM ở fast path.
2. CHAT confidence cao giữ skip tại routing guard. KNOWLEDGE/COMMAND/AMBIGUOUS vẫn theo flow hiện có; B2 không tự ép ambiguous thành clarification.
3. Default precedence theo workflow: exact → `EXACT`; relation/linked multi-entity → `ENTITY_RELATIONAL`; broad cue → `GLOBAL_TOPIC`; còn lại → `LOCAL_FACTUAL`. `TEMPORAL` là modifier composable; cho phép primary TEMPORAL qua typed override nếu contract public được duyệt.
4. Giữ exact+temporal và relational+temporal; parsed interval/as-of chỉ từ timestamp/date/version có nghĩa rõ. Cue “trước/sau/khi” đơn độc không tạo date filter. Query causal/multi-clause ghi candidate reason; `MULTI_HOP` là escalation có bounded evidence/coverage reason, không bật vì query dài.
5. Routing entropy có thể refine auto LOCAL→GLOBAL; explicit override không bị âm thầm rewrite. Trace ghi requested/effective primary/modifiers cùng lý do fallback hoặc escalation.
6. Output typed, immutable: `planner_version`, `feature_version`, primary/modifiers, exact terms, resolved entities nếu có, typed temporal constraint hoặc unresolved reason, stable reason codes. Không bịa planner confidence như `0.91` khi chưa calibrated; Laya confidence và route score tách nghĩa.

API hiện dùng `strategy` legacy enum. **Giữ nguyên enum đó**; sáu mode không thay vào field cũ. Đề xuất additive `retrieval_mode` và `retrieval_modifiers` nếu cần explicit override; kiểm tra schema/compatibility trước edit. Trace giữ legacy fields và thêm planner fields; canonical `effective_execution` phải báo hybrid thực tế.

### 5.2. Snapshot nhất quán và revocation

- Đọc frozen Source/ready-version/manifest/tree pointer trong một read transaction với snapshot semantics của PostgreSQL; kiểm tra session/SQLite test path trước khi chọn cách bật repeatable read. Materialize rồi release connection trước network/LLM; không giữ long transaction qua SSE.
- Toàn bộ local/escape/filter/profile đọc cùng request snapshot và cùng effective time. Hiện ready-version query dùng `datetime.now(UTC)` mỗi lần; B2 cần captured time hoặc policy explicit để query không tự đổi current version giữa hai nhánh.
- Trước hydration/pack, recheck mappings/lifecycle/latest attempt; chỉ **giao** current authorization/verified attempts với set snapshot ban đầu. Không thêm version mới khi recheck, không thay active tree pointer giữa chừng.
- Revocation là security override đối với frozen snapshot: loại evidence vừa bị revoke. ACL fingerprint chỉ nhận diện scope, không là revocation authority; baseline chưa có ACL revision contract.
- PG snapshot không tạo cross-system ACID với Qdrant. B1 phải bảo đảm immutable version/profiles và slot retention. Mismatch/reused slot khiến local path invalid → escape trong pinned authorized scope; nếu search epoch/manifests thay đổi khiến scope không còn hợp lệ thì loại candidate hoặc bounded full restart có trace, không trộn epochs âm thầm.

### 5.3. ACL-safe beam và broad routing

- Prune inaccessible/invalid profiles **trước** scoring/beam/entropy; không ghi node IDs/profile text ngoài grant vào response trace. Counts phải dựa trên snapshot/version/source intersection, không count toàn Project.
- Score từ dense/sparse/entity/time/prior chỉ khi signal hợp lệ và cùng model/config; normalize/calibrate theo config version, renormalize trọng số khi optional signal thiếu. Không cộng raw cosine/BM25 ngoài scale.
- Rank/tie-break deterministic theo project/tree/node ID. Với node scores nonnegative đã calibrated, tạo distribution từ score mass để tính entropy normalized; total mass=0 → undecidable/broad, không ép top1. Số eligible children≤1 là trường hợp riêng.
- Giữ broad parent/multiple branches khi entropy cao và margin nhỏ; cap depth/beam/children/membership expansion. Workflow defaults B=3, H>0.72+margin<0.10, decisive margin≥0.18 chỉ là điểm bắt đầu cho fixtures/benchmark, không production acceptance threshold đã chốt.
- Missing tree/profile/model hoặc router lỗi không chặn canonical retrieval: global-only escape với reason tương ứng.

### 5.4. Local hybrid + global escape + latency

- Deadline dùng monotonic clock cho toàn retrieval, bao gồm queue/semaphore, embedding, network/retry, hydration và optional graph/rerank. Timeout hiện `search_source_timeout=12s` thuộc legacy path; canonical `_query_group` chưa áp total request deadline. Không copy số 12s thành SLO B2 chưa đo.
- Dành global escape candidate/time budget ngay từ đầu; workflow 80–90% local / 10–20% global là starting ratio. Kích hoạt escape core song song với local hoặc không muộn hơn điểm reservation, thay vì chờ local dùng hết deadline. Query embedding reuse chỉ trong cùng model identity, không reuse cross-model.
- Local thêm pinned branch condition vào base ACL; escape bỏ **chỉ** branch condition, giữ Sources/version/tenant/project/partition và model constraints. Candidate/group/concurrency caps dùng config hiện có kết hợp request budget.
- Escape core chạy cho mọi query tree-enabled để có candidate ngoài wrong route và delta chưa assigned; local empty, inaccessible, profile error, weak/anchor miss yêu cầu sử dụng hoặc mở rộng escape trong phần budget còn. Không chỉ dựa vào local nonempty để kết luận route đúng.
- Global-only ở tree off/lag/error vẫn dùng reader Checkpoint A. Escape timeout/index error không trở thành `empty_evidence`; dùng sanitized unavailable/error trace. Nếu có local evidence còn hợp lệ thì trả partial theo policy đã ghi; nếu không có evidence hợp lệ và index lỗi thì giữ error semantics. Đừng trả no-answer giả vì mất mạng.
- Optional graph/rerank không dùng escape reserve; khi thiếu thời gian skip và trả validated core candidates. Hết total deadline trước khi core có evidence → sanitized timeout/unavailable, không làm giả success.

### 5.5. Fusion → dedup → MMR → coverage → optional expansion/rerank

- Tách bounded candidate acquisition/validation khỏi final top-k selection của reader để local+escape không bị cắt top-k quá sớm. Reuse readiness/hydration và RRF helper; không tạo retrieval stack thứ hai hoặc đổi raw score semantics.
- Cùng candidate có local+escape hits phải hợp nhất thành một ranking cho mỗi channel/scope trước RRF; không coi local/escape là hai retrievers để vô tình double-count. Giữ global ranks/channel provenance cho audit.
- Fuse rank lists, collapse exact content hash trên **authorized validated evidence**. Representative deterministic với locator; giữ internal provenance aliases nếu cần, không nối nội dung/locator của các version khác nhau.
- Near duplicate chỉ dùng cluster/config có provenance đã tồn tại hoặc thuật toán bounded với evaluation. Không collapse dựa trên prefix text hoặc topic similarity có thể xóa facts/contradictions/version khác nhau.
- MMR: `lambda * relevance - (1-lambda) * max_redundancy_to_selected`, relevance là RRF rank signal. Redundancy dùng bounded normalized lexical overlap ở baseline chưa trả vectors; vector cosine chỉ nếu model identity tương thích và vectors được provider cung cấp an toàn. Lambda/config version được benchmark; deterministic ties. Bảo vệ exact anchors/time/entity facets trước khi tối ưu diversity để MMR không loại evidence bắt buộc.
- Coverage v1 là **structural/required-facet**, không calibrated answerability: valid locator+ready manifest+authorized evidence, exact anchor coverage, typed temporal match, resolved entity/relationship requirements nếu thực sự có resolver/evidence. Trường thiếu tín hiệu là UNKNOWN, không suy thành sufficient semantic coverage.
- `EMPTY`: không có evidence sau local+escape thành công. `WEAK`: untraceable/required anchors hoặc explicit constraints chưa được chứng minh, hoặc budget không pack được. `SUFFICIENT_STRUCTURAL`: qua checks hiện có, vẫn còn gap claim entailment. RRF thấp/cao không quyết định confidence.
- Relation evidence hợp lệ+coverage thiếu mới thử MULTI_HOP/graph expansion, giới hạn hop/nodes/query count/deadline; candidate mở rộng quay về canonical guard và merge→dedup→MMR→coverage. Chưa có Phase 5 contract thì skip expansion có trace; không gọi legacy graph như fallback không kiểm soát.
- Neural rerank là optional adapter có config/model/time budget nếu đã có dependency; không thêm package/model mặc định cho lane này. Không có adapter thì `rerank_skipped=unavailable`.
- Context/token/citations dùng pack hiện có; route profile/safe summary chỉ là prior, không được cite thay source text. Temporal history cần explicit retained-version selector cùng ACL/manifest guard; baseline chỉ search current validity. Chưa thống nhất history policy thì đánh dấu unsupported/unresolved, không tuyên bố Temporal historical acceptance đã đạt.

## 6. Trace/API/tool changes dự kiến

Additive `stats.routing` (hoặc tên được chốt trong schema) gồm snapshot ID, planner/config version, per-Project pinned tree+slot+search epoch, requested/effective primary/modifiers, reason codes, accessible selected nodes, entropy/margin, local/escape counts, escape trigger/outcome, coverage status/missing requirements, stage durations/deadline/skip reasons và partial/error flags. Giữ `stats.query_route` hiện có; không overwrite legacy strategy bằng sáu-mode enum.

Blackhole phân biệt: inaccessible nodes được prune; selected route không có authorized evidence; escape phục hồi; toàn authorized scope không có evidence. Empty corpus/no grant không phải routing blackhole. Không expose hidden node identifiers/counts hoặc raw SQL/network exception. `blackhole_detected` là trace sự kiện; tỷ lệ corpus là metric benchmark có denominator.

`SearchContextTool` hiện chỉ trả một phần outcome counters, chưa chuyển đầy đủ `stats` vào ToolResult; host event cũng chưa copy routing trace. B2 phải wire cả hai điểm, kể cả EMPTY/WEAK/error path. `/search/stream` giữ event order/cancellation/validated answer buffering, không stream profile/provisional unauthorized evidence.

## 7. Test matrix và nghiệm thu

| Matrix | Assertions bắt buộc | Loại evidence |
|---|---|---|
| Planner 6 modes | Exact phrase/UUID/hash/path/error/env; local factual; multi-entity/relation; explicit date/version/as-of; broad topic; causal/multi-hop escalation; stable precedence+reason/version | Unit fixtures deterministic, tiếng Việt/English, CHAT/AMBIGUOUS/Laya error guard |
| Composition/override | Exact+Temporal, Relational+Temporal, explicit mode validation, legacy strategy compatibility, long query không auto graph | Contract/unit |
| ACL-safe route | Two tenant/project/partition, no grants, Source narrowing, profile chứa forbidden membership, inaccessible prune trước beam, trace không lộ hidden IDs | Provider+query-filter fixtures |
| Broad beam | Uniform/zero mass, one child, score ties, decisive margin, high entropy, missing signals, capped descendants/secondary nodes | Router unit |
| Escape recovery | Wrong accessible route nonempty nhưng miss gold, wrong route empty, inaccessible, unassigned delta, absent/off/lag/error tree | Gold SearchUnit IDs được authorized; branch/escape requests có base ACL giống nhau |
| Snapshot races | Slot publish/reuse, latest attempt retry, mapping revoke trong query, new version xuất hiện, per-Project epochs khác nhau | Frozen snapshot + recheck intersection, không mix old/new hoặc resurrect evidence |
| Fusion/diversity | Positive raw-score scale invariance, duplicate channels/scopes, exact hash, distinct facts/version retained, MMR stable và không làm mất required anchors | Existing RRF regression + new selection tests |
| Latency/error | Semaphore/embedding/local hang, escape reserve, retry consumes deadline, graph/rerank skip, cancellation closes tasks, DB/Qdrant error secret sanitation | Controlled clock/async blocking fixtures; tránh timing tests flaky |
| API/stream/tool | Cùng planner/effective scope/snapshot semantics; trace ở success/empty/weak/error; context/citation click đúng unit/version/page/anchor | ASGI/tool contract integration |
| Producer→reader | Upload API → worker manifest → pinned tree/provider → search+stream+tool → citation click | Mock dependency regression trước; real PG/Qdrant+DATN-37 corpus sau |
| Benchmark B | Gold labels cho 6 mode, Recall@k/MRR/nDCG và citation/provenance correctness; compare global-only baseline; branch recall, escape recovery, blackhole, p50/p95/p99 | Corpus/model/config/commit versioned, evidence report; không dùng unit test pass làm benchmark pass |

Phase-0 doc đề xuất `routing_recall@5≥0.90`, escape win rate≤0.15, ACL blackhole=0. Workflow v1.1 yêu cầu benchmark rồi freeze defaults. **Đây chưa là benchmark B2 đã được owner thống nhất**. Escape win rate cao trên fixture cố ý route sai là dấu hiệu recovery hoạt động, không tự là lỗi acceptance của B2; quality gate publish thuộc B1. ACL leakage phải bằng 0 ở mọi kiểm chứng; production recall/latency thresholds và corpus cần xác nhận riêng.

Existing regression files đọc được: `test_query_analysis.py`, `test_laya_router.py`, `test_search_strategy.py`, `test_search_unit_retrieval_service.py`, `test_search_unit_store.py`, `test_retrieval_relevance.py`, `test_search_stream.py`, `test_agent_tools.py`, `test_agentic.py`, `test_agent_history_acl.py`, `test_checkpoint_a_ingestion.py`. Test commands cụ thể nằm trong `plan.md`; chưa chạy trong research.

## 8. Ownership và out-of-scope

B2 sở hữu planner, routing consumer, branch/escape orchestration, post-fusion diversity/coverage, safe trace và regression. Shared files (`search_unit_retrieval_service.py`, `search_unit_store.py`, `retrieval_service.py`, API/schema/tool/agent/config) cần chốt diff với owner trước implementation; đọc/đề xuất contract không đồng nghĩa có thỏa thuận edit.

Không xây Knowledge Units/Phase 5 extraction, B1 topology/prototypes/profile builder/quality publish gates, DATN-33 worker/readiness/index writer, C incremental/publish/rollback, DB migration/index/backfill, global ACL authority, answer/route cache, frontend UI, training/calibration pipeline hoặc mandatory neural model.

## 9. Remaining gaps và khả năng bắt đầu

| Gap | Owner/điểm chốt | Có thể làm ngay |
|---|---|---|
| B1 snapshot/profile/membership DTO chưa có agreed artifact | DATN-37+B2: schema/fixture, descendant+secondary filters, safe narrowed profiles, immutable versions/slot retention | Planner, fixtures, router consumer và global-only fallback bằng proposed contract |
| Phase 5 graph/entity/model signals chưa tích hợp | Knowledge lane: unit map/edge validity/provenance/calibration | Structural coverage + skip graph có trace |
| Checkpoint A real services/security acceptance | DATN-33/34 + BE/security: model identity, grants/revoke, real index upload→reader | Giữ guard baseline; mock contract regression |
| Consistent PG read + frozen manifest ID + ACL revision | B2 reader + B1/security owner; DB transaction semantics, principal expiry/revoke policy | Thiết kế snapshot/recheck intersection; chưa claim cross-system ACID |
| Temporal historical policy/constraint parse | B2 + document/version owner: as-of/interval timezone/retention/explicit-version scope | Current-version safety; typed explicit constraints fixtures |
| Model compatibility/profile calibration/MMR weights | Index+B1+B2: model/config identity, score calibration artifact | Rank fusion reuse và lexical diversity prototype trong fixtures |
| Answerability/claim entailment chưa calibrated | Retrieval/evaluation owner | EMPTY/WEAK structural+required anchors, UNKNOWN coverage → escape; không score threshold bịa |
| Recall/SLO thresholds và gold corpus chưa thống nhất | B1/B2/benchmark owner | Chạy fixture acceptance; chưa đóng ROUTING_READY |

Đủ cơ sở để triển khai các lát planner/fixture/consumer/escape an toàn sau khi Thang yêu cầu code. Chưa đủ bằng chứng để gọi task hoàn thành end-to-end với DATN-37 hoặc Checkpoint B đạt gate. Checklist thực thi ở `todo.md`; kế hoạch theo thứ tự dependency ở `plan.md`.
