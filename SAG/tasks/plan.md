# Kế hoạch triển khai SAG Knowledge Routing RAG

Nguồn chuẩn: [Workflow v1.1](../docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md). Kế hoạch này chuyển các phase và Definition of Done trong đặc tả thành thứ tự triển khai có phụ thuộc và cổng nghiệm thu. Đây là kế hoạch mục tiêu; không coi các phase là tính năng đã có. Trước mỗi phase, đối chiếu code, schema, API và cách vận hành hiện tại rồi mới chọn nơi sửa.

## Mục tiêu và nguyên tắc

- Đạt luồng truy vấn tài liệu dùng được sau ingestion và hybrid search; knowledge enrichment/tree là nhánh bất đồng bộ, không chặn tìm kiếm.
- PostgreSQL là source of truth cho metadata, provenance, version, ACL, job state và tree lineage. Qdrant giữ index tăng tốc và phải dựng lại được từ dữ liệu chuẩn.
- SEARCH_READY nghĩa là canonicalization, dedup và search index đã nhất quán. KNOWLEDGE_READY chỉ đạt sau khi knowledge extraction và tree assignment cần thiết hoàn tất.
- Laya chỉ cho coarse intent. Query features và strategy được xác định theo contract; Laya lỗi hoặc mơ hồ phải fallback an toàn.
- Tree là routing prior, luôn giữ global escape retrieval; Qdrant search phải áp ACL chính xác.
- Retry, reprocess, rebuild và publish phải idempotent, có version/manifest, có thể truy nguyên và rollback.

## Phase 4 execution plan — P4 Global Retrieval, ACL và Fusion

### Phạm vi task

Task P4 này tích hợp global hybrid retrieval vào query flow sau P3, enforce ACL
ở trước và trong candidate retrieval, rồi hợp nhất dense/sparse bằng phương pháp
không phụ thuộc raw score khác scale. Không triển khai evidence context,
citation, no-answer flow, Knowledge Graph hoặc Knowledge Routing Tree.

> **Cập nhật trạng thái sau PR #12 (2026-10-01):** các ghi chú ACL bên dưới
> phản ánh trạng thái trước runtime ACL integration và không còn là blocker code
> hiện tại. `search_source_candidates()` nay nhận verified principal, chỉ lấy
> Source có mapping `CONFIRMED` trong organization/project scope; PR này kế thừa
> contract đó và không sửa ACL, global retrieval hay fusion. Chưa suy ra từ việc
> merge rằng môi trường staging/production đã được cấu hình hoặc nghiệm thu.

### Hiện trạng đã xác nhận

- P3 đã route vào global `/search`, `/search/stream` và source-scoped search.
- `retrieve_relevant_sections()` đang chạy semantic engine retrieval song song
  với lexical `grep_chunks()` rồi rerank bằng một công thức có dùng raw score.
- `EngineManager.search_many()` hỗ trợ batch vector retrieval và strategy
  `multi_es_fast`; hidden logical-delete/reprocess sources đã được prefilter
  qua `exclude_source_ids_by_config`.
- `Source` hiện chưa có owner/user/tenant/project hoặc ACL mapping.
- `search_source_candidates()` hiện không nhận principal và không enforce user ACL;
  app hiện được mô tả là single-user. Vì vậy logical-delete filter hiện có không
  thể thay thế cho authorization ACL.

### Execution status — 2026-09-29

- Đã tích hợp RRF cho semantic/lexical rank lists; raw score không tham gia fused
  ranking. Exact lexical match được ưu tiên trong lexical rank list, output score
  chuẩn hóa về `[0, 1]`, candidate dedupe/tie-break ổn định.
- Global `/search` và `/search/stream` bỏ event recall/graph projection; response
  giữ `events`, `entities`, `relations` rỗng. Source-scoped P3 graph path không đổi.
- **P4 chưa hoàn tất và chưa đạt yêu cầu ACL.** Kiểm tra `DATN_BE` chỉ thấy
  membership/role-assignment persistence và organization-context login resolver;
  không thấy project/source permission resolver/API. `Source` và vector payload
  hiện cũng chưa được map sang tenant/project/security partition trong phạm vi
  lane SAG đang cho phép sửa.
- Không được merge/đánh dấu P4 hoàn thành cho tới khi BE cung cấp trusted,
  fail-closed permission scope contract và SAG có mapping tương ứng; sau đó cần
  lọc scope trước dense/lexical retrieval và thêm ACL leakage/pre-top-k tests.

### Follow-up review fix — 2026-09-30

- Semantic-only relevance gate không còn dùng absolute `semantic_floor`; mỗi
  candidate được so với semantic score cao nhất trong cùng kết quả, dùng ngưỡng
  tỷ lệ `0.68`. Regression xác nhận ranking/relevance giữ nguyên khi score bị
  nhân hoặc chia `1000`.
- Frontend global SearchPanel hiện chuyển sang `ResultList` khi `events` rỗng;
  response contract vẫn giữ các graph fields dưới dạng mảng rỗng.
- ACL authority/resolver và source-scope mapping vẫn thiếu; đây tiếp tục là
  blocker P1 của PR.

### Retrieval review follow-up — 2026-09-30

- Deduplicated semantic sections retain the maximum retriever score while the
  longer deterministic representative supplies the returned content.
- Relevance is evaluated per candidate: a lexical hit on one result no longer
  disables the relative semantic gate for other dense candidates.
- Search and Dify API schemas document that `score` is a normalized RRF rank
  score, not cosine similarity or a probability. Dify's `score_threshold` is
  applied to this RRF score; callers must tune it accordingly.

### ACL search integration seam — 2026-09-30

- Global `/search`, `/search/stream` và `/search/eval-compare` nhận một
  `SearchACLScope` nội bộ có `authorized_source_ids`. Dependency hiện trả `None`
  mặc định vì repo chưa có trusted resolver; truy vấn cần retrieval dừng bằng
  `503`, không rơi về global candidate selection. High-confidence CHAT vẫn bỏ
  retrieval nên không cần scope.
- Khi scope được cấp, explicit `source_ids` chỉ được giao với
  `authorized_source_ids`; khi không gửi ID, candidate Sources được chọn chỉ từ
  tập authorized và vẫn giữ candidate limit hiện có. Scope rỗng trả rỗng trước
  dense/lexical retrieval. Hai retriever nhận cùng danh sách Source; response
  cũng loại section không map được về Source trong scope.
- Test dùng fake scope để kiểm tra intersection, implicit scope, empty scope,
  fail-closed, dense/lexical parity và stream behavior. Đây chỉ là kiểm chứng
  contract tại seam, không phải nghiệm thu ACL production.
- Quyết định kiến trúc đã chốt: BE/Continuum là authority và cấp signed
  principal assertion; SAG xác minh assertion rồi dùng `allowed_project_ids`
  qua Project→Source mapping đáng tin cậy. P4 không tự diễn giải membership/role,
  không thêm IAM/JWKS/shared config hoặc mapping schema. Source chưa map sẽ không
  searchable khi mapping runtime được triển khai.
- **P4 retrieval/fusion code có ACL integration seam fail-closed; ACL/P1 runtime
  acceptance vẫn BLOCKED** cho tới khi có assertion verifier thật, mapping
  Project→Source/backfill và runtime cross-project/cross-organization leakage
  tests.

### Decision gate trước implementation

Kiến trúc quyền đã chốt; runtime contract/mapping vẫn là dependency riêng:

1. BE/Continuum xác thực user/organization và quyết định Project permission,
   sau đó cấp signed principal assertion. SAG không tự diễn giải role hoặc
   membership.
2. Assertion mang project scope; SAG ánh xạ allowed projects sang Source IDs từ
   mapping có source of truth rõ ràng. Project→Source là authorization boundary
   hiện tại; Document/Version kế thừa scope Source.
3. P4 chỉ nhận `authorized_source_ids` ở seam nội bộ. Không thêm JWT/JWKS
   framework, shared authorization contract/config hoặc schema/migration khi
   chưa có deliverable đã thống nhất.
