# Research: Context toàn session chat

## Review độc lập và bàn giao lên PR40 — 27/10/2026

Thang yêu cầu review độc lập, sau đó yêu cầu push kết quả lên cùng PR40. Snapshot runtime được review là `05b5b19c00822d124f351dc5019e64e0effe3605`, base `77722700fa8ed83112bff6640213b12d0235c11c`. Báo cáo và reproduction nằm tại [reviews/2026-10-27-independent/REVIEW.md](reviews/2026-10-27-independent/REVIEW.md).

Kết luận: merge REQUEST CHANGES, rollout NO-GO, giữ OFF. Bốn lỗi mới đã tái hiện: full-session bypass explicit Source grounding, PostgreSQL ask/delete deadlock40P01, thiếu provenance của failed map batch đã dispatch, và evaluation resume chấp nhận config/caps mới nhưng giữ manifest cũ. Hai nhóm chất lượng normal/full-session đã biết còn nguyên; không coi traversal complete là semantic recall.

Kiểm chứng lượt review: full offline304PASS/5SKIP/2DESELECTED; nativePostgreSQL16.15 trên container disposable riêng5PASS; bốn reproduction invariantFAIL như báo cáo; targetedRuffPASS, fullRuff123diagnostics. Source/corpus/config/model/dependency hashes của calibration/holdout đã lưu khớp code review. Không chạy lại paid provider/holdout; không migrate/backfill dữ liệu thật. Container QA đã được xóa.

Commit bàn giao chỉ thêm evidence review/scripts và cập nhật record này. Reproduction nằm ngoài test suite mặc định, nativePG mặc địnhSKIP; chạy explicit runner để quan sát lỗi chưa sửa. Không sửa production, schema/config hoặc test gốc; không bật feature, merge hoặc approve. User đã yêu cầu push trực tiếp lên nhánh PR40; không tạo PR/nhánh thay thế. `.serena/` được giữ nguyên.

Ngày: 2026-10-26. Record gồm nghiên cứu/đặc tả ban đầu, T1 baseline, T2 hợp đồng, T3a reader/hierarchy và T3b selector offline theo yêu cầu Thang. Chưa tích hợp/đạt nghiệm thu runtime.

Tiếp tục T4 qua đêm26–27/10/2026: đã có [calibration report](CALIBRATION.md), [numeric policy](CALIBRATION-POLICY.json) và [license audit](NER-LICENSE-AUDIT.md). Measurement hoàn tất, rollout NO-GO; xem phần kết quả cuối record.

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

## T3b — Nghiên cứu trước implementation

Thang yêu cầu graph retrieval, fusion và evidence closure. PR #38 vẫn OPEN, main `7772270` sau fetch/pull; nhánh mới `feat/Thang-session-context-graph-be-api` có T1–T3a làm local prerequisite (f82eb78, eb822bc, 919513b). Không sửa PR #38 hoặc tự push task mới. Đã đọc workspace rules, README root/API và task spec/contracts/reader/hierarchy. `.serena/` giữ nguyên.

Paper Eq. 3–14 dùng entity–context + adjacency, PPR, dense/lexical signals, hierarchical view, query-wise min-max fusion và bounded graph/local support. API docs đã kiểm qua find-docs/Context7 và primary sources: https://arxiv.org/html/2607.29377v1 ; https://huggingface.co/BAAI/bge-m3 ; https://huggingface.co/Davlan/xlm-roberta-base-wikiann-ner ; https://huggingface.co/docs/transformers/main_classes/pipelines ; https://sbert.net/docs/package_reference/sentence_transformer/model.html . Không đổi NER thành regex rồi gọi tương đương paper.

Môi trường có sentence-transformers 5.7.0, transformers 5.17.0, torch 2.14.0; chưa có spaCy. Chọn optional local model adapter: BGE-M3 revision `5617a9f61b028005a4858fdac845db406aefb181` (1024 dimensions, 8192 tokens, MIT); Davlan wikiann NER revision `642b9a14e71df124c43c67e47703a91341576dfb` có Vietnamese/English theo card, PER/ORG/LOC. Card NER có mô tả base/large không nhất quán và thiếu license metadata: dùng config thực để kiểm cấu trúc; chưa phê duyệt redistribution/production license. Đã tải public weights vào HF cache ngoài repo; không credentials/chat thật hoặc paid generative call. Không dùng encoder mặc định bge-large-en-v1.5 của SAG cho corpus Việt rồi ngầm coi là M3.

Phạm vi 5 files implementation/config/tests: graph.py chứa feature DTO/local model adapter và graph/PPR; selection.py chứa lexical+dense hierarchy ranking, routing/fusion và closure/packing; 2 tests mới; pyproject.toml chỉ thêm optional extra đúng packages đang kiểm. Default app không import/load model, không dependency production bắt buộc mới. Không sửa T2/T3a/public API/DB/FE.

Security: chỉ encode NER trên HistoryRead đã authorized; features ràng buộc raw hash/snapshot/model revisions, reject stale/scope mismatch trước ranking. Graph entity nodes không vượt thread, adjacency không vượt denied ordinal/time gap. Fitter giữ dependency groups nguyên tử, không đưa một phần group khi cap nhỏ. Final validate T2 vẫn bắt buộc với quyền/rows mới; prototype không có durable fence.

Thiết kế prototype: observed entity keys theo type + NFC/casefold nguyên văn span; context/entity frequency edges và adjacency. Query exact entity anchors + cosine context reset, bounded personalized PageRank, không anchor thì dense/lexical fallback. Hierarchy centroid embeddings + lexical/phrase scores, coarse episode/window/turn view. Cả hai view chạy; route local/relational là explicit deterministic structural signal hoặc caller chọn, min-max fusion theo Eq12–13. Closure gồm graph bridge/local neighbors và mọi message dependency; dedup nguyên văn, caps tường minh. Không tự giải mâu thuẫn hoặc biến assistant thành user approval.

Sai khác được công bố: exact entity alignment, context-level PPR thay sentence-level Eq9; temporal episodes T3a; coarse-to-fine điểm centroid; encoder long-text dùng bounded raw chunks/mean vectors, NER chunk offsets không sinh summary. T4 chưa chốt số thresholds/budgets; T8 full-session chưa triển khai trong selector normal. Tests double kiểm thuật toán; model smoke riêng synthetic Việt/Anh mới chứng minh adapter chạy, không chứng minh chất lượng/cost nghiệm thu.

Budget/work: tokenization/NER/encoder chunks, dot products, nodes/edges/PPR steps, hierarchy aggregation/ranking, group traversal và prompt packing đều tính index-work trước bước. Encoder metrics tách input tokens/calls khỏi generative budget. Model load/inference synchronous chưa preempt được giữa một call; kiểm trước/sau và không publish khi hết deadline. Không chạy holdout để tuning.

