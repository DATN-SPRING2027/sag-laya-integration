# Research — Checkpoint A: Global hybrid retrieval từ SEARCH_READY index

- Owner: KeyT / Thang.
- Ngày nghiên cứu/cập nhật: 2026-10-02.
- Trạng thái: research, implementation và một lượt review đã hoàn thành trên task branch; các acceptance end-to-end và owner/staging gates **chưa hoàn tất**.
- Baseline đọc code: `6035130562cb763c9effa3d0115e9b800c11d308` trên `origin/main`, sau PR #13.
- Checkout nghiên cứu: `sag-laya-main-sync`; nhánh `feat/Thang-checkpoint-a-search-ready-be-api`.
- Folder này là nơi Thang yêu cầu lưu hồ sơ. Checkout `sag-laya-integration` đang có thay đổi ACL chưa commit trên nhánh cũ; không dùng các thay đổi đó làm baseline và không sửa chúng.
- Nguồn chuẩn: `SAG/tasks/plan.md` (Phase 4 / Checkpoint A), `SAG/tasks/todo.md`, và `[SAG][Checkpoint A].md` trong folder này. DATN-29/DATN-30 cung cấp nền retrieval/ACL/evidence; DATN-33 sở hữu readiness/worker/index producer.
- Danh sách công việc và gate: [todo.md](todo.md).
- Kết quả implementation, code-review finding/fix, lệnh kiểm tra và môi trường: [researchtask.md](researchtask.md).

## 1. Kết luận và mục tiêu

Hiện index Phase 2C và đường đọc Search chưa nối end-to-end. Producer ghi SearchUnits vào Qdrant theo Project, nhưng global Search vẫn gọi legacy EngineManager; `search_context` còn gọi event/graph. Evidence resolver nhận diện SearchUnit nhưng chỉ khi ID legacy hit tình cờ khớp unit và version đạt trạng thái mong đợi. Citation click vẫn đọc SourceChunk legacy.

Deliverable của task: một đường đọc canonical SearchUnit dùng chung cho `/search`, `/search/stream`, `search_context` và mở citation; quyền được áp dụng trước candidate generation, evidence có provenance được kiểm chứng, context theo budget hiện tại, và không phụ thuộc enrichment. Không thiết kế lại fusion hay triển khai worker/readiness thay DATN-33.

**Các quyết định trong tài liệu là phương án của lane retrieval. Contract liên lane bên dưới chưa được owner DATN-33/security xác nhận. Không coi việc ghi kế hoạch là đã thống nhất shared files.**

## 2. Baseline flow trước implementation và nguồn trong code

Các đường dẫn trong phần này tương đối với `SAG/apps/api/sag_api/`, trừ khi ghi khác.

| Luồng | Code hiện tại | Kết quả / gap |
|---|---|---|
| Upload Project | `api/v1/documents.py`, `services/document_service.py::get_or_create_project_source` | Tạo Source có `is_project_source`, tenant/project config. Không tạo mapping CONFIRMED. |
| Phase 2C producer | `services/search_index_service.py::run_search_indexing_stage`, `index_search_units_to_qdrant` | Persist SearchUnit, ghi dense/sparse, kiểm count/checksum, ghi StageRun metrics. |
| Readiness | `search_index_service.py`, `jobs/tasks.py` | Stage index thành công ghi `search_status=READY`; worker cuối ghi `SEARCH_READY` dựa legacy chunk_count. Index đã tốt vẫn có thể đợi hoặc bị ảnh hưởng bởi enrichment. |
| ACL candidate scope | `services/source_service.py::_authorized_source_statement`, `search_source_candidates` | Project assertion giao CONFIRMED SourceProjectMapping theo org. Selector loại Source có `is_project_source`, kể cả khi mapped. |
| Global Search | `api/v1/search.py::_prepare_global_search` → `services/retrieval_service.py::retrieve_relevant_sections` | Dense `EngineManager.search_many` + lexical `grep_chunks`; chưa query `search_units_{project_id}`. CHAT confidence cao bỏ qua retrieval. |
| Tool | `tools/builtin.py::SearchContextTool.invoke` | Retrieval song song event recall, sau đó graph/prioritize event evidence; còn dependency enrichment. |
| Evidence | `services/evidence_service.py::resolve_traceable_evidence`, evidence pack | Join SearchUnit/version/document/start block; yêu cầu active document READY và version SEARCH_READY. Chưa là reader canonical. |
| LLM / citation | `generation/prompt.py::build_citations`; `api/v1/search.py::_validated_answer`; `services/agent_service.py::_finalize_answer_citations` | Citation ID theo pack; syntax/provenance kiểm được, chưa chứng minh entailment từng claim. |
| Click | `api/v1/sources.py::get_chunk` → `sag/engine_manager.py::get_chunk` | Đọc SourceChunk legacy. Unit ID canonical hiện có thể 404. |