4. Resolver/mapping runtime chưa tồn tại; mặc định fail closed. Dùng fake scope
   chỉ để phát triển và test integration logic, không đánh dấu P1 hoàn thành.

Không chọn cách coi `body.source_ids` là ACL: đây là input do client gửi, không
phải bằng chứng user được phép xem.

### Dependency graph và task slices

#### Task P4.0 — Chốt ACL boundary

- [x] Chốt BE/Continuum là permission authority; SAG chỉ nhận scope đã được
      xác thực/resolve. Runtime assertion verifier còn là dependency.
- [x] Chốt Project→Source là granularity hiện tại; Document/Version kế thừa
      Source. Explicit unauthorized IDs bị giao với authorized scope.
- [x] Chốt không có verified scope thì global retrieval trả lỗi fail-closed;
      scope rỗng không sinh candidate.
- [ ] Hoàn tất signed assertion contract/verifier và mapping/backfill runtime
      theo deliverable P1 riêng.

#### Task P4.1 — ACL-safe global candidate scope

- [x] Yêu cầu authorized scope trước khi gọi global dense hoặc lexical retrieval;
      dependency thiếu scope fail-closed trước candidate generation.
- [x] Giao requested IDs với authorized IDs; scope implicit/empty cũng chỉ dùng
      authorized Sources. Cùng Source list đi vào dense batch và lexical grep.
- [x] Giữ logical-delete/reprocess barrier; không có fallback unfiltered khi
      ACL scope thiếu hoặc rỗng.
- [x] Chỉ trả section có `source_config_id` nằm trong candidate Source scope;
      guard response là defense-in-depth, không thay prefilter.
- [ ] Cắm real resolver và mapping Project→Source, gồm revoke/backfill semantics.

#### Task P4.2 — Rank fusion ổn định

- [x] Tách dense/engine candidates và lexical candidates thành các ranked lists.
- [x] Dùng RRF hoặc calibrated rank fusion; không cộng raw dense/sparse score.
- [x] Dedupe exact candidate theo source-config/chunk key, có fallback fingerprint
      ổn định; deterministic tie-break theo fused score/rank/key.
- [x] Chuẩn hóa score output về contract hiện có `[0, 1]` và ghi fusion method,
      candidate counts, filtered counts trong retrieval stats.
- [x] Không thêm MMR/context/citation/no-answer vào task này.

#### Task P4.3 — Global query-flow integration

- [x] Giữ P3 `query_route` và strategy/fallback trace.
- [x] Đảm bảo `/search`, `/search/stream` và `eval-compare` dùng cùng scope
      resolver/helper trước retrieval.
- [x] Không thêm dependency vào Knowledge Graph/Tree; event/graph response hiện
      có chỉ giữ nguyên nếu không làm thay đổi evidence ACL contract.

#### Task P4.4 — Regression và review gate

- [x] Relevance: semantic paraphrase, exact identifier, Vietnamese/domain mix,
      duplicate candidate và deterministic ordering.
- [x] Fusion: raw score scale khác nhau không làm một retriever lấn át do scale;
      rank agreement và tie-break ổn định.
- [x] ACL seam tests: intersection, implicit scope, empty scope, unauthorized
      request, dense/lexical parity, fail-closed `/search` và stream.
- [ ] Production ACL: real assertion, Project→Source mapping/backfill, revoke
      behavior và cross-project/cross-organization leakage tests (DATN-61).
- [x] API: global `/search`, `/search/stream`, source scope và P3 trace không
      regression.
- [x] Cập nhật `tasks/todo.md` và Phase 4 review evidence sau khi code pass.

### Checkpoints

- **Checkpoint P4.0:** ACL authority/contract và scope mapping thực thi được
  phải tồn tại trước khi P4 được coi là hoàn chỉnh hoặc merge-ready. Các thay đổi
  RRF/global graph hiện tại mới là phần triển khai độc lập, chưa đảm bảo ACL.
- **Checkpoint P4.1:** ACL tests pass độc lập, không có forbidden candidate sau
  candidate cap.
- [x] **Checkpoint P4.2:** fusion tests pass và score/rank trace deterministic;
  xem [DATN-67 fusion evidence](../docs/tai_task/phase-4-fusion-evidence.md).
- [x] **Checkpoint P4 complete (DATN-67 code gate):** focused tests, lint,
  relevant regression và review report pass; không thay đổi ingestion/index lane
  hoặc shared contract/config. Production E2E/ACL acceptance tiếp tục theo dõi
  riêng tại DATN-61.

#### DATN-67 review — 2026-10-05

- Engine/dense và lexical results được fuse như hai ranked lists bằng RRF; output
  score nằm trong `[0, 1]`, trace có fusion method và candidate/filter counts.
- Candidate thiếu chunk ID dùng SHA-256 của toàn bộ nội dung đã chuẩn hóa; tie
  được xếp theo fused score, rank tổng và source-config/chunk key.
- Regression bao phủ semantic paraphrase, exact identifier, query tiếng Việt và
  thuật ngữ domain, duplicate/source isolation, rank agreement, score-scale
  invariance, deterministic ordering, global `/search`, `/search/stream` và P3
  strategy/fallback trace.
- Dùng authorized-scope contract hiện có. Không đổi production ACL resolver,
  ingestion/index, MMR, context, citation hoặc no-answer path; DATN-61 vẫn là
  cổng production E2E/ACL riêng.
- Lệnh, kết quả và review evidence: [phase-4-fusion-evidence.md](../docs/tai_task/phase-4-fusion-evidence.md).

### Files dự kiến

- `SAG/apps/api/sag_api/services/retrieval_service.py`
- `SAG/apps/api/sag_api/api/v1/search.py`
- `SAG/apps/api/tests/test_retrieval_relevance.py`
- `SAG/apps/api/tests/test_search_strategy.py`
- `SAG/tasks/todo.md`
- `SAG/docs/phase-4-review-report.md` hoặc phần Phase 4 tương ứng trong review report

### Rủi ro và rollback

| Rủi ro | Tác động | Giảm thiểu |
|---|---|---|
| Chưa có ACL source of truth | Có thể trả nhầm evidence hoặc giả vờ đã secure | Decision gate; fail closed; không dùng client `source_ids` làm ACL |
| Dense/sparse score khác scale | Ranking lệch, relevance không ổn định | RRF/calibrated fusion và regression scale-invariance |
| ACL filter sau candidate cap | Evidence được phép bị starvation bởi evidence cấm | Filter trước retrieval/top-k nếu backend hỗ trợ; test prefilter |
| Filter failure fallback unfiltered | ACL leakage nghiêm trọng | Fail closed hoặc trả empty/error an toàn |
| Thay đổi shared contract/migration ngoài phạm vi | Xung đột lane và PR | Tách decision/database deliverable, không tự ý sửa |

## Follow-up implementation plan — [SAG][P4] Evidence context, citation và no-answer

**Trạng thái:** implementation trên task branch sau `main`/PR #12. Baseline-flow
notes bên dưới ghi trạng thái trước code; implementation status và kiểm chứng
được ghi ngay sau phần plan. Mục tiêu là đóng gói evidence sau ACL-filtered P3/P4
retrieval, trả citation truy nguyên được và abstain an toàn khi evidence
rỗng/yếu. Không đổi candidate generation, ACL resolver, RRF/fusion hoặc
ingestion/index lane.

### Baseline flow before implementation

1. `/search` và `/search/stream` gọi `_build_query_route()`. Chỉ `CHAT` confidence
   cao mới skip retrieval. `AMBIGUOUS`, `KNOWLEDGE`, `COMMAND`, CHAT confidence
   thấp và lỗi Laya đều tiếp tục retrieval (lỗi Laya dùng strategy `multi`).
2. Global search lấy Source qua `search_source_candidates(principal, requested_ids)`;
   helper chỉ trả mapping Project→Source ở trạng thái `CONFIRMED` trong scope
   của signed principal. Source-scoped search gọi `get_authorized_source()` trước
   retrieval. Dense và lexical nhận cùng scope Source.