Test matrix: bridge distributed evidence, no entity anchor, follow-up query/local route, both views/normalization/ties, stale model/hash/snapshot/scope, cross-project/revoke/unknown legacy, denied adjacency, dependency packing/truncation/empty/long text, finite caps/deadline/cancel. Verify focused/full pytest, optional actual model smoke, Ruff touched/full baseline, diff/secrets/docs links/self-review. Remaining T4–T9 không được đánh dấu chỉ vì T3b chạy offline.

### T3b — Kết quả và self-review

Thang sau đó yêu cầu hoàn tất T3b rồi push trực tiếp PR #38. Authorization này cho phép cập nhật head remote `feat/Thang-session-context-reader-be-api` bằng commit descendant của `919513b`, không force-push/merge/approve. Nhánh triển khai local vẫn là nhánh task mới `feat/Thang-session-context-graph-be-api`.

- Thực hiện graph.py, selection.py, 2 test files và optional extra đúng phạm vi 5 implementation/config/tests; đồng bộ [GRAPH-SELECTION](GRAPH-SELECTION.md), SPEC, plan/todo và reader handoff. Không đổi production ask/retrieval/auth/DB/FE hoặc modules T1–T3a.
- Tests viết trước: missing modules RED; 16 tests đầu GREEN. Regression duplicate entity span RED trước sửa, sau sửa reject duplicate để không inflate weights. Bổ sung frequency/caps/long raw/old message/no-anchor/offset/usage và actual model opt-in. Không đổi expectation để che lỗi.
- Full offline cuối: `.venv/Scripts/python.exe -m pytest -m "not ai_smoke" -q --tb=short`: **161 passed, 2 deselected**, **13,46 giây**. 33 ca T3b offline; live provider smoke cũ và local learned-model smoke mới được tách riêng bằng marker, không giả đã chạy trong offline suite.
- Actual model integration: `$env:SAG_RUN_SESSION_CONTEXT_MODEL_SMOKE='1'` rồi `.venv/Scripts/python.exe -m pytest tests/test_session_context_selection.py -m ai_smoke -q -s --tb=short`: **1 passed, 16 deselected**, **19,54 giây**, phần đo pipeline **16,25 giây**. 5 raw synthetic messages, 2 câu hỏi Việt, NER có PER/LOC, vectors 1024; supporting IDs có đủ, 3 spans/query qua T2 validator. 7 encoder calls/81 tokenizer input tokens, 7 NER calls/81 tokenizer input tokens, 0 generative calls, 108040 work units. Smoke không phải accuracy/cost calibration; câu hỏi thứ hai có một local distractor được giữ, đã ghi rõ để T4 đo precision.
- Source model revisions/API được xác minh qua primary docs/HF metadata và chạy thực tế. `get_sentence_embedding_dimension` deprecated alias được thay bằng `get_embedding_dimension` sau đọc source installed; actual smoke cuối dùng tên mới, không còn warning alias. Không cài model mới vào dependencies mặc định; pinned optional extra chỉ dành người chủ động dùng adapter. NER license metadata chưa rõ là rollout gap thực tế, không tự coi model distribution đã được duyệt.
- Ruff targeted toàn package session_context + 2 tests mới PASS. Ruff toàn repo vẫn 124 findings cũ, không file mới bị finding. No install mới cần cho run vì packages đã có; không có typecheck/build command riêng trong pyproject. Không chạy holdout, live answer generation, backfill hay chat thật.
- Review correctness: dense fallback khi không anchor, positive min-max/flat ties, both-view execution, centroid-zero guard, closure/reference/dedup/atomic caps và exact snapshot checks; readability: chỉ hai modules theo task, optional load rõ; architecture: reuse T2/T3a và stopwords hiện có, không financial rewrites cho chat; security: content hash/model/scope gate, no credentials/raw logs, no implicit legacy provenance; performance: finite PPR/beam/work/index caps, dimensions trước inference, tránh hash lại dependency text mỗi edge, không last-N. Local sync model calls/lookup scans vẫn có chi phí và không preempt giữa một inference; không claim production scalability.
- Sai khác paper và giới hạn temporal route/exact entity/PPR context/raw chunk mean được công bố. Codepoint packing chưa là final token fitter; cache/provenance persistence/DB fences/production model quality/license/thresholds thuộc T4–T9. Rollback riêng commit T3b, không data migration. `.serena/`, `.data/` và HF weights giữ ngoài commit. Final commit/PR head verification được ghi trong handoff.

## T4 — Thiết kế phép đo trước thực hiện

Thang yêu cầu đo chất lượng/chi phí, chốt ngưỡng và xác minh license NER. Nhánh mới `feat/Thang-session-context-calibration-be-api` từ main đã fetch/pull (`7772270`) + prerequisites T1–T3b (`5db4464`); PR38 vẫn OPEN. Giữ `.serena/`, không có production integration/DB change. Đọc rules, README/API, BASELINE, contracts và các hàm replay/selector/accounting trước coding.

Corpus calibration cố định 12 ca: 10 normal, 2 full-session. Ba ca >525k codepoints, còn lại 4k–63k. Không mở rộng/tuning holdout. NER đã kiểm pinned repository API, card/tree và 2 closed discussions: không khai license, không có LICENSE/COPYING, discussions chỉ thêm TF/safetensors; base XLM-R MIT không đủ chứng minh quyền fine-tune. Ghi kết luận license audit là UNVERIFIED/NO-GO rollout nếu không tìm thấy grant từ chủ model, không tự suy MIT hoặc liên hệ tác giả khi chưa được yêu cầu gửi tin.

Phạm vi dự kiến: calibration CLI mới + pure metrics module + tests; mở rộng replay T1 bằng keyword history_override (default None giữ behavior), không sửa app. Candidate dựng history từ selected raw refs qua T2, prepare_ask llm=None tránh compression call rồi build lại plan bằng history override; giữ agent/model/tools/grounding/runtime như baseline. Không bypass document-only gate để làm score đẹp. Baseline T1 historical và paired legacy hiện tại báo riêng; source/config drift phải được ghi.

Pre-registration: features BGE/NER pinned, raw chunks 384 codepoints (khác smoke128 phải ghi); CPU threads4 chỉ trong harness; không cache chunk/bỏ filler để giả cold cost. Cold bao gồm feature build; warm reuse đúng features cùng scope trong RAM, vẫn encode query thật. Sweep structural profiles compact/balanced/wide (main 2/4/6, bridge1/2/3, local1/1/2, max messages8/16/24, codepoints8000/16000/24000, beam2/4/6, PPR16/20/24, rho/damping0.6; hierarchy window4/episode4/radius1/gap1800). Chọn profile nhỏ nhất giữ đủ gold evidence trên normal calibration, không dùng answers để chọn profile. Sweep maxwork100M/case và 1800s whole experiment là giới hạn đo, không phải product default.