## 3. Existing contracts và bảng field thực tế

### 3.1 Producer → Qdrant → PostgreSQL

Collection hiện tại: `search_units_{project_id}`. Vector dense `content_vector` dùng Cosine; sparse `bm25_sparse` dùng modifier `idf`. Xem `ensure_qdrant_collection_and_indexes`, `build_qdrant_payload` trong `services/search_index_service.py`.

| Field | Nguồn thật hiện tại | Quy tắc cho reader |
|---|---|---|
| `search_unit_id` | Payload `_sag_id` / `search_unit_id`; PG `SearchUnit.id` | Đối chiếu PG; không dùng Qdrant point ID thay unit ID. |
| Qdrant point ID | `generate_search_unit_point_id(collection_name, unit.id)` UUID5 | Chỉ dùng nội bộ để query/read exact point. |
| `document_version_id` | Payload và `SearchUnit.document_version_id` | Phải khớp cùng unit và version eligible trong PG. |
| `document_id` | `DocumentVersion.document_id` | Không có trong payload; join version → document. |
| `source_id` | `Document.source_id` → Source và CONFIRMED mapping | Payload không chứa; join PG, xác nhận scope. Source config không cấp quyền. |
| `project_id`, `tenant_id` | Payload; Document / IngestionRun | Kiểm tính nhất quán với trusted scope. Không tự coi orgId là tenant_id. |
| `security_partition_id` | Payload và `SearchUnit.security_partition_id` | Cần contract authority của partition; không nhận grant từ request/payload. |
| `content` | Payload `content`, producer lấy transient `unit._text_content` | PG SearchUnit không persist text. Kiểm SHA256 UTF-8 khớp `SearchUnit.content_hash`. |
| `content_hash` | PG SearchUnit + payload; producer SHA256 | Không tin chỉ payload tự xác nhận payload. |
| `block_from_id`, `block_to_id` | PG SearchUnit | Không có trong payload. Join cả hai block, cùng version, ordinal đúng thứ tự. |
| `page_from`, `page_to` | Payload và PG SearchUnit / CanonicalBlock | PG là nguồn locator sau đối chiếu; không bịa page khi producer không có. |
| `section_path` | Payload và PG SearchUnit | Truyền qua evidence/citation, có thể không có giá trị. |
| `canonical anchor` | `CanonicalBlock.source_anchor` | Hiện resolver lấy block đầu. Giữ anchor gốc; range dùng block IDs riêng. |
| `token_count` | Payload và PG SearchUnit | Producer đếm words; không dùng như tokenizer LLM để bảo đảm budget. |
| `score` | Qdrant dense/sparse hit; legacy DTO score / fusion score | Ghi rõ channel. Không cộng raw scores và không coi RRF là probability. |
| `rank` | Vị trí hit trong từng channel; rank sau `rerank_sections` | Tính ổn định với tie-break unit ID; không phải provenance/ACL. |
| `valid_from/to`, `*_ts` | Payload, từ version producer | Chốt temporal/current-version policy; không tự lọc ngày theo suy đoán. |
| Readiness / manifest | `DocumentVersion.search_status`, `search_ready_at`; `StageRun.metrics_json` của INDEX_SEARCH qua IngestionRun | Xác định current successful attempt, verified manifest, trạng thái hợp lệ. Không chỉ nhìn count > 0. |
| ACL metadata | Signed principal + SourceProjectMapping CONFIRMED theo org/project | Cấu trúc runtime phía server. Payload tree/node fields không phải ACL grants. |