3. `retrieve_relevant_sections()` gọi engine `search_many()` cùng lexical
   `grep_chunks()`, lọc logical-delete/reprocess derivatives, rồi rerank bằng
   RRF và relevance gate hiện có. Global API còn có defense-in-depth filter theo
   `source_config_id` đã được authorize. P4 follow-up bắt đầu **sau** các bước đó.
4. Search summary hiện gọi `_search_answer_messages()`: prompt gồm system rule,
   query và các section đánh số; nội dung evidence bị cắt theo tổng **12,000 ký
   tự**, có thể cắt giữa section. Không có explicit no-answer status; không có
   section thì fallback hiện là chuỗi rỗng.
5. Agent gọi `search_context`. Tool lấy retrieval sections, dựng `_format_sections`
   và `build_citations`, rồi `_adapt_tool()` đưa `result.content` vào runtime tool
   response. Citation number được tăng offset xuyên các lần gọi. Agent chỉ
   compress lịch sử ban đầu; các lượt tool/evidence về sau do `AgentRuntime`
   ghép vào request.

### Implemented flow

1. Source/global search resolve versioned locator sau retrieval bằng exact
   `SearchUnit.id == chunk_id`, exact Source được cấp bởi authorization flow,
   active/READY Document, SEARCH_READY version và start/end CanonicalBlock cùng
   version. Locator miss không được đoán từ engine SourceChunk hoặc tên file.
2. Search answer và Agent `search_context` chỉ render section có document/version,
   chunk, page range, anchor và body. Whole items được pack sau ACL/relevance;
   Search API không gọi answer LLM khi pack rỗng/yếu. Agent tool trả kết quả
   fail-closed, sau đó runtime có thể gọi một lượt cuối nhưng terminal gate chặn
   output nếu không có citation claim hợp lệ.
3. Global/source search validator chỉ nhận citation number thuộc exact pack đã
   gửi model; citation response mang SAG Source ID cùng document/version/chunk/
   page/anchor. SSE buffer provider output tới validation rồi emit canonical text.
4. Agent runtime transform hook tính lại toàn bộ messages + tool schemas mỗi
   model turn, dành hai `llm_max_tokens` reserves (answer turn và evidence/next
   turn), rồi đưa phần còn lại vào `search_context`. Assistant deltas được giữ
   lại khi P4 local grounding được yêu cầu hoặc `search_context` đã chạy; các
   high-confidence direct/chat turns giữ nguyên câu trả lời và streaming contract.
5. Exact phrase/identifier/path terms từ QueryAnalysis phải xuất hiện nguyên văn
   theo lexical normalization trong packed evidence; đây là deterministic exact
   match gate, không phải semantic answerability score.

### Existing contracts

#### Retrieval result hiện có

`RetrievedSection` trong `sag/dto.py` là contract nội bộ được tạo từ section của
engine trong `SearchOutcome.from_result()`. Các giá trị cuối cùng sau rerank:

| Field cần cho evidence | Giá trị có ngay trong kết quả hiện tại | Nguồn thực tế / giới hạn |
|---|---|---|
| `document_id` | Không có | Không có trong `RetrievedSection` hoặc section mapping trong `from_section()`. Có thể tìm Document qua `Document.sag_source_id` nếu join đúng engine document-source ID và giới hạn `Document.source_id` trong Source đã authorize; không được tra cứu chỉ bằng client `source_ids`. |
| `document_version_id` | Không có | `DocumentVersion` liên kết tới `Document`, nhưng chunk kết quả không chứa version key và code retrieval không nối tới `SearchUnit`. Không được gán “latest version” theo phỏng đoán. |
| `chunk_id` | Có, có thể nullable | `RetrievedSection.from_section()` lấy `section.chunk_id` hoặc `section.id`; lexical path lấy `SourceChunk.id` trong relational DB của zleap. Đây là locator hiện dùng trong UI/tool, không đồng nghĩa với `SearchUnit.id`. |
| `page` | Không có | DTO và kết quả engine không mang page. `SearchUnit.page_from/page_to` được khai báo trong model nhưng code retrieval hiện không đọc/populate nó. |
| `anchor` | Không có | DTO/engine result không mang anchor. `CanonicalBlock.source_anchor` có trong model nhưng không được nối từ section truy vấn hiện tại. |
| `source_id` | Có nhưng nghĩa đổi theo boundary | Nội bộ `RetrievedSection.source_id` đến từ engine section; lexical path lấy `SourceChunk.source_id` (engine document/source ID), dùng cùng `Document.sag_source_id` trong logical-delete barrier. Đây không phải luôn là SAG Source ID. Search API chuyển sang SAG `Source.id`; citations trong `build_citations()` cũng resolve SAG ID qua `source_config_id` trong map của các Source đã authorize. |
| `content` | Có | Dense result dùng `section.content`; lexical path dùng `SourceChunk` text để dựng `snippet`, rồi snippet trở thành `content`. Khi duplicate, reranker chọn representative có nội dung dài hơn theo quy tắc deterministic. |
| `score` | Có nhưng semantics đổi | Engine score được giữ trong candidate DTO; final selected section ghi đè bằng normalized RRF score `[0,1]`. Công thức dùng rank lists (`k=60`), chia ideal score theo số retriever hoạt động. Không phải cosine similarity, xác suất relevance hay answerability. Raw score không còn trong final result. |
| `rank` | Có, zero-based ở output sau rerank | `from_section()` đọc engine `rank`/`metadata.rank`; lexical path tạo rank theo thứ tự grep. `rerank_sections()` cuối cùng ghi đè `rank` bằng vị trí sau sort, bắt đầu từ 0. |
| ACL metadata | Không có trên từng row | Không có `project_id`, organization, mapping state hay allowed-scope claim trong `RetrievedSection`. ACL được áp từ verified principal → confirmed Source mapping trước candidate generation; `source_config_id` nối result về Source đã authorize. Engine `source_id` chỉ giúp logical-delete/reprocess filtering, không phải ACL token. |

Schema/model hiện có `DocumentVersion`, `CanonicalBlock` và `SearchUnit` cùng
DDL đặc tả, nhưng truy vấn production code ở `EngineManager.search_many()`/
`grep_chunks()` hiện lấy chunk từ zleap `SourceChunk`/engine search. Không thấy
đường join ổn định từ chunk trả về sang `SearchUnit`/canonical block; model fields
đơn thuần không chứng minh dữ liệu đã được populate hay gắn với chunk đang trả.
`Document.sag_source_id` giúp định vị document hiện tại cho logical-delete, nhưng
không cung cấp version/page/anchor của đúng indexed chunk.

#### Citation provenance hiện tại, end-to-end

| Stage | Search API | Agent |
|---|---|---|
| Retrieval → evidence | `SearchOutcome.sections: list[RetrievedSection]`, sau ACL filter | Cùng retriever với authorized `host_context.sources` |
| Evidence → context | `_search_answer_messages()` tạo `[1] heading + content`; cắt theo character limit | `_format_sections()` tạo `[n] heading + content` hoặc event synopsis và nguyên văn; chưa có bounded evidence pack |
| ID → LLM | Số `[n]` trong prompt; `evidence_count` là số block | Citation offsets toàn cục của tool calls; tool text và `result.citations` sinh song song từ sections |
| LLM → validator | `_validated_answer()` chỉ yêu cầu có ít nhất một `[n]`, và mọi `n` nằm trong `1..section_count` | `_finalize_answer_citations()` bỏ số không có citation object mang `chunk_id` + SAG `source_id`; syntax/provenance cơ bản, không đối chiếu evidence ID của final context pack |
| Final → source locator | Summary inline; `SearchResponse` không có citation objects | Citation object có `source_id/name`, `chunk_id`, heading/snippet/score và có thể event refs |

