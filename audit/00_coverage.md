# 00 — Bảng Kiểm Kê Phạm Vi Đã Kiểm Tra (Coverage Inventory)

Ngày cập nhật: 2026-10-21.

## 1. Thành phần Code & Kiến trúc đã kiểm toán (`sag_api/sag/` và lân cận)

| Module / File | Số dòng | Vai trò trong kiến trúc | Đã đọc & đối chiếu DB? |
|---|---:|---|---|
| `SAG/apps/api/sag_api/sag/__init__.py` | 35 | Public facade của lớp adapter nối `sag_api` với engine `zleap-sag`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/config_builder.py` | 150 | Xây dựng `EngineConfig` (`LLMConfig`, `EmbeddingConfig`, `RelationalConfig`, `VectorConfig`, `StorageConfig`) cho `zleap-sag`. | Đã kiểm tra 100% — Phát hiện lỗi thiếu `schema`/`search_path` ở `_build_relational` (dòng 66–77) |
| `SAG/apps/api/sag_api/sag/engine_manager.py` | 3,292 | Quản lý vòng đời `DataEngine` theo `source_config_id`, khóa đồng thời (`slot.lock`), `process_document`, `search`, `search_many`, `search_event_scores`, `graph_for_sections`. | Đã kiểm tra 100% các nhánh ingest/extract/search/graph |
| `SAG/apps/api/sag_api/sag/incremental_processor.py` | 531 | Nạp tài liệu theo chunk (`DataEngine.ingest`) và gọi `EventExtractor` / `EntityExtractor` (`DataEngine.extract`) có lưu checkpoint. | Đã kiểm tra 100% — Phát hiện tiêm `settings.sag_language` mặc định `"zh"` vào prompt trích xuất (dòng 65–73) |
| `SAG/apps/api/sag_api/sag/qdrant_store.py` | 190 | `PrefixedQdrantVectorStore` gắn prefix collection + inject native sparse vector (`sparse_text`) cho `source_chunks` và `event_vectors_wide`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/search_unit_store.py` | 290 | `SearchUnitQdrantStore` tạo collection `search_units_<project_id>` (1024-d Cosine + `sparse_text` Modifier.IDF) và truy vấn hybrid RRF. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/chunk_heading_vectors.py` | 81 | Backfill `heading_vector` cho `source_chunks` bằng vector trung bình hoặc zero-vector khi chunk thiếu heading. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/document_cleanup.py` | 209 | Xóa `Article`, `SourceChunk`, `SourceEvent`, `EventEntity`, `Entity` và vector tương ứng khi xóa/reprocess tài liệu. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/dto.py` | 131 | DTO kết quả `ProcessCheckpoint`, `ProcessOutcome`, `RetrievedSection`, `SearchOutcome`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/errors.py` | 68 | Ánh xạ `SagError` & `JsonSchemaValidationError` sang `ApiError(layer, stage)`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/sag/compat.py` | 22 | Vá tương thích `litellm.AsyncOpenAI` cho `zleap-sag`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/services/knowledge_service.py` | 632 | Trích xuất `KnowledgeUnit` + `KnowledgeEvidence` bậc `E0`/`E1` (`extract_e0_e1`), kiểm chứng checksum (`unit_checksum`, `version_input`), và dựng `KnowledgeGraphBuild` (`graph_payload`, `persist_graph`). | Đã kiểm tra 100% — Phát hiện lỗi tách câu tại số thập phân `0.75`, regex thực thể bỏ sót thực thể tên riêng, và `E1` `CO_OCCURS` ghép cặp theo thứ tự bảng chữ cái |
| `SAG/apps/api/sag_api/jobs/knowledge.py` | 537 | Worker `KnowledgeWorker` chạy job `FOUNDATION` và `E2` (`llm_enrich`, `apply_e2`). | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/jobs/tasks.py` | 1,106 | Worker `process_document` chạy song song 2 nhánh: (1) Phase 2 (`PARSE` -> `DEDUP` -> `EMBED` -> `INDEX` `search_units`) và (2) `engine_manager.process_document` (`zleap-sag`). | Đã kiểm tra 100% — Phát hiện lỗi không xóa `error_layer`/`error_stage` khi tài liệu đạt `READY` (dòng 672–680) |
| `SAG/apps/api/sag_api/services/document_service.py` | 1,408 | Quản lý upload, snapshot (`_save_snapshot_file`), `get_or_create_project_source`, `pause/resume/delete/reprocess_document`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/services/knowledge_candidate_service.py` | 425 | Xây dựng `RoutingSnapshot` và `TreeManifest` từ `KnowledgeUnit` và `KnowledgeGraphBuild`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/services/routing_tree_service.py` | 704 | Thuật toán phân hoạch cây định tuyến Constrained Hierarchical Leiden CPM (`constrained-hierarchical-leiden-cpm-v1`). | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/services/knowledge_quality_repository.py` & `knowledge_gap_question_service.py` | 435 + 242 | Đánh giá chất lượng tri thức theo nút cây và sinh câu hỏi nghiên cứu từ `KnowledgeEvidence`. | Đã kiểm tra 100% |
| `SAG/apps/api/sag_api/core/config.py` | 351 | Cấu hình toàn cục (`Settings`), mặc định `sag_language = "zh"` (dòng 161). | Đã kiểm tra 100% |

