# Bằng Chứng Kiểm Toán Chuyên Sâu: Project `6ad61cb2e1f989c367a1eb46` & Tài Liệu `sag-metric-partition-universe.md`

Ngày trích xuất: 2026-10-21 (Chế độ kết nối: `readonly=True`, `statement_timeout = '5s'`).

---

## 1. Xác Minh Trạng Thái Tài Liệu, Phiên Bản & File Snapshot Vật Lý (INV-2)

### 1.1 Truy vấn SQL đã chạy (trên schema `sag_metric_e2e_67a1eb46`)

```sql
SELECT id, tenant_id, project_id, source_id, sag_source_id, filename, status, progress,
       chunk_count, event_count, error, error_layer, error_stage, storage_path
FROM "sag_metric_e2e_67a1eb46".documents
WHERE project_id = '6ad61cb2e1f989c367a1eb46'
  AND filename = 'sag-metric-partition-universe.md';

SELECT id, document_id, version_no, file_hash, status, search_status, knowledge_status,
       valid_from, valid_to, metadata_json
FROM "sag_metric_e2e_67a1eb46".document_versions
WHERE document_id = '98f47e08-c8f8-5c3f-9843-1d222af80552';

SELECT id, document_version_id, storage_uri, original_filename, byte_size, checksum_sha256
FROM "sag_metric_e2e_67a1eb46".source_snapshots
WHERE document_version_id = '9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2';
```

### 1.2 Kết quả thực tế & Đối chiếu SHA-256 trên đĩa

| Trường / Kiểm tra | Giá trị thực tế trong DB / Đĩa | Đánh giá |
|---|---|---|
| `documents.id` | `98f47e08-c8f8-5c3f-9843-1d222af80552` | Khớp |
| `documents.project_id` | `6ad61cb2e1f989c367a1eb46` | Khớp |
| `documents.tenant_id` | `6ad615f6e1f989c367a1eac8` | Khớp |
| `documents.source_id` | `6f87b67132654b509b075fa911adf2b4` (`sag_source_config_id = 'tp_94e9997535651d8909088bee64844a1b'`) | Khớp |
| `documents.sag_source_id` | `4bb7e2c9-283b-4058-96b0-05ad06b6be3b` (khớp `sag.article.id`) | Khớp |
| `documents.status` / `progress` | `READY` / `100` (`chunk_count = 1`, `event_count = 5`, `token_usage = 6406`) | Khớp yêu cầu |
| `documents.error_layer` / `error_stage` | `error = NULL`, nhưng **`error_layer = 'engine'`**, **`error_stage = 'extract'`** | **LỖI VỆ SINH TRẠNG THÁI** (`jobs/tasks.py:672-680` chỉ xóa `error=None`, bỏ quên `error_layer` và `error_stage`) |
| `document_versions.id` (`version_no = 1`) | `9dcae843-fc85-5f85-ac13-0dd6fe8fd8a2` | Khớp |
| `document_versions.status` / `search_status` / `knowledge_status` | `SEARCH_READY` / `SEARCH_READY` / `DATA_READY` | Khớp 100% |
| `document_versions.metadata_json` | `{"security_partition_id": "6ad61cb2e1f989c367a1eb46", "knowledge_input_checksum": "d1a46b2f6c114dc076cd36f3051dd6bb22ab9d20b67e19c52b2d5b762a3d9f48", ...}` | Khớp |
| File vật lý `storage_path` / `storage_uri` | `D:\DoAnTotnghiep\sag-laya-integration\SAG\.data\engine\snapshots\e3\e34fa7e7a98198ffe713713078f3cfdfd0f142e26008689ef8aeb9e33978072b\sag-metric-partition-universe.md` | **TỒN TẠI** (`2,105 bytes`) |
| SHA-256 tính trực tiếp từ file trên đĩa | `e34fa7e7a98198ffe713713078f3cfdfd0f142e26008689ef8aeb9e33978072b` | **KHỚP 100%** với `document_versions.file_hash`, `source_snapshots.checksum_sha256` và `knowledge_units.provenance_json.source_checksum` |
| `knowledge_jobs` (`FOUNDATION`) | `id = '1fff6ad5-64be-576a-9dd1-4db8f3e21bf2'`, `status = 'SUCCEEDED'`, `attempts = 1` | Khớp (`E2` không có job nào được tạo do `e2_enabled = False`) |

