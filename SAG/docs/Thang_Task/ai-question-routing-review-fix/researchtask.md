# AI Question Routing & Model Compatibility — PR #62 Review Remediation

## Research record

- Repository: `sag-laya-integration` (`SAG/`)
- Branch: `Tien/feat/ai-question-routing` (PR #62)
- Base: `origin/main` at `9ec9a110d23e8550cbbddfaa3a81d1234708ce2e`; reviewed PR head commit `839621390b76a1f2e2ed9ec243974c03668013f8`
- Status: implementation complete; regression tests and linter verification passing

## Current flow & verified review findings

Verified paths on PR #62 (`8396213`):

- `SAG/apps/api/sag_api/services/question_scope.py`: `classify_question_scope(llm, query)` calls `llm.complete` with a 10s timeout and parses JSON `{"scope":"general|project|application"}`, falling back to `"project"` on errors or unknown values.
- `SAG/apps/api/sag_api/services/agent_service.py`:
  1. **Major regression (`tools` & `scene_notes` overwrite):** Unconditionally setting `tools = ()` and replacing `scene_notes` with `"Internet access is disabled..."` whenever `question_scope in {"general", "application"}` ignored `knowledge_only`. When Internet was enabled (`knowledge_only=False`) and a public query required current or external evidence (e.g., `"Thời tiết hôm nay thế nào?"`), `web_search`, `open_webpage`, and `get_time` were stripped and the system prompt falsely stated Internet was disabled.
  2. **Vietnamese/English temporal word-order gap:** `_TIME_SENSITIVE_INTENT` only matched `(time_marker).{0,40}(topic)` (e.g., `"hôm nay thời tiết"`), missing natural noun-first queries such as `"Thời tiết hôm nay thế nào?"`, `"Giá vàng hôm nay"`, `"Tin tức mới nhất"`, or `"Weather today"`.
  3. **Multi-turn context & Session Memory preservation:** `classify_question_scope` received only the isolated `plan.query` string without prior conversation turns (`plan.messages`), and `scene_notes = [...]` overwrote previously appended `[Session Memory Evidence]` (`[S1], [S2]`) instructions. Turns already grounded by `session_grounded_scope` should retain their session-memory lane without invoking the classifier.
  4. **Unnecessary extra LLM round-trip on deterministic fast paths:** Greetings/identity queries (`_DIRECT_INTENT`, including `"Xin chào Continuum AI!"`), simple arithmetic (`_SIMPLE_ARITHMETIC`), clock-only queries (`_TIME_ONLY_INTENT`), and high-confidence `is_chitchat` decisions already bypass forced `search_context` grounding deterministically without needing an extra sequential `llm.complete` call.
  5. **Classifier JSON output parsing resilience:** `LLMClient.complete` (`sag_api/generation/llm.py`) supports `response_format={"type": "json_object"}` with automatic 400/422 downgrade, whereas `question_scope.py` neither requested JSON response format on `LLMClient` nor stripped Markdown code fences (` ```json ... ``` `) or closed `<think>...</think>` blocks emitted by reasoning models.

## Security, ACL, and provenance boundaries

- Explicit `@source` selection (`bool(plan.source_ids)`) always skips the classifier and enforces `question_scope = "project"` with full `local_grounding_required`.
- `general` never receives private knowledge tools (`search_context`, `get_entity`), persona-configured tools, or bound MCP tools. Allowed tools in `general` are strictly `()` for standalone conceptual questions, `get_time` for offline clock queries, and `get_time` / `web_search` / `open_webpage` when `knowledge_only=False` and the query is time-sensitive or explicitly asks for external lookup.
- `application` never exposes private project tools, MCP tools, or public web tools (public web cannot verify private Continuum tasks/Work Reports), and never grants backend record access.
- Uncertain, malformed, or failed classification always fails closed to `"project"`.

## Actual changed files

- `SAG/apps/api/sag_api/services/question_scope.py`:
  - Accepts optional `prior_turns` to include bounded, untrusted recent turn snippets for multi-turn disambiguation.
  - Passes `response_format={"type": "json_object"}` when `llm` is an `LLMClient` instance while remaining compatible with 1-arg protocol stubs.
  - Strips closed `<think>...</think>` blocks and Markdown code fences before `json.loads`, validates `Mapping`, and logs fallback exception type at `debug` without logging raw user text.
- `SAG/apps/api/sag_api/services/agent_service.py`:
  - Updates `_GREETING_PREFIX` so `"Xin chào <name/Continuum AI>"` matches alongside `"Chào <name/Continuum AI>"`.
  - Updates `_TIME_SENSITIVE_INTENT` to match both `(marker ... topic)` and `(topic ... marker)` orders.
  - Adds `_needs_current_or_external_lookup(query)` and `_should_classify_question_scope(...)` fast-path skip for deterministic greetings, arithmetic, clock-only queries, high-confidence chitchat, and `session_grounded_scope`.
  - Scopes tools and `scene_notes` accurately per `(question_scope, knowledge_only, needs_external_lookup)` while preserving `[Session Memory Evidence]` notes and skipping `resolve_mcp_specs` when `question_scope != "project"`.
- `SAG/apps/api/README.md`: synchronizes Continuum question scope documentation with Internet-enabled public lookup and session-memory behavior.
- `SAG/apps/api/tests/test_question_scope.py`: expands to 15 offline unit and `generate_stream` integration regression tests covering all review scenarios (including word-boundary conceptual queries like `"Explain what a database index is"` and project policy routing like `"Chính sách nghỉ phép hiện tại là gì?"`).
- `SAG/docs/Thang_Task/ai-question-routing-review-fix/researchtask.md`: records task research, scope, verification, and rollback notes.
- Follow-up refinement after CodeRabbit review of `e8c4835`:
  1. Added `\b` word boundaries to `_EXPLICIT_RESEARCH_INTENT` and introduced `_PUBLIC_EXTERNAL_LOOKUP_INTENT` for `general` scope so conceptual terms (`"database"`, `"binary search"`, `"data structures"`, `"đánh giá"`) do not falsely trigger external web research.
  2. Restored `_TIME_SENSITIVE_INTENT` to its forward-only `(marker ... topic)` pattern for `project` scope and isolated bidirectional `_PUBLIC_TIME_SENSITIVE_INTENT` to `general` (`public_scope=True`), preventing `"Chính sách nghỉ phép hiện tại là gì?"` from invoking `get_time` ahead of `search_context` on turn 1.

## Verification outcomes & impact

- `python -m unittest discover -s tests -p test_question_scope.py`: **15 passed**.
- `python -m unittest discover -s tests -p test_litellm_mcp_compatibility.py`: **1 passed**.
- `python -m pytest tests/test_question_scope.py tests/test_litellm_mcp_compatibility.py tests/test_agent_retrieval_failure.py tests/test_continuum_identity_and_greeting.py tests/test_vietnamese_ai_chat_reliability.py tests/test_ecce_session_context.py tests/test_execution_trace_stream.py -q`: **74 passed**.
- `ruff check` on changed Python files: **passed** (`E501` ignored only for untouched legacy long Vietnamese prompt literals in `agent_service.py`).
- `git diff --check`: **passed**.
- Database / Configuration / Security impact: none (no schema, migration, `.env`, or permission elevation changes). Rollback is a clean commit revert on `Tien/feat/ai-question-routing`.
- Remaining operational gaps: Docker image build and live external LLM/web-search provider smoke checks were not executed in this offline review-fix environment.
