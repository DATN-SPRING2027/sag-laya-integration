# Research & Implementation Specification: `@continuumAI` Direct Chat Grounded Answer Endpoint (`sag_api`)

- **Date:** 2026-11-28
- **Owner:** Thang
- **Repository:** `sag-laya-integration` (`SAG/`)
- **Branch:** `feat/Thang-continuum-ai-dm-sag-be-api`
- **Base Commit:** `origin/main` (`05b0492c5c275e4ecc2867af5b3116d8125f97bf`)
- **Binding Contract:** `_docs-and-media/specs/continuum-ai-chat-rieng/tasks/contract.md` (§4) · `plan.md` (Hạng mục 3)

---

## 1. Current Flow & Verified Codebase Trace on `main` (`05b0492`)

All facts below were verified directly against tracked source files on `origin/main` (`05b0492`):

1. **Router Registration (`SAG/apps/api/sag_api/api/v1/__init__.py:1-57`):**
   - `api_router = APIRouter(prefix="/api/v1")` mounts every v1 module router and is attached to the FastAPI app in `sag_api/main.py:168`.
   - `sag_api/main.py:145-166` registers global exception handlers converting `ApiError` subclasses to `JSONResponse(status_code=exc.status_code, content=exc.to_envelope(request_id=...))`.
2. **Service-Only BFF Auth Boundary (`SAG/apps/api/sag_api/core/principal_assertion.py:497-515` & `api/v1/platform_operations.py:16-43`):**
   - `require_bff_service_token(request)` validates `X-SAG-BFF-Service-Token` with constant-time `secrets.compare_digest` against `settings.bff_service_token` (raising `403 ForbiddenError` on missing/duplicate/mismatch and `503 ServiceUnavailableError` when unconfigured).
   - `required_bff_header(request, name, max_length=...)` validates single, non-empty, non-padded header values (`403 ForbiddenError` on failure).
   - `platform_operations.py` establishes the convention for internal service endpoints: reject unexpected query params with `BadRequestError("Unsupported query parameters")` (`400 validation_error`) and return `Cache-Control: no-store`.
3. **LLM Client, Structured JSON & Error Taxonomy (`sag_api/generation/llm.py`, `json_parser.py`, `core/errors.py`, `core/error_taxonomy.py`):**
   - `LLMClient` (`sag_api/generation/llm.py:149-423`, injected via `Depends(get_llm)` in `sag_api/core/deps.py:191-192`) implements `StructuredLLMClient` with `complete(messages, response_format=..., temperature=..., max_tokens=...)`.
   - `_ensure_configured()` raises `ConfigurationError` (`400 configuration_error`) when `SAG_LLM_API_KEY` is missing.
   - `_classify_llm_error()` maps provider timeouts (`Timeout`, `APITimeoutError`), rate limits (`429`), and provider `5xx` to `ServiceUnavailableError(..., code=ErrorCode.LLM_UNAVAILABLE, layer=ErrorLayer.LLM, stage=ErrorStage.GENERATE)` (`503 llm_unavailable`, `retryable=True`); `400`/`422` provider errors to `UpstreamError(..., code=ErrorCode.LLM_BAD_REQUEST)` (`502`); empty choices to `UpstreamError(..., code=ErrorCode.LLM_EMPTY_RESPONSE)` (`502`).
   - `decode_json_object(raw_text)` (`sag_api/generation/json_parser.py:231-234`) strips `<think>` blocks and markdown fences safely without regex backtracking.
   - `token_usage_scope(component=..., tenant_id=...)` and `extract_chat_usage(...)` (`sag_api/core/token_usage.py:171-228, 477-498`) meter exact provider tokens (or deterministic tiktoken fallback for test doubles).
4. **Enterprise Editorial Formatting Standard (`sag_api/generation/prompt.py:183-199`, commit `05b0492`):**
   - Enforces executive technical tone in Vietnamese/English, forbids decorative emojis (`✅`, `🚀`, `📌`, `💡`, `🔹`, `⚡`, `🎯`), favors concise takeaway-first answers and compact Markdown (at most 2 bullet levels, concise label bolding, bracketed citations immediately before terminal punctuation with no trailing space).
5. **Reference Grounded BFF Pattern (`api/v1/handover.py` & `services/handover_assist_service.py`):**
   - Uses `StrictModel(BaseModel)` with `ConfigDict(extra="forbid")` (`schemas/handover_assist.py:19-20`), short-circuits empty context to `INSUFFICIENT_EVIDENCE` with zero LLM calls, wraps LLM calls in `token_usage_scope`, and verifies all output citations against the input catalog.
   - Unlike `handover_assist_service.py` (which queries PostgreSQL `SearchUnit`/`CanonicalBlock`), the DM assistant endpoint MUST remain **100% stateless with ZERO PostgreSQL, Qdrant, or web retrieval**.
