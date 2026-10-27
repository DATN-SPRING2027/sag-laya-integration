# Checklist: Context session v1

Trạng thái: T1–T3b prototype và T4 measurement/numeric contract đã thực hiện theo yêu cầu Thang. T5–T8 đã tích hợp và kiểm thử offline; T9 đã đo xong calibration/holdout trên cùng bản cố định: acceptance FAIL/rollout NO-GO; xem VERIFICATION. Không bật rollout. Theo [plan](plan.md), [SPEC](../SPEC.md), [CALIBRATION](../CALIBRATION.md) và các tài liệu liên kết cùng task.

## Chuẩn bị đã làm

- [x] Ghi D1–D7 từ xác nhận trực tiếp của Thang.
- [x] Ghi Q1: cả chat cũ và mới.
- [x] Ghi Q2: full-session cho phép multi-call có giới hạn.
- [x] Ghi Q3: đo baseline trước rồi chốt ngưỡng, đánh giá full-session riêng.
- [x] Fetch/pull main và tạo nhánh tài liệu mới.
- [x] Đọc luồng ask/history/compression/runtime/evidence/ownership/usage.
- [x] Ghi rõ provenance gap, missing full-request usage và legacy owner limitations.
- [x] Soạn SPEC, researchtask và plan/checklist.

## Tiến độ triển khai

- [x] T0: Thang cho phép phạm vi T1 baseline sau khi chốt yêu cầu. Thiết kế selector/DB/API ở T2 trở đi vẫn cần review, chưa coi là đã duyệt.
- [x] T1: Version corpus/gold; observer compression/generation, usage missing và observed deployment failures; chạy 24 replay calibration qua provider thật. Giới hạn SDK retry/billing visibility và giá tiền chưa xác minh được ghi rõ trong BASELINE.
- [x] T2: Hợp đồng nội bộ snapshot, typed evidence/coverage/budget và dependency/ACL validators/tests; xem [CONTRACT](../CONTRACT.md). Chưa có production caller hoặc durable fence.
- [x] T3a: Fixture reader/hierarchy, phân trang toàn snapshot, không last-N; structural tests và work budget. Chưa SQL adapter/semantic segmentation/production integration.
- [x] T3b: Graph/fusion/closure prototype, pinned local encoder/NER smoke và offline tests. Quality calibration và NER production-license review vẫn còn trước rollout.
- [x] T4 measurement/contract: calibration và numeric gates khóa trước holdout; xem [CALIBRATION](../CALIBRATION.md). **Không phải acceptance PASS**: filler-echo regression/default grounding/NER license chưa đóng; rollout vẫn NO-GO.
- [x] T5: Schema/migrations0007/down riêng, SQLiteFK và PostgreSQLWASM up/down; chưa push/PR/serverPGconcurrency acceptance.
- [x] T6a: Backfill chat cũ, incremental chat mới, restart/resume.
- [x] T6b: Delete/revoke/generation fences và request leases; serverPG đa tiến trình còn là gate riêng.
- [x] T7: Normal-mode thread ask, prompt groups, feature flag, legacy OFF.
- [x] T8: Full-session bounded synthesis, coverage ledger, native partial/cancel; browser fixture livepartial/reload đã kiểm chứng (evidence/browser).
- [x] T9 measurement/review: holdout48/48, provider/HTTP/browser/nativePG/rollback đã kiểm chứng và báo cáo. Acceptance FAIL, không đồng nghĩa hoàn tất mục tiêu chất lượng.

## Điều kiện nghiệm thu bắt buộc

- [ ] Recall được message hợp lệ ở đầu session rất dài, không phụ thuộc last-8/last-200.
- [ ] Đúng quyết định hiện tại và giữ provenance khi có thay đổi/mâu thuẫn.
- [ ] Câu tiếp nối có đủ ngữ cảnh; assistant suggestion không bị coi là user approval.
- [x] Validators/JWT/HTTP/SQLite và nativePG tests chặn cross-owner/thread/project và evidence revoke/delete; không thay thế kiểm chứng workload triển khai.
- [x] Protocol tests: stale/failed/cold/cancel không trở thành complete success. Coverage ready vẫn không chứng minh semantic correctness (T9 đã phát hiện lỗi).
- [ ] Mọi model request/retry/compression/synthesis được tính usage; unknown khác zero.
- [ ] Không giảm chất lượng theo ngưỡng ghim trước holdout; cải thiện nhớ thông tin cũ.
- [x] Đã đo long-normal holdout token giảm73,81%/77,99%; fullsession báo riêng, qualitygateFAIL chặn rollout.
- [x] Client SAG thật hiển thị partial/live/reload qua API/provider thật, không mock HTTP; Continuum embed riêng chưa chứng nhận.
- [x] Không thay retrieval/ingestion tài liệu hoặc BE identity ngoài scope.

## Handoff triển khai sau này

- [ ] Relevant tests, lint và integration checks hoàn tất; không coi mock là production acceptance.
- [x] Git status/diff/history và secrets/generated/debug review; giữ .serena/ không liên quan.
- [x] DB impact/migration/rollback và config/model/license ghi rõ ở PERSISTENCE/RUNTIME/MODEL-LICENSES/HANDOFF.
- [x] Agent code review/regression hoàn tất; vẫn chờ con người review/CI/merge, không tự approve.
- [x] Nhánh/commit, checks, hạn chế, exact push command ở HANDOFF; không tự push.

Không checkbox implementation/benchmark nào được đánh dấu chỉ vì tài liệu này đã được viết.
