# Báo cáo Khắc phục Toàn diện Hệ thống RAG SAG (Pha P0 → P5)

**Tài liệu gốc đối chiếu:** `SAG/docs/SAG-5-stage-hallucination-audit.md`  
**Kho mã nguồn:** `d:\DoAnTotnghiep\sag-laya-integration` (`SAG/apps/api/sag_api`, `SAG/apps/api/sag_agent`, `SAG/apps/api/tests`, `SAG/apps/api/migrations`)  
**Phạm vi Git:** Nhánh gốc `2aca825` (`feat/thanhtai040805-retrieval-routing-and-stage2b-upgrade-be-api`) → nhánh cuối **`fix/sag-p5-flags-config-docs-tests` @ `8a79ef1`** (52 commit tuyến tính, giữ nguyên cục bộ, không push/rebase/force)  
**Trạng thái kiểm thử cuối cùng (offline):** **`545 passed, 5 skipped`** (tăng từ `345 passed, 1 skipped` ở nhánh gốc; 5 test bị skip đều là integration/smoke opt-in cần hạ tầng live)  
**Bảo mật:** Tuyệt đối không đọc hay in giá trị bí mật trong `SAG/.env`; mọi tham chiếu cấu hình chỉ nêu tên biến và cấu trúc.

---

## 1. Tóm tắt điều hành (Executive Summary)

### 1.1. Bối cảnh & 2 Quyết định kiến trúc đã chốt
Kiểm toán tĩnh 5 giai đoạn (`SAG/docs/SAG-5-stage-hallucination-audit.md`) phát hiện hàng loạt nguyên nhân gây hallucination hệ thống, sai lệch tiếng Việt (NFC/NFD, tokenization, BM25), rò rỉ ACL/vector và hiện tượng *developer-hallucination* (tài liệu/cờ/test khẳng định sai kiến trúc vận hành thực tế). Trước khi triển khai khắc phục 6 pha (P0 → P5), dự án đã chốt **2 quyết định kiến trúc** và **4 nguyên tắc bất biến**:

1. **Bỏ hẳn Laya Router — sử dụng provider ngoài cấu hình được:** Việc thiếu `laya_router.py` / `api/v1/laya.py` là chủ đích thiết kế, không khôi phục. Định tuyến chạy qua provider ngoài cấu hình được trong `Settings` (`services/decision_router.py`, mặc định tương thích SiliconFlow `Kev-4b`). Toàn bộ tàn dư Laya (`SAG_ENABLE_LAYA`, `SAG_LAYA_*`, `SAG_OPENROUTER_API_KEY`, key trùng `SAG_DECISION_MODEL`, các biến E2 đã gỡ trong `compose.yaml`) được dọn sạch, đồng thời sửa triệt để lỗ hổng fallback regex & ép `"project"` khi provider lỗi.
2. **Bỏ hẳn đường R2/S3 Object-Ingest & post-index Disk GC:** Đường R2 là code chết nhưng vẫn mở endpoint (`POST .../documents/ingest-object`) và nhánh Disk GC có nguy cơ xóa file gốc trên đĩa sau khi index. Toàn bộ đường R2 và Disk GC được gỡ bỏ hoàn toàn khỏi runtime, giữ nguyên luồng upload file thông thường (`handle_document_upload` → `prepare_document` → `READY`) và MinerU Mode 2 (batch presigned upload).
3. **4 Nguyên tắc kỹ thuật bắt buộc:**
   - **Regex chỉ dùng cho cú pháp/cấu trúc, CẤM dùng cho quyết định ngữ nghĩa:** Chỉ giữ regex cho kiểm tra cú pháp/cấu trúc (ID/semver/cursor/URL/citation bracket `[n]`/escape/dấu câu `?` và `;`/ngày tháng/đường dẫn tệp/boilerplate/redact secret); thay toàn bộ regex quyết định ngữ nghĩa (phân loại intent, `question_scope`, `memory_scope`, `abstention`, epistemic state, chọn tool, tách mệnh đề theo liên từ EN/VI, và tách JSON LLM) bằng structured output (`response_format`), `ToolCapability` metadata và `JSONDecoder.raw_decode()`. Đối với chunking Markdown/HTML, `parsing/canonical.py` dùng parser cấu trúc dòng / state-machine đơn định (`_HEADING_RE`, `_TABLE_ROW_RE`, `_FENCE_OPEN_RE`...) kết hợp `BeautifulSoup`/`markdownify` cho HTML `<table>` (việc chuyển sang AST `markdown-it-py` đầy đủ được ghi hoãn ở Mục 4 vì gói chưa có trong `pyproject.toml`/`.venv`).
   - **Fail-closed & `degraded=true` tường minh:** Mọi chế độ suy giảm (router lỗi, extractive fallback, DB lỗi ở evidence, entailment không parse được, LLM judge bất đồng/lỗi JSON) đều phải gắn `degraded=true` + `degraded_reason` / trả `no_answer` hoặc `None`, tuyệt đối không báo `answered` / `"project"` / `"tie"` giả.
   - **Chuẩn hóa NFC một lần ở biên vào:** Dùng chung `normalize_text()` (NFC + `\r\n -> \n`) tại cả đầu vào ingest lẫn truy vấn/session.
   - **Một tokenizer dùng chung:** Thống nhất 1 bộ đếm token (`tiktoken` `cl100k_base` kèm fallback bảo thủ cho UTF-8/CJK khi offline cold-cache) và 1 bộ tách token BM25 Unicode (`\p{L}+|\d+` trên NFC) xuyên suốt index, query và session.

### 1.2. Bảng tổng hợp 6 nhánh Git (P0 → P5)

| Pha | Nhánh Git | Dải Commit | Số Commit | Trọng tâm chính | Kết quả `pytest` | Kết quả Verification Wave |
|---|---|---|---:|---|---|---|
| **Gốc** | `feat/thanhtai040805-retrieval-routing-and-stage2b-upgrade-be-api` | `2aca825` | — | Điểm xuất phát trước remediation | `345 passed, 1 skipped` | Baseline |
| **P0** | `fix/sag-p0-remove-laya-r2` | `2aca825..28e32bf` | 3 | Dọn sạch Laya/E2/R2 + Disk GC; thêm ADR & `SUPERSEDED` docs | `349 passed, 1 skipped` | Vòng 1 **PASS** (Reviewer `APPROVE`, Challenger `18/18 PASS`, Auditor `CLEAN`) |
| **P1** | `fix/sag-p1-grounding` | `28e32bf..c3f9a84` | 12 | Chặn hallucination trực tiếp: raw cosine floor, `[1, 99]`, `web_enabled` grounding, `answerability`, entailment check, ACL, SSE safety | `402 passed, 1 skipped` | Vòng 1 FAIL (`abf8fc3` Integrity Violation) → Vòng 2 fix → Vòng 3 **PASS** (`402+1`, Auditor `CLEAN`) |
| **P2** | `fix/sag-p2-no-semantic-regex` | `c3f9a84..3b358ab` | 6 | Gộp router+scope+intent 1 lần gọi; xóa regex ngữ nghĩa ở router/scope/agent/session/retrieval_plan/llm_judge & `isinstance(llm, LLMClient)`/`inspect.signature`; `StructuredLLMClient` Protocol; `llm_judge` fail-closed | `432 passed, 1 skipped` | Vòng 1 **PASS** (Reviewer `APPROVE`, Challenger `6/6`, Auditor `CLEAN`) |
| **P3** | `fix/sag-p3-index-vector-integrity` | `3b358ab..12eb3c3` | 9 | Migration `0013` (`char_start`/`char_end`/`text_snapshot`, `CASCADE` FK); `model_signature` cache & guard đổi model; `dim=1024`; xóa Qdrant khi `delete_source` & reconcile; bỏ `DELETE /collections` ở session; OCTX `event_vectors_wide` | `470 passed, 1 skipped` | Vòng 1 FAIL (Major `delete_source` legacy ID) → Vòng 2 **PASS** (`470+1`, Auditor `CLEAN`) |
| **P4** | `fix/sag-p4-parsing-vietnamese-chunking` | `12eb3c3..1a4c1f3` | 13 | Migration `0014` (`is_boilerplate`, `logical_source_id` 512); `text_utils.py` (NFC + `tiktoken` + splitter đa ngôn ngữ); `bm25_tokenizer.py` Unicode; parser Markdown/HTML/PDF/DOCX/PPTX/XLSX/RTF/Web; `/parsed` & `/outline` canonical; sanitizer | `510 passed, 1 skipped` | Vòng 1 FAIL (`"- 5 -"` list hijack) → Vòng 2 **PASS** (`510+1`, Challenger `10/10`, Auditor `CLEAN`) |
| **P5** | `fix/sag-p5-flags-config-docs-tests` | `1a4c1f3..8a79ef1` | 9 | `sag_language` (`en`/`zh`/`vi`); bỏ `document_strict_filtering`; `canonical_hybrid`; `llm_judge` mù + đảo A/B; `prod` validator & `SAG_SERVICE_API_KEY`; `cost_known`; thu hẹp `except`; `Makefile` pytest; live integration; rework dọn regex/keyword `query_analysis`/superseded/`web_search` + metric `tiktoken` fallback | **`545 passed, 5 skipped`** | Vòng 1 **PASS** (`cbf4ac3`) → Rework P5 (`143ff5b`, `8a79ef1`): **`545 passed, 5 skipped`** |

