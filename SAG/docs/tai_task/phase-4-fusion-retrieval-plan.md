# Kế hoạch Thực thi: DATN-67 Phase 4 Fusion & Retrieval Regression

> **Nguồn chuẩn:** `SAG/tasks/plan.md` (Phase 4, Task P4.2 & P4.4, Checkpoints P4.2 / P4 Complete) và `SAG/tasks/todo.md` (Phase 4).  
> **Nhánh làm việc:** `feat/Tai-DATN-67-phase-4-fusion-retrieval`  
> **Nguyên tắc Ponytail:** Tối giản tối đa diff code, chỉ giải quyết đúng yêu cầu bài toán, không viết code thừa, tập trung vào tính bao phủ và sự vững chắc của test hồi quy.

---

## 1. Mục tiêu & Phạm vi Thực thi

### 1.1 Mục tiêu
* Hoàn thành trọn vẹn **Task P4.2** (Rank fusion ổn định) và **Task P4.4** (Regression và review gate).
* Đóng các mục còn mở của Phase 4 trong `SAG/tasks/todo.md` và `SAG/tasks/plan.md`.
* Chứng minh thuật toán RRF fusion, deduplication, tie-breaking và score normalization hoạt động ổn định trên cả các tập dữ liệu đa ngôn ngữ (tiếng Việt, tiếng Anh, mã hiệu văn bản).

### 1.2 Tiêu chí Chấp thuận (Acceptance Criteria)
1. **Fusion Ổn định & Scale Invariance:** Dense và Lexical được fuse bằng RRF với hằng số $k=60$; raw score scale (cosine, dot-product, BM25) không làm một bên lấn át bên kia.
2. **Deduplication & Tie-break Xác định:** Khóa dedupe theo `(source, chunk_id)` hoặc fingerprint ổn định khi thiếu `chunk_id`. Tie-break hoàn toàn xác định (deterministic) dựa trên `(fused_score, retriever_coverage, exact_match, rank_sum, unique_key)`.
3. **Score & Stats Hợp đồng:** Điểm số đầu ra luôn nằm nghiêm ngặt trong đoạn $[0.0, 1.0]$. Retrieval stats ghi nhận chính xác `fusion_method: "rrf"`, candidate counts (`candidates`, `relevant`, `filtered_irrelevant`, `lexical_candidates`).
4. **Bộ Test Hồi quy Toàn diện:** Bao phủ:
   * Semantic paraphrase (truy vấn đồng nghĩa, diễn đạt lại).
   * Exact identifiers (mã văn bản, mã hợp đồng có ký tự số/gạch nối).
   * Vietnamese / domain queries (tiếng Việt có dấu, không dấu, ngữ cảnh tài chính/ngân hàng/hành chính).
   * Duplicate candidates (loại bỏ trùng lặp, giữ bản ghi có nội dung dài hơn và điểm cao nhất).
   * Rank agreement vs Rank divergence (đồng thuận xếp hạng vs lệch luồng).
   * Scale invariance (thay đổi thang đo điểm số không làm đảo lộn thứ tự RRF).
   * Deterministic ordering (100 lần lặp trả về cùng một danh sách).
5. **API Global Retrieval Không Regression:** `/search` và `/search/stream` chạy mượt mà, ghi nhận đầy đủ P3 strategy trace và stats hợp đồng.
6. **Bằng chứng & Todo Update:** Sinh file bằng chứng `SAG/docs/tai_task/phase-4-fusion-retrieval-evidence.md` và cập nhật các mục Checkpoint P4 trong `SAG/tasks/todo.md`.

---

## 2. Các Gói Công việc Chi tiết (Work Packets)

### Work Packet 1 (WP1): Xây dựng Bộ Kiểm thử Hồi quy Chuyên sâu (`test_phase_4_fusion_retrieval.py`)
Tạo mới file test `SAG/apps/api/tests/test_phase_4_fusion_retrieval.py` kiểm chứng các khía cạnh:
* **TC1 - Scale Invariance Tuyệt đối:**
  * Dense scores trong khoảng $[0.0001, 0.0009]$, Sparse scores trong khoảng $[150.0, 450.0]$.
  * So sánh với baseline điểm chuẩn $[0.7, 0.95]$ và $[2.0, 8.0]$.
  * Khẳng định thứ tự RRF đầu ra hoàn toàn đồng nhất 100%.
* **TC2 - Đồng thuận Thứ hạng (Rank Agreement Boost):**
  * Ứng viên xuất hiện ở Top 1 của cả Dense và Sparse đạt điểm fused gần tiệm cận 1.0 ($1.0$).
  * Ứng viên chỉ xuất hiện ở một kênh có điểm số thấp hơn rõ rệt (chuẩn hóa về khoảng $0.5$).
* **TC3 - Deduplication với Fingerprint Ổn định:**
  * Ứng viên thiếu `chunk_id` tự động sinh fingerprint dựa trên `normalize_lexical_text(heading + content)[:240]`.
  * Trùng lặp nội dung cùng source được gom nhóm thành 1, giữ điểm cao nhất và nội dung đầy đủ nhất.
