# [SAG][Checkpoint C] Implementation evidence

**Date:** 2026-10-04
**Repository:** `sag-laya-integration`
**Branch:** `feat/Thang-checkpoint-c-blue-green-be-api-db`
**Base:** `origin/main` at `0985929`
**Commit:** see the task branch and linked pull request.

## Changed behavior

- Publishes a Phase 6 candidate plus explicit `SearchUnitAssignment` bridge into the inactive Qdrant slot. It clears only the target slot's routing payload keys for the candidate project/tenant, waits for the delete and each payload write to complete, verifies exact point/partition counts and point payload identities, and switches the PostgreSQL manifest/pointer/epoch in one transaction.
- Persists canonical manifest JSON, checksummed per-Source/version/partition routing-profile projections, and request-slot leases. Publisher serializes per project and refuses to reuse a slot with a live lease. Query capture reads only indexed compact profiles (not the full manifest JSON), validates PostgreSQL pointer/manifest/checksum alignment, captures all project scopes together, and keeps the request lease until local and global escape Qdrant reads finish. Only exact authorized document-version profile rows are loaded, and the same IDs remain pinned into routing and the canonical Qdrant retrieval filter.
- A Qdrant mock regression creates an orphaned point before slot reuse. It verifies that the old target-slot tree payload is removed and the other slot's payload remains untouched.

## Code review

- **P1 found and fixed:** an idempotent publish previously accepted `active_tree_version == candidate.tree_version` and returned success without checking that the active slot's `slot_a_tree_version` / `slot_b_tree_version` matched that pointer. The request reader would then reject the pointer and silently fall back globally. `_active_slot` now validates the slot/version invariant before returning an idempotent success; a regression corrupts the stored slot version and proves publish fails closed.
- **Performance finding fixed:** query capture previously loaded and scanned the full serialized manifest—including vectors and assignments—on every request, and the configured profile limit was enforced only after fetching rows. Query capture now selects only manifest identity columns and reads a checksummed, indexed Source/version/partition projection with a SQL row limit; the fail-closed regression detects a corrupted projection.
- **Snapshot-scope finding fixed:** the compact projection initially aggregated profiles across document versions belonging to the same Source/partition. It now keys and fetches each profile by Source/version/partition; `test_query_snapshot_does_not_mix_profile_signals_across_document_versions` injects a stale-version profile and confirms the pinned version's routing scores are unchanged.
- Review also checked Qdrant payload identity/filtering, ACL scope fingerprints and canonical version filters, lease acquisition/release, failure paths, SQL parameterization, and bounded profile reads. No direct ACL bypass or secret exposure was found in this change.

## Follow-up PR review #20 (2026-10-04)

- Added `document_version_id` to the persisted-profile verification order so it matches candidate ordering `(source_id, document_version_id, partition_id, node_id)`. `test_stored_profile_verification_orders_document_versions_before_nodes` reproduces the false-negative when that key is omitted.
- Replaced the query-side 1,024 threshold with `settings.search_tree_profile_limit`. The same review exposed a second 1,024 cap in `GroupRoutingSnapshot.profiles`; that DTO cap now uses the configured limit too. `test_query_snapshot_honors_configured_profile_limit_above_1024` captures a request with 1,025 profiles under a configured limit of 2,048.
- Kept the PostgreSQL session advisory lock across Qdrant writes so publishers for one project remain serialized, and moved it to a dedicated pool with four connections, no overflow, and a one-second checkout timeout. A busy lock pool returns `TreePublishInProgress` without consuming request/session-pool connections; only lock-pool checkout timeouts are translated, so request-pool errors retain their original cause. If unlock fails, the connection is invalidated so a session lock cannot be returned to the pool. This reserves up to four additional PostgreSQL connections per API process.
- Removed the duplicate `_verify_staged_candidate` read before `_atomic_switch`; the switch transaction still re-reads and verifies the persisted manifest and profile rows under row locks.
- No schema migration or new environment setting was added by this follow-up. PostgreSQL pool behavior has not been exercised against a live service; `asyncpg` is absent from the local API virtual environment.

## Checks run

From `SAG/apps/api`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_checkpoint_c_publish.py tests/test_search_unit_retrieval_service.py tests/test_query_routing_service.py -q
```

Result: **35 passed** on the original implementation review.

After the four new PR comments and the adjacent DTO limit finding were fixed, the same focused command returned **37 passed**. The two follow-up regressions also passed individually. The ordering regression fails when `document_version_id` is temporarily omitted, confirming that it exercises the reported mismatch.

The changed feature/model/test files passed Ruff. `sag_api/core/db.py` contains two pre-existing `E501` findings on unchanged lines; its one-line additive change passed `ruff check sag_api/core/db.py --ignore E501`.

```powershell
.\.venv\Scripts\python.exe -m compileall -q sag_api
```

Result: passed.

`uv build --out-dir <temporary-directory>` produced `sag_api-0.1.0.tar.gz` and `sag_api-0.1.0-py3-none-any.whl` outside the repository.

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=no
```

Result: **867 passed, 30 failed, 1 skipped, 108 warnings** in 229.85s. None of the failures are in the Checkpoint C focused suites. Existing failures span ACL monkeypatch expectations, Agent/stream behavior, document ingestion/lifecycle, DSH platform assumptions, settings, and OCTX/Windows permissions; the detailed test names are in the test-run output. An initial run caught one new SQLite fixture regression from the profile index; the index upgrade now checks that its table exists, and `test_existing_sqlite_documents_gain_octx_columns_and_active_index` passes separately.

Repository-wide `ruff check .` reported **165 findings** across the API codebase. The focused changed-file set passed.

## Evidence boundary and open validation

- Publisher and query lease tests use SQLite and `httpx.MockTransport`; they are not a live PostgreSQL or Qdrant integration run.
- No local PostgreSQL service/`psql` or Qdrant server was available. PostgreSQL DDL, row/advisory-lock behavior, and live Qdrant operation completion still need staging verification.
- The Checkpoint C publisher accepts the Phase 6 snapshot with an explicit KnowledgeUnit→SearchUnit bridge. DATN-58's producer/update contract is not on `origin/main`, so normal ingestion/subtree integration is still open.
- `SAG/tasks/todo.md` keeps Phase 8 and Checkpoint C readiness unchecked. The task-base Checkpoint B / `ROUTING_READY` discrepancy remains recorded in `researchtask.md`.

## Database and rollback

New additive migration: `SAG/apps/api/migrations/0002_checkpoint_c_publish_snapshots.sql`. It creates PostgreSQL project search state, snapshot lease, and indexed Source/version/partition routing-profile tables, stores canonical manifest JSON, and adds indexes. Migration `0001` was not edited. The migration documents retaining the additive control-plane rows and columns during application rollback; production rollback and data retention policy still need operator confirmation.

## Qdrant API reference

The inactive-slot cleanup uses Qdrant's official delete-payload endpoint with a filter and `wait=true`: [Delete payload API](https://api.qdrant.tech/api-reference/points/delete-payload).
