# Platform AI Operations Telemetry — SAG Service Endpoint (`GET /api/v1/platform/ai/operations`)

## Scope and Provenance

- **Task**: Expose a service-only, zero-tenant-content operational telemetry endpoint on `SAG` so `DATN-BE` (`SagPlatformAiOperationsRepository`) can replace its V1 `'NOT_EXPOSED'` placeholders on the Admin (`PLATFORM_OPERATOR`) `/platform/ai` console with live operational runtime, job, and corpus counts.
- **Branch**: `feat/Thang-platform-ai-operations-be-api` (branched from `origin/main` at `2050103`).
- **Paired Deliverables**:
  - `DATN-BE` (`feat/Thang-platform-ai-operations-be-api`): consumes `GET /api/v1/platform/ai/operations` with `X-SAG-BFF-Service-Token`, updates `PlatformAiOperationsSummary` contract and OpenAPI (`docs/openapi/iam-v1.openapi.json`).
  - `DATN-FE` (`feat/Thang-platform-ai-operations-fe-ui`): displays live dependency states and safe operational telemetry cards on `/platform/ai`.

## Current Flow & Data Model Verified

- **Existing Public System Endpoints (`SAG/apps/api/sag_api/api/v1/system.py`)**:
  - `GET /api/v1/system/health` (`system.py:173`): process liveness probe returning `{"status": "ok"}`.
  - `GET /api/v1/system/ready` (`system.py:198`): checks PostgreSQL (`SELECT 1`), `app.state.knowledge_runtime.ready`, and Qdrant `_qdrant_ready(request)` (`/collections`).
  - `GET /api/v1/system/capabilities` (`system.py:151, 333`): returns `llm_configured`, `llm_provider`, `llm_model`, `embedding_model`, `effective_document_parser`, `sag_vector_provider`, etc.
- **ORM Enum Storage (`SAG/apps/api/sag_api/db/models/job.py` & `document.py`, `sag_api/enums.py`)**:
  - `Job.status` (`SAEnum(JobStatus, native_enum=False, length=16)`) persists uppercase enum member names (`QUEUED`, `RUNNING`, `PAUSED`, `SUCCEEDED`, `FAILED`) in PostgreSQL; SQLAlchemy ORM queries map cleanly via `JobStatus` enum keys.
  - `Document.status` (`SAEnum(DocumentStatus, native_enum=False, length=16)`) persists uppercase enum member names (`PENDING`, `LOADING`, `EXTRACTING`, `PAUSING`, `PAUSED`, `DELETING`, `DELETE_FAILED`, `READY`, `FAILED`); `Document.is_active` marks active documents (`True`).

## Security & Privacy Boundary (Strict Fail-Closed)

- **Service-Only Authentication (`sag_api/core/principal_assertion.py:497`)**:
  - `GET /api/v1/platform/ai/operations` MUST require `Depends(require_bff_service_token)` validating `X-SAG-BFF-Service-Token` via `secrets.compare_digest` against `settings.bff_service_token`.
  - Unauthenticated requests or ordinary Continuum user JWTs without `X-SAG-BFF-Service-Token` MUST fail closed with `403 ForbiddenError`.
  - Missing `settings.bff_service_token` MUST fail closed with `503 ServiceUnavailableError`.
- **Query Parameter Hygiene**:
  - Reject any query parameters on `GET /api/v1/platform/ai/operations` with `400 BadRequestError` (`platform_operations.py`).
- **Response Cache & Zero-Leak Projection**:
  - Always return `Cache-Control: no-store`.
  - Return ONLY deployment runtime identifiers (`llm_configured`, `llm_provider`, `llm_model`, `retrieval_model`, `vector_store`, `parser_provider`, `worker_concurrency`) and global integer counts by enum status.
  - Sanitize runtime identifier strings via `_safe_identifier` (strip/reject whitespace, URLs `http://` / `https://`, and secret-like keywords `api_key`, `apikey`, `secret`, `token`, `password`, `credential`, `bearer` → `None`).
  - **NEVER** expose tenant/organization/project IDs, document filenames, prompts, answers, raw error messages, base URLs, or credentials/API keys.

## Failure & Edge Cases

