# PostgreSQL Read-Only Profile (`localhost:5432/sag`)

Ngày cập nhật: 2026-10-21.
Chế độ truy vấn: `readonly=True`, `statement_timeout = '5s'`.

## 1. Cấu hình Kết nối & Phân mảnh Schema (Split-Brain Schema)

- **Server**: `PostgreSQL 18.3 (Debian 18.3-1.pgdg13+1) on x86_64-pc-linux-gnu`
- **Database**: `sag`, User: `sag`
- **`SHOW search_path`**: `"$user", public` $\rightarrow$ Khi kết nối bằng user `sag` mà không chỉ định schema, `current_schema()` mặc định là `"sag"`.
- **Tổng số schema nghiệp vụ phát hiện**:
  1. **`sag_metric_e2e_67a1eb46`** (46 bảng): Schema chứa toàn bộ bảng ORM ứng dụng `sag_api` của project `6ad61cb2e1f989c367a1eb46` (tạo bởi suite kiểm thử/chạy E2E với `search_path` riêng).
  2. **`sag`** (91 bảng): Chứa 8 bảng của engine `zleap-sag` (`data_source`, `article`, `source_chunk`, `source_event`, `entity`, `event_entity`, `entity_relation`, `chat_message`) bị ghi nhầm vào đây do `sag_api/sag/config_builder.py:66-77` (`_build_relational`) không truyền schema/search_path, cộng với 83 bảng từ một dự án khác (`aiinvest`: `sag.entities` có 77,033 dòng, `sag.entity_aliases` có 79,532 dòng).

---

## 2. Thống kê các bảng trong Schema `sag_metric_e2e_67a1eb46` (Project `6ad61cb2e1f989c367a1eb46`)

| Bảng (`sag_metric_e2e_67a1eb46.*`) | Số dòng thực (`COUNT(*)`) | Ghi chú chất lượng dữ liệu |
|---|---:|---|
| `sources` | 1 | `id = '6f87b67132654b509b075fa911adf2b4'`, `sag_source_config_id = 'tp_94e9997535651d8909088bee64844a1b'`, `document_count = 6`, **`chunk_count = 3`** (lệch với 218 `source_chunk` và 150 `search_units`), `event_count = 9` |
| `source_project_mappings` | 1 | `project_id = '6ad61cb2e1f989c367a1eb46'`, `source_id = '6f87b67132654b509b075fa911adf2b4'`, `state = 'CONFIRMED'` |
| `documents` | 6 | 3 dòng `READY` (`5ec94f5f`, `9a1c589f`, `98f47e08`), 3 dòng `PAUSED` (`208ffd14`, `8cb2b390`, `fc68cd0a`). Dòng `98f47e08` (`sag-metric-partition-universe.md`) có `status='READY'` nhưng còn sót `error_layer='engine'`, `error_stage='extract'`. |
| `document_versions` | 7 | 6 dòng ở `SEARCH_READY` (`version_no=1`, trong đó 3 dòng `DATA_READY` và 3 dòng `NOT_STARTED` thuộc 3 tài liệu `PAUSED`), 1 dòng `SUPERSEDED` (`dc49dc96`). |
| `source_snapshots` | 7 | Khớp 1:1 với 7 `document_versions`. |
| `canonical_blocks` | 328 | 9 blocks thuộc `sag-metric-partition-universe.md`; 297 blocks thuộc tài liệu `PAUSED` `208ffd14` (`tmpb2747l19.md`). |
| `search_units` | 150 | **Chỉ 6 units thuộc 3 tài liệu `READY`** (4 của `sag-metric-partition-universe.md` + 1 + 1); **144 units (96%) thuộc 3 tài liệu `PAUSED`** nhưng vẫn mang `search_status = 'SEARCH_READY'`! |
| `knowledge_units` | 7 | 5 units của `sag-metric-partition-universe.md` + 1 của `5ec94f5f` + 1 của `9a1c589f`. Tất cả đều có `features_json.dense = []` (rỗng). |
| `knowledge_evidence` | 42 | 28 dòng của `sag-metric-partition-universe.md` + 7 của `5ec94f5f` + 7 của `9a1c589f`. Tất cả ở bậc `E0`/`E1` (`E2` = 0 dòng). |
| `knowledge_jobs` | 3 | 3 jobs `FOUNDATION` đều `SUCCEEDED`. |
| `knowledge_graph_builds` | 3 | `graph-241abf74...` (1 unit, 0 edges), `graph-77703634...` (2 units, 1 edge), `graph-8d5a0342...` (7 units, 3 edges). |
| `knowledge_unit_edges` | 4 | Mọi cạnh đều có `signals.semantic = null` do `dense = []`. |
| `tree_manifests` | 3 | `tree-886e21d389c03c74ac1d95c5` (`ACTIVE`, 7 units, 2 nodes), 2 bản `INACTIVE`. |
| `tree_routing_profiles` | 6 | 2 nodes $\times$ 3 `tree_manifests`. Tất cả đều có `dense_medoid_json = null`. |
| `ingestion_runs` / `stage_runs` / `jobs` | 7 / 74 / 6 | Lưu vết đầy đủ các bước `CANONICAL_EXTRACTION`, `DEDUP_TEMPORAL`, `INDEX_SEARCH`, `loading`, `extracting`. |

---

## 3. Thống kê các bảng `zleap-sag` trong Schema `sag`

| Bảng (`sag.*`) | Số dòng thực (`COUNT(*)`) | Phát hiện chính |
|---|---:|---|
| `sag.data_source` | 3 | Cột `id` kiểu **`CHAR(36)`** tự động độn khoảng trắng ở cuối cho chuỗi 35 ký tự: `'tp_94e9997535651d8909088bee64844a1b '` (`LENGTH(id) = 36` vs `35` trong `sag_metric_e2e_67a1eb46.sources.sag_source_config_id`). |
| `sag.article` | 7 | Cho `tp_94e9997535651d8909088bee64844a1b`: 3 `COMPLETED` (`4bb7e2c9` là `sag-metric-partition-universe.md`, `72dd8960`, `f40275e9`), 4 `EXTRACTION_FAILED` (`3aa3f8d2`, `18bd9510`, `2a71d292`, `9aa95ef3`). |
| `sag.source_chunk` | 218 | 218 chunks của `tp_94e9997535651d8909088bee64844a1b`: chỉ **3 chunks** thuộc 3 `article` `COMPLETED` (mỗi bài 1 chunk), còn **215 chunks (98.6%)** thuộc 4 `article` `EXTRACTION_FAILED` (riêng `3aa3f8d2` chiếm 212 chunks)! |
| `sag.source_event` | 10 | 9 events của `tp_94e9997535651d8909088bee64844a1b` (5 events của `sag-metric-partition-universe.md` bị sinh bằng tiếng Trung) + 1 event của `smoke_live_qdrant`. |
| `sag.entity` / `sag.event_entity` | 29 / 39 | Thực thể của tài liệu tiếng Anh bị dịch sang tiếng Trung (`知识治理团队`, `证据门控`, `证据账本`, `访问控制服务`). |
