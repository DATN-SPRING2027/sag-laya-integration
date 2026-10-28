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


---

## 9. Thẩm Định & Khắc Phục Review PR #42 (`pullrequestreview-5678088744`)

Đã kiểm chứng độc lập toàn bộ 5 nhận xét trong review PR #42 của `thanhtai040805` đối chiếu với mã nguồn thực tế:

1. **Mục 1 (`[HIGH]` — Xung đột `requires_grounded_answer` & `_initial_tool_choice` khi `knowledge_only=True`) → ĐÚNG 100%:**
   - Trong `sag_api/schemas/agent.py:101`, `AskRequest.web_enabled` mặc định là `False`, dẫn tới `knowledge_only=True` ở mọi lượt hỏi thông thường.
   - Bổ sung `route_decision` và `session_citations` (kèm `cited_source_ids` bắc cầu) vào `AskPlan`.
   - Trong `agent_service.py`: khi câu hỏi có `memory_scope in {"session_history", "full_session_audit"}`, `not scoped` và có bằng chứng lịch sử hợp lệ (`bool(plan.session_citations)`), `_initial_tool_choice` trả `"auto"` thay vì cưỡng bức `search_context`, đồng thời `_finalize_session_citations` xác thực các nhãn `[S1], [S2]` (loại bỏ `[S99]` bịa đặt) và cho phép vượt qua cổng `requires_grounded_answer` mà vẫn bảo toàn thu hồi quyền bắc cầu qua `_message_source_ids`.
2. **Mục 2 (`[HIGH]` — Gọi trùng lặp 2 lần `SiliconFlow /systemone` & rơi telemetry `session_audit_trace` ở `shadow`) → ĐÚNG 100%:**
   - Tái sử dụng `plan.route_decision` trực tiếp trong `_initial_tool_choice` để giảm từ 2 xuống **1 lần gọi `route_query` duy nhất** mỗi lượt hỏi.
   - Ghi structured log và đẩy mục `{"kind": "session_context", ...}` vào `trace` (`Message.steps`) ở cả chế độ `shadow` và `on` (kèm `details={"scope": "knowledge", "sources": ...}` khi có nguồn tài liệu bắc cầu).
3. **Mục 3 (`[MEDIUM]` — Phủ định tiếng Việt trước `_CONFIRMED_CUE_RE` & thay thế ngoài 7 Slot cố định) → ĐÚNG 100%:**
   - Bổ sung kiểm tra phủ định theo mệnh đề (`_has_unnegated_confirmation`) chặn các phó từ `không / chưa / chẳng / đừng / chớ / khoan / tạm thời chưa / not / never` đứng trước động từ chốt, trong khi vẫn giữ đúng câu ghép 2 mệnh đề (*"Không dùng MongoDB nữa, chốt dùng PostgreSQL"*).
   - Bổ sung `extract_superseded_entity_keys` bắt mẫu thay thế tường minh (*"bỏ A, chốt B"*, *"thay A bằng B"*, *"chuyển sang B thay cho A"*, *"instead of A"*) để đánh dấu `SUPERSEDED` chính xác ngay cả với thực thể ngoài 7 slot cố định và bất kể thứ tự nhắc thực thể.
4. **Mục 4 (`[MEDIUM]` — Cấu hình `embedder` tường minh & Chuẩn hóa IDF động của BM25) → ĐÚNG 100%:**
   - Thêm `session_context_embedding_mode: Literal["zero_cost_local", "configured_http"] = "zero_cost_local"` trong `config.py` và nối HTTP Embedder tương thích OpenAI/SiliconFlow qua `_SHARED_TEXT_EMBEDDING_CACHE` khi bật `"configured_http"`.
   - Lưu `term_freqs` + chỉ mục đảo `term_postings` trong `IncrementalSessionIndex` để tính Robertson–Spärck Jones IDF động trên toàn bộ `len(self.turns)` tại thời điểm truy vấn, giúp điểm BM25 nhất quán 100% giữa lượt đầu và lượt cuối phiên với độ phức tạp $O(\sum_{t \in q} df(t))$.