---

## 2. Kiểm Tra 9 `canonical_blocks` & 4 `search_units` của `sag-metric-partition-universe.md`

- **9 `canonical_blocks`** (tất cả 9/9 đều khớp `sha256(normalized_text) == content_hash`):
  - `ordinal=0` (`heading`, `h1-0`): `"Northwind Labs Governance Universe"`
  - `ordinal=1` (`paragraph`, `p-1`, `3bc2d227-e7dc-5b0d-bb48-90b541c9221c`): `"This synthetic test note was prepared on 2026-10-19 for validating ingestion, extraction, tree publication, and project-scoped retrieval in Northwind Labs. It contains no production or personal data."`
  - `ordinal=2` (`heading`, `h2-2`): `"Evidence Gate Policy"`
  - `ordinal=3` (`paragraph`, `p-3`, `3dd25e37-1906-532b-be7b-5b3d6f565672`): `"On 2025-03-18, Northwind Labs' Knowledge Governance Team reviewed 120 answers produced by an internal language model and found 18 fabricated citations. The team recorded the incident in the AI Quality Register. On 2025-03-25, the team introduced an Evidence Gate: every factual answer must cite a source identifier, source version, and page or chunk. If the evidence score is below 0.75, the assistant must abstain and request human review. A reviewer can approve a corrected answer only after checking the cited passage in the original document."`
  - `ordinal=4` (`heading`, `h2-4`): `"Provenance and Security Partitioning"`
  - `ordinal=5` (`paragraph`, `p-5`, `8cc7711d-8ec5-5d11-ab08-fb23ef317f83`): `"The Evidence Ledger stores the original source URI, submitting team, observed date, SHA-256 content hash, and immutable document version. When a source changes, the system creates a new version and preserves the previous version for audit. Answers must identify the version used, because a statement that was correct in version 2 can be outdated in version 3."`
  - `ordinal=6` (`paragraph`, `p-6`, `02f7a034-4a38-5555-8a59-55b334e2570a`): `"Before retrieval, the Access Control Service filters evidence by organization, project, and security partition. It repeats the authorization check before returning citations. The service never treats a document's own text as an authorization instruction. This prevents one project from receiving another project's evidence and reduces the risk of prompt injection."`
  - `ordinal=7` (`heading`, `h2-7`): `"Operational Review"`
  - `ordinal=8` (`paragraph`, `p-8`, `0275f785-f5a8-5908-be29-428748b4ca96`): `"The Governance Team samples 50 answers each week. It tracks unsupported-claim rate, citation-match rate, stale-source rate, and cross-project denial checks. If the unsupported-claim rate exceeds 5 percent in a weekly sample, the team pauses automatic answers and investigates the Evidence Gate, source freshness, and model configuration. This process keeps extraction traceable and gives auditors a concrete path from an answer to the source passage that supports it."`
- **4 `search_units`** (tất cả 4/4 đều có mặt trong Qdrant collection `search_units_6ad61cb2e1f989c367a1eb46` với vector `dense` 1024 chiều + `sparse_text`):
  1. `0a36f3bc-c940-5bf2-b570-f5bf721fb2c0` (gộp `h1-0` .. `p-1`)
  2. `de0e3315-f4cf-52c0-87e1-e6174ac5c555` (gộp `h2-2` .. `p-3`)
  3. `b51f31b7-f1fa-55de-b037-d4ef1b1c95f6` (gộp `h2-4` .. `p-6`, tức gộp cả `p-5` và `p-6`)
  4. `4ffdc768-0cf0-544d-89cd-8ed94170ad28` (gộp `h2-7` .. `p-8`)

