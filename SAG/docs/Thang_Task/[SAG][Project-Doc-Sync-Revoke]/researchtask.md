# [SAG][Project-Doc-Sync-Revoke] Project Source Mapping, Safe Version Replacement (`v1 -> v2`), and Project-Scoped Revocation

## Task source

User-approved implementation task before running the Continuum Project Documents -> SAG Pilot sync.
Related plans and checklists: [`SAG/tasks/plan.md`](../../../tasks/plan.md) and [`SAG/tasks/todo.md`](../../../tasks/todo.md).

## Purpose

Close the architectural, consistency, and concurrency gaps in the Project-scoped document ingestion and lifecycle pipeline before executing the live Pilot sync for Continuum Project Documents:

1. **Option B Project Source Mapping (`CONFIRMED`)**: Ensure auto-provisioned Project Sources (`is_project_source=True`) receive a `CONFIRMED` `SourceProjectMapping` so Agent and Project search (`include_project_sources=True`) and `_load_current_ready_versions_checked` authorize them while `list_sources` keeps them hidden from the general Notebook UI list.
2. **Safe `v1 -> v2` Version Replacement & PostgreSQL <-> Qdrant Consistency**:
   - Do not prematurely expire `v1.valid_to` at upload time (`handle_document_upload`) while `v2` is still `RECEIVED`/`PENDING`; retain `v1` as `SEARCH_READY` if `v2` fails indexing or rolls back.
   - Fix `run_search_indexing_stage` where `SUPERSEDED` pruning called undefined `delete_search_units_from_qdrant` and `logger.warning` instead of `delete_document_points_from_qdrant(qdrant_client, project_id=project_id, document_version_ids=[str(superseded_version_id)])` and `log.warning`.
   - Commit the PostgreSQL readiness and temporal supersede handoff (`v2.search_status = "SEARCH_READY"`, `v1.valid_to = cur_start`) **before** pruning `v1` points from Qdrant so a PostgreSQL rollback never leaves `v1` active in PostgreSQL with its Qdrant points already deleted.
3. **Project-Scoped Revocation Endpoint & In-Flight Worker Race Protection**:
   - Add `POST /api/v1/projects/{project_id}/documents/revoke` (and `DELETE /api/v1/projects/{project_id}/documents/{document_id}`) on `project_router` enforcing principal assertion / BFF service scope (`tenant_id`, `project_id`, `security_partition_id`).
   - Immediately close active `DocumentVersion.valid_to = now_utc`, set `Document.is_active = False` and `Document.status = DocumentStatus.DELETING`, signal active `PROCESS_DOCUMENT` jobs to pause, and schedule `DELETE_DOCUMENT` to remove Qdrant vectors, engine records, and DB rows.
   - Guard `run_search_indexing_stage` and `_process_document_unlocked` so an in-flight worker on a revoked/deleting document never publishes `SEARCH_READY`, never reopens `valid_to`, and cleans up any uncommitted Qdrant points for the aborted version.

## Scope & Affected Files

- `SAG/apps/api/sag_api/schemas/routing_rag.py`: Add `DocumentRevokeRequest` and `DocumentRevokeResponse`.
- `SAG/apps/api/sag_api/services/document_service.py`:
  - `get_or_create_project_source`: Create/ensure `CONFIRMED` `SourceProjectMapping` for `(source_id, organization_id, project_id)`.
  - `handle_document_upload`: Preserve `latest_ver.valid_to` until `v2` completes manifest verification in `resolve_temporal_supersedes`; reject uploads onto documents in `DELETING`/`DELETE_FAILED` state.
  - `revoke_project_document`: Project-scoped idempotent revocation by `logical_source_id` or `document_id`.
- `SAG/apps/api/sag_api/api/v1/documents.py`: Expose `POST /api/v1/projects/{project_id}/documents/revoke` and `DELETE /api/v1/projects/{project_id}/documents/{document_id}`.
- `SAG/apps/api/sag_api/services/dedup_and_temporal_service.py`: Ensure monotonic interval boundary when `v2` supersedes `v1` even under sub-millisecond/identical timestamps.
- `SAG/apps/api/sag_api/services/search_index_service.py`:
  - Lock and verify `Document` state (`is_active`, not `DELETING`/`DELETE_FAILED`/`PAUSING`/`PAUSED`) before publishing `SEARCH_READY`.
  - Commit PostgreSQL readiness and temporal handoff before calling `delete_document_points_from_qdrant` for `superseded_version_id`.
  - Clean up partial Qdrant points for `document_version.id` if manifest verification or commit fails or if the document was paused/revoked in flight.
- `SAG/apps/api/sag_api/jobs/tasks.py`: Check `not document.is_active` alongside `_DELETE_CONTROL_STATES` / `_CONTROL_TRANSITION_STATES` across stage callbacks, `should_pause`, `_pause_or_yield`, and completion transitions.
- `SAG/apps/api/tests/test_project_doc_sync_and_revoke.py`: Automated tests covering all 4 acceptance scenarios.

## Acceptance Criteria

1. `get_or_create_project_source` provisions a `CONFIRMED` `SourceProjectMapping`; `search_source_candidates(..., include_project_sources=True)` and `_load_current_ready_versions_checked` include the Project Source while `list_sources` excludes it.
2. Uploading `v2` for an existing `logical_source_id` keeps `v1` searchable while `v2` is `PENDING` or if `v2` fails indexing/commit; once `v2` is committed `SEARCH_READY`, `v1.valid_to` is closed, `v1` Qdrant vectors are pruned via `delete_document_points_from_qdrant`, and only `v2` is returned by `_load_current_ready_versions_checked`.
3. Calling project-scoped revocation (`POST /api/v1/projects/{project_id}/documents/revoke`) immediately excludes the document from `_load_current_ready_versions_checked`, schedules `DELETE_DOCUMENT` to delete Qdrant vectors and DB records, and returns `ALREADY_REVOKED` idempotently on repeat calls.
4. If revocation occurs while `run_search_indexing_stage` / `process_document` is in flight, the worker aborts publication (`JobPaused`), rolls back uncommitted PostgreSQL state, and cleans up Qdrant vectors for the in-flight version so the revoked document never becomes searchable.

## Status

`DONE`

## Actual Changed Files

- `SAG/apps/api/sag_api/schemas/routing_rag.py`
- `SAG/apps/api/sag_api/services/document_service.py`
- `SAG/apps/api/sag_api/api/v1/documents.py`
- `SAG/apps/api/sag_api/services/dedup_and_temporal_service.py`
- `SAG/apps/api/sag_api/services/search_index_service.py`
- `SAG/apps/api/sag_api/jobs/tasks.py`
- `SAG/apps/api/tests/test_project_doc_sync_and_revoke.py`

## Verification Results

- `.\.venv\Scripts\python.exe -m pytest -v`: **33 passed, 1 skipped** (including all 5 tests in `tests/test_project_doc_sync_and_revoke.py` and 15 tests in `tests/test_audit_remediation.py`).
- `.\.venv\Scripts\python.exe -m ruff check tests/test_project_doc_sync_and_revoke.py sag_api/schemas/routing_rag.py sag_api/services/document_service.py sag_api/api/v1/documents.py sag_api/services/dedup_and_temporal_service.py sag_api/services/search_index_service.py sag_api/jobs/tasks.py`: **All checks passed!**
