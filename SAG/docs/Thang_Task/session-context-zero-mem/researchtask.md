# Research: Context toàn session chat

Ngày: 2026-10-26. Record gồm nghiên cứu/đặc tả ban đầu và T1 baseline được Thang yêu cầu sau đó. Selector mới chưa implementation.

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
