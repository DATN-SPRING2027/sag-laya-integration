# Checklist triển khai SAG RAG + Laya Local

## Phase 0 — Chuẩn bị

- [ ] Xác nhận `SAG_ENABLE_LAYA=true`.
- [ ] Xác nhận `SAG_LAYA_MODEL_PATH=C:\laya-local`.
- [ ] Xác nhận `SAG_LAYA_DEVICE=cpu` hoặc device triển khai thực tế.
- [ ] Xác nhận LLM provider/model trong Settings.
- [ ] Xác nhận Document/Job states và response contract của Laya.

## Phase 1 — Upload và Job

- [ ] Test upload PDF.
- [ ] Test upload DOCX.
- [ ] Test upload TXT.
- [ ] Test file sai extension/MIME/size.
- [ ] Kiểm tra một upload chỉ tạo một Document/Job.
- [ ] Kiểm tra retry và reprocess.
- [ ] Kiểm tra UI hiển thị trạng thái job.

## Phase 2 — Parse, chunk và indexing

- [ ] Kiểm tra parser theo từng định dạng.
- [ ] Kiểm tra text normalization.
- [ ] Kiểm tra chunk size và overlap.
- [ ] Kiểm tra metadata chunk.
- [ ] Kiểm tra embedding/vector indexing.
- [ ] Kiểm tra idempotency khi reprocess.
- [ ] Kiểm tra Document chỉ chuyển `READY` sau indexing.
- [ ] Kiểm tra trạng thái `FAILED` và error message.

## Checkpoint A

- [ ] Upload một tài liệu mẫu.
- [ ] Chờ Document chuyển `READY`.
- [ ] Tìm được một đoạn nội dung đã biết.
- [ ] Reprocess không tạo duplicate.

## Phase 3 — Laya Local routing

- [ ] Test câu chào hỏi.
- [ ] Test câu hỏi kiến thức bằng tiếng Việt.
- [ ] Test câu hỏi có confidence thấp.
- [ ] Test khi model không load được.
- [ ] Xác nhận model chỉ load một lần.
- [ ] Xác nhận chitchat không gọi retrieval.
- [ ] Xác nhận factual query vẫn gọi retrieval.

## Phase 4 — Retrieval và context

- [ ] Xác định retrieval tool chính.
- [ ] Kiểm tra query được truyền nguyên vẹn.
- [ ] Kiểm tra top-k/chunk ranking.
- [ ] Kiểm tra loại duplicate chunk.
- [ ] Kiểm tra metadata/citation.
- [ ] Kiểm tra empty context.
- [ ] Kiểm tra giới hạn context window.

## Phase 5 — LLM

- [ ] Kiểm tra provider protocol.
- [ ] Kiểm tra Base URL.
- [ ] Kiểm tra API key không xuất hiện trong log.
- [ ] Kiểm tra model/context window/output tokens.
- [ ] Kiểm tra timeout/retry.
- [ ] Kiểm tra câu trả lời grounded vào context.
- [ ] Kiểm tra behavior khi không có context.

## Phase 6 — Frontend tiếng Việt

- [ ] Việt hóa trạng thái upload.
- [ ] Việt hóa trạng thái indexing.
- [ ] Việt hóa lỗi parse/indexing/retrieval/LLM.
- [ ] Hiển thị citation.
- [ ] Hiển thị Laya enabled/disabled.
- [ ] Đảm bảo Settings LLM không bị nhầm với Settings Laya.

## Phase 7 — Kiểm thử và vận hành

- [ ] Unit tests.
- [ ] Integration test upload → READY → search.
- [ ] E2E test upload → hỏi → retrieval → answer.
- [ ] Đo latency first-load của Laya.
- [ ] Đo latency inference của Laya.
- [ ] Đo latency retrieval và LLM.
- [ ] Kiểm tra memory/CPU.
- [ ] Kiểm tra concurrent jobs/requests.
- [ ] Chuẩn bị bộ tài liệu và câu hỏi regression.

## Hoàn tất

- [ ] Tất cả tiêu chí trong `tasks/plan.md` đạt.
- [ ] Test và lint/typecheck pass.
- [ ] Không commit secret hoặc model artifact lớn.
- [ ] Cập nhật README/runbook triển khai.
- [ ] Ghi lại các quyết định kiến trúc phát sinh.