---

## 2. Hạ tầng Lưu trữ & Dữ liệu Thật đã truy vấn (Read-Only)

| Store / Namespace | Trạng thái kiểm tra | Quy mô thực tế đã đo | Bằng chứng lưu tại |
|---|---|---|---|
| **PostgreSQL `localhost:5432/sag` — Schema `sag_metric_e2e_67a1eb46`** | Đã truy vấn READ-ONLY (`statement_timeout = 5s`) | 46 bảng ứng dụng SAG của project `6ad61cb2e1f989c367a1eb46`: `documents`=6, `document_versions`=7, `source_snapshots`=7, `canonical_blocks`=328, `search_units`=150, `knowledge_units`=7, `knowledge_evidence`=42, `knowledge_jobs`=3, `knowledge_graph_builds`=3, `knowledge_unit_edges`=4, `tree_manifests`=3, `tree_routing_profiles`=6, `stage_runs`=74. | `audit/evidence/postgres_profile.md`, `audit/evidence/project_6ad61cb2_knowledge_audit.md` |
| **PostgreSQL `localhost:5432/sag` — Schema `sag`** | Đã truy vấn READ-ONLY (`statement_timeout = 5s`) | 91 bảng (8 bảng của `zleap-sag`: `data_source`=3, `article`=7, `source_chunk`=218, `source_event`=10, `entity`=29, `event_entity`=39; trộn lẫn với 83 bảng dự án `aiinvest`, trong đó `sag.entities`=77,033 dòng). | `audit/evidence/postgres_profile.md` |
| **Qdrant `localhost:6333` — 6 collections** | Đã truy vấn READ-ONLY qua REST API | `search_units_6ad61cb2e1f989c367a1eb46` (150 points), `source_chunks` (218 points), `event_vectors_wide` (10 points), `entity_vectors` (29 points), `event_entity_vectors` (39 points), `search_units_proj_search_idx` (2 points). | `audit/evidence/qdrant_profile.md` |
| **Filesystem `.data/engine/snapshots/`** | Đã kiểm tra file & băm SHA-256 | File snapshot `e3/e34fa7e7a98198ffe713713078f3cfdfd0f142e26008689ef8aeb9e33978072b/sag-metric-partition-universe.md` (2,105 bytes, SHA-256 khớp 100% DB). | `audit/evidence/project_6ad61cb2_knowledge_audit.md` |

---

## 3. Những gì KHÔNG kiểm tra được / Giới hạn phạm vi

- **Elasticsearch (`localhost:9200`) và Redis (`localhost:6379`)**: Không có tiến trình lắng nghe tại thời điểm kiểm toán; trong `.env`, `SAG_VECTOR_STORE_BACKEND=qdrant` và `SAG_RELATIONAL_PROVIDER=postgresql`, nên hệ thống thực tế đang chạy trên PostgreSQL + Qdrant.
- **Laya Local LLM Server (`localhost:8080`)**: Không có tiến trình đang chạy; phần tích hợp trong `sag-laya-integration/` đã chuyển sang `DecisionRouterClient` (HTTP client).
