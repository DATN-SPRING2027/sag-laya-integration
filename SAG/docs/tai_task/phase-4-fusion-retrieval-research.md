# Nghiên cứu Kỹ thuật: DATN-67 Phase 4 Fusion & Retrieval Regression

> **Nguồn chuẩn:** `SAG/tasks/plan.md` (Phase 4, Task P4.2/P4.4, checkpoints P4.2/P4 complete) và `SAG/tasks/todo.md` (Phase 4).  
> **Nguyên tắc Ponytail:** Tối giản, thực dụng, không over-engineering. Tận dụng tối đa logic và patterns sẵn có trong repo, không thêm dependency mới, không đụng vào các ranh giới đã ổn định (Ingestion/Index, Production ACL, Routing Laya).

---

## 1. Bối cảnh & Ranh giới Kỹ thuật (Context & Boundaries)

### 1.1 Vị trí trong Chu kỳ Phát triển
* **DATN-35 / Checkpoint B:** Đã hoàn tất (Routing, Laya query flow, strategy fallback).
* **DATN-67 (Phase 4 Fusion/Regression) & DATN-61 (Checkpoint A Production E2E):** Chạy song song độc lập.
  * DATN-67 tập trung vào tính đúng đắn của thuật toán Rank Fusion (RRF), Deduplication, Score normalization $[0, 1]$, và bộ kiểm thử hồi quy (Relevance, Scale invariance, Vietnamese queries, API trace).
  * DATN-67 **chỉ tiêu thụ** authorized-scope contract hiện có (`SearchACLScope.authorized_source_ids`, `principal`), **không** can thiệp vào production ACL issuer/JWKS hay staging cross-tenant checks.
* **DATN-62 (Phase 5–6 Knowledge Units & Graph):** Sẽ bắt đầu sau khi cả DATN-67 và DATN-61 hoàn tất.

### 1.2 Ranh giới Bất biến (Non-Negotiable Boundaries)
1. **Không sửa Ingestion/Index lane:** Không chạm vào Phase 1–2C pipeline (`SEARCH_READY`, parser, worker tasks, Qdrant index schema).
2. **Không làm lại DATN-35:** Giữ nguyên `ROUTING_READY`, Laya query planner, fallback trace và primary strategy.
3. **Không làm Production ACL infrastructure:** Không viết thêm JWT/JWKS issuer, không sửa Project→Source mapping backfill script, không mock staging multi-tenant server.
4. **Không làm biến tướng Evidence Context / Citation:** Không can thiệp vào MMR context budget packing, citation anchor mapping hay cơ chế no-answer đã hoàn thiện ở PR #13.

---

## 2. Rà soát Hiện trạng Codebase (Current State Audit)

### 2.1 Thành phần Fusion cốt lõi (`sag_api/services/retrieval_service.py`)

#### A. Công thức Reciprocal Rank Fusion (RRF)
* Mã nguồn hiện tại sử dụng hằng số RRF chuẩn:
  $$\text{RRF}(d) = \sum_{r \in \{\text{dense}, \text{sparse}\}} \frac{1}{k + \text{rank}_r(d) + 1} \quad \text{với } k = 60$$
* Chuẩn hóa điểm số đầu ra về thang $[0, 1]$:
  $$\text{ideal\_score} = \frac{\text{active\_retrievers}}{k + 1}$$
  $$\text{fused\_score} = \min\left(1.0, \frac{\text{reciprocal\_rank\_score}}{\text{ideal\_score}}\right)$$
* **Ưu điểm thiết kế:**
  * Miễn nhiễm với sự chênh lệch thang đo điểm số gốc (scale-invariant): dù điểm dense là cosine $[0.6, 0.95]$ hay dot-product $[-50, 200]$, và điểm sparse là BM25 $[0.5, 35.0]$, việc xếp hạng chỉ dựa trên thứ hạng (rank).
  * Ứng viên xuất hiện ở cả hai luồng (rank agreement) sẽ nhận điểm boost cao hơn hẳn ứng viên đơn luồng.

#### B. Khóa phân biệt & Deduplication (`_section_key`)
```python
def _section_key(section: RetrievedSection) -> tuple[str, str]:
    source = (section.source_config_id or section.source_id or "").strip()
    chunk = (section.chunk_id or "").strip()
    if chunk:
        return source, chunk
    fingerprint = normalize_lexical_text(f"{section.heading}\n{section.content}")[:240]
    return source, fingerprint
```
* Khi `chunk_id` có sẵn: Sử dụng `(source, chunk_id)` làm khóa duy nhất.
* Khi thiếu `chunk_id`: Fallback sang chuỗi băm/fingerprint 240 ký tự đầu chuẩn hóa của `heading + content`.
* Khi trùng khóa: Đại diện được chọn là bản ghi có nội dung dài hơn, sạch hơn và giữ lại điểm `score` cao nhất (`best_score = max(score1, score2)`).

#### C. Quy tắc Tie-breaking Xác định (Deterministic Ordering)
```python
ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4]))
# item: (fused_score, retriever_coverage, int(exact), rank_sum, key, section)
```
1. `-item[0]`: `fused_score` cao hơn xếp trước.
2. `-item[1]`: `retriever_coverage` (xuất hiện ở cả 2 channel dense + sparse xếp trước 1 channel).
3. `-item[2]`: Khớp từ khóa chính xác (`exact_match`) xếp trước expansion-only.
4. `item[3]`: Tổng thứ hạng (`rank_sum = dense_rank + sparse_rank`) nhỏ hơn xếp trước.
5. `item[4]`: Khóa duy nhất `(source, chunk_id)` đảm bảo thứ tự luôn cố định qua các lần chạy (deterministic).

