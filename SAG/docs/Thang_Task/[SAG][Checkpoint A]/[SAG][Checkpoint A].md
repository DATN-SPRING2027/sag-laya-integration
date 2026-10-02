# [SAG][Checkpoint A] Global hybrid retrieval, context & citation from SEARCH_READY index

Ngày ghi task: 2026-10-02.

Trạng thái: Task mới; nội dung phạm vi được lưu trước bước research và implementation.

## Nguồn chuẩn và phạm vi kế thừa

- [SAG/tasks/plan.md](../../../tasks/plan.md): Phase 4, Checkpoint A.
- [SAG/tasks/todo.md](../../../tasks/todo.md): Phase 4, Checkpoint A.

Kế thừa phạm vi đã đóng trong DATN-29/DATN-30; tập trung vào tích hợp end-to-end với SEARCH_READY index.

## Phạm vi

1. Nối global `/search`, `/search/stream` và `search_context` tới Phase 2C SearchUnits trong `search_units_{project_id}`; không phụ thuộc Knowledge Tree.
2. Chỉ tìm trong các SEARCH_READY version thuộc Project/Source scope mà principal được phép truy cập; `source_ids` từ client chỉ được phép thu hẹp phạm vi ACL.
3. Hybrid dense + sparse/lexical retrieval và fusion ổn định; tạo context chỉ từ evidence được phép, trong token budget.
4. Citation truy ngược source/document version/Search Unit/block range/page/section/canonical anchor; đường click mở đúng evidence.
5. Evidence rỗng/yếu trả no-answer, không bịa; query vẫn hoạt động khi knowledge enrichment/tree tắt, trễ hoặc lỗi.

## Acceptance

- [ ] Regression chứng minh upload → verified SEARCH_READY → global hybrid search và `search_context` trả evidence.
- [ ] Regression bao phủ ACL filter/narrowing, citation source/version/page/anchor, empty/no-evidence, index error/retry và enrichment off/lag/failure.
- [ ] Error response/trace không lộ secret.
- [ ] Ghi lại tests/evidence và cập nhật các mục Checkpoint A thuộc retrieval/context/citation.

## Ranh giới và phối hợp

Issue này phụ trách retrieval/context/citation; không sửa index readiness/worker lane của DATN-33.

Chốt payload/status contract với DATN-33 sớm để hai lane làm song song. Checkpoint B/C sẽ theo sau khi A đạt gate.
