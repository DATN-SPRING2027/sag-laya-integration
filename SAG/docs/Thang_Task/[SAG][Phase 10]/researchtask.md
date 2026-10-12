# Nghiên cứu Kỹ thuật: Checkpoint D & Phase 10 — Research Agent (Bounded Ingestion)

> **Nguồn chuẩn:** `SAG/tasks/plan.md` (Phase 10, Checkpoint D — AGENT_READY), `SAG/tasks/todo.md` (Phase 10 & Checkpoint D), và workflow v1.1 mục 16.  
> **Nguyên tắc Ponytail:** Tối giản, thực dụng, tuyệt đối tránh over-engineering. Tận dụng tối đa logic/patterns sẵn có trong repo, không thêm dependency mới, không xây dựng agent runtime rườm rà, đưa dữ liệu đã fetch quay về trực tiếp qua Normal Ingestion Pipeline.

---

## 1. Bối cảnh & Ranh giới Kỹ thuật (Context & Boundaries)

### 1.1 Vị trí trong Chu kỳ Phát triển
* **Phase 9 (DATN-63) — Đã hoàn tất:**
  * Hệ thống chấm điểm chất lượng tri thức xác định (`KnowledgeQualityAssessment`, `KnowledgeGap`).
  * Bộ sinh câu hỏi nghiên cứu có căn cứ và giới hạn (`KnowledgeGapQuestion`, `generate_question_for_gap`).
  * Chỉ các gap đủ điều kiện (`eligible=True`, có anchored evidence, priority đạt ngưỡng) mới có câu hỏi.
* **Phase 10 & Checkpoint D — AGENT_READY (Mục tiêu hiện tại):**
  * Biến các scored gap thành bounded research tasks.
  * Thiết lập vòng lặp có kiểm soát: `Gap Detector` → `Question Generator` → `Research Planner` → `Discovery/Fetch` → `Normal Ingestion` → `Incremental Tree Update`.
  * Đảm bảo tính có căn cứ (traceability), hạn mức ngân sách (budget), danh sách nguồn cho phép (domain allowlist), chống trùng lặp (duplicate guard) và cơ chế dừng dứt khoát khi không có thông tin mới (no-new-information stop).

### 1.2 Ranh giới Bất biến (Non-Negotiable Boundaries)
1. **Không xây dựng Multi-Agent / ReAct Framework tự do:** Không sử dụng các vòng lặp hội thoại đa bước, không cho model tự gọi tool ngoài kiểm soát.
2. **Không crawl vô hạn (No Crawl Loop):** Bắt buộc phải có trần fetch (`max_fetches`), trần dung lượng (`max_bytes`), trần số lần thử lại (`max_retries`) và cơ chế dừng sớm khi phát hiện nội dung trùng lặp.
3. **Không tạo Ingestion Pipeline thứ hai:** Dữ liệu sau khi fetch bắt buộc phải quay lại qua luồng Ingestion chuẩn (`handle_document_upload`) để đi qua phân vùng ACL, tính checksum, tạo `DocumentVersion` và chạy worker chuẩn.
4. **Không sửa các migration cũ (0001–0004):** Mọi thay đổi schema lưu trữ research task phải nằm trong migration mới mang tính bổ sung (`0005_research_agent_tasks.sql`).
5. **Không để LLM quyết định logic nghiệp vụ:** LLM chỉ hỗ trợ diễn đạt câu hỏi từ gap (đã có ở Phase 9); Research Planner và Fetcher là các hàm thuần túy, tất định và kiểm soát bằng quy tắc (rule-based).

---

## 2. Rà soát Hiện trạng & Ponytail Audit (Over-Engineering Audit)

Dưới đây là bảng phân tích toàn diện mã nguồn hiện có theo các nhãn của `ponytail-audit`:

