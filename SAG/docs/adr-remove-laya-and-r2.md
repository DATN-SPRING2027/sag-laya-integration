# ADR: Loại Bỏ Laya Local Router Và Đường Nạp R2 Object-Ingest Khỏi SAG

- **Trạng thái:** Accepted & Implemented (2026-11-25)
- **Phạm vi ảnh hưởng:** `sag_api/services/decision_router.py`, `sag_api/core/config.py`, `sag_api/services/settings_service.py`, `sag_api/api/v1/documents.py`, `sag_api/services/document_service.py`, `sag_api/jobs/tasks.py`, `sag_api/parsing/service.py`, `sag_api/parsing/mineru_official.py`, `sag_api/schemas/routing_rag.py` (và xóa `sag_api/parsing/remote_object.py`), `SAG/.env.example`, `SAG/compose.yaml`, và các tài liệu thiết kế lịch sử.
- **Các tầng được giữ nguyên 100%:** `handle_document_upload` (upload file cục bộ) → `CanonicalBlock` → `SearchUnit` (PostgreSQL + Qdrant Hybrid) → `Phase 5A FOUNDATION (E0/E1 + DenseGraph + Leiden)`; **Smart Hybrid MinerU Official v4 Option A** (Mode 2 batch presigned `PUT` upload + local isolated worker fallback); `services/decision_router.py` (configurable external provider, mặc định SiliconFlow `Kev-4b` hoặc `disabled`).

---

## 1. Bối cảnh & Câu hỏi kiến trúc

Trong quá trình phát triển ban đầu của `sag-laya-integration`:
1. **Laya Local Router:** Hệ thống từng dự kiến nhúng mô hình phân loại cục bộ **Laya Local** (`laya_router.py`, `api/v1/laya.py`, `SAG_ENABLE_LAYA`, `SAG_LAYA_*`, `OpenRouter` fallback) để định tuyến câu hỏi. Thực tế vận hành đã chuyển hoàn toàn sang `services/decision_router.py` (gọi provider ngoài cấu hình qua `Settings`, mặc định SiliconFlow `Kev-4b`), và các tệp runtime Laya không còn tồn tại. Tuy nhiên, biến môi trường `.env` và nhiều tài liệu thiết kế/kế hoạch (`README.md`, `phase-0`, `phase-1-2`, `phase-3`, `Workflow_v1.1`, `tasks/plan.md`, `tasks/todo.md`) vẫn mô tả Laya như luồng đang chạy.
2. **Đường nạp R2/S3 Object-Ingest & Post-Index Disk GC:** Hệ thống từng bổ sung nhánh `POST /projects/{project_id}/documents/ingest-object`, `parsing/remote_object.py`, các trường R2 trên `DocumentReprocessRequest`, chế độ Mode 1 Direct URL Task (`remote_source_url` / `/api/v4/extract/task`) trên `OfficialMinerUClient`, cùng khối **Post-index Disk GC** trong `jobs/tasks.py` xóa file gốc trên đĩa cục bộ sau khi index xong (vì giả định bản gốc bất biến đang nằm trên Cloudflare R2). Khi dự án quyết định **không sử dụng Cloudflare R2**, các thành phần này trở thành **code chết nhưng bề mặt tấn công/rủi ro vẫn mở**.

---

## 2. Lý do kỹ thuật & Rủi ro khi giữ tàn dư

1. **Rủi ro Developer-Hallucination với Laya & E2:**
   - Giữ biến `SAG_ENABLE_LAYA`, `SAG_LAYA_*`, `SAG_OPENROUTER_API_KEY`, dòng trùng `SAG_DECISION_MODEL=typesafe/jev-1.14`, biến `SAG_KNOWLEDGE_E2_*` trong `.env`/`compose.yaml`, và các tài liệu cũ chưa gắn nhãn `SUPERSEDED` khiến lập trình viên/agent hiểu nhầm kiến trúc đang chạy Laya Local hoặc Tier E2.
   - Việc thiếu `laya_router.py` / `api/v1/laya.py` là **chủ đích kiến trúc**, không phải lỗi cần khôi phục.
