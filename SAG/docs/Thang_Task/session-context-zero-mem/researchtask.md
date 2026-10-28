# SAG Task Research Record — Session Context Engine (`Zero-Mem` + `ECCE`, Main-Native & SiliconFlow-Direct)

- **Ngày lập:** 2026-10-28
- **Chủ sở hữu:** Thang
- **Nhánh thực hiện (Chặng 1):** `feat/Thang-ecce-baseline-harness-be-api`
- **Commit gốc (`origin/main`):** `3411d16` (`Merge pull request #41 from DATN-SPRING2027/feat/sag-e2e-top5-ingestion`)
- **Phạm vi PR hiện tại:** Backend API & Evaluation Baseline Harness (`be-api`, **0 thay đổi schema/migration PostgreSQL**; deliverable DB `0009_*.sql` được tách sang nhánh/PR riêng ở Chặng 2 theo `AGENTS.md`).

---

## 1. Bối Cảnh & Các Quyết Định Kiến Trúc Đã Chốt

1. **Nền tảng mã nguồn (`main`-first, 0 lệch code):**
   - Phát triển trực tiếp trên `origin/main` (`3411d16`). Không cherry-pick hoặc rebase từ nhánh thử nghiệm `PR #40` cũ (vốn tách từ `7772270`, xung đột migration `0007`/`0008` và kéo theo model local nặng).
2. **Chiến lược Model & Định tuyến (`SiliconFlow`-direct, 0 model local mới):**
   - Không khôi phục model local trong `laya/` và không thêm `spacy`, `torch`, `transformers` hay checkpoint NER local vào container API.
   - Tái sử dụng trực tiếp `sag_api/services/decision_router.py` gọi `POST {siliconflow_base_url}/systemone` (`Kev-4b`) cho định tuyến ý định / phạm vi bộ nhớ / chế độ thời gian, kèm cơ chế fail-open tất định khi `SAG_DECISION_PROVIDER=disabled` hoặc lỗi mạng.
   - Tái sử dụng trực tiếp hạ tầng HTTP Embedder (`compute_unit_embeddings` + `_SHARED_TEXT_EMBEDDING_CACHE` trong `search_index_service.py`), tương thích trực tiếp với SiliconFlow `/v1/embeddings` (ví dụ `BAAI/bge-m3`) hoặc embedder cấu hình sẵn trên `main`.
3. **Yêu cầu sản phẩm & bảo mật đã chốt (`phong-van/2026-10-26` & `2026-10-27`):**
   - Giữ nguyên pipeline retrieval kho tri thức tài liệu của `SAG` (`SearchContextTool`, `search_units_{project_id}`, Leiden Tree, HippoRAG 2).
   - Toàn bộ tin nhắn gốc của phiên chat (cả chat cũ và chat mới) nằm trong phạm vi có thể truy xuất; không bị mù bởi trần `8` hay `200` tin nhắn.
   - Lượt hỏi thông thường tốn **0 LLM calls** cho thao tác bộ nhớ; lượt yêu cầu tổng hợp toàn phiên (`full_session_audit`) được phép tổng hợp theo lô có giới hạn trần ngân sách/số lô rõ ràng và báo trung thực nếu chạm trần (`partial_coverage`).
   - Giữ cờ tính năng `SAG_SESSION_CONTEXT_MODE="off"` làm mặc định ban đầu (`off | shadow | on`), mặc định cô lập chặt theo từng `Thread` (`SAG_SESSION_CONTEXT_SCOPE="thread"`) trong phạm vi `Project` đã được Continuum BE xác thực quyền.

---

## 2. Truy Vết Luồng Hiện Tại Trên `main` (`3411d16`)

### 2.1. Luồng hội thoại & nén lịch sử hiện hữu
1. `sag_api/api/v1/agents.py:ask` (dòng `353–399`):
   - Xác thực `owner_key = Depends(get_conversation_owner)` và `principal = Depends(require_project_read_principal)`.
   - Gọi `agent_domain.prepare_ask(...)` rồi stream qua `agent_service.generate_stream(...)`.