5. **Mục 5 (`[SCALABILITY]` — Đọc DB $O(\Delta)$ bằng cursor & Khoanh vùng Ego-Subgraph PPR) → ĐÚNG 100%:**
   - Lưu cursor `(last_db_created_at, last_db_id)` và tập `seen_raw_ids` trong `IncrementalSessionIndex`: lượt hỏi kế tiếp chỉ query phần tin nhắn mới tăng thêm từ PostgreSQL sau khi đối chiếu nhanh `COUNT(id)` (phát hiện xóa tin nhắn giữa phiên) và kiểm tra quyền `get_authorized_source_ids` trên `cached_cited_source_ids`.
   - Trong `score_graph_ppr_view`, khoanh vùng Ego-Subgraph có trần khi phiên vượt ngưỡng `400` lượt (chỉ dựng `ig.Graph` trên các lượt chứa thực thể kích hoạt + Top-`256` ứng viên hybrid + lân cận `±1` cùng `Episode`).


### Kết Quả Thực Thi & Kiểm Chứng Sau Khắc Phục Review PR #42
- **Tập tin cập nhật trong bản sửa PR #42:**
  - `SAG/.env.example`, `SAG/apps/api/sag_api/core/config.py`
  - `SAG/apps/api/sag_api/services/agent_domain.py`, `SAG/apps/api/sag_api/services/agent_service.py`
  - `SAG/apps/api/sag_api/services/session_context/__init__.py`, `models.py`, `extractor.py`, `substrate.py`, `optimizer.py`, `runtime.py`
  - `SAG/apps/api/tests/test_ecce_session_context.py`, `SAG/docs/Thang_Task/session-context-zero-mem/BASELINE_ECCE_MAIN.md`, `SAG/docs/Thang_Task/session-context-zero-mem/researchtask.md`
