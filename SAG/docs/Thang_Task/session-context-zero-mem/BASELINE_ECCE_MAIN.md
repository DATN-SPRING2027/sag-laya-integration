# Báo Cáo Baseline & Chuỗi Thực Nghiệm Ablation (`V0 → V5`) Trên `SAG main` (`3411d16`)

- **Ngày thực hiện:** 2026-10-28
- **Nhánh:** `feat/Thang-ecce-baseline-harness-be-api` (tách trực tiếp từ `origin/main` @ `3411d16`)
- **Bộ chạy kiểm chứng:** `SAG/apps/api/tests/eval_ecce_baseline/benchmark_suite.py` & `SAG/apps/api/tests/test_ecce_ablation_benchmark.py`
- **Quy mô tập hội thoại thử nghiệm:** `239` lượt trao đổi kỹ thuật tiếng Việt trải qua `27` phân đoạn chủ đề (`Episode`) có khoảng cách thời gian thực tế, vượt trần `history_load_limit = 200` của cơ chế cũ.

---

## 1. Bảng Kết Quả Định Lượng Chuỗi Ablation (`V0 → V5`)

| Phiên bản | Cơ chế chính | Mean Recall | Số ca đạt 100% Gold | Tỷ lệ nhiễm quyết định cũ (`Stale Contamination`) | Tokens lịch sử TB / lượt | Mật độ bằng chứng (`Gold / 1k tokens`) | Số lần gọi LLM nén bộ nhớ | Độ trễ P50 / P95 (ms) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **`V0_legacy`** | Cơ chế hiện hữu trên `main` (`LIMIT 200` + cắt cứng `40%` / gọi LLM nén mỗi lượt) | `0.0000` | `0/12` | `0.0000` *(mù toàn bộ đoạn cũ)* | `556.8` | `0.00` | `12` *(1 call/lượt)* | `0.11 / 0.16` *(chưa gồm ~1.5–3.5s chờ LLM nén)* |
| **`V1_hybrid`** | Keyset toàn phiên + Dense Vector + True BM25 Unicode tiếng Việt (`search_index_service`) | `0.8750` | `10/12` | `1.0000` (`3/3`) | `516.8` | `2.74` | `0` | `3.87 / 4.05` |
| **`V2_graph_ppr`** | `+` `Zero-Mem` Sentence-Gated `Entity ↔ Sentence` + `igraph` PPR (`damping = 0.6`, `prpack`) | `0.9583` | `11/12` | `1.0000` (`3/3`) | `513.0` | `2.92` | `0` | `4.73 / 5.42` |
| **`V3_dual_hierarchy`** | `+` Phân cấp 4 tầng `Thread → Drift Episode → 3-Turn Window → Turn` & hợp nhất động | `0.9583` | `11/12` | `1.0000` (`3/3`) | `525.9` | `2.85` | `0` | `7.93 / 8.57` |
| **`V4_closure_temporal`** | `+` **ECCE** Bi-Temporal Decision Ledger (`SUPERSEDES`) & Đóng bao bằng chứng (`±1` kề + Lineage) | `1.0000` | `12/12` | `0.3333` (`1/3`) | `596.4` | `2.79` | `0` | `8.92 / 11.20` |
| **`V5_ecce_full`** | `+` **ECCE** Submodular Lazy-Greedy Context Budget Optimizer + Self-Auditing Trace | **`1.0000`** | **`12/12`** | **`0.0000` (`0/3`)** | **`390.2`** | **`4.27`** | **`0`** | **`9.07 / 10.96`** |

---

## 2. Phân Tích Ý Nghĩa Của Từng Bước Cải Tiến