2. `sag_api/services/agent_domain.py:prepare_ask` (dòng `628–689`):
   - Lưu `Message(role=USER)` mới vào PostgreSQL (`messages` đã có sẵn composite index `ix_messages_thread_created_id (thread_id, created_at, id)` trong `sag_api/db/models/agent.py:49`).
   - Gọi `_history(...)` (dòng `509–534`): chỉ đọc `LIMIT settings.history_load_limit` (`200`) tin nhắn mới nhất, lọc quyền nguồn dữ liệu qua `filter_messages_for_scope(...)` (dòng `462–506`).
   - Gọi `compress_history(..., budget_tokens=int(settings.llm_context_window * 0.4))` (dòng `540–586`, `680`): khi vượt 40% cửa sổ, cắt cứng `older` ở `[:12000]` ký tự và **gọi đồng bộ `await llm.complete(...)` ở mỗi lượt hỏi** (vì bản tóm tắt không được lưu cache), làm mất số liệu/quyết định chi tiết và tăng độ trễ + chi phí token.
3. `sag_api/services/agent_service.py`:
   - `_initial_tool_choice` (dòng `140–189`): gọi `asyncio.to_thread(route_query, normalized)` qua `decision_router.py`.
   - `_fit_agent_context` (dòng `288–360`): cắt ghép cửa sổ runtime giữ `system` + `latest_user` + cặp `tool_call/tool`.
   - `_finalize_answer_citations` (dòng `211–267`): xác thực trích dẫn tài liệu `[n]`.

### 2.2. Các tài sản kỹ thuật sẵn có trên `main` được tái sử dụng trực tiếp (Ponytail Step 2 & 5)
- `sag_api/services/decision_router.py`: `route_query(query, context)` gọi SiliconFlow `/systemone` (`Kev-4b`, timeout `5.0s`, connect `3.0s`), trả cấu trúc trắc nghiệm có `confidence` và `fallback_used`.
- `sag_api/services/knowledge_service.py`: bộ lexer tiếng Việt Unicode `_UPPER_CHAR`, `_LOWER_CHAR`, `_ENTITY_RE`, `_VI_COMPOUND_TAIL_RE`, `_SENTENCE_SPLIT_RE`, `terms`, `extract_e0_e1` (0 LLM token, không tách nhầm số thập phân).
- `sag_api/services/search_index_service.py`: `COMMON_STOPWORDS` (Anh + Việt), `compute_sparse_bm25_vector` (Robertson–Spärck Jones IDF), `_SHARED_TEXT_EMBEDDING_CACHE` (4.096 mục), `compute_unit_embeddings`.
- `sag_api/services/dedup_and_temporal_service.py`: mô hình hiệu lực lưỡng thời gian `[valid_from, valid_to)` và ngữ nghĩa thay thế `SUPERSEDES` / `CONTRADICTS`.
- `pyproject.toml`: đã cài sẵn `igraph>=0.11,<1` (hỗ trợ `Graph.personalized_pagerank(..., damping=0.6, implementation="prpack")` tốc độ C).

---

## 3. Ranh Giới Bảo Mật, ACL & Chống Nhiễm Độc Bộ Nhớ

1. **Continuum Fail-Closed Source Revocation (Thu hồi quyền bắc cầu):**
   - Mọi lượt `Message` đọc từ PostgreSQL bắt buộc đi qua `filter_messages_for_scope(session, messages, principal=principal, requested_source_ids=source_ids)` trước khi lập chỉ mục hoặc truy xuất bằng chứng.
   - Tin nhắn `ASSISTANT` từng trích dẫn nguồn tài liệu đã bị thu hồi quyền (nội dung bị thay bằng placeholder ẩn quyền hoặc không thuộc `authorized`) **tuyệt đối không được đưa vào tập bằng chứng truy xuất**; bộ nhớ cache theo phiên gắn chữ ký phạm vi nguồn được phép (`scope_signature`) để lập tức loại bỏ dữ liệu cũ khi quyền thay đổi.