* **TC4 - Tie-breaking Xác định Tuyệt đối:**
  * Tạo 5 ứng viên có cùng `fused_score`, cùng `coverage`, cùng `exact`, cùng `rank_sum`.
  * Chạy shuffle danh sách đầu vào và thực hiện rerank 50 lần.
  * Khẳng định thứ tự đầu ra không bao giờ bị xáo trộn nhờ khóa `key=(source, chunk_id)`.
* **TC5 - Ngữ cảnh Nghiệp vụ Tiếng Việt (Vietnamese Domain Queries):**
  * Truy vấn: `"quy trình giải ngân hồ sơ vay vốn doanh nghiệp"`.
  * Phân biệt giữa văn bản quy trình chính xác với văn bản rác giới thiệu chung hoặc điều khoản chung.
* **TC6 - Diễn đạt lại Ngữ nghĩa (Semantic Paraphrase):**
  * Truy vấn gốc: `"hướng dẫn chi trả chế độ thai sản"`.
  * Truy vấn diễn đạt lại: `"thủ tục thanh toán quyền lợi sinh con"`.
  * Kết quả truy xuất của Dense retriever đưa đúng tài liệu liên quan lên đầu dù sparse score thấp.
* **TC7 - Mã Hiệu Văn bản Chính xác (Exact Alphanumeric Identifiers):**
  * Truy vấn: `"QD-2026/TT-BTC"`, `"HD-9988-KT/2026"`.
  * Kênh Lexical đưa chính xác tài liệu chứa mã hiệu lên đầu bảng, vượt qua các tài liệu chung chung có điểm ngữ nghĩa tương tự.

### Work Packet 2 (WP2): Rà soát Hồi quy API Toàn cục (`/search` & `/search/stream`)
* Kiểm tra `SAG/apps/api/tests/test_search_strategy.py` và `test_search_stream.py`.
* Bổ sung / xác nhận assertion cho:
  * `stats["fusion_method"] == "rrf"`
  * `stats["semantic_candidates"] >= 0`
  * `stats["lexical_candidates"] >= 0`
  * Toàn bộ điểm số `section.score` đều thỏa mãn $0.0 \le \text{score} \le 1.0$.

### Work Packet 3 (WP3): Chạy Toàn bộ Focused Test Suite & Đảm bảo Không Regression
* Chạy kiểm thử:
  * `tests/test_phase_4_fusion_retrieval.py` (file test mới tạo)
  * `tests/test_retrieval_relevance.py` (35 tests hiện tại)
  * `tests/test_search_unit_retrieval_service.py` (20 tests)
  * `tests/test_search_strategy.py` (25 tests)
  * `tests/test_search_stream.py` (10 tests)
* Kiểm tra linter bằng `ruff check`.

### Work Packet 4 (WP4): Lập Báo cáo Bằng chứng & Cập nhật Tiến độ Dự án
* Sinh tài liệu `SAG/docs/tai_task/phase-4-fusion-retrieval-evidence.md` ghi nhận toàn bộ log thực thi của pytest, thông số kiểm thử, bảng tổng kết kết quả.
* Cập nhật file `SAG/tasks/todo.md` và `SAG/tasks/plan.md`:
  * Đánh dấu hoàn thành Task P4.2 và Task P4.4.
  * Ghi chú rõ các tiêu chí đã pass.

---

## 3. Lịch trình Thực hiện (Execution Schedule)

| Bước | Nội dung | File Tác động | Kết quả Dự kiến |
|:---:|---|---|---|
| **1** | Tạo file test `test_phase_4_fusion_retrieval.py` | `SAG/apps/api/tests/test_phase_4_fusion_retrieval.py` | Bao phủ 7 test cases hồi quy cốt lõi |
| **2** | Chạy kiểm thử & tinh chỉnh (nếu cần) | `pytest tests/test_phase_4_fusion_retrieval.py` | 100% PASSED |
| **3** | Chạy kiểm thử chéo toàn bộ Phase 4 suite | `pytest` trên 5 test files liên quan | Tất cả ~95 tests đều PASSED |
| **4** | Lập báo cáo Evidence | `SAG/docs/tai_task/phase-4-fusion-retrieval-evidence.md` | Bằng chứng kiểm thử chi tiết |
| **5** | Cập nhật Todo & Plan | `SAG/tasks/todo.md`, `SAG/tasks/plan.md` | Cập nhật trạng thái hoàn thành P4 |

---

## 4. Định nghĩa Hoàn thành (Definition of Done)

* [ ] Nhánh git `feat/Tai-DATN-67-phase-4-fusion-retrieval` được sử dụng xuyên suốt.
* [ ] File `tests/test_phase_4_fusion_retrieval.py` được tạo mới, độc lập và pass 100%.
* [ ] Toàn bộ các kiểm thử hiện có của Phase 4 (`test_retrieval_relevance.py`, `test_search_unit_retrieval_service.py`, `test_search_strategy.py`, `test_search_stream.py`) tiếp tục pass 100%.
* [ ] Ruff check pass không có cảnh báo hay lỗi linter trên code mới.
* [ ] Tài liệu `phase-4-fusion-retrieval-research.md`, `phase-4-fusion-retrieval-plan.md` và `phase-4-fusion-retrieval-evidence.md` có mặt đầy đủ trong `SAG/docs/tai_task`.
* [ ] `SAG/tasks/todo.md` được cập nhật chính xác các mục Phase 4.
