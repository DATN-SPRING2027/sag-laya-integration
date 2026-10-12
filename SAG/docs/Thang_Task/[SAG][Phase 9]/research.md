# Research — [SAG][Phase 9] Knowledge quality metrics and evidence-based gap questions

- Trạng thái: Thang đã duyệt; implementation metric/generator v2 sau review và kiểm tra local đã hoàn tất.
- Ngày rà soát/triển khai: 2026-10-12.
- Repository: SAG, nhánh feat/Thang-sag-phase9-knowledge-quality-be-api-db.
- Base: origin/main tại 1f1f3df.

## 1. Cách hiểu yêu cầu

Phase 9 cần biến tín hiệu knowledge và routing/search thành các gap có thể giải thích, chấm điểm lặp lại được, rồi tạo câu hỏi nghiên cứu có giới hạn từ gap đã được xác định. Thứ tự bắt buộc:

1. Chụp đầu vào có scope và phiên bản.
2. Tính metrics theo node/topic bằng code xác định.
3. Chấm điểm gap bằng công thức/config đã version.
4. Lưu assessment, điểm, reason codes và bằng chứng.
5. Chỉ khi gap đạt điều kiện mới gọi LLM để diễn đạt câu hỏi. LLM không được quyết định có gap, thay điểm/evidence, tự mở research task hay gọi công cụ research.

Phạm vi chạm backend và lưu trữ dữ liệu; theo quy tắc workspace, nếu duyệt triển khai thì tách migration mới và không sửa migration cũ.

## 2. Hiện trạng đã xác minh trên origin/main

### 2.1 Knowledge units, evidence và tree đã được lưu

- SAG/apps/api/sag_api/db/models/knowledge.py định nghĩa KnowledgeUnit, KnowledgeEvidence, KnowledgeGraphBuild, KnowledgeUnitEdge và KnowledgeTreeNode.
- KnowledgeUnit giữ tenant/project/source/security partition, document version, checksums, trạng thái current và provenance/features. KnowledgeEvidence gắn evidence với unit, tier/kind/version/confidence và payload.
- KnowledgeConfig trong SAG/apps/api/sag_api/services/knowledge_service.py có version. Input provenance có source/document/version/partition và timestamp published_at, observed_at, ingested_at; evidence có quote/span/locator.
- Candidate builder trong SAG/apps/api/sag_api/services/knowledge_candidate_service.py dựng graph/routing snapshot, benchmark evidence và lưu candidate tree/profile có checksums. TreeManifest hiện có các chỉ số cấu trúc/routing như node/leaf counts, giant ratio, routing recall, escape win rate và ACL blackhole rate.

Các chỉ số tree/candidate hiện hữu đo cấu trúc hoặc routing; chưa phải bộ metrics chất lượng coverage/diversity/freshness/contradiction theo Phase 9. KnowledgeUnitEdge lưu tín hiệu graph similarity/structure; không phải nhãn mâu thuẫn đã được hiệu chuẩn.

### 2.2 Routing/search có tín hiệu, nhưng ý nghĩa và độ bền khác nhau

- query_routing_service.py tính entropy và margin từ điểm profile để quyết định route hẹp/rộng. Đây là độ mơ hồ định tuyến, không phải độ bất định của nội dung evidence.
- API search gắn query_route trace vào response stats; trace gồm scope/strategy/reason/fallback/query features. Không tìm thấy model SQL chuyên biệt đang lưu SearchTrace/QueryTrace hoặc aggregate demand.
- Candidate benchmark là tập query fixture cố định. Nó không đại diện cho tần suất truy vấn thực tế.
- Message lưu nội dung hội thoại; dùng raw message làm query demand sẽ tạo thêm rủi ro riêng tư, scope/ACL và retention. Không nên coi đó là nguồn demand mặc định.
- Knowledge evidence confidence và Laya confidence hiện chưa có bằng chứng calibration; không thể diễn giải trực tiếp thành xác suất đúng.
- Tìm thấy heuristic semantic signals cho support/contradict/supersede ở lane dedup/temporal, nhưng chưa có metric contradiction theo topic/node được xác nhận và hiệu chuẩn.
- MMR/retrieval coverage là kiểm tra coverage phục vụ chọn context/retrieval, không tạo ra mẫu số coverage topic/evidence cho Phase 9.