2. **Cô lập Tenant / Owner / Thread:**
   - Mặc định `session_context_scope = "thread"`: chỉ truy xuất tin nhắn thuộc đúng `(owner_key, thread_id)` đã qua `get_thread(session, agent.id, thread.id, owner_key=owner_key)`.
   - Khi `delete_message` được gọi (`agent_domain.py:692`), chỉ mục bộ nhớ trong tiến trình của `thread_id` đó được xóa ngay lập tức để tin nhắn đã xóa không bao giờ xuất hiện lại.
3. **Chống Memory Poisoning / Prompt Injection xuyên lượt:**
   - Bằng chứng lịch sử được truy xuất không bao giờ chèn lại làm `role: "system"` hay giả mạo `role: "assistant"` cho đoạn xa; chúng được đóng gói vào một khối JSON dữ liệu trơ (`untrusted_historical_session_evidence`) có hướng dẫn rõ chỉ dùng làm dữ liệu tham chiếu sự kiện/quyết định kèm mã nguồn `[S1], [S2]`.

---

## 4. Đặc Tả Toán Học & Chuỗi Ablation `V0 → V5`

- **`V0_legacy`:** Cơ chế hiện hữu trên `main` (`LIMIT 200` + giữ `8` tin gần nhất + cắt ngân sách cứng `40%` context window).
- **`V1_hybrid`:** Toàn bộ lịch sử phiên (keyset pagination không trần 200) + kết hợp chuẩn hóa điểm Dense Vector Cosine và True BM25 Unicode tiếng Việt (`compute_sparse_bm25_vector`).
- **`V2_graph_ppr` (`Zero-Mem` Graph View cải tiến tiếng Việt):**
  - Trích xuất thực thể qua `extract_e0_e1` + danh xưng hội thoại tiếng Việt (`anh/chị/bạn/team + Tên` nối cạnh mềm `ALIAS_CANDIDATE` `weight=0.65`, không gộp cứng hai định danh khác vai trò như `Minh FE` vs `Minh BE`).
  - Kích hoạt thực thể có cổng ngữ nghĩa câu (`Sentence-Gated Sparse Propagation`): từ thực thể hạt giống, chỉ truyền qua tối đa `top_k_sentence = 3` câu có $\cos(\mathbf{e}_s, \mathbf{e}_q)$ cao nhất, sau đó chạy `igraph` Personalized PageRank (`damping = 0.6`, `prpack`) trên đồ thị `Entity ↔ Turn` trọng số tần suất tương đối $w(p, e) = c(p, e) / \sum_{e'} c(p, e')$.
- **`V3_dual_hierarchy` (`Zero-Mem` Dual-View):**
  - Phân cấp 4 tầng `Thread → Drift Episode → 3-Turn Window → Turn`, tự động ngắt `Episode` mới khi khoảng cách thời gian `> 30 phút` hoặc $\cos(\mathbf{c}_{\text{cur}}, \mathbf{e}_w) < 0.72 \land \text{Jaccard}_{\text{vi}} < 0.18$.
  - Hợp nhất điểm động $(\rho_{\text{graph}}, \rho_{\text{hier}})$ có tách biệt *ràng buộc thời gian* khỏi *đích hỏi thời gian* (`_requires_temporal_route`) và tự nâng chế độ khi `complementary_coverage` cao.
- **`V4_closure_temporal` (Nâng cấp ECCE — Trạng thái quyết định & Đóng bao bằng chứng):**
  - Nhận diện trạng thái nhận thức (`FACT`, `PROPOSED`, `CONSIDERING`, `CONFIRMED`, `SUPERSEDED`, `NEGATED`, `OPEN_ISSUE`) và khóa chủ đề `topic_slot_key`; tự động gắn `superseded_by_turn_id` khi quyết định mới thay thế quyết định cũ mà vẫn giữ nguyên tin nhắn gốc.
  - Đóng bao bằng chứng có trần (`±1` lượt kề trong cùng `Episode` cho câu trả lời ngắn/đại từ + tối đa `2` cầu nối thực thể + bắt buộc kéo quyết định `CONFIRMED` thay thế nếu chạm mảnh `SUPERSEDED`).
