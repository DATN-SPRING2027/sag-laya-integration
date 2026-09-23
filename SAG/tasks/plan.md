# Kế hoạch triển khai SAG Knowledge Routing RAG

Nguồn chuẩn: [Workflow v1.1](../docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md). Kế hoạch này chuyển các phase và Definition of Done trong đặc tả thành thứ tự triển khai có phụ thuộc và cổng nghiệm thu. Đây là kế hoạch mục tiêu; không coi các phase là tính năng đã có. Trước mỗi phase, đối chiếu code, schema, API và cách vận hành hiện tại rồi mới chọn nơi sửa.

## Mục tiêu và nguyên tắc

- Đạt luồng truy vấn tài liệu dùng được sau ingestion và hybrid search; knowledge enrichment/tree là nhánh bất đồng bộ, không chặn tìm kiếm.
- PostgreSQL là source of truth cho metadata, provenance, version, ACL, job state và tree lineage. Qdrant giữ index tăng tốc và phải dựng lại được từ dữ liệu chuẩn.
- SEARCH_READY nghĩa là canonicalization, dedup và search index đã nhất quán. KNOWLEDGE_READY chỉ đạt sau khi knowledge extraction và tree assignment cần thiết hoàn tất.
- Laya chỉ cho coarse intent. Query features và strategy được xác định theo contract; Laya lỗi hoặc mơ hồ phải fallback an toàn.
- Tree là routing prior, luôn giữ global escape retrieval; Qdrant search phải áp ACL chính xác.
- Retry, reprocess, rebuild và publish phải idempotent, có version/manifest, có thể truy nguyên và rollback.

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

**Checkpoint A — SEARCH_READY end-to-end:** upload → extract → dedup → index → global hybrid retrieval → context/citation hoạt động; có failure path; có thể tắt/trễ toàn bộ knowledge enrichment mà vẫn hỏi được tài liệu.

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