Payload indexes hiện gồm tenant/project/partition/version/time/node; không có source_id và search_unit_id index. Không cần thêm source_id payload để làm đúng ACL: reader có thể lấy eligible version IDs từ PG và filter version trước top-k. Nếu quy mô allowlist vượt giới hạn cần đo và phối hợp owner index, không tự thêm schema/index trong task này.

### 3.2 Principal và Source contract cần xác nhận

Có **hai** principal khác nhau:

- Upload Project: `core/identity.py::VerifiedPrincipal` với tenant, allowed_projects và allowed_partitions; dependency `core/deps.py::get_verified_principal`.
- Search/agent ACL: `core/principal_assertion.py::VerifiedPrincipal` với organization_id và allowed_project_ids từ signed assertion; không có tenant_id/allowed_partitions.

Không có bằng chứng contract cho phép `organization_id == tenant_id`. Không mở rộng assertion bằng alias không được phép, không tin `Source.config` để xác nhận mapping. Source tạo khi upload Project chưa có CONFIRMED mapping và selector hiện loại project Source. Đây là gap thực tế của happy path upload → search.

Phương án: quyền source vẫn lấy từ resolver signed assertion hiện có; thêm selector canonical cho Source Project đã CONFIRMED trong phạm vi global canonical, không mở rộng legacy selector toàn hệ thống. Việc tạo/duyệt/backfill mapping thuộc owner mapping riêng. Tenant/partition phải có quyết định trusted inheritance hoặc grant rõ ràng trước acceptance; thiếu authority thì fail closed.

## 4. Contract đề xuất gửi DATN-33 / security

| Nội dung cần xác nhận | Phương án reader | Chủ thể / ảnh hưởng |
|---|---|---|
| Ready state | Một search-ready contract sau INDEX_SEARCH verified, độc lập knowledge_status/legacy chunk_count. Không chấp nhận READY và SEARCH_READY vô điều kiện. | DATN-33 sửa producer/status nếu cần; lane này chỉ consume. |
| Manifest/current attempt | Reader xác định đúng attempt hiện hành: stage SUCCESS, manifest_verified, collection/version/count/checksum nhất quán; retry/failure không được tái dùng success cũ. | DATN-33 chốt lifecycle; chưa có model manifest riêng. |
| Empty verified version | Zero SearchUnit + verified zero index là valid empty, trả no-answer. | Hai lane cùng xác nhận. |
| Model identity | Query dùng đúng embedder/model/dimension đã index cho collection; policy khi nhiều Source cùng Project khác model. | Producer hiện không thể hiện identity đầy đủ trong manifest; không đoán từ dimension. |
| Sparse encoder | Cùng regex/lower/hash/collision policy; query weights được xác nhận với collection IDF. | Không sửa producer BM25 trong reader PR. |
| Tenant/partition authority | Ánh xạ org → tenant và partition access phải có trusted contract, không lấy từ payload làm grant. | BE/security + DATN-33. |
| Project Source ACL | Upload-derived Source cần CONFIRMED mapping trước searchable; selector canonical chỉ chọn mapped Source. | Mapping owner; không auto-confirm/backfill. |
| Unit content/locator | Exact content đọc indexed payload và hash-check PG; split unit dùng point content, giữ parent anchor + unit ID + block range. | Không reconstruct split unit bằng whole block. |
| Current/historical version | Chốt active-version/validity policy cho search và citation. Mở citation cũ vẫn phải auth và đúng policy. | Product/DATN-33, không ngầm chọn version mới nhất. |
| Retry/delete race | Readiness/scope kiểm lại trước emit/pack và click; không dùng snapshot đã revoked. | Owner lifecycle; reader fail closed. |

Các mục này là điều kiện nghiệm thu kỹ thuật, không khẳng định cần deploy production trước khi phát triển phần reader. Adapter/payload parsing/test scaffolding có thể làm song song; chưa có đồng thuận thì không viết policy giả để pass test.