| Nhãn Ponytail | Thành phần / Vấn đề | Hiện trạng & Nguy cơ Over-engineering | Giải pháp Tinh gọn & Thay thế |
|---|---|---|---|
| `yagni` | Agent Runtime trong `sag_agent` | `sag_agent/runtime.py` chứa event stream, approval handle, stateful multi-turn. Việc dùng cho research task sẽ biến hệ thống thành black box, dễ rơi vào crawl loop và tốn token vô ích. | Xây dựng pipeline tuyến tính, tuần tự và tất định: `GapDetector` → `QuestionGenerator` → `ResearchPlanner` → `BoundedFetcher` → `IngestionRelay`. |
| `native` | Ingestion Pipeline | Nguy cơ tự viết hàm ingest, parser, hoặc chunker riêng cho Research Agent. | Tái sử dụng trực tiếp `handle_document_upload` trong `sag_api/services/document_service.py`. Tự động kế thừa `IngestionRun`, tính `payload_hash`, tạo `DocumentVersion` và job `PROCESS_DOCUMENT`. |
| `native` | Incremental Tree Update | Nguy cơ chạy full build lại Knowledge Tree khi có tài liệu mới từ research. | Sử dụng `incremental_tree_service.update_tree_incrementally` để gán delta units vào base tree một cách tiết kiệm và nhất quán. |
| `native` | Idempotency & Concurrency | Nguy cơ xây dựng cơ chế khóa phân tán phức tạp cho research task. | Sử dụng `UniqueConstraint` trên PostgreSQL giữa `(tenant_id, project_id, gap_id, checksum)` và `idempotency_key` của `handle_document_upload`. |
| `delete` | Câu hỏi nghiên cứu tự sinh | Nguy cơ để agent tự bịa câu hỏi và tự tìm kiếm tự do trên internet. | Tái sử dụng `generate_question_for_gap` trong `sag_api/services/knowledge_gap_question_service.py`: chỉ gap đã lưu, có anchored evidence và đạt ngưỡng priority mới được sinh câu hỏi. |
| `stdlib` | Kiểm tra Domain Allowlist | Nguy cơ cài đặt thư viện regex phức tạp hoặc matcher ngoài. | Sử dụng thư viện chuẩn `urllib.parse.urlsplit` để tách hostname và kiểm tra khớp exact domain hoặc wildcard suffix trong allowlist. |
| `stdlib` | Duplicate Guard & Hash | Nguy cơ tự chế thuật toán phát hiện trùng lặp văn bản. | Sử dụng `hashlib.sha256` để băm URL chuẩn hóa và nội dung payload đã tải về. |
| `shrink` | SSRF & An toàn Mạng | Nguy cơ viết lại logic kiểm tra IP và cổng kết nối. | Tái sử dụng `_validated_public_web_url` trong `sag_api/tools/builtin.py`: tự động chặn IP private/loopback, chỉ mở cổng 80/443, giới hạn số lần redirect <= 3. |
| `shrink` | HTML to Markdown | Nguy cơ cài đặt thư viện trích xuất web mới. | Tái sử dụng `extract_web_markdown` và `extract_web_title` trong `sag_api/connectors/web.py`. |

---

## 3. Kiến trúc Chi tiết Phase 10 & Checkpoint D

### 3.1 Sơ đồ Luồng Hoạt động (Workflow Diagram)