1. **`V0_legacy → V1_hybrid` (Phá bỏ trần mù `LIMIT 200` & sửa lỗi ASCII tiếng Việt của `Zero-Mem`):**
   - `V0_legacy` tốn `1` lệnh gọi `llm.complete` ở mỗi lượt hỏi nhưng vẫn bỏ sót toàn bộ thông tin đầu/giữa phiên (`0/12`) và cắt mất các chi tiết số liệu cụ thể (`1450 USD`, `15/11/2026`).
   - Nhờ tái sử dụng `compute_sparse_bm25_vector` có `COMMON_STOPWORDS` tiếng Việt của `SAG main` thay cho regex ASCII `[a-zA-Z]` trong code gốc `Zero-Mem`, `V1_hybrid` đạt ngay `10/12` với `0` LLM memory calls.
2. **`V1_hybrid → V2_graph_ppr` (Bắc cầu thực thể 2 bước nhảy bằng `igraph` sẵn có trên `main`):**
   - Giải quyết ca `C09_multihop_entity_bridge` (*`payment-gateway-v2` → `Vault-HSM-Prod` → chu kỳ xoay vòng `72 giờ`*), nơi lượt chứa con số `72 giờ` không hề nhắc lại tên dịch vụ `payment-gateway-v2`.
3. **`V2/V3 → V4_closure_temporal` (Khắc phục điểm mù lớn nhất của `Zero-Mem` gốc về trôi quyết định & đại từ tiếng Việt):**
   - Các biến thể thuần `Zero-Mem` (`V2`, `V3`) vẫn lấy nhầm các phương án đã bị hủy bỏ (`MongoDB`, `CockroachDB`, `Redis Streams` — `Stale Contamination = 100%`) và bỏ sót lượt đề xuất gốc khi người dùng chốt ngắn gọn (*"Đồng ý luôn, chốt phương án 2 như anh Tuấn vừa đề xuất nhé!"* ở ca `C05`).
   - `V4` bổ sung sổ cái `DecisionLedgerEntry` (`PROPOSED / CONSIDERING / CONFIRMED / SUPERSEDED`) và đóng bao lân cận trong cùng `Episode`, đưa Recall lên `12/12` (`1.0000`), kể cả ca truy vết lý do chuyển đổi `A → B → A` (`C04_decision_lineage_why`).
4. **`V4_closure_temporal → V5_ecce_full` (Tối ưu hóa ngân sách Submodular & khử trùng lặp/đồng âm):**
   - Triệt tiêu hoàn toàn nhiễu đồng âm vai trò (`anh Minh BE` vs `anh Minh FE` ở ca `C06`, đưa `Stale Contamination` về **`0.0000`**) và loại bỏ 5 lượt nhắc lại trùng lặp ở ca `C11_redundant_spam_submodular`.
   - Giảm **`34,6%`** lượng token lịch sử so với `V4` (`596.4 → 390.2` tokens), thấp hơn **`29,9%`** so với cửa sổ cắt cụt của `V0_legacy` (`556.8` tokens) và tiết kiệm **`> 93%`** so với toàn bộ phiên thô (`~6.100` tokens), nâng mật độ bằng chứng lên **`4.27` mảnh chuẩn / 1.000 tokens**.

---

## 3. Giới Hạn Hiện Tại & Điều Kiện Chuyển Sang Chặng 2

- Số liệu trên đo khả năng truy xuất, đóng bao bằng chứng, chống nhiễm quyết định cũ và ngân sách token ở tầng **Context Engine** với bộ nhúng tất định (offline reproducible); khi kết nối trực tiếp embedder đa ngữ qua SiliconFlow (`BAAI/bge-m3`) và chạy live replay với LLM sinh câu trả lời cuối, cần ghi nhận thêm chi phí HTTP embedding `1` batch/lượt mới và chất lượng câu trả lời tự nhiên trên tập holdout.
- **Bước kế tiếp theo lộ trình:** Sau khi PR Chặng 1 (`be-api`) được review, tách nhánh Chặng 2 (`feat/Thang-ecce-session-schema-db`) cho migration `0009_session_context_ecce.sql` để lưu bền vững `session_units` và `session_decision_ledger` xuống PostgreSQL + Qdrant `session_units_{project_id}`.