## 5. Evidence model và thuật toán retrieval

### 5.1 Model tối thiểu

Giữ DTO/pack hiện tại, bổ sung additive nếu cần: `search_unit_id`, `block_from_id`, `block_to_id`, `section_path`; `chunk_id` là compatibility alias của SearchUnit ID ở canonical path. Source ID public phải là SAG Source đã auth, không legacy data_source_id. Doc/version/page/anchor dùng fields hiện có.

Provenance nội bộ gồm collection/point ID/content hash/channel ranks/verified index attempt; chỉ expose dữ liệu cần dùng và không expose grants, token, endpoint/key. Unit thiếu required identity hoặc content hash mismatch bị loại; page/anchor không có ở nguồn thì không tự sinh locator có vẻ hợp lệ. Citation phải mô tả đúng mức locator thực có; gate click bằng unit/version vẫn phải đạt.

### 5.2 Ordered algorithm

1. Giữ routing CHAT/QA hiện tại. P4 không tự đổi ambiguous thành clarification.
2. Resolve signed principal và CONFIRMED Source scope, giao client source_ids (nếu có). Lấy Project/version/partition eligible từ PG theo contract đã xác nhận; không enumerate collections từ client input.
3. Scope rỗng → empty/no-answer; không gọi Qdrant. Chốt bound project/source/candidate fan-out và trace truncation; không silently claim đã tìm mọi Source khi bị cap.
4. Theo từng authorized Project, generate query vector bằng indexing-compatible embedder và sparse terms bằng tokenizer/hash contract. Kiểm vector dimension/finite values và model identity; không gọi ingestion để tạo index khi search.
5. Query dense và sparse độc lập vào cùng collection, **cùng filter trước limit**: project, tenant và partition theo trusted policy, eligible document_version_ids và validity nếu contract yêu cầu. Không global top-k rồi mới ACL filter. Hai query độc lập giúp regression chứng minh cả channel được scope.
6. Hydrate hits qua PG theo batch. Kiểm unit/version/document/source, both block endpoints, project/tenant/partition, point identity và SHA256(content). Không dùng payload làm authority. Recheck lifecycle/scope trước pack/emit/click.
7. Reuse RRF/rank fusion hiện tại: dedupe bằng canonical unit identity, không cộng raw dense/sparse, tie-break ổn định. Không thêm Qdrant server fusion rồi fuse lần nữa. Giữ semantic/lexical channel metadata và relevance policy đã có; không tự normalize confidence bằng RRF threshold.
8. Cross-project raw scores chỉ so sánh khi model/score contract tương thích. Nếu không, chốt cách ghép channel ranks có kiểm thử trước triển khai; không âm thầm đổi RRF weighting/fusion. Đây là GAP cần quyết định, không mặc định cosine khác model so được.
9. Dùng evidence pack/token budget hiện tại; map citation chỉ từ units thật đã vào serialized context. Search và tool gọi chung reader; canonical path không gọi graph/event/tree hoặc fallback legacy khi index lỗi.

Sparse hiện dùng regex `\w+`, lower, MD5-derived bucket `% 1000000`, sorted unique indices và collision handling. Producer tính BM25 document weights từ corpus từng batch rồi collection còn bật IDF; không mặc định gọi encoder document với defaults là query encoder đúng. Proposed query term weighting phải được owner xác nhận/fixture relevance kiểm tra; ranking calibration không thuộc task này.

## 6. Token budget algorithm

Giữ `services/evidence_service.py` và `generation/prompt.py::estimate_tokens`: hiện là estimator (CJK theo ký tự, phần còn lại xấp xỉ chars/4), **không phải tokenizer model chính xác**. Context window từ `settings.llm_context_window` với provider defaults/config; reserved output từ `llm_max_tokens`. Không dùng hằng token mới thay 12,000 chars.

Budget evidence = max(0, context_window − reserved_output − estimate(serialized system/query/history/tool schemas/other overhead)). Search tính system/query; agent dùng remaining budget sau history/tools và `_fit_agent_context` cho toàn lượt. Unit.token_count của index không thay estimator này.