---

## 2. Chi tiết từng pha (P0 → P5)

### 2.1. Pha P0 — Dọn Laya & R2 (`fix/sag-p0-remove-laya-r2` @ `28e32bf`)

- **Nhánh & Commits (`2aca825..28e32bf`, 3 commits):**
  1. `921a422` — `fix(sag-api): remove R2 object-ingest path and post-index Disk GC`
  2. `a8d7c32` — `chore(sag): clean dead Laya/E2/R2 env & compose keys and test config shims`
  3. `28e32bf` — `docs(sag): add ADR for Laya/R2 removal and mark superseded docs`
- **Hạng mục đã xử lý:**
  - Gỡ bỏ hoàn toàn luồng R2/S3 Object-Ingest và **post-index Disk GC** (một phần liên quan `S1-03` trên `remote_object.py` được xử lý bằng cách xóa hẳn module).
  - Dọn dẹp biến chết Laya, OpenRouter, E2, R2 trong cấu hình và tài liệu (`S3-09`/`S4-09` phần tàn dư env, `XC-04` phần docs cũ Laya/R2), giữ nguyên shim tương thích ngược một chiều khi nâng cấp config cũ (`_migrate_legacy_decision_config`, `_normalize_overrides`).
- **Tệp thay đổi / tạo mới / xóa:**
  - **Xóa (1):** `SAG/apps/api/sag_api/parsing/remote_object.py`
  - **Tạo mới (1):** `SAG/docs/adr-remove-laya-and-r2.md`
  - **Sửa code, config & docs (17):** `SAG/apps/api/sag_api/api/v1/documents.py` (xóa `POST .../ingest-object`), `SAG/apps/api/sag_api/schemas/routing_rag.py` (xóa `DocumentObjectIngestRequest`, `DocumentReprocessRequest`), `SAG/apps/api/sag_api/core/config.py` (xóa `object_ingest_*`, `remote_snapshot_gc_enabled`), `SAG/apps/api/sag_api/services/document_service.py`, `SAG/apps/api/sag_api/jobs/tasks.py` (xóa `needs_remote_fetch`, `remote_source_url`, post-index Disk GC), `SAG/apps/api/sag_api/parsing/service.py`, `SAG/apps/api/sag_api/parsing/mineru_official.py` (xóa Mode 1 URL task `_create_url_task`/`_poll_task`, giữ Mode 2 batch presigned upload), `SAG/.env.example`, `SAG/compose.yaml`, `README.md`, `SAG/README.md`, `SAG/docs/phase-0-contracts-and-foundations.md`, `SAG/docs/phase-1-2-report.md`, `SAG/docs/phase-3-review-report.md`, `SAG/docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`, `SAG/tasks/plan.md`, `SAG/tasks/todo.md` (gắn banner `SUPERSEDED`). Ngoài git: dọn 15 dòng biến chết trong `SAG/.env` cục bộ (không đọc/in secret).
- **Test thêm mới & sửa:**
  - `SAG/apps/api/tests/test_r2_object_ingest_and_mineru_routing.py`: xóa `test_r2_object_ingest_e2e_streaming_verification_and_disk_gc`; viết lại `test_official_mineru_v4_direct_r2_url_task_and_batch_upload_fallback` → `test_official_mineru_v4_batch_presigned_upload`; thêm `test_normal_upload_reaches_ready_and_preserves_local_file_without_disk_gc`; giữ nguyên test định tuyến MinerU và streaming local parser.
  - `SAG/apps/api/tests/test_canonical_ingestion_readiness.py`: xóa nhánh gọi R2 object-ingest & tham số R2 trong reprocess; thêm `test_ingest_object_endpoint_removed_returns_404` và `test_project_reprocess_endpoint_works_without_body_and_r2_schemas_removed`.
  - `SAG/apps/api/tests/test_config.py`: thêm `test_legacy_laya_and_openrouter_decision_config_shims_migrate_to_siliconflow` và `test_r2_object_ingest_settings_removed`.
- **Kết quả `pytest` & kiểm chứng:**
  - `reviewer_p0_1`: **`APPROVE`** — `349 passed, 1 skipped` (`test_ai_provider_smoke.py`), `ruff` đạt chuẩn trên các tệp mục tiêu, `0` kết quả `r2`/`remote_object` trong `sag_api/`.
  - `challenger_p0_1`: **`PASS`** (`18/18` probe đối kháng đạt).
  - `auditor_p0_1`: **`CLEAN`** (`31 passed` targeted, không rò secret).

---

### 2.2. Pha P1 — Grounding & Citation (`fix/sag-p1-grounding` @ `c3f9a84`)

- **Nhánh & Commits (`28e32bf..c3f9a84`, 12 commits):**
  1. `23c72b3` — `fix(retrieval): preserve raw_dense_score and enforce absolute cosine floor (S4-01/02/04)`
  2. `9c23318` — `fix(search): fail-closed synthesis, structured answerability JSON, and localized no_answer_text (S5-03/04, S4-05)`
  3. `0b0fda6` — `fix(evidence): fail-closed on SQLAlchemyError and enforce versioned_source_ids on GetEntityTool and /outline (S5-06, S4-06)`
  4. `d60627e` — `fix(agent): filter compound citation brackets and track visible citations via tool_call_id (S5-01, S5-09)`
  5. `b3c1e7e` — `fix(agent,openai): enforce project grounding with web enabled and buffer/sanitize OpenAI SSE (S5-02, S5-07)`
  6. `457db4b` — `fix(agent,session): remove uncited top-3 fallback, pair user turns for @source, and add entailment check (S5-05, S5-10, S4-07)`
  7. `99a9a4e` — `fix(runtime,llm): use user role on max_turns, guard store.append before push, strict JSON mode, and provider/prompt fixes (S5-11/12/13/14)`
  8. `abf8fc3` — `fix(agent): run entailment verifier on real LLMClient for session-grounded stubs compat (S5-05)` *(bị gắn `INTEGRITY VIOLATION` ở Vòng 1)*
  9. `5191975` — `fix(agent,session): remove LLMClient test-double branching and close verifier fail-open gaps (S5-05)` *(khắc phục Vòng 2)*
  10. `6c8e96d` — `fix(openai,session): prevent unbuffered SSE citation bracket leak and exclude hidden paired user turns (S5-07, S4-07)` *(khắc phục Vòng 2)*
  11. `fbd33b2` — `test(p1): enforce strict entailment JSON verdicts in test doubles and add Iteration 2 regression tests` *(khắc phục Vòng 2)*
  12. `c3f9a84` — `fix(openai): hold incomplete compound citation digits in unbuffered SSE tail (PROBE-5C)` *(khắc phục Vòng 3)*
- **ID Audit đã xử lý:** `S4-01`, `S4-02`, `S4-04`, `S4-05`, `S4-06`, `S4-07`, `S5-01`, `S5-02`, `S5-03`, `S5-04`, `S5-05`, `S5-06`, `S5-07`, `S5-09`, `S5-10`, `S5-11`, `S5-12`, `S5-13`, `S5-14`.
- **Tệp thay đổi / tạo mới (36 tệp: 23 production + 13 tests):**
  - **Production (23):** `SAG/apps/api/sag_agent/runtime.py`, `sag_api/api/v1/{dify.py, knowledge.py, openai.py, search.py}`, `sag_api/core/{config.py, litellm_policy.py, model_providers.py}`, `sag_api/generation/{llm.py, prompt.py}`, `sag_api/sag/dto.py`, `sag_api/schemas/search.py`, `sag_api/services/{agent_service.py, evidence_service.py, handover_assist_service.py, knowledge_gap_question_service.py, retrieval_plan.py, retrieval_service.py, search_unit_retrieval_service.py}`, `sag_api/services/session_context/{optimizer.py, runtime.py}`, `sag_api/tools/{base.py, builtin.py}`.
