# Knowledge Foundation & Provider Token Usage Metering

> **Cập nhật kiến trúc 2026-10-29:** Pha **E2 LLM Enrichment** (`KnowledgeJob.kind == "E2"`, `SAG_KNOWLEDGE_E2_*`, `SAG_KNOWLEDGE_DAILY_TOKENS`) đã được **loại bỏ vĩnh viễn** sau phép đo đối chứng A/B trên toàn bộ bộ chuẩn RetrievalCI 81 tài liệu / 50 câu hỏi (`docs/adr-remove-e2-llm-enrichment.md`), nơi việc bật E2 không tăng Recall (`99.17% -> 99.17%`), làm giảm Overall Score (`93.64 -> 93.42`), giảm Precision (`80.73% -> 80.00%`) và giảm Abstention (`100% -> 80%`) nhưng làm tăng chi phí ingest gấp `3.42x` (`+151,435` LLM tokens).
>
> Hệ thống hiện chỉ vận hành **Phase 5A `FOUNDATION`** (`E0/E1` tất định + `DenseGraph` tái sử dụng 100% cache embedding từ Phase 2C + cây định tuyến `Leiden`).

## 1. Phân tách Scope & Quản lý Hàng đợi Foundation

- `tenant_id` được lấy từ phạm vi tổ chức Continuum đã xác thực (`organization_id`), kết hợp với `project_id` và `security_partition_id` làm ranh giới phân quyền & cô lập dữ liệu.
- `KnowledgeWorker` (`sag_api/jobs/knowledge.py`) chỉ nhận và thực thi `KnowledgeJob(kind="FOUNDATION")`, được bảo vệ bởi `SAG_KNOWLEDGE_CONCURRENCY` và `SAG_KNOWLEDGE_QUEUE_CAPACITY`, không phát sinh bất kỳ lời gọi LLM nào trong quá trình ingest.

## 2. Đo & Tổng hợp Token chính xác từ Provider (`sag_api/core/token_usage.py`)

Toàn bộ lượng token tiêu thụ đều được đọc trực tiếp từ `usage` do provider (SiliconFlow / OpenAI-compatible) trả về thay vì ước lượng cảm tính:

- **Embeddings (`Qwen/Qwen3-Embedding-0.6B`)**: ghi nhận `embedding_input_tokens` và `embedding_cache_hits` cho:
  - `search_index_embedding` (Phase 2C lập chỉ mục `SearchUnit` & preload `CanonicalBlock`)
  - `knowledge_foundation_embedding` (Phase 5A gắn vector `DenseGraph` cho `KnowledgeUnit` — đạt `100%` cache hit khi đi qua Phase 2C)
  - `query_embedding` (nhúng câu truy vấn lúc tìm kiếm)
- **Chat / Streaming Synthesis (`Qwen/Qwen3-8B`, `stream_options={"include_usage": True}`)**: ghi nhận `llm_input_tokens`, `llm_uncached_input_tokens`, `llm_cached_input_tokens`, `llm_output_tokens`, `llm_reasoning_tokens` cho:
  - `answer_generation` (tổng hợp câu trả lời RAG qua `/api/v1/search` và `/api/v1/search/stream`)
  - `agent_turn` (tác tử hội thoại)
- **Lưu trữ bền vững & API**:
  - Lưu theo từng tài liệu tại `documents.token_usage`, `documents.token_usage_json`, `document_versions.metadata_json` và `knowledge_jobs.usage_json`.
  - Truy xuất báo cáo chi tiết từng file, từng loại token, từng thành phần và chi phí USD tại `GET /api/v1/projects/{project_id}/documents/token-usage` và `GET /api/v1/system/metrics`.
