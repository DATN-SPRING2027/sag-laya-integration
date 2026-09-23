# Kế hoạch triển khai luồng Upload tài liệu → RAG → Laya Local → LLM

## 1. Mục tiêu

Hoàn thiện và kiểm chứng luồng xử lý tài liệu của SAG theo thứ tự:

```text
Upload tài liệu
    ↓
SAG lưu file + tạo Document/Job
    ↓
Background worker đọc tài liệu, tách đoạn, tạo dữ liệu tìm kiếm/vector
    ↓
Document chuyển sang READY
    ↓
User đặt câu hỏi
    ↓
Laya Local phân loại câu hỏi
    ↓
Nếu là câu hỏi kiến thức → SAG tìm kiếm tài liệu
    ↓
LLM sinh câu trả lời dựa trên context đã tìm được
```

Mục tiêu cuối cùng là người dùng có thể upload tài liệu, đợi tài liệu sẵn sàng, hỏi về nội dung tài liệu và nhận câu trả lời có căn cứ từ tài liệu liên quan.

## 2. Trạng thái hiện tại

- SAG đã có API upload, tạo `Document` và tạo background `Job`.
- SAG đã có pipeline xử lý tài liệu và trạng thái `READY`.
- Laya Local đã được tích hợp ở bước phân loại câu hỏi.
- Laya Local hiện chỉ route câu hỏi; chưa xử lý trực tiếp từng tài liệu hoặc thay thế embedding/vector search.
- LLM trong trang Settings là model sinh câu trả lời cuối cùng, không phải Laya Local.
- PostgreSQL là relational store chính cho user, source, document, job và OCTX state; Qdrant là vector store chính cho embedding và retrieval.
- Cấu hình Laya Local hiện dùng biến môi trường:

```env
SAG_ENABLE_LAYA=true
SAG_LAYA_MODEL_PATH=C:\laya-local
SAG_LAYA_DEVICE=cpu
```

## 3. Quyết định kiến trúc

### 3.1. Phân tách vai trò

| Thành phần | Vai trò |
|---|---|
| Upload API | Nhận file, validate và tạo Document/Job |
| Background worker | Parse, chunk, tạo dữ liệu indexing và cập nhật trạng thái |
| Knowledge/RAG layer | Tìm các đoạn tài liệu liên quan đến câu hỏi |
| Laya Local | Phân loại intent, chitchat và nhu cầu retrieval |
| Agent service | Quyết định tool/retrieval flow và điều phối request |
| LLM trong Settings | Tổng hợp context và sinh câu trả lời |
| Frontend | Upload, hiển thị tiến trình/trạng thái và hiển thị câu trả lời |

### 3.2. Nguyên tắc routing an toàn

- Chỉ bỏ qua retrieval khi Laya nhận diện chitchat với confidence cao.
- Với câu hỏi factual/knowledge hoặc kết quả Laya không chắc chắn, vẫn cho phép SAG tìm kiếm tài liệu.
- Laya không được làm mất context của câu hỏi chỉ vì model trả về nhãn `noul` hoặc confidence thấp.
- Nếu Laya lỗi hoặc chưa load được model, SAG phải có fallback và vẫn xử lý được câu hỏi theo cơ chế retrieval hiện có.

### 3.3. Quyết định storage và indexing

Ranh giới ownership giữa Continuum BE và SAG được ghi rõ trong
[`docs/data-ownership-and-storage.md`](../docs/data-ownership-and-storage.md).
BE sở hữu user/project/team/permission; SAG sở hữu metadata RAG trong
PostgreSQL và vector trong Qdrant.

- `sag_relational_provider=postgres` là cấu hình relational mặc định.
- `sag_vector_provider=qdrant` là cấu hình vector mặc định.
- PostgreSQL lưu metadata và trạng thái nghiệp vụ; Qdrant lưu vector collection, payload chunk và kết quả vector search.
- Embedding provider/model vẫn là một cấu hình độc lập với Qdrant. Qdrant chỉ lưu và tìm vector, không tự tạo embedding.
- Qdrant phải được cấu hình bằng endpoint/API key qua environment; secret không được commit.
- Quyền truy cập tài liệu phải được truyền từ BE bằng external ID/scope đã xác thực; SAG không tin tưởng `project_id` hoặc permission do frontend tự gửi.
- Vì Qdrant không cung cấp lexical search trong adapter hiện tại, strategy `multi` có thể fallback về `vector`. Test phải kiểm tra cả strategy yêu cầu và strategy thực tế.
- Unit test có thể dùng in-memory/mock Qdrant, nhưng Checkpoint A và integration test phải có ít nhất một lần chạy với Qdrant thật (local container hoặc Qdrant Cloud).