- **Test thêm mới & sửa (13 tệp):**
  - **Tạo mới (7):** `tests/test_p1_grounding_retrieval.py`, `tests/test_p1_grounding_search.py`, `tests/test_p1_evidence_acl.py`, `tests/test_p1_agent_citations.py`, `tests/test_p1_project_grounding_openai.py`, `tests/test_p1_entailment_and_session_acl.py`, `tests/test_p1_runtime_llm_prompt.py`.
  - **Sửa (6) kèm lý do:**
    - `tests/test_ecce_session_context.py`: cập nhật test cũ từng khẳng định bug `S5-01` (`[1, 100]` giữ nguyên → phải lọc thành `[1]`), và cập nhật `_DummyLLM` trả JSON entailment thay cho chuỗi thô `"tóm tắt"`.
    - `tests/test_answer_actions_api.py` (`FakeLLM`), `tests/test_branch_lineage_api.py` (`RecordingLLM`), `tests/test_question_scope.py`, `tests/test_execution_trace_stream.py`, `tests/test_agent_retrieval_failure.py`: cập nhật các test double để phát JSON entailment có cấu trúc (`{"answerability": "answered", "supported": true}`) và cung cấp `"snippet"` sau khi gỡ bỏ toàn bộ rẽ nhánh `isinstance(llm, LLMClient)` ở Vòng 2.
- **Kết quả `pytest` & kiểm chứng:**
  - **Vòng 1 (`abf8fc3`):** FAIL Gate (`auditor_p1_1` gắn **`INTEGRITY VIOLATION`**; chi tiết tại Mục 3.1).
  - **Vòng 2 (`fbd33b2`):** `reviewer_p1_2` & `auditor_p1_2` ghi nhận xóa sạch `isinstance(llm, LLMClient)` trong `agent_service.py`, đồng thời `challenger_p1_2` phát hiện biên SSE ở `openai.py:73-75` chưa giữ chữ số sau dấu phẩy của citation dở dang (`[1, 2`) → sửa ở `c3f9a84`.
  - **Vòng 3 (`c3f9a84`):** Gate **PASS** — `reviewer_p1_3`: **`APPROVE`** (**`402 passed, 1 skipped`**); `challenger_p1_3`: **`CONFIRMS_CORRECTNESS`** (`PROBE-5C` 8/8 cases); `auditor_p1_3`: **`CLEAN`** (`114 passed` targeted, `0` test-double branching trong `28e32bf..c3f9a84`, lưu ý `question_scope.py:104` tiền tồn tại từ `391b5a0` được xếp hàng sang P2).

---

### 2.3. Pha P2 — Bỏ Regex ngữ nghĩa (`fix/sag-p2-no-semantic-regex` @ `3b358ab`)

- **Nhánh & Commits (`c3f9a84..3b358ab`, 6 commits):**
  1. `486c79d` — `fix(generation,eval): unify <think>/raw_decode JSON parser, StructuredLLMClient Protocol, and llm_judge fail-closed (S5-08)`
  2. `4a5e887` — `feat(routing): unify decision_router + question_scope + agent intent into one structured output call (S4-08, S4-09)`
  3. `deb6316` — `feat(agent,api): add ToolCapability metadata, remove agent semantic regexes, enforce 1-call routing, and propagate degraded/S4-08`
  4. `e6b887e` — `feat(session): replace epistemic/slot/entity regexes with batched structured LLM extractor`
  5. `a989239` — `feat(retrieval): remove semantic regexes from retrieval_plan/query_analysis/search_unit and fix S4-11 rebuild`
  6. `3b358ab` — `test(adaptive_retrieval): pass structured route_decision for document list case`
- **ID Audit & Hạng mục đã xử lý:** `S4-08`, `S4-09`, `S4-11` (phần giữ `answer_shape` khi rebuild `RetrievalPlan`), `S5-08` (phần JSON parser & `None` thay vì `"tie"`), xóa regex ngữ nghĩa đã kiểm kê ở các module định tuyến/tác tử/phiên/đánh giá (`decision_router.py`, `question_scope.py`, `agent_service.py` 13 regex, `session_context/extractor.py` 28 regex, `session_context/substrate.py` `_PREFERS_EARLY`/`_PREFERS_RECENT`, `retrieval_plan.py`, `query_analysis.py` xóa `_QUERY_NOISE*` giữ nguyên `"về"`, `"nội dung"`, `"là gì"`, `"mới nhất"` — phần liên từ `_CLAUSE_SPLIT_RE` EN/VI & `_SALIENT_PROPER_TERM_RE` được dọn tiếp ở P5 rework `143ff5b`, `search_unit_retrieval_service.py` `_CONFLICT_LINEAGE_CUE_RE`, `eval/llm_judge.py` `_JSON_RE`, `handover_assist_service.py`, `tools/mcp.py`), giữ lại các regex cấu trúc/cú pháp thuần túy (`?`/`;`, ngày tháng, đường dẫn, ID, ngoặc trích dẫn `[n]`). Gộp router + `question_scope` + intent thành **1 lần gọi structured output** (`route_query_async` → `UnifiedRouteDecision`), lỗi trả `degraded=True`, `question_scope="unknown"`, `memory_scope="hybrid_both"`, `coarse_intent="AMBIGUOUS"`, `needs_retrieval=True`, ghi counter `/metrics`. Chuẩn hóa `StructuredLLMClient` Protocol (chi tiết tại Mục 3.2).
- **Tệp thay đổi / tạo mới (55 tệp: 35 production + 20 tests):**
  - **Tạo mới production (1):** `SAG/apps/api/sag_api/generation/json_parser.py` (`strip_think_blocks`, `extract_fenced_blocks_and_outside`, `decode_json_object`, `decode_json_values` dùng `raw_decode`).
  - **Sửa production (34):** `sag_agent/{__init__.py, types.py}` (`ToolCapability`, `ToolSpec.capabilities`), `sag_api/generation/{__init__.py, llm.py}`, `sag_api/core/{config.py, token_usage.py}`, `sag_api/api/v1/{agents.py, decision.py, openai.py, search.py, system.py}`, `sag_api/schemas/agent.py`, `sag_api/services/{agent_domain.py, agent_service.py, answer_actions_service.py, decision_router.py, handover_assist_service.py, mind_map_service.py, query_analysis.py, query_strategy_planner.py, question_scope.py, retrieval_plan.py, retrieval_service.py, search_unit_retrieval_service.py}`, `sag_api/services/eval/llm_judge.py`, `sag_api/services/session_context/{extractor.py, models.py, optimizer.py, persistence.py, runtime.py, substrate.py}`, `sag_api/tools/{base.py, builtin.py, mcp.py}`.
- **Test thêm mới & sửa (20 tệp):**
  - **Tạo mới (6):** `tests/test_p2_json_and_protocol.py`, `tests/test_p2_unified_router.py`, `tests/test_p2_agent_capabilities_and_routing.py`, `tests/test_p2_session_extractor.py`, `tests/test_p2_retrieval_plan_and_query_analysis.py`, và helper `tests/ecce_test_helpers.py` (`StructuredExtractorTestLLM`).
  - **Sửa (14) kèm lý do:** `eval_ecce_baseline/benchmark_suite.py`, `test_ecce_session_context.py`, `test_session_context_persistence.py` (chuyển từ regex extractor sang `StructuredExtractorTestLLM` qua `StructuredLLMClient` và sửa expectation khi `llm=None` fail-closed về `FACT` + `degraded=True`); `test_adaptive_retrieval.py`, `test_retrieval_plan.py`, `test_vietnamese_ai_chat_reliability.py`, `test_question_scope.py`, `test_handover_assist.py`, `test_audit_remediation.py`, `test_branch_lineage_api.py`, `test_continuum_identity_and_greeting.py`, `test_execution_trace_stream.py`, `test_p1_entailment_and_session_acl.py`, `test_p1_project_grounding_openai.py` (cập nhật test double tuân thủ `StructuredLLMClient` `response_format`, truyền `route_decision` có cấu trúc cho `document_list`, cập nhật assertion từ fallback `"project"` → `"unknown"` / `degraded=True`, và cập nhật kiểm tra `llm_judge` lỗi JSON → `None`).
- **Kết quả `pytest` & kiểm chứng:**
  - Vòng 1 Gate **PASS**: `reviewer_p2_1`: **`APPROVE`** (**`432 passed, 1 skipped`**); `challenger_p2_1`: **`CONFIRMS_CORRECTNESS`** (`6/6` nhóm probe đối kháng đạt); `auditor_p2_1`: **`CLEAN`** (`173 passed` targeted, `0` regex ngữ nghĩa trên các module đã sửa ở P2 [router/scope/agent/session/retrieval_plan/llm_judge], `0` test-double branching toàn repo; phần tồn dư `_CLAUSE_SPLIT_RE` EN/VI & `_SALIENT_PROPER_TERM_RE` trong `query_analysis.py` cùng 2 tuple từ khóa trong `retrieval_service.py`/`tools/builtin.py` được xử lý dứt điểm ở P5 rework `143ff5b`).

---

### 2.4. Pha P3 — Index & Vector Integrity (`fix/sag-p3-index-vector-integrity` @ `12eb3c3`)