2. **Rủi ro bảo mật & mất dữ liệu với R2 / Disk GC:**
   - Endpoint `POST /projects/{project_id}/documents/ingest-object` vẫn mở nhận URL từ bên ngoài.
   - Nhánh **Post-index Disk GC** trong `jobs/tasks.py` gọi `os.remove(target_storage_path)` nếu payload có `remote_object_sourced=True`, gây nguy cơ xóa mất file gốc cục bộ cần cho reprocess và truy xuất nguyên bản.

---

## 3. Quyết định kiến trúc

1. **Ngừng vĩnh viễn Laya Local & OpenRouter trong SAG runtime:**
   - Định tuyến truy vấn sử dụng `sag_api/services/decision_router.py` thông qua provider ngoài cấu hình được qua `Settings` (`decision_provider`, `decision_model`, `siliconflow_*`), không hardcode và **không khôi phục** `laya_router.py` hay `api/v1/laya.py`.
   - Thư mục `laya/` ở gốc workspace (`laya/demo.py`) chỉ là mã demo độc lập mang tính lịch sử, tách biệt hoàn toàn khỏi runtime SAG.
2. **Gỡ bỏ hoàn toàn đường R2 / Object-Ingest & Post-Index Disk GC:**
   - Xóa `sag_api/parsing/remote_object.py`.
   - Gỡ endpoint `POST /projects/{project_id}/documents/ingest-object`, schema `DocumentObjectIngestRequest` và `DocumentReprocessRequest` (đưa `POST /projects/{project_id}/documents/{document_id}/reprocess` về dạng không cần request body).
   - Gỡ `object_ingest_*` và `remote_snapshot_gc_enabled` khỏi `Settings` (`core/config.py`), `.env`, `.env.example`.
   - Gỡ nhánh `needs_remote_fetch`, `remote_object_sourced`, `remote_source_url` và **Post-index Disk GC** khỏi `jobs/tasks.py`, `services/document_service.py`, `parsing/service.py`.
   - Gỡ `remote_source_url` và Mode 1 Direct URL Task (`_create_url_task`, `_poll_task`) khỏi `parsing/mineru_official.py`; giữ nguyên **Mode 2 Batch Presigned Streaming Upload** (`POST /api/v4/file-urls/batch` + `PUT` streaming) và local isolated parser fallback.
   - Luồng chuẩn `handle_document_upload -> prepare_document -> CanonicalBlock -> SearchUnit -> READY` luôn giữ nguyên tệp gốc cục bộ trên đĩa.

---

## 4. Tác động tới Cấu hình, Tài liệu & Tương thích ngược

1. **Shim di trú ngược một chiều (Backward-Compatibility):**
   - Giữ lại `_migrate_legacy_decision_config` (`core/config.py`) và `_normalize_overrides` (`services/settings_service.py`) để tự động chuyển dữ liệu/cấu hình cũ (`"laya"`, `"openrouter"`, `"typesafe/jev-1.14"`) sang `"siliconflow"` / `"Kev-4b"` và loại bỏ khóa `openrouter_api_key`, `laya_model_path`, `laya_device` khi đọc từ DB cũ lúc khởi động.
   - Trong `reprocess_document`, xóa phòng vệ các khóa `remote_object_sourced` / `remote_source_url` nếu còn tồn đọng trong `Job.payload` cũ.
2. **Cập nhật cấu hình & tài liệu:**
   - Xóa các biến Laya/OpenRouter/E2/R2 chết khỏi `SAG/.env`, `SAG/.env.example`, và `SAG/compose.yaml`.
   - Đánh dấu `> **SUPERSEDED (2026-11-25):**` ở đầu các tài liệu lịch sử (`README.md` gốc, `SAG/docs/phase-0-contracts-and-foundations.md`, `SAG/docs/phase-1-2-report.md`, `SAG/docs/phase-3-review-report.md`, `SAG/docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`, `SAG/tasks/plan.md`, `SAG/tasks/todo.md`) và cập nhật `SAG/README.md` trỏ tới ADR này cùng `docs/adr-remove-e2-llm-enrichment.md`.
