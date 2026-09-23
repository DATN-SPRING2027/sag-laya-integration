# Todo — SAG Knowledge Routing RAG

Nguồn chuẩn: [Workflow v1.1](../docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md). Thứ tự và phụ thuộc: [plan.md](plan.md). Các mục dưới đây là việc cần làm, chưa hàm ý trạng thái hiện tại của code.

## Phase 0 — Contracts & Foundations

- [ ] Map flow upload, worker/job, search, query, schema, config và ACL hiện có; ghi rõ code tái sử dụng và gap.
- [ ] Chốt Document/Version/SourceSnapshot/IngestionRun, stable ID, idempotency key, provenance và temporal fields.
- [ ] Chốt stage/status/error contract và ý nghĩa riêng của SEARCH_READY, KNOWLEDGE_READY, FAILED.
- [ ] Chốt Laya, query features/planner, retrieval trace, index/tree manifest và config version contracts.
- [ ] Chốt tenant/project/security scope; rà migration, compatibility và rollback.

## Phase 1 — Upload & Versioned Source

- [ ] Giữ validation quyền, extension, MIME signature, size và policy trước khi xử lý file.
- [ ] Stream/checksum source; xác nhận source identity và duplicate policy.
- [ ] Tạo/liên kết Document, Version, SourceSnapshot, IngestionRun trong transaction.
- [ ] Kiểm tra bốn trường hợp cùng hash/cùng identity, cùng hash/khác nguồn, hash mới/cùng identity, hash mới/nguồn mới.
- [ ] Đảm bảo frontend retry dùng idempotency key, không tạo workflow trùng.
- [ ] Ghi stage/status/error cho worker; xác nhận status API và UI phản ánh tiến trình/lỗi/retry.

## Phase 2A — Canonical Extraction

- [ ] Xác nhận danh sách định dạng thực sự hỗ trợ và parser/fixtures cho từng định dạng.
- [ ] Lưu canonical block type, ordinal, page range, section path, anchor và document version.
- [ ] Kiểm tra normalization giữ bảng/code/punctuation/identifier và dấu vết boilerplate.
- [ ] Xác nhận LLM không được dùng để sửa text mặc định.
- [ ] Kiểm tra extraction output versioned/temp, retry và lỗi stage.

## Phase 2B — Dedup & Temporal

- [ ] Kiểm tra file exact hash và block exact hash; reuse content vẫn giữ mọi provenance/evidence.
- [ ] Kiểm tra near-duplicate tạo candidate cluster, có ngưỡng phù hợp loại dữ liệu.
- [ ] Kiểm tra semantic similarity chỉ tạo candidate, không tự merge CONTRADICTS/SUPERSEDES.
- [ ] Kiểm tra EQUIVALENT, SUPPORTS, CONTRADICTS, SUPERSEDES, RELATED và evidence mapping.
- [ ] Kiểm tra published/observed/ingested time, validity, supersedes lineage và truy vấn lịch sử.
- [ ] Xác nhận reprocess deterministic, retry an toàn và không mất lịch sử.

## Phase 2C — Search Index

- [ ] Xác nhận Search Unit boundary theo heading/paragraph/table trước token window.
- [ ] Giữ document/version, canonical block range, hash, page, section và security partition.
- [ ] Tạo dense+sparse representation theo provider/model đã xác nhận; giữ riêng Search Unit và Knowledge Unit.
- [ ] Tạo Qdrant payload indexes cho filter fields trước ingestion.
- [ ] Upsert bằng stable point ID; kiểm tra reprocess không tạo point/vector trùng hoặc evidence cũ.
- [ ] Lưu/verify index manifest; chỉ bật search readiness sau index nhất quán.
- [ ] Xác nhận Qdrant có thể rebuild từ PostgreSQL/source artifacts.

## Phase 3 — Laya & Query Analyzer

- [ ] Kiểm tra coarse intent CHAT/KNOWLEDGE/COMMAND/AMBIGUOUS và giữ query gốc/user scope.
- [ ] Chỉ bỏ retrieval với CHAT confidence cao; low-confidence/AMBIGUOUS vẫn fallback.
- [ ] Kiểm tra Laya lazy/singleton, timeout/init failure và fallback khi unavailable.
- [ ] Trích deterministic exact terms, identifiers, paths, entities, relation cues, temporal filters, global cues và multi-hop cues.
- [ ] Kiểm tra lỗi/nhãn Laya không xóa query hoặc context.

## Phase 4 — Retrieval Engine v1