Thiếu trong cả đường dẫn: evidence ID bền vững; `document_id`,
`document_version_id`, page, anchor; citation registry gắn với **đúng phần đã
được render vào prompt**. Search validator chỉ kiểm tra cú pháp/range, và vẫn có
thể chấp nhận ID của block bị cắt một phần. Agent validator xác nhận citation
object có source/chunk nhưng chưa chứng minh nó nằm nguyên vẹn trong actual tool
context. Cả hai không kiểm tra entailment giữa từng câu trả lời và đoạn trích.
Search SSE và agent stream hiện phát raw model deltas trước final validation;
invalid citation có thể xuất hiện tạm thời dù bị sửa trong terminal payload.

**ACL-safe provenance rule:** chỉ enrich từ result đã nằm trong authorized
Source scope. Nếu tra metadata, truy vấn phải ràng buộc `Document.source_id` với
IDs lấy từ verified `Source` set, khớp `source_config_id`, khớp engine document
source ID và loại Document không active/deleting. Không tin `source_id` gửi từ
client, không query toàn cục rồi mới lọc citation, không suy diễn locator từ
filename hay text similarity. Nếu không có mapping exact thì locator thiếu và
evidence không được đánh dấu traceable.

### Evidence model

Implementation thêm `EvidencePack`/`ToolEvidencePack` phía service và các
optional locator fields trên `RetrievedSection`; DTO fields chỉ là transport,
không trở thành authorization source:

- Numeric `[n]` là ID theo từng pack/context và giữ tương thích UI/API; chưa có
  persistent/stable `evidence_id` riêng.
- Pack giữ section body, heading, `source_config_id`, final RRF `rank/score` và
  engine `chunk_id`; API citation gắn public SAG `source_id/name`. Hiện chưa có
  `content_hash` trên `RetrievedSection`/citation contract.
- Locator `document_id`, `document_version_id`, page range và anchor chỉ được set
  từ exact DB join đã xác minh. Incomplete locator không được pack; không suy
  `latest version` hoặc giá trị thiếu.
- ACL scope không được phát ra như citation metadata. Authorization provenance
  nội bộ giữ lại reference tới Source object đã authorize để validator xác minh.
- Chỉ pack items được phép và đầy đủ mới được đưa vào LLM context. Citation IDs
  được cấp **sau** chọn evidence cuối cùng; mọi final citation resolve từ đúng
  registry đó. Fallback cũng chỉ render pack, không quay lại candidate list.

### Token budget algorithm

#### Hiện trạng đã xác minh

- Không có model tokenizer trong search answer path. `generation/prompt.py`
  `estimate_tokens()` dùng heuristic CJK characters ≈ 1 token và ký tự còn lại
  ≈ 1/4 token (round up); đây là estimator xấp xỉ, không phải provider tokenizer.
- `settings.llm_context_window` lấy mặc định từ provider registry
  (`model_providers.py`: OpenAI-compatible 128,000; Anthropic 1,000,000; Gemini
  1,048,576), nhưng có thể cấu hình qua `SAG_*`/Settings API. Đây là provider
  default/configured value, không lookup giới hạn model cụ thể của gateway.
- `settings.llm_max_tokens` mặc định 20,000, có thể cấu hình và được truyền thành
  `max_tokens` trong `LLMClient`; code hiện không trừ nó khỏi context window.
- Search answer chỉ giới hạn evidence ở 12,000 ký tự; không tính system prompt,
  query, role/message framing hoặc output reserve; có thể cắt ngang block.
- Agent history dùng `int(llm_context_window * 0.4)` với cùng estimator, nhưng
  chỉ giới hạn lịch sử ban đầu. System prompt, current query, tool definitions,
  các lượt tool result và output reserve chưa có một phép tính tổng tại SAG layer.

#### Thuật toán đã triển khai

Không thay 12,000 chars bằng một hằng số token. Với từng lần gọi model:

1. Dùng `llm_context_window` và `llm_max_tokens` đang hiệu lực; cap reserve ở
   context window để cấu hình không hợp lệ tạo pack rỗng/no-answer.
2. Render fixed system instructions, current query, conversation/history hiện có,
   tool schema/framing (nếu runtime cung cấp), và evidence theo format cuối cùng.
   Tính `base_input_estimate = estimate_tokens(render(base messages))`, không chỉ
   đếm content của chunks.
3. Tính evidence capacity động từ
   `context_window - reserved_output_tokens - base_input_estimate`. Tính token
   trên từng block đã render đầy đủ, gồm heading, citation ID và separator.
4. Thêm từng evidence item theo thứ tự retrieval hiện có khi toàn block còn vừa;
   bỏ item quá lớn và tiếp tục thử item nhỏ hơn, không cắt block. Citation number
   được render theo đúng thứ tự packed; ranking/fusion không đổi.
5. Search path packer ước tính toàn bộ rendered messages trước call. Agent
   `transform_context` re-estimate actual runtime messages + tool schemas từng
   turn; remaining evidence budget đã trừ hai output reserves. Packer không ghi
   thống kê token ra response; tests kiểm tra không vượt estimator budget.

`estimate_tokens()` chỉ là estimator; nó không bảo đảm provider token count.
Agent hook có actual runtime messages tại transform boundary và có thể loại bỏ
history/evidence units; nếu system prompt + current query + tool schemas tự vượt
budget thì runtime fail-closed. Hard guarantee theo tokenizer/context window
chính xác của từng provider/model vẫn là gap.

### Citation provenance algorithm

1. Lấy candidate sections đã qua authorization + logical-delete + existing
   relevance/fusion. Enrich source/document locator trong scope Source đã được
   resolve; không thay thứ tự hay eligibility retriever.
2. Chỉ nhận chunk/document match exact theo engine `source_config_id` + engine
   document-source ID + `chunk_id`. Xác minh document còn searchable/active và
   `Document.source_id` thuộc Source set đã authorized.
3. Resolve version/page/anchor qua exact versioned index lineage nếu có. Không
   dùng `latest DocumentVersion` hoặc text-hash similarity làm bằng chứng mapping.
   Nếu locator bắt buộc chưa resolve đầy đủ, đánh dấu missing; theo policy an
   toàn hiện đề xuất loại khỏi answerable pack và trả `no_answer`.
4. Token-pack các EvidenceItems hoàn chỉnh. Numeric citation number được render
   theo đúng thứ tự pack; danh sách packed sections là registry cụ thể của lượt
   answer (Agent đọc citation IDs từ tool messages còn lại sau context fitting).
5. Search validator kiểm tra cú pháp/range trên chính packed sections; Agent
   finalizer yêu cầu citation object có locator và lọc số theo evidence ID thực
   sự còn trong rendered `search_context` message. Số không có trong pack bị
   loại; factual claim không có mapping citation thì Search fallback dùng trích
   đoạn có citation, còn local-only Agent trả no-answer.
6. Citation output mang `document_id`, `document_version_id`, `chunk_id`, page
   range/anchor, SAG `source_id/name` và excerpt. Agent còn đánh dấu `claim_level`;
   Search API chỉ trả citations được dùng theo numeric references. Không có hash
   hoặc entailment proof; Agent run-level source fallback không được dùng để
   vượt qua local-evidence no-answer gate.

### Empty/Weak/Sufficient algorithm

Score semantics hiện tại không cung cấp answerability: RRF `[0,1]` phản ánh rank
agreement/position; relevance gate có lexical feature heuristic và semantic
relative ratio `0.68`, không phải xác suất, confidence hoặc claim support. Không
được đặt một RRF cutoff mới để phân loại.

- **EMPTY:** sau ACL, logical-delete filtering và existing relevance gate không
  còn section nào. Search API không gọi answer LLM; trả no-answer có cấu trúc,
  rỗng citation. Agent SearchContext trả tool result không có evidence/citation;
  runtime hiện vẫn có thể chạy lượt cuối để hoàn thành tool loop, nhưng giữ kín
  deltas và canonical output sẽ bị thay bằng no-answer.