```text
  ┌─────────────────────────────────────────────────────────────┐
  │ 1. Gap Detector                                             │
  │    - Truy vấn KnowledgeGap thỏa mãn:                         │
  │      eligible == True & priority >= threshold                │
  │    - Kiểm tra chưa có active ResearchTask                    │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 2. Question Generator (Phase 9 Service)                     │
  │    - generate_question_for_gap(...)                         │
  │    - Tạo KnowledgeGapQuestion gắn chặt với evidence quote   │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 3. Research Planner (Pure Deterministic Function)           │
  │    - Ghép: Gap + Question + Policy                          │
  │    - Gán ngân sách cố định (Budget: max_fetches, max_bytes) │
  │    - Gán điều kiện dừng (Stop conditions)                   │
  │    - Sinh ResearchTask với checksum xác định                │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 4. Bounded Discovery & Fetcher                              │
  │    - Kiểm tra URL hợp lệ qua Allowlist                      │
  │    - SSRF Guard (chặn IP private/loopback)                  │
  │    - Manual redirect revalidation (tối đa 5 hops)          │
  │    - Tải trang có giới hạn streaming bytes                  │
  │    - Trích xuất Markdown & tính SHA-256 payload             │
  │    - Duplicate Guard scoped by security partition           │
  │    - Nếu payload hash đã tồn tại:                           │
  │      ==> DỪNG SỚM (STOPPED_NO_NEW_INFO)                     │
  └──────────────────────────────┬──────────────────────────────┘
                                 │ (Nếu có thông tin mới)
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 5. Normal Ingestion Pipeline                                │
  │    - Gọi handle_document_upload(...)                        │
  │    - Ghi nhận IngestionRun & DocumentVersion                │
  │    - Đẩy Job PROCESS_DOCUMENT vào hàng đợi chuẩn            │
  └──────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ 6. Incremental Tree Update                                  │
  │    - update_tree_incrementally(...)                         │
  │    - Cập nhật node profile mà không cần full rebuild        │
  │    - Hoàn tất vòng đời Task: status = COMPLETED             │
  └─────────────────────────────────────────────────────────────┘
```

### 3.2 Hợp đồng Dữ liệu & State Machine

#### A. Trạng thái Vòng đời Task (`ResearchTaskStatus`)
- `PENDING`: Task mới được khởi tạo từ Gap, chờ lập kế hoạch.
- `PLANNING`: Đang xây dựng danh sách nguồn và cấu hình ngân sách.
- `FETCHING`: Đang thực hiện thu thập dữ liệu trong phạm vi allowlist.
- `INGESTING`: Đang chuyển tiếp dữ liệu vào normal upload pipeline.
- `COMPLETED`: Đã thu thập và đưa vào ingestion pipeline thành công.
- `STOPPED_NO_NEW_INFO`: Thu thập dừng dứt khoát do gặp payload trùng lặp, không còn nguồn mới, hoặc trang rỗng.
- `FAILED`: Lỗi mạng/upstream vượt quá số lần retry tối đa (`max_retries`).
- `CANCELLED`: Bị hủy bởi người dùng hoặc hệ thống.

#### B. Ngân sách & Chính sách (Budget & Policy Contracts)
```python
@dataclass(frozen=True, slots=True)
class ResearchPolicy:
    allowed_domains: tuple[str, ...] = ("example.com", "docs.python.org", "wikipedia.org")
    allow_subdomains: bool = True
    require_https: bool = True


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    max_fetches: int = 3              # Tối đa số URL được fetch trong 1 task
    max_bytes_per_page: int = 2 * 1024 * 1024  # 2MB mỗi trang
    max_total_bytes: int = 6 * 1024 * 1024     # 6MB tổng cộng
    timeout_seconds: float = 15.0     # Timeout cho mỗi request
    max_retries: int = 2              # Số lần retry tối đa khi gặp lỗi mạng tạm thời
```

#### C. Điều kiện Dừng & Chống Vòng Lặp Crawl (Stop Conditions)
1. **Duplicate Guard:** Mỗi URL sau khi tải được tính SHA-256 payload. Nếu `payload_hash` trùng với bất kỳ tài liệu nào đã tồn tại trong cùng security partition, bỏ qua việc ingest và ghi nhận `DUPLICATE_PAYLOAD`.
2. **No-New-Information Termination:** Nếu toàn bộ các URL dự kiến đều trùng lặp hoặc không trích xuất được nội dung mới, task chuyển ngay sang `STOPPED_NO_NEW_INFO`, không thử tiếp URL tiếp theo.
3. **Budget Exhaustion:** Khi `fetch_count >= max_fetches` hoặc `total_bytes >= max_total_bytes`, quá trình fetch lập tức kết thúc.
4. **Retry Bounds & Backoff:** Mỗi URL chỉ được retry tối đa `max_retries` lần với exponential backoff. Nếu toàn bộ attempts lỗi mạng, task đánh dấu `FAILED` kèm `error_message`.