- **Kết quả kiểm thử & chất lượng mã nguồn:**
  - `.\.venv\Scripts\python.exe -m ruff check sag_api/core/config.py sag_api/services/decision_router.py sag_api/api/v1/decision.py sag_api/services/session_context tests/test_ecce_session_context.py tests/eval_ecce_baseline tests/test_ecce_ablation_benchmark.py` → **`All checks passed!`**
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E501 sag_api/services/agent_service.py sag_api/services/agent_domain.py` → **`All checks passed!`** (mọi dòng thêm/sửa mới đều `<= 120` ký tự).
  - `.\.venv\Scripts\python.exe -m pytest tests -q` → **`98 passed, 1 skipped in 34.67s`** (bổ sung 3 bài test hồi quy mới trong `test_ecce_session_context.py` kiểm chứng toàn bộ 5 mục review PR #42: `knowledge_only=True` session grounding + `[S99]` stripping + empty-thread blocking, `1` lần gọi `route_query`, lưu `Message.steps` ở cả `shadow` và `on`, thu hồi quyền ACL bắc cầu xuyên lượt, đọc DB tăng dần $O(\Delta)$ bằng cursor, chắn phủ định tiếng Việt + chuỗi thay thế ngoài slot `React → Vue → Svelte`, IDF động BM25 + cấu hình `session_context_embedding_mode`, và Ego-Subgraph PPR `<= 400` lượt).


---

## 10. Chặng 2 (`feat/Thang-ecce-session-schema-db`) — Thiết Kế Schema Provenance, Decision Ledger & Graph Substrate

- **Ngày thực hiện:** 2026-10-28
- **Nhánh thực hiện:** `feat/Thang-ecce-session-schema-db` (tách mới trực tiếp từ `origin/main` @ `76f3ab9` sau khi merge PR #42)
- **Phạm vi PR (`-db` deliverable độc lập theo `AGENTS.md`):**
  - Chỉ bổ sung migration PostgreSQL tiến (`0009_session_context_ecce.sql`), script rollback (`scripts/0009_session_context_ecce_down.sql`), SQLAlchemy ORM models (`sag_api/db/models/session_context.py` + đăng ký `Base.metadata`), đồng bộ tài liệu lưu trữ (`data-ownership-and-storage.md`) và kiểm thử ràng buộc schema/cascade/migration.
  - **Không sửa bất kỳ migration cũ nào (`0001`–`0008`)**, không sửa cột của `threads`/`messages`, và không trộn logic runtime Qdrant/API của Chặng 3 (`-be-api`) vào nhánh `-db`.

### 10.1. Sự Thật Schema Hiện Hữu Trên `main` (`76f3ab9`) & PostgreSQL 16 Thực Tế
Qua kiểm tra trực tiếp `sag_api/db/models/agent.py`, `sag_api/db/models/source_project_mapping.py`, `sag_api/db/models/routing_rag.py` và `\d threads`, `\d messages` trên container `postgres:16` (`sag-db-1`):
1. `threads.id` và `messages.id` đều có kiểu **`VARCHAR(32)`** (`uuid.uuid4().hex`), không phải `VARCHAR(36)` → mọi khóa ngoại trỏ về `threads(id)` và `messages(id)` bắt buộc dùng `VARCHAR(32)`.
2. `threads.owner_key` có kiểu `VARCHAR(64)` (nullable cho dữ liệu cũ cục bộ, có mặt với phiên Continuum).
3. `source_project_mappings.organization_id` và `project_id` dùng **`VARCHAR(256)`** để chứa đầy đủ định danh từ Continuum BE (MongoDB); theo `SAG/docs/data-ownership-and-storage.md`, tuyệt đối không tạo bảng hay FK sang User/Project của MongoDB mà lưu `tenant_id VARCHAR(256)` và `project_id VARCHAR(256)` kèm `CHECK (length(trim(...)) > 0)`.
4. Các bảng dẫn xuất chuẩn của `SAG main` (`DocumentVersion`, `SearchUnit`, `KnowledgeUnit`, `KnowledgeGraphEdge`) dùng khóa chính UUIDv5 tất định **`VARCHAR(36)`** và mốc mở lưỡng thời gian `valid_to = 9999-12-31T23:59:59+00:00` (`UTCDateTime()`).
5. Thư mục `SAG/apps/api/migrations/` chỉ chứa các tệp migration tiến `0001_*.sql` → `0008_*.sql`; script rollback được đặt tách riêng tại `SAG/apps/api/scripts/` (như `acl_001_source_project_mappings_down.sql`) để tránh rủi ro script chạy tuần tự `migrations/*.sql` vô tình xóa bảng.

### 10.2. Đặc Tả Chi Tiết 3 Bảng Dẫn Xuất (`0009_session_context_ecce.sql`)

1. **`session_units` (Đơn vị truy xuất đa độ mịn có truy nguyên gốc hội thoại — `TURN | WINDOW | EPISODE`):**
   - Khóa chính `id VARCHAR(36)` (UUIDv5 tất định `generate_session_unit_id(thread_id, unit_kind, ordinal, message_from_id)`).
   - Phạm vi cô lập: `tenant_id VARCHAR(256) NOT NULL`, `project_id VARCHAR(256) NOT NULL`, `thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE`, `owner_key VARCHAR(64) NULL`.
   - Phân cấp & truy nguyên: `unit_kind VARCHAR(16) NOT NULL DEFAULT 'TURN'` (`TURN | WINDOW | EPISODE`), `ordinal INTEGER NOT NULL >= 0`, `episode_id VARCHAR(64) NOT NULL`, `episode_ordinal INTEGER NOT NULL >= 0`, `window_id VARCHAR(64) NULL`, `message_from_id VARCHAR(32) NOT NULL REFERENCES messages(id) ON DELETE CASCADE`, `message_to_id VARCHAR(32) NOT NULL REFERENCES messages(id) ON DELETE CASCADE`, `role VARCHAR(16) NULL`.
   - Ngữ nghĩa quyết định & đóng bao: `epistemic_status VARCHAR(24) NOT NULL DEFAULT 'FACT'` (`FACT | PROPOSED | CONSIDERING | CONFIRMED | SUPERSEDED | NEGATED | OPEN_ISSUE`), `topic_slot_key VARCHAR(128) NULL`, `is_anaphoric_or_short BOOLEAN NOT NULL DEFAULT FALSE`.
   - Ranh giới ACL bắc cầu (Fail-Closed): `security_partition_ids_json JSON NOT NULL DEFAULT '[]'`, `cited_source_ids_json JSON NOT NULL DEFAULT '[]'`.
   - Đặc trưng Zero-Token & kiểm soát vector drift: `entity_mentions_json JSON NOT NULL`, `sentences_json JSON NOT NULL`, `term_freqs_json JSON NOT NULL` (phục vụ Dynamic Corpus-Wide BM25), `doc_word_len INTEGER NOT NULL >= 0`, `token_count INTEGER NOT NULL >= 0`, `content_hash VARCHAR(64) NOT NULL` (SHA-256), `embedding_model_signature VARCHAR(128) NOT NULL`, `qdrant_point_id VARCHAR(36) NULL`.
   - Hiệu lực lưỡng thời gian: `[valid_from, valid_to)` với `CHECK (valid_to >= valid_from)`, kèm `created_at`, `updated_at`.
   - Ràng buộc duy nhất & chỉ mục nóng: `uq_session_units_thread_kind_ordinal (thread_id, unit_kind, ordinal)`, `ix_session_units_project_thread`, `ix_session_units_thread_episode`, `ix_session_units_message_from`, `ix_session_units_message_to`, `ix_session_units_temporal`.

2. **`session_decision_ledger` (Sổ cái trạng thái công việc & quyết định lưỡng thời gian):**
   - Khóa chính `id VARCHAR(36)` (UUIDv5 tất định `generate_session_decision_id(session_unit_id, topic_slot_key)`).
   - Khóa ngoại cascade: `thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE`, `session_unit_id VARCHAR(36) NOT NULL REFERENCES session_units(id) ON DELETE CASCADE`.
   - Phân loại & trạng thái: `ledger_kind VARCHAR(24) NOT NULL DEFAULT 'DECISION'` (`DECISION | OPEN_TASK | CONSTRAINT | OPEN_ISSUE`), `topic_slot_key VARCHAR(128) NOT NULL`, `turn_ordinal INTEGER NOT NULL >= 0`, `epistemic_status VARCHAR(24) NOT NULL`, `summary_text TEXT NOT NULL`.
   - Dòng dõi thay thế (`Lineage` & `Non-Slot Replacement`): `entity_keys_json JSON`, `superseded_entity_keys_json JSON`, `companion_unit_ids_json JSON`, `supersedes_id VARCHAR(36) NULL REFERENCES session_decision_ledger(id) ON DELETE SET NULL`, `superseded_by_id VARCHAR(36) NULL REFERENCES session_decision_ledger(id) ON DELETE SET NULL`, `supersedes_ids_json JSON`.
   - Ranh giới ACL & thời gian: `security_partition_ids_json JSON`, `cited_source_ids_json JSON`, `valid_from`, `valid_to` (`CHECK (valid_to >= valid_from)`), `created_at`, `updated_at`.
   - Ràng buộc duy nhất & chỉ mục: `uq_session_decision_unit_slot (session_unit_id, topic_slot_key)`, `ix_session_decision_thread_slot_status`, `ix_session_decision_project_status`, `ix_session_decision_supersedes`, `ix_session_decision_superseded_by`.

3. **`session_graph_edges` (Đồ thị đa quan hệ phục vụ `igraph` Personalized PageRank & Đóng bao bằng chứng):**
   - Khóa chính `id VARCHAR(36)` (UUIDv5 tất định `generate_session_graph_edge_id(source_unit_id, source_node_key, target_node_key, edge_type)`).
   - Neo truy nguyên chống rò rỉ dữ liệu mồ côi khi xóa lẻ tin nhắn: `source_unit_id VARCHAR(36) NOT NULL REFERENCES session_units(id) ON DELETE CASCADE` (mọi cạnh — kể cả `ALIAS_CANDIDATE` hay `ENTITY_MENTION` — đều bắt buộc neo vào `SessionUnit` phát sinh ra nó), kết hợp `target_unit_id VARCHAR(36) NULL REFERENCES session_units(id) ON DELETE CASCADE` (cho cạnh nối 2 lượt như `ADJACENT_IN_EPISODE`, `SUPERSEDES`). Nhờ `source_unit_id NOT NULL`, ràng buộc `UNIQUE (source_unit_id, source_node_key, target_node_key, edge_type)` hoạt động đồng nhất tuyệt đối trên cả PostgreSQL và SQLite (không gặp bẫy `NULL != NULL`) và xóa bất kỳ `Message` nào cũng tự động dọn sạch 100% cạnh đồ thị phát sinh từ tin nhắn đó.
   - Đỉnh & trọng số: `source_node_type` / `target_node_type` (`UNIT | SENTENCE | ENTITY | EPISODE | SOURCE`), `source_node_key` / `target_node_key` (`VARCHAR(256)`), `edge_type` (`ENTITY_MENTION | SENTENCE_IN_UNIT | ADJACENT_IN_EPISODE | SUPERSEDES | ALIAS_CANDIDATE | CITES_DOCUMENT_SOURCE`), `weight DOUBLE PRECISION NOT NULL DEFAULT 1.0` (`CHECK (weight >= 0.0)`), `metadata_json JSON`, `created_at`.

### 10.3. Danh Sách Thay Đổi Dự Kiến & Ma Trận Kiểm Thử Chặng 2

| File | Hành động | Mục đích |
|---|---|---|
| `SAG/apps/api/migrations/0009_session_context_ecce.sql` | Tạo mới | Forward DDL tạo 3 bảng dẫn xuất, `CHECK`, `UNIQUE`, FK cascade và indexes. |
| `SAG/apps/api/scripts/0009_session_context_ecce_down.sql` | Tạo mới | Script rollback tường minh xóa sạch indexes & 3 bảng dẫn xuất theo thứ tự ngược phụ thuộc. |
| `SAG/apps/api/sag_api/db/models/session_context.py` | Tạo mới | SQLAlchemy 2.0 ORM models (`SessionUnit`, `SessionDecisionLedger`, `SessionGraphEdge`) + hàm sinh UUIDv5 tất định. |
| `SAG/apps/api/sag_api/db/models/__init__.py` | Cập nhật nhỏ | Đăng ký 3 model mới vào `Base.metadata` để đồng bộ `init_db()` và test harness. |
| `SAG/docs/data-ownership-and-storage.md` | Cập nhật nhỏ | Đồng bộ bảng phân định quyền sở hữu lưu trữ theo Rule 04. |
| `SAG/apps/api/tests/test_session_context_schema.py` | Tạo mới | Kiểm thử toàn diện: UUIDv5 tất định, ORM CRUD, mọi `CHECK` & `UNIQUE` constraints, cascade khi xóa lẻ `Message` / xóa `Thread` / `ON DELETE SET NULL` trên chuỗi `SUPERSEDES`, và kiểm chứng tương đương DDL của `0009_*.sql` / `_down.sql`. |

**Tiêu chí nghiệm thu Chặng 2:**
- [x] Không chạm vào `0001`–`0008` hay cột của bảng hiện hữu.
- [x] Kiểm chứng trực tiếp vòng lặp `up → insert & FK cascade checks → down → up` trên **PostgreSQL 16 thật** (`sag-db-1`, trên schema tạm cô lập `sag_ecce_migration_qa_temp`, sau đó dọn sạch schema tạm).
- [x] Toàn bộ test suite `pytest tests -q` và `ruff check` xanh 100%.

### 10.4. Kết Quả Thực Thi Chặng 2 & Đánh Giá Tác Động Migration / Rollback
- **Danh sách tập tin đã tạo/cập nhật (`6` tệp):**
  - Tạo mới: `SAG/apps/api/migrations/0009_session_context_ecce.sql`, `SAG/apps/api/scripts/0009_session_context_ecce_down.sql`, `SAG/apps/api/sag_api/db/models/session_context.py`, `SAG/apps/api/tests/test_session_context_schema.py`.
  - Cập nhật đồng bộ: `SAG/apps/api/sag_api/db/models/__init__.py`, `SAG/docs/data-ownership-and-storage.md`, `SAG/docs/Thang_Task/session-context-zero-mem/researchtask.md`.
- **Kết quả kiểm chứng thực tế:**
  - `.\.venv\Scripts\python.exe -m ruff check sag_api/db/models/session_context.py sag_api/db/models/__init__.py tests/test_session_context_schema.py` → **`All checks passed!`**
  - `.\.venv\Scripts\python.exe -m pytest tests -q` → **`102 passed, 1 skipped in 35.85s`** (`4` test mới trong `test_session_context_schema.py` kiểm chứng sinh khóa UUIDv5, đối khớp `1:1` cột/index giữa SQL DDL và ORM `Base.metadata`, `ON DELETE CASCADE` + `ON DELETE SET NULL`, và toàn bộ `CHECK`/`UNIQUE` constraints).
  - **PostgreSQL 16 live (`sag-db-1`):** Đã chạy thực tế trên schema tạm `sag_ecce_migration_qa_temp` (`LIKE public.agents / public.threads / public.messages INCLUDING ALL`): `0009_session_context_ecce.sql` chạy lặp idempotent `2` lần (`exit code 0`), xóa lẻ `messages` dọn sạch `session_units`, `session_decision_ledger` và `session_graph_edges` đồng thời chuyển `supersedes_id` sang `NULL`, chạy `0009_session_context_ecce_down.sql` xóa sạch 3 bảng dẫn xuất mà giữ nguyên `messages`, chạy lại `0009_session_context_ecce.sql` khôi phục đủ `20` chỉ mục (PK + UNIQUE + B-Tree), và `DROP SCHEMA sag_ecce_migration_qa_temp CASCADE`.
- **Tác động Database, Lock & Rollback (`AGENTS.md` Mandatory DB Impact Report):**
  - **Tính chất thay đổi:** Thuần túy bổ sung mới (`additive expand migration`) — tạo 3 bảng mới `session_units`, `session_decision_ledger`, `session_graph_edges`. Không sửa cột của `threads`/`messages` và không chạm vào migration `0001`–`0008`.
  - **Tác động Lock khi chạy trên Production:** Các câu lệnh `CREATE TABLE IF NOT EXISTS ... REFERENCES threads(id) / messages(id)` trong PostgreSQL 16 chỉ giữ khóa ngắn `SHARE ROW EXCLUSIVE` ở bước ghi metadata FK (mili-giây trên bảng rỗng mới tạo, không quét dữ liệu lịch sử cũ và không khóa đọc `SELECT` trên `threads`/`messages`).
  - **Quy trình Rollback an toàn:** Chạy `psql -v ON_ERROR_STOP=1 -f SAG/apps/api/scripts/0009_session_context_ecce_down.sql` để xóa sạch 3 bảng dẫn xuất theo thứ tự ngược phụ thuộc (`session_graph_edges → session_decision_ledger → session_units`). Toàn bộ lịch sử gốc trong `threads` và `messages` được bảo toàn 100%, không mất dữ liệu người dùng.
- **Bước tiếp theo (Chặng 3):** Sau khi PR Chặng 2 (`-db`) được review & merge vào `main`, tách nhánh mới `feat/Thang-ecce-multiview-persistence-be-api` từ `main` để nối persistence hai chiều (`PostgreSQL` 3 bảng mới + `Qdrant` `session_units_{project_id}` + backfill cuốn chiếu) vào `sag_api/services/session_context/runtime.py`.


---

## 11. Thẩm Định & Khắc Phục 4 Điểm Review Trên PR #43 (`feat/Thang-ecce-session-schema-db`)

Đã kiểm chứng độc lập cả 4 nhận xét trên PR #43 bằng test thực nghiệm trên SQLite & PostgreSQL 16 (`sag-db-1`). **Kết luận: Cả 4 điểm đều ĐÚNG ở góc độ thiết kế ràng buộc CSDL và hàm băm khóa, xứng đáng được khóa chặt ngay ở tầng Schema Chặng 2 trước khi sang Chặng 3:**

1. **Finding 1 (`Xóa tin giữa window A–B–C`) → ĐÚNG 100%:**
   - **Nguyên nhân:** `session_units` ban đầu chỉ giữ FK tới hai đầu mút `message_from_id` (`A`) và `message_to_id` (`C`). Khi tạo đơn vị `WINDOW` (hoặc `EPISODE`) gộp `A–B–C` rồi xóa tin ở giữa `B`, FK ở `A` và `C` không kích hoạt nên hàng `WINDOW` chứa đặc trưng dẫn xuất từ `B` vẫn tồn tại.
   - **Khắc phục triệt để ở tầng DB:**
     - Bổ sung cột `source_message_ids_json JSON NOT NULL DEFAULT '[]'` cùng ràng buộc `ck_session_units_turn_single_msg` (`unit_kind != 'TURN' OR message_from_id = message_to_id`) và `ck_session_units_span_source_ids` (`message_from_id = message_to_id OR json_array_length(source_message_ids_json) >= 2`).
     - Bổ sung bảng liên kết truy nguyên `session_unit_messages` (`SessionUnitMessage`) nối `ON DELETE CASCADE` vào `messages(id)` và `(session_unit_id, tenant_id, project_id, thread_id)`, kết hợp bộ trigger:
       - `trg_session_units_before_update_guard`: chặn sửa định danh truy nguyên & danh sách tin nhắn cấu thành khi `UPDATE`.
       - `trg_session_units_after_insert_link`: kiểm tra kiểu mảng chuỗi JSON + bắt buộc chứa đủ hai đầu mút `message_from_id` / `message_to_id`, tự động liên kết toàn bộ tin nhắn thành viên (bao gồm cả dải `TURN` ở giữa) vào `session_unit_messages`, và kiểm tra mọi tin nhắn thành viên thuộc đúng `NEW.thread_id`.
       - `trg_session_unit_messages_before_update_guard`: khóa bất biến các dòng trong `session_unit_messages`.
       - `trg_session_unit_messages_cascade_unit`: `AFTER DELETE ON session_unit_messages` tự động `DELETE FROM session_units WHERE id = OLD.session_unit_id`.
     - Kết quả: Xóa bất kỳ tin nhắn nào (`A`, **`B` ở giữa**, hay `C`) trong `WINDOW` / `EPISODE` đều tự động xóa sạch `SessionUnit` bao phủ nó cùng toàn bộ `session_decision_ledger` và `session_graph_edges` dẫn xuất (hoạt động đồng nhất trên cả PostgreSQL 16 và SQLite, kể cả qua `0009_*.sql` lẫn `Base.metadata.create_all`).

2. **Finding 2 (`Default ORM khác SQL` trên `sentences_json` và các cột `JSON`/số) → ĐÚNG 100%:**
   - **Nguyên nhân:** Trong `sag_api/db/models/session_context.py`, các cột `JSON` chỉ khai báo `default=list`/`default=dict` ở tầng Python mà thiếu `server_default=text("'[]'")` / `server_default=text("'{}'")` ở tầng DDL CSDL; các cột số dùng chuỗi `'0'` thay vì hằng số `0`.
   - **Khắc phục:** Bổ sung đầy đủ `server_default=text("'[]'")`, `server_default=text("'{}'")`, `server_default=text("0")`, `server_default=text("1")`, `server_default=text("1.0")` trên toàn bộ cột của ORM để DDL sinh từ `Base.metadata.create_all` khớp `1:1` với `0009_session_context_ecce.sql`, kèm bài test đối khớp `server_default` tự động.

3. **Finding 3 (`Liên kết sai tenant/project/thread` giữa các bảng dẫn xuất và `messages`) → ĐÚNG 100%:**
   - **Nguyên nhân:** FK đơn cột `REFERENCES session_units(id)` và `REFERENCES messages(id)` cho phép bản ghi con (`ledger`, `graph_edges`) mang `(tenant_id, project_id, thread_id)` khác với bản ghi cha.
   - **Khắc phục:**
     - Thêm `CONSTRAINT uq_session_units_scope_identity UNIQUE (id, tenant_id, project_id, thread_id)` trên `session_units`.
     - Chuyển FK trên `session_unit_messages`, `session_decision_ledger` và cả 2 đầu `source_unit_id` / `target_unit_id` của `session_graph_edges` sang **Composite Foreign Key** `(unit_id, tenant_id, project_id, thread_id) REFERENCES session_units (id, tenant_id, project_id, thread_id) ON DELETE CASCADE` (`MATCH SIMPLE` chuẩn SQL vẫn cho phép `target_unit_id IS NULL` nhưng bắt buộc khớp đủ 4 cột khi `NOT NULL`).
     - Bổ sung kiểm tra khớp `messages.thread_id = NEW.thread_id` trong trigger của `session_units`, và kiểm tra khớp `(tenant_id, project_id, thread_id)` (cùng chống tự tham chiếu `id = supersedes_id`) trên `supersedes_id` / `superseded_by_id` của `session_decision_ledger`.

4. **Finding 4 (`Trùng UUIDv5 khi ghép chuỗi bằng dấu ':'`) → ĐÚNG 100%:**
   - **Nguyên nhân:** Ghép `f"...:{source_node_key}:{target_node_key}:..."` gây va chạm tiền ảnh khi `node_key` chứa dấu `:` (ví dụ `("entity:a:b", "c")` và `("entity:a", "b:c")` sinh cùng một chuỗi đầu vào UUIDv5).
   - **Khắc phục:** Chuyển cả 4 hàm `generate_session_unit_id`, `generate_session_unit_point_id`, `generate_session_decision_id`, `generate_session_graph_edge_id` sang mã hóa tuple JSON chuẩn hóa đơn ánh (`json.dumps([...], separators=(",", ":"), ensure_ascii=False)` — song ánh toán học trên bộ chuỗi/số nguyên bất kỳ), đồng thời đưa cả `source_node_type` và `target_node_type` vào khóa duy nhất `uq_session_graph_edges_provenance` và hàm sinh ID cạnh.

### 11.1. Kết Quả Kiểm Chứng Thực Nghiệm Sau Khắc Phục
- **`ruff check`:** `.\.venv\Scripts\python.exe -m ruff check sag_api/db/models/session_context.py sag_api/db/models/__init__.py tests/test_session_context_schema.py` → **`All checks passed!`**
- **`pytest` toàn bộ API:** `.\.venv\Scripts\python.exe -m pytest tests -q` → **`102 passed, 1 skipped in 30.24s`** (kiểm chứng đầy đủ cả 4 finding: xóa tin giữa `WINDOW` `A–B–C` & `X–Y–Z`, đối khớp `server_default` + raw SQL `INSERT` bỏ trống `sentences_json`, chặn tham chiếu chéo `tenant_id/project_id/thread_id` trên mọi bảng con & `supersedes_id`, và không đụng độ UUIDv5 khi đầu vào chứa `:`).
- **PostgreSQL 16 live (`sag-db-1`):**
  - `PG16_SQL_MIGRATION_QA_PASSED`: Chạy `0009_session_context_ecce.sql` lặp 2 lần idempotent, chặn FK chéo tenant/thread, xóa tin giữa `mB` tự động xóa sạch `WINDOW` `wABC` + `session_decision_ledger` + `session_graph_edges`, chạy `0009_session_context_ecce_down.sql` xóa sạch 5 trigger + 5 function + 4 bảng dẫn xuất trong khi giữ nguyên `messages`, chạy lại `0009_*.sql` thành công và dọn sạch schema tạm `sag_ecce_migration_qa_temp`.
  - `PG16_ORM_COMPILED_DDL_QA_PASSED`: Kiểm chứng DDL PostgreSQL biên dịch từ `Base.metadata.create_all` trên `sag-db-1` (`sag_ecce_orm_qa_temp`) cấp đúng default `'[]'` cho `sentences_json` ở tầng DB và kích hoạt cascade xóa tin giữa `mB` đồng nhất 100% với file SQL migration.
