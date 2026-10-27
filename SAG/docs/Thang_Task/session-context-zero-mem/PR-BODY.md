Bổ sung quản lý context cho **một thread chat** từ toàn bộ lịch sử được phép, giữ SAG và retrieval kho tri thức hiện có. Câu hỏi thường nhận evidence phù hợp trong budget; `full_session` đọc theo lô và tổng hợp với coverage ledger. Phạm vi nhớ không còn bị giới hạn ở 8 message gần nhất hoặc 200 message đã load.

**PR hợp nhất T1–T9, bao gồm toàn bộ code của #38, #39 và #40.** Theo yêu cầu trực tiếp của Thang, chỉ tiếp tục sửa/review trên PR này và nhánh `feat/Thang-session-context-runtime-be-api`. #38 và #39 được đóng vì đã được thay thế; **không cần merge hai PR đó trước**. Đây là ngoại lệ đã được người dùng cho phép cho việc gộp deliverable DB và API.

**Draft — NO-GO rollout. Giữ `SAG_SESSION_CONTEXT_MODE=off`.** Implementation và phép đo đã hoàn tất, nhưng nghiệm thu chất lượng chưa đạt. Không coi việc gộp PR là cho phép merge, triển khai hoặc bật feature.

## Kiểm tra không mất code

- Head #38 `5db4464` và head #39 `74971ba` đều là ancestor của head #40 trước hợp nhất `df0c904`.
- Schema/model/migration của #39 có nội dung giống hệt trong nhánh này; không có diff ở các đường dẫn DB/migrations.
- Giữ nguyên commit history; không cherry-pick lặp, rebase, squash hoặc force-push. Chỉ thêm cập nhật tài liệu bàn giao khi chuyển sang một PR.
- Các nhánh cũ được giữ để tra cứu; nhánh làm việc tiếp theo duy nhất là `feat/Thang-session-context-runtime-be-api`, PR target `main`.

## Phạm vi triển khai T1–T9

| Hạng mục | Nội dung |
|---|---|
| T1 — Baseline | Corpus synthetic Việt/Anh, calibration/holdout cố định; replay qua luồng ask/generation thật, usage/accounting và dữ liệu chuẩn. |
| T2 — Hợp đồng | Immutable snapshot, scope/partition, message refs/span/hash, Source dependencies, provenance bắc cầu, coverage và budget hữu hạn. Lịch sử thiếu authority không được đoán quyền. |
| T3a — Reader/hierarchy | Keyset pagination toàn lịch sử, kiểm missing/duplicate pages; temporal turn/window/episode/local views giữ nguyên văn và offsets. |
| T3b — Retrieval/fusion | Graph entity/context/adjacency, PPR, lexical+dense hierarchy, normalized fusion, graph/local support và atomic evidence closure; thiếu budget thì partial. |
| T4 — Calibration | Đo baseline/candidate, khóa caps và ngưỡng quality/token/time/calls trước holdout; giữ các báo cáo cũ và lỗi thay vì ghi đè kết quả. |
| T5 — Persistence | Hai bảng dẫn xuất, models, migration mới 0007 và down script; raw chat/owner không đổi. |
| T6 — Lifecycle | Index theo owner/thread/scope/model/hash, checkpoint và resume; generation CAS, refresh/revalidation, delete/revoke fences; backfill có xác thực, mặc định dry-run. |
| T7 — Normal runtime | Tích hợp thread ask, `off/shadow/on`, tokenizer fitter, inert evidence JSON, ID→quote gốc, native tool groups/citations, kiểm quyền trước input/output và persist provenance. |
| T8 — Full-session | Map/reduce có giới hạn calls/tokens/time, chỉ đánh dấu visited sau map hợp lệ; intermediate items giữ raw refs, role/order/time và support; partial/cancel dùng native runtime/SSE. |
| T9 — Verification | Offline/SDK/JWT/HTTP/SSE/lifecycle, PostgreSQL thật, browser/API/provider thật, calibration và holdout cố định; kết quả acceptance FAIL được công khai bên dưới. |