- **`V5_ecce_full` (Nâng cấp ECCE — Submodular Context Budget Optimizer & Self-Auditing Trace):**
  - Tối ưu hóa hàm mục tiêu submodular theo Lazy Greedy dưới ngân sách token động:
    $$\Delta(g \mid C) = \frac{U(g, q) - \lambda \cdot \text{Redundancy}(g, C) + \mu \cdot \Delta\text{Coverage}(g \mid C, q)}{\text{Tokens}(g)^\alpha}$$
  - Sinh `ContextAuditTrace` giải thích tường minh từng cụm được chọn và từng ứng viên bị loại (`SUPERSEDED_BY_NEWER_DECISION`, `REDUNDANT_SEMANTIC_DUPLICATE`, `TOKEN_BUDGET_CAP`, `ACL_REVOKED`).

---

## 5. Danh Sách Thay Đổi Theo File (Chặng 1)

| File | Hành động | Mô tả |
|---|---|---|
| `SAG/docs/Thang_Task/session-context-zero-mem/researchtask.md` | Tạo mới | Hồ sơ nghiên cứu & thiết kế kỹ thuật theo Rule 05. |
| `SAG/apps/api/sag_api/core/config.py` | Cập nhật nhỏ | Thêm cấu hình `session_context_mode` (`"off"` mặc định), `session_context_scope` (`"thread"`), `session_context_max_messages`, `session_context_full_audit_max_batches`. |
| `SAG/.env.example` | Cập nhật nhỏ | Đồng bộ chú thích biến môi trường `SAG_SESSION_CONTEXT_MODE=off`. |
| `SAG/apps/api/sag_api/services/decision_router.py` | Cập nhật tương thích ngược | Mở rộng `_DECISION_QUESTIONS` trên SiliconFlow `/systemone` (`Kev-4b`) thêm `memory_scope` & `temporal_mode`, kèm suy diễn tất định fail-open. |
| `SAG/apps/api/sag_api/api/v1/decision.py` | Cập nhật tương thích ngược | Bổ sung trường mặc định `memory_scope`, `temporal_mode` vào `DecisionRouteResponse`. |
| `SAG/apps/api/sag_api/services/session_context/__init__.py` | Tạo mới | Public API của gói `session_context`. |
| `SAG/apps/api/sag_api/services/session_context/models.py` | Tạo mới | Dataclasses cho Turn, Episode, DecisionState, EvidenceGroup, ContextAuditTrace. |
| `SAG/apps/api/sag_api/services/session_context/extractor.py` | Tạo mới | Tái sử dụng `extract_e0_e1` + danh xưng tiếng Việt + nhận diện trạng thái quyết định (`PROPOSED/CONFIRMED/SUPERSEDED`). |
| `SAG/apps/api/sag_api/services/session_context/substrate.py` | Tạo mới | Incremental Multi-View Indexer (`O(Δ)` append, Dense + True BM25 Unicode, 4-Level Drift Episode, Sentence-Gated `igraph` PPR). |
| `SAG/apps/api/sag_api/services/session_context/optimizer.py` | Tạo mới | Bounded Evidence Closure, Temporal Conflict Resolver, Submodular Lazy-Greedy Optimizer, Inert JSON Packing & Audit Trace. |
| `SAG/apps/api/sag_api/services/session_context/runtime.py` | Tạo mới | Keyset PostgreSQL reader + Continuum ACL integration + `off/shadow/on` orchestrator cho `agent_domain.py`. |
| `SAG/apps/api/sag_api/services/agent_domain.py` | Cập nhật nhỏ | Nối hook `session_context_mode` trong `prepare_ask` (mặc định `off` giữ nguyên 100% đường cũ) và vô hiệu hóa cache trong `delete_message`. |
| `SAG/apps/api/tests/test_ecce_session_context.py` | Tạo mới | Unit & integration tests cho SiliconFlow router, tiếng Việt, incremental index, ACL fail-closed, chống prompt injection và `off/shadow/on`. |
| `SAG/apps/api/tests/eval_ecce_baseline/benchmark_suite.py` | Tạo mới | Bộ 12 kịch bản chuẩn tiếng Việt & đa bước + runner đo chuỗi Ablation `V0 → V5`. |
| `SAG/apps/api/tests/test_ecce_ablation_benchmark.py` | Tạo mới | Kiểm thử tự động kiểm chứng chất lượng/tiết kiệm token của `V0 → V5`. |
| `SAG/docs/Thang_Task/session-context-zero-mem/BASELINE_ECCE_MAIN.md` | Tạo mới | Báo cáo số liệu thực nghiệm `V0 → V5` đo trực tiếp từ harness trên nhánh. |

