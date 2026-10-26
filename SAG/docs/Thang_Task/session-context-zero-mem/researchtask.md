# Research: Context toàn session chat

Ngày: 2026-10-26. Record gồm nghiên cứu/đặc tả ban đầu, T1 baseline, T2 hợp đồng và T3a reader/hierarchy offline theo yêu cầu Thang. Selector retrieval mới chưa implementation.

## Nguồn và mức độ bằng chứng

- User decisions D1–D7: [SPEC](SPEC.md), từ hội thoại trực tiếp với Thang; bản ghi phỏng vấn giữ tại workspace `phong-van/2026-10-26-sag-session-context-zero-mem.md` ngoài repo.
- `origin/main` đã fetch/pull ff-only, base `77722700fa8ed83112bff6640213b12d0235c11c`; nhánh tài liệu `feat/Thang-session-context-spec-be-api`.
- Base commit là PR #37; không dùng lịch sử merge này làm bằng chứng session memory đã được triển khai.
- Không tìm thấy task spec session-context/Zero-Mem hiện có khi tìm trong `SAG/docs/Thang_Task/`, `SAG/docs/`, `SAG/tasks/`; tạo task folder này. Không ghi đè kế hoạch retrieval chung.
- Các nhận định code dưới đây được đọc tại base trên; không phải kết quả live/staging/production.

## Hiện trạng đã xác minh và quan hệ gọi

| Điểm vào/hàm | Hành vi và phụ thuộc |
|---|---|
| `web/lib/sse.ts` | Gửi ask theo agent/thread; endpoint thread là phạm vi v1 |
| `api/v1/agents.py::ask` | Owner và principal dependencies; tải agent/thread, truyền LLM tới `prepare_ask`, gọi `generate_stream` |
| `agent_domain.prepare_ask` | Lưu user, tải history, gọi compression, tạo `AskPlan` |
| `agent_domain._history` | User/assistant trong thread, sort created_at/id, limit cấu hình, lọc source scope; trả role/content |
| `agent_domain.compress_history` | So estimate với budget, giữ recent mặc định 8, summary từ 12.000 ký tự đầu phần cũ; fallback cắt đuôi |
| `generation/prompt.py::build_agent_messages` | System + history + current query/attachment; `estimate_tokens` dựa trên ký tự |
| `agent_service.generate_stream` | `transform_context` gọi `_fit_agent_context`; `sag_agent/runtime.py::_model_turn` áp trước mỗi provider call |
| `tools/builtin.py::SearchContextTool.invoke` | `retrieve_search_unit_sections`, kiểm canonical/locator, pack theo evidence budget; đây là retrieval tài liệu |
| `agent_domain.persist_answer` | Lưu assistant text, citations, steps, preview, status/error |
| `api/v1/openai.py::chat_completions` | Caller history, `build_ask_context`, thread_id=None; không lưu bền thread |

Tất cả đường dẫn bảng trên tính từ `SAG/apps/`; các file API nằm dưới `api/sag_api/` trừ `sag_agent/runtime.py`.

## Contracts và data model hiện có

- `db/models/agent.py`: Thread có owner_key/agent_id/title/archived; Message có thread_id, role, content, citations, attachments, steps, prompt_preview, status/error và timestamps từ mixin.
- `db/base.py`: IDs là UUID hex; TimestampMixin có created_at/updated_at. Không có session-memory generation/mutation revision trong model đã đọc. Không xem UUID maximum là sequence.
- `core/config.py`: history_keep_recent=8, history_load_limit=200 (configurable); đây là default, không chứng minh giá trị môi trường đang chạy.
- `core/conversation_ownership.py::conversation_owner_key`: key dựa trên user và verified principal; với issuer continuum-sag-session thêm tập project. Local identity có nhánh riêng; không tự thay semantics.
- `agent_domain.get_thread`: kiểm agent_id và owner_key, mismatch trả NotFoundError.
- `delete_message`/`delete_thread` có đường xóa; không thấy API sửa nội dung message trong file domain đã khảo sát. Test invalidation khi có mutation tương lai vẫn cần nhưng không tự thêm edit-message endpoint.
- Attachment history hiện được chiếu thành content text qua `_history`; không có bằng chứng nội dung ảnh cũ đã được semantic-index.