Sau khóa profile mới gọi answer provider cùng Settings hiện tại và 2 web profiles; warm candidate lặp 2 lượt cho normal, full-session chẩn đoán riêng vì T8 chưa triển khai. Global max96 logical LLM calls/1200s generation/2M estimated tokens; không chạy chat thật. Mỗi replay báo provider usage, missing/failure/retries, model TTFT/duration và full ledger. CPU encoder/NER tokens/calls và cold/warm duration riêng, không coi zero encoder cost.

Ngưỡng chất lượng dự kiến để review dựa trên baseline: không giảm semantic correctness từng normal case so paired baseline, old-evidence recall phải tăng so T1, gold raw evidence normal100% trên regression corpus; no scope/hash/provenance violations. Mục tiêu token normal dài (>=120 messages) giảm ít nhất50% mean input+output so paired baseline, generation1 call/query, compression0; unknown usage không được pass cost gate. Latency cold/warm là measured gates sau sweep, không đoán SLA. Không đạt thì khóa NO-GO và defect list, không hạ ngưỡng theo output.

Chốt numeric contract trước holdout, không đồng nghĩa production readiness; full-session call/quality gates chỉ provisional envelope tới T8/T9. Pricing đúng provider/account chưa có primary source thì monetary cost unknown, báo tokens thay vì tuyên bố tiết kiệm tiền. T4 report phải phân biệt completed measurement với acceptance PASS/FAIL và license gate.

### T4 — Điều chỉnh profiling trước khi khóa profile/answer replay

Pilot serial ghi 120 messages ~51s; batch16 không reuse ghi ~28s, nhưng ba case dài có 1419–1433 chunks mà chỉ 201 chunks khác nhau. Dừng các pilot trước provider replay, giữ artifacts đã hoàn tất; CPU của phần bị dừng không được coi miễn phí hoặc tính vào chi phí steady-state. Không có answer/gold-score tuning trong bước tối ưu inference này.

Thêm `LocalFeatureEncoder.featurize_many` với batching16 và optional exact chunk reuse **chỉ trong một call**, default reuse=False; không cache liên scope/case, không bỏ message/filler khỏi graph. Mọi raw chunk được materialize lại đủ offsets và mean vectors, work vẫn tính copy/aggregation. Kiểm RED/GREEN cho batch offsets/counts và reuse không mất raw messages. Final calibration v2 dùng reuse=True; report phải nêu hit ratios vì synthetic repetitive filler làm cold timing thuận lợi hơn dữ liệu thật. Source hash graph khác nên không trộn feature cache pilot vào final. Thêm pin Torch2.14.0 vào optional extra vì harness import trực tiếp để ghim threads4, cùng version đang đo; phạm vi thực tế 6 implementation/config/test files, không default runtime dependency mới.

Đã xác minh giá công khai chính chủ tại https://www.siliconflow.com/models/qwen3-8b và https://www.siliconflow.com/en/pricing : USD0.06/M input và output. Provider host thực là api.siliconflow.com. Chỉ tính uncached public-list reference, không coi là account invoice hoặc tính CPU bằng0. Preflight riêng: 1 logical call, 15 input/22 output tokens, một observed deployment attempt, usage đủ; không gộp call này vào paired cohort để làm savings đẹp.

### T4 — Kết quả, accounting và review

- Sweep12 calibration cases, không dùng holdout để tuning. Compact chỉ8/9 evidence normal; balanced/wide9/9, nên khóa balanced trước answer replay. Query-mode cap16 selected messages/16000 codepoints không phải last16. Một ca compression-middle vẫn partial khi optional support không fit.
- V2 ban đầu chạm reservation2M tại1.998.270 tokens ước tính,80 calls. Có11 replay bị resource-blocked; metric cũ có thể tính failed/no-call thành0 tokens và savings giả. Test RED bắt đúng lỗi này; paired_tokens nay reject failed/unpaired/unknown. Giữ report gốc và không dùng comparisons đó làm acceptance.
- Resume11 attempts chưa completed với cumulative cap128 calls/4M estimated tokens; calls/reservations cũ được seed,1200s replay budget trừ866,747s đã dùng. 61 completed rows/answers/usage được post-check giữ nguyên hoàn toàn, gồm outputs xấu; profile/hierarchy/extra-body/source identities khớp. Cuối72/72 replay completed; tổng94 observed logical calls,394817 input +8847 output, reservation2317032; gồm3737 paid tokens của failed attempt. Preflight37 tokens riêng. Không reset cost history hoặc rerun sai model answers để green.
- Normal web-enabled semantic review5/10→9/10 ở cả2 candidate repetitions; long cases2/6→5/6. Long input+output82550→19173/19239, giảm76,77%/76,69%; all-normal giảm72,46%/72,39%. Candidate normal1 generation/case,0 compression; maxinput5681. Rubric conservative phát hiện UNKNOWN→unrelated filler echo ở compression-middle (2 lượt), nên gate0 per-case regressions FAIL. Default web-disabled36/36 no-answer vẫn chưa dùng được session-only grounding. Full diagnostic6/6 gold items không phải T8 exhaustive coverage.
- CPU cold/warm đo thật, pinned local models, threads4/batch16/chunks384/exact per-call reuse. Warm p50/p950,554/0,687s; cold p50/p9546,89/80,41s, load14,70s riêng. Cold context109 model invocations/131064 input tokens mỗi encoder/NER; query overhead/resume overhead và high synthetic duplication được tách trong summary. Không gán CPU cost0 hoặc suy total-system monetary savings. Public-list experiment estimate~USD0,02422 gồm preflight, không phải invoice.
- Chốt contract CALIBRATION-POLICY cho kiểm chứng: normal input8192/output4096/one-attempt/SDK retry0/retrieval5s/request60s/work16M; cold job180s/work32M; full provisional12 attempts/262144 total tokens/360s, partial khi cap. Không áp các ceiling này vào production Settings; T6/T7/T8 phải enforce/test và T9 holdout. License audit pinned fine-tune không có grant: UNVERIFIED/NO-GO, không tự suy base MIT.
- Review5 trục: correctness giữ failed/status và strict resume identities, preserves poor answers; readability metrics tách CLI; architecture chỉ eval lane, old featurize/default replay behavior giữ nguyên; security optional cache chỉ1 call, scope/content/model hash, không model weights/env/real chat vào Git; performance batching/exact reuse không bỏ raw chunks, work vẫn tính aggregation/copy. Không schema/model/index/migration DB, public API/FE hoặc app feature activation; optional Torch pin là dependency config change duy nhất thêm ở T4.
- Nguồn/raw outputs trong .data ignore; curated summary +72 answer hashes/review labels +license proof +policy version trong task docs. Single assistant review không phải human/staging/provider-transport certification. Final code checks/commit được ghi handoff, không claim toàn repo lint green khi124 findings cũ vẫn tồn tại.

