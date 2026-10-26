# Kế hoạch đề xuất: Context session v1

**T1 đã thực hiện theo yêu cầu Thang:** corpus, observer, replay và calibration provider thật. Kết quả/giới hạn ở [BASELINE](../BASELINE.md). Selector T2–T9 chưa triển khai; thiết kế cần review cùng [SPEC](../SPEC.md). Không mở rộng thành thay retrieval tài liệu hay chat chéo session.

## Thứ tự và phụ thuộc

```text
T0 Review đặc tả
  -> T1 Baseline/accounting
  -> T2 Hợp đồng snapshot, scope, provenance, budget
  -> T3 Prototype selector trên corpus offline
  -> T4 Calibration và chốt ngưỡng số
  -> T5 DB derived-index/provenance (nếu cần, PR riêng)
  -> T6 Backfill, lifecycle, deletion và concurrency
  -> T7 Tích hợp normal-mode vào thread ask
  -> T8 Tổng hợp toàn-session có giới hạn
  -> T9 Holdout, integration, review và rollout gate
```

Thực hiện tuần tự; task lớn phải tách thành PR nhỏ trước khi code. Không yêu cầu thêm agent hoặc tạo chat mới. Các đường dẫn `services/`, `generation/`, `core/` dưới đây tương đối với `SAG/apps/api/sag_api/`; `tests/` và `scripts/` tương đối với `SAG/apps/api/`. File mới luôn được đánh dấu đề xuất, không coi là đã có.

## T0 — Review đặc tả và ranh giới

- Acceptance: D1–D7 được giữ; xác định phần paper-inspired và phần mở rộng; ghi quyết định provider/dependency còn phải nghiên cứu.
- Verify: review SPEC/researchtask, không còn phát biểu coi đề xuất là tính năng có sẵn.
- Files: `SPEC.md`, `researchtask.md`, `tasks/plan.md`, `tasks/todo.md` trong task này.
- Dependencies: không. Scope: 4 docs.

## T1 — Baseline có accounting đầy đủ

- Acceptance: fixture Việt/Anh và gold evidence/answer được version; legacy compression và runtime đều có usage/call accounting; missing usage không tính là 0.
- Verify: test harness offline bằng provider double; live baseline là bước riêng được kiểm soát sau khi có cấu hình và ngân sách. Ghim model/settings, cold/warm, normal/full-session.
- Files đã thực hiện: `scripts/eval_session_context.py`, `tests/fixtures/session_context.jsonl`, `tests/test_session_context_baseline.py`, `services/eval/session_context_accounting.py`, `generation/llm.py`.
- Dependencies: phạm vi T0/T1 đã được Thang cho phép. Scope thực tế 5 files implementation/data; thay đổi production chỉ là option stream usage mặc định tắt. Không đặt instrumentation vào runtime chung. Lệnh chạy ở BASELINE.

## T2 — Hợp đồng context, snapshot và provenance

- Acceptance: định nghĩa typed selection/coverage/budget; source/message dependency đóng bắc cầu; xác minh authorized snapshot trước traversal và trước model output.
- Verify: test cross-owner/thread/project, revoke, missing legacy provenance và snapshot mutation; phân biệt trạng thái unsupported/partial/indexing.
- Files đề xuất: `services/session_context/types.py`, `services/session_context/scope.py`, `tests/test_session_context_scope.py`, SPEC.
- Dependencies: T1. Scope: 4 files. DB field cần thiết chỉ đề xuất ở bước này, chưa áp schema.

## T3 — Prototype selector không gọi generative LLM

Tách thành T3a và T3b, không gom quá 5 files mỗi phần:

- **T3a:** reader phân trang + hierarchical units; files đề xuất `services/session_context/reader.py`, `hierarchy.py` và 2 test files. Verify replay vượt 200 message, spans/thứ tự/IDs ổn định, mọi eligible message thuộc phạm vi nhớ.
- **T3b:** graph retrieval + fusion/closure; files đề xuất `services/session_context/graph.py`, `selection.py` và 2 test files. Verify golden ranking/closure có bridge, query nối, không entity anchor và boundaries.
- Prototype dùng fixture store; chưa biến thành runtime memory ephemeral không bền. Encoder/NER phải nghiên cứu API/model/version và dependency trước khi cài; ghi sai khác nếu đổi so với phương pháp tham chiếu.
- Dependencies: T2, T3b sau T3a. Không dùng LLM để tạo memory summaries trong normal selection.

## T4 — Calibration và chốt ngân sách/ngưỡng

- Acceptance: baseline đủ để đề xuất token/call/time/index-work caps hữu hạn; ghi ngưỡng chất lượng, old-information recall và savings riêng normal/full-session trước holdout.
- Verify: replay calibration, kiểm tổng chi phí kể cả non-stream/retries/encoder và sensitivity; không xem holdout để chỉnh ngưỡng.
- Files: harness T1, fixtures calibration và báo cáo metrics trong `.data/`; decision update tại task docs.
- Dependencies: T1–T3. Checkpoint: không tích hợp/bật mode mới khi budget/threshold chưa chốt.

T1 phát hiện default knowledge-only trả no-answer cho câu hỏi history khi không có Sources; T2 phải chốt typed evidence/grounding cho session, không chỉ retrieval history. Corpus baseline và holdout chưa phải bằng chứng production ACL.