- If `app.state.knowledge_runtime` is absent or `ready` is `False`, or Qdrant `_qdrant_ready` returns `False`, report `status = "degraded"` while still returning verified DB counts (when DB is reachable) so Admin sees exact failure attribution.
- Exclude `Document.is_active == False` and `DocumentStatus.DELETING` from active corpus counts (`ready`, `processing`, `paused`, `failed`); combine `DELETE_FAILED` into `failed`.
- When DB probe/query fails inside `read_platform_operations`, propagate/fail rather than fabricating zeroes.

## Planned File-by-File Changes

1. `SAG/apps/api/sag_api/schemas/platform_operations.py` (new): Strict Pydantic v2 output schemas (`ConfigDict(extra="forbid")`).
2. `SAG/apps/api/sag_api/services/platform_operations_service.py` (new): Safe identifier sanitizer + ORM aggregate queries on `Job` and active `Document`.
3. `SAG/apps/api/sag_api/api/v1/platform_operations.py` (new): FastAPI router `/platform/ai` with `@router.get("/operations")` gated by `require_bff_service_token`, strict empty-query check, and `Cache-Control: no-store`.
4. `SAG/apps/api/sag_api/api/v1/__init__.py`: Register `platform_operations.router`.
5. `SAG/apps/api/tests/test_platform_operations_api.py` (new, RED-first): Auth (`403`/`503`), query validation (`400`), ORM aggregation accuracy, `is_active=False` & `DELETING` exclusion, secret/URL sanitization, and `degraded` runtime/Qdrant state checks.

## Out-of-Scope

- Any database schema/migration change.
- Any modification to document ingestion, indexing lanes, or per-tenant usage queries.
- Exposing per-organization or per-project document/job breakdowns without a separate contract.

## Acceptance Checklist

- [x] RED-first pytest written and confirmed failing prior to implementation.
- [x] Unauthenticated / user JWT calls rejected (`403`), missing BFF secret rejected (`503`), query params rejected (`400`).
- [x] `Cache-Control: no-store` set on response.
- [x] `jobs` and active `corpus` counts match `Job` / `Document` rows in DB.
- [x] Secret-like / URL config strings sanitized to `None`.
- [x] `pytest` passes on new suite and `tests/test_answer_actions_api.py` + `tests/test_answer_actions_schema.py`.

## Results

- **Actual changed/created files (`sag-laya-integration` on branch `feat/Thang-platform-ai-operations-be-api`)**:
  - `SAG/apps/api/sag_api/schemas/platform_operations.py`: added strict Pydantic v2 response models (`extra="forbid"`) for `PlatformRuntimeProfileOut`, `PlatformJobSummaryOut`, `PlatformCorpusSummaryOut`, and `PlatformAiOperationsOut`.
  - `SAG/apps/api/sag_api/services/platform_operations_service.py`: added `_safe_identifier` sanitizer (rejects whitespace, URLs, secret keywords) and `read_platform_operations` performing aggregate ORM queries on `Job` and active `Document` rows.
  - `SAG/apps/api/sag_api/api/v1/platform_operations.py`: added `GET /api/v1/platform/ai/operations` gated unconditionally by `require_bff_service_token`, rejecting arbitrary query params (`400`), reading `knowledge_runtime` and `_qdrant_ready`, and responding with `Cache-Control: no-store`.
  - `SAG/apps/api/sag_api/api/v1/__init__.py`: registered `platform_operations.router`.
  - `SAG/apps/api/tests/test_platform_operations_api.py`: 4 async ASGI behavioral and security tests written RED-first (confirmed 4 failed with `404` before implementation).
- **Verification**:
  - `python -X utf8 -m py_compile` on all 5 new/modified Python files -> exit code `0`.
  - `pytest tests/test_platform_operations_api.py tests/test_answer_actions_schema.py tests/test_answer_actions_api.py -q --tb=short` -> **`51 passed in 18.46s`**.
- **Database / Configuration / Security / Rollback Impact**:
  - Zero PostgreSQL schema, column, index, or migration changes. Read-only `GROUP BY` queries against `jobs` and `documents`.
  - Reuses existing `SAG_BFF_SERVICE_TOKEN` (`settings.bff_service_token`); no new environment variables added.
  - Rollback: revert PR/commit on `sag-laya-integration`; `DATN-BE` falls back cleanly to `'NOT_EXPOSED'` / `'UNKNOWN'` when the endpoint is absent.