6. **OpenAPI Snapshot (`SAG/docs/openapi/sag-api.openapi.json`):**
   - Committed snapshot exported from `create_app().openapi()` using `json.dumps(..., indent=2, sort_keys=True, ensure_ascii=False) + "\n"` (currently `114` paths on `main`; 2 minor docstring diffs already existed on `/api/v1/sources/{source_id}/documents/{document_id}/{file,preview}` since PR #76). No automated pytest drift guard existed yet.

---

## 2. Target Contract, ACL/Security Boundary & Hard Token/Payload Caps

### 2.1. Endpoint & Auth Boundary (Fail-Closed)
- **Route:** `POST /api/v1/internal/dm-assistant/answer`
- **Service Auth (`X-SAG-BFF-Service-Token`):**
  - If `settings.bff_service_token` is unset/blank → `503 ServiceUnavailableError` (`code="service_unavailable"`), fail-closed regardless of caller headers.
  - Otherwise verified via `require_bff_service_token(request)` (`secrets.compare_digest`, constant-time) → `403 ForbiddenError` (`code="forbidden"`) on missing, empty, duplicate, or mismatched token.
  - `Authorization: Bearer ...` user/connector JWT MUST NOT authenticate this endpoint.
- **Required Continuum Context Headers (all 3 required, exact 24-hex `^[a-fA-F0-9]{24}$`):**
  - `X-Continuum-Organization-Id`
  - `X-Continuum-User-Id`
  - `X-Continuum-Conversation-Id`
  - Missing, padded, duplicate, or non-24-hex values → `403 ForbiddenError` (`code="forbidden"`).
- **Out-of-Scope Rejection:**
  - Any query parameter (`request.query_params`) → `400 BadRequestError` (`code="validation_error"`).
  - Any extra JSON body/message key (`projectId`, `sourceIds`, `threadId`, etc.) → `422` via `ConfigDict(extra="forbid")`.
- **HTTP Response Header:** `Cache-Control: no-store`.
- **Stateless Data Isolation:** Route injects only `Depends(require_dm_assistant_context)` and `Depends(get_llm)`. Never injects `get_session`, `get_engine_manager`, or `get_agent_runtime`. Chat Backend (`DATN_BE`) remains sole owner of DM ACLs and history retrieval.

### 2.2. Request & Response Schemas (`schemas/dm_assistant.py`)
- **Request (`DmAssistantAnswerRequest`, `extra="forbid"`):**
  - `question`: trimmed `1..2000` chars.
  - `timezone`: trimmed `1..64` chars, validated via `zoneinfo.ZoneInfo` (IANA timezone; `tzdata` installed).
  - `referenceNowIso`: ISO-8601 timezone-aware `datetime` (rejects naive datetimes and numeric timestamps).
  - `queryMode`: `Literal["TIME_WINDOW", "KEYWORD", "HYBRID"]`.
  - `partialCoverage`: `bool = False`.
  - `coverageHint`: optional trimmed `str | None` (`<= 500` chars; blank normalized to `None`).
  - `messages`: `list[DmAssistantMessageInput]` (`0..80` items). Each item (`extra="forbid"`):
    - `refId`: `^M[1-9][0-9]{0,2}$`, unique across `messages`.
    - `senderLabel`: trimmed `1..120` chars.
    - `sentAtIso`: ISO-8601 timezone-aware `datetime`.
    - `text`: trimmed `1..1200` chars.
  - **Total Context Cap:** `sum(len(m.text) for m in messages) <= 24_000` (violation → `422`).
- **Response (`DmAssistantAnswerResponse`, `extra="forbid"`):**
  - `status`: `Literal["ANSWERED", "INSUFFICIENT_EVIDENCE"]`.
  - `answerText`: trimmed `1..4000` chars.
  - `citedRefIds`: `0..20` unique `^M[1-9][0-9]{0,2}$` strings; strictly a subset of input `messages[].refId`; non-empty iff `status == "ANSWERED"`, `[]` iff `status == "INSUFFICIENT_EVIDENCE"`.
  - `coverageNote`: `str | None` (`<= 500` chars); guaranteed populated when `partialCoverage is True`.
  - `model`: `str | None` (`<= 120` chars).
  - `usage`: `{ inputTokens, cachedTokens, outputTokens, totalTokens, calls }` (all `int >= 0`).

### 2.3. LLM Generation Budget & Timeout Semantics
- `LLMClient.complete(..., response_format={"type": "json_object"}, temperature=0.1, max_tokens=1024)`.
- Request-level timeout enforced via `asyncio.timeout(min(settings.llm_timeout_ms, 40_000) / 1000.0)`.
- Timeout (either `asyncio.timeout` `TimeoutError` or LiteLLM timeout via `_classify_llm_error`) maps uniformly to **`503 ServiceUnavailableError` (`code=ErrorCode.LLM_UNAVAILABLE`, `layer=ErrorLayer.LLM`, `stage=ErrorStage.GENERATE`, `retryable=True`)**, aligned with `contract.md` §4.4 and `sag_api/core/errors.py` (confirmed in swarm board #123/#124).

---

## 3. Provenance, Prompt-Injection Guardrails & Failure/Edge Cases

1. **Zero-Context Short-Circuit:** `messages == []` returns `200 OK` immediately with `status="INSUFFICIENT_EVIDENCE"`, `citedRefIds=[]`, polite Vietnamese guidance suggesting a more specific question, `model=None`, `usage.calls=0` — zero LLM calls and no requirement for `SAG_LLM_API_KEY`.
2. **Untrusted Data Boundary:** System prompt explicitly frames both `question` and `messages[].text` as untrusted conversation data, instructs the model to ignore instructions attempting to escape DM scope (other chats, projects, files, web, system prompt), and encodes sender labels safely with `json.dumps(..., ensure_ascii=False)`.
3. **Chronology, Corrections & Conflicting Facts:** Each message renders both local time in `body.timezone` and UTC ISO so relative Vietnamese/English time windows ("hôm qua", "tuần trước") are deterministic; prompt instructs handling follow-up corrections and reporting conflicting dates/values with both `[M#]` citations or returning `INSUFFICIENT_EVIDENCE` when unresolved.
4. **Strict Output & Citation Verification:**
   - Empty raw completion → `502 UpstreamError(code=ErrorCode.LLM_EMPTY_RESPONSE)`.
   - Malformed / non-JSON / wrong field types / invalid `status` → `502 UpstreamError(code=ErrorCode.SCHEMA_VALIDATION_ERROR)`.
   - **Any `refId` in `citedRefIds` not present in `{m.refId for m in body.messages}`** → hard refusal `502 UpstreamError("LLM cited unknown message references", code=ErrorCode.SCHEMA_VALIDATION_ERROR)` (even if `status == "INSUFFICIENT_EVIDENCE"`).
   - `status == "ANSWERED"` with empty `citedRefIds` → safe downgrade to `status="INSUFFICIENT_EVIDENCE"`, `citedRefIds=[]`, replacing any uncited text with standard insufficient-evidence copy.
   - `answerText` exceeding `4000` chars is cleanly clamped (`<= 4000` chars with ellipsis).
   - Unconfigured LLM (`not llm.configured`) on non-empty messages → `400 ConfigurationError` (`code="configuration_error"`).
5. **Privacy-Safe Telemetry:** Structured log emits only `request_id`, `organization_id`, `conversation_id`, `query_mode`, `message_count`, `status`, `cited_count`, `latency_ms`, `input_tokens`, `output_tokens`, `calls`. Never logs `question`, `messages[].text`, `senderLabel`, `answerText`, `coverageHint`, or service tokens.

---

## 4. Proposed File-by-File Changes

1. `SAG/docs/Thang_Task/continuum-ai-dm-assistant/researchtask.md` — Mandatory Rule 05 research & handoff record (this document).
2. `SAG/apps/api/sag_api/schemas/dm_assistant.py` — Strict Pydantic v2 request/response/usage schemas with IANA timezone, aware datetime, unique `refId`, count (`<= 80`), per-message (`<= 1200`), and total character (`<= 24_000`) validators.
3. `SAG/apps/api/sag_api/services/dm_assistant_service.py` — Stateless DM synthesis service: empty-message short-circuit, untrusted-data/editorial prompt construction, `LLMClient.complete` call under `asyncio.timeout` + `token_usage_scope`, strict JSON & `citedRefIds` verification, safe downgrade, and privacy-safe logging.
4. `SAG/apps/api/sag_api/api/v1/dm_assistant.py` — Internal route `POST /api/v1/internal/dm-assistant/answer`, fail-closed BFF token + 24-hex header dependency, query-param rejection (`400`), `Cache-Control: no-store`.
5. `SAG/apps/api/sag_api/api/v1/__init__.py` — Mount `dm_assistant.router`.
6. `SAG/docs/openapi/sag-api.openapi.json` — Regenerated canonical OpenAPI snapshot including `POST /api/v1/internal/dm-assistant/answer` (`115` paths).
7. `SAG/apps/api/tests/test_dm_assistant_api.py` — Automated pytest suite covering auth, headers, query params, payload/cap limits, empty short-circuit, grounded synthesis, conflicting sources, fabricated `refId` refusal, empty `citedRefIds` downgrade, malformed JSON, timeout/provider errors, prompt-injection isolation, privacy log scrubbing, and OpenAPI snapshot drift guard.

---

## 5. Test Matrix & Acceptance Checklist

| # | Case | Expected Result |
| --- | --- | --- |
| 1 | Missing / wrong `X-SAG-BFF-Service-Token`, or `Authorization: Bearer` only | `403 forbidden`, zero LLM calls |
| 2 | Unconfigured `SAG_BFF_SERVICE_TOKEN` (`None` / blank) | `503 service_unavailable`, fail-closed |
| 3 | Missing / malformed / duplicate `X-Continuum-{Organization,User,Conversation}-Id` (non-24-hex) | `403 forbidden` |
| 4 | Any query parameter (`?projectId=...`) | `400 validation_error` |
| 5 | Extra body/message fields (`projectId`, `sourceIds`, `threadId`), invalid IANA timezone, naive datetime, `> 80` messages, duplicate `refId`, `refId` not `^M[1-9][0-9]{0,2}$`, `text > 1200`, `sum(len(text)) > 24_000`, blank question | `422` validation error |
| 6 | Empty `messages: []` | `200 OK`, `INSUFFICIENT_EVIDENCE`, `citedRefIds: []`, `usage.calls == 0`, `Cache-Control: no-store`, LLM not called |
| 7 | Non-empty `messages` when LLM unconfigured | `400 configuration_error` |
| 8 | Grounded Vietnamese/English answer + valid `citedRefIds` + `partialCoverage` `coverageNote` | `200 OK`, `ANSWERED`, deduped valid `citedRefIds`, usage tokens populated, `Cache-Control: no-store` |
| 9 | Conflicting dates/figures across two messages | System prompt instructs citing both sources or returning `INSUFFICIENT_EVIDENCE`; verified valid response with both `[M1, M2]` |
| 10 | Prompt-injection in `question` or `messages[].text` attempting to escape DM scope | Prompt explicitly isolates untrusted data; `INSUFFICIENT_EVIDENCE` returned without leaking outside scope |
| 11 | Fabricated `refId` (e.g., `M99` not in input, including under `INSUFFICIENT_EVIDENCE`) | `502 schema_validation_error` |
| 12 | `ANSWERED` with empty `citedRefIds: []` | Safely downgraded to `200 INSUFFICIENT_EVIDENCE`, `citedRefIds: []`, uncited answer text discarded |
| 13 | Empty LLM completion / malformed non-JSON / invalid schema fields | `502 llm_empty_response` / `502 schema_validation_error` |
| 14 | `answerText > 4000` chars returned by LLM | Clamped cleanly to `<= 4000` chars |
| 15 | LLM timeout (`TimeoutError` / slow coroutine exceeding cap) & provider `ServiceUnavailableError` | `503 llm_unavailable` (`retryable: true`) |
| 16 | Privacy log audit (`caplog`) | Zero leakage of `question`, `messages[].text`, `answerText`, `senderLabel`, or `X-SAG-BFF-Service-Token` |
| 17 | OpenAPI snapshot parity (`SAG/docs/openapi/sag-api.openapi.json == create_app().openapi()`) | Exact match (`115` paths) |

### Acceptance Checklist
- [x] `researchtask.md` created before implementation and updated after verification.
- [x] All schema, service, router, and OpenAPI snapshot changes implemented cleanly.
- [x] `ruff check sag_api tests` passes with 0 errors.
- [x] `pytest -q tests/test_dm_assistant_api.py` passes (`21 passed`, 0 failures) and full SAG `pytest -q` passes all 942 unaffected tests (`942 passed, 5 skipped`; 1 unrelated Windows-local LiteLLM `1.111.0` fixture failure in `tests/test_litellm_mcp_compatibility.py`, documented below).
- [x] Git diff clean, `.serena/` preserved, no secrets or temporary files tracked, committed on `feat/Thang-continuum-ai-dm-sag-be-api` (no `git push`).

---

## 6. Out-of-Scope, Known Gaps & Environment Notes

- **Out-of-Scope:** MongoDB schemas/migrations (`DATN_BE` DB branch), Chat BE retrieval/worker/WebSocket (`DATN_BE` BE branch), `HttpAiEngineClient` allowlist (`DATN_BE` BE branch), FE composer/bubble/Visual QA (`DATN_FE` FE branch), and live SiliconFlow ZDR/DPA production compliance sign-off (tracked separately in `plan.md` & runtime audit #122).
- **Windows Test Environment Note (verified in #122):** `SAG/apps/api/uv.lock` pins `litellm==1.92.0` without a Windows wheel, causing `uv sync --frozen` to invoke a local Rust/maturin build that fails without MSVC `link.exe`. Local verification uses the pre-synced ASCII-path Python 3.12 environment `$env:UV_PROJECT_ENVIRONMENT='C:\Users\KAYT\.cache\sag-api-venv'` (`litellm==1.111.0`) with `uv run --no-sync`. Under `litellm==1.111.0`, one unrelated pre-existing test (`tests/test_litellm_mcp_compatibility.py::LiteLLMMCPCompatibilityTest::test_function_tools_stream_without_mcp_gateway_dependencies`) fails with `MidStreamFallbackError: Stream ended before a finish_reason was received` because its stub SSE chunk sends `finish_reason: None` before `[DONE]`. That test was intentionally left untouched to avoid mixing unrelated fixes into this branch.
- **Unverified Acceptance Scope:** Passing unit/mock tests do not replace live end-to-end integration verification with `DATN_BE` + live LLM provider or production SiliconFlow ZDR/DPA verification.

---

## 7. Actual Implementation, Verification & Handoff

- **Actual Changed Files (`7` files on `feat/Thang-continuum-ai-dm-sag-be-api`):**
  - `SAG/docs/Thang_Task/continuum-ai-dm-assistant/researchtask.md` (new)
  - `SAG/apps/api/sag_api/schemas/dm_assistant.py` (new)
  - `SAG/apps/api/sag_api/services/dm_assistant_service.py` (new)
  - `SAG/apps/api/sag_api/api/v1/dm_assistant.py` (new)
  - `SAG/apps/api/sag_api/api/v1/__init__.py` (modified: mounted `dm_assistant.router`)
  - `SAG/docs/openapi/sag-api.openapi.json` (modified: `+243 / -2`, synced to `115` paths including `POST /api/v1/internal/dm-assistant/answer` plus 2 pre-existing docstring syncs on `/file` and `/preview`)
  - `SAG/apps/api/tests/test_dm_assistant_api.py` (new: `21` tests including OpenAPI snapshot drift guard)
- **Checks Executed (`$env:UV_PROJECT_ENVIRONMENT='C:\Users\KAYT\.cache\sag-api-venv'`):**
  - `uv run --no-sync ruff check sag_api tests` → **PASS** (`All checks passed!`)
  - `uv run --no-sync pytest -q tests/test_dm_assistant_api.py` → **PASS** (`21 passed in 10.75s`)
  - `uv run --no-sync pytest -q` (full SAG suite) → **`942 passed, 5 skipped, 1 failed`** (`tests/test_litellm_mcp_compatibility.py` only, pre-existing Windows `litellm==1.111.0` SSE `finish_reason: None` incompatibility; zero regressions in any other SAG test)
- **Database / Configuration / Security / Rollback Impact:**
  - **Database / Migrations:** **None** — zero PostgreSQL schema/migration changes, zero Qdrant changes, zero DB session injection (`get_session` is absent from route dependencies).
  - **Configuration:** **None** — reuses existing `SAG_BFF_SERVICE_TOKEN` and `SAG_LLM_*` settings (`llm_timeout_ms` capped at `40_000`ms for DM assistant).
  - **Security & Privacy:** Fail-closed BFF token (`503` unconfigured, `403` missing/wrong, `Authorization: Bearer` rejected), strict 24-hex Continuum context headers (`403`), query-param rejection (`400`), `extra="forbid"` schemas (`422`), `Cache-Control: no-store`, strict fabricated-`refId` refusal (`502`), safe `ANSWERED`-without-citations downgrade (`INSUFFICIENT_EVIDENCE`), sanitized `request_id`, and verified zero-PII/zero-secret structured logging.
  - **Rollback:** Revert the branch commit (or disable `CHAT_DM_ASSISTANT_ENABLED=false` on `DATN_BE`); zero schema or data rollback required.
- **Exact Push Command (for Thang after review):**
  ```powershell
  Set-Location -LiteralPath 'F:\LEARN KÌ 8\ĐATN\sag-laya-integration'
  git status --short
  git log -1 --stat
  git push -u origin feat/Thang-continuum-ai-dm-sag-be-api
  ```

---

## 8. Codex Review Hardening Round (PR #92 — P1/P2/P3 Remediation)

### 8.1. Verified Findings & Remediation Plan
1. **[P1-3] Pre-Parser Auth & HTTP Body Byte-Cap Guard (`sag_api/api/v1/dm_assistant.py`, `sag_api/main.py`, `sag_api/core/error_taxonomy.py`, `sag_api/core/errors.py`):**
   - FastAPI 0.139.0 parses request body JSON and Pydantic models before executing route `Depends(...)`, allowing unauthenticated malformed JSON (`422`) or unbounded body buffering before auth.
   - Add `ErrorCode.PAYLOAD_TOO_LARGE = "payload_too_large"` and `PayloadTooLargeError` (`413`).
   - Implement `DmAssistantGuardMiddleware` (registered in `create_app()`, plus defense-in-depth `DmAssistantRoute(APIRoute)`) scoped to `POST /api/v1/internal/dm-assistant/answer` that runs strictly before any body bytes are read:
     - `503 service_unavailable` when `SAG_BFF_SERVICE_TOKEN` is unconfigured/blank.
     - `403 forbidden` when `X-SAG-BFF-Service-Token` is missing/duplicate/invalid (including `Authorization: Bearer`-only callers) or any of the three 24-hex `X-Continuum-{Organization,User,Conversation}-Id` headers is missing/duplicate/invalid.
     - `400 validation_error` when any query parameter is present or `Content-Length` is malformed/duplicate.
     - `413 payload_too_large` (with `Cache-Control: no-store`) when `Content-Length > 65_536` (64 KiB) OR when streamed ASGI `receive` chunks exceed `65_536` bytes without buffering unbounded memory.
   - Ensure `Cache-Control: no-store` is set on every response (success and error) for `/api/v1/internal/dm-assistant/answer`.
2. **[P2-5] Strict Single JSON Object, Inline `[M#]` Ref Binding & Deterministic `INSUFFICIENT_EVIDENCE` Copy (`sag_api/services/dm_assistant_service.py`):**
   - Replace permissive `decode_json_object` fallback with strict single-JSON-object parsing: strip only well-formed leading `<think>...</think>` and at most one outer surrounding markdown code fence, then require `json.loads` to decode the entire remaining string into one JSON object (`dict`); reject mixed prose, trailing text, multiple objects, or unclosed `<think>` with `502 schema_validation_error`.
   - Scan `answerText` for all inline `[M(\d+)]`, `[M[^\]]*]`, and lowercase `[m\d+]` citation tokens; reject any inline token not in input `valid_ref_ids` with `502 schema_validation_error`.
   - On all `INSUFFICIENT_EVIDENCE` outputs (empty short-circuit, model-declared insufficient, or uncited `ANSWERED` downgrade), discard free-form model text and return deterministic emoji-free Vietnamese (default) or English copy with `citedRefIds: []`.
3. **[P2-6] OpenAPI Snapshot Parity & Sanitized 422 Envelope (`sag_api/main.py`, `sag_api/api/v1/dm_assistant.py`, `sag_api/schemas/dm_assistant.py`, `SAG/docs/openapi/sag-api.openapi.json`):**
   - Register a global `RequestValidationError` (`422`) handler in `main.py` emitting the standard `{"error": {"code": "validation_error", ...}}` envelope while stripping raw `input` and `ctx` values so user questions/DM text are never echoed back.
   - Document all 4 required headers (`X-SAG-BFF-Service-Token`, `X-Continuum-Organization-Id`, `X-Continuum-User-Id`, `X-Continuum-Conversation-Id`) and all error responses (`400`, `403`, `413`, `422`, `502`, `503`) on `POST /api/v1/internal/dm-assistant/answer` and regenerate `SAG/docs/openapi/sag-api.openapi.json`.
4. **[P2-7] Sanitized Provider Errors & Consistent `503 llm_unavailable` Mapping (`sag_api/generation/llm.py`, `sag_api/services/dm_assistant_service.py`):**
   - Expand `_classify_llm_error()` in `llm.py` so all provider `5xx` (`500 <= status < 600`), `408`, `429`, and connection/timeout errors map consistently to `503 ServiceUnavailableError(code=ErrorCode.LLM_UNAVAILABLE)`.
   - Never expose raw provider exception strings in DM assistant HTTP responses or logs: map all LLM timeouts, 429, 5xx, network, and generic provider errors to `503 llm_unavailable` (`retryable: true`) with fixed safe message `"DM assistant LLM synthesis is temporarily unavailable."`, and sanitize `400`/`502` LLM errors to fixed safe copy.
5. **[P3-1] Strict RFC 3339 Timestamp Validation & Explicit UTC Conversion (`sag_api/schemas/dm_assistant.py`, `sag_api/services/dm_assistant_service.py`):**
   - Require strict RFC 3339 datetime strings with explicit `Z` or `±HH:MM` offset on `referenceNowIso` and `sentAtIso` (rejecting numeric ints/floats and digit-only Unix timestamp strings such as `"1700000000"`).
   - Explicitly convert `.astimezone(timezone.utc)` before formatting UTC prompt timestamps (`referenceNowUtc` and `utc=...`) using canonical `Z` notation.

### 8.2. Hardening Verification Results
- **Changed Files (`10` files on `feat/Thang-continuum-ai-dm-sag-be-api`):**
  - `SAG/docs/Thang_Task/continuum-ai-dm-assistant/researchtask.md` — Recorded PR #92 Codex hardening plan and verified outcomes.
  - `SAG/apps/api/sag_api/core/error_taxonomy.py` — Added `ErrorCode.PAYLOAD_TOO_LARGE = "payload_too_large"`.
  - `SAG/apps/api/sag_api/core/errors.py` — Added `PayloadTooLargeError` (`413`).
  - `SAG/apps/api/sag_api/generation/llm.py` — Expanded `_classify_llm_error` so all provider `5xx` (`500 <= status < 600`), `408`, `429`, and connection/timeout errors map consistently to `503 ServiceUnavailableError(code=ErrorCode.LLM_UNAVAILABLE)`.
  - `SAG/apps/api/sag_api/main.py` — Mounted `DmAssistantGuardMiddleware` and registered sanitized `RequestValidationError` (`422`) handler returning standard `{"error": {"code": "validation_error", ..., "fields": [...]}}` without raw `input`/`ctx`.
  - `SAG/apps/api/sag_api/schemas/dm_assistant.py` — Enforced strict RFC 3339 timestamps with explicit `Z` or `±HH:MM` offset (rejecting numeric/digit-only Unix timestamps) and added `DmAssistantErrorEnvelopeOut` OpenAPI schemas.
  - `SAG/apps/api/sag_api/api/v1/dm_assistant.py` — Added `DmAssistantGuardMiddleware` + `DmAssistantRoute` pre-parser auth/query/64 KiB body byte-cap guard (`403`/`503`/`400`/`413` + `Cache-Control: no-store`), explicit OpenAPI `Header(...)` declarations for all 4 required headers, and explicit OpenAPI error response schemas (`400`/`403`/`413`/`422`/`502`/`503`).
  - `SAG/apps/api/sag_api/services/dm_assistant_service.py` — Added strict single-JSON-object parser (`_parse_strict_json_object`), inline `[M#]` / `[M...]` / `[m#]` ref validation (`_validate_inline_citations`), deterministic emoji-free VI/EN copy for all `INSUFFICIENT_EVIDENCE` paths, sanitized fixed-copy LLM error mapping (`503 llm_unavailable`), and explicit UTC `.astimezone(UTC)` (`Z`) prompt timestamp normalization.
  - `SAG/docs/openapi/sag-api.openapi.json` — Regenerated from live `create_app().openapi()`.
  - `SAG/apps/api/tests/test_dm_assistant_api.py` — Expanded suite using live `create_app()` covering pre-parser `403`/`503`/`400` on malformed/oversized bodies, `Content-Length` + streamed chunked `413 payload_too_large`, strict JSON & inline `[M#]` refusal (`502`), deterministic VI/EN `INSUFFICIENT_EVIDENCE` copy, non-echoing sanitized `422` envelope, raw 5xx provider error sanitization (`503 llm_unavailable`), RFC 3339 Unix rejection + `+07:00 -> Z` UTC normalization, and OpenAPI header/response parity.
- **Checks Executed (`$env:UV_PROJECT_ENVIRONMENT='C:\Users\KAYT\.cache\sag-api-venv'`):**
  - `uv run --no-sync ruff check sag_api tests` → **PASS** (`All checks passed!`, exit `0`)
  - `uv run --no-sync pytest -q tests/test_dm_assistant_api.py tests/test_handover_assist.py tests/test_p1_runtime_llm_prompt.py tests/test_full_audit_remediation.py tests/test_p2_json_and_protocol.py` → **PASS** (`84 passed in 24.96s`, exit `0`)
  - `uv run --no-sync pytest -q --ignore=tests/test_litellm_mcp_compatibility.py` → **PASS** (`955 passed, 5 skipped, 0 failed in 257.96s`, exit `0`; only the pre-existing Windows-local `litellm==1.111.0` fixture in `test_litellm_mcp_compatibility.py` ignored as documented in §6)
- **Database / Configuration / Security / Rollback Impact:**
  - **Database / Migrations:** **None.**
  - **Configuration:** **None.**
  - **Security & Privacy:** Eliminates unauthenticated JSON parsing / unbounded body buffering (`403`/`503` before reading body, `413` at `65_536` bytes), strips raw user input from all `422` validation responses, sanitizes all upstream LLM provider exceptions to fixed safe copy in both HTTP responses and logs, blocks mixed-prose JSON and fabricated inline `[M#]` citations (`502`), enforces deterministic `INSUFFICIENT_EVIDENCE` copy, and guarantees `Cache-Control: no-store` on all DM assistant responses.
  - **Rollback:** Revert the hardening commit on `feat/Thang-continuum-ai-dm-sag-be-api`; zero schema or migration rollback needed.

---

## 9. Codex Round 2 P2 Remediation (PR #92)

### 9.1. Findings & Fixes
1. **[P2 · 128 KiB HTTP Body Byte-Cap Aligned with BE UTF-8 Budget (`sag_api/api/v1/dm_assistant.py`):**
   - 24,000 Vietnamese UTF-8 characters (up to 3 bytes/char) plus JSON metadata can legitimately reach ~95–112 KiB, exceeding the previous 64 KiB cap.
   - Raised `DM_ASSISTANT_MAX_BODY_BYTES = 131_072` (128 KiB, coordinated with `DATN_BE`'s `<= 114_688` B / 112 KiB serialized UTF-8 JSON cap) and updated OpenAPI `413` documentation + error message.
   - Retained both `Content-Length` pre-check and chunked streaming byte-counter returning `413 payload_too_large` + `Cache-Control: no-store` strictly before JSON parsing for payloads `> 131_072` bytes (while unauthenticated `> 131_072` byte requests still return `403 forbidden`).
2. **[P2 · DM-Scoped Sanitized `422` Validation Handler (`sag_api/main.py`):**
   - Scoped the custom sanitized `RequestValidationError` (`422`) handler strictly to `DM_ASSISTANT_ANSWER_PATH` (`/api/v1/internal/dm-assistant/answer`), returning the sanitized `{"error": {"code": "validation_error", ...}}` envelope without `input`/`ctx` plus `Cache-Control: no-store`.
   - All other SAG routes delegate cleanly to FastAPI's built-in `await request_validation_exception_handler(request, exc)` so existing routes preserve their published `HTTPValidationError` (`{"detail": [...]}`) contract.
3. **[P2 · Inline `[M#]` Strictly Bound to Final Returned `citedRefIds` (`sag_api/services/dm_assistant_service.py`):**
   - Computed deduplicated, validated `final_cited_ref_ids = deduped_refs[:MAX_DM_CITED_REFS]` (capped at 20 refs) first on `ANSWERED` responses and enforced `_validate_inline_citations(answer_text, set(final_cited_ref_ids))` so every inline `[M#]` token in `answerText` MUST appear in the final returned `citedRefIds` (rejecting omitted or cap-truncated inline refs with `502 schema_validation_error`).
   - Updated the system prompt instruction to match and preserved fabricated inline-ref rejection (`502`) under `INSUFFICIENT_EVIDENCE`.

### 9.2. Round 2 Verification Results
- **Changed Files (`6` files on `feat/Thang-continuum-ai-dm-sag-be-api`):**
  - `SAG/apps/api/sag_api/api/v1/dm_assistant.py` — Raised `DM_ASSISTANT_MAX_BODY_BYTES` from `65_536` to `131_072` (128 KiB), unified `_BODY_TOO_LARGE_MESSAGE`, and updated OpenAPI `413` response description.
  - `SAG/apps/api/sag_api/main.py` — Scoped the sanitized `RequestValidationError` (`422`) handler strictly to `DM_ASSISTANT_ANSWER_PATH` (with explicit `Cache-Control: no-store`) and delegated all other routes to `fastapi.exception_handlers.request_validation_exception_handler`.
  - `SAG/apps/api/sag_api/services/dm_assistant_service.py` — Computed `final_cited_ref_ids = deduped_refs[:MAX_DM_CITED_REFS]` first and bound `_validate_inline_citations(answer_text, set(final_cited_ref_ids))` on `ANSWERED` outputs (rejecting omitted or 20-ref-cap-truncated inline `[M#]` tokens with `502 schema_validation_error`), while keeping fabricated inline ref rejection on `INSUFFICIENT_EVIDENCE` and tightening the system prompt contract.
  - `SAG/docs/openapi/sag-api.openapi.json` — Regenerated from live `create_app().openapi()` (1-line sync on `413` description: `64 KiB` → `128 KiB`).
  - `SAG/apps/api/tests/test_dm_assistant_api.py` — Updated 128 KiB body-cap tests (including ~92 KiB valid Vietnamese UTF-8 payload within 24,000 chars returning `200`, `> 131_072` B `Content-Length`/chunked returning `413`, and unauth `> 131_072` B returning `403`), added non-DM `POST /api/v1/auth/register` standard FastAPI `422` (`{"detail": [...]}`) preservation test, and added inline-to-final-`citedRefIds` binding tests (omitted `[M2]`, cap-truncated `[M21]`, empty `citedRefIds` with inline `[M1]`, valid `[M1]/[M2]`, and valid 20-ref cap boundary).
  - `SAG/docs/Thang_Task/continuum-ai-dm-assistant/researchtask.md` — Recorded Codex Round 2 P2 remediation and verified outcomes.
- **Checks Executed (`$env:UV_PROJECT_ENVIRONMENT='C:\Users\KAYT\.cache\sag-api-venv'`):**
  - `uv run --no-sync ruff check sag_api tests` → **PASS** (`All checks passed!`, exit `0`)
  - `uv run --no-sync pytest -q tests/test_dm_assistant_api.py tests/test_handover_assist.py` → **PASS** (`46 passed in 37.00s`, exit `0`)
  - `uv run --no-sync pytest -q tests/test_dm_assistant_api.py tests/test_handover_assist.py tests/test_all_projects_chat_scope.py tests/test_answer_actions_api.py tests/test_branch_lineage_api.py tests/test_p2_conflicts_api.py` → **PASS** (`92 passed in 58.78s`, exit `0`)
- **Database / Configuration / Security / Rollback Impact:**
  - **Database / Migrations:** **None.**
  - **Configuration:** **None.**
  - **Security & Contract Parity:** Preserves pre-parser auth (`403`/`503`) and 128 KiB body cap (`413` + `Cache-Control: no-store`) while accommodating full 24,000-char Vietnamese UTF-8 payloads aligned with BE's 112 KiB budget; preserves standard FastAPI `HTTPValidationError` (`{"detail": [...]}`) on all legacy SAG routes while keeping DM `422` sanitized; guarantees every inline `[M#]` returned in `ANSWERED` `answerText` exists in the final returned `citedRefIds` (`<= 20` refs).
  - **Rollback:** Revert the Round 2 commit on `feat/Thang-continuum-ai-dm-sag-be-api`; zero schema or migration rollback needed.