---

## 6. Ngoài Phạm Vi Của PR Chặng 1 (Out of Scope)

- **Không thay đổi schema/migration PostgreSQL (`0009_*.sql`):** Lưu trữ bền vững bảng dẫn xuất xuống PostgreSQL & Qdrant `session_units_{project_id}` sẽ đi ở PR riêng `feat/Thang-ecce-session-schema-db` (Chặng 2) và `feat/Thang-ecce-qdrant-persistence-be-api` (Chặng 3) theo đúng ranh giới `AGENTS.md`.
- **Không sửa giao diện Frontend (`SAG/apps/web` / `DATN_FE`):** Drawer hiển thị `ContextAuditTrace` thuộc Chặng 4 (`-fe-ui`).
- **Không bật mặc định `SAG_SESSION_CONTEXT_MODE=on` trên môi trường production** khi chưa nghiệm thu holdout thật.

---

## 7. Ma Trận Kiểm Thử & Tiêu Chí Nghiệm Thu Chặng 1

- [x] Toàn bộ unit tests hiện có trên `main` (`test_config.py`, `test_adaptive_retrieval.py`, `test_audit_remediation.py`, ...) tiếp tục xanh 100%.
- [x] `ruff check` sạch lỗi trên toàn bộ file mới và file cập nhật.
- [x] `decision_router.route_query` tương thích ngược 100% với payload cũ của SiliconFlow `/systemone` lẫn payload mới có `memory_scope` + `temporal_mode`, và fail-open chính xác khi tắt mạng/thiếu key.
- [x] Khi `SAG_SESSION_CONTEXT_MODE=off` (mặc định), `prepare_ask` giữ nguyên hành vi cũ; khi `SAG_SESSION_CONTEXT_MODE=on`, số lần gọi `llm.complete` cho nén bộ nhớ bằng **0** ngay cả trên hội thoại `> 220` lượt.
- [x] Không rò rỉ nội dung của tin nhắn có nguồn tài liệu bị thu hồi quyền (`ACL Leak Rate = 0.0%`) và tin nhắn bị xóa qua `delete_message` biến mất tức thì khỏi bộ nhớ.
- [x] Chuỗi Ablation `V0 → V5` chứng minh định lượng cải thiện `Evidence Recall`, `Decision Update Accuracy`, `Coreference Closure` và giảm mạnh prompt tokens trên phiên dài.

---

## 8. Kết Quả Thực Thi Thực Tế (Cập Nhật Sau Khi Chạy Checks)

- **Trạng thái hiện tại:** Đã hoàn tất triển khai Chặng 1 trên nhánh `feat/Thang-ecce-baseline-harness-be-api` (tách từ `origin/main` @ `3411d16`).
- **Danh sách file đã thay đổi / thêm mới (17 files):**
  - Cập nhật trên `main`: `SAG/.env.example`, `SAG/apps/api/sag_api/core/config.py`, `SAG/apps/api/sag_api/services/decision_router.py`, `SAG/apps/api/sag_api/api/v1/decision.py`, `SAG/apps/api/sag_api/services/agent_domain.py`.
  - Tạo mới động cơ `SAG-ECCE`: `SAG/apps/api/sag_api/services/session_context/__init__.py`, `models.py`, `extractor.py`, `substrate.py`, `optimizer.py`, `runtime.py`.
  - Tạo mới kiểm thử & Ablation Harness: `SAG/apps/api/tests/test_ecce_session_context.py`, `SAG/apps/api/tests/eval_ecce_baseline/__init__.py`, `SAG/apps/api/tests/eval_ecce_baseline/benchmark_suite.py`, `SAG/apps/api/tests/test_ecce_ablation_benchmark.py`.
  - Tài liệu nghiên cứu & báo cáo baseline: `SAG/docs/Thang_Task/session-context-zero-mem/researchtask.md`, `SAG/docs/Thang_Task/session-context-zero-mem/BASELINE_ECCE_MAIN.md`.