Thêm evidence nguyên unit theo ranking/coverage hiện có, tính cả citation labels, metadata và separators trong serialized context. Fit xong mới gán/rebuild citation IDs. Nếu agent tiếp tục prune context, citation map phải theo phần còn visible. Budget không đủ cho unit hợp lệ → weak/no-answer, không cắt bỏ provenance để nhét text. Config invalid hoặc overhead vượt window phải có regression; token estimate gap giữ ghi rõ, không tuyên bố strict provider tokenizer guarantee.

## 7. Citation provenance và click algorithm

Chuỗi bắt buộc:

`authorized ready snapshot → Qdrant hit → PG SearchUnit/version/document/source + blocks → hash-verified evidence → packed context [n] → LLM [n] → validated final citation → authorized exact unit content`.

1. Bổ sung fields explicit unit/block range/section vào DTO, `SectionOut`/`SearchCitationOut`, tool result/citation builder khi cần. Schema hiện lọc mất phần provenance từ section; additive changes tránh phá consumer.
2. Map [n] chỉ chứa evidence còn trong context; duplicate/unknown/out-of-range ID không được thành citation. Với query factual cần evidence mà output không có valid visible citation, dùng policy no-answer hiện có.
3. Syntax validation (regex/bounds) khác provenance validation (ID thực trong context, source/version/unit/hash/locator khớp). Không gọi syntax validator là factual verification. Chưa có entailment verifier: remaining gap.
4. Giữ URL FE `/api/v1/sources/{sourceId}/chunks/{chunkId}` nếu tương thích: canonical SearchUnit.id qua chunkId, BE resolve canonical trước bằng source guard + same eligible-version policy, đọc deterministic indexed point và hash-check. Không trả legacy chunk fallback cho canonical unit biết là unauthorized/unready.
5. Click trả content đúng unit, heading/section, source/doc/version/pages/anchor/block range. Whole canonical block không thay exact split unit content. Parent block anchor không đồng nghĩa có sub-block offset chính xác; UI có thể mở unit excerpt và parent anchor, ghi gap precision.
6. Legacy ID thực vẫn giữ route cũ theo auth hiện tại. Kiểm Source khác, version bị revoke/delete/retry, unit/payload mismatch: fail closed; không leak phân biệt nguồn private qua error text.

FE hiện `citation-block.tsx`/`conversation-panel.tsx` mở source+chunk và `detail-panel.tsx` gọi getChunk. Có thể giữ FE code nếu additive response đủ; nếu phải thay navigation/render version/anchor, lập deliverable FE/PR riêng, không tự trộn vào nhánh BE.

## 8. EMPTY / WEAK / SUFFICIENT và lỗi hạ tầng

| State | Điều kiện | Hành vi |
|---|---|---|
| EMPTY | Không eligible scope/version hoặc canonical query thành công không có evidence usable | no-answer cho factual; không gọi LLM bịa; CHAT greeting giữ routing. |
| WEAK | Có hits nhưng provenance/content/required identifier coverage không đủ, hoặc budget không giữ được evidence cần thiết | Reuse structural pack policy; no-answer. Không lấy RRF cutoff làm confidence. |
| SUFFICIENT | Pack có usable traceable evidence, đáp ứng structural coverage và budget | Có thể generate grounded answer, citation chỉ trong context; chưa bảo đảm semantic answerability. |
| INDEX_UNAVAILABLE / ERROR | Timeout, missing expected collection/vector, query/response lỗi, stale/mismatch manifest | Error contract rõ, sanitized, retry bounded cho lỗi transient. Không giả empty/success, không fallback graph/legacy. |

GAP: chưa có calibrated answerability signal; high relative semantic score có thể đến từ toàn bộ kết quả không liên quan. Policy an toàn trong scope là giữ các điều kiện structural/exact coverage hiện có, không nới gate, prompt abstain và no-answer nếu thiếu valid citation; cần negative semantic corpus để phát hiện hạn chế. Không tuyên bố nó giải quyết mọi weak semantic query.