## T5 — Persistence riêng, chỉ khi có DB impact

- Acceptance: duyệt logical schema/generation/dependency contract; thêm migration mới, up/down và tests; derived index dựng lại được, không thay raw messages/owner authority.
- Verify: disposable database upgrade/rollback, isolation, delete/invalidation, restart/rebuild; report impact trước merge do người có quyền thực hiện.
- Files: model, migration mới, rollback script khi cần, persistence tests; chốt tên/số migration theo repo state ở thời điểm thực hiện, không đoán trước.
- Dependencies: T2–T4. Nhánh đề xuất riêng `feat/Thang-session-context-db`; PR DB target main, không merge/push tự động.

## T6 — Chat cũ/mới, lifecycle và concurrency

- Acceptance: idempotent backfill có resume/checkpoint; new messages index incremental; delete/revoke không trả lại nội dung từ stale index/cache.
- Verify: old/new chat, duplicate jobs, crash, concurrent ask/delete/publish, stale-generation fences và owner chưa map.
- Chia thành: T6a storage/index synchronization (tối đa 4 files); T6b write/delete hooks vào `agent_domain.py` và lifecycle tests (tối đa 4 files).
- Dependencies: T5 nếu cần, T4 luôn bắt buộc. Không chạy backfill chat thật trong task tài liệu.

## T7 — Tích hợp normal-mode vào thread ask

- Acceptance: không dùng last-N làm phạm vi nhớ; query không bị đưa hai lần; actual fitted prompt giữ dependency groups/coverage và tool protocol; OFF tái hiện legacy.
- Verify: integration endpoint, mock/provider contract tách riêng; provider thật để xác minh context-window/usage sau đó. Stateless OpenAI endpoint không bị gắn memory bền ngoài phạm vi.
- Files dự kiến: `services/agent_domain.py`, `services/agent_service.py`, `core/config.py`, `tests/test_session_context_ask.py`; tách nếu cần đổi public contract.
- Dependencies: T6. Backend branch mới từ main cập nhật: `feat/Thang-session-context-be-api`.

## T8 — Chế độ toàn-session có coverage ledger

- Acceptance: duyệt đầy đủ snapshot hợp lệ, provenance trung gian, multi-call synthesis hữu hạn; partial/cancel không được nhận là hoàn tất.
- Verify: list-all golden set, lô cuối còn quyết định quan trọng, conflict qua lô, budget exhaustion, missing usage và client hiển thị partial thật.
- Files đề xuất: `services/session_context/aggregate.py`, mode selection/integration module, `tests/test_session_context_aggregate.py`, contract doc nếu cần; chia nhỏ nếu vượt 5 files.
- Dependencies: T7. Cần public SSE/API thay đổi thì contract-first; FE branch/PR riêng nếu cần, visual QA theo workspace lúc đó.

## T9 — Holdout và handoff

- Acceptance: đạt các ngưỡng đã ghim ở T4, sửa được nhóm nhớ cũ, không leakage/deletion regression; chứng minh savings normal session dài và báo riêng full-session.
- Verify: focused + full offline suite, Ruff, provider/integration smoke có kiểm soát, cold/warm measurements, rollback drill. Không sửa test che lỗi.
- Files: báo cáo nghiệm thu, decision record, config example placeholders, tests sửa theo defect thật; không gom refactor ngoài scope.
- Dependencies: T8. Chỉ lúc đạt gate mới đề xuất bật; con người quyết định rollout/merge.

## Lệnh kiểm tra khi triển khai

Từ `SAG/apps/api` với environment đã cài đúng dependencies:

```powershell
python -m pytest -m "not ai_smoke" -q
python -m ruff check sag_api sag_agent tests
```

Chạy focused tests bằng path vừa được tạo ở task tương ứng trước khi mở rộng. Không gọi harness mới trước khi hiện thực và kiểm `--help`; lệnh provider smoke/live benchmark phải được ghi chính xác trong báo cáo task thực hiện.

## Risks và rollback

- Provenance xuyên history hiện còn gap: phải đóng trước recall nhạy cảm, không chỉ thêm vector search.
- Legacy thiếu owner mapping: không index bằng quyền đoán; report blocked records.
- NER/encoder tiếng Việt: đo trên corpus thật/fixture chuẩn; không assume model tiếng Anh đủ tốt.
- Source mutation/ACL invalidate dẫn xuất: read revalidation + generation fence; test race.
- Giá trị token/call/time chưa biết: T4 là gate, không có default unlimited.
- OFF/SHADOW/ON là cấu hình đề xuất; shadow không tự gọi thêm LLM. Rollback tắt mode và jobs, giữ lịch sử gốc; down schema thực hiện riêng sau khi dừng readers.

## Git/handoff

Nhánh đặc tả ban đầu là `feat/Thang-session-context-spec-be-api`. T1 thực hiện ở nhánh mới `feat/Thang-session-context-baseline-be-api` từ main đã fetch/pull, giữ nguyên gói tài liệu đã soạn. Các nhánh triển khai tương lai tiếp tục tạo mới từ main cập nhật. Không tự push, merge hoặc approve PR. PRs target main và ghi scope/tests/database/config/security/risk/rollback.