---

## 3. Đánh Giá Chi Tiết 5 `knowledge_units`, 26 `knowledge_evidence` & 2 Quan Hệ `E1` `CO_OCCURS`

### 3.1 Bảng tổng hợp 5 `knowledge_units` hiện hành (`is_current = TRUE`)

| Unit ID (`ordinal`) | Block gốc (`source_anchor`) | Checksum (`content_hash` / `unit_checksum`) | `features_json.entities` (E0) | Thực thể miền bị E0 bỏ sót | `local_confidence` / `importance` / `e2_reason` | Số `knowledge_evidence` |
|---|---|---|---|---|---|---|
| `b8d25158-593d-5cf1-a1c8-856f911dd6ca` (`ord=1`) | `3bc2d227...` (`p-1`) | Hợp lệ 100% | `["2026-10-19"]` (1) | `"Northwind Labs"` | `0.68` / `0.35` / `null` (Không đủ ngưỡng 0.5 để chạy E2!) | 3 (`1 entity`, `2 claim`) |
| `711097df-4121-533f-8c5a-a35dabdeae95` (`ord=3`) | `3dd25e37...` (`p-3`) | Hợp lệ 100% | `["0.75", "2025-03-18", "2025-03-25", "AI"]` (4) | `"Northwind Labs"`, `"Knowledge Governance Team"`, `"AI Quality Register"`, `"Evidence Gate"`, `"120 answers"`, `"18 fabricated citations"` | `0.68` / `0.80` / `"high_importance"` | 11 (`4 entity`, `6 claim` — **2 câu bị cắt đôi sai tại `0.75`**, `1 relation` — **ghép cặp sai**) |
| `4f3f0c92-122a-5309-9607-026b42502dba` (`ord=5`) | `8cc7711d...` (`p-5`) | Hợp lệ 100% | `["SHA-256", "URI"]` (2) | `"Evidence Ledger"`, `"version 2"`, `"version 3"` | `0.68` / `0.60` / `"high_importance"` | 6 (`2 entity`, `3 claim`, `1 relation` — **thiếu chủ thể chính**) |
| `55a58f78-2ff2-55d0-accb-755290517a78` (`ord=6`) | `02f7a034...` (`p-6`) | Hợp lệ 100% | **`[]` (0 thực thể!)** | `"Access Control Service"`, `"security partition"`, `"prompt injection"` | `0.60` / **`0.25`** / **`null`** (Bị loại khỏi E2!) | 4 (`0 entity`, `4 claim`, `0 relation`) |
| `cc5c6c78-6b99-54d4-99ab-e51e41a2c57e` (`ord=8`) | `0275f785...` (`p-8`) | Hợp lệ 100% | **`[]` (0 thực thể!)** | `"Governance Team"`, `"50 answers"`, `"5 percent"`, `"Evidence Gate"` | `0.60` / **`0.25`** / **`null`** (Bị loại khỏi E2!) | 4 (`0 entity`, `4 claim`, `0 relation`) |

> **Lưu ý về số lượng `knowledge_evidence`**: Tổng cộng 5 unit có `3 + 11 + 6 + 4 + 4 = 28` dòng `knowledge_evidence` thuộc tài liệu `sag-metric-partition-universe.md` (gồm 7 `entity`, 19 `claim`, 2 `relation`), cộng với 14 dòng của 2 tài liệu `READY` còn lại = 42 dòng trong bảng `sag_metric_e2e_67a1eb46.knowledge_evidence`.

### 3.2 Bằng chứng 3 Lỗi Ngữ Nghĩa Nghiêm Trọng trong `extract_e0_e1` (`services/knowledge_service.py:54-189`)