Không đổi P3 routing: ambiguous query vẫn đi theo router/agent contract. Test chứng minh khi route QA thì evidence safety được áp dụng, khi route khác thì không ép clarification mới.

Provider failure khác empty evidence: nếu existing contract dùng extractive fallback từ verified pack, phải bảo đảm không tạo claim mới và citations đúng. Chốt response status của fallback bằng regression trước thay đổi; không tự gọi mọi provider error là answered hoặc mọi error là no evidence.

## 9. API / stream / error changes

- `/search`, `/search/stream`, `search_context` dùng canonical reader chung cho global scope; scope/routing legacy ngoài target giữ contract.
- SSE giữ result → summary.delta → completed như hiện tại; provider deltas không được emit trước validation. No-answer terminal và citation map phải khớp non-stream/tool.
- Internal Qdrant/embedding exception phải translate sang stable code/message. Không đưa `str(error)`, response body, endpoint chứa credentials hoặc headers vào API/SSE/tool trace. Rà nhánh eval compare nếu dùng chung reader vì hiện có đường trả error text.
- Trace tối thiểu: channel/candidate counts, rejected reason counts, readiness gate, latency, budget và evidence state; không chứa principal assertion, bearer, API key hay tài liệu unauthorized.
- Retry có bound/deadline theo existing config; không hardcode vòng retry mới tùy tiện. 4xx contract/auth/hash mismatch không retry như transient; exhausted retry trả index error rõ.
- Partial channel failure: mặc định không claim hybrid success đầy đủ. Có thể degrade chỉ khi có explicit policy/trace/test đồng thuận; chưa quyết định thì trả sanitized error.

## 10. Test matrix cần thực hiện

| Nhóm | Regression / bằng chứng |
|---|---|
| Vertical happy path | Actual upload API → worker Phase2C với parser/embedder doubles → verified manifest → producer-written Qdrant fixture → global Search/SSE/tool đọc đúng units; không seed readiness hoặc matching legacy chunks để giả end-to-end. |
| Qdrant semantics | Stateful HTTP transport kiểm filter/using/limit cho cả dense/sparse, named vectors, empty sparse, collision; thêm real Qdrant smoke cho filter/top-k/IDF/version tương thích. Mock không chứng minh engine ANN thực. |
| ACL | Cross-org/project/source/partition; invalid/absent assertion; client source_ids narrowing; unauthorized high-score point bị chặn trước top-k ở cả channel; mapped project Source, unmapped/pending/revoked Source. |
| Lifecycle | Unready/index failed/version retry stale points/delete/revocation; latest/current attempt xác định đúng; verified zero units; không lấy success cũ làm readiness. |
| Fusion/relevance | Dense/sparse scales dương không đổi ranking gate hiện tại; deterministic ties/dedupe; exact identifier/path; cross-project model compatibility; không RRF confidence cutoff. |
| Evidence/budget | Hash mismatch, missing unit/version/blocks, reordered block endpoints, multi-block & split block, CJK/long query/history/tool overhead, zero remaining budget, provider output reservation. |
| Citation/click | Source/doc/version/unit/block range/page/section/anchor nhất quán; unknown/removed/out-of-range citation; click exact unit; other Source/revoked/mismatched unit fail closed. |
| Safety/routes | Greeting, factual, exact identifier, ambiguous, irrelevant/no evidence; streaming không emit unsupported draft; tool agent final citation chỉ visible context. |
| Enrichment independence | Tree/event/graph off/lag/failure; mocks raise nếu gọi; verified canonical Search vẫn hoạt động độc lập knowledge status theo owner readiness contract. |
| Errors/retry | Timeout/5xx then recovery, exhausted retries, collection missing, malformed payload, embedding model mismatch; error/trace không echo synthetic secrets. |

Tests sẵn có: `tests/test_phase_2c_search_indexing.py`, `test_phase_2_worker_execution.py`, `test_search_stream.py`, `test_retrieval_relevance.py`, `test_agentic.py` và agent tool/ACL suites. Các seed SearchUnit + SEARCH_READY với fake legacy hits hiện tại không chứng minh Search đọc index producer thật. Đề xuất thêm `tests/test_checkpoint_a_search_ready.py` và fixture transport dùng chung; không sửa worker tests của owner để hợp thức hóa contract sai.