Không thay ingestion/retrieval tài liệu, không sửa DATN_BE/DATN_FE hoặc frontend source. Stateless OpenAI endpoint giữ đường hiện có; thay đổi memory áp dụng cho thread ask.

## Hành vi và API

- `SAG_SESSION_CONTEXT_MODE=off|shadow|on`, mặc định `off`. OFF dùng legacy generation; SHADOW xây/chọn context nhưng không thêm LLM trả phí ngoài legacy; ON dùng session context đã kiểm quyền.
- Thread ask nhận thêm optional `context_mode=normal|full_session`. Bỏ trống thì chỉ route full khi câu hỏi có ý toàn phiên rõ ràng; client không được tự cấp owner/project/Source/model/budget authority.
- Normal dùng graph/fusion chọn raw evidence, không tạo generative memory summary. Evidence luôn là dữ liệu lịch sử, không trở thành chỉ dẫn hoặc active assistant completion. Query hiện tại giữ đúng một lần.
- `supports` nhận ID evidence, backend lấy quote từ span gốc; quote được model cung cấp vẫn phải khớp nguyên văn. ID ngoài scope/ngoài batch/chưa map bị từ chối. Quote membership là provenance, không chứng minh semantic entailment.
- SSE terminal có thêm `session_context` và `session_accounting`; giữ output/citations/answer_status cũ. Nội dung JSON được buffer/validate trước khi đưa ra UI. Partial có cảnh báo bằng ngôn ngữ tự nhiên, không yêu cầu client hiểu event mới.
- Native tool call/result được fit nguyên nhóm; citations của tool result bị bỏ khỏi prompt cũng bị loại. Source đã dùng được kiểm lại trước dispatch/persist. Explicit Source filter vẫn đi qua retrieval/citation gate cũ.
- Full-session duyệt snapshot hợp lệ theo thứ tự, giữ dependency closure và overlap; không cắt intermediate items rồi tự nhận đầy đủ. Cancellation/failed/unknown usage không được coi là successful complete answer.
- ON hiện chỉ hỗ trợ text. Stateless endpoint, legacy assistant thiếu provenance và cross-session recall không được mở rộng ngoài hợp đồng đã ghi.

## Budget, models và accounting

- Profile memory-only normal: tối đa 1 model attempt; input 8192, output reserve 4096, total 12288 tokens; retrieval 5 giây, request 60 giây, index work 16M (context window nhỏ hơn tiếp tục được tôn trọng).
- Native tool-assisted normal: tối đa `min(7, agent_max_steps+1)` attempts, mỗi lượt input/output có reserve, total hữu hạn và chung deadline 60 giây. GetTime chỉ nâng profile khi cần câu hỏi thời gian.
- Cold index job: 180 giây/32M work units, checkpoint để resume. Full-session: tối đa 12 attempts/262144 tokens/360 giây; deadline bị giới hạn thêm bởi principal expiry.
- Qwen3-8B tokenizer/model route được xác minh, BGE-M3 và NER revisions pinned trong local cache. Runtime dùng OpenAI SDK retries 0, không follow HTTP redirects, JSON mode; unknown/cancelled usage không coi là 0 và chặn paid follow-up.
- NER runtime dùng OpenMed Vietnamese candidate với publisher Apache-2.0; BGE-M3 MIT. Davlan/NER của prototype và báo cáo T4 là checkpoint lịch sử, không phải model runtime mới. Training-data rights chưa được xác minh độc lập; giữ attribution, không redistribute weights trong repo.

## Database — đã nằm ngay trong PR này

