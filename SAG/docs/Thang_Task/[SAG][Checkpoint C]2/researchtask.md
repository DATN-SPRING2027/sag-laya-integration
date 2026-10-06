# [SAG][Checkpoint C] Failure injection, rollback and acceptance evidence — Research Record

## Task and base

- Task spec: `[SAG][Checkpoint C]2.md` in this folder; user supplied the purpose, scope, acceptance, and dependencies DATN-58 / DATN-59.
- Repository: `sag-laya-integration`.
- Branch: `feat/Thang-checkpoint-c-failure-rollback-be-api`.
- Base: fetched `origin/main` at `0102e1ec003be3f41923c6923f9333ea5875fa23` (Checkpoint C blue-green implementation PR #19). `git pull --ff-only origin main` reported already up to date.
- Existing untracked artifacts in the prior Checkpoint C folder and this task folder were preserved. `research.md` in this task folder is an existing directory, so this record uses the mandated `researchtask.md` filename without replacing it.
- Workspace `.agents/rules/` and repository root/SAG/API READMEs were inspected. No nested `AGENTS.md`, `GEMINI.md`, or `CLAUDE.md` was found. The Rule 05-required `PROJECT_MEMORY.md` does not exist in this checkout; `SAG/tasks/plan.md` and `SAG/tasks/todo.md` are the available project plan/checklist sources.

## Current flow and contracts

- `SAG/apps/api/sag_api/services/tree_publish_service.py::prepare_tree_candidate` freezes a checked routing candidate, checksum, quality gates, SearchUnit assignment bridge, leaf memberships, ACL-scoped profiles, and expected point counts.
- `publish_tree_candidate` serializes writers per project, stages the inactive manifest/profile rows, verifies canonical PostgreSQL SearchUnit mappings, clears the inactive Qdrant slot with `wait=true`, writes dual-slot payload batches with completed acknowledgements, checks exact total/partition counts and point identities/ACL payloads, verifies the persisted manifest/profile projection, and commits active slot/version/search epoch together in `_atomic_switch`.
- Failure verification catches errors before `_atomic_switch` and marks a non-active candidate `REJECTED`. `_atomic_switch` errors currently propagate outside that rejection handler. Candidate preparation occurs before any DB/Qdrant mutation.
- The current model already stores both slot tree versions, active/previous tree versions, active slot, search epoch, last switch time, manifests with canonical JSON/checksum, and durable query-slot leases (`db/models/routing_rag.py::ProjectSearchState`, `TreeManifest`, `TreeSnapshotLease`). No schema change is needed for the planned rollback/test work.
- Candidate manifests persist the SearchUnit assignments and routing-profile leaf memberships needed to reconstruct a previous candidate and verify/repair its Qdrant slot.
- `tree_query_snapshot_service.py::acquire_tree_query_snapshot` reads the PG pointer/active manifest and scoped profiles in one transaction and records leases. `EngineManager` exposes acquire/release; `search_unit_retrieval_service` releases after local and escape Qdrant reads. Existing lease tests show a live query prevents a subsequent publisher from reusing its slot.
- `incremental_tree_service.py::execute_tree_rollback` is a legacy PG-only rollback helper. It flips slot/version/status/epoch but does not verify or restore Qdrant. `test_checkpoint_c_incremental.py::test_fault_injection_and_rollback_restores_consistency` directly invokes the legacy atomic DB publisher after a failed mock write, then performs the PG-only rollback; it does not prove the mock Qdrant slot matches the restored manifest.
- At this base there are no production call sites for `publish_tree_candidate` outside tests. DATN-58/59 ingestion/subtree integration is not wired into this publisher. This task will test the subtree builder's failure boundary and the current publish service without claiming producer integration.

## Code-review follow-up (2026-10-05)

- Review finding P1 confirmed by repository-wide Python call-site search: `publish_tree_candidate` and `rollback_tree_candidate` are invoked only from tests. `coordinate_ingest_delta` also has no production caller and still calls the legacy `execute_atomic_tree_publish` path rather than the blue-green publisher.
- The current Checkpoint C APIs require a `TreeRoutingSnapshot` plus explicit `SearchUnitAssignment` records. The ingest coordinator accepts KnowledgeUnit inputs and scalar source/version defaults; it does not persist/provide the complete KnowledgeUnit→SearchUnit and source/version/partition mapping required by the publisher. No production KnowledgeUnit store/builder or endpoint/job contract is present in this checkout. Wiring an API or inventing that mapping here would bypass the producer and ACL contract, so this finding remains blocked on the DATN-58/59 producer contract and owner integration.
- Review finding P2 confirmed: rollback chooses `state.previous_tree_version` implicitly; after a successful rollback `_atomic_rollback` sets that field to the version just left, so retrying the same command toggles the active pointer back and increments the epoch again.
- Follow-up implementation: require an explicit `target_tree_version`; if it is already active, revalidate its active PG manifest/profile, canonical SearchUnit mappings and Qdrant slot and return the existing result without pointer or epoch mutation. Otherwise only roll back when the explicit target is the retained previous version. Add regression coverage for duplicate retries and update all call sites.
- Validation plan: focused Checkpoint C failure/rollback tests, Ruff on changed Python files, and `git diff --check`. Do not claim P1/runtime acceptance closed without the producer integration dependency.

## Gaps to address

1. No rollback API in the current blue-green publisher verifies the retained prior Qdrant slot before switching back.
2. A candidate switch error is not caught by the publisher's rejection path.
3. When a new publish starts overwriting the inactive slot, PostgreSQL still advertises the older slot version and `previous_tree_version` until the new switch commits; after a partial write that metadata can describe Qdrant content that no longer exists. Reserve/invalidate that inactive slot in the staging transaction before remote mutation, while keeping the active pointer and active slot untouched.
4. Current focused tests cover point-count failure, uncompleted acknowledgement, ACL payload failure, and lease-based reuse protection, but not subtree-builder failure against an active tree, a partially applied Qdrant batch, an atomic pointer commit failure, Qdrant-verified rollback, or a query pinned through both publish and rollback.
5. `SAG/tasks/todo.md` currently marks all Phase 8/Checkpoint C implementation lines checked while separately stating DATN-58 integration, PostgreSQL/Qdrant runtime verification and Checkpoint B readiness remain open. Update the Checkpoint C notes/checklist to point to new focused evidence without claiming live-service or broader phase acceptance.

## ACL/security boundary

- Candidate SearchUnit IDs and their Source, document version, project, tenant and security partition are checked against PostgreSQL before publish.
- Qdrant verification uses exact tree/project/tenant/partition counts plus exact point IDs and per-point SearchUnit/Source/version/project/tenant/partition/slot payloads.
- Request capture binds routing profiles to the caller's authorized Source/version/partition scope; retrieval carries the same pinned tree/slot/epoch and canonical ACL filter. Tests in this task must preserve these boundaries and fail closed on mismatches.
- No auth/ACL contract or trust configuration change is planned.

## Implementation plan

1. Extend the current publisher service with a rollback operation that validates the retained previous manifest, profiles, slot mapping and Qdrant payload inventory before an atomic PG pointer/status/epoch switch. Reconstruct the immutable candidate from its persisted checksummed manifest; reject rollback if the previous slot is unavailable or corrupted.
2. On staging a new publication, reserve the inactive slot in PostgreSQL by clearing its old slot-version/rollback eligibility before Qdrant cleanup or writes. This prevents a partial inactive-slot update from being represented as an intact retained snapshot; the current active pointer/version/slot remains unchanged.
3. Include the atomic switch in failure handling so a rolled-back DB transaction leaves the old active pointer intact and the candidate is rejected when safe to do so.
4. Add focused failure-injection tests for subtree build, quality/manifest/checksum/count/ACL verification, partial Qdrant writes, pointer switch transaction failure, successful publish/rollback Qdrant↔PG parity, and concurrent request snapshots across publish and rollback.
5. Update all Phase 8/Checkpoint C rows and evidence notes in `SAG/tasks/todo.md`; append actual test outcomes, file changes and unverified live-service boundaries here after implementation.

## Test matrix

| Scenario | Expected evidence |
|---|---|
| Subtree build raises before slot preparation | Active PG pointer/manifest and old query snapshot stay unchanged; no Qdrant mutation occurs. |
| Candidate fails quality, stored manifest/checksum, exact counts, or ACL payload verification | Candidate is rejected where staged; active version/slot/epoch and old-tree query remain unchanged. |
| Qdrant applies one inactive batch then fails a later batch | Inactive slot is no longer advertised as the prior version; active slot and old-tree query remain usable; candidate does not become active. |
| PG atomic switch fails during transaction | Transaction rolls back pointer/manifest status/epoch; failed candidate is rejected; old query remains usable. |
| Successful publish then rollback within retained-slot window | Restored PG pointer, slot-version field, manifest status/checksum and Qdrant exact point payloads identify the same old version; epoch advances. |
| Requests captured before/after publish and rollback | Every request retains one immutable slot/version/epoch; newly captured requests see the committed snapshot; leases prevent destructive slot reuse while reads are in flight. |

## Validation boundary and risks

- API documentation lists `pytest` and `ruff check .`; focused commands will run first and the relevant suite next.
- Existing evidence used SQLite plus `httpx.MockTransport`; no local PostgreSQL or Qdrant service was documented. Live advisory/row-lock and Qdrant behavior must remain clearly separated from mock evidence unless such services are actually available during validation.
- The term “retention window” is represented by the single prior tree retained in the opposite slot. It ends when a new publish reserves that slot; no numeric time-based rollback policy exists in the current schema/config. The implementation will make this boundary explicit and fail closed if the stored previous pointer and slot no longer match.
- No migration/config/security impact is expected. No migration will be edited or added unless repository evidence reveals a necessary schema gap.

## Implementation and review outcome

### Files changed

- `SAG/apps/api/sag_api/services/tree_publish_service.py`: reserve/invalidate the inactive slot's old PG version/rollback pointer before remote writes; reject a non-active candidate when the atomic pointer-switch transaction fails.
- `SAG/apps/api/sag_api/services/tree_rollback_service.py`: new fail-closed rollback. Rehydrate and validate the retained checksummed manifest, assignments and leaf inventory; verify PG profiles/SearchUnits and Qdrant counts/point ACL payloads outside long DB transactions; recheck and atomically switch manifests, active slot/version and epoch in PG.
- `SAG/apps/api/sag_api/services/incremental_tree_service.py`: mark the legacy `execute_tree_rollback` contract as PostgreSQL-only. It remains for legacy callers and is not the cross-store Checkpoint C rollback operation.
- `SAG/apps/api/tests/checkpoint_c_test_support.py`: consolidate deterministic routing fixtures and a payload-aware Qdrant mock with acknowledged/partial-write behavior and filtered dense/sparse queries.
- `SAG/apps/api/tests/test_checkpoint_c_failure_rollback.py`: fault injection, previous-tree read availability, PG/Qdrant parity, corrupt retained snapshot rejection and request reads/leases through publish/rollback.
- `SAG/apps/api/tests/test_checkpoint_c_incremental.py`: subtree-build failure keeps the prior legacy coordinator snapshot; rename the old PG-only rollback test to make its boundary explicit.
- `SAG/apps/api/tests/test_checkpoint_c_publish.py`: import shared fixtures instead of duplicating builders/mocks.
- `SAG/tasks/todo.md`: update Phase 8 and Checkpoint C acceptance/evidence; reflect Thang's confirmation that DATN-35 / `ROUTING_READY` is complete without claiming this task reran it.
- This folder contains the task spec, this research record, `plan.md` and `evidence.md`.

### Actual validation (2026-10-05)

- Focused API regression command covering `test_checkpoint_c_publish.py`, `test_checkpoint_c_failure_rollback.py`, `test_checkpoint_c_incremental.py`, `test_search_unit_retrieval_service.py`, `test_query_routing_service.py`: **63 passed in 8.87s** (rerun after strengthening concurrent Qdrant reads).
- Ruff on all 7 changed Python files: **All checks passed**.
- `compileall` on all 7 changed Python files: passed.
- `uv build --out-dir "$env:TEMP/sag-checkpoint-c2-build"`: sdist and wheel built successfully outside the repo.
- `git diff --check`: passed.
- Full API suite was first started and remained without a summary/progress for approximately six minutes; it was stopped. A bounded diagnostic `pytest -q -x --tb=short` found the first existing-suite failure after **34 passed**: `tests/test_acl_runtime.py::test_global_scope_is_applied_before_search_unit_candidate_generation` patches `retrieve_search_unit_sections` with `record_search_unit_scope(..., principal, top_k)` but production `_prepare_global_search` passes `query_strategy_plan`. Neither file is changed in this task. The full suite is therefore not reported clean, and later tests' status is unknown.
- Whole API Ruff reports 163 existing findings, including files not changed here. Changed-file lint is clean.

### Review findings resolved

- Inactive slot staging previously left the old previous-version metadata advertised while Qdrant was being partially overwritten. The short PG staging commit now removes that old slot mapping and rollback pointer before network I/O, while keeping the current active pointer untouched. A verification failure can no longer mistake partially written Qdrant as a retained prior snapshot.
- Atomic switch errors previously fell outside the candidate rejection path. They now reject the candidate after a failed transaction, while `_mark_rejected` leaves an already-ACTIVE manifest alone if commit outcome is ambiguous.
- The existing legacy rollback updated PG only. A separate cross-store service verifies the retained Qdrant slot, PG manifest/profile/SearchUnit inventory and ACL before atomic rollback; its failure paths leave the current tree active.
- Query tests previously asserted routing snapshot objects across switches but not retrieval filters/results. The focused suite now routes the captured snapshot and reads dense/sparse candidates after the pointer has moved, checking version, source, document version and partition, including a retained lease after rollback.
- Shared test fixtures were consolidated into one support module. No schema or dependency migration was introduced.

### Impact, delivery and open acceptance

- Database: PostgreSQL application rows only; no schema/migration/seed changes. Rollback pointer/slot/version metadata changes at stage and switch as above.
- Configuration/security: no config, dependency, auth, ACL-policy or secret changes. Existing ACL checks are reused and exercised by negative payload tests.
- Rollback plan: the previous version is eligible while its opposite slot contents remain. The next publish ends that window when it reserves that slot. Cross-store rollback fails closed after corruption/reuse. Reverting the code requires no down migration.
- Task changes are committed locally on `feat/Thang-checkpoint-c-failure-rollback-be-api`; not pushed and no PR created. PR target remains `main`.
- Runtime acceptance remains open: no live PG/Qdrant, multi-process/production load or crash test was available; DATN-58/59 producer is not wired into the publisher at this base. Do not mark operational `INCREMENTAL_READY` from mock evidence.

### Review-fix outcome (2026-10-05)

- `SAG/apps/api/sag_api/services/tree_rollback_service.py`: `rollback_tree_candidate` now requires `target_tree_version`. A target that is already active is rehydrated as an ACTIVE manifest, its stored profiles/SearchUnit mappings and Qdrant slot are verified, then the existing `TreePublishResult` is returned without changing active pointer, previous pointer, slot or search epoch. A non-active target is accepted only when it equals the retained previous version.
- `SAG/apps/api/tests/test_checkpoint_c_failure_rollback.py`: added a red/green duplicate rollback regression; all existing rollback calls now name their target version.
- `SAG/docs/Thang_Task/[SAG][Checkpoint C]2/evidence.md` and `SAG/tasks/todo.md`: record the idempotency evidence and keep producer/live-service acceptance explicitly open.
- Validation after the code change: five focused Checkpoint C/query modules **64 passed in 7.78s**; rollback/failure module **12 passed in 3.09s**; includes rejection of a target that is not the retained version. Ruff and `compileall` for the two changed Python files passed; `git diff --check` passed.
- `uv run` could not create its isolated dependency environment because building `litellm` needs the unavailable MSVC `link.exe`; the pre-existing API `.venv` successfully ran the focused tests. Packaging build was not rerun for this review-fix.
- Review P1 remains unresolved for a concrete dependency reason: exhaustive call-site search shows no application caller for either blue-green API, and `coordinate_ingest_delta` is uncalled and still publishes through the legacy PG-only helper. No persistent KnowledgeUnit producer or complete KnowledgeUnit→SearchUnit/source/version/partition mapping is available in this checkout. Adding a route or synthesizing this bridge would be unsafe and would not make it the actual producer flow. DATN-58/59 owner integration is required before that finding/runtime acceptance can close.
- No PostgreSQL schema, migration, dependency, configuration, authorization policy or secret changes. Review-fix code/test commit: `06e6d66` on `feat/Thang-checkpoint-c-failure-rollback-be-api`. Push/PR are pending because workspace policy requires Thang to push.

### Review follow-up research (2026-10-06)

- Current checkout is the existing PR #21 branch `feat/Thang-checkpoint-c-failure-rollback-be-api`, based on the fetched `origin/main` at `0102e1e`; the branch is three commits ahead and has no tracked working-tree changes. Preserve the existing untracked `SAG/docs/Thang_Task/[SAG][Checkpoint C]/research.md`.
- Finding: `_prepared_candidate_from_manifest` validates `routing_profiles` but not `source_routing_profiles`. `PreparedTreeCandidate.source_routing_profiles` silently maps a missing/non-list field to `[]`; validate the persisted shape before constructing the candidate and add focused malformed-manifest coverage.
- Finding: partition count equality wraps `Counter(...)` in `dict(...)` even though mapping equality supports the direct `Counter` comparison; simplify without changing behavior.
- Lease cleanup: publish uses PostgreSQL `func.now()` via `_database_now` and deletes expired `TreeSnapshotLease` rows before checking slot reuse. Reuse that helper and the same project-scoped expiration predicate in rollback; test expired rows are removed while live rows remain.
- Slot selection: the current two-branch lookup of the retained version can select the target as the slot opposite the active slot, then verify that slot's recorded tree version equals the retained version. Preserve the fail-closed mismatch error.
- Test transport: verified installed HTTPX `0.28.1`; `httpx.MockTransport.handle_async_request` awaits an async handler result, so `_AsyncMockTransport` adds no behavior and can be removed in favor of `httpx.MockTransport(handler)`.
- Duplicate profile verification: retain both checks. The early check validates the manifest/profile projection before remote Qdrant verification. `_atomic_rollback` rechecks it while holding the final PostgreSQL row locks after Qdrant I/O, closing a time-of-check/time-of-use window before pointer mutation.
- Planned files: `tree_rollback_service.py`, `checkpoint_c_test_support.py`, and `test_checkpoint_c_failure_rollback.py`; update this record and `SAG/tasks/todo.md` with exact outcomes. No endpoint/producer integration, schema, migration, dependency, config, or ACL policy change is in scope.
- Test matrix: source-profile field missing and wrong type are rejected as malformed rollback manifests; successful rollback removes only expired project leases; retained target selection still matches the opposite slot; async mock-client suite remains green. Run the focused rollback/publish/incremental and retrieval/routing tests, Ruff on changed Python files, compileall, and `git diff --check`.

### Review follow-up implementation (2026-10-06)

- `tree_rollback_service.py`: reject rehydrated manifests whose `source_routing_profiles` field is missing or not a list; compare the stored partition-count mapping directly with `Counter`; delete only expired `TreeSnapshotLease` rows for the project using PostgreSQL time; select the slot opposite the active pointer and verify its stored version equals the retained target. Keep the final source-profile check inside `_atomic_rollback` after remote Qdrant verification to prevent a profile change from racing the pointer switch.
- `checkpoint_c_test_support.py`: use the installed HTTPX `MockTransport` directly; HTTPX 0.28.1's async transport handler awaits async handlers.
- `test_checkpoint_c_failure_rollback.py`: add malformed-manifest tests for missing/non-list source profiles and verify rollback removes the expired lease while retaining live and other-project leases.
- Validation on the final working tree: five focused API modules **66 passed in 9.07s**; Ruff on all three changed Python files passed; `compileall` passed; `git diff --check` passed. The new malformed-manifest and lease regressions were observed failing before the implementation and passing afterward.
- Database/config/security impact: deletes expired lease rows for the rollback project's query snapshots; no schema/migration, dependency, configuration, authorization, ACL-policy, or secret change. The production producer/runtime integration gap remains open as before.
- Delivery: local changes are on the existing PR #21 branch; not pushed. The unrelated untracked research artifact in `[SAG][Checkpoint C]/research.md` remains untouched.