#### D. Relevance Gate & Boilerplate Filtering
* Không để một hit lexical của tài liệu rác chặn đứng tài liệu semantic liên quan của tài liệu khác.
* Ngưỡng sàn ngữ nghĩa tương đối: $\text{relative\_semantic\_score} = \frac{\text{score}}{\text{top\_semantic\_score}} \ge 0.75$.
* Lọc bỏ văn bản rác bản quyền / điều hướng khi không có tín hiệu lexical mạnh.

---

### 2.2 Tích hợp luồng Canonical SearchUnit (`sag_api/services/search_unit_retrieval_service.py`)
* Truy xuất song song Dense hits và Sparse hits từ Qdrant theo từng group authorized.
* Ràng buộc số lượng candidate (`_bounded_candidate_unit_ids`) theo cấu hình `search_unit_candidate_limit`.
* Gọi `_fair_rank_merge` để trộn công bằng các scopes được cấp quyền.
* Gọi `rerank_sections(...)` để thực thi RRF fusion và sinh `rerank_stats` (`candidates`, `relevant`, `filtered_irrelevant`).
* Trả về `SearchOutcome` với `stats.fusion_method = "rrf"` (hoặc `"rank_interleave_fallback"` nếu cạn budget thời gian xử lý).

---

## 3. Phân tích Khoảng cách (Gap Analysis)

Mặc dù logic fusion cốt lõi đã có trong `retrieval_service.py` và `search_unit_retrieval_service.py`, qua rà soát test suite `tests/test_retrieval_relevance.py` phát hiện các khoảng trống kiểm thử sau:

1. **Khoảng trống Tiếng Việt (Vietnamese / Domain Query Gap):**
   * Hiện tại đa số test case mẫu sử dụng tiếng Trung (Sina, Lâm Tuấn Kiệt, Tây Du Ký) hoặc tiếng Anh.
   * Thiếu các bộ test case đặc thù tiếng Việt với dấu thanh, từ ghép, từ đồng nghĩa và ngữ cảnh nghiệp vụ (tài chính, ngân hàng, bảo hiểm, pháp lý).
2. **Kiểm thử Diễn đạt lại Ngữ nghĩa (Semantic Paraphrase):**
   * Cần kiểm chứng khi người dùng hỏi bằng các cách diễn đạt khác nhau (paraphrase) nhưng cùng bản chất ("thủ tục cấp tín dụng" vs "hồ sơ vay vốn ngân hàng"), dense retriever đưa đúng tài liệu lên đầu dù sparse score thấp.
3. **Mã định danh Chính xác (Exact Identifiers / Codes):**
   * Các truy vấn chứa mã hiệu văn bản, mã hợp đồng có ký tự đặc biệt ("QD-2026/TT-BTC", "HD-9988-KT"), lexical channel phải đưa đúng ứng viên lên đầu, vượt qua semantic noise.
4. **Kiểm chứng Đồng thuận Thứ hạng (Rank Agreement vs Divergence):**
   * Trường hợp cả 2 kênh đều đồng thuận xếp hạng cao ➔ Điểm số tăng vọt.
   * Trường hợp 1 kênh phát hiện, 1 kênh không tìm thấy ➔ Vẫn được giữ lại an toàn và chuẩn hóa điểm đúng $[0, 1]$.
5. **Độ ổn định Tie-break khi Bằng Điểm (Identical Scores Determinism):**
   * Cần kiểm tra kịch bản nhiều candidates có cùng `fused_score`, `retriever_coverage`, và `exact` để đảm bảo thứ tự trả về 100% nhất quán qua 100 lần lặp.
6. **Bất biến Thang đo Điểm số (Scale Invariance Deep Check):**
   * Đảm bảo việc nhân dense score với $10^5$ hoặc chia sparse score cho $10^3$ hoàn toàn không làm đổi thứ tự xếp hạng RRF.
7. **Hồi quy API Toàn cục (`/search`, `/search/stream`) & Trace Stats:**
   * Xác nhận endpoint trả về `fusion_method: "rrf"`, đầy đủ candidate counts và không làm rò rỉ dữ liệu ngoài authorized scope.

---

## 4. Định hướng Ponytail: Tối giản & Hiệu quả

1. **Không viết lại RRF:** Hàm `rerank_sections` trong `retrieval_service.py` đã giải quyết bài toán RRF rất chuẩn và ổn định. Không cần viết thêm class trừu tượng hay framework fusion mới.
2. **Tập trung vào Regression Suite hoàn chỉnh:** Tạo một test file chuyên trách `tests/test_phase_4_fusion_retrieval.py` bao phủ toàn bộ 7 khoảng trống trên, độc lập, chạy nhanh (< 5 giây), không phụ thuộc DB ngoài.
3. **Bổ sung Assertions vào API regression:** Đảm bảo test `/search` và `/search/stream` kiểm tra đúng `stats["fusion_method"] == "rrf"` và các chỉ số thống kê candidate counts.
4. **Cập nhật tài liệu minh bạch:** Ghi chép đầy đủ kết quả chạy kiểm thử vào `SAG/docs/tai_task/phase-4-fusion-retrieval-evidence.md` và tick hoàn thành Phase 4 trong `SAG/tasks/todo.md`.
