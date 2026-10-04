# [SAG][Checkpoint C] Research Record

## User-provided task

**Purpose:** Publish verified tree snapshots without exposing mixed versions to concurrent queries.

**Scope:**
- Build the candidate into the inactive routing slot and update Qdrant dual-slot payloads using acknowledged writes.
- Verify the PostgreSQL manifest, expected point counts, checksum, quality gates, and ACL smoke checks before switching the active pointer.
- Switch active tree version/slot and search epoch atomically in PostgreSQL.
- Pin tree/search/ACL snapshot once per request and pass it through routing and retrieval.
- Prevent inactive-slot reuse while queries pinned to its previous contents may still be running.

**Acceptance:**
- Verification failure never switches the active pointer.
- Concurrent requests see a complete old or new snapshot, never a mixture.
- Qdrant slot and PostgreSQL manifest/pointer identify the same tree version after publish.
- Add focused tests and evidence.

**Dependencies:** DATN-58 candidate update/subtree build contract; DATN-35 / `ROUTING_READY` is complete.

## Base and repository instructions

- Repository: `sag-laya-integration`; task branch `feat/Thang-checkpoint-c-blue-green-be-api-db` is based on fetched `origin/main` at `0985929` (2026-10-04). `main` is checked out in sibling worktree `sag-laya-main-after-16`, is clean, and `git pull --ff-only origin main` reported up to date.
- Existing edits on `feat/Thang-acl-runtime-be-api-db` were preserved in `stash@{0}` before branch creation. They are unrelated to this task and are not part of this branch.
- Workspace rules and repository README were read. No repository-local `AGENTS.md`, `GEMINI.md`, or `CLAUDE.md` was found. API README documents `pytest` and `ruff check .` for the Python service.
- `SAG/tasks/plan.md`, `SAG/tasks/todo.md`, Workflow v1.1, Phase 0 contract notes, API models/services/tests, migrations, and local Git history were inspected.

## Verified current flow and contracts on the task base

1. `services/search_unit_retrieval_service.py::retrieve_search_unit_sections` loads only verified `SEARCH_READY` versions through `_load_current_ready_versions_checked`. The DB query checks the verified principal's organization, allowed projects and partitions, confirmed Project→Source mapping, tenant, active document/version window, latest successful index attempt, and manifest counts/checksum. It groups those results by Project, Source, tenant, and partition.
2. `services/query_routing_service.py::capture_routing_decisions` makes one provider call for all groups, validates a frozen request snapshot and exact scope fingerprints, then returns immutable per-group `RouteDecision`s. On the task base, `EngineManager` has no `get_routing_snapshot` provider, so production routing currently takes the safe global-search fallback.
3. `_query_group` passes the route's tree version, slot and membership into `sag/search_unit_store.py::build_search_filter`. That filter ANDs tree predicates with `project_id`, `tenant_id`, `security_partition_id` and document-version ACL predicates. Global escape omits tree predicates but retains canonical ACL predicates. Retrieval rechecks authorized/current versions after Qdrant and before evidence hydration.
4. `services/routing_tree_service.py::build_routing_snapshot` produces an immutable in-memory Phase 6 `RoutingSnapshot` with deterministic tree version, nodes/profiles, manifest JSON, metrics, quality gates, publishability and lineage. It does not persist candidate state or update Qdrant.
5. `services/search_index_service.py` pre-provisions A/B payload values (`primary_node_*`, `secondary_node_ids_*`, `tree_version_*`) as empty on SearchUnit points. The search filter already understands dual slots; a production writer/publisher is absent from `origin/main`.
6. `db/models/routing_rag.py` defines `ProjectSearchState` (both slot versions, active slot/version, previous version, search epoch, switch time) and `TreeManifest` (counts, quality metrics, status and checksum). `TreeManifest` lacks serialized manifest content. No query-lease model exists. Checked-in `migrations/0001_phase_0_routing_rag_schema.sql` does not create these control-plane tables; `core/db.py::init_db` uses ORM `create_all` for development. The API README says production uses Alembic, but no Alembic environment/revisions were found in `SAG/apps/api`.
7. `VerifiedPrincipal` in `core/principal_assertion.py` carries signed organization/project/partition scope and assertion `token_id`. Search scope fingerprints bind the exact source/version/tenant/partition set, but the current request snapshot contract has no explicit ACL epoch or persisted pin lifetime.