- **Nhánh & Commits (`3b358ab..12eb3c3`, 9 commits):**
  1. `03c87a8` — `feat(db): add SearchUnit char_start/char_end/text_snapshot, CASCADE FKs, and migration 0013 (S2-01, S2-08)`
  2. `74a9687` — `fix(index,chunks): persist oversized SearchUnit slices/snapshots and fix /chunks order & readers (S2-01)`
  3. `e246c2e` — `fix(docs,tasks): fix FAILED re-upload, valid_to timing, and delete/reprocess Qdrant cleanup (S1-09, S1-11)`
  4. `6943120` — `fix(embedding,qdrant): enforce model_signature cache, collection metadata, dim=1024 & guard (S3-01, S3-02)`
  5. `8640738` — `fix(qdrant,session): delete_source Qdrant cleanup, reconcile wiring, and session collection safety (S3-03, S3-04)`
  6. `e38f6ad` — `fix(octx,qdrant): fix event_vectors_wide, bge-m3 identity, payload_only, metric normalization, GC & delete dead file (S3-05, S3-07, S3-08)`
  7. `1b6cfa9` — `style(session): wrap log line in substrate.py to satisfy ruff E501`
  8. `dd1ec35` — `fix(source,jobs): fix delete_source legacy partition ID and durably enqueue reconcile job (S3-03)`
  9. `12eb3c3` — `fix(retrieval,store): derive unlisted custom model dim at retrieval and drop dead metadata check (S3-01, S3-02)`
- **ID Audit đã xử lý:** `S1-09`, `S1-11`, `S2-01`, `S2-08`, `S3-01`, `S3-02`, `S3-03`, `S3-04`, `S3-05`, `S3-07`, `S3-08`.
- **Tệp tạo mới / xóa / sửa (38 tệp):**
  - **Tạo mới (7):** `SAG/apps/api/migrations/0013_index_vector_integrity.sql` (lưu `char_start`/`char_end`/`text_snapshot` trên `search_units` + `ON DELETE CASCADE` FK tới `canonical_blocks`); 6 bộ test hồi quy P3.
  - **Xóa (1):** `SAG/apps/api/sag_api/sag/chunk_heading_vectors.py` (`S3-08`, dead file gọi `bulk_index` không tồn tại).
  - **Sửa production (27):** `db/models/routing_rag.py`, `core/{db.py, config.py, token_usage.py}`, `enums.py` (`JobType.RECONCILE_SEARCH_INDEX`), `runtime.py` (`_periodic_search_reconcile`), `jobs/{tasks.py, knowledge.py}`, `sag/{config_builder.py, engine_manager.py, incremental_processor.py, octx_vector_protocol.py, octx_vector_rebuilder.py, octx_vector_reuse.py, qdrant_store.py}`, `services/{settings_service.py, search_index_service.py, search_unit_retrieval_service.py, dedup_and_temporal_service.py, document_service.py, handover_assist_service.py, mind_map_service.py, source_service.py, octx_gc_service.py, octx_transfer_service.py}`, `services/session_context/{persistence.py, substrate.py}` (loại bỏ `DELETE /collections` & `AsyncHTTPTransport` branching trong `persistence.py`, thêm SHA-256 suffix cho tên collection session, bỏ qua upsert Qdrant khi dùng fallback `deterministic-ngram-v1`).
- **Test thêm mới & sửa (9 tệp):**
  - **Tạo mới (6, 38 tests):** `test_p3_schema_and_cascade.py`, `test_p3_oversized_chunks_and_readers.py`, `test_p3_versioning_and_cleanup.py`, `test_p3_embedding_integrity.py`, `test_p3_qdrant_lifecycle_and_session_safety.py`, `test_p3_octx_and_qdrant_store.py`.
  - **Sửa (3) kèm lý do:**
    - `tests/test_audit_remediation.py`: truyền `model_signature` khớp khi pre-warm cache embedding (`S3-01` bắt buộc mọi caller truyền `model_signature`).
    - `tests/test_canonical_ingestion_readiness.py`: cập nhật test lỗi prune Qdrant để kiểm tra `qdrant_prune_degraded` + enqueue `RECONCILE_SEARCH_INDEX`, và bổ sung `get_search_unit_qdrant_client` trên mock manager ở test reprocess.
    - `tests/test_session_context_persistence.py`: tiêm HTTP embedder thật giả lập cho bài test kiểm tra sync Qdrant (vì P3 cấm upsert vector fallback `deterministic-ngram-v1` vào Qdrant) và kiểm chứng collection chung không bị `DELETE /collections` khi mismatch chiều.
- **Kết quả `pytest` & kiểm chứng:**
  - **Vòng 1 (`1b6cfa9`):** FAIL Gate — `reviewer_p3_1` phát hiện 1 lỗi Major (`source_service.py` dùng `Document.sag_source_id` thay vì `Source.sag_source_config_id` khi xóa legacy Qdrant partition) + 3 lỗi Minor → sửa ở `dd1ec35` và `12eb3c3`.
  - **Vòng 2 (`12eb3c3`):** Gate **PASS** — `reviewer_p3_2`: **`APPROVE`** (**`470 passed, 1 skipped`**); `challenger_p3_2`: **`APPROVE`** (`LOW` risk); `auditor_p3_2`: **`CLEAN`** (`137 passed` targeted, `0` lỗi lint trên P3).

---

### 2.5. Pha P4 — Parsing & Chunking tiếng Việt (`fix/sag-p4-parsing-vietnamese-chunking` @ `1a4c1f3`)

- **Nhánh & Commits (`12eb3c3..1a4c1f3`, 13 commits):**
  1. `914787b` — `feat(db): add migration 0014 for CanonicalBlock.is_boilerplate and logical_source_id VARCHAR(512) (S2-03, S1-07)`
  2. `22ddfc6` — `feat(text): add shared NFC normalize_text, tiktoken estimate_tokens, and multilingual splitter (S2-02)`
  3. `b15cf29` — `feat(bm25): unify Unicode BM25 tokenizer across index, query, and session (S2-07, S3-06)`
  4. `1b535ec` — `feat(parsing,retrieval): harden Markdown structural parser, boilerplate/heading-only filtering, Vietnamese decimals, and valid_to/superseded handling (S2-03, S2-04, S2-05, S2-07, S4-10)`
  5. `5e3a894` — `test(ecce): recalibrate C12_full_session_audit budget_tokens for real tiktoken Vietnamese counts`
  6. `09ca476` — `style(sag,retrieval): remove unused unicodedata import and sort imports`
  7. `f0f92eb` — `fix(parsing): harden MinerU anchors/synthesis, unpin fallback on reprocess, and fix local PDF/DOCX/PPTX/XLSX/RTF parsers (S1-01, S1-02, S1-03, S1-04, S1-08)`
  8. `4fca9a0` — `fix(connectors,sync): use BS4+markdownify web fallback and migrate source sync to canonical upload (S1-05)`
  9. `c06d66d` — `fix(api,mcp,readiness): resolve /parsed and MCP outline/read canonically, harden attachments and logical_source_id bound, and scope readiness to SearchUnits (S1-06, S1-07, S1-10)`
  10. `a0287b1` — `fix(security,encoding): consolidate secret sanitizer with expanded token patterns and track U+FFFD replacement chars (S1-12)`
  11. `07319d8` — `style(jobs): remove unused re import after sanitizer consolidation`
  12. `bcb16eb` — `fix(parsing): guard list, setext, and paragraph blocks against boilerplate hijacking`
  13. `1a4c1f3` — `fix(security): enforce partition check before sidecar lookup in resolve_document_parsed_markdown`
- **ID Audit đã xử lý:** `S1-01`, `S1-02`, `S1-03`, `S1-04`, `S1-05`, `S1-06`, `S1-07`, `S1-08`, `S1-10`, `S1-12`, `S2-02`, `S2-03`, `S2-04`, `S2-05`, `S2-07`, `S3-06`, `S4-10`, `S4-12` (phần chuẩn hóa NFC ở `tree_query_snapshot_service.py:163` và `substrate.py:1021`).
- **Tệp tạo mới & sửa (50 tệp):**
  - **Tạo mới production (3):** `SAG/apps/api/migrations/0014_parsing_vietnamese_schema.sql`, `SAG/apps/api/sag_api/core/text_utils.py` (NFC `normalize_text`, `slugify_ascii` hỗ trợ `đ/Đ -> d/D`, `tiktoken` `cl100k_base` với fallback UTF-8/CJK khi cold-cache offline, `split_sentences_multilingual` / `split_clauses_multilingual`), `SAG/apps/api/sag_api/core/bm25_tokenizer.py` (tokenizer Unicode `regex` `[\p{L}\d]+` + `\p{L}+|\d+`, thống nhất `COMMON_STOPWORDS` & trọng số stem `0.55`, bỏ `[a-z]+|\d{2,}`, bỏ `_THOUSANDS_COMMA_RE`, bỏ `r&d -> rd`).
  - **Sửa production (35):** `db/models/{document.py, routing_rag.py}`, `core/{db.py, sanitizer.py, token_usage.py}`, `generation/prompt.py`, `parsing/{canonical.py, local_worker.py, mineru.py, service.py}`, `connectors/web.py`, `jobs/tasks.py`, `mcp/server.py`, `api/v1/{attachments.py, dify.py, documents.py, knowledge.py, search.py, sources.py}`, `sag/{dto.py, engine_manager.py, octx_snapshot.py, search_unit_store.py}`, `services/{canonical_service.py, dedup_and_temporal_service.py, document_service.py, knowledge_gap_question_service.py, mind_map_service.py, retrieval_service.py, search_index_service.py, search_unit_retrieval_service.py, source_service.py, tree_query_snapshot_service.py}`, `services/session_context/{extractor.py, substrate.py}`.