- [ ] Chạy global hybrid search với scope/filter ACL, chưa phụ thuộc tree.
- [ ] Chọn RRF/DBSF hoặc calibrated fusion; không cộng trực tiếp raw dense/sparse scores khác scale.
- [ ] Collapse exact/near duplicate; kiểm tra MMR giữ evidence đa dạng.
- [ ] Giới hạn candidate rerank và chỉ rerank khi còn latency budget.
- [ ] Build context theo coverage/diversity và token budget.
- [ ] Citation map được về source/version/block/page/anchor; kiểm tra no-answer khi evidence thiếu.
- [ ] Xác nhận LLM Settings độc lập Laya và chỉ nhận evidence pack.
- [ ] Ghi stage latency, requested/effective strategy và fallback trong trace.
- [ ] Tạo/chạy retrieval regression corpus theo Phụ lục E của workflow.

## Checkpoint A — SEARCH_READY end-to-end

- [ ] Upload → extraction → dedup → index → global hybrid retrieval hoạt động.
- [ ] Citation trả ngược đúng source/version/page/anchor.
- [ ] Query vẫn hoạt động khi knowledge enrichment/tree bị tắt, trễ hoặc lỗi.
- [ ] Kiểm tra empty result, parse/index failure, retry và không lộ secret.

## Phase 5 — Knowledge Units & Graph

- [ ] Xây Knowledge Unit ổn định, tách khỏi Search Unit.
- [ ] Kiểm tra E0 deterministic, E1 model extraction và điều kiện đưa việc sang E2.
- [ ] Đưa E2 vào queue async có token/day budget, concurrency, backpressure và retry độc lập.
- [ ] Gắn evidence/provenance/confidence/validity cho entity, alias, claim, relation.
- [ ] Sinh graph candidates từ semantic, lexical, entity, structure, citation và temporal signals.
- [ ] Kiểm tra missing-signal renormalization, calibration [0,1], version config/quantile và sparse degree cap.
- [ ] Xác nhận lỗi/queue lag không chặn hoặc hạ SEARCH_READY.

## Phase 6 — Knowledge Routing Tree

- [ ] Build topology theo tenant/project và ACL-safe profiles theo security partition.
- [ ] Chạy Constrained Hierarchical Leiden; xử lý giant component bằng guard/fallback.
- [ ] Kiểm tra N_min/N_target/N_max, max children/depth, small-cluster repair và stop criteria.
- [ ] Tạo node prototype/profile: dense/medoid, sparse, entity, temporal, accessible count.
- [ ] Đo cohesion, giant ratio, child-size entropy, edge cut, depth và routing recall.
- [ ] Chỉ publish tree có manifest/lineage khi mọi quality gate đạt.
- [ ] Xác nhận summary/centroid/profile không trộn nội dung giữa ACL boundary.

## Phase 7 — Tree-guided Retrieval

- [ ] Planner map deterministic features sang primary strategy + modifiers; ghi planner version/reason codes.
- [ ] Regression EXACT, LOCAL_FACTUAL, ENTITY_RELATIONAL, TEMPORAL, GLOBAL_TOPIC, MULTI_HOP; xác nhận MULTI_HOP là escalation có căn cứ.
- [ ] Chụp một snapshot tree/search/ACL cho mỗi query.
- [ ] Prune node inaccessible trước beam; dùng ACL-safe profiles và exact ACL filter ở Qdrant.
- [ ] Kiểm tra beam/entropy/margin; giữ broad route khi tín hiệu không quyết định.
- [ ] Chạy branch-local retrieval và global escape theo budget; local rỗng/coverage thấp phải thử escape.
- [ ] Kiểm tra fusion → dedup → MMR → coverage → bounded graph expansion → rerank → context.
- [ ] Kiểm tra latency exhaustion có thể bỏ graph expansion/rerank mà vẫn trả fallback phù hợp.
- [ ] Trace đủ selected nodes/version, requested/effective strategy, reason codes và fallback/blackhole.
- [ ] Kiểm tra ACL leakage và routing blackhole bằng principal có quyền khác nhau.

## Checkpoint B — ROUTING_READY

- [ ] Tree ổn định và routing recall đạt ngưỡng benchmark.
- [ ] Escape retrieval cứu được query route sai/inaccessible.
- [ ] Tree không publish khi quality/ACL blackhole gate thất bại.

## Phase 8 — Incremental Tree

- [ ] Gán dữ liệu mới vào base + delta; cập nhật node/ancestor và drift signals.
- [ ] Chỉ rebuild subtree khi drift/quality gate yêu cầu; giữ stable node lineage.
- [ ] Build inactive routing slot và cập nhật Qdrant dual-slot payload.
- [ ] Verify manifest/checksum/quality trước khi đổi active pointer.
- [ ] Query trong lúc publish đọc một snapshot nhất quán.
- [ ] Inject/kiểm tra lỗi build và publish; active tree cũ vẫn phục vụ và rollback được.

## Checkpoint C — INCREMENTAL_READY

- [ ] Ingest bình thường không đòi full tree rebuild.
- [ ] Drift, subtree rebuild, publish, concurrent query và rollback đã kiểm chứng.

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