- **WEAK (có thể phát hiện bằng contract hiện tại):** candidates có nhưng không
  có item vừa context budget nguyên vẹn; provenance/ACL exact join thiếu; locator
  bắt buộc chưa đủ; hoặc query features có exact identifier/path nhưng không có
  packed content nào khớp qua lexical normalization. Search API không gọi answer
  LLM; Agent không phát raw answer delta và terminal gate trả no-answer.
- **SUFFICIENT (operational pack eligibility, không calibrated confidence):** có
  ít nhất một evidence item đầy đủ, được phép, traceable, được render nguyên vẹn
  trong budget và vượt existing relevance gate; với exact identifier query còn
  phải hiện diện chính identifier trong evidence. Chỉ khi đó mới gọi LLM với
  prompt grounded/citation-constrained. Không dùng score để tuyên bố chắc chắn
  nội dung evidence entail câu trả lời.

**GAP còn lại:** code không có calibrated answerability, claim coverage hay
entailment validator. Với câu hỏi factual tự nhiên, “có section relevant + locator
đầy đủ” vẫn không chứng minh section trả lời đủ câu hỏi. Policy trong scope có thể
chặn EMPTY và WEAK theo điều kiện cấu trúc trên, nhưng không thể chứng nhận semantic
WEAK ở mọi truy vấn. Safest rollout là abstain khi không đạt structural pack
eligibility, chỉ cho phép answer generation với citation cho từng factual claim,
và ghi rõ đây chưa phải bảo đảm entailment. Đo/calibrate answerability là follow-up
riêng; không đặt ngưỡng RRF tạm thời.

### Ambiguous query behavior

P3 contract không đổi: `_build_query_route()` tiếp tục gửi `AMBIGUOUS` qua
retrieval; chỉ high-confidence `CHAT` skip. Agent initial tool choice và prompt
hiện có thể yêu cầu hoặc chọn clarification theo intent/tool contract; P4 không
thêm rule “ambiguous ⇒ hỏi lại”. Sau routing hiện có, P4 chỉ pack evidence và
đánh giá EMPTY/WEAK/SUFFICIENT. Nếu ambiguous query có evidence đủ structural
eligibility thì dùng evidence đó; nếu rỗng/yếu thì no-answer theo evidence policy,
không tự sinh câu trả lời và cũng không tự biến thành clarification.

### API/stream changes

- Giữ request, query route, source scope và `sections` retrieval response tương
  thích. Additive response fields: `answer_status`, `citations` có
  locator provenance, và `no_answer_reason`/evidence diagnostics không nhạy cảm.
- Search summary/fallback chỉ sử dụng final EvidencePack. Empty/Weak trả thông
  báo no-answer rõ nghĩa thay vì `summary=""` hoặc excerpt bất kỳ; Search API
  bỏ answer LLM call, Agent giữ raw output kín và thay final answer bằng
  no-answer nếu evidence/citation không đạt gate.
- SSE giữ tên event `result`, `summary.delta`, `completed`, `error`, nhưng không
  phát raw LLM text/citation trước validator. Buffer đến khi citation IDs được
  xác thực; sau đó có thể emit canonical answer và completed. Đây là thay đổi
  latency/streaming semantics so với true provider deltas hiện tại; cần regression
  cho consumers.
- Agent final citation validator dùng registry EvidencePack thực tế của các tool
  results. Để đảm bảo empty/weak không leak qua runtime delta, buffer assistant
  answer deltas trong knowledge-only, scoped, hoặc các run có thể dùng
  `search_context` đến final validation/no-answer. External web citations giữ
  loại/validator riêng và không được coi là SAG document citation.
- Không thay routing trace, ACL fields, result ranking hoặc graph/source-scoped
  event behavior.

### Test matrix

| Case | Setup/expectation |
|---|---|
| Greeting / high-confidence CHAT | Routing giữ nguyên; retrieval/answer LLM không bị gọi; không có fabricated citation; no-answer không thay greeting contract của Agent. |
| Factual question, evidence rõ | Chỉ section trong authorized scope được pack; block nguyên vẹn và nằm trong budget; answer dùng facts trong evidence; final citation map về locator fixture đầy đủ. |
| Exact identifier | Exact lexical result ưu tiên như hiện tại; identifier phải có nguyên văn trong pack; score `0.5` hay RRF position không làm mất match; cite đúng chunk/version/page/anchor. |
| Ambiguous | Assert query_route/coarse intent và strategy không đổi; evidence đủ thì grounded answer; evidence rỗng/yếu thì no-answer, không ép clarification. |
| No evidence | Zero post-ACL relevant sections; explicit no-answer, `citations=[]`, Search API không gọi answer LLM, Agent không phát raw delta/claim. |
| Weak structural evidence | Missing locator, unauthorized/mismatched source, exact ID vắng, hoặc không item nào vừa budget; Search API không gọi answer LLM, Agent trả no-answer và không lộ raw answer delta. |
| Token budget | Tính tổng system + conversation/query + tool framing + evidence bằng estimator; output reserve lấy `llm_max_tokens`; không vượt `llm_context_window`; item không bị cắt ngang. Kiểm tra nhiều provider settings và prompt lớn. |
| Citation syntax/provenance | Valid ID trong final pack được giữ; malformed/out-of-range/invented ID, item bị bỏ/không render, source ngoài authorized set hoặc thiếu locator bị loại/answer abstains. |
| Stream | Không thấy speculative invalid text trước validation; status/citations nhất quán giữa result và completed; cancellation/provider failure vẫn đóng stream an toàn. |
| Existing regressions | `tests/test_search_stream.py`, `tests/test_retrieval_relevance.py`, `tests/test_agentic.py`; bổ sung tool path (`test_agent_tools.py`) nếu thay đổi `SearchContextTool`. |

### File-by-file changes

**Implemented in this task branch:**

- `SAG/apps/api/sag_api/services/evidence_service.py` — exact ACL-scoped locator
  resolver, whole-item token packers, exact identifier guard and no-answer text.
- `SAG/apps/api/sag_api/services/retrieval_service.py` — answer orchestration,
  canonical citation validation and buffered search generation; candidate
  generation, ACL filters, candidate limits và RRF không đổi.
- `SAG/apps/api/sag_api/api/v1/search.py` — dùng pack ở source/global search và
  stream; trả no-answer/citations sau final validation.
- `SAG/apps/api/sag_api/schemas/search.py` — additive `answer_status`, citation
  locator schema và no-answer reason; giữ field score documentation semantics.
- `SAG/apps/api/sag_api/tools/builtin.py` — exact resolve, locator filter,
  context-budgeted `search_context` và citations chỉ từ items trả runtime.
- `SAG/apps/api/sag_api/services/agent_service.py` — actual-message/tool-schema
  fitting each runtime turn, remaining evidence budget, citation visibility gate,
  no-answer for ungrounded local evidence, and protected answer deltas.
- `SAG/apps/api/sag_api/sag/dto.py` — optional locator transport fields; legacy
  hits remain incomplete unless the exact resolver fills them.
- `SAG/apps/api/sag_api/generation/prompt.py` — locator fields on citation
  objects; existing `estimate_tokens()` is reused.
- `SAG/apps/api/sag_api/tools/base.py` — per-run context budget and local-search
  gate state.
- No changes to `generation/llm.py`, `sag/engine_manager.py`, schema migrations,
  or ingestion/index lane.
- Tests: `SAG/apps/api/tests/test_search_stream.py`,
  `test_retrieval_relevance.py`, `test_agentic.py`, `test_agent_tools.py` and
  `test_traceability.py` cover the API, tool and DB resolver seams.
- `SAG/tasks/todo.md` — actual implementation status, verified checks and open
  corpus/answerability gaps.

### Out-of-scope

- Thay đổi global retrieval, ACL/principal resolver, Project→Source mapping,
  dense/lexical candidate generation, RRF/fusion, score contract hoặc P3 routing.
- Sửa ingestion, canonicalization, chunk generation, embedding/vector index,
  populate `CanonicalBlock`/`SearchUnit`, reindex/backfill hay schema migration.