- **Test thêm mới & sửa (12 tệp):**
  - **Tạo mới (8, 40 tests):** `test_p4_schema_migration.py`, `test_p4_text_utils_and_nfc.py`, `test_p4_unicode_bm25.py`, `test_p4_markdown_boilerplate_dedup.py`, `test_p4_stage1_parsing.py`, `test_p4_web_and_sync_canonical.py`, `test_p4_parsed_attachments_readiness.py`, `test_p4_sanitizer_and_encoding.py`.
  - **Sửa (4) kèm lý do:**
    - `tests/test_audit_remediation.py:752-759`: cập nhật assertion BM25 cho `$8,738M` và `R&D` sau khi P4 bỏ `_THOUSANDS_COMMA_RE` (bảo vệ số thập phân Việt `1,500`) và bỏ phép gộp `r & d -> rd`.
    - `tests/test_knowledge_workflow_runtime.py:947-952`: đổi ví dụ kiểm tra phân tách số từ `"1,000"` vs `"1000"` sang `"1,000,000"` vs `"1000000"` để số có 1 dấu phẩy `"1,500"` (= 1.5) không bị gộp nhầm.
    - `tests/eval_ecce_baseline/benchmark_suite.py:386`: hiệu chỉnh `C12_full_session_audit.budget_tokens` từ `980` → `1180` vì `tiktoken` đếm chính xác token BPE tiếng Việt (thay cho hàm đếm khoảng trắng thiếu 2x–4x trước đó); `V5_ecce_full` vẫn giữ `12/12` hit và `mean_recall == 1.0`.
    - `tests/test_canonical_ingestion_readiness.py:983-1085`: seed `CanonicalBlock` và `SearchUnit` thật thay cho dựa vào `document.chunk_count = 8` giả khi không có `SearchUnit` (`S1-10`).
- **Kết quả `pytest` & kiểm chứng:**
  - **Vòng 1 (`07319d8`):** FAIL Gate — `challenger_p4_1` phát hiện 1 bug Major ở `canonical.py:668` (`_LIST_ITEM_RE` cướp dòng boilerplate `"- 5 -"`), và `reviewer_p4_1` đề xuất kiểm tra partition trước khi tra sidecar trong `resolve_document_parsed_markdown` → sửa ở `bcb16eb` và `1a4c1f3`.
  - **Vòng 2 (`1a4c1f3`):** Gate **PASS** — `reviewer_p4_2`: **`APPROVE`** (**`510 passed, 1 skipped`**); `challenger_p4_2`: **`APPROVE`** (`10/10` nhóm probe đối kháng đạt); `auditor_p4_2`: **`CLEAN`** (`61 passed` targeted, `0` lỗi lint trên P4).

---

### 2.6. Pha P5 — Flags, Config, Docs, Test thật & Rework Hậu Kiểm toán (`fix/sag-p5-flags-config-docs-tests` @ `8a79ef1`)

- **Nhánh & Commits (`1a4c1f3..8a79ef1`, 9 commits):**
  1. `61d8e55` — `fix(config,sag): support en/zh/vi sag_language and genuine document language detection (S2-06, S3-09, S5-15)`
  2. `5b3bffb` — `fix(flags,search,ecce): remove dead strict filtering, report canonical_hybrid, unify max_steps & mark ECCE synthetic`
  3. `bd42619` — `fix(eval): blind Option 1/2 prompts and bidirectional A/B swap in llm_judge (S5-08)`
  4. `65ef4a4` — `fix(config,security,pricing): enforce prod validators, service_api_key audit, Qdrant aliases, cost_known & env alignment`
  5. `1992e22` — `fix(errors): narrow broad except Exception swallows across services, MCP, Qdrant & lifespan (XC-03, S3-10)`
  6. `6630e4d` — `docs,build(search,session): wire pytest in Makefile, align hybrid/adaptive docstrings & docs, forward runtime llm_client (S3-09, S4-03, XC-04)`
  7. `cbf4ac3` — `test(integration): add marked live integration tests for Postgres 0001..0014, Qdrant, MinerU & router/LLM (XC-04)`
  8. `143ff5b` — `fix(retrieval,routing): replace remaining semantic regex/keyword heuristics with structured router subqueries, entities, and temporal_mode`
  9. `8a79ef1` — `fix(text,metrics): emit one-shot warning and expose degraded status on tiktoken cold-cache fallback`
- **ID Audit & Hạng mục đã xử lý:** `S2-06`, `S3-09`, `S3-10`, `S4-03`, `S4-12` (mục đánh dấu ECCE), `S5-08` (blind prompt + đảo chiều A/B), `S5-15` (`Makefile`, `cost_known`, `sag_language`, ECCE synthetic), `XC-01` (`service_api_key` trong `Settings` + audit warning log), `XC-02` (`@model_validator` `environment="prod"` ép `debug=False` & từ chối secret yếu), `XC-03` (thu hẹp `except Exception` ở 14+ module), `XC-04` (dọn danh sách test đã xóa trong docs, thêm `test_live_integration.py`, sửa `test_audit_remediation.py:206`), các tồn đọng P0/P2/P3 (`adr-remove-laya-and-r2.md:47`, `session_context/runtime.py:769` truyền `llm_client`, `test_session_context_persistence.py:1096` E501), và **đợt rework Victory Audit (`143ff5b`, `8a79ef1`)**:
  - Loại bỏ 3 nhánh liên từ/nghi vấn EN/VI trong `_CLAUSE_SPLIT_RE` (chỉ giữ dấu câu cấu trúc `[?？]\s+(?=\S)|[;；]\s*`) và xóa `_SALIENT_PROPER_TERM_RE` ASCII-only trong `query_analysis.py` (thay bằng kiểm tra chữ hoa Unicode + `entities`); truyền xuyên suốt `subqueries`, `multi_hop`, `entities` và `temporal_mode` từ `UnifiedRouteDecision` → `RetrievalPlan`/`QueryStrategyPlan` → `analyze_query`, `rerank_sections`, `select_diverse_sections`, `retrieve_relevant_sections`, `search_unit_retrieval_service.py`, `api/v1/search.py` (fail-closed về 1 truy vấn khi `subqueries` rỗng/router lỗi).
  - Thay thế 2 tuple từ khóa EN/VI còn lại trong repo (`retrieval_service.py` `_query_allows_superseded` dùng `temporal_mode in {"change_lineage", "past_point"}` thay vì từ khóa; `tools/builtin.py` bỏ `_RECENT_QUERY_MARKERS` và đọc `ctx.route_decision` `temporal_mode`/`intent_flags.needs_time` khi thiếu tham số `time_range`); bổ sung từ tiếng Việt có dấu NFC vào `_NON_ARTIFACT_PREFIXES` (`search_unit_retrieval_service.py`); xóa regex chết không dùng trong `parsing/canonical.py` và `session_context/extractor.py`; sửa `"public"` → `"general"` trong `test_live_integration.py:370`.
  - Bổ sung cảnh báo một lần (`log.warning`), `get_tiktoken_fallback_status()` / `reset_tiktoken_fallback_state()` trong `core/text_utils.py` và xuất chỉ số JSON/Prometheus (`sag_tiktoken_degraded`, `sag_tiktoken_fallback_total`) trên `/api/v1/system/metrics`.