## ACL, provenance và các khoảng trống

`filter_messages_for_scope` kiểm Source IDs từ citations/steps, ẩn câu trả lời thiếu quyền và loại frozen prompt previews. Memory mới phải giữ việc này cả trước traversal và lúc xuất context.

`SAG/tasks/todo.md` còn mục mở về propagate Source provenance qua assistant history-dependent turns. `Message` hiện không có trường dependency message/source riêng dành cho memory trong model đã đọc. Việc cần làm: xác định cách lưu phụ thuộc xuyên lượt, xử lý legacy không rõ provenance, kiểm revoke/delete cho context và cache. Không khẳng định boundary đã an toàn chỉ vì filter từng message tồn tại.

`scripts/migrate_legacy_conversations.py` yêu cầu mapping owner được xác minh và hỗ trợ kiểm tra trước apply. Không gán owner tự động cho legacy chưa map để đáp ứng câu “chat cũ”.

## Ngữ nghĩa token và đo lường

- `_fit_agent_context` giữ system và latest user, ghép atomic tool-call/results, ưu tiên units gần đây; tính schemas và reserve. Đây là chốt an toàn phải giữ nhưng cách nhóm context mới cần kiểm lại.
- `generation/prompt.py::estimate_tokens` xấp xỉ CJK/character, không phải tokenizer provider.
- `LLMClient.stream_turn` nhận provider usage khi có; `sag_agent` cộng usage/requests của runtime.
- `LLMClient.complete` trả text; compression chạy trước runtime. Do đó không được lấy runtime usage làm tổng chi phí ask. Cần instrumentation phía provider cho non-stream calls/retries và quy tắc missing usage.
- Baseline phải gồm actual legacy summary cost; candidate gồm encoder/index, mọi agent calls và full-session synthesis. Cost tiền cần rate có provenance; chưa đo hoặc xác minh giá.

## Dependencies và tooling

`apps/api/pyproject.toml` có FastAPI, SQLAlchemy, asyncpg, pytest, Ruff, igraph, zleap-sag==0.12.0. Không thấy spaCy/BGE-M3 direct dependency được khai báo trong file này. Không kết luận chúng đã cài hoặc chưa có transitive adapter; cần research trước khi chọn/cài. Không tự thay NER bằng regex rồi coi là tái lập paper.

Tests được track trong `apps/api/tests/`: test_ai_provider_smoke.py, test_audit_remediation.py, test_config.py, test_universe_knowledge.py. Chưa thấy bộ test session context chuyên biệt trong tập file đã khảo sát; tài liệu retrieval có nhắc tests cũ không chứng minh chúng hiện tồn tại.

`test_ai_provider_smoke.py` là live opt-in bằng SAG_RUN_AI_SMOKE=1, không chạy trong task tài liệu. Test fixture logic không phải live cost acceptance.

## Thiết kế đề xuất theo file, chưa sửa code

| Khu vực | Trách nhiệm tương lai |
|---|---|
| `services/session_context/` (mới, đề xuất) | Snapshot read, selection, graph/hierarchy, budget/coverage; chia modules theo task nhỏ |
| `services/agent_domain.py` | Tích hợp selector vào prepare_ask, lifecycle hooks cho message/thread |
| `services/agent_service.py` | Group-preserving prompt fit, diagnostics và scope dependency khi xuất câu trả lời |
| `generation/llm.py`, `sag_agent` | Accounting/provider usage; tránh đưa app-specific memory vào generic runtime |
| `db/models/`, `migrations/` | Chỉ thay ở DB deliverable sau khi duyệt persistence contract |
| `tests/`, `scripts/` | Gold fixtures, baseline harness, integration/lifecycle/security tests |

Không sửa source ingestion/index, graph UI, Laya, BE IAM, DATN_FE trong task này. Nếu phát hiện dependency cần sửa public contract/FE thì tách work package, không tự mở rộng.

## Failure/edge cases cần chứng minh

Index stale; chat lớn hơn compression threshold; câu tiếp nối không có tên thực thể; cùng tên khác người/thread; sửa quyết định; thông tin mâu thuẫn chưa chốt; tool output lỗi; assistant failed/cancelled; legacy missing owner/provenance; delete/revoke giữa retrieval và generation; concurrent asks; rebuild crash/resume; encoder unavailable; insufficient budget; partial synthesis; query/system quá lớn; provider thiếu usage; mapping/source scope thay đổi.