- Tự gán version bằng latest row, trang/anchor từ suy đoán, hoặc ghép chunk với
  canonical content bằng fuzzy/text similarity.
- Thay đổi Graph/Tree routing, agent clarification policy, external web search,
  hoặc citation rules của web sources.
- Frontend UX/FE streaming changes ngoài additive API event compatibility; nếu
  client dựa vào raw token deltas, cần task/owner thống nhất riêng.

### Remaining gaps

1. **Hard locator gap — rollout blocker:** resolver exact đã có, nhưng legacy
   zleap `SourceChunk.id` không được chứng minh bằng `SearchUnit.id` của
   versioned index. Locator test dùng fixture cùng exact IDs; corpus thực tế cần
   xác nhận `SearchUnit` đã được populate và IDs khớp. Với kết quả legacy không
   khớp, search trả no-answer thay vì câu trả lời không citation. Owner ingestion/
   index (phan tai) cần thống nhất mapping/reindex deliverable riêng; task này
   không sửa lane đó.
2. **Answerability gap:** không có calibrated signal/claim coverage/entailment.
   RRF/relevance gate không thể lấp gap này. Tác vụ này chỉ có thể định nghĩa
   operational structural eligibility và abstain ở case yếu phát hiện được.
3. **Tokenizer/model window gap:** `estimate_tokens()` là heuristic; provider
   registry chứa provider default chứ không exact model-specific limit. Runtime
   messages/tool schemas được fit mỗi turn nhưng provider token usage có thể cao
   hơn estimator; hard guarantee cần tokenizer/model metadata chính xác.
4. **Agent stream latency:** buffering tới final validation tăng
   time-to-first-answer; consumer phải nghiệm thu single canonical delta thay vì
   provider's speculative token deltas. Cancellation/provider failure tests cover
   stream close, nhưng không đo latency thực tế.

**Database/config/security impact thực tế:** không đổi schema/migration/ACL hoặc
LLM settings mặc định. Packer đọc các config hiện hữu. Metadata lookup chỉ dùng
Source set đã authorize và exact versioned IDs; lookup miss fail closed. Nếu
muốn persist exact chunk→version/page mapping thì đó là deliverable DB/index
riêng, ngoài task này. Retrieved document text được đánh dấu là dữ liệu không
đáng tin cậy trong Search và Agent instructions; không thể thay thế entailment
validation.

### Acceptance checklist

- [x] Retrieval-result table và source-of-truth fields được giữ trong review notes;
      không nhầm engine `source_id` với SAG Source ID hoặc RRF với confidence.
- [x] Context chỉ chứa whole EvidenceItems thuộc ACL scope và nằm trong budget
      estimate từ actual messages, configured context window và output reserve.
- [x] Citation IDs chỉ resolve tới item được render; citation locator có
      document/version/chunk/page/anchor đã verify, không có metadata suy đoán.
- [x] EMPTY và structural WEAK không lộ raw stream deltas;
      trả no-answer rõ ràng với citation list rỗng.
- [x] Factual/exact-ID Checkpoint A có positive test với fixture lineage đầy đủ;
      exact identifier không bị RRF score scale/position làm mất.
- [x] Ambiguous query giữ routing contract hiện có; P4 chỉ thay evidence outcome.
- [x] Checkpoint A regression đủ greeting, factual, exact identifier, ambiguous, no evidence;
      stream và Agent citations được kiểm tra riêng.
- [x] `tasks/todo.md` ghi rõ hard locator gap, answerability gap và validation
      chưa chạy/không đạt; không đánh dấu hoàn thành chỉ vì unit suite xanh.
- [ ] Trước review/merge, xác nhận corpus production có exact
      version/page/anchor mapping hoặc owner chấp thuận rõ partial citation
      contract; nếu chưa thì merge readiness bị chặn.

### Implementation verification — 2026-10-01

- Final focused regression command: `test_retrieval_relevance.py`,
  `test_search_stream.py`, `test_traceability.py`, `test_agent_tools.py` và
  `test_agentic.py`: **69 passed, 1 deselected** after all code changes.
  Deselect là `test_initial_tool_policy_anchors_time_and_preserves_clarification`;
  an unfiltered run failed there because `_initial_tool_choice("最近 ChatGPT 有哪些更新？")`
  returns `"none"` instead of `get_time`. That routing logic/test is unchanged by
  this task.
- Ruff trên mọi Python file đổi: **passed**. `git diff --check`: **passed**.
- Runner dùng sibling checkout's populated `.venv` với `PYTHONPATH` trỏ về
  current task checkout; local task `.venv` thiếu binary `link.exe` để build
  dependency `litellm`, nên full environment install/build chưa xác minh.

## Luồng mục tiêu

### Ingestion và readiness

~~~text
Upload
  -> validate quyền/file
  -> checksum + source registration + Document/Version/Job (idempotent)
  -> canonical extraction
  -> exact/near dedup + temporal lineage
  -> Search Units + dense/sparse index + manifest verification
  -> SEARCH_READY

Sau SEARCH_READY, chạy nhánh knowledge bất đồng bộ:
Canonical/Search Units
  -> Knowledge Units + E0/E1 + selective E2
  -> calibrated sparse multi-signal graph
  -> constrained Knowledge Routing Tree + ACL-safe profiles
  -> KNOWLEDGE_READY
~~~

### Truy vấn

~~~text
Query
  -> Laya coarse intent
  -> deterministic feature extraction
  -> Query Strategy Planner
  -> ACL-aware tree routing khi knowledge tree sẵn sàng
     hoặc global hybrid retrieval làm baseline/fallback
  -> branch-local search + global escape search
  -> fusion -> content dedup -> MMR
  -> coverage check -> bounded graph expansion nếu cần và còn budget
  -> bounded rerank -> context/citation builder
  -> grounded LLM hoặc no-answer response
~~~

Checkpoint A xác nhận nhánh đầu đã truy vấn được và citation hoạt động; không chờ nhánh knowledge. Checkpoint B/C/D lần lượt xác nhận routing, incremental tree và research loop.

## Thứ tự triển khai và cổng nghiệm thu

### Phase 0 — Contracts & Foundations

**Phụ thuộc:** Không.

1. Khảo sát flow upload/job/search/query, schema, config, ACL và trạng thái hiện có; ghi rõ phần tái sử dụng, gap và migration cần thiết.
2. Chốt contract cho Document/Version/SourceSnapshot/IngestionRun, provenance/timestamps, stable IDs, idempotency key và stage runs.
3. Chốt readiness/capability semantics: search chỉ sẵn sàng sau index manifest hợp lệ; knowledge readiness độc lập; lỗi enrichment không hạ search readiness.
4. Chốt Laya coarse-intent contract, query-feature/planner contract, retrieval trace và version fields cho index/tree/config.
5. Xác nhận tenant/project/security scope và cách enforce ACL trên SQL/Qdrant; rà soát rollback cho schema/index changes.

**Cổng ra:** các contract và thay đổi schema/migration đã được đối chiếu với code hiện hữu; có thể truy lineage từ artifact về source/version; không còn trạng thái READY mơ hồ gộp search với knowledge.

### Phase 1 — Upload & Versioned Source

**Phụ thuộc:** Phase 0.

1. Giữ validation quyền, extension, MIME signature, size và policy trước xử lý nặng.
2. Stream file vào storage tạm, tính checksum; dùng source identity + checksum để áp duplicate policy.
3. Trong transaction, tạo/liên kết Document, DocumentVersion, SourceSnapshot và IngestionRun; request retry dùng idempotency key.
4. Triển khai đúng bốn trường hợp upload: cùng hash/cùng identity; cùng hash/nguồn khác; hash mới/cùng identity; hash mới/nguồn mới.
5. Worker ghi stage/status/error có thể tra cứu; status API và frontend thể hiện tiến trình, retry/failure mà không làm mất lineage.

**Cổng ra:** retry không tạo workflow/document logic trùng; upload sai bị từ chối trước pipeline; lỗi stage truy nguyên được.

### Phase 2A — Canonical Extraction

**Phụ thuộc:** Phase 1.

