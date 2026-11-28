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