## Test matrix và acceptance

Chi tiết ở [SPEC §9](SPEC.md#9-đo-baseline-và-nghiệm-thu), thực thi theo [plan](tasks/plan.md). Tách normal/full-session, cold/warm, calibration/holdout. Review với cùng snapshot và model; không tự chạy live để lấy số giả.

- [x] Ghi quyết định người dùng, hiện trạng và gaps từ code.
- [x] Soạn SPEC, kế hoạch và checklist để review.
- [ ] Duyệt thiết kế kỹ thuật, dependency và DB contract.
- [ ] Đo baseline, chốt ngưỡng và giới hạn hữu hạn.
- [ ] Xây prototype và đánh giá holdout.
- [ ] Xác minh provider/integration/ACL/lifecycle/rollback trước rollout.

## Impact, migration và rollback của giai đoạn đặc tả ban đầu

Lượt này chỉ tài liệu; không schema/config/dependency/runtime/secret changes. Nhánh chưa commit; không push/PR.

Triển khai tương lai có thể cần DB derived index và provenance dependency: nhánh/PR DB riêng, migration mới và down plan; không sửa migration lịch sử. Rollback giữ raw messages, tắt selector, dừng jobs, vô hiệu index generation; không tự drop dữ liệu đang được request sử dụng.

## Các kiểm tra thực hiện trong lượt tài liệu

Đọc source/config/tests/docs, kiểm Git base/remotes/worktree. Validator đã kiểm 4 file Markdown, 15 liên kết tương đối, whitespace và dấu conflict: PASS. Đã kiểm patch từng file mới và quét mẫu private-key/token phổ biến: không phát hiện; kiểm này không phải chứng nhận security của runtime. `git diff --check`: PASS; tracked code không đổi, `.serena/` untracked có sẵn được giữ nguyên. Chưa chạy pytest/Ruff/build/live benchmark; không thay code để cần chạy các quality gates đó trong lượt này.

## T1 — Bắt đầu thực hiện theo yêu cầu Thang

Thang đã yêu cầu thực hiện T1: dựng bộ dữ liệu chuẩn và đo baseline. Nhánh mới từ main cập nhật: `feat/Thang-session-context-baseline-be-api`; base vẫn `7772270`. Giữ nguyên gói docs untracked đã tạo và `.serena/`.

Phạm vi: fixture synthetic Việt/Anh có gold answer/evidence, CLI replay trên disposable SQLite, observer LLM dùng riêng trong harness, tests accounting và corpus, báo cáo baseline. Không đọc chat thật, thay schema hay sửa thuật toán production. Baseline chạy `prepare_ask` và `generate_stream` thật, không mock backend retrieval để nhận là integration thành công.

Môi trường kiểm tra: Python environment có LiteLLM 1.102.0, pytest 9.1.1, Ruff 0.16.8; configured model Qwen/Qwen3-8B tại SiliconFlow; context 128000, output reserve 4096, retries 2, history_load_limit 200, history_keep_recent 8. Preflight provider thành công, trả usage 16 input/2 output tokens. Không log key, URL credentials hoặc raw env.

Thiết kế corpus: split calibration/holdout cố định, deterministic filler expansion để kiểm history quá 8, quá 200 và compression thật theo cấu hình. Không giảm context window để tạo baseline giả. Tách profile web-disabled (default hiện tại) và web-enabled cho cùng câu hỏi; không bật web tools ngoài cấu hình. Gold chỉ có trong evaluator, không đưa vào prompt. Report không coi literal matching là semantic correctness.

Accounting observer phải ghi compression ngoài runtime, final generation/stream, usage missing, failures/cancel và observed deployment attempts. SDK-internal transport retries không quan sát được phải nêu rõ, không giả là 0. Các giới hạn benchmark là tài nguyên chạy thí nghiệm, không phải ngân sách sản phẩm đã chốt ở T4. Provider pricing chưa xác minh thì monetary cost giữ unknown.

Các file dự kiến mới: `services/eval/session_context_accounting.py`, `scripts/eval_session_context.py`, `tests/fixtures/session_context.jsonl`, `tests/test_session_context_baseline.py`. TDD: tests offline trước, live chỉ dùng dữ liệu synthetic đã kiểm tra. Không sửa ingestion/index, production auth hoặc session selection.

## T1 — Kết quả thực hiện và review

- Đã thêm 4 file mới theo phạm vi trên, cùng một option `include_stream_usage=False` trong constructor `LLMClient` để harness có thể bật usage cho stream. Không đổi mặc định production, thuật toán history/compression, public API, schema, dependencies hoặc config env.
- Corpus 24 ca có calibration/holdout cố định. Đã chạy 12 calibration × 2 profile bằng provider thật, không chạy holdout. Sửa một lỗi metadata group cho ca approval-distinction sau review; giữ raw report, không đổi messages/query/model output, không gọi lại model để sửa nhãn.
- Kết quả chính: 30 logical calls, 246898 input tokens, 3191 output tokens; 6 compression calls ngoài runtime đã được ghi. Mọi logical call có provider usage và một observed deployment attempt, không observed failures trong primary run. Không chứng nhận SDK-internal HTTP retry coverage hoặc hóa đơn; monetary cost vẫn unknown.
- Kết quả/samples/lineage ở [BASELINE](BASELINE.md) và [summary evidence](evidence/baseline-calibration.summary.json); raw synthetic reports trong `.data/` không track.
- Literal recall sau sửa nhóm: normal web-on 50% (n=10), full-session web-on 66,67% (n=2); web-off có 12/12 no-answer. Không gọi đây là semantic accuracy hoặc SLA. Review phát hiện mất bằng chứng ngoài 200 message và giữa phần compression; default local-grounding gate cũng loại câu trả lời history khi không có Sources.
- Code review theo correctness/readability/architecture/security/performance: observer tách trong eval layer, không ghi keys/prompts/raw exception; tests kiểm no-gold-leak, default opt-in, missing usage/retry, cleanup/cancel/budget và corpus. Không sửa shared retrieval/ingestion lane để làm baseline tốt hơn.
- Ruff phần thay đổi PASS. Ruff toàn repo 124 findings ở 26 file không sửa; xác minh không finding nào thuộc file modified. Không claim full repo lint green.
- Không có DB migration/backfill thật. Rollback T1 là revert corpus/harness/opt-in option; disposable SQLite không chạm dữ liệu production. `.serena/` có sẵn được giữ ngoài commit.
- Ngưỡng nghiệm thu T4, giá tiền provider đúng account, production effective Settings và repeated cold/warm measurements chưa được chốt/đo. Baseline dùng Settings/dotenv, không nạp database overrides production.

Full offline suite cuối: 43 passed, 1 deselected trong 14,87 giây; 12 tests chuyên biệt đều pass. Sáu source hashes, corpus hash, sửa metadata không thay facts/queries, 23 relative links và whitespace/diff check đã xác minh. Final commit được ghi ở handoff. T1 đã có baseline có thể sử dụng để bắt đầu T2; chưa đánh dấu giải pháp session-memory v1 đạt nghiệm thu.

## T2 — Bắt đầu hợp đồng evidence/provenance/scope/budget

Thang yêu cầu tiếp tục T2. Nhánh mới `feat/Thang-session-context-contract-be-api` tạo từ main đã fetch/pull (`7772270`), sau đó `cherry-pick --ff f82eb78` làm phụ thuộc cục bộ. T1 chưa nằm trên origin/main tại lúc bắt đầu; không coi phụ thuộc này đã được review/merge. Giữ nguyên nhánh T1 và `.serena/`.

### Nghiên cứu và ranh giới đã xác minh

- `get_thread()` kiểm owner_key/agent_id trước khi trả thread. `get_conversation_owner()` và VerifiedPrincipal hiện có là nguồn identity; các DTO T2 không tự xác thực JWT hoặc cấp quyền.
- `get_authorized_source_ids()` nhận danh sách ID cần kiểm, batch lookup đúng principal/mapping; không dùng search candidate limit làm whitelist toàn bộ Source quyền đọc.
- `filter_messages_for_scope()` hiện chỉ đọc citations/steps từng message; cần metadata dependency xuyên lượt riêng. Không dùng citations rỗng để kết luận assistant legacy source-free.
- Message/Thread hiện chưa có durable generation hay dependency ledger. T2 chỉ định nghĩa token snapshot và hợp đồng đọc lại; T5/T6 mới xử lý persistence/mutation fences.
- T1 cho thấy session history phải có kind riêng `user_statement`/`assistant_statement`; không nhận là canonical document evidence hoặc tự thay grounding gate hiện tại.

### Thiết kế T2 trước khi coding

Thêm types.py (immutable identity/scope/message refs/span/snapshot/selection/coverage), scope.py (reuse get_thread và Source resolver; kiểm scope/version; dependency closure không đệ quy; kiểm lại trước use/output), budget.py (limits hữu hạn, reserve/settle mỗi attempt, unknown usage không miễn phí), và tests/test_session_context_scope.py. Không thêm public API, production caller, model/SQL schema/config hoặc selector.

Snapshot digest phải thay đổi khi content, role/status, source/dependency metadata, thứ tự hoặc scope đổi; generation là token do trusted storage adapter cung cấp trong tương lai, không giả có durable field sẵn. Span theo Unicode codepoint trên nguyên văn, content hash theo UTF-8. Scope fingerprint có owner/agent/thread, issuer/subject/org/tenant, projects/partitions, Source whitelist và @ filter; bỏ token_id để không vô hiệu cache chỉ do refresh cùng quyền.

Unknown legacy provenance, dependency thiếu/xóa, Source revoke, cycle, hash mismatch, owner/thread/project khác và expired principal đều fail closed. Evidence assistant vẫn là phát biểu lịch sử, không tự là sự thật bên ngoài. Lời user chưa xác nhận đề xuất của assistant không được typed contract tự nâng thành approval.

Coverage ledger phân biệt ready/partial/indexing/unsupported; full-session ready yêu cầu duyệt hết eligible IDs do trusted prefilter xác định, không phải top-k tự khai đủ. Traversal coverage không phải semantic recall. Budget không có số default sản phẩm; T4 chốt giá trị sau đo. Reserve token/call/output trước khi gọi; mỗi retry là attempt riêng; settle unknown giữ reservation và chặn call mới. Deadline/cancel/index-work dùng cùng request budget.

### Test matrix và phạm vi nghiệm thu T2

Tests contract/pure helper + disposable SQLite cho ownership adapter: user history không có Source vẫn có thể là session statement; assistant thiếu dependency metadata bị loại; transitive source revoke qua nhiều assistant; cache/fingerprint đổi; snapshot mutate/delete; Unicode spans; cycle/deep chain; full-session coverage; partial/indexing states; budget calls/tokens/deadline/index/unknown usage/duplicate settlement/cancel.

Kết quả offline sẽ chỉ chứng minh invariant và adapter seam. Chưa gọi provider, chưa tích hợp vào ask/output gate, chưa chứng minh production TOCTOU/deletion/ACL: caller tương lai phải refresh quyền và snapshot ngay trước model/output. Việc validate snapshot ở T2 không tạo transaction/fence bền thay T5/T6.

### T2 — Kết quả và review

- Thực hiện 4 file implementation/test đúng phạm vi; thêm [CONTRACT](CONTRACT.md), đồng bộ SPEC/plan/todo. Immutable DTO, hash/spans, authority scope resolver, leaf-first dependency authorization, snapshot validation và request budget tách khỏi production flow.
- Dấu partition gốc bắt buộc: kiểm source cho thấy owner key chỉ thêm projects với issuer `continuum-sag-session`; không suy partition từ owner key hay principal hiện tại. Unknown legacy được loại khỏi evidence và chặn full-session ready. Không tự gán provenance khi đọc.
- TDD ban đầu import module chưa tồn tại fail; sau implementation test bổ sung bắt được source-filter widening và reservation cross-request. Đã sửa invariant filter và gắn UUID budget vào reservation. Test bổ sung full-session còn unknown legacy cũng fail trước sửa và pass sau sửa; không đổi kỳ vọng để che lỗi.
- Full offline: `.venv/Scripts/python.exe -m pytest -m "not ai_smoke" -q --tb=short` từ `SAG/apps/api`: **92 passed, 1 deselected**, 21,97 giây; 49 ca mới thuộc `test_session_context_scope.py`. Không live provider call, không đọc lịch sử người dùng thật. SQLite disposable kiểm ownership thật, Source resolver là double kiểm adapter seam; không gọi đó là production ACL acceptance.
- Ruff targeted: `.venv/Scripts/python.exe -m ruff check sag_api/services/session_context tests/test_session_context_scope.py`: PASS. Ruff toàn `sag_api sag_agent tests`: 124 lỗi tồn tại từ T1 ở các file không thay đổi; không claim toàn repo lint green.
- Self-review 5 trục: correctness kiểm expiry/filter/hash/status/cycle/deletion/closure/coverage; readability tách types/scope/budget; architecture tái sử dụng ownership/Source resolver, không thêm endpoint; security không log nội dung/credential, provenance fail closed; performance leaf-first không đệ quy, không last-N, fingerprint partition tính một lần mỗi prefilter. Helper vẫn nhận mapping trong bộ nhớ; adapter phân trang/index-work metering và fencing phải làm T3/T5/T6, không coi là đã phục vụ session lớn production.
- Không thêm dependency, public API, env/config, DB schema/model/collection/index hoặc migration; rollback riêng commit T2, không data rollback. Giữ `.serena/` ngoài commit. Không push, approve hoặc merge.
- T2 phụ thuộc local T1 `f82eb78`; origin/main lúc bắt đầu vẫn `7772270`. Handoff nêu commit T2 riêng; chưa coi T1 được merge. Các giới hạn production grounding, durable provenance/generation, async cancellation, hidden SDK retries, tokenizer chính xác và monetary cost được ghi rõ trong CONTRACT. T3 là bước tiếp theo, chưa nghiệm thu giải pháp memory v1.

## T3a — Nghiên cứu trước implementation

Thang yêu cầu reader phân trang toàn lịch sử và hierarchy. Fetch/pull main `7772270`; tạo nhánh mới `feat/Thang-session-context-reader-be-api`, fast-forward cherry-pick T1 `f82eb78` và T2 `eb822bc` làm phụ thuộc local chưa merge. `.serena/` có sẵn giữ nguyên. Không có AGENTS/GEMINI/CLAUDE riêng trong repo; đã đọc README root/SAG/API, pyproject và task docs. Không có `docs/architecture/PROJECT_MEMORY.md` trong repository này.

Luồng đã kiểm: `agent_domain.list_messages_page()` có keyset descending cho UI nhưng không có pinned snapshot, durable provenance/generation hoặc ACL đầu vào riêng; `_history()` vẫn last-200. Không tái sử dụng UI projection làm nguồn provenance, không sửa các caller production. T2 `make_snapshot`, `eligible_message_ids`, `Coverage`, spans và `BudgetState` được tái sử dụng; chưa có caller production cho chúng.

Đọc lại Zero-Mem v1 Method/Hierarchical trace units, phương trình 5: turn/window/episode/local giữ provenance; episode paper có semantic continuity và temporal/session boundaries. T3a chỉ prototype deterministic temporal grouping, không encoder/NER/generative model và không claim tái lập semantic episode segmentation. Nguồn: https://arxiv.org/html/2607.29377v1 (đọc ngày 26/10/2026).

Thiết kế và file-by-file: `reader.py` chứa immutable page/cursor/read result và fixture-only pinned store; đọc keyset ascending `(UTC created_at,id)` hết các trang, cursor ràng buộc snapshot/scope, kiểm digest sau đọc; không last-N. Prefilter transitive T2 trên toàn snapshot trước tạo hierarchy, giữ ordinal gốc để không nối qua message bị loại. `hierarchy.py` tạo turn/window/episode/local units không rewrite text, ID deterministic theo scope và refs/children; giữ raw span ở leaf, parent dùng child IDs để tránh nhân bản toàn văn. Tham số window/episode/radius/time gap truyền tường minh, chưa là default sản phẩm. Hai test files mới riêng reader/hierarchy; không thay types/scope/budget T2.

Budget: fixture cold load/hash/order, page I/O, đọc/hash content/dependencies, authorization scan, tạo node/edge và resolve spans đều trừ index-work; page size chỉ giới hạn I/O. Công việc phải kiểm trước lô; hết budget/deadline/cancel raise, không trả bản đọc/hierarchy nửa chừng như đã đủ. Work-unit gồm page/node/edge và block 1024 Unicode codepoints; sort charge n*bit_length(n). Không quy work-unit ra milliseconds hoặc monetary cost. Fixture store vẫn in-memory, không durable reader/transaction hoặc resumable backfill production.

Threat/edge matrix: scope/cursor/generation mismatch, duplicate IDs, equal timestamps/different timezone, reversed input, page size đổi, thiếu/empty page, nguồn bị revoke kéo theo assistant, unknown legacy, empty content, message dài/Unicode, denied ordinal barrier, time-gap boundary, budget/cancel/deadline. Tests đọc hơn 200 message và kiểm mọi eligible message/span có đường về raw; calibration corpus T1 chỉ dùng để kiểm cấu trúc/facts preserved, không chạy/tune holdout hoặc đo answer quality.

Acceptance T3a: 4 implementation/test files, docs đồng bộ; pytest focused + full offline, targeted Ruff, diff/secrets/generated checks và self-review 5 trục. Không public API/DB/config/dependency/FE, không backfill chat thật, không live model/cost claim. T3b retrieval/graph, T4 ngưỡng số, T5/T6 persistence/fence và T7 integration vẫn ngoài phạm vi.

### T3a — Kết quả thực hiện và self-review

- Thêm đúng `reader.py`, `hierarchy.py` và hai test files theo phạm vi; thêm [hướng dẫn và giới hạn](READER-HIERARCHY.md), đồng bộ SPEC/plan/todo. Không sửa các module T1/T2 hoặc production callers. Search caller xác nhận reader chỉ được hierarchy/tests import, chưa nối ask.
- TDD RED ban đầu: 2 collection errors vì modules chưa tồn tại. 26 tests đầu pass sau implementation. Review bổ sung bắt lỗi nhiều empty turns trùng ID; sửa bằng origin ref, local ID gắn anchor. Review tiếp bắt truncated HistoryRead còn denominator đủ; thêm exact eligible-ID check. Các regression tests fail trước fix và pass sau fix, không đổi expectation để che lỗi.
- Full offline command `.venv/Scripts/python.exe -m pytest -m "not ai_smoke" -q --tb=short` từ `SAG/apps/api`: **128 passed, 1 deselected**, 27,61 giây; **36 ca T3a** trong hai file mới. Lần focused trước đó 34 passed, sau thêm regression denominator và cross-page dependency thì full suite bao gồm cả hai. Không chạy lại cùng suite chỉ để trấn an.
- Targeted Ruff toàn package session_context + 2 tests mới PASS. Ruff toàn `sag_api sag_agent tests` vẫn **124 errors**, các findings tồn tại từ trước trong files không sửa. Không sửa lint ngoài phạm vi. Không install thêm dependency, không có typecheck/build command riêng trong pyproject; test suite kiểm import và integration seams offline. `ai_smoke` live được deselect có chủ ý, không coi provider acceptance đã đạt.
- Cấu trúc: 431 messages qua 5 page sizes, order ties/timezone, duplicate/cursor/snapshot/omitted-page guards; filter closure cả dependency ở trang sau; không nối qua denied ordinal/time gap; Unicode/empty provenance/anchor; budget cold load/pages/hash/nodes/edges/resolve, deadline/cancel/expiry. Calibration-only corpus kiểm turn count/gold raw quote preservation, không retrieval/answer-quality hoặc holdout evaluation.
- Review correctness: pages/digest/denominator và refs exact; readability: 2 modules trách nhiệm riêng, không adapter protocol giả hoặc config defaults; architecture: reuse T2, không UI projection/ORM migration; security: content không vào repr/log, không suy legacy provenance, scope recheck trước build và T2 output gate vẫn bắt buộc; performance: keyset bisect, parent child refs thay copy toàn văn, meter trước copy/traversal, không last-N. Lookup mỗi resolve vẫn O(units), store/read giữ snapshot trong RAM; đã ghi giới hạn, chưa claim production scalability.
- Không DB schema/index/data change, env/config, API/FE hoặc model call; rollback riêng commit T3a không có data migration. T1/T2 vẫn local dependencies; chưa push/merge. `.serena/` có sẵn giữ nguyên ngoài commit. Handoff nêu commit và exact push command; T3b và T4–T9 còn lại.
