# Báo cáo Phase 1–2: SAG RAG và nền tảng tích hợp Laya Local

> Cập nhật: 2026-09-22
> Branch: `feat/verify-sag-rag-baseline-be-api`
> Trạng thái checkpoint: **PASS**

## 1. Phạm vi báo cáo

Báo cáo này ghi nhận phần đã chuẩn bị và kiểm chứng cho luồng:

```text
Upload tài liệu
  → tạo Document/Job
  → parse/chunk
  → embedding local
  → trích xuất event/entity bằng LLM local
  → persist/index vector
  → Document READY
  → retrieval trực tiếp
```

Laya Local chưa được chạy trong checkpoint này. Agent và LLM sinh câu trả lời cuối cũng chưa được chạy; mục tiêu là chứng minh dữ liệu đã sẵn sàng cho bước routing/query ở Phase 3.

## 2. Phase 1 — Nền tảng upload và Document/Job

### Mục tiêu

Xác nhận SAG có thể nhận tài liệu, lưu file, tạo `Document` và `Job`, sau đó chuyển giao việc xử lý cho background pipeline.

### Kết quả

| Hạng mục | Kết quả |
|---|---|
| Source tạo thành công | PASS |
| Upload fixture TXT | PASS |
| Document được tạo | PASS |
| Job `process_document` được tạo | PASS |
| Trạng thái ban đầu | `pending` / `queued` |
| Trạng thái cuối | `ready` / `succeeded` |
| Lỗi xử lý | Không có |

Thông tin lần chạy:

- Source ID: `0af2f37b178e451fb17844fa9c340486`
- Document ID: `a2d328402dc44b8696170b26e3154d8f`
- Job ID: `48dd99f6bab142978e8593405771fdaf`
- Job attempts: `1`
- Job progress: `1.0`

### Giới hạn của Phase 1 trong báo cáo này

Lần kiểm chứng này tập trung vào fixture TXT và API backend. Các trường hợp PDF/DOCX, kiểm thử giao diện upload và retry/reprocess chuyên biệt chưa được xem là hoàn tất trong checkpoint này.

## 3. Phase 2 — Parse, chunk, embedding và indexing

### Pipeline đã kiểm chứng

1. Parser `markitdown` đọc tài liệu thành công.
2. Nội dung được chuẩn hóa và chia thành `5` sections.
3. Pipeline tạo `1` chunk.
4. `Qwen3-Embedding-0.6B` local tạo vector dimension `1024`.
5. LLM local OpenAI-compatible trích xuất event/entity theo structured JSON.
6. Event, entity, reference và vector được persist/index.
7. Document chỉ chuyển `READY` sau khi pipeline hoàn tất.

### Kết quả cuối

| Hạng mục | Kết quả |
|---|---:|
| Document status | `ready` |
| Progress | `100` |
| Chunk count | `1` |
| Event count | `1` |
| Entity count | `3` |
| Reference count | `4` |
| Parser | `markitdown` |
| Error | `null` |

Event được lưu với ID `7a687652-699c-433c-97a0-fd8fe4cbdd46`, liên kết với chunk `5cf8a1d8-7e40-468c-bc17-329aeab29ea8`.

### Local model configuration

Embedding server:

```text
http://127.0.0.1:8080/v1
Qwen3-Embedding-0.6B
device=cpu
dimension=1024
```

LLM extraction server:

```text
http://127.0.0.1:8090/v1
provider=openai-compatible
model=Qwen2.5-1.5B-Instruct
temperature=0
structured_output_mode=json_object
```

Runtime API:

```text
http://127.0.0.1:18000
SAG_ENABLE_LAYA=false
```

Các file tiện ích local:

- [`local_llm_server.py`](../tools/local_llm_server.py)

## 4. Checkpoint A — Direct retrieval

Endpoint search sinh câu trả lời cuối không được gọi. Thay vào đó, retrieval service của SAG được gọi trực tiếp để kiểm tra chunk và điểm xếp hạng.

### Query 1

```text
Mã bí mật kiểm thử của Continuum AI là gì?
```

Kết quả:

- Chunk đúng: `5cf8a1d8-7e40-468c-bc17-329aeab29ea8`
- Rank: `0`
- Score: `0.955`
- Nội dung chứa: `CTM-92841`

### Query 2

```text
Dịch vụ thử nghiệm sử dụng gì để kiểm tra kết nối cache?
```

Kết quả:

- Chunk đúng: `5cf8a1d8-7e40-468c-bc17-329aeab29ea8`
- Rank: `0`
- Score: `0.955`
- Nội dung chứa: `Redis`

### Dimension proof

- Document vector dimension: `1024`
- Query vector length: `1024`
- `source_chunks` schema dimension: `1024`
- Kết luận: dimension khớp, không có lỗi schema/vector.

## 5. Thời gian xử lý

| Giai đoạn | Thời gian |
|---|---:|
| Toàn bộ job | `63.80s` |
| LLM extraction | khoảng `60s` |
| Persist/index | khoảng `2s` |
| Retrieval query CTM-92841 | `3716.89ms` |
| Retrieval query Redis | `361.53ms` |

## 6. Kiểm thử

Lệnh đã chạy:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_api_smoke.py tests/test_retrieval_relevance.py tests/test_document_job_retry.py
```

Kết quả: **36 passed**, thời gian `18.23s`.

## 7. Thay đổi trong PR

- Thêm fixture kiểm thử [`continuum_rag_baseline.txt`](../apps/api/tests/fixtures/continuum_rag_baseline.txt).
- Thêm local OpenAI-compatible embedding server.
- Thêm local OpenAI-compatible LLM server cho extraction smoke/full ingestion.
- Thêm `.data/` vào root `.gitignore` để tránh commit database/index/runtime data.
- Thêm báo cáo Phase 1–2 này.

Không commit `.env`, API key, model artifact, virtual environment hoặc dữ liệu runtime.

## 8. Phạm vi chưa thực hiện

- Chưa chạy Laya Local.
- Chưa kiểm chứng query routing greeting/knowledge bằng Laya.
- Chưa chạy Agent hoặc final answer generation.
- Chưa triển khai frontend tiếng Việt.
- Chưa kiểm thử đầy đủ PDF/DOCX và E2E qua giao diện.

## 9. Kết luận và bước tiếp theo

Phase 1 đạt baseline backend upload → Document/Job. Phase 2 và Checkpoint A đạt: tài liệu được parse, chunk, embedding, extraction, persist/index và retrieval trực tiếp thành công.

**Next: Phase 3 — standalone Laya Local validation.**