- **Kết quả kiểm tra chất lượng thực tế:**
  - `.\.venv\Scripts\python.exe -m ruff check sag_api/core/config.py sag_api/services/decision_router.py sag_api/api/v1/decision.py sag_api/services/session_context tests/test_ecce_session_context.py tests/eval_ecce_baseline tests/test_ecce_ablation_benchmark.py` → **`All checks passed!`** (`exit code 0`).
  - `.\.venv\Scripts\python.exe -m pytest tests -q` → **`95 passed, 1 skipped in 27.91s`** (`test_ai_provider_smoke.py` opt-in skip).
  - Ablation `V0 → V5` (`239` turns, `27` episodes, `12` kịch bản tiếng Việt & đa bước — chi tiết tại `BASELINE_ECCE_MAIN.md`):
    - `V0_legacy`: Recall `0.0000` (`0/12`), `556.8` avg tokens, `12` LLM memory calls.
    - `V3_dual_hierarchy` (`Zero-Mem` Dual-View): Recall `0.9583` (`11/12`), Stale Contamination `1.0000`, `525.9` avg tokens, `0` LLM memory calls.
    - `V4_closure_temporal` (+ ECCE Superseding & Closure): Recall `1.0000` (`12/12`), Stale Contamination `0.3333`, `596.4` avg tokens, Density `2.79`.
    - `V5_ecce_full` (+ Submodular Lazy-Greedy Optimizer): Recall **`1.0000` (`12/12`)**, Stale Contamination **`0.0000` (`0/3`)**, Avg History Tokens **`390.2`** (`-29.9%` so với `V0`, `-34.6%` so với `V4`, giảm `>93%` so với raw session), Evidence Density **`4.27`** (`+53.0%` so với `V4`), **`0` LLM memory calls**, P50/P95 **`9.07 / 10.96 ms`**.
- **Tác động Database / Cấu hình / Bảo mật / Rollback:**
  - **Database:** `0` thay đổi migration, schema hay dữ liệu PostgreSQL/Qdrant (giữ tách biệt hoàn toàn với Chặng 2 `-db` theo `AGENTS.md`).
  - **Cấu hình & Rollback:** `SAG_SESSION_CONTEXT_MODE="off"` là mặc định ban đầu (`off | shadow | on`), nên hành vi runtime mặc định trên `main` được bảo toàn 100%; rollback tức thì bằng cách đặt lại `SAG_SESSION_CONTEXT_MODE=off` mà không cần sửa dữ liệu.
  - **Bảo mật & ACL:** Mọi tin nhắn đi qua `filter_messages_for_scope` fail-closed của Continuum BE; chữ ký `scope_signature` và vô hiệu hóa cache tức thì tại `delete_message`/`delete_thread` đảm bảo `ACL Leak Rate = 0.0%`; đóng gói bằng chứng lịch sử dưới dạng JSON dữ liệu trơ `sag.ecce.untrusted_session_evidence.v1` chống prompt injection xuyên lượt.
- **Rủi ro còn lại & các bước tiếp theo:**
  - Chặng 1 lưu chỉ mục `IncrementalSessionIndex` trong RAM có LRU/TTL (tự dựng lại từ bảng `messages` chuẩn của PostgreSQL qua keyset pagination khi restart worker); Chặng 2 (`feat/Thang-ecce-session-schema-db`) và Chặng 3 (`feat/Thang-ecce-qdrant-persistence-be-api`) sẽ bổ sung lưu trữ bền vững bảng dẫn xuất PostgreSQL `0009_*.sql` và collection `session_units_{project_id}` trên Qdrant.
  - Sau khi có môi trường staging kết nối khóa SiliconFlow thật, chạy thêm vòng `shadow` replay với `Kev-4b` (`/systemone`) và `BAAI/bge-m3` live trước khi chuyển mặc định từ `off` sang `on`.