## 4. Kế hoạch theo phase

### Phase 0 — Chuẩn bị môi trường và hợp đồng dữ liệu

**Mục tiêu:** Xác nhận các thành phần nền tảng, cấu hình và contract giữa upload, job, retrieval và Laya.

**Công việc:**

- [ ] Xác nhận đường dẫn model Laya Local và device chạy model.
- [ ] Xác nhận các biến môi trường `SAG_ENABLE_LAYA`, `SAG_LAYA_MODEL_PATH`, `SAG_LAYA_DEVICE`.
- [ ] Kiểm tra cấu hình LLM trong Settings: provider, base URL, API key và model.
- [ ] Chuẩn hóa các trạng thái Document/Job: `PENDING`, `LOADING`, `EXTRACTING`, `READY`, `FAILED`.
- [ ] Xác định format tối thiểu của chunk: `document_id`, nội dung, vị trí/trang, metadata và source reference.
- [ ] Xác định response contract của Laya: intent, domain, `is_chitchat`, `need_retrieval`, confidence và model.

**Tiêu chí nghiệm thu:**

- [ ] Có file `.env` hoặc cấu hình triển khai chứa đủ biến cần thiết nhưng không commit secret.
- [ ] Có tài liệu mô tả rõ component nào chịu trách nhiệm cho từng bước.
- [ ] Có test hoặc fixture cho response contract của Laya.

**Phụ thuộc:** Không có.

**Phạm vi dự kiến:** Nhỏ.

---

### Phase 1 — Upload và quản lý Document/Job

**Mục tiêu:** Upload được tài liệu và theo dõi được trạng thái xử lý.

**Công việc:**

- [ ] Kiểm tra frontend gửi multipart đúng endpoint upload.
- [ ] Kiểm tra validation extension, MIME type, kích thước file và tên file.
- [ ] Kiểm tra file gốc được lưu đúng `SAG_UPLOAD_DIR`.
- [ ] Kiểm tra Document được tạo cùng metadata: tên, loại file, source, owner và timestamps.
- [ ] Kiểm tra Job được tạo và không bị tạo trùng khi frontend retry.
- [ ] Bổ sung hoặc hoàn thiện API lấy trạng thái Document/Job.
- [ ] Hiển thị trạng thái upload và xử lý trên frontend.
- [ ] Xử lý lỗi upload, timeout, cancel và retry rõ ràng.

**Tiêu chí nghiệm thu:**

- [ ] Upload PDF/DOCX/TXT hợp lệ tạo đúng một Document và một Job.
- [ ] File không hợp lệ bị từ chối trước khi chạy pipeline nặng.
- [ ] Frontend hiển thị được trạng thái đang xử lý, thành công và thất bại.
- [ ] Reprocess có thể chạy lại một Document lỗi mà không tạo bản ghi rác.

**Phụ thuộc:** Phase 0.

**Files dự kiến:**

- `SAG/apps/api/sag_api/api/v1/documents.py`
- `SAG/apps/api/sag_api/services/document_service.py`
- `SAG/apps/api/sag_api/jobs/tasks.py`
- `SAG/apps/api/sag_api/jobs/inproc.py`
- `SAG/apps/web/lib/api.ts`
- Component upload tài liệu ở frontend.

**Phạm vi dự kiến:** Trung bình.

---

### Phase 2 — Parse, chunk và indexing tài liệu

**Mục tiêu:** Biến tài liệu upload thành dữ liệu có thể truy xuất bằng RAG.

**Công việc:**

- [ ] Xác nhận parser tương ứng với PDF, DOCX, TXT và các định dạng được hỗ trợ.
- [ ] Chuẩn hóa text: bỏ nội dung rỗng, xử lý whitespace, encoding và ký tự lỗi.
- [ ] Thiết kế chunking theo heading/đoạn/trang, có overlap hợp lý.
- [ ] Gắn metadata vào từng chunk: `document_id`, source, page, section, title và timestamps.
- [ ] Tạo embedding hoặc dữ liệu search theo pipeline hiện có của SAG.
- [ ] Tạo hoặc kiểm tra Qdrant collection với dimension khớp embedding model và lưu payload metadata của chunk.
- [ ] Lưu chunk/index theo transaction hoặc cơ chế idempotent.
- [ ] Xóa hoặc thay thế index cũ khi reprocess.
- [ ] Đảm bảo bản ghi Document/Job trong PostgreSQL và vector/payload tương ứng trong Qdrant không bị lệch.
- [ ] Ghi log theo từng stage để biết lỗi xảy ra ở parse, chunk, embedding hay persist.
- [ ] Đảm bảo worker không làm mất trạng thái cuối cùng nếu một tài liệu bị lỗi.