### 2.3 Kế hoạch và dependency

- SAG/tasks/plan.md, mục Phase 9, yêu cầu coverage, source diversity, freshness, contradiction, query demand, uncertainty, growth rate; gap priority dựa trên metrics có evidence/reason; scoring chạy trước LLM.
- SAG/tasks/todo.md, mục Phase 9, hiện có bốn checkbox chưa hoàn tất cho metrics, scoring/version/evidence, scoring trước LLM và giới hạn vai trò LLM.
- Jira đã kiểm tra ở ngày rà soát: DATN-62 (knowledge pipeline/tree persistence) Done; DATN-35 / Checkpoint B ROUTING_READY Done; DATN-67 (Phase 4 hybrid fusion/retrieval regression gate) vẫn In Progress. Vì task brief nói phụ thuộc Phase 4/7 metrics/trace, cần xác nhận Phase 9 dùng trace hiện có trong khi DATN-67 còn mở hay chờ ticket đóng. Code trace đã có không đồng nghĩa với production acceptance đã hoàn tất.
- Tài liệu evidence Phase 5/6 hiện có báo cáo kết quả trên corpus curated/fixture. Đây là evidence lịch sử hữu ích cho pipeline, không phải production-wide acceptance cho Phase 9.

## 3. Contract V1 đã duyệt; implementation v2 sau review

V1 dùng trực tiếp persisted tree, current KnowledgeUnits và evidence đã kiểm tra lineage; không yêu cầu taxonomy/facet catalog mới. Sau review, implementation bump metric definition/config lên `knowledge-quality-metrics-v2` / `knowledge-quality-v2` để đánh dấu missing demand window là unknown, đồng thời persist đầy đủ typed scorer input và xác thực checksum/lineage trước khi lưu. Scoring formula vẫn là `knowledge-gap-priority-v1`. Đây là local implementation evidence, chưa phải production acceptance.

### 3.1 Đầu vào reproducible

Mỗi lần scoring nhận snapshot xác định rõ:

- tenant_id, project_id, security_partition_id, tree_version, tree manifest checksum và node profile checksum;
- current KnowledgeUnit IDs, KnowledgeEvidence IDs/anchors, provenance timestamps và CONTRADICTS candidate relation IDs;
- node_id làm topic; coverage denominator là các current KnowledgeUnit thuộc node;
- cửa sổ demand hiện tại và cửa sổ liền trước cùng độ dài, daily aggregate counts;
- `as_of` timezone-aware, normalize về UTC; timestamp ưu tiên published_at → observed_at → ingested_at;
- config/version checksums và canonical input/assessment checksums.

Serialize canonical theo thứ tự ổn định. Không dùng đồng hồ chạy ngầm trong scoring. Cùng input/config/as_of cho cùng metrics, reason codes và priority.

### 3.2 Định nghĩa metric đã triển khai

| Metric | Định nghĩa implementation v2 |
|---|---|
| Coverage | Tỷ lệ current KnowledgeUnit trong node có ít nhất một evidence với quote và locator/anchor không rỗng. Đây là anchored-evidence coverage, không phải semantic answerability. |
| Source diversity | Normalized Shannon entropy của phân bố source_id trên các unit có anchored evidence; một source cho entropy 0. |
| Freshness | Trung bình `exp(-ln(2) × age_days / half_life_days)` trên các unit; half-life mặc định 90 ngày. Thiếu timestamp ở bất kỳ unit nào làm metric không đủ dữ liệu. |
| Contradiction | Cặp unit trong cùng node có persisted `KnowledgeGraphEdge.edge_type=CONTRADICTS`, map qua SearchUnit block range và có anchored evidence. Gọi là candidate/unresolved signal, không phải conflict đã xác minh. |
| Query demand | Số route decisions đã ACL-fingerprint kiểm tra theo selected node, ghi vào tenant/project/partition/tree/node/day aggregate; raw query/message không đi vào writer hoặc schema. Bucket thiếu ở một trong hai cửa sổ là unknown; không suy thành zero. |
| Demand growth | So sánh tổng count trong hai cửa sổ ngày liền nhau, cùng độ dài; tăng dương được chuẩn hoá và cap theo config. |
| Uncertainty | `1 - mean(evidence.confidence)` trên anchored evidence; nếu không có anchored evidence thì 1. Đây là proxy chưa calibration, không phải xác suất sai. |