## 11. File-by-file changes và trạng thái thực tế

| File / module | Thay đổi thực tế | Trạng thái |
|---|---|---|
| `sag/search_unit_store.py` (mới) | Read-only Qdrant dense/sparse/exact-point adapter, bounded retry/error sanitization | Đã implement; không sửa producer. |
| `services/search_unit_retrieval_service.py` (mới) | Authorized readiness/scope, chấp nhận `search_status=READY/SEARCH_READY`, batch hydration, hash/range validation, recheck lifecycle và orchestration | Đã implement; producer/owner gates vẫn mở. |
| `services/retrieval_service.py` | Không đổi legacy/global fusion module; canonical reader tái sử dụng semantics liên quan | Giữ ngoài diff để không thay retrieval/fusion chung. |
| `services/source_service.py`, `services/agent_domain.py` | Canonical Project Source candidate selection chỉ khi mapping CONFIRMED | Đã implement theo ACL runtime; không auto-map. |
| `services/evidence_service.py` | Provenance cho unit/range, chấp nhận trạng thái search `READY/SEARCH_READY`, hash validation, token pack và citations visible-context | Đã implement; tokenizer/answerability limitations còn mở. |
| `sag/dto.py`, `schemas/search.py`, `generation/prompt.py` | Additive unit/range/section fields, prompt citation locator | Đã implement. |
| `api/v1/search.py`, `api/v1/sources.py` | Shared canonical reader cho search/stream và exact-unit click resolver | Đã implement; real provider/staging smoke còn mở. |
| `tools/builtin.py`, `services/agent_service.py`, `tools/base.py` | `search_context` dùng reader chung, không gọi event/graph; giữ context fit/citation flow | Đã implement. |
| `core/principal_assertion.py` | Signed tenant/partition claims được validate; canonical retrieval fail closed khi thiếu | Đã implement; claim issuer/authority chưa được owner xác nhận. |
| `sag/engine_manager.py` | Quản lý pooled Qdrant HTTP client cho canonical search/citation; đóng client khi runtime đóng hoặc cấu hình đổi | Đã implement theo review comment; có regression reuse/reconfigure/close. |
| Tests mục 10 | Store/retrieval/ACL/traceability/stream/agent/index-worker regressions | Đã chạy focused suite: 161 passed, 4 warnings; fixture/mock không phải vertical provider E2E. |
| `SAG/tasks/plan.md`, `SAG/tasks/todo.md` | Cập nhật trạng thái code và tách rõ gate còn mở | Đã cập nhật; không đánh dấu toàn Checkpoint A hoàn tất. |
| Folder task này | Research, todo và kết quả review/validation | Đã cập nhật cho task hiện tại. |

Không thêm abstractions/cache/provider framework ngoài nhu cầu reader chung. Thay đổi hiện tại không chạm ingestion/index producer; xem working-tree diff để biết chính xác file implementation và regression.

## 12. Out-of-scope và impact

- Không sửa ingestion/parse/dedup/worker/index readiness producer, không đổi SearchUnit ID/hash generation, không sửa `jobs/tasks.py` hay index writer của DATN-33.
- Không thay global ACL principal verification/grants, tự confirm mapping, chạy migration/backfill hoặc sửa schema/index. Nếu cần DB change, tách DB branch/PR với owner và rollback.
- Không thiết kế lại RRF/DBSF/MMR, calibrated confidence, reranker, tree/KG/event recall, Checkpoint B/C.
- Không deploy, merge hoặc tự push. Chưa có production acceptance và chưa test real principals/data.
- Dự kiến BE changes không cần database migration; chỉ consume fields/metrics hiện có. Embedding identity/readiness nếu cần producer metadata bổ sung là deliverable owner, không giả là reader tự giải quyết.
- Config: tận dụng Qdrant/embedding/LLM settings; chỉ thêm setting khi có nhu cầu bound chứng minh được. Không lưu endpoint/key/assertion trong evidence records.
- Rollback dự kiến: revert reader/wiring PR; không xóa index/mapping. Legacy fallback không tự bật cho canonical query vì có thể phá ready/ACL contract.