**Tiêu chí nghiệm thu:**

- [ ] Một tài liệu hợp lệ tạo ra các chunk có nội dung và metadata đầy đủ.
- [ ] Document chỉ chuyển sang `READY` sau khi indexing hoàn tất.
- [ ] Nếu parse/indexing lỗi, Document chuyển `FAILED` và có error message có thể hiển thị.
- [ ] Reprocess không tạo duplicate chunk hoặc duplicate vector.
- [ ] Có thể tìm thấy một đoạn nội dung đã biết bằng truy vấn tương ứng.
- [ ] Query không làm mất metadata dùng cho source/document/page/citation trong payload Qdrant.

**Phụ thuộc:** Phase 1.

**Files dự kiến:**

- `SAG/apps/api/sag_api/jobs/tasks.py`
- Các parser/extractor trong `SAG/apps/api/sag_api/`
- Các module chunking, embedding và search/index.
- Test ingestion và retrieval.

**Phạm vi dự kiến:** Lớn; nên tách tiếp thành parse/chunk và indexing nếu triển khai độc lập.

---

### Checkpoint A — Tài liệu đã sẵn sàng cho RAG

- [ ] Upload một tài liệu mẫu thành công.
- [ ] Document chuyển đúng sang `READY`.
- [ ] Có thể truy vấn trực tiếp và nhận được chunk liên quan.
- [ ] Reprocess và failure path đã được kiểm tra.
- [ ] Đã kiểm tra bằng đúng cấu hình PostgreSQL + Qdrant của môi trường triển khai.
- [ ] Không có secret trong log hoặc file cấu hình đã commit.

---

### Phase 3 — Tích hợp Laya Local vào query routing

**Mục tiêu:** Dùng Laya Local để phân loại câu hỏi trước khi agent quyết định retrieval.

**Công việc:**

- [ ] Load Laya Local lazy, không load model khi API chưa có request cần dùng.
- [ ] Hỗ trợ model multilingual cho câu hỏi tiếng Việt.
- [ ] Cấu hình `model_path` và `device` từ environment.
- [ ] Cache singleton router để không load model ở mỗi request.
- [ ] Cache init error hoặc dùng fallback để tránh retry load model nặng liên tục.
- [ ] Chuẩn hóa timeout và xử lý exception của Laya.
- [ ] Gọi Laya trong bước initial tool choice của agent.
- [ ] Với chitchat confidence cao, chọn `none` hoặc bỏ qua retrieval.
- [ ] Với câu hỏi kiến thức, chọn flow có retrieval.
- [ ] Với kết quả không chắc chắn, ưu tiên retrieval thay vì trả lời không có nguồn.

**Tiêu chí nghiệm thu:**

- [ ] Câu chào hỏi không gọi search_context.
- [ ] Câu hỏi về nội dung tài liệu gọi retrieval.
- [ ] Câu hỏi tiếng Việt được route đúng bằng model multilingual.
- [ ] Laya không load lại model ở mỗi request.
- [ ] Khi Laya không khả dụng, SAG vẫn xử lý được câu hỏi bằng fallback.
- [ ] Có test cho greeting, factual query, low confidence và init failure.

**Phụ thuộc:** Phase 0 và Phase 2.

**Files dự kiến:**

- `SAG/apps/api/sag_api/services/laya_router.py`
- `SAG/apps/api/sag_api/api/v1/laya.py`
- `SAG/apps/api/sag_api/services/agent_service.py`
- `SAG/apps/api/tests/test_laya_router.py`
- `SAG/compose.yaml`

**Phạm vi dự kiến:** Trung bình.

---

### Phase 4 — Retrieval và đưa context vào Agent/LLM

**Mục tiêu:** Kết nối kết quả Laya với công cụ tìm kiếm tài liệu và context cho LLM.

**Công việc:**