## Related branch/history audit (not merged into the task base)

- Local remote ref `origin/feat/Tai-checkpoint-c-incremental-ready` points to `da05d25` (`feat(incremental-tree): implement Checkpoint C INCREMENTAL_READY with dual-slot Qdrant and atomic publish`), ahead of `origin/main`. It adds an incremental service, manifest JSON, an EngineManager provider, tests and evidence. This is relevant prior work, not an implementation present on the required fresh base.
- That commit's `verify_inactive_slot_manifest` checks the in-memory snapshot and optional Qdrant count; it does not read/verify the persisted PostgreSQL manifest or run an ACL smoke check before publish. `execute_atomic_tree_publish` accepts a caller-supplied slot and does not itself require a verified candidate. Its Qdrant writer uses `wait=true` but accepts HTTP 200 without validating the operation result payload.
- Its `EngineManager.get_routing_snapshot` reads state and manifest without request leases. The reported `test_concurrent_queries_read_isolated_consistent_snapshot` captures one Python snapshot, publishes sequentially, then routes the retained object; it does not overlap a live Qdrant retrieval or test a second publish reusing a slot while the first query still runs. The adjacent evidence report claims 47 tests passed on another checkout/date; those results have not been rerun or treated as production acceptance here.
- Reuse should be limited to verified contracts/patterns; do not copy the branch's broad DATN-58/59 implementation into this task branch. The task should consume a candidate snapshot at the publish boundary.

## Security and data boundaries

- PostgreSQL is the control-plane source of truth; Qdrant is an index. A route filter can only narrow the already-authorized project/tenant/partition/version scope.
- Snapshot construction must validate that state slot, slot version, active version, active manifest project/version/status/checksum, and search epoch agree before exposing profiles. Invalid/incomplete data must fall back to the canonical global query path.
- Persist a per-request lease for each pinned project/slot/version and release it after all tree-filtered Qdrant reads finish. Expired leases may be reclaimed only after a hard retrieval deadline plus margin. A publisher must serialize per project, choose the inactive slot from locked/current control state, and refuse to mutate a slot with a live lease. This needs to work across API processes, so process-local locks alone are insufficient.
- ACL smoke checks must exercise at least two distinct partition/principal scopes and prove that Qdrant filters retain all canonical ACL predicates while adding only candidate-tree membership. Candidate profiles and memberships outside the requested authorized scope must be rejected or pruned.
- The existing post-Qdrant authorization/version recheck remains a revocation safety check; it must not refresh the pinned tree/search snapshot halfway through the request.

## Known gaps and conflicts

- The task input says DATN-35 / `ROUTING_READY` is complete. At `origin/main`, `SAG/tasks/todo.md` still leaves Checkpoint B incomplete and states that B1/provider, Phase 5 integration, principal/leakage evidence and agreed recall thresholds remain open. Preserve the user-provided dependency as task context, but do not mark broader `ROUTING_READY` or operational acceptance complete without new evidence.
- DATN-58's incremental candidate/subtree contract is not merged into `origin/main`; the related remote branch includes an implementation but is separate. This branch consumes the existing Phase 6 candidate `RoutingSnapshot` plus an explicit `SearchUnitAssignment` bridge because KnowledgeUnit and SearchUnit are distinct contracts. The DATN-58 producer is still not wired.
- The current DB migration story for control-plane tables is incomplete in checked-in SQL. Add a new migration; do not rewrite `0001`. Production migration execution/configuration remains to be confirmed during validation.
- No production PostgreSQL/Qdrant credentials or service were used during research. Mock/SQLite tests cannot certify deployment behavior.

