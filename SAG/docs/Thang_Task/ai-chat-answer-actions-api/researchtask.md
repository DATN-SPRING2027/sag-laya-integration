# AI Chat answer actions — SAG API & domain services

## Scope and provenance

Thang approved `_docs-and-media/specs/ai-chat-answer-actions.md` and
`_docs-and-media/specs/ai-chat-answer-actions.openapi.json` on 2026-10-30.
Branch `feat/Thang-ai-chat-answer-actions-be-api` in `sag-api` builds directly
on the verified database foundation commit `4b16fc9`
(`feat/Thang-ai-chat-answer-actions-db`), which must merge before this PR.

## Current contracts and security boundaries

- Conversation endpoints under `/api/v1/agents/{agent_id}/threads/{thread_id}`
  require `get_conversation_owner` and `require_project_read_principal`.
- Message visibility and provenance checks use `_message_source_ids` and
  `filter_messages_for_scope` (`get_authorized_source_ids`), failing closed
  when provenance is malformed or any cited/knowledge source is unauthorized.
- Service-to-service endpoints use `require_bff_service_token` /
  `require_project_bff_principal`. Ordinary user JWTs must never access global
  platform feedback projections.

## Planned files

- `sag_api/core/errors.py`: add `BadRequestError` (`status_code = 400`) without
  changing existing `ValidationError` (`422`) semantics.
- `sag_api/schemas/agent.py`: expose optional `reply_to_message_id` and
  `regenerated_from_message_id` on `MessageOut`.
- `sag_api/schemas/answer_actions.py`: strict Pydantic schemas (`extra="forbid"`)
  for feedback upsert/read, shares, regenerate, and admin feedback pages.
- `sag_api/services/agent_domain.py` & `sag_api/services/agent_service.py`:
  preserve `reply_to_message_id` / `regenerated_from_message_id` in
  `filter_messages_for_scope`, `AskPlan`, `persist_answer`, and `generate_stream`;
  add `prepare_regenerate` using only authorized history strictly before the
  server-resolved parent user question.
- `sag_api/services/answer_actions_service.py`: feedback upsert/get/delete,
  share create/read/revoke with live source ACL revalidation, thread-level live
  run conflict guard (`409`), and HMAC-cursor-bound admin feedback query.
- `sag_api/api/v1/agents.py`, `sag_api/api/v1/answer_actions.py`,
  `sag_api/api/v1/__init__.py`: route wiring with `Cache-Control: no-store`.
- `tests/test_answer_actions_api.py`: behavioral and security tests written
  RED-first before implementation.

## Results

- **Actual changed/created files (`sag-api` on top of DB commit `4b16fc9`)**:
  - `SAG/apps/api/sag_api/core/errors.py`: added `BadRequestError` (`status_code = 400`, `code = ErrorCode.VALIDATION_ERROR`) while preserving `ValidationError` (`422`) for legacy endpoints.
  - `SAG/apps/api/sag_api/schemas/agent.py`: exposed optional `reply_to_message_id` and `regenerated_from_message_id` on `MessageOut`.
  - `SAG/apps/api/sag_api/schemas/answer_actions.py`: strict Pydantic v2 models (`extra="forbid"`) for `FeedbackUpsertIn` (camelCase + snake_case aliases, category/OTHER/length invariants), `FeedbackOut`, `OwnFeedbackOut`, `EmptyBodyIn`, `RegenerateIn`, `ShareOut`, `SharedAnswerOut`, and `AdminFeedbackPageOut`.
  - `SAG/apps/api/sag_api/services/agent_domain.py` & `SAG/apps/api/sag_api/services/agent_service.py`: preserved `reply_to_message_id` and `regenerated_from_message_id` across `filter_messages_for_scope`, `AskPlan`, `persist_answer`, and terminal SSE payloads (`run.completed`, `run.failed`, `run.cancelled`).
  - `SAG/apps/api/sag_api/services/answer_actions_service.py`: implemented `get_own_feedback`, race-safe `upsert_own_feedback`, idempotent `delete_own_feedback`, `create_or_get_share`, `read_project_share` (revalidating live `Message` + `SourceProjectMapping` `CONFIRMED` status via `_message_source_ids` and `get_authorized_source_ids`), creator-only `revoke_project_share`, deterministic `resolve_parent_question` (with safe legacy single-user-parent fallback and `400` on ambiguity), `prepare_regenerate` (strictly prior authorized turns only), mutual-exclusion `claim_thread_generation`/`release_thread_generation` (`409` on concurrent `ask`/`regenerate`), and `list_admin_feedback` (HMAC-SHA256 cursor bound to active filter hash + page-independent totals).
  - `SAG/apps/api/sag_api/api/v1/agents.py`, `SAG/apps/api/sag_api/api/v1/answer_actions.py`, `SAG/apps/api/sag_api/api/v1/__init__.py`: wired feedback, share, regenerate, and service-authenticated `/api/v1/platform/ai/feedback` routes with `Cache-Control: no-store`.
  - `SAG/apps/api/tests/test_answer_actions_api.py`: 5 end-to-end async ASGI behavioral/security test suites written RED-first.