Final review thêm test RED cho checkpoint thiếu ledger, repeat-resume bỏ mất thời gian failed attempts và thay generation source/dependency/case IDs. Sửa guard/checkpoint, không thay provider answers; không cho output đè input resume. Header v2 lịch sử thiếu bốn generation hashes: không sửa raw cũ; tám source hashes đã ghi và config/profile/hierarchy/extra-body/dependency/case IDs khớp. Bốn file generation ở header cuối khớp Git5db4464 không working diff; guard mới sẽ từ chối resume header cũ thiếu hashes. Checkpoint không chứng minh request còn đang chạy khi process bị kill, cần đối soát trước resume.

Final offline suite: **177 passed, 2 deselected**,12,79s;16 tests T4 PASS; targeted Ruff6 implementation/config/test files (Python subset) PASS; full Ruff124 findings cũ. Optional pins match installed environment, no install needed. Post-check giữ61 successful original rows nguyên vẹn;72 final rows đều completed. Không paid provider rerun sau khi report xong; không chạy holdout. Numeric policy default rollout=False. Git handoff giữ `.serena/` ngoài commit, không tự merge/approve.

## T5–T9 — Tiếp tục hoàn tất theo yêu cầu Thang (27/10/2026)

Base main đã fetch/pull 7772270, nhánh DB mới feat/Thang-session-context-persistence-db có prerequisites T1–T4 tới1cb7cad. Giữ .serena và regression characterization test do worker TDD tạo ngoài DB commit. Không tự push/merge. Đọc workspace rules, task spec/plan/contracts, models/agent.py, get_thread/prepare_ask/persist_answer/filter_messages_for_scope và terminal grounding.

T5 dự kiến hai derived tables: session_context_indexes (thread/owner/scope/model, generation, snapshot digest, state/checkpoint và bounded feature cache); session_message_provenance (message FK cascade, content hash, immutable partition stamp, Source IDs và message dependency refs). Không sửa raw Message/Thread hay migrations cũ; thêm migration0007 up/down và disposable schema tests. Bảng provenance không dùng citation rỗng để suy assistant không phụ thuộc nguồn. Legacy không xác minh được origin phải unknown/unsupported, không giả stamp lịch sử. T5 commit/branch riêng; BE branch mới từ main + dependencies để T6–T9.

T6 adapter keyset toàn lịch sử, stable digest/fence, cache feature theo raw hash/model/scope, checkpoint từng batch; source rows luôn là authority. User mới và assistant mới phải lưu provenance xuyên lượt. Legacy user owner-authorized cần chính sách explicit/restricted và test; assistant legacy không rõ transitive dependencies không được promote. Before dispatch/final publication revalidate scope/expiry/dependencies và mutation generation; deletion cannot resurrect through cache. Tests old/new/>200, resume/duplicate jobs/cancel, mutate/delete/revoke/cross-owner/unknown legacy.

T7 OFF/SHADOW/ON flag mặc định OFF; typed evidence packing nằm lane session riêng, giữ role và citations/span refs, atomic dependency groups; không hạ document grounding hoặc gắn stateless endpoint vào thread. Exact model tokenizer/count bao gồm system/query/tool reserve; normal8192 input4096 output/one attempt/retry0/deadline60. T8 bounded full traversal/map/reduce, source-linked intermediates, coverage ledger, partial text/error existing client contract; không dùng normal top-k làm full. T9 freeze calibration changes rồi chạy holdout một lần có ledger, review quality từng ca; nếu fail giữ NO-GO và report defect thay vì chỉnh ngưỡng/tuning holdout.

NER chưa có grant không được tự approve. Nghiên cứu artifact thay thế có license rõ và test/calibration riêng trước holdout; không gọi source audit là legal approval. Docker CLI hiện không có daemon, Docker Desktop executable không có ở đường cài thông dụng, psql/postgres không trong PATH: PostgreSQL server acceptance chưa có; tiếp tục triển khai và disposable SQLite tests, tìm runtime PG thử migration riêng, không gọi SQLite là concurrency acceptance PG.

### T5 — Schema verification

Review độc lập không thấy schema blocker;7 SQLite tests với FK ON passed1.40s, targeted Ruff PASS. Migration0007 up → message cascade → down giữ raw thread → re-up → thread cascade chạy thật trên PostgreSQL18.6 WASM/PGlite0.5.12, PASS; parent Agent/Thread/Message DDL compile trực tiếp từ ORM PostgreSQL dialect. PGlite package QA chỉ ở .data ignore, không dependency app mới; không dùng kết quả này để chứng nhận server PostgreSQL đa process hoặc production migration. Full offline trước mở rộng test180passed2deselected12.91s. T5 không chạy migration/backfill trên DB thật. T6 phải CAS generation và revalidate stale JSON after message delete; JSON assignment thay vì mutation in-place. Hướng dẫn apply/down tại PERSISTENCE.md.

### T6a — Storage và feature checkpoints

read_records keyset128 duyệt toàn lịch sử, get_thread owner check force refresh, populate_existing cả Message/Provenance để đóng hai identity-map security bugs đã test RED→GREEN. Legacy user chỉ được recover khi issuer continuum-sag-session và đúng1 signed Project: conversation_owner_key của issuer này đã gồm Project, nên không gán membership lịch sử cho general assertion. Legacy assistant vẫn unknown; origin/hash/owner sai không được fallback legacy. Đây là policy explicit có giới hạn, không phục hồi authority assistant từ citation rỗng.

Index cache riêng thread/owner/scope/model, feature raw hash, cache entity offsets validate trước dùng; encoder jobs thread-serialized chỉ share weights, không share user features. Checkpoint từng batch và SQL CAS generation/digest; interrupted/restart tính tiếp những message thiếu, append reuse features cũ, delete/revoke/corrupt artifact không resurrect. Thử race thật bằng pause encode + writer session khác: raw mutate→snapshot_changed; delete+invalidate→generation_changed/stale; overlap same established snapshot→consistent ready. Đồng thời insert first-ever row chưa chứng nhận cross-process; unique constraint fail closed, không claim distributed lease.

17 storage tests PASS;6 index tests PASS sau regression cached out-of-bounds mention RED→rebuild. T6a chưa wire write/delete hooks hoặc dispatch/output lock: T6b/T7 còn thực hiện; read_records một mình không là snapshot transaction. PG multi-process isolation acceptance chưa chứng minh bởi SQLite/WASM. Không backfill chat thật. T6b mutations phải invalidate trong cùng transaction; runtime revalidation và row locks trước output vẫn bắt buộc.

### NER runtime replacement