#### (A) Lỗi tách câu (`claim`) cắt ngang số thập phân `0.75` (`knowledge_service.py:128`)
- **Code**: `for match in re.finditer(r"[^.!?\n]+(?:[.!?]|$)", text):`
- **Bằng chứng DB** trên Unit `711097df-4121-533f-8c5a-a35dabdeae95` (`p-3`):
  - `KnowledgeEvidence` `ac6ec2da-fb96-5e26-852c-bf23e3940255` (`tier='E0'`, `kind='claim'`, `start=350`, `end=384`):
    ```json
    {"quote": " If the evidence score is below 0.", "confidence": 0.55, "source_anchor": "p-3"}
    ```
  - `KnowledgeEvidence` `c16fc8f3-891c-5b3f-a801-13eb27911402` (`tier='E0'`, `kind='claim'`, `start=384`, `end=440`):
    ```json
    {"quote": "75, the assistant must abstain and request human review.", "confidence": 0.55, "source_anchor": "p-3"}
    ```
- **Tác động**: Làm hỏng hoàn toàn mệnh đề quan trọng nhất của chính sách `Evidence Gate` (`score < 0.75` bị biến thành `score < 0.` và một mảnh câu cụt `"75, the assistant..."`). Các `claim` này được trả thẳng ra API `knowledge_universe_detail` (`universe_knowledge_service.py:437`) và đưa vào prompt sinh câu hỏi nghiên cứu `KnowledgeGapQuestion` (`knowledge_gap_question_service.py:90-126`).

#### (B) Đánh giá tính chính xác ngữ nghĩa của 2 Quan hệ `E1` `CO_OCCURS` (`knowledge_service.py:148-167`)
- **Code hiện tại**:
  ```python
  labels = sorted({str(f["label"]) for f in facts if f["kind"] in {"entity", "alias"}})
  if len(labels) >= 2 and len(facts) < config.max_evidence_per_unit:
      facts.append({
          "tier": "E1", "kind": "relation", ...,
          "subject": labels[0], "predicate": "CO_OCCURS", "object": labels[1],
          "quote": text, "start": 0, "end": len(text), ...
      })
  ```
- **Quan hệ E1 #1** — `id = 'ab457b32-cb90-544c-9881-0bdde0dbcf4b'` (thuộc Unit `711097df`, `p-3`):
  - **Nội dung DB**: `subject = "0.75"`, `predicate = "CO_OCCURS"`, `object = "2025-03-18"`, `confidence = 0.4`, `quote = <toàn bộ đoạn p-3 dài 545 ký tự>`.
  - **Đánh giá ngữ nghĩa**: **SAI / GÂY HIỂU LẦM (Semantically Misleading & Spurious)**.
    - Trong đoạn `p-3`, tập `labels` do E0 rút ra là `["0.75", "2025-03-18", "2025-03-25", "AI"]`. Do sắp xếp ASCII (`sorted()`), hai phần tử đầu tiên là `"0.75"` và `"2025-03-18"`.
    - Thực tế trong văn bản: ngày `2025-03-18` thuộc **câu 1** (ngày kiểm tra 120 câu trả lời và phát hiện 18 trích dẫn bịa đặt), còn ngưỡng điểm `0.75` thuộc **câu 4** (quy định của `Evidence Gate`, vốn được ban hành vào ngày **`2025-03-25`** ở câu 3!). Việc ghép `"0.75" --CO_OCCURS--> "2025-03-18"` tạo liên kết sai giữa ngưỡng `0.75` với ngày `2025-03-18` và bỏ qua hoàn toàn ngày đúng `2025-03-25` cũng như chủ thể `"Evidence Gate"`.
- **Quan hệ E1 #2** — `id = '850e36a8-c0dd-5832-a910-1b63604480b9'` (thuộc Unit `4f3f0c92`, `p-5`):
  - **Nội dung DB**: `subject = "SHA-256"`, `predicate = "CO_OCCURS"`, `object = "URI"`, `confidence = 0.4`, `quote = <toàn bộ đoạn p-5 dài 357 ký tự>`.
  - **Đánh giá ngữ nghĩa**: **NGHÈO NÀN / MẤT CHỦ THỂ CHÍNH (Incomplete / Missing Hub Entity)**.
    - Trong câu 1 của `p-5`: *"The Evidence Ledger stores the original source URI, submitting team, observed date, SHA-256 content hash, and immutable document version."*
    - Vì regex `_ENTITY_RE` không bắt được `"Evidence Ledger"`, chỉ còn 2 từ viết hoa `"SHA-256"` và `"URI"` là hai thuộc tính ngang hàng được lưu bởi `Evidence Ledger`. Ghép `"SHA-256" --CO_OCCURS--> "URI"` làm mất thực thể trung tâm (`Evidence Ledger`).