## Proposed implementation files

- `SAG/apps/api/sag_api/db/models/routing_rag.py`: persist the complete candidate manifest and add a durable request-slot lease model with expiry/indexes.
- `SAG/apps/api/sag_api/core/db.py`: add the development/SQLite additive column upgrade needed by the model.
- `SAG/apps/api/migrations/0002_checkpoint_c_publish_snapshots.sql`: create/upgrade the PostgreSQL control-plane tables/manifest payload and lease table idempotently. Keep `0001` immutable; include rollback guidance.
- `SAG/apps/api/sag_api/services/tree_publish_service.py`: stage the inactive candidate manifest, clear stale inactive-slot keys and write Qdrant batches with acknowledged completion, verify persisted PG manifest + checksum + expected exact point count + quality/ACL smoke gates, then switch manifest/pointer/slot/search epoch in one PG transaction. Serialize concurrent publishers and reject/retry reuse of a leased slot.
- `SAG/apps/api/sag_api/services/tree_query_snapshot_service.py` plus `sag/engine_manager.py`: provide one request snapshot over all authorized scopes in a consistent DB transaction, acquire durable leases atomically with reading pointers, and release the request lease.
- `SAG/apps/api/sag_api/services/search_unit_retrieval_service.py`: keep that lease through routing and all local/escape Qdrant reads, release in `finally`, and continue passing the same frozen scope/route through retrieval.
- `SAG/apps/api/tests/`: focused Checkpoint C tests for verification failure, acknowledged writes, atomic pointer/epoch, ACL parity, multi-scope snapshot, live-query slot retention, failure recovery and reuse after release/expiry.
- `SAG/docs/Thang_Task/[SAG][Checkpoint C]/researchtask.md`, task-local evidence, and relevant Phase 8 checklist entries in `SAG/tasks/todo.md`; update task/architecture docs only for verified behavior.

## Test matrix

| Area | Focused evidence |
|---|---|
| Candidate/manifest gate | Stored PG manifest/version/checksum mismatch, wrong project/status, failed quality gate, ACL smoke failure, Qdrant partial/error/not-completed acknowledgement, and exact count mismatch all leave the active pointer and epoch unchanged. |
| Atomic publish | Active slot/version and manifest status switch together; epoch increments once; wrong/stale target slot and concurrent publishers cannot produce mismatched pointers. |
| Request snapshot | Multiple projects/scopes captured once; every group has one timestamp/request ID and exact ACL fingerprint; routing and branch-local/escape retrieval use that same snapshot. |
| Slot lifetime | Query Q pins slot A; publish switches A→B; another publish cannot overwrite A until Q's reads finish/release. New queries use B. Expired crashed-request leases recover only after the configured hard deadline. |
| ACL | Two partitions/principals; local route membership is ANDed with exact project/tenant/partition/version filters; mismatched profiles fail closed to authorized global escape. |
| Regression | Existing query-routing, SearchUnit Qdrant filter, canonical search readiness, and search stream/tool caller contracts remain intact. |

Repository test commands are documented as `cd SAG/apps/api && pytest` and `ruff check .`; focused tests should run first, then the relevant suite. PostgreSQL-specific lock/migration evidence must be called out separately from SQLite/mocked tests.

## Out of scope

- Implementing DATN-58 delta assignment or DATN-59 subtree/lineage algorithms; consume the Phase 6 candidate snapshot at the publish boundary.
- Normal-ingestion/Phase 5 knowledge-unit producer integration, changes to unrelated ACL/source mapping contracts, frontend changes, major vector-model migration/collection aliasing, and benchmark threshold sign-off.
- Claiming staging/production readiness from unit or mocked transport tests.

## Acceptance checklist