- [ ] Xác định tool retrieval chính: `search_context`, entity search hoặc tool tương đương.
- [ ] Xác định Qdrant là vector path chính và ghi rõ behavior khi `multi` fallback về `vector`.
- [ ] Đảm bảo query gốc của người dùng được giữ nguyên khi tìm kiếm.
- [ ] Truyền filter theo source, document, quyền truy cập hoặc domain nếu có.
- [ ] Xếp hạng và giới hạn số chunk trả về.
- [ ] Loại bỏ chunk trùng lặp.
- [ ] Truyền metadata nguồn vào context để tạo citation/reference.
- [ ] Đặt giới hạn context để không vượt context window của LLM.
- [ ] Xử lý trường hợp không tìm thấy context.
- [ ] Ngăn LLM bịa thông tin khi câu hỏi yêu cầu dữ liệu không có trong tài liệu.

**Tiêu chí nghiệm thu:**

- [ ] Câu hỏi knowledge trả về ít nhất một context liên quan khi tài liệu có dữ liệu.
- [ ] Câu trả lời chỉ sử dụng context được chọn hoặc nêu rõ khi không đủ dữ liệu.
- [ ] Citation trỏ được về tài liệu/chunk/trang tương ứng.
- [ ] Không gửi toàn bộ tài liệu vào prompt khi chỉ cần một số chunk.
- [ ] Truy vấn chitchat không chạy retrieval không cần thiết.

**Phụ thuộc:** Phase 2 và Phase 3.

**Files dự kiến:**

- `SAG/apps/api/sag_api/services/agent_service.py`
- `SAG/apps/api/sag_api/tools/`
- Module search/context builder.
- API response schema và frontend hiển thị citation.

**Phạm vi dự kiến:** Lớn.

---

### Phase 5 — LLM generation và cấu hình Settings

**Mục tiêu:** Đảm bảo LLM trong Settings nhận đúng context và tạo câu trả lời ổn định.

**Công việc:**

- [ ] Kiểm tra provider protocol và Base URL OpenAI-compatible.
- [ ] Kiểm tra API key không bị log hoặc lưu plaintext ngoài cơ chế cấu hình hiện tại.
- [ ] Kiểm tra model, context window, maximum output tokens, temperature, timeout và retry.
- [ ] Tách rõ lỗi Laya, lỗi retrieval và lỗi LLM trên response/log.
- [ ] Thiết lập prompt yêu cầu trả lời dựa trên context.
- [ ] Thiết lập behavior khi context rỗng hoặc confidence thấp.
- [ ] Hiển thị trạng thái lỗi dễ hiểu trên frontend.

**Tiêu chí nghiệm thu:**

- [ ] LLM sinh được câu trả lời từ context của tài liệu.
- [ ] LLM không được gọi nếu request bị chặn từ bước validation.
- [ ] Timeout/retry hoạt động đúng và không tạo duplicate job.
- [ ] Người dùng phân biệt được câu trả lời có nguồn và câu trả lời hội thoại thông thường.

**Phụ thuộc:** Phase 4.

**Phạm vi dự kiến:** Trung bình.

---

### Phase 6 — Frontend, tiếng Việt và trải nghiệm end-to-end

**Mục tiêu:** Người dùng có thể thao tác toàn bộ quy trình bằng giao diện tiếng Việt.

**Công việc:**

- [ ] Hiển thị tiến trình upload và indexing.
- [ ] Disable nút hỏi hoặc cảnh báo khi tài liệu chưa ở trạng thái `READY`.
- [ ] Hiển thị lỗi parse/indexing/retrieval/LLM bằng tiếng Việt.
- [ ] Hiển thị nguồn/citation của câu trả lời.
- [ ] Việt hóa các nhãn liên quan đến upload, processing, ready, failed và retry.
- [ ] Đảm bảo model settings vẫn cho phép cấu hình LLM độc lập với Laya Local.
- [ ] Thêm trạng thái hoặc diagnostic để biết Laya Local đang enabled/disabled.

**Tiêu chí nghiệm thu:**

- [ ] Người dùng hiểu tài liệu đang upload, đang xử lý hay đã sẵn sàng.
- [ ] Người dùng biết câu trả lời lấy từ tài liệu nào.
- [ ] Các lỗi chính được hiển thị bằng tiếng Việt, không lộ stack trace.
- [ ] Giao diện không nhầm Laya Local với LLM sinh câu trả lời.

**Phụ thuộc:** Phase 1, Phase 4 và Phase 5.

**Phạm vi dự kiến:** Trung bình.

---

### Phase 7 — Test, quan sát và tối ưu

**Mục tiêu:** Xác minh hệ thống ổn định với dữ liệu thật và dễ chẩn đoán khi lỗi.

**Công việc:**