Bucket demand vắng ở bất kỳ cửa sổ nào hoặc thiếu freshness làm priority unknown và không đủ điều kiện tạo question. Writer chỉ ghi node đã được route, nên không suy zero từ bucket vắng. Future timestamp được clamp tuổi về 0 và thêm reason code. Demand trước thời điểm rollout chưa được backfill; cần thận trọng với cửa sổ đầu tiên sau rollout.

### 3.3 Gap scoring V1

Chuẩn hoá các thành phần về [0,1]. Version/config checksum bao phủ trọng số, threshold và normalization:

- `severity = 0.30×(1−coverage) + 0.15×(1−source_diversity) + 0.15×(1−freshness) + 0.25×contradiction_candidate_rate + 0.15×uncertainty_proxy`.
- `importance = 0.80×query_demand + 0.20×demand_growth`.
- `priority = clamp(severity × importance, 0, 1)`, làm tròn 12 chữ số để checksum ổn định.
- Threshold mặc định: eligible priority ≥ 0.20; source-diversity-low < 0.50; stale freshness < 0.50; high uncertainty ≥ 0.50. Demand daily cap 50; growth cap 2; freshness half-life 90 ngày.
- Nếu freshness hoặc demand/growth bắt buộc unknown, severity/importance/priority không được suy đoán; `eligible=false`. Còn lại question chỉ đủ điều kiện khi priority đạt ngưỡng và có anchored supporting evidence.
- Mỗi điểm lưu metrics, config/input/result checksum, reason codes và evidence IDs/locators. Reason codes do deterministic rules sinh, không do LLM.

**Làm rõ với workflow:** phương trình mục 16.2 là mô hình mục tiêu khái quát. Contract V1 đã duyệt dùng weighted severity và combined demand/growth importance vì repo chưa có independent importance, freshness need theo domain hoặc uncertainty đã calibration. Các phiên bản metric/scoring hiện tại là proxy có version, không phải calibration cuối cùng.

### 3.4 Output/persistence và giới hạn LLM

Persistence additive đã triển khai:

- `knowledge_query_demand_daily`: scoped daily counts, không có raw query column.
- `knowledge_quality_assessments`: scope, as_of, versions/checksums, config và snapshot input.
- `knowledge_gaps`: node score, metrics, eligibility, reason codes, support evidence refs/checksum.
- `knowledge_gap_questions`: FK tới gap, text, generator/model/prompt versions, evidence IDs, reason codes và checksum.

`generate_question_for_gap` là explicit service call sau persist; không có endpoint/job tự động và không gọi khi gap không eligible hoặc evidence hiện hành/được phép không còn. Generator/prompt dùng version `knowledge-gap-question-v2` / `knowledge-gap-question-prompt-v2`. Assessment snapshot lưu toàn bộ typed scorer input và persistence xác nhận checksum, scope, unit/evidence lineage. Prompt gửi gap ID, node ID, priority, reason codes và tối đa 5 quote/locator (mỗi quote tối đa 500 ký tự); adapter không có tool parameter. Output phải là đúng một câu hỏi ≤320 ký tự và giữ một cụm ba từ liên tiếp (hoặc toàn bộ quote ngắn hơn) từ evidence được gửi. Đây là lexical grounding guard, không phải semantic entailment validator. Server gắn lại gap/evidence/reason linkage; không có research trigger.

## 4. Security, ACL và provenance

