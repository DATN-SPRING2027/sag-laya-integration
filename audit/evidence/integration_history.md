# Laya/SAG integration history and prior results

Ngày rà soát: 2026-10-21. Các số dưới đây là kết quả được ghi trong tài liệu của repo, không phải test do lượt này chạy lại.

## Dòng thời gian code

- Workspace README mô tả route câu hỏi bằng Laya Local và cấu hình SAG_ENABLE_LAYA, SAG_LAYA_MODEL_PATH, SAG_LAYA_DEVICE.
- Git history ghi nhận 74b0a31 tích hợp Laya vào search flow.
- Commit 0133687 ngày 2026-10-17 thêm services/decision_router.py, xóa services/laya_router.py và api/v1/laya.py.
- Cây code hiện tại có services/decision_router.py; không có Laya service, endpoint hoặc các biến cấu hình Laya. Root README đang trỏ tới các path không tồn tại.
- Search flow gọi route_query; Decision Router hiện gọi SiliconFlow System One khi có key. Nếu thiếu key/lỗi, nó đánh dấu fallback và giữ retrieval.

## Checkpoint 1–2

Theo docs/phase-1-2-report.md, lần chạy ngày 2026-09-22 dùng một fixture TXT:

- 1 Document, 1 chunk, 1 event, 3 entity, 4 reference.
- Embedding Qwen3-Embedding-0.6B dimension 1024.
- Hai truy vấn đều lấy cùng chunk ở rank 0, score 0.955.
- Job 63.80 giây; extraction khoảng 60 giây; hai retrieval đo được 3,716.89 ms và 361.53 ms.
- Báo cáo nói rõ chưa chạy Agent, LLM final answer, Laya routing hoặc PDF/DOCX E2E ở checkpoint đó.

Đây là smoke test một chunk, không thể ước lượng recall/precision hoặc latency percentile trên corpus.

## Checkpoint Laya và routing

Theo docs/phase-3-review-report.md:

- Standalone Laya được báo PASS với 9 test; first load khoảng 21.1 giây; warm latency 164–204 ms.
- Query-flow integration được báo 63 focused tests pass và Ruff pass.
- Full API suite ở báo cáo là 612 passed, 1 skipped, 6 failed, 1 error.
- Phạm vi checkpoint vẫn không chứng minh quality của final answer hoặc live deployment; các số này thuộc code trước khi Laya bị xóa khỏi HEAD.

## Fusion / routing tree / vận hành

- docs/tai_task/phase-4-fusion-retrieval-evidence.md ghi 99 retrieval/fusion/stream test pass ngày 2026-10-12; nội dung gồm RRF, dedupe, ranking tiếng Việt và exact identifier trên fixture.
- docs/tai_task/phase-6-tree-evidence.md ghi recall@k 1.0 và ACL leakage 0 trên synthetic fixture; chính tài liệu nói đây không phải production benchmark và chưa nối producer/publish.
- docs/tai_task/phase-8-incremental-update-evidence.md ghi 21 service-level test pass; chưa có normal-ingestion caller cho incremental update.
- docs/tai_task/phase-11-hardening-and-dod-research.md ghi DoD #10 CHƯA ĐỦ EVIDENCE; percentile benchmark theo stage, live staging ACL/failure checks và deployable alert configuration còn mở.

## Khả năng tái lập test

- Commit 8ae2a06 ngày 2026-10-14 xóa 93 file test API, tổng 45,534 dòng.
- HEAD hiện tại không track file nào dưới apps/api/tests. Working tree có một số test mới nhưng untracked.
- Vì vậy các kết quả test ở tài liệu phase cũ là lịch sử có giá trị tham khảo; chúng không thể tái chạy trực tiếp từ test files của HEAD.