- [ ] Unit test cho upload validation, chunking, routing và context builder.
- [ ] Integration test upload → job → READY → search.
- [ ] E2E test upload → hỏi → retrieval → LLM answer.
- [ ] Test Laya greeting, câu hỏi factual, tiếng Việt, fallback và model unavailable.
- [ ] Đo thời gian parse, indexing, Laya first-load, Laya inference, retrieval và LLM.
- [ ] Theo dõi số lượng job `FAILED`, thời gian xử lý và lỗi theo stage.
- [ ] Có smoke test với PostgreSQL và Qdrant thật; không chỉ dựa vào mock adapter.
- [ ] Kiểm tra memory/CPU khi Laya chạy CPU.
- [ ] Kiểm tra xử lý đồng thời nhiều request hỏi và nhiều job upload.
- [ ] Tạo bộ tài liệu mẫu và bộ câu hỏi chuẩn để regression test.

**Tiêu chí nghiệm thu:**

- [ ] Test focused và integration pass.
- [ ] Có test chứng minh Document không chuyển `READY` khi indexing chưa xong.
- [ ] Có log đủ để xác định request lỗi ở upload, worker, Laya, retrieval hay LLM.
- [ ] Không có regression nghiêm trọng trên flow chat/search hiện tại.

**Phụ thuộc:** Tất cả phase trước.

**Phạm vi dự kiến:** Lớn.

## 5. Dependency graph

```text
Phase 0: Contract/config
       ↓
Phase 1: Upload + Document/Job
       ↓
Phase 2: Parse + chunk + indexing
       ↓
Checkpoint A
       ↓
Phase 3: Laya query routing ─────┐
       ↓                         │
Phase 4: Retrieval + context ←───┘
       ↓
Phase 5: LLM generation
       ↓
Phase 6: Frontend Vietnamese UX
       ↓
Phase 7: Test + observability
```

## 6. Rủi ro và cách giảm thiểu

| Rủi ro | Mức độ | Cách giảm thiểu |
|---|---:|---|
| Laya load chậm lần đầu trên CPU | Trung bình | Lazy load, singleton cache, warm-up khi startup tùy môi trường |
| Laya phân loại sai câu hỏi factual | Cao | Chỉ bỏ retrieval khi chitchat confidence cao; fallback về retrieval |
| Document chuyển READY quá sớm | Cao | Chỉ cập nhật READY sau khi parse, chunk và indexing thành công |
| Reprocess tạo duplicate chunk/vector | Cao | Dùng idempotency key và xóa/thay thế index cũ |
| Tài liệu dài vượt context window | Cao | Chunk, top-k, rerank và giới hạn context |
| LLM trả lời không có căn cứ | Cao | Prompt grounding, citation và response khi không đủ context |
| Lộ API key trong log/frontend | Cao | Không log secret, chỉ lưu theo cơ chế bảo mật của ứng dụng |
| Worker lỗi nhưng UI không biết | Trung bình | Persist error stage/message và API polling hoặc event status |
| Parser xử lý khác nhau giữa các định dạng | Trung bình | Bộ tài liệu mẫu theo từng định dạng và integration test |

## 7. Definition of Done

- [ ] Upload được tài liệu hợp lệ.
- [ ] Document/Job có trạng thái và lỗi rõ ràng.
- [ ] Tài liệu được parse, chunk và indexing trước khi chuyển `READY`.
- [ ] Có thể tìm được nội dung đã upload bằng query phù hợp.
- [ ] Laya Local route đúng câu hỏi tiếng Việt và có fallback.
- [ ] Câu hỏi knowledge nhận được context từ tài liệu.
- [ ] LLM tạo câu trả lời dựa trên context và có nguồn tham chiếu.
- [ ] Chitchat không gọi retrieval không cần thiết.
- [ ] Giao diện hiển thị trạng thái và lỗi bằng tiếng Việt.
- [ ] Unit, integration và E2E tests quan trọng đều pass.

## 8. Các điểm cần xác nhận trước khi triển khai tiếp

- [x] SAG dùng PostgreSQL cho relational state và Qdrant cho vector retrieval production.
- [ ] Embedding model hiện tại là model nào và chạy ở đâu?
- [ ] Có yêu cầu filter quyền truy cập theo user/source/document không?
- [ ] Cần hỗ trợ thêm định dạng nào ngoài PDF, DOCX và TXT?
- [ ] Có muốn Laya tham gia thêm vào ingestion/document classification không, hay chỉ giữ vai trò query router?
- [ ] Mục tiêu latency cho upload indexing và query end-to-end là bao nhiêu?
