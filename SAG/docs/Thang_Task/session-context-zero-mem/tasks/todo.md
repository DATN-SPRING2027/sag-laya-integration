# Checklist: Context session v1

Trạng thái: T1–T3b prototype và T4 measurement/numeric contract đã thực hiện theo yêu cầu Thang. Chưa tích hợp/đạt nghiệm thu runtime; T4 rollout gate vẫn NO-GO. Theo [plan](plan.md), [SPEC](../SPEC.md), [CALIBRATION](../CALIBRATION.md) và các tài liệu liên kết cùng task.

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
- [ ] T5: DB PR/migrations/rollback riêng nếu cần persistence mới.
- [ ] T6a: Backfill chat cũ, incremental chat mới, restart/resume.
- [ ] T6b: Delete/revoke/concurrency/generation fences.
- [ ] T7: Normal-mode thread ask, prompt groups, feature flag, legacy OFF.
- [ ] T8: Full-session bounded synthesis, coverage ledger, partial/cancel behavior.
- [ ] T9: Holdout + provider/integration/lifecycle/rollback verification.

## Điều kiện nghiệm thu bắt buộc

- [ ] Recall được message hợp lệ ở đầu session rất dài, không phụ thuộc last-8/last-200.
- [ ] Đúng quyết định hiện tại và giữ provenance khi có thay đổi/mâu thuẫn.
- [ ] Câu tiếp nối có đủ ngữ cảnh; assistant suggestion không bị coi là user approval.
- [ ] Không cross-owner/thread/project hoặc dùng nguồn đã revoke/delete.
- [ ] Index stale/failed/cold/cancel không tạo khẳng định nhớ đầy đủ sai.
- [ ] Mọi model request/retry/compression/synthesis được tính usage; unknown khác zero.
- [ ] Không giảm chất lượng theo ngưỡng ghim trước holdout; cải thiện nhớ thông tin cũ.
- [ ] Savings normal session dài được đo; full-session có báo cáo riêng.
- [ ] Client thật hiển thị giới hạn/partial theo contract đã duyệt.
- [ ] Không thay retrieval/ingestion tài liệu hoặc BE identity ngoài scope.

## Handoff triển khai sau này

- [ ] Relevant tests, lint và integration checks hoàn tất; không coi mock là production acceptance.
- [ ] Git status/diff/history và secrets/generated/debug review.
- [ ] DB impact/migration/rollback và config/model/license ghi rõ.
- [ ] Code review hoàn tất; con người review/CI/merge.
- [ ] Báo nhánh/commit, checks, hạn chế, exact push command; không tự push.

Không checkbox implementation/benchmark nào được đánh dấu chỉ vì tài liệu này đã được viết.
