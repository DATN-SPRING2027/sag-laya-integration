# Checklist: Context session v1

Trạng thái: yêu cầu sản phẩm đã chốt; T1 baseline được Thang yêu cầu và đã thực hiện. Selector mới chưa implementation. Theo [plan](plan.md), [SPEC](../SPEC.md) và [BASELINE](../BASELINE.md).

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
- [ ] T2: Chốt snapshot, typed evidence/coverage/budget và dependency/ACL contracts.
- [ ] T3a: Reader/hierarchy, phân trang không giới hạn phạm vi nhớ.
- [ ] T3b: Graph/fusion/closure, model NER/encoder được xác minh.
- [ ] T4: Calibration; chốt budgets/thresholds trước holdout.
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