1. Xác nhận parser theo định dạng thực sự được sản phẩm hỗ trợ; không tự mở rộng format.
2. Lưu canonical blocks có loại block, ordinal, page range, heading/section path, source anchor và version nguồn.
3. Chuẩn hóa Unicode/whitespace nhưng giữ cấu trúc bảng/code, punctuation và identifier; nhận diện boilerplate mà vẫn giữ dấu vết audit.
4. Chỉ dùng model/LLM cho layout ambiguity có giá trị; không dùng LLM để sửa text mặc định.
5. Ghi output versioned/temp trước commit để extraction có thể retry an toàn.

**Cổng ra:** regression fixtures chứng minh block và vị trí ổn định; lỗi extraction có stage/error; canonical block đủ làm nguồn cho dedup, retrieval và citation.

### Phase 2B — Dedup & Temporal

**Phụ thuộc:** Phase 2A.

1. Thực hiện theo thứ tự: file exact hash; block exact hash; near duplicate bằng shingle/MinHash/SimHash/LSH hoặc cơ chế hiện có; semantic similarity chỉ sinh candidate.
2. Giữ nhiều provenance/evidence cho content dùng chung; không xóa lineage khi tái sử dụng.
3. Phân loại quan hệ semantic (EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED); không auto-merge contradiction hoặc superseding version.
4. Lưu source-published/observed/ingested time, claim validity và supersedes lineage khi có dữ liệu.
5. Xác định quy tắc reprocess để retry deterministic và giữ truy vấn lịch sử.

**Cổng ra:** regression corpus có exact, near-duplicate, contradiction và version update; không mất evidence hoặc lịch sử.

### Phase 2C — Search Index

**Phụ thuộc:** Phase 2B.

1. Tạo Search Unit theo ranh giới heading/paragraph/table trước token window; giữ document version, block range, content hash, page/section và security partition.
2. Tạo dense và sparse representation theo cấu hình provider/model đã xác nhận; không đồng nhất Search Unit với Knowledge Unit.
3. Tạo payload/filter index cần thiết trước ingestion; Qdrant point ID ổn định và upsert idempotent.
4. Persist index manifest/version; chỉ báo search capability sẵn sàng sau khi dữ liệu và manifest nhất quán.
5. Giữ PostgreSQL làm nguồn có thể rebuild Qdrant; reprocess thay index theo version, không để point cũ thành evidence hợp lệ.

**Cổng ra:** index có thể rebuild; retry không tạo point trùng; SEARCH_READY không bị đặt khi indexing/manifest còn thiếu. Query UX chưa được nghiệm thu cho đến Checkpoint A.

### Phase 3 — Laya & Query Analyzer

**Phụ thuộc:** Phase 0; dùng query/search contract của Phase 2C.

1. Tạo coarse intent CHAT/KNOWLEDGE/COMMAND/AMBIGUOUS; giữ nguyên query gốc và user scope.
2. Chỉ nhận diện CHAT confidence cao mới được bỏ retrieval; AMBIGUOUS, confidence thấp, model unavailable hoặc init error phải fallback retrieval.
3. Load Laya lazy/singleton theo config hiện có; tránh retry load nặng liên tục khi init lỗi.
4. Trích xuất deterministic features: exact phrase/identifier/path/code, entity/relation cues, time/version filters, global/topic cues và multi-hop cues.
5. Giữ feature extraction có thể chạy khi Laya tắt; tách lỗi Laya khỏi lỗi retrieval/generation.

**Cổng ra:** fixture/regression cho chat, factual, tiếng Việt, exact identifier, temporal, low confidence và Laya unavailable; không mất query/context do output nhãn lỗi.

### Phase 4 — Retrieval Engine v1

**Phụ thuộc:** Phase 2C và Phase 3.

1. Hoàn thiện global hybrid retrieval có ACL/filter scope làm đường chạy đầu tiên; chưa phụ thuộc tree.
2. Kết hợp dense/sparse bằng rank fusion (RRF/DBSF) hoặc calibrated fusion; không cộng raw score khác scale.
3. Collapse exact/near-duplicate evidence, áp MMR để tăng diversity; rerank chỉ trên tập nhỏ nếu latency budget cho phép.
4. Dựng context theo coverage/diversity và token budget; citation map ngược được về source/version/block/page/anchor.
5. Tích hợp generation LLM trong Settings độc lập với Laya; evidence không đủ phải trả lời no-answer/thiếu nguồn, không gửi cả tài liệu vào prompt.
6. Ghi trace theo stage, requested/effective strategy và fallback; tạo regression query corpus theo taxonomy ở Phụ lục E.

#### P1 ACL runtime rollout gate

`tasks/acl-runtime-implementation-plan.md` và `docs/security/` ghi chi tiết contract, inventory và implementation status. Nhánh runtime đang có verifier RS256/JWKS, Project→Source mapping resolver, fail-closed route guards, RRF fusion và mock/contract tests. Đây **chưa** phải production ACL acceptance: cần issuer/JWKS thật từ BE/Continuum, header propagation an toàn, DB/data-owner phê duyệt DDL/backfill, mapping writer/revoke flow, cùng cross-Project/cross-Organization leakage tests trên mọi evidence path. Giữ PR Draft và P1 chưa hoàn thành cho tới khi có đủ các bằng chứng này.

**Checkpoint A — SEARCH_READY end-to-end:** upload → extract → dedup → index → global hybrid retrieval → context/citation hoạt động; có failure path; có thể tắt/trễ toàn bộ knowledge enrichment mà vẫn hỏi được tài liệu.

**Implementation status — 2026-10-02:** Task branch `feat/Thang-checkpoint-a-search-ready-be-api` wires global Search, stream, and `search_context` to ACL-scoped, verified Phase 2C SearchUnits; accepts producer `search_status=READY/SEARCH_READY` independently of `DocumentVersion.status`; packs/cites only traceable evidence; and fails closed for empty/weak/unready evidence. PR review follow-up added a pooled Qdrant HTTP client, reduced duplicate channel materialization, and suppressed raw HTTP exception chaining. Relevant checks: **161 passed, 4 warnings**; Ruff and `git diff --check` pass. This is not the full Checkpoint A gate: actual upload → producer-written index → API against real Qdrant, trusted tenant/partition and Project Source owner contracts, model-identity compatibility, and staging leakage/revocation evidence remain open. See the [Checkpoint A research task record](../docs/Thang_Task/%5BSAG%5D%5BCheckpoint%20A%5D/researchtask.md) for commands, review fixes, and exact gaps.

### Phase 5 — Knowledge Units & Graph

**Phụ thuộc:** Checkpoint A; dữ liệu canonical/versioned từ Phase 2A–2C.

1. Tạo Knowledge Unit ổn định cho clustering/tree, tách biệt Search Unit tối ưu retrieval.
2. Chạy E0 deterministic trước; thêm E1 specialized model khi có ích; chỉ đưa trường hợp khó/giá trị cao vào E2 LLM queue.
3. E2 chạy async với budget, concurrency, backpressure, retry độc lập; không chặn hoặc hạ SEARCH_READY.
4. Mỗi entity/alias/claim/relation phải giữ evidence, provenance, confidence và validity.
5. Sinh candidate edges bằng semantic, lexical, entity, structure, citation và temporal signals; xử lý missing signal bằng renormalization.
6. Calibrate từng signal về [0,1], version config/quantiles; giới hạn degree/edge threshold để lưu sparse graph trong PostgreSQL.

**Cổng ra:** graph có thể tái tạo và truy nguyên; E2 backlog/failure không ảnh hưởng search; score calibration được đo trên corpus trước khi dùng cho tree.

### Phase 6 — Knowledge Routing Tree

**Phụ thuộc:** Phase 5.