Review độc lập kiểm pinned OpenMed278M revisionf253fd..., card/API Apache2, base XLMR MIT, source hashes và phạm vi VI/PII; không suy English quality từ backbone. Giữ limitation dataset license metadata unknown. Chọn drop-in này thay vì cài GLiNER đòi Transformers<5.17. Weights cache HF ngoài Git; mapping explicit PER/ORG/LOC bỏ nhóm PII khác và giữ offsets, model fingerprint version hóa. Mapping test RED→GREEN; actual model smoke không sửa PER/LOC expectations vẫnPASS1/16deselected22.35s. T4 model/config/report gốc giữ nguyên; T7/T9 phải đo candidate mới trước holdout. License/attribution và hạn chế tại MODEL-LICENSES.md.

### T7a — Transport, tokenizer và packing

13 actual-SDK/MockTransport tests PASS3.75s, cancellation with partial usage RED→unknown-final accounting, redirects disabledexplicitly. New pure packing3tests PASS2.57s: inert role distinction/querylast/quote authority/atomic dependencies. Full offline229passed2deselected18.17s trên working candidate với integration đang triển khai; không paid model replays/holdout. Qwen tokenizers cachedpinnedb968826d... cầnconfig.json ngoài tokenizerfiles. Model/transport/packing primitives commitriêng5implementation/config/tests. RUNTIME.md xác định optional API mode, additive terminal metadata, visible partial text và OFF/SHADOW/ON contract cho T7/T8; integration/final acceptance chưa được coi hoàn tất.

Actual pinned tokenizer smoke phát hiện Transformers5 trả BatchEncoding mapping: len(result)=2 là số keys, không phải tokens. Test RED1234token→258counter đã thêm; sửa đọc input_ids, GREEN; local Qwen smoke synthetic2words count271 gồm256headroom. Chưa có paid benchmark nào dùng counter sai. Giữ thành regression riêng, không sửa số đo T4 vì T4 dùng providerusage.


### T6b/T7/T8 runtime và regression review

Thang yêu cầu thực hiện toàn bộ T còn lại. Áp dụng flags OFF/SHADOW/ON mặc địnhOFF và additive context_mode theo RUNTIME trước implementation. Runtime nối actual prepare_ask/generate_stream, giữ native runtime runID/cancel/SSE; không sửa ingestion/retrieval tài liệu. 60s/360s deadline chung gồm initial DB/prepare/lease/model/publication, clipped principal expiry. Full raw keyset reader, immutable provenance, checkpoints/CAS và shared row locks/fresh authority bảo vệ input/output; SQLite chỉ xác minh fail-closed interleavings, chưa chứng nhận server PG lock isolation.

Bugs được sửa qua regression: BatchEncoding tokenizer; revoked ORM identity map; query dropped/duplicated; dropped native tool pair/citation; old Source dependency used via generated tool args; cumulative/missing/cancelled usage; unvisited support quoted by reducer; >128 full decisions erased by normal support cap; greetings/time-only unnecessarily UNKNOWN; authorized history internal marker leakage. Encoder single-load lock và serialized inference không share feature dictionaries xuyên user. Budget profile tool_assisted ghi trước protocol test, nguyên one-call pure-memory gate giữ nguyên.

Independent review theo code-review skill: actual native runtime Source/citation/revoke and tool-pressure tests, actual native GetTimeTool, full130decision regression và real signedJWT backfill tests; không dùng doubles làm provider/data-engine/browser acceptance. Initial full suite270PASS2deselected; final counts ghi T9 report. Backfill CLI defaultdryrun, real auth functions, no owner/project override, apply180s/32Mwork/clippedexpiry; chưa chạy dữ liệu thật. Full maps giữ trustedrole/order/time và nguyên quote, partialvisiblenaturalprefix, failure/cancel không claim complete.

Provider verification: verify_session_runtime.py chạy isolated synthetic SQLite với recorded fixture provenance (không promote reallegacyassistant), actual runtime + configured provider. Original T4 immutable. Diagnostic v1 interrupted during review fixes, giữ riêng không dùng nghiệm thu. v2 freezes source hashes, resumable cumulative call/token/wall caps, no rerun completed bad answers. Pairedlegacy OFF và runtime ON, bothwebflags, cold-indexcost táchwarm. Holdout chỉ chạy sau calibration review/freeze, không tuning từ holdout. Detailed evidence và gates bổ sung T9.

T7/T8 public contract, auth revalidation, runtime lease, provenance persistence và native terminal handling được commit atomically: tách riêng hook có thể tạo trạng thái ON xuất dữ liệu trước fence hoặc thiếu provenance. Phạm vi nhiều hơn đề xuất5files trong plan gồm tests/docs; không gom cleanup ngoài task. Lint whitespace cuối không đổi AST (hash trước/sau được giữ ở ignored t9-calibration-v2-ast.json); provider calibration v2 và holdout cùng semantic candidate.


### Browser acceptance defect và calibration pre-holdout correction

Chạy Chrome headless isolated profile với SAGwebdev3002, actual FastAPI8019/signedsession/SQLitefixture, configured provider thật, không intercept/mock HTTP. Trước sửa: model2calls200OK, DBpartialđúng nhưng UI báo Agent event missing required fields; screenshotbefore và client report lưu evidence/browser. Rootcause sanitized MESSAGE_DELTA được dựng thiếuversion; copy event.to_dict() giữversion/timestamp/turn. HTTP regression exact FEparseFrame requiredfields, wiretype/runID/strictsequence: counterfactual bỏđúngdòngcopytrongmemory→RED; actualsource→GREEN. Sau sửa: actualprovider2calls, UI livepartial đúng, reload vẫn đúng; screenshots390/768/1280/1440, errors/exceptions/failedrequests/HTTPerrors đều0. Không sửaFE files; dừngservers/profile sauQA; privatefixturetokenkhôngcommit. Bốn providerrequestsQA nằm ngoài pairedbenchmarkledger, usage khôngđủđểclaimtổngtiền.

Calibrationv2 metadata lộGetTimeTool description chứa truyvấn bị regex legacy research classifier nhận nhầm, nâng pure-memory normal thànhtool_assisted7calls. Actualpaidcalls vẫn1nhưngenforcement/profile sai. New actualnativeGetTime-only/non-timequery regression RED7vs1; excludeget_time khỏiresearchtrigger, time-only vẫnprofile2+calls khiđúngquery; GREEN. Đây là sửa trướcholdout; giữv2làdiagnostic, finalcalibrationv3 chạycandidate mới và giữbaselinev2đãhoànthành, khôngresamplebadrowslàmcost/qualityPASS.