- Lọc tenant/project/security partition và quyền source trước khi tổng hợp metrics; không đưa evidence của partition khác vào entropy, coverage hay demand.
- Ghi rõ actor/scope/source set hoặc policy snapshot dùng cho assessment. Revalidate access khi đọc assessment/question; revoked source không được lộ qua evidence payload.
- Không lưu raw query hoặc raw message trong demand aggregate. Chốt retention và mức độ aggregate trước khi có production event writer.
- Câu hỏi gửi tới model chỉ chứa evidence tối thiểu đã được phép dùng; không đưa toàn văn tài liệu hoặc secret/config credentials.
- Assessment, gap, question cần lineage đến unit/evidence/source version, scorer/config/prompt version và checksum để audit/reproduce.

## 5. Files đã triển khai

Implementation nằm trong backend API; không thêm route/API public hoặc caller tự động:

- `SAG/apps/api/sag_api/services/knowledge_quality_service.py` — immutable input/config, deterministic metrics/scoring/reasons/checksums.
- `SAG/apps/api/sag_api/services/knowledge_quality_repository.py` — manifest/profile/current-unit/evidence/conflict collector theo tenant/project/partition/tree.
- `SAG/apps/api/sag_api/services/knowledge_quality_persistence.py` — atomic daily demand upsert, scoped windows, snapshot validation và assessment/gap persistence.
- `SAG/apps/api/sag_api/services/knowledge_gap_question_service.py` — explicit phrase-only LLM call, evidence revalidation, output validation và question persistence.
- `SAG/apps/api/sag_api/db/models/knowledge_quality.py`, `db/models/__init__.py`, `migrations/0004_knowledge_quality_gaps.sql` — 4 bảng additive; migration 0001–0003 giữ nguyên.
- `SAG/apps/api/sag_api/services/search_unit_retrieval_service.py` — fail-soft writer cho routed node demand sau ACL scope fingerprint validation.
- `SAG/apps/api/tests/test_knowledge_quality_service.py`, `test_knowledge_quality_persistence.py`, `test_knowledge_query_demand.py` — metric fixtures, persistence, ACL/lineage, question gating và query demand.
- `SAG/tasks/plan.md`, `SAG/tasks/todo.md` — plan và implementation evidence.

Assessment và question generation hiện chỉ có service interface nội bộ. Nếu sau này thêm API/job caller, caller đó cần authorization theo tenant/project/partition trước khi đọc hoặc phrase gap.

## 6. Verification đã chạy

| Lệnh/check | Kết quả |
|---|---|
| Focused Phase 9 + routing/foundation regressions | 77 passed, 2 skipped sau review fixes. |
| Ruff trên toàn bộ file Python thuộc Phase 9 đã đổi | All checks passed; command và phạm vi nằm trong `todo.md`. |
| Ruff toàn `sag_api/ sag_agent/ tests/` | Có lỗi lint nền ngoài phạm vi Phase 9; không sửa cleanup không liên quan. |
| Full API `pytest -q` | 955 passed, 3 skipped, 23 failed; lượt này khởi chạy trước thay đổi Phase 9 cuối và failures nằm ngoài file Phase 9; xem `todo.md`. |
| Apply `0004_knowledge_quality_gaps.sql` trên SQLite in-memory | Pass; tạo đủ bốn bảng. PostgreSQL live chưa được kiểm chứng. |

Fixtures cho thấy scoring checksum ổn định; thêm anchored evidence hoặc freshness cải thiện sẽ hạ priority; contradiction candidate và demand/growth cao làm priority tăng. Persistence kiểm tra assessment/gap idempotency, raw-query key/value rejection, full scorer-input checksum and lineage, collector partition/checksum/source-mapping isolation, missing demand window/freshness, source no-longer-current, no-call khi gap ineligible, unrelated/invalid output và provider error; question liên kết evidence/reasons và persist idempotently.

## 7. Ngoài phạm vi