- [x] Failed Qdrant acknowledgement/count/ACL payload verification leaves the active pointer and epoch unchanged in focused SQLite + mocked-Qdrant regressions; PostgreSQL runtime behavior remains unverified.
- [x] Successful focused publish leaves the mocked Qdrant slot and SQLite active slot/version/manifest aligned; real PostgreSQL/Qdrant acceptance remains open.
- [ ] A request uses one frozen tree/search/ACL scope across routing and retrieval; no response can mix versions.
- [ ] A live request lease prevents reuse of the slot/version it pinned across API workers. The focused coroutine-overlap regression demonstrates the lease guard on SQLite; multi-process PostgreSQL locking remains unverified.
- [ ] Concurrent publishers serialize and stale slot selection cannot publish.
- [x] Focused tests and evidence distinguish mock/SQLite checks from PostgreSQL/Qdrant runtime acceptance.
- [x] `SAG/tasks/todo.md` and this record report only evidence-backed scope; broader Checkpoint B status remains separately stated.

## Implementation and validation (2026-10-04)

- Implemented the publish boundary with a frozen explicit KnowledgeUnit→SearchUnit bridge, persisted canonical manifest/checksum, PostgreSQL staged-manifest and source/version/partition checks, Qdrant completed-write acknowledgements, exact point/partition counts, point-payload ACL smoke checks, and one transactional active slot/version/manifest/search-epoch switch.
- Before reusing a lease-free inactive slot, the publisher deletes that slot's three tree payload keys from points filtered by project and tenant, waits for Qdrant completion, writes the candidate, and verifies the resulting candidate point inventory. This removes orphaned payload from SearchUnits absent in a later candidate while leaving the other slot intact. Endpoint contract: [Qdrant delete-payload API](https://api.qdrant.tech/api-reference/points/delete-payload).
- Query capture validates all project pointers and manifest identity/checksum columns in one transaction without loading the full manifest JSON. It reads checksummed, indexed profiles scoped to authorized Source/version/partition combinations; the same exact IDs, tenant and partition scope are pinned into route decisions and retained in the canonical Qdrant retrieval filter. Durable expiring slot leases are released after local/escape retrieval completes. Invalid routing snapshots release an identifiable lease and fall back to global retrieval.
- A focused regression injects an orphaned Qdrant point during slot reuse and asserts the inactive slot's old tree keys are removed while active-slot payload remains unchanged. Other focused cases cover failed count, bad acknowledgement, wrong ACL payload, multi-scope profile filtering, concurrent snapshot/publish overlap, and blocked slot reuse until lease release.
- Focused command `python -m pytest tests/test_checkpoint_c_publish.py tests/test_search_unit_retrieval_service.py tests/test_query_routing_service.py -q`: **35 passed** after review fixes.
- Changed feature/model/test lint command: **All checks passed**. `core/db.py` has two pre-existing `E501` findings on untouched lines; `ruff check core/db.py --ignore E501` is used to validate its one-line additive change separately.
- Syntax compile: `python -m compileall -q sag_api` **passed**. Package build: `uv build --out-dir <temporary-directory>` produced both sdist and wheel.
- Full API command `python -m pytest -q --tb=no`: **867 passed, 30 failed, 1 skipped, 108 warnings** in 229.85s. None of the failures are in the Checkpoint C focused suites; existing failures span ACL monkeypatch expectations, Agent/stream, document ingestion/lifecycle, DSH platform assumptions, settings, and OCTX/Windows permissions. The full run first exposed an SQLite index-upgrade regression in an OCTX fixture; the index now skips when its target table is absent, and the specific regression test passes after the fix.
- Repository-wide Ruff reported **165 findings** across the existing API codebase; the focused changed-file set passes as described above.
- No PostgreSQL service/`psql` or Qdrant server was available for deployment-level migration, advisory-lock, or live protocol verification. SQLite and `httpx.MockTransport` tests are evidence for code paths only. `SAG/tasks/todo.md` leaves Phase 8 and Checkpoint C readiness unchecked, and Checkpoint B remains independently incomplete at the task base.
- Code review found and fixed a P1 idempotency invariant: re-publishing the already-active tree previously returned success without confirming that the PostgreSQL active slot's slot-version field matched the active tree version. `_active_slot` now validates the pair on both idempotent publish and inactive-slot selection; `test_idempotent_publish_rejects_active_slot_manifest_mismatch` covers the corrupted-state case.
- Code review also found a per-request cost issue: snapshot capture loaded the full manifest JSON and enforced the configured row limit only after reading profile rows. The hot path now reads only manifest identity columns plus a checksummed indexed Source/version/partition projection, with the profile limit applied in SQL; `test_query_snapshot_fails_closed_on_corrupt_indexed_profile` covers projection integrity.
- A follow-up scope review found profile signals initially mixed across document versions for one Source/partition. Profiles are now keyed and filtered by the exact pinned version IDs; `test_query_snapshot_does_not_mix_profile_signals_across_document_versions` covers the regression.
- The first full API run after adding the indexed profile table exposed that a development index upgrade could run before that table existed in a partial SQLite fixture. `_ensure_indexes` now checks table existence before creating that new index; the failing OCTX regression passes, and the final full run has no failures in the Checkpoint C suites.
- The new SQL migration `0002_checkpoint_c_publish_snapshots.sql` is additive; it creates the control-plane state/manifest/lease and routing-profile schema and adds `manifest_json`. Rollback guidance intentionally retains these additive records. Production migration execution and rollback still require operator validation.