## 13. Remaining gaps / acceptance checklist

### GAP cần owner xác nhận trước nghiệm thu

1. Retrieval/citation/evidence chấp nhận `search_status=READY` hoặc `SEARCH_READY` và không dựa vào `DocumentVersion.status`; owner vẫn cần xác nhận current-attempt/manifest và zero-unit readiness contract độc lập enrichment.
2. Mapping upload Project Source và selector project Source.
3. Hai principal contract: org/tenant/partition authority chưa thống nhất.
4. Current verified ingestion attempt, stale readiness khi retry, current/historical version policy.
5. Embedding identity theo collection và nhiều Source/project; cross-project channel rank policy.
6. Sparse document IDF + collection IDF/query weighting chưa là calibrated BM25 contract.
7. Exact split unit content có thể đọc point/hash; anchor offset trong parent block chưa được persist.
8. Token estimator chưa phải tokenizer model và structural sufficiency chưa phải answerability/entailment.
9. Real Qdrant/principal/staging smoke chưa thực hiện; mock success không chứng minh leakage/production readiness.

### Acceptance của lane retrieval/context/citation

- [ ] Contract producer/readiness/identity/scope được owner xác nhận, ghi decision và ngày; implementation hiện fail-closed khi thiếu tenant/partition claims.
- [ ] Upload → verified producer-written index → Search/SSE/search_context chạy E2E thật; hiện mới có PG fixtures và mock Qdrant transport.
- [x] Dense/sparse scope được áp dụng trước top-k, request chỉ narrowing, stale/unready/mismatch fail closed trong focused regressions; [ ] leakage/revocation staging với principal thật.
- [x] Search, stream và `search_context` dùng canonical reader chung; tests bảo đảm graph/event không nằm trên đường evidence; [ ] chạy với enrichment thật đang tắt/trễ/lỗi cùng index producer.
- [x] Context chỉ pack evidence đã authorize/hash-check trong estimator budget hiện có, tính overhead/output reserve; tokenizer chính xác theo provider/model vẫn là gap.
- [x] Citation provenance gồm source/document/version/unit/block range/page/section/anchor; click endpoint đọc exact unit và reauthorize trong service regressions; [ ] FE navigation/render split unit.
- [x] EMPTY và structural WEAK/exact-coverage thiếu trả no-answer; greeting và ambiguous routing được giữ nguyên trong tests; calibrated semantic answerability/entailment vẫn là gap.
- [x] Index failure tách khỏi empty, retry bounded, synthetic DB/index secrets không xuất hiện trong API/tool error regression.
- [x] Focused tests, Ruff, `git diff --check` và một lượt code review đã chạy; commands/results/limitations ghi ở [researchtask.md](researchtask.md).
- [x] Todo chuẩn chỉ đánh dấu phần đã chứng minh; Checkpoint A toàn hệ thống còn phụ thuộc gate DATN-33/security/staging.

## 14. Tài liệu ngoài repo đã đối chiếu

Tra cứu qua skill `find-docs` / Context7 ngày 2026-10-02, đối chiếu tài liệu chính thức Qdrant:

- [Qdrant search](https://qdrant.tech/documentation/search/search/): sparse query có indices/values và chọn named vector bằng using.
- [Qdrant indexing](https://qdrant.tech/documentation/manage-data/indexing/): payload indexes phục vụ filter/query planning; filter trên field và index field là hai vấn đề khác nhau.
- [Qdrant hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/): API hybrid tồn tại, nhưng lane này giữ RRF hiện có để tránh đổi fusion.

Tài liệu hiện hành không thay thế việc xác nhận version Qdrant được deploy, query encoder tương thích producer và kiểm chứng filter/IDF trên instance thật. Không đưa khuyến nghị API mới vào implementation chỉ vì latest docs hỗ trợ.