- **Tương tự trên 2 tài liệu `READY` còn lại của project** (`5ec94f5f` và `9a1c589f`): E1 sinh ra 2 quan hệ giống hệt nhau `subject = "AI"`, `predicate = "CO_OCCURS"`, `object = "ID"` chỉ vì `"AI"` và `"ID"` (trong cụm `"source ID"`) đứng đầu bảng chữ cái!
- **Hơn nữa**: Không có bất kỳ hàm nào trong toàn bộ codebase (`knowledge_service.py`, `knowledge_quality_repository.py`, `universe_knowledge_service.py`) đọc các trường `subject`, `predicate`, `object` của `KnowledgeEvidence`!

---

## 4. Bằng Chứng Nhiễm Bẩn Ngôn Ngữ Tiếng Trung ở Nhánh `zleap-sag` (`sag.source_event` & `sag.entity`)

Do `settings.sag_language` mặc định là `"zh"` (`sag_api/core/config.py:161`), khi `IncrementalDocumentProcessor` (`sag_api/sag/incremental_processor.py:65-73`) trích xuất sự kiện và thực thể từ tài liệu tiếng Anh `sag-metric-partition-universe.md` (`sag.article.id = '4bb7e2c9-283b-4058-96b0-05ad06b6be3b'`), LLM đã sinh ra **5 sự kiện và 15 thực thể hoàn toàn bằng tiếng Trung**:

| `sag.source_event.id` | `rank` | `title` (trong DB `sag.source_event`) | `summary` (trong DB `sag.source_event`) | Thực thể gắn kèm (`sag.entity.name` / `type`) |
|---|---:|---|---|---|
| `5b24be26-0732-434e-bf9e-d2af8a31fc55` | 1 | `Northwind Labs 合成测试说明准备完成` | `2026年10月19日准备了一份合成测试说明，用于验证Northwind Labs中的摄入、提取、树发布和项目范围检索。` | `2026-10-19` (`time`), `Northwind Labs` (`organization`) |
| `2c580259-28a3-4636-bc76-083dd4c4c1f5` | 2 | `Northwind Labs 发现并处理 AI 生成的虚假引文` | `2025年3月18日，Northwind Labs的知识治理团队审查了120个由内部语言模型生成的答案...` | `2025-03-18` (`time`), `2025-03-25` (`time`), `Northwind Labs` (`organization`), `知识治理团队` (`organization`), `AI质量登记册` (`document`), `证据门控` (`policy`), `0.75` (`metric`) |
| `44167799-8d39-40b2-8fa1-2c1d675b6f3b` | 3 | `证据账本管理源版本和审计信息` | `证据账本存储原始源URI、提交团队、观察日期、SHA-256内容哈希和不可变文档版本...` | `证据账本` (`system`), `SHA-256` (`identifier`) |
| `f63506ae-a074-41d5-bb4e-e0d74a15bc8f` | 4 | `访问控制服务实施检索前后的授权检查` | `在检索之前，访问控制服务根据组织、项目和安全分区过滤证据...` | `访问控制服务` (`service`), `安全分区` (`identifier`) |
| `e394058c-562c-414b-a10d-eddc5c8b48fb` | 5 | `治理团队每周抽样检查自动回答质量` | `治理团队每周抽样检查50个答案，跟踪无支持声明率...` | `治理团队` (`organization`), `证据门控` (`policy`), `50 个答案` (`quantity`), `5%` (`percentage`) |