Harnesstốiưu benchmark riêng: syntheticindex-templatecopypercase để profilewebthứ2 dùngcacheđúngrawhash/model/scope trongDBmới, khôngcarryanswer giữareplays; coldCPUreportchỉfirstbuild. Bổsung--variants đểkhôngtrảphírerunbaselinekhôngđổi, lưurawsyntheticoutputtrongledger đểphânbiệtJSON/provenancefail vàsemanticabstention. Không thêmrawloggingproduction. SiliconFlowprimarydocs response_formatjson_object verifiedhttps://docs.siliconflow.cn/docs/api/chat-completions-post; chưaápJSONmode từsuyđoánmodel-specificbehavior. Numericgateskhôngđổi.


### Freeze cuối trước holdout — reference protocol v2

Theo lỗi calibrationv2 mapquote khôngkhớp và plainanswer JSONparsefail, model chỉchọn supportsID; backendmaterializefullauthorizedspan. Legacyquotedpairvẫnstrict; decoderdefaultkhôngnới, optininternal. Reducer_expandchỉtừmappedcanonicalpairs, khôngtừmọivisitedID. Suppliedbadquote/unknownID/visitedbutunmapped/batchoutsideIDkhôngđượcrepair. Independentreview thêm6protocolcases+2native fullruntimecases:20PASS,RuffPASS; fullsuite trướcfinalreview288PASS2deselected. ActualJSONmodeQwenSDKpreflight2calls209input10output; finalreferenceprotocol browserrepeat thêm2requests, livepartial/reload/all4viewports/noerrorsPASS. TổngbrowserQA6requests+t2preflight nằmngoàipairedevaluation; khônggiảusage/costcomplete. FeatureflagOFF.

Finalcalibrationv3 started/resumed durable output, finalsourcehashfixed. Holdoutqueuedchỉsaucalibrationcommandkếtthúc, chạymộtlầnpairedlegacy/runtimebothwebflags. Khôngsửacaps/NER/prompt/decoderđểkhớpholdout. v2diagnostic48rows56calls1593s/accountingincomplete đượcgiữnguyên; finalcandidatebaselinepairdùngbaselinev2nhưngmọifailed/unknownpairkhôngđượccostPASS. V3harnesscopyindexpercase(profile2)trongfreshfixtureDBkhôngcopygeneratedanswers; coldCPUaccountseparate. RolloutdecisionghiT9saukhiđủresults,khôngclaimimplementation=acceptance.

Calibrationv3 trảcorrectanswers nhưng5normalrows supportsstringID thayobjectID, decoderstrict→UNKNOWN. Dừngv3 trướcholdout, xácnhận khôngholdoutfile/process. Regressionstringreference RED→normalizationoptinGREEN, unknown/unselected/badquotevẫnfailclosed. ReducerstringID dùngchỉmappedcanonicalpairs. V4calibrationchạylạisourcefixed; v3rawdiagnostic khôngsửa; khôngthaygold/caps/qualitygate. Fullsuite296PASS trướcserializationpatch; focusedpacking/aggregate/full27PASSsaupatch. FinalholdoutchỉbắtđầusaukhiV4calibrationkếtthúc/đãreview,khôngqueuedtựđộng.


### Native PostgreSQL available during T9

Môi trường thayđổi: Dockerdaemon29.8.0 vàpostgres16imageđãcósẵn. Không đụngsag-db-1/API/webcontainers đangchạycủangườidùng. Tạo riêng --rmcontainer sag-session-context-qa,127.0.0.1:55439,tmpfsdata, dedicatedDB/user session_qa,trustauthchỉQAloopback. Opt-in tests/test_session_context_postgres.py khôngdùngsettings.database_url; fixed dedicatedendpoint. TạocurrentlegacyORMtablesrồiexecuteactualmigration0007file, khôngmigrateDBthật.

Actual PostgreSQL two connections: runtime SessionContext.lease FORSHARE blocksmessageDelete/threadDelete/indexInvalidate/mappingRevoke; separatepersist_answer session vẫninsertanswer+provenance được tronglease (5stimeout,no selfdeadlock). Writercommitsauleaseclose, subsequentrevalidation/revokeaccessrejects. Actualdown0007 giữ2rawmessages rồireapplyemptyderived. Initial5PASS21.84s, exactrerun/PGversionghiT9report. Đây làserverlockevidence,khôngclaimarbitrarydistributedload/deploymentacceptance. ContainerQAstoppedremoved sauverification; usercontainersuntouched.

NativePGexactrerun5PASS20.20s, server16.15Debian; QAcontainerstopped--rmconfirmed, userDockercontainersuntouched. Fullsuite304PASS5optinPGskipped2ai_smokedeselected97.66s trongmôitrườngđangcontendedCPU; targetedRuffPASS. Calibrationv4coldmiddle2profileschạm180s, giữstatuserror/TimeoutError vàkhôngdiễn0LLMcallsnhưcostsaving. Kiểmđếmkhôngweights:180messages,278chunkrequestsdeduptrongbatchvs201uniquejobchunks; chưathêmcrossjobcache/tuningpipelineđểđạtdataset. Feature/caps/promptfrozen. V4normalsemanticreviewđộclập,chưađọcholdout.

Finalcode-review nêu sleep0.2 khôngđủchứngminhwriterđãsubmitSQL. Đãthaytest bằngwriterpg_backend_pid + parentpollpg_blocking_pids cardinality>0 trong5s trướcpublication; khôngđổiproduction/sourcehash. NativePGexactstrengthened5PASS18.08s,RuffPASS; dedicatedcontainerđãremoved. DBauditcũđãđánhcheckpointT5 vàtrỏT9tránhmâuthuẫn. Calibv4đủ24rows; holdoutv4startedonceaftersourcefreeze vàcalibreview, khôngtunetừholdout.

Finalopsreview phát hiệnexistingcore/db.pyinit_dbcreate_allunconditional. Modelsnewaggregatecanautocreate2tablesatstartup evenOFF; taskkhôngstartrealAPInewbinary. DocsPERSISTENCE/HANDOFF/PRbodyđãchỉrõmanual0007beforestartup vàOFFkhôngguardDDL. Khôngđổibootstraplegacyngoàiscope/khôngsửaproductionfrozencode.

T9final:calibration24runtime36calls445159input1137output;holdout48paired67calls694252input4449outputaccountingcomplete2083.86s. Source/config/caps/models/dependenciesfrozenidentical. Holdoutreview22child+2parentafterchildusagelimit,16/24correct(normal16/20full0/4),4falseabsence0proposalpromotion. FullstrictsemanticFAIL; longnormalLLMtoken-73.81%/-77.99%4cases/profileincludingbadanswers,fullcostseparate. No-tunefromholdout,noresamplebadanswers. RawfinalsyntheticJSON+hashes/semanticreviews/NO-GOatVERIFICATION,evidence; noAPIkeys/rawrealchat. T9executioncomplete, productqualitynotachieved;OFF,nopush/merge/realDBmigration/backfill.


### Consolidation PR38/39/40 theo yêu cầu trực tiếp của Thang