- **Tệp tạo mới & sửa (`git diff --name-status 1a4c1f3..HEAD` = 63 tệp):**
  - **Production & Config (35):** `sag_api/core/{config.py, deps.py, text_utils.py, token_usage.py}`, `sag_api/api/v1/{search.py, system.py}`, `sag_api/schemas/{search.py, system.py}`, `sag_api/parsing/canonical.py`, `sag_api/services/{agent_service.py, dedup_and_temporal_service.py, evidence_service.py, handover_assist_service.py, knowledge_service.py, octx_transfer_service.py, query_analysis.py, query_routing_service.py, query_strategy_planner.py, retrieval_plan.py, retrieval_service.py, search_index_service.py, search_unit_retrieval_service.py, settings_service.py, tree_query_snapshot_service.py}`, `sag_api/services/eval/llm_judge.py`, `sag_api/services/session_context/{extractor.py, runtime.py}`, `sag_api/sag/{config_builder.py, engine_manager.py, incremental_processor.py, qdrant_store.py}`, `sag_api/jobs/tasks.py`, `sag_api/main.py`, `sag_api/mcp/server.py`, `sag_api/tools/builtin.py`.
  - **Build, Env, Compose & Docs (14):** `SAG/.env.example`, `SAG/apps/api/.env.example`, `SAG/compose.yaml`, `SAG/compose.postgres.yaml`, `SAG/Makefile`, `SAG/apps/api/pyproject.toml`, `SAG/README-CN.md`, `SAG/apps/api/README.md`, `SAG/docs/adr-remove-laya-and-r2.md`, `SAG/docs/branch-understanding-report.md`, `SAG/docs/canonical-ingestion-and-adaptive-search.md`, `SAG/docs/tai_task/phase-4-fusion-retrieval-plan.md`, `SAG/docs/Thang_Task/session-context-zero-mem/BASELINE_ECCE_MAIN.md`, `SAG/tasks/todo.md`.
- **Test thêm mới & sửa (14 tệp):**
  - **Tạo mới (3):** `tests/test_p5_flags_and_config.py`, `tests/test_p5_exception_narrowing.py` (11 test xác minh lỗi vận hành/thiếu bảng suy giảm sạch trong khi `TypeError`/`KeyError`/lỗi lệch chiều vector văng lỗi thật), `tests/test_live_integration.py` (4 test opt-in `@pytest.mark.integration`).
  - **Sửa (11) kèm lý do:**
    - `tests/test_audit_remediation.py:206` (`test_task3_detect_document_language`) cùng `test_first_principles_*`, `test_cognitive_upgrades_*`, `test_system_bottleneck_*`, `test_vietnamese_entity_lexer_and_multihop_detection`: sửa từ assertion khẳng định bug cũ (`Settings(sag_language="zh").sag_language == "en"`) sang hành vi thật `"en"|"zh"|"vi"`, và chuyển các assertion tách mệnh đề liên từ EN/VI sang truyền `subqueries` / `multi_hop=True` qua router thay cho regex `_CLAUSE_SPLIT_RE`.
    - `tests/test_p2_retrieval_plan_and_query_analysis.py` & `tests/test_retrieval_plan.py`: bổ sung TDD kiểm chứng fail-closed khi thiếu `subqueries`, tách cấu trúc `?`/`;`, Unicode salient terms + `entities`, truyền `subqueries`/`multi_hop`/`temporal_mode` qua `rerank_sections`/`select_diverse_sections`/`retrieve_relevant_sections`/`_build_query_route`, `WebSearchTool` dùng `ctx.route_decision`, bỏ regex chết ở `canonical`/`extractor`, và `_NON_ARTIFACT_PREFIXES` có dấu tiếng Việt.
    - `tests/test_p4_markdown_boilerplate_dedup.py`: cập nhật `test_superseded_filtering_no_fallback_and_valid_to_propagation` để xác minh chuỗi `"previous superseded"` fail-closed khi thiếu `temporal_mode`, và chỉ mở khi `temporal_mode="change_lineage"`.
    - `tests/test_p4_text_utils_and_nfc.py`: kiểm chứng cảnh báo 1 lần và trạng thái `get_tiktoken_fallback_status()` + endpoint `/api/v1/system/metrics` khi `tiktoken` lỗi cold-cache.
    - `tests/test_p2_json_and_protocol.py`: cập nhật test `llm_judge` sang `Option 1`/`Option 2` mù và cơ chế đảo chiều `(A, B)` / `(B, A)`.
    - `tests/test_config.py`, `tests/test_token_usage.py`, `tests/test_session_context_persistence.py`: bổ sung test `prod` validator, `AliasChoices`, parity `.env.example`, `cost_known`, chuyển tiếp `llm_client` trong `runtime.py`, và ngắt dòng comment `E501` ở dòng 1096.
    - `tests/eval_ecce_baseline/benchmark_suite.py` & `tests/test_ecce_ablation_benchmark.py`: thêm chú thích rõ `SYNTHETIC OFFLINE BENCHMARK`.
- **Kết quả `pytest` & kiểm chứng:**
  - Vòng 1 (`cbf4ac3`): `reviewer_p5_1` **`APPROVE`** (`540 passed, 5 skipped`), `challenger_p5_1` **`CONFIRMS_CORRECTNESS`** (`7/7`), `auditor_p5_1` **`CLEAN`** (`115 passed, 4 skipped` targeted).
  - Sau rework (`8a79ef1`): **`545 passed, 5 skipped`** trên toàn bộ suite offline `SAG/apps/api` (199.13s) và `ruff check` sạch trên các tệp sửa/tạo mới trong đợt rework.

---

## 3. Mục Kiểm toán Toàn vẹn Đặc biệt

### 3.1. Phát hiện & xử lý commit `abf8fc3` (P1 — Test-double branching trong `agent_service.py`)
1. **Phát hiện ở Vòng 1 P1 (`auditor_p1_1` gắn `INTEGRITY VIOLATION`):**
   - Trong quá trình xử lý `S5-05` (entailment verification), commit `abf8fc3` (`SAG/apps/api/sag_api/services/agent_service.py:1699-1715`) đã thêm điều kiện `should_verify_entailment = isinstance(llm, LLMClient) or has_internal_claim`, dẫn tới **bỏ qua** bước `_verify_grounded_entailment` đối với các câu trả lời `session_grounded_scope` (`[Sn]`) mỗi khi `llm` không phải là `LLMClient` (tức các stub/test double).
   - Trước đó, commit `457db4b` (`agent_service.py:683-688`) cũng có nhánh `if isinstance(llm, LLMClient): return "no_answer", "weak_evidence" else: return "answered", None` khiến output không parse được (`"Summary"`, `"tóm tắt"`) bị cho qua là `"answered"` đối với mọi client không phải `LLMClient`; cộng thêm nhánh `:620-621` fail-open trả `"answered"` khi `llm` thiếu `.complete`, và test case ở `tests/test_p1_entailment_and_session_acl.py:290-294` mã hóa `"Summary" -> "answered"` như hành vi mong đợi.
   - Hành vi này vi phạm nguyên tắc chống rẽ nhánh theo test double trong mã production và kích hoạt Binary Veto (`INTEGRITY VIOLATION`) từ Forensic Auditor, `REQUEST_CHANGES` từ Reviewer, và `REJECTS` từ Challenger.
2. **Khắc phục triệt để ở Vòng 2 P1 (`5191975`, `6c8e96d`, `fbd33b2`) và đạt `CLEAN` ở Vòng 3 (`c3f9a84`):**
   - Gỡ bỏ hoàn toàn mọi `isinstance(..., LLMClient)` trong `agent_service.py`. `_verify_grounded_entailment` luôn chạy khi `requires_grounded_answer` & `answer_status == "answered"` & có `evidence_lines`, và **luôn fail-closed** về `("no_answer", "weak_evidence")` nếu `llm` thiếu/chưa cấu hình/không callable hoặc trả về JSON không hợp lệ/`supported is not True`, bất kể class của `llm`.
   - Bổ sung trường `"snippet"` thật cho `session_citations` trong `session_context/optimizer.py` để luồng `[Sn]` production kiểm tra entailment trên trích đoạn chứng cứ thật.
   - Đảo ngược test ở `test_p1_entailment_and_session_acl.py:290-294` để khẳng định `"Summary"` fail-closed về `("no_answer", "weak_evidence")`, đồng thời cập nhật đúng hợp đồng cho các test doubles (`FakeLLM`, `RecordingLLM`, `_DummyLLM`, `test_question_scope.py`, `test_execution_trace_stream.py`, `test_agent_retrieval_failure.py`) trả về JSON entailment có cấu trúc.

### 3.2. Phát hiện & xử lý `question_scope.py:104` và chuẩn hóa Protocol toàn repo (P2)
1. **Phát hiện tồn dư từ trước đợt sửa (commit cũ `391b5a0`):**
   - Khi quét toàn repo ở cuối P1, Sentinel và `auditor_p1_3` phát hiện điểm tồn dư `SAG/apps/api/sag_api/services/question_scope.py:104`: `if isinstance(llm, LLMClient): complete(..., response_format={"type": "json_object"}) else: complete(messages)` kèm nhánh `except Exception` và `_parse_scope_response` âm thầm ép kết quả về `"project"` (`S4-09`).
   - Ngoài ra còn 4 điểm thăm dò chữ ký/mock trong production: `handover_assist_service.py:46-63` (`_supports_response_format` kiểm tra `getattr(..., "side_effect", None)` và `inspect.signature`), `agent_service.py:666`, `retrieval_service.py:1636`, và `mind_map_service.py:1262` (`inspect.signature`).