- `session_context_indexes`: thread/owner/scope/model, generation/snapshot digest/state, checkpoint/features; là cache dựng lại được.
- `session_message_provenance`: Message/hash/owner/partition, Source IDs và dependency refs; origin mặc định unknown.
- FK cascade theo Message/Thread; không thay raw messages, owner authority hoặc migration cũ. Chưa chạy migration/backfill trên dữ liệu thật.
- Áp `0007_session_context.sql` sau 0006 **trước khi khởi động binary mới**. Bootstrap legacy `init_db` gọi `create_all` cả khi OFF; OFF không phải guard DDL. Nếu startup đã tạo bảng, phải kiểm schema/reconcile bằng quy trình migration có review, không chạy SQL up mù quáng.
- Runtime kiểm lại raw hash/scope/quyền, không tin index JSON như authority. Shared PostgreSQL locks và deadline bảo vệ input/publication; first-insert contention có thể fail closed, không claim distributed lease hoàn chỉnh.

## Kiểm chứng đã chạy

Các số sau là bằng chứng của bản triển khai trước cập nhật tài liệu hợp nhất; lần hợp nhất không đổi runtime và không chạy lại provider trả phí.

- Full offline: **304 PASS**, 5 native-PG opt-in SKIPPED trong lần full suite, 2 ai_smoke DESELECTED.
- Native PostgreSQL **16.15: 5 PASS / 18,08 giây**, chạy riêng trên container QA tạm: migration up/down/reapply giữ raw chat; hai kết nối xác nhận writer chờ row lock qua `pg_blocking_pids`; separate answer publication không deadlock; delete/invalidate/revoke được kiểm lại sau release. Container QA đã xóa, DB người dùng không bị đụng tới.
- Schema SQLite FK/lifecycle: 7 tests PASS; up/down cũng được chạy trên PGlite. SDK MockTransport xác minh one attempt với 400/429/500, không redirect 307/308, deadline/cumulative/unknown/cancel usage.
- JWT/current-user/Project owner thật + HTTP/SSE/router tests kiểm sai token 401, project khác 404, DTO enum sai 422; local inference/provider doubles được phân biệt rõ với live quality.
- Native tool pressure tests giữ current query/evidence frame, bỏ nguyên nhóm call/result cũ và citation tương ứng; Source revoke làm history/provenance bị ẩn đúng scope.
- Browser SAG thật + API/DB fixture riêng + provider thật: partial hiển thị live và sau reload, screenshots 390/768/1280/1440px, không có console exception/failed-request/HTTP error được ghi nhận. Có ảnh before/after của lỗi SSE envelope đã sửa; không mock HTTP hoặc sửa FE để che lỗi.
- Targeted Ruff PASS; full Ruff còn **123 findings legacy** (baseline 124), không báo full lint xanh. `uv pip check`: 159 packages tương thích; compileall PASS.

## Quality/cost gate — không đủ điều kiện rollout

| Candidate | Calibration | Holdout |
|---|---:|---:|
| Normal đúng | 16/20 | 16/20 |
| Full-session strict đúng | 1/4 | **0/4** |
| Lỗi thực thi | 2 | 0 |
| Khẳng định sai rằng quyết định vắng mặt | 3 | 4 |

- Calibration có hai lượt dựng index lạnh chạm 180 giây. Không cắt last-N hoặc coi 0 model calls của lượt lỗi là tiết kiệm thành công.
- Holdout chạy **một lần**, 48/48 paired replays, **67 model calls** có usage đầy đủ; source/config/caps/models/dependencies khớp calibration cuối. Không tune/rerun chọn đáp án tốt theo holdout.
- Có regression từng ca: baseline trả đúng nhưng candidate UNKNOWN. Kiểm tra xác nhận gold vẫn hiện trong fitted prompt, raw provider explicit UNKNOWN; không đổ lỗi cho decoder/fitter khi bằng chứng không bị mất.
- Full-session bỏ quyết định hoặc khẳng định không tồn tại dù traversal đủ. Coverage visited **không** là semantic recall. Partial prefix đúng vẫn không làm nội dung sai trở thành đạt chất lượng.
- Bốn long-normal cases/profile, kể cả đáp án xấu: token input+output giảm **73,81%** web-off (58594→15343), **77,99%** web-on (60163→13244). Token gate riêng đạt nhưng quality gate FAIL; không tuyên bố tiết kiệm tổng tiền.
- Full holdout báo riêng: legacy 6 calls/57849 tokens; candidate 17 calls/389164 tokens, chất lượng chưa đạt. CPU/storage chưa định giá, legacy SDK-internal retries/billing visibility và diagnostic crash checkpoints có giới hạn; không biến khoảng thiếu thành 0 chi phí.
- Giữ toàn bộ raw synthetic reports, source/answer hashes, semantic review và bad answers. Review semantic do assistant thực hiện, không phải blinded human acceptance.