Thang yêu cầu gộp cả ba PR thành một PR để tiếp tục sửa trên cùng nhánh, đóng hai PR còn lại và mô tả đầy đủ. Đây là ngoại lệ được người dùng cho phép cho quy trình tách PR DB/API trước đó. Fetch/inspect main và cả ba remote heads: PR38=5db4464, PR39=74971ba, PR40=df0c904. git merge-base --is-ancestor xác nhận cả38/39 là ancestors của40; diff schema/migrations39→40 bằng0. Không cần cherry-pick, rebase, merge thêm hoặc force-push; giữ toàn bộ commit history.

Giữ PR40/nhánh feat/Thang-session-context-runtime-be-api, targetmain, draft/NO-GO/OFF. Chỉ cập nhật tài liệu PR-BODY/HANDOFF và ghi archive context cho DB PR body; production code/sourcehash/fixtures/gates không đổi. Publish mô tả hợp nhấtT1–T9 và đóng38/39 sau khi xác minh remote code đủ, khôngxóanhánh/mergePR/approve/deploy/migrate/backfill. Kiểm docs/diff/ancestry; không rerun paid/provider tests cho thay đổi chỉ docs. Các số test/holdout bên dưới là bằng chứng từ lần triển khai đã ghi, không giả thành lần chạy mới.

## Sửa lỗi sau review độc lập PR #40 (27/10/2026) — Kế hoạch trước implementation

Tiếp tục trên nhánh `feat/Thang-session-context-runtime-be-api` từ head `9c243af9182886387b808b2f6df5786f9ad0439a` (base `main` `77722700fa8ed83112bff6640213b12d0235c11c`). Không tách PR, không rebase/reset/force-push, giữ `.serena/` và giữ `SAG_SESSION_CONTEXT_MODE=off`. Không sửa các calibration/holdout evidence artifacts đã đóng băng.

### Kế hoạch sửa theo từng finding

1. **F1 — `full_session` bypass explicit `@Source` filter (`agent_domain.py`, `session_context/runtime.py`, `agent_service.py`)**:
   - Phân biệt rõ `source_ids=None` (không truyền filter), `source_ids=[]` (filter rỗng hợp lệ: chỉ lấy message không phụ thuộc Source), và `source_ids` không rỗng (`bool(source_ids) is True`: người dùng yêu cầu lọc theo `@Source`).
   - Vì `full_session` hiện tắt document retrieval tools (`host_tools = []`), tổ hợp `mode == "full_session"` (cả explicit và auto-routed qua `select_mode`) kèm `source_ids` không rỗng phải bị từ chối tường minh bằng `ValidationError(..., code="session_context_source_filter_unsupported")` (HTTP 422) **trước** khi lưu `user_msg` trong `prepare_ask` (khi `session_context_mode == "on"`, đồng thời kiểm `resolved` attachments trước khi lưu `user_msg`) và **trước** mọi provider attempt trong `prepare_context` / `generate_stream`. Ở chế độ `shadow`, nếu tổ hợp không hỗ trợ thì bỏ qua nhánh shadow thay vì làm hỏng legacy ask.
   - Tại final grounding gate (`agent_service.py:1038–1039`), bỏ miễn trừ `or memory.selection.mode == "full_session"` khi có `plan.source_ids` hoặc `memory.scope.requested_source_ids` không rỗng.

2. **F4 — Thứ tự khóa PostgreSQL giữa `SessionContext._lease` và `delete_message`/`invalidate` (`session_context/runtime.py`)**:
   - Chuẩn hóa thứ tự khóa trên mọi đường đọc/ghi: `Thread` → `SessionContextIndex` → `Message` → `SessionMessageProvenance` → `Source` → `SourceProjectMapping`.
   - Trong `SessionContext._lease`, chuyển câu lệnh `SELECT ... FROM session_context_indexes WHERE id = ... FOR SHARE` lên ngay sau `Thread` và trước `Message`/`SessionMessageProvenance`.
   - Khi `delete_message` và `context.lease` đua tại thời điểm acquire: nếu `_lease` giữ `SessionContextIndex` trước thì `invalidate` chờ tới khi lease đóng; nếu `invalidate` giữ `SessionContextIndex` trước thì `_lease` chờ trên `SessionContextIndex` khi chưa giữ `Message`, sau đó `revalidate` fail-closed (`index_generation_changed`/`snapshot_changed`) mà không bao giờ tạo chu trình chờ `SQLSTATE 40P01`.

3. **F5 — Ledger `dispatched_evidence` tách biệt với `visited`/`resolved` (`session_context/runtime.py`, `session_context/aggregate.py`, `session_context/model.py`, `agent_service.py`)**:
   - Tách riêng: (a) `original` (eligible/selected spans), (b) `request_evidence` (tập `ResolvedEvidence` của request map/reduce hoặc normal fit hiện hành, đã đóng bắc cầu message/Source dependencies), (c) `dispatched_evidence` (hợp lũy kế các `ResolvedEvidence` đã qua boundary revalidation + budget reservation ngay trước provider attempt), (d) `visited` (chỉ cập nhật sau khi map batch trả về và `_items` validate thành công), (e) final `supports` đã validate.
   - Trong `aggregate_session`, gọi `context.stage_request(evidence)` trước map `llm.complete(messages)` và `context.stage_request(visited_evidence)` trước reduce `llm.complete(messages)`, rồi xóa `stage_request(())` khi thoát.
   - Trong `BudgetedLLM._create_completion`, thực hiện `before_dispatch()` (revalidate + lock_sources) và `budget.reserve_call(...)` trước khi ghi nhận dispatch và append call record; nếu `reserve_call` từ chối trước attempt thì rollback snapshot `dispatched_evidence` của lần gọi đó để không ghi nhận nhầm request chưa gửi.

4. **F6 — Kiểm tra toàn vẹn manifest đóng băng khi resume trong `scripts/verify_session_runtime.py`**:
   - Dùng chung hàm dựng manifest (`build_runtime_manifest`) cho cả run mới và resume, bao gồm đầy đủ source hashes (gồm cả `sag_api/generation/llm.py`, `sag_api/generation/prompt.py`, `sag_api/services/eval/session_context_accounting.py`, `scripts/eval_session_context.py`), `corpus_sha256`, `split`, `case_ids`, `variants`, `caps`, `config`, `provider_extra_body_sha256`, `models`, `dependency_versions`.
   - Hàm `validate_runtime_resume(report, expected)` kiểm tra nghiêm ngặt mọi trường bắt buộc trước bất kỳ provider attempt nào và trước khi ghi đè file output; thiếu trường hoặc lệch bất kỳ giá trị nào đều raise `ValueError`. Giữ nguyên kết산 lũy kế `calls`, `reserved_tokens`, `elapsed_seconds` khi resume hợp lệ.