## Follow-up PR review fixes (2026-10-04)

PR #20 received four follow-up comments. Persisted routing-profile verification now sorts by the same four-key tuple used by candidate construction, including `document_version_id`. Query snapshot capture now uses `settings.search_tree_profile_limit` instead of a second hardcoded 1,024 threshold. The review regression at 1,025 profiles also found `GroupRoutingSnapshot.profiles` had an independent 1,024 maximum; it now uses the configured limit, keeping capture and DTO validation consistent. A duplicate staged-candidate verification immediately before `_atomic_switch` was removed because the switch transaction re-reads the manifest and profiles under row locks.

For publisher serialization, the PostgreSQL session advisory lock still spans Qdrant network I/O because releasing it between short database transactions would allow competing publishers to mutate the same inactive slot. Its connection now comes from a dedicated pool (`pool_size=4`, no overflow, one-second checkout timeout), keeping long Qdrant waits out of the API request/session pool. Timeout translation is scoped only to that connection checkout, so an unrelated request-pool timeout is not mislabeled. A saturated lock pool fails the publish quickly with `TreePublishInProgress`; an unlock error invalidates the connection so a possibly-held session lock is not returned to the pool. This adds capacity for up to four lock connections per API process, but adds no schema or environment setting.

Regression evidence: `test_stored_profile_verification_orders_document_versions_before_nodes` fails when `document_version_id` is omitted and passes with the complete ordering. `test_query_snapshot_honors_configured_profile_limit_above_1024` verifies a pinned 1,025-profile snapshot at a configured cap of 2,048. The focused Checkpoint C/retrieval/routing command returned **37 passed**. Changed Python files passed focused Ruff, `core/db.py` passed Ruff with pre-existing E501 lines ignored, `compileall` passed, and `uv build` produced an sdist and wheel outside the repository.

PostgreSQL runtime pool behavior remains unverified: the local API environment lacks optional `asyncpg`, and no PostgreSQL service was available. Qdrant remains mock-transport evidence only. The follow-up has no database migration or external configuration impact; application rollback is reverting the follow-up code commit. Existing operational gaps (DATN-58 integration, Checkpoint B status, and staging verification) remain open. The changes continue on `feat/Thang-checkpoint-c-blue-green-be-api-db` and PR #20: https://github.com/DATN-SPRING2027/sag-laya-integration/pull/20.