## Rủi ro còn lại và công việc fix tiếp

1. Cải thiện semantic extraction/synthesis để không bỏ quyết định hoặc tự khẳng định vắng mặt; giữ phân biệt đề xuất assistant với xác nhận user.
2. Đóng regression abstention của normal mode và cold-index timeout trên workload dài; dùng validation mới, không tuning dựa trên holdout đã mở.
3. Kiểm chứng workload nhiều worker, production DataEngine/Qdrant và Continuum embed riêng. Lock evidence có thể làm delete/revoke chờ tới request deadline.
4. Giữ flag OFF đến khi quality gates đạt; kiểm license/attribution, triển khai model cache và ngân sách thực tế trước bật.

## Rollback và thứ tự triển khai

- Một PR/nhánh chứa cả DB + API; không còn dependency phải merge #38/#39.
- Khi được con người chấp thuận triển khai: review tác động DB, apply migration trước startup, xác minh schema/model cache; **không tự bật ON** khi T9 còn FAIL.
- Rollback: OFF, dừng jobs/drain requests, giữ raw chat. Chỉ chạy `0007_session_context.down.sql` sau khi readers dừng; nếu cần giữ bảng đã drop thì rollback binary trước restart để tránh `create_all` dựng lại bảng rỗng. Không phục hồi provenance bằng đoán authority cho assistant cũ.
- Không merge, approve hoặc bypass CI trong thao tác hợp nhất PR này.

## Tài liệu và evidence

Các file nằm tại `SAG/docs/Thang_Task/session-context-zero-mem/`:

- `SPEC.md`, `BASELINE.md`, `CONTRACT.md`: yêu cầu, baseline và hợp đồng.
- `READER-HIERARCHY.md`, `GRAPH-SELECTION.md`, `CALIBRATION.md`, `CALIBRATION-POLICY.json`: selector và ngưỡng đã khóa.
- `PERSISTENCE.md`, `MODEL-LICENSES.md`, `RUNTIME.md`: DB, license và vận hành runtime.
- `VERIFICATION.md`, `evidence/t9-calibration-runtime-v4.json`, `evidence/t9-holdout-runtime-v4.json`, semantic reviews và `evidence/browser/`: kết quả T9 và bằng chứng.
- `HANDOFF.md`, `researchtask.md`, `tasks/plan.md`, `tasks/todo.md`: nhánh làm việc duy nhất, lịch sử quyết định và các gate còn thiếu.

Liên kết báo cáo đã đo cố định: [VERIFICATION](https://github.com/DATN-SPRING2027/sag-laya-integration/blob/df0c904ec50e27cb1dd8a9caf5e628bfdf3ec658/SAG/docs/Thang_Task/session-context-zero-mem/VERIFICATION.md), [RUNTIME](https://github.com/DATN-SPRING2027/sag-laya-integration/blob/df0c904ec50e27cb1dd8a9caf5e628bfdf3ec658/SAG/docs/Thang_Task/session-context-zero-mem/RUNTIME.md), [PERSISTENCE](https://github.com/DATN-SPRING2027/sag-laya-integration/blob/df0c904ec50e27cb1dd8a9caf5e628bfdf3ec658/SAG/docs/Thang_Task/session-context-zero-mem/PERSISTENCE.md), [MODEL-LICENSES](https://github.com/DATN-SPRING2027/sag-laya-integration/blob/df0c904ec50e27cb1dd8a9caf5e628bfdf3ec658/SAG/docs/Thang_Task/session-context-zero-mem/MODEL-LICENSES.md). Hướng dẫn HANDOFF hiện hành nằm trên nhánh của PR này.