- **Verification**:
  - `ruff check` on all new/modified API, schema, service, and test files → `All checks passed!`.
  - `python -X utf8 -m pytest tests/test_answer_actions_schema.py tests/test_answer_actions_api.py -q --tb=short` (verified Python 3.12 runtime) → `26 passed in 6.90s`.
  - `python -X utf8 -m pytest tests -m "not ai_smoke" -q --tb=short` (verified Python 3.12 runtime) → `179 passed, 1 failed` (the single failure is untouched pre-existing `tests/test_ecce_session_context.py::test_incremental_substrate_appends_delta_without_cache_wipe` due to hardcoded `2026-10-28` fixture timestamp vs `2026-10-30` wall clock; left untouched per workspace branch hygiene rules).
- **Database / Config / Security / Rollback Impact**:
  - Requires `feat/Thang-ai-chat-answer-actions-db` (`0010_ai_answer_actions.sql`, commit `4b16fc9`) applied before deploying these routes.
  - Uses existing `SAG_BFF_SERVICE_TOKEN` (`require_bff_service_token` / `require_project_bff_principal`) and `SECRET_KEY`; no new `.env` secrets required.
  - Rollback: revert the API deployment/PR first so no application traffic hits `answer_feedback` / `answer_shares` before considering any owner-approved DB downgrade.
- **Remaining Gaps**:
  - Staging/production end-to-end verification across deployed NestJS BFF + Next.js UI remains tracked in the BE (`feat/Thang-ai-chat-answer-actions-be-api`) and FE (`feat/Thang-ai-chat-answer-actions-fe-ui`) deliverables.

## Review-fix pass (PR #53 — Findings #1, #2, #3, #4)

### Verified findings and root causes
1. **[P1] Direct SAG share read/revoke bypasses live Project membership (`sag_api/api/v1/answer_actions.py:39`)**:
   - `require_project_share_principal` fell back to `require_project_read_principal` when `X-SAG-BFF-Service-Token` was absent, allowing a non-member with a PUBLIC-project or Org-ADMIN SAG JWT (or stale JWT after membership removal) to call `GET /api/v1/projects/{project_id}/shares/{share_id}` directly on SAG and receive `200`.
   - Fix: Enforce `require_bff_service_token` + `require_project_bff_principal` unconditionally on `project_share_router` (`GET` and `DELETE /projects/{project_id}/shares/{share_id}`), removing the direct user JWT fallback so all share reads/revocations must pass BFF live active-member verification.
2. **[P1] Shared answer still serves snapshot after cited Document/DocumentVersion is revoked (`sag_api/services/answer_actions_service.py:458`)**:
   - `revoke_project_document` (`document_service.py:2325`) sets `Document.is_active = False` and marks every `DocumentVersion` (`status`, `search_status`, `knowledge_status`) `"REVOKED"` while preserving `SourceProjectMapping.CONFIRMED`. Checking only `get_authorized_source_ids` allowed revoked document snapshots/snippets to remain readable.
   - Fix: Add `_verify_evidence_document_provenance` checking every internal citation/match across `Message.citations`, `AnswerShare.citations`, and `Message.steps[*].details.matches` against live `Document` (`is_active == True`, non-deleting status, tenant/project/source scope) and `DocumentVersion` (non-`REVOKED` `status`/`search_status`/`knowledge_status`, unrevoked metadata, and `security_partition_id` scope), failing closed (`404`) in both `_ensure_authorized_assistant_message` and `read_project_share`.
3. **[P2] Concurrent feedback/share race causes `500` (`MissingGreenlet` / `StaleDataError`) (`sag_api/services/answer_actions_service.py:277, 349, 382`)**:
   - Accessing expired ORM attributes after `session.rollback()` raised `MissingGreenlet`; concurrent `DELETE` between `SELECT` and ORM `UPDATE`/`DELETE` raised `StaleDataError`.
   - Fix: Use dialect-native atomic `INSERT ... ON CONFLICT (message_id, voter_subject) DO UPDATE ... RETURNING` in `upsert_own_feedback`, atomic SQL `DELETE` in `delete_own_feedback`, atomic `UPDATE` in `revoke_project_share`, and pre-extracted plain scalar variables + savepoint/retry loop in `create_or_get_share`.
4. **[P2] Legacy parent question fallback picks wrong user message on consecutive USER turns (`sag_api/services/answer_actions_service.py:210`)**:
   - `resolve_parent_question` only rejected `preceding[1].role == MessageRole.USER` when timestamps were identical, mis-resolving `q1(T), q2(T+1), a1(T+2)` to `q2`.
   - Fix: Fail closed (`400 BadRequestError`) whenever `preceding[1].role == MessageRole.USER` regardless of timestamp, or when another unlinked legacy assistant turn / explicit non-regeneration `reply_to_message_id` makes the legacy turn ambiguous.

### Review-fix verification
- `ruff check sag_api/api/v1/answer_actions.py sag_api/services/answer_actions_service.py tests/test_answer_actions_api.py` → `All checks passed!`.
- `python -X utf8 -m pytest tests/test_answer_actions_schema.py tests/test_answer_actions_api.py -q --tb=short` → `30 passed in 9.21s` (verified RED failure on all 4 new regression suites prior to fix, then GREEN).
- No new database migrations or environment variables introduced; builds on `4b16fc9`.