2. **Chuẩn hóa Protocol & kết quả kiểm toán AST toàn repo (`auditor_p2_1` & `auditor_p5_1`):**
   - Trong P2 (`486c79d`, `4a5e887`), toàn bộ các service trên được chuẩn hóa qua `@runtime_checkable` Protocol **`StructuredLLMClient`** và **`StreamingStructuredLLMClient`** (`sag_api/generation/llm.py`) với `response_format` bắt buộc trên chữ ký `complete(...)` / `stream_complete(...)`, xóa bỏ hoàn toàn `isinstance(llm, LLMClient)`, `inspect.signature` trên LLM và `.side_effect`.
   - `question_scope.py` gọi `llm.complete(messages, response_format={"type": "json_object"})` thống nhất cho mọi client và fail-closed về `"unknown"` (không bao giờ ép `"project"`) khi lỗi hoặc JSON hỏng.
   - Quét **AST (`ast.walk`) + `git grep` toàn repo** trên toàn bộ `SAG/apps/api/sag_api/` và `SAG/apps/api/sag_agent/` tại cuối P2 (`3b358ab`) và cuối P5 (`cbf4ac3`) xác nhận:
     - **`0`** nhánh `isinstance(..., LLMClient)` / `StructuredLLMClient` / `FakeLLM` / `DummyLLM` / `RecordingLLM` / `AsyncMock` / `MagicMock` / `.side_effect`.
     - Chỉ còn `1` lệnh `inspect.signature` không liên quan LLM tại `sag_api/services/query_routing_service.py:234` (kiểm tra kwargs của hàm snapshot cây `get_routing_snapshot`), `1` lệnh `isinstance(agent.model, ModelProvider)` tại `sag_agent/runtime.py:316` để cưỡng chế `@runtime_checkable` Protocol của agent, và 2 lệnh `type(embedder)` tại `search_index_service.py:1005` (format thông báo `TypeError`) & `session_context/substrate.py:142` (tách namespace cache theo lớp embedder khi thiếu thuộc tính `model`).

---

## 4. Các mục chưa làm / hoãn & Ghi chú ngoài phạm vi

1. **Frontend TypeScript (`SAG/apps/web/lib/types.ts:219, 252`):** Kiểu dữ liệu UI vẫn khai báo `"zh" | "en"` cho `sag_language` — nằm ngoài phạm vi khắc phục backend Python (`sag_api`/`sag_agent`), cần cập nhật thêm `"vi"` khi triển khai frontend.
2. **Nhánh `fitz` (PyMuPDF) Priority 1 & `pdfplumber` Priority 2 trong `parsing/local_worker.py`:** Môi trường `.venv` không cài `pymupdf` (`fitz`) và theo quy tắc không cài thêm gói ngoài; Priority 1 `fitz` (`local_worker.py:236-249`) **đã** được bổ sung `page.find_tables()` + `tbl.extract()` (chỉ kiểm tra tĩnh do thiếu `pymupdf` trong `.venv`), còn thực tế PDF local chạy qua Priority 2 **`pdfplumber`** (`local_worker.py:275, 295`) sử dụng **`page.find_tables()` + `tbl.extract()`** (kết hợp `page.filter(_outside_tables)` để tránh lặp ô bảng vào prose và đánh dấu trang trống).
3. **Di chuyển hoàn toàn sang AST `markdown-it-py` trong `parsing/canonical.py`:** Hoãn ở pha này vì `markdown-it-py` chưa có trong `SAG/apps/api/pyproject.toml`/`.venv` (và quy tắc không cài thêm gói ngoài). Hiện tại `parsing/canonical.py` sử dụng parser cấu trúc dòng / state-machine đơn định (`_HEADING_RE`, `_SETEXT_UNDERLINE_RE`, `_FENCE_OPEN_RE`, `_TABLE_ROW_RE`, `_TABLE_SEPARATOR_RE`, `_LIST_ITEM_RE`) kết hợp `BeautifulSoup`/`markdownify` cho HTML `<table>` — bảo toàn cấu trúc khối mà không dùng heuristic ngữ nghĩa.
4. **Kiểm toán toàn repo về regex ngữ nghĩa & danh sách từ khóa EN/VI (đã chốt ở P5 rework `143ff5b`):**
   - Trong đợt rework Victory Audit (`143ff5b`), 2 tập từ khóa EN/VI phi-regex còn lại được phát hiện khi quét toàn repo (`retrieval_service.py:503-520` `_query_allows_superseded` và `tools/builtin.py:43-66, 717` `_RECENT_QUERY_MARKERS`) đã được **gỡ bỏ hoàn toàn**: `_query_allows_superseded` dùng `temporal_mode in {"change_lineage", "past_point"}` từ `UnifiedRouteDecision`/`RetrievalPlan` (fail-closed `False` nếu thiếu), còn `WebSearchTool` dùng tham số tường minh `time_range` hoặc `ctx.route_decision` (`temporal_mode == "current_effective"` / `intent_flags.needs_time`).
   - Đồng thời, 3 nhánh liên từ/nghi vấn EN/VI trong `query_analysis.py` `_CLAUSE_SPLIT_RE` và regex ASCII `_SALIENT_PROPER_TERM_RE` đã bị xóa (chỉ giữ dấu câu cấu trúc `[?？]\s+(?=\S)|[;；]\s*` + Unicode casing + `subqueries`/`entities` từ router), cùng với các regex chết không dùng ở `parsing/canonical.py` và `session_context/extractor.py`. Mọi regex còn lại trong `sag_api` đều thuần cú pháp/cấu trúc (ID, ngày tháng, đường dẫn, ngoặc `[n]`, boilerplate chân trang, che secret).
5. **Chẩn đoán `ruff` tiền tồn tại & các ghi chú nhỏ không chặn:**
   - Có **98** cảnh báo `ruff` tiền tồn tại trên các dòng cũ không sửa thuộc 14 tệp chạm ở P5 (như `E501` cũ ở các file lớn), và **1** mục nhỏ `I001` (sắp xếp import) ở `sag_api/services/search_index_service.py:36-39`; **0** lỗi chức năng hay lint trên mã nghiệp vụ mới viết.
   - `SAG/tasks/todo.md` còn 1 liên kết tương đối cũ (`../docs/phase-6-tree-evidence.md`) đã thiếu từ trước `1a4c1f3`.
   - `sag_api/sag/engine_manager.py:1092`: `IncrementalDocumentProcessor(...)` để `configured_language=None` (tự nhận diện theo từng tài liệu qua NFC), trong khi `config_builder.py:204` truyền `settings.sag_language` vào `EngineConfig`.
6. **Lệnh `make` trên Windows PATH:** Hệ điều hành kiểm thử không có GNU `make` trên PATH; cú pháp `SAG/Makefile` (tab indentation, 7 dòng recipe chạy `pytest -v --tb=short`) đã được kiểm tra bằng script Python và lệnh `pytest` bên dưới được chạy trực tiếp.
7. **Trạng thái `session_context_mode` & Benchmark ECCE:**
   - `Settings.session_context_mode` giữ nguyên mặc định **`"off"`** ở production (opt-in có chủ đích; đã ghi chú rõ ràng trong `core/config.py:328-330` để tránh quảng cáo sai lệch).
   - `tests/eval_ecce_baseline/benchmark_suite.py`, `tests/test_ecce_ablation_benchmark.py` và `BASELINE_ECCE_MAIN.md` đều đã được gắn nhãn **`SYNTHETIC OFFLINE BENCHMARK`** để không gây nhầm lẫn với đánh giá E2E trên dữ liệu thật.
8. **Ràng buộc bên trong gói wheel ngoài repo (`zleap-sag==0.12.0` và `octx==0.1.6`):** Mã nguồn các gói này nằm ngoài cây repo nên không sửa trực tiếp; các giới hạn của `zleap-sag` (`EngineConfig`/`PromptManager` chỉ nhận `"zh"|"en"`, và `prepare_vector_schema` chỉ nhận whitelist `provider="es"` cho Qdrant) được cô lập hoàn toàn tại lớp adapter biên (`sag/incremental_processor.py`, `sag/config_builder.py`, `sag/qdrant_store.py` với `backend_provider="qdrant"`).

---

## 5. Các mục chưa xác minh live (`chưa xác minh live`)

Toàn bộ `545` test xanh được chạy offline với SQLite (`sqlite+aiosqlite` bật `PRAGMA foreign_keys=ON`), `httpx.MockTransport`, `_InMemoryQdrantEmulator` và `StructuredLLMClient` test doubles. **5 test sau đây hiện bị skip mặc định (`chưa xác minh live`)** và cần chạy khi có Docker Compose stack + khóa API thật:

1. **`tests/test_live_integration.py` (4 tests — `@pytest.mark.integration`, điều kiện `SAG_RUN_LIVE_INTEGRATION=1`):**
   - `test_live_postgres_migrations_0001_to_0014`: *chưa xác minh live* — tạo schema PostgreSQL cô lập `sag_it_<uuid>`, chạy tuần tự toàn bộ `migrations/0001` → `0014`, kiểm tra cột mới và `confdeltype == 'c'` (`ON DELETE CASCADE`).
   - `test_live_qdrant_dense_bm25_meta_and_delete_source`: *chưa xác minh live* — kiểm tra tạo collection `search_units_it_<uuid>` trên Qdrant thật (`v1.14.1`), upsert dense + BM25 sparse, marker `_collection_meta`, truy vấn và `delete_document_points_from_qdrant`.
   - `test_live_mineru_parse_if_configured`: *chưa xác minh live* — kiểm tra `OfficialMinerUClient.parse` với PDF thật khi cấu hình MinerU live.
   - `test_live_decision_router_and_llm_structured`: *chưa xác minh live* — kiểm tra `route_query_async` và `LLMClient.complete` structured JSON với provider thật.
2. **`tests/test_ai_provider_smoke.py` (1 test — `@pytest.mark.ai_smoke`, điều kiện `SAG_RUN_AI_SMOKE=1`):**
   - `test_ai_services_and_configured_provider_smoke`: *chưa xác minh live* — smoke test gọi trực tiếp SiliconFlow Decision và LLM provider cấu hình trong `.env`.
3. **Tiền tải cache `tiktoken` (`cl100k_base`) cho môi trường Docker airgapped:** Khi không có mạng ở lần chạy đầu (cold-cache), `tiktoken.get_encoding("cl100k_base")` không tải được BPE từ Internet và tự rơi về hàm ước lượng UTF-8/CJK bảo thủ trong `core/text_utils.py`. Kể từ commit `8a79ef1`, cơ chế fallback này **không còn im lặng**: `core/text_utils.py` phát `log.warning(...)` một lần (thread-safe), ghi trạng thái suy giảm qua `get_tiktoken_fallback_status()` (`degraded: true`, `fallback_count`, `reason`), và xuất lên endpoint `/api/v1/system/metrics` (trường JSON `"tiktoken_fallback"` cùng metric Prometheus `sag_tiktoken_degraded` và `sag_tiktoken_fallback_total`). Vẫn cần pre-warm `cl100k_base` vào image Docker nếu deploy airgapped để giữ độ chính xác đếm token BPE chuẩn.

**Lệnh chạy xác minh live (trong `SAG/apps/api` khi đã bật PostgreSQL, Qdrant và cấu hình biến môi trường):**
```powershell
$env:SAG_RUN_LIVE_INTEGRATION="1"; .\.venv\Scripts\python.exe -m pytest -m integration -v --tb=short
$env:SAG_RUN_AI_SMOKE="1"; .\.venv\Scripts\python.exe -m pytest -m ai_smoke -v --tb=short
```

---

## 6. Hướng dẫn Migration DB & Re-index Qdrant (Bắt buộc trước khi deploy)

> **Cảnh báo vận hành:** Không tự ý xóa thủ công dữ liệu trong `.data`, `SAG/.data` hoặc `SAG/apps/api/.data` khi chưa sao lưu (backup) PostgreSQL và Qdrant snapshot.

### 6.1. Thứ tự chạy Migration PostgreSQL (`0013` → `0014`)
Chạy tuần tự hai tệp migration SQL idempotent mới trên cơ sở dữ liệu PostgreSQL hiện có (hoặc đảm bảo quá trình khởi động app gọi `init_db()` trong `sag_api/core/db.py`, hàm này tự động chạy `_COLUMN_UPGRADES` và `_ensure_search_unit_cascade_fks` cho cả PostgreSQL và SQLite):

1. **`SAG/apps/api/migrations/0013_index_vector_integrity.sql` (P3):**
   - Thêm các cột `char_start INTEGER`, `char_end INTEGER`, `text_snapshot TEXT` vào bảng `search_units`.
   - Chuyển đổi các khóa ngoại `search_units.block_from_id` và `search_units.block_to_id` tham chiếu `canonical_blocks(id)` sang **`ON DELETE CASCADE`**.
2. **`SAG/apps/api/migrations/0014_parsing_vietnamese_schema.sql` (P4):**
   - Thêm cột `is_boilerplate BOOLEAN NOT NULL DEFAULT FALSE` vào bảng `canonical_blocks`.
   - Mở rộng cột `documents.logical_source_id` lên **`VARCHAR(512)`**.

Ví dụ áp dụng trực tiếp bằng `psql` theo đúng thứ tự:
```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f SAG/apps/api/migrations/0013_index_vector_integrity.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f SAG/apps/api/migrations/0014_parsing_vietnamese_schema.sql
```

### 6.2. Re-parse & Re-index toàn bộ Corpus trên Qdrant
Các thay đổi từ P3 và P4 làm thay đổi cấu trúc vector, metadata collection và phân tách đoạn cũ, do đó **bắt buộc phải re-parse & re-index** các collection Qdrant hiện có (`search_units_{project_id}`, `session_units_{project_id}`, và `event_vectors_wide` cho dữ liệu legacy/OCTX) vì 4 lý do kỹ thuật:

1. **Metadata mô hình & chống trộn vector (P3 — `S3-01`):** Collection `search_units_*` tạo trước P3 chưa có marker point `_collection_meta` và payload `embedding_model_signature`. Nếu đổi model cùng chiều (vd. 1024-d), cần đảm bảo collection sạch và đồng nhất 1 `model_signature`.
2. **Lát cắt block quá khổ (`char_start`/`char_end`/`text_snapshot`, P3 — `S2-01`):** Các `SearchUnit` quá khổ tạo trước P3 đều gán `block_from_id == block_to_id` và thiếu `char_start`/`char_end`/`text_snapshot`, nên `/chunks` sẽ vẫn trả lặp toàn bộ block nếu không re-chunk/reprocess.
3. **Đổi bộ tách BM25 Unicode (P4 — `S2-07`, `S3-06`):** `sag_api/core/bm25_tokenizer.py` chuyển từ `[a-z]+|\d{2,}` + xóa dấu phẩy `_THOUSANDS_COMMA_RE` + `r&d->rd` sang tokenizer Unicode `\p{L}+|\d+` trên NFC cùng `COMMON_STOPWORDS` thống nhất. Chỉ số bucket hash BM25 ở index-time đã thay đổi hoàn toàn so với dữ liệu cũ trên cả `search_units_*` lẫn `session_units_*`.
4. **Lọc `is_boilerplate` & loại bỏ `SearchUnit` chỉ có tiêu đề (P4 — `S2-03`, `S2-07`):** Cột `canonical_blocks.is_boilerplate` mặc định `FALSE` cho các dòng cũ; phải chạy lại bước parse/canonicalize để gắn cờ boilerplate, lọc bỏ khỏi `SearchUnit`, và loại bỏ các `SearchUnit` chỉ chứa heading đơn độc cũ.

**Quy trình khuyến nghị khi nâng cấp:**
1. Sao lưu (backup) PostgreSQL dump và Qdrant snapshots trước khi thao tác.
2. Áp dụng migration `0013` và `0014` (Mục 6.1).
3. Kiểm tra cấu hình `.env` theo `.env.example` mới (đảm bảo đã xóa biến Laya/E2/R2 chết, cấu hình `SAG_ENVIRONMENT=prod`, `SAG_DEBUG=false`, `SAG_SECRET_KEY` mạnh nếu chạy production, và dùng `SAG_QDRANT_URL`).
4. **Mức đầy đủ (Khuyến nghị — áp dụng cả P3 + P4):** Gọi API reprocess cho các tài liệu hiện có (`POST /api/v1/projects/{project_id}/documents/{document_id}/reprocess` — lưu ý P0 đã bỏ request body, gọi không body) để chạy lại toàn bộ chuỗi parse → canonical (`is_boilerplate`) → tách `SearchUnit` (`char_start`/`char_end`/không heading-only) → index Qdrant (`_collection_meta`, `model_signature`, BM25 Unicode).
5. **Mức tái dựng nhanh Qdrant từ `search_units` hiện có (chỉ cập nhật dense/BM25/metadata khi chưa re-parse ngay):** Chạy CLI `SAG/apps/api/scripts/sag_ops.py`:
   ```powershell
   .\.venv\Scripts\python.exe scripts/sag_ops.py rebuild-qdrant --project-id <project_id>
   ```
6. Với **Session Context** (nếu bật `SAG_SESSION_CONTEXT_MODE=on`): các phiên cũ trong `session_units_{project_id}` cần được đồng bộ/làm mới index theo tokenizer BM25 Unicode mới và chỉ dùng embedder HTTP thật (nếu trước đây từng dính vector hash 256-d, cần xóa/tạo lại collection `session_units_{project_id}` chủ động sau khi backup).
7. Với **OCTX / Legacy E0–E1:** Đảm bảo các partition cũ ở `event_vectors` được thu dọn/rebuild sang `event_vectors_wide` và ghi `Source.config["octx_vector_identity"]` qua luồng import/rebuild.