1. Build theo tenant/project topology; tách ACL-safe routing profiles theo security partition, không trộn raw summary/centroid giữa quyền.
2. Dùng Constrained Hierarchical Leiden làm primary; xử lý component lớn bằng giant-component guard và balanced fallback.
3. Áp capacity/depth/children constraints; repair cụm quá nhỏ và xác định stop criteria để tránh tree quá sâu/rộng.
4. Sinh node prototypes/profile cần cho routing, gồm dense/medoid, sparse, entity, temporal và accessible-unit metadata.
5. Đo cohesion, giant ratio, child-size entropy, edge-cut, depth và routing recall.
6. Chỉ publish khi quality gates đạt; defaults như N_min/N_max/Depth_max, giant ratio phải benchmark trước khi freeze.

**Cổng ra:** tree version có manifest và lineage; build không đạt gate bị từ chối publish; profile không tạo leakage ACL.

#### Checkpoint B — implementation slice (2026-10-03)

- Builder dùng contract `routing-snapshot.v1` cho Knowledge Unit + graph edge; không
  dùng SearchUnit thay thế Knowledge Unit. Tenant/project phải đồng nhất; mọi
  cluster/profile được tạo riêng theo `security_partition_id`, edge cắt qua
  partition bị bỏ qua.
- `constrained-hierarchical-leiden-cpm-v1` chạy có seed ổn định và community
  capacity; giant guard/balanced fallback, small-cluster repair, max children/depth
  và stop conditions nhận từ config đã version.
- Profile gồm dense medoid, sparse terms, entity set, temporal range và
  accessible-unit count. Node ID ổn định theo scope + membership; tree version /
  manifest checksum phụ thuộc input, edge, config và lineage.
- Builder trả candidate snapshot kèm metrics/gates; quality fail cho trạng thái
  `REJECTED`. B2 fixture/contract được ghi tại
  [phase-6-tree-evidence.md](../docs/phase-6-tree-evidence.md).
- **Chưa đạt Checkpoint B:** Phase 5 Knowledge Unit producer/store chưa được triển
  khai ở repo; candidate chưa được persist/publish vào active pointer; routing
  recall hiện chỉ đo bằng fixture, chưa có corpus benchmark. Không dùng task này để
  đánh dấu `ROUTING_READY`; Checkpoint C incremental publish vẫn ngoài phạm vi.

### Phase 7 — Tree-guided Retrieval

**Phụ thuộc:** Phase 3, Phase 4 và Phase 6.

1. Hoàn thiện QueryStrategyPlanner giữa coarse intent/query features và retrieval modes; lưu reason codes/version. Laya không map trực tiếp sang strategy.
2. Hỗ trợ primary strategy + modifiers: EXACT, LOCAL_FACTUAL, ENTITY_RELATIONAL, TEMPORAL, GLOBAL_TOPIC và MULTI_HOP; MULTI_HOP là escalation khi có bằng chứng/coverage thấp.
3. Chụp một immutable search/tree/ACL snapshot đầu request; prune node không có accessible units trước beam expansion.
4. Route bằng ACL-safe profile với dense+sparse+entity+time+prior; dùng entropy/margin để giữ nhiều branch khi chưa chắc.
5. Chạy branch-local hybrid retrieval cùng global escape budget; Qdrant luôn áp exact ACL filter. Local rỗng/coverage yếu bắt buộc thử escape.
6. Fuse → content dedup → MMR → coverage check; graph expansion giới hạn hop/node/latency và chỉ chạy khi coverage yếu; rerank có thể bị bỏ qua khi hết budget.
7. Trả trace phân biệt requested/effective strategy, reason codes, selected nodes/tree version và fallback/blackhole flags.

**Checkpoint B — ROUTING_READY:** regression các mode Exact/Temporal/Relational/Global/Multi-hop đạt ngưỡng; route sai hoặc node inaccessible không làm recall về 0; không có ACL leakage/blackhole vượt ngưỡng.

### Phase 8 — Incremental Tree

**Phụ thuộc:** Checkpoint B.

1. Gán dữ liệu mới vào base tree bằng delta; cập nhật node/ancestor và theo dõi drift trước khi rebuild rộng.
2. Rebuild subtree khi drift/quality gate yêu cầu; giữ stable node matching và lineage.
3. Build vào inactive routing slot trên snapshot/version riêng; cập nhật Qdrant dual-slot payload theo manifest.
4. Verify counts/checksum/quality trước khi đổi active pointer; mỗi query đọc một snapshot thống nhất.
5. Nếu build/publish lỗi, giữ active tree cũ; hỗ trợ rollback và không để PG/Qdrant lệch slot.

**Checkpoint C — INCREMENTAL_READY:** ingest bình thường không cần full rebuild; subtree update, publish, query trong lúc switch và rollback đã được kiểm chứng.

### Phase 9 — Knowledge Quality & Gap

**Phụ thuộc:** Phase 6–7 và metrics/trace của Phase 4, 7.

1. Tính coverage, source diversity, freshness, contradiction, query demand, uncertainty và growth rate theo node/topic.
2. Tính gap priority từ tín hiệu có cấu trúc; version các metric/threshold.
3. Chỉ gọi LLM để diễn đạt gap đã xác định thành câu hỏi; không để LLM tự phát hiện gap vô điều kiện.

**Cổng ra:** gap có evidence/reason/priority truy nguyên được; score thay đổi theo dữ liệu kiểm thử dự kiến.

### Phase 10 — Research Agent

**Phụ thuộc:** Phase 9 cùng upload/ingestion pipeline Phase 1–2.

1. Đi theo vòng lặp: Gap Detector → Question Generator → Research Planner → source discovery/fetch → normal ingestion → dedup/version/tree update.
2. Ghi nguồn, lý do, policy/domain allowlist, ngân sách và trạng thái cho từng research task.
3. Áp giới hạn crawl/fetch, duplicate guard, idempotency và dừng lặp khi không có thông tin mới.

**Checkpoint D — AGENT_READY:** research task có căn cứ từ gap; fetch có giới hạn; kết quả đi qua ingestion thường và không tạo crawl loop/duplicate.

### Phase 11 — Hardening

**Phụ thuộc:** Checkpoint B–D; có thể xử lý từng lớp theo rủi ro rollout.

1. Thêm cache sau khi key/version boundary ổn định; key phải bao gồm scope/ACL, search/tree/model/planner/config version và filters phù hợp.
2. Tắt answer cache mặc định; semantic route cache chỉ bật sau khi đo route-equivalence precision và ACL isolation.
3. Đo p50/p95/p99 theo stage; định nghĩa SLO và latency budget theo benchmark của môi trường, không đóng cứng số chưa đo.
4. Chạy regression/load, ACL leakage/blackhole, failure/retry, tree publish/rollback và cache invalidation checks.
5. Hoàn thiện migration/rollback runbook, dashboard/alerts và hướng dẫn khôi phục/rebuild Qdrant từ PostgreSQL.

**Cổng ra:** lỗi có thể định vị theo stage; query có fallback khi hết budget; vận hành phục hồi được mà không làm mất active search/tree version.

## Definition of Done

- Upload/version/job idempotent; canonical blocks, dedup và citations giữ provenance/version.
- SEARCH_READY chỉ sau index nhất quán; knowledge enrichment không chặn tìm kiếm.
- Hybrid retrieval và tree-guided retrieval có regression evidence, citation và escape path.
- PostgreSQL giữ source of truth; Qdrant rebuild được.
- ACL được enforce trên retrieval và routing; leakage/blackhole được đo.
- Tree/incremental publish có quality gates, snapshot, manifest verification và rollback.
- LLM trả lời grounded hoặc nêu thiếu evidence; Laya lỗi không làm knowledge query thất bại.
- UI phân biệt processing, SEARCH_READY, KNOWLEDGE_READY, FAILED; trace/metrics đủ chẩn đoán.
- Gap/Research Agent có provenance, ngân sách và giới hạn; regression, integration và failure checks đạt.
- Cache không vượt ACL/version boundary; p50/p95/p99 và failure recovery có runbook.

Checklist tác nghiệp nằm ở [todo.md](todo.md); checklist chỉ được đánh dấu sau khi có evidence trên implementation/corpus tương ứng.