- Tự động chạy research/search/task chỉ vì model gợi ý câu hỏi.
- Tái định nghĩa retrieval coverage/MMR, route entropy hoặc tree quality metrics thành knowledge quality mà không có semantics riêng.
- Đọc conversation Message/raw query làm demand mà chưa có privacy/retention contract.
- Tự suy diễn contradiction hoặc semantic answerability từ graph similarity, lexical negation hay uncalibrated confidence.
- Đóng các Jira dependency hoặc khẳng định live production acceptance dựa trên local fixtures.

## 8. Quyết định đã chốt khi duyệt nghiên cứu

1. Dùng KnowledgeUnit hiện hành trong node làm denominator coverage vì Phase 9 không có facet catalog chuẩn.
2. Query demand là daily scoped counts của routed selected nodes; không ghi raw query/message. Không backfill demand lịch sử.
3. Growth là demand growth giữa hai cửa sổ liền kề bằng nhau.
4. Contradiction chỉ lấy edge `CONTRADICTS` có evidence anchor; vẫn hiển thị là candidate/unresolved.
5. Confidence uncertainty được dùng như proxy uncalibrated; không diễn giải thành xác suất đúng.
6. Triển khai dựa trên persisted tree/current units và local fixture/regression evidence; DATN-67 Phase 4 vẫn In Progress, nên không tuyên bố production acceptance.
7. Question generation là explicit call sau scoring/persistence. Prompt chỉ nhận lý do và tối đa năm quote có giới hạn; không có research tools/trigger.

## 9. Giới hạn rollout và rollback

- `0004` thêm demand, assessment, gap và question tables. Rollback thủ công chỉ sau khi consumer dừng: drop theo thứ tự `knowledge_gap_questions`, `knowledge_gaps`, `knowledge_quality_assessments`, `knowledge_query_demand_daily`; thao tác này xóa toàn bộ dữ liệu Phase 9.
- Search writer ghi qua session riêng và fail-soft trong tối đa 100 ms. Trước migration, lỗi table thiếu được log và search vẫn trả kết quả; demand chỉ bắt đầu khi writer và migration được deploy.
- Không có live PostgreSQL/Qdrant test, production traffic check, authentication endpoint cho assessment/question, hay automatic Phase 10 integration trong task này.
- PR #26 là thay đổi Phase 9 đang chờ review/merge trên `main`. DATN-67 Phase 4 còn In Progress; local fixtures không đóng dependency hoặc checkpoint production.

## 10. PR #26 review follow-up

- Scoring, persistence và question generation tái sử dụng `knowledge_service.checksum`, giữ nguyên canonical serialization và checksum values; ba hàm trùng lặp đã được xóa.
- Retrieval khởi chạy demand write bằng task nền được giữ strong reference đến completion. Route scope/ACL fingerprint validation, session riêng, timeout 100 ms, logging và fail-soft behavior vẫn giữ nguyên. Đây là metric best-effort: process shutdown/crash có thể làm mất write đang chờ; bucket thiếu tiếp tục là unknown.
- Cold-start yêu cầu ít nhất một daily bucket của node trong cửa sổ demand hiện tại **và** cửa sổ liền trước có cùng độ dài. Trước khi có hai bucket-window quan sát được này, demand/growth unknown và không đủ điều kiện sinh question; không backfill.
- Evidence folder đã sửa từ `[SAG][Phase 9}` sang `[SAG][Phase 9]`; `tasks/plan.md` trỏ tới đường dẫn mới.
- Follow-up local checks: Ruff trên 5 file Python và Python 3.11 `py_compile` đều pass. Pytest chưa collection được do môi trường thiếu `zleap`; dùng `uv run --frozen --extra dev --python 3.11` cũng dừng lúc build `litellm` vì thiếu MSVC `link.exe`. Reviewer báo 26/26 Phase 9 tests và 27/27 routing/retrieval tests trước follow-up; chưa rerun các test đó trên thay đổi follow-up.
- Implementation commit: `c5ca0946fd83f13f3cbd153b2d49dbb854f5d63b` (`fix(sag): address Phase 9 review feedback`). Thay đổi follow-up không sửa schema, config hoặc public API. PR #26 vẫn chờ merge; production PostgreSQL/Qdrant và DATN-67 vẫn là giới hạn ngoài xác nhận local.