5. **F2/F3 — Sửa lỗi deterministic orchestration/fallback và cold-index reuse có thể chứng minh (`session_context/aggregate.py`, `session_context/index.py`, `session_context/graph.py`, `agent_service.py`, `scripts/verify_session_runtime.py`)**:
   - Trong `agent_service.py`: khi `memory.selection.mode == "full_session"`, `host_tools` đã bị tắt (`[]`), nên không gắn `scene_notes` ép gọi `search_context`/`web_search`/`open_webpage` vào `system_prefix` của map/reduce.
   - Trong `aggregate.py`:
     - Làm rõ `MAP_RULE` và `REDUCE_RULE`: phân biệt phủ định tường minh có căn cứ của người dùng với trường hợp batch không có kết quả khớp (`{"items":[]}`) hoặc câu mô tả hoạt động thường ngày; reducer bắt buộc trả JSON `{"answer": ..., "supports": ...}` (không trả mảng `items`) và không suy diễn việc thiếu intermediate item thành kết luận phủ định toàn phiên.
     - Khi reduce không chạy hoặc thất bại (`synthesis_not_ready`, budget cap), fallback chỉ được phép nối các trích dẫn gốc đã xác thực (`support["quote"]` từ `supports`), tuyệt đối không xuất trực tiếp `item["text"]` trung gian chưa qua bước tổng hợp (vốn có thể chứa câu phủ định toàn cục do map tự suy diễn).
   - Trong `index.py` và `graph.py`: truyền `chunk_cache` cục bộ trong phạm vi một lần gọi `sync_index` (cùng một `SessionScope`, hủy ngay khi hàm kết thúc, không chia sẻ xuyên scope/request) vào `LocalFeatureEncoder.featurize_many(..., deduplicate=True)` để tái sử dụng exact 384-codepoint chunks giữa các batch checkpoint 16-message của cùng một job cold-index; trong `verify_session_runtime.py`, lưu lại cả checkpoint `SessionContextIndex` đang ở trạng thái `indexing` vào `INDEX_TEMPLATES` của harness khi hết thời gian để lần chạy kế tiếp tiếp tục từ checkpoint thay vì mất trắng.
   - Không tuyên bố chất lượng ngữ nghĩa của model trên F2/F3 đã đạt khi chưa chạy đợt đánh giá live provider mới trên tập validation độc lập; giữ trạng thái rollout **NO-GO** và `SAG_SESSION_CONTEXT_MODE=off`.

### Kết quả thực thi và kiểm chứng (27/10/2026)

- **Hoàn thành sửa code trên `feat/Thang-session-context-runtime-be-api`**:
  - **F1 (`agent_domain.py`, `session_context/runtime.py`, `agent_service.py`)**: Dùng `select_mode` trong `prepare_ask` để kiểm cả explicit và auto-routed `full_session` trước khi lưu `user_msg`; khi `session_context_mode == "on"`, từ chối `resolved` attachments (`session_context_text_only`) và `full_session` kèm `source_ids` không rỗng (`session_context_source_filter_unsupported`, HTTP 422) trước mọi side effect DB; ở `shadow` mode thì bỏ qua nhánh shadow thay vì làm hỏng legacy ask. Cặp `source_ids=None` (không truyền filter) và `source_ids=[]` (chỉ lấy message không phụ thuộc Source) giữ nguyên hành vi hợp lệ. Bỏ miễn trừ `or memory.selection.mode == "full_session"` tại final grounding gate (`agent_service.py:1040`) khi có `plan.source_ids` hoặc `memory.scope.requested_source_ids`.
  - **F4 (`session_context/runtime.py`)**: Chuyển `SELECT ... FROM session_context_indexes WHERE id = ... FOR SHARE` trong `SessionContext._lease` lên ngay sau `Thread` và trước `Message`/`SessionMessageProvenance`, đồng bộ thứ tự khóa với `delete_message(..., invalidate)`.
  - **F5 (`session_context/runtime.py`, `session_context/aggregate.py`, `session_context/model.py`, `agent_service.py`)**: Thêm `request_evidence`, `stage_request(evidence)`, và callback `on_dispatch=context.note_dispatch` trong `BudgetedLLM` ngay sau khi `before_dispatch()` và `budget.reserve_call(...)` thành công. `persist_answer` ghi nhận hợp của `dispatched_evidence` (gồm cả map batch thứ 2 bị hỏng JSON ở output) và final `supports`; các batch bị chặn bởi `max_calls` trước dispatch không bị đưa vào provenance.
  - **F6 (`scripts/verify_session_runtime.py`)**: Bổ sung `build_runtime_manifest(args, cases)` (thêm hashes cho `llm.py`, `prompt.py`, `session_context_accounting.py`, `eval_session_context.py`, cùng `case_ids`, `variants`, `corpus_sha256`, `caps`, `config`, `provider_extra_body_sha256`, `models`, `dependency_versions`) và `validate_runtime_resume(report, expected)` kiểm tra toàn bộ trường đóng băng trước mọi provider call hoặc ghi file.
  - **F2/F3 (`session_context/aggregate.py`, `session_context/index.py`, `session_context/graph.py`, `agent_service.py`, `scripts/verify_session_runtime.py`)**: Tắt `scene_notes` ép gọi `search_context`/`web_search` khi `full_session`; siết `MAP_RULE`/`REDUCE_RULE` và sửa fallback khi reduce lỗi/skip chỉ nối trích dẫn gốc `support["quote"]` đã xác thực (không lộ `item["text"]` trung gian chưa tổng hợp); thêm `chunk_cache` cục bộ theo từng job `sync_index` (cùng `SessionScope`, giải phóng khi thoát hàm) và giữ `SessionContextIndex` đang `indexing` trong `INDEX_TEMPLATES` của harness.
- **Bằng chứng kiểm tra**:
  - Offline test suite (`pytest -q -m "not ai_smoke"`): **309 passed, 6 skipped, 2 deselected in 28.70s**.
  - Reproduction suite offline (`run_repros.py`): **3 passed, 1 skipped in 11.55s**.
  - Native PostgreSQL 16.15 disposable container (`1e0e43bd3c51387483f4ee4cdae3bac7f172d4b5ea712c567fff441129b8e2be` trên `127.0.0.1:55439`, đã dừng và xóa sau QA; không chạm DB/containers của người dùng):
    - `pytest tests/test_session_context_postgres.py -q`: **6 passed in 14.30s**.
    - `run_repros.py --native-pg`: **4 passed in 13.90s** (0 deadlock `40P01`).
  - Targeted `ruff check` và `git diff --check`: **PASS (0 lỗi)**.
  - Giữ nguyên `SAG_SESSION_CONTEXT_MODE=off` và toàn bộ calibration/holdout evidence artifacts lịch sử không đổi. Rollout vẫn **NO-GO** cho tới khi chạy đánh giá live provider mới trên tập validation độc lập.
