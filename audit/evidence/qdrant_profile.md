# Qdrant Read-Only Profile (`localhost:6333`)

Ngày cập nhật: 2026-10-21.
Chế độ truy vấn: Chỉ đọc (`GET /collections`, `POST /collections/{name}/points/scroll`, `POST /collections/{name}/points/search`).

## 1. Tổng quan 6 Collections trên Qdrant

| Collection | Số points | Cấu hình Dense Vector | Cấu hình Sparse Vector | Payload Indexes |
|---|---:|---|---|---|
| `search_units_6ad61cb2e1f989c367a1eb46` | **150** | `dense`: `1024-d`, `Cosine` (`Qwen/Qwen3-Embedding-0.6B`) | `sparse_text`: `Modifier.IDF` | `tenant_id`, `project_id`, `security_partition_id`, `source_id`, `document_version_id`, `slot_a_node_id`, `slot_b_node_id` |
| `source_chunks` | **218** | `heading_vector`: `1024-d`, `Cosine`; `content_vector`: `1024-d`, `Cosine` | `sparse_text` | `data_source_id`, `source_id`, `id` |
| `event_vectors_wide` | **10** | `title_vector`: `1024-d`, `Cosine`; `content_vector`: `1024-d`, `Cosine` | `sparse_text` | `data_source_id`, `source_id`, `category` |
| `entity_vectors` | **29** | `vector`: `1024-d`, `Cosine` | Không có | `data_source_id`, `type`, `entity_id` |
| `event_entity_vectors` | **39** | `vector`: `1024-d`, `Cosine` | Không có | `data_source_id`, `event_id`, `entity_id` |
| `search_units_proj_search_idx` | **2** | `dense`: `4-d`, `Cosine` (fixture test) | `sparse_text`: `Modifier.IDF` | `tenant_id`, `project_id`, ... |

---

## 2. Phân tích Chất lượng & Rò rỉ Dữ liệu trong `search_units_6ad61cb2e1f989c367a1eb46` (150 points)

Đối chiếu 150 point IDs trong Qdrant `search_units_6ad61cb2e1f989c367a1eb46` với bảng `sag_metric_e2e_67a1eb46.search_units` và `sag_metric_e2e_67a1eb46.documents`:

| `documents.id` | `filename` | `documents.status` | `document_versions.search_status` | `document_versions.knowledge_status` | Số points trong Qdrant `search_units_6ad61cb2e1f989c367a1eb46` | Tỷ trọng |
|---|---|---|---|---|---:|---:|
| `98f47e08-c8f8-5c3f-9843-1d222af80552` | `sag-metric-partition-universe.md` | **`READY`** | `SEARCH_READY` | `DATA_READY` | **4** | 2.67% |
| `5ec94f5f-9004-5f93-bf4b-2dcbb06a9ae5` | `tmp42b7q59t.md` | **`READY`** | `SEARCH_READY` | `DATA_READY` | **1** | 0.67% |
| `9a1c589f-cb59-5505-ad6b-4cf399d80115` | `tmp31n548e1.md` | **`READY`** | `SEARCH_READY` | `DATA_READY` | **1** | 0.67% |
| `208ffd14-b173-5a19-9430-6a98a28c70df` | `tmpb2747l19.md` | **`PAUSED`** | `SEARCH_READY` | `NOT_STARTED` | **138** | **92.00%** |
| `fc68cd0a-60fd-5d97-a309-9d6ac98ff678` | `sag-metric-partition-universe.md` | **`PAUSED`** | `SEARCH_READY` | `NOT_STARTED` | **4** | 2.67% |
| `8cb2b390-c0eb-56f7-85f0-804aa22989e8` | `tmpbf4j_78e.md` | **`PAUSED`** | `SEARCH_READY` | `NOT_STARTED` | **2** (`1` active + `1` superseded) | 1.33% |

### Phát hiện quan trọng trên Qdrant:
1. **96% vectors (144/150 points) trong `search_units_6ad61cb2e1f989c367a1eb46` thuộc về các tài liệu đang `PAUSED` (hoặc `SUPERSEDED`)**:
   - Do Phase 2 (`PARSE` $\rightarrow$ `DEDUP` $\rightarrow$ `EMBED` $\rightarrow$ `INDEX_SEARCH` trong `jobs/tasks.py:430-478`) chạy và commit `search_status = 'SEARCH_READY'` + đẩy vector lên Qdrant **TRƯỚC KHI** chạy `engine_manager.process_document()` (dòng 550), khi bước extract của `zleap-sag` bị lỗi/tạm dừng (`PAUSED`), toàn bộ 144 points vẫn nằm lại trên Qdrant.
   - Hơn nữa, tài liệu `fc68cd0a-60fd-5d97-a309-9d6ac98ff678` (`PAUSED`, `security_partition_id = 'sag-metric-e2e-private'`) là bản sao nội dung giống hệt `sag-metric-partition-universe.md` (`98f47e08`, `READY`), tạo ra 4 near-duplicate/exact-duplicate vectors trong cùng collection của project.
2. **Tương tự trên collection `source_chunks` (218 points)**:
   - Chỉ có **3 points** thuộc 3 `article` `COMPLETED` (trong đó point `c5a99157-88ef-48dd-a103-0edba036d535` là chunk duy nhất của `sag-metric-partition-universe.md`), còn **215 points (98.6%)** thuộc về 4 `article` có trạng thái `EXTRACTION_FAILED` trong `sag.article`!
3. **Kiểm tra 4 points của `sag-metric-partition-universe.md` (`9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2`) trong `search_units_6ad61cb2e1f989c367a1eb46`**:
   - Point IDs: `0a36f3bc-c940-5bf2-b570-f5bf721fb2c0`, `4ffdc768-0cf0-544d-89cd-8ed94170ad28`, `b51f31b7-f1fa-55de-b037-d4ef1b1c95f6`, `de0e3315-f4cf-52c0-87e1-e6174ac5c555`.
   - Cả 4 points đều có đầy đủ metadata (`tenant_id = '6ad615f6e1f989c367a1eac8'`, `project_id = '6ad61cb2e1f989c367a1eb46'`, `security_partition_id = '6ad61cb2e1f989c367a1eb46'`, `source_id = '6f87b67132654b509b075fa911adf2b4'`, `document_version_id = '9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2'`, `slot_a_node_id = 'node-c257c3a82c987444643f5df4'`).
   - Lưu ý: Trong PostgreSQL (`sag_metric_e2e_67a1eb46.search_units`), cột `active_tree_version` của 4 dòng này đang là `NULL` (chỉ được cập nhật trên payload Qdrant `slot_a_node_id`).
