# Research and execution plan — [SAG][Checkpoint A] Production E2E

- Date: 2026-10-06
- Repository baseline: `e2de5e7` (`origin/main`, PR #22 merged)
- Task branch: `feat/Thang-checkpoint-a-production-e2e-acceptance-be-api`
- Jira (read-only lookup on 2026-10-06): DATN-61 is In Progress, assigned to KeyT, with no comments; DATN-33 and DATN-34 are Done.
- Status: local contract research recorded; production acceptance is not yet run.

## Findings verified in the repository

### Upload, identity, and source mapping

- Project upload is implemented in `apps/api/sag_api/api/v1/documents.py` and delegates to `services/document_service.py::handle_document_upload`.
- Upload uses `core.deps.VerifiedPrincipal`: tenant, allowed Projects, and allowed security partitions are checked before the file is accepted. Search instead uses the separately verified `core.principal_assertion.VerifiedPrincipal`, whose required claims include `orgId` and `allowedProjectIds`; `tenantId` and `allowedPartitionIds` are accepted but may be absent. Search retrieval returns no canonical scope when tenant or partition scope is absent.
- `get_or_create_project_source` makes a project Source carrying tenant/project config but does not create a `SourceProjectMapping`. Search candidate selection and canonical citation authorization both require a `CONFIRMED` mapping for the assertion's organization and Project. The repository has a mapping creation path for regular Sources that starts in `PENDING`; no Project-upload mapping confirmation path was found. Do not infer organization from tenant or auto-confirm this mapping without the security/BE owner contract.
- `search_source_candidates` applies confirmed organization/Project scope before retrieval. PostgreSQL then joins Documents, DocumentVersions, SearchUnits, and mappings and checks tenant, Project, security partition, active/readiness/version validity, and current successful ingestion attempt. Qdrant dense and sparse filters use Project, tenant, partition, and exact eligible version IDs before top-k; payload values do not grant access.

### Readiness, indexing, and provider identity

- Worker indexing writes `search_units_{project_id}`, checks PostgreSQL/Qdrant point counts and checksums, records an `INDEX_SEARCH` `StageRun`, and marks the version `SEARCH_READY` only after successful manifest verification. Retrieval reselects the latest run and rejects stale/failed attempts, unverified manifests, and documents hidden by active delete/reprocess jobs.
- The manifest metrics include SearchUnit/PG/Qdrant/indexed counts, checksums, verification status, and collection name. Runtime settings contain an embedding model and optional dimensions, but those settings are not bound into the recorded index manifest metrics; the producer's actual model/revision/dimension identity therefore needs owner confirmation and may need a producer contract change before it can be evidenced.
- Search ignores enrichment readiness: the worker preserves `SEARCH_READY` if later enrichment fails, and existing focused tests cover enrichment disabled, delayed, and failed behavior.

### Search, routing snapshot, citations, and FE

- `/search` and `/search/stream` share `_prepare_global_search`; `search_context` calls the same `retrieve_search_unit_sections` reader. The reader captures one routing snapshot for all authorized Project/source/version/partition groups in that retrieval, applies it to the branch-local and global-escape Qdrant reads, and releases its lease after those reads. The snapshot contains the active PG tree manifest/pointer, routing slot, search epoch, scope-specific profiles, and a durable slot lease. Checkpoint C tests exercise snapshot lease protection and slot reuse with test database/Qdrant doubles.
- An agent turn can invoke `search_context` more than once. Each tool invocation calls the retrieval reader independently and obtains/releases its own snapshot; no shared snapshot token is passed through the whole agent turn. Confirm whether Checkpoint C's “once per request” means the full agent HTTP turn or one retrieval invocation; if it means the full turn, this is an implementation gap for answers assembled across repeated tool calls.
- Canonical evidence resolution checks the exact SearchUnit ID, authorized Source, version, content hash, canonical block range, page, and anchor. SearchContext only packs verified evidence with a complete locator, and structural/exact-anchor checks fail closed when evidence is missing or does not contain exact query terms. This does not establish semantic entailment or calibrated answerability.
- Internal citation buttons route using `source_id` plus SearchUnit/chunk ID to `/api/v1/sources/{source_id}/chunks/{chunk_id}`. The FE type currently models only chunk/source/name/content for that response; a real split-SearchUnit navigation and displayed block/page/anchor behavior has not been verified in a browser with indexed corpus data.

## Existing evidence and its limits

- `apps/api/tests/test_checkpoint_a_ingestion.py::test_checkpoint_a_e2e_real_upload_api_to_manifest_verified` exercises the upload HTTP route and worker path but replaces Qdrant with `MockQdrantStorage` and uses `FakeIngestionEngine`; it stops at a verified manifest and does not run `/search`, `/search/stream`, or `search_context` against a real index.
- `test_acl_runtime.py` seeds synthetic confirmed/pending/revoked mappings and principals. Retrieval, traceability, agent, and stream suites use fixture rows, fake engines/Qdrant, or stub LLMs. Checkpoint C publish/snapshot tests use local test persistence and a Qdrant double. These are useful regression evidence, not provider/staging acceptance.
- `SAG/tasks/todo.md` already leaves production upload-to-answer, owner contract, staging leakage/revocation/enrichment/FE navigation, and answerability calibration unchecked. Keep them unchecked until corresponding evidence exists.

## Runtime and owner gates observed

- No SAG API/PostgreSQL/Qdrant container is running in the current local Docker inventory; only ERP PostgreSQL and pgAdmin containers were present. The SAG `.env` files exist, but their values were not opened or copied. Their key names do not show a SAG database URL or principal-assertion issuer/JWKS configuration. No staging endpoint, signed upload principal, search assertion, approved Project-to-Source mapping, or real corpus was supplied in this task context.
- No live connection, upload, revoke, or mutation was attempted. Consequently the actual provider, corpus, model identity, deployment/runtime, and post-revoke result are **unknown**, and production acceptance cannot be claimed from local fixtures.
- Owner confirmations are still needed for: (1) upload principal ↔ search assertion tenant/organization/partition authority, (2) who creates/confirms/revokes the Project-to-Source mapping for auto-created Project Sources, (3) readiness/current-attempt and historical-version policy, and (4) embedding model/revision/dimension identity recorded for a verified manifest.

## Jira dependency evidence (read-only, 2026-10-06)

- [DATN-61](https://trankimthang0207.atlassian.net/browse/DATN-61) remains **In Progress**, assigned to KeyT, and has zero comments. Its linked issue [DATN-62](https://trankimthang0207.atlassian.net/browse/DATN-62) is **To Do**; the link direction says DATN-61 blocks DATN-62.
- [DATN-33](https://trankimthang0207.atlassian.net/browse/DATN-33) is **Done** for ingestion/index/readiness and enrichment independence; [DATN-34](https://trankimthang0207.atlassian.net/browse/DATN-34) is **Done** for global retrieval/context/citation. Their completed scopes do not approve a live Project→Source mapping, trusted production principal, or embedding identity. The unchecked `todo.md` owner item now points to DATN-61 for those remaining production gates.
- [DATN-67](https://trankimthang0207.atlassian.net/browse/DATN-67), **To Do**, states DATN-61 owns production ACL issuer/JWKS, Project→Source mapping/backfill/revoke, and staging cross-tenant checks; DATN-61 and DATN-67 may proceed in parallel, and DATN-62 starts after both. This supports leaving the owner/staging gate open; it does not supply the missing owner decisions or runtime evidence.
- [DATN-82](https://trankimthang0207.atlassian.net/browse/DATN-82) and subtasks DATN-207–212 are **To Do** for the separate Project Access Permission Provisioning V1 work package. Record it as adjacent authorization work, not as proof that a specific Checkpoint A contract is approved or as a confirmed blocker to DATN-61.
- The prior Rovo search returned an incomplete-source warning. The issue status and descriptions above were therefore checked directly through Jira issue lookups; no comments or issue updates were sent.

## Ordered plan and evidence checklist

### 1. Close trusted contracts before writing acceptance data

- [ ] Record dated BE/security/ingestion-owner decisions for the four contracts above; use Jira/docs evidence or owner-provided answers.
- [ ] Confirm a dedicated staging runtime, permitted test Project/corpus, disposable upload identity, signed search principal, revocation path, and cleanup procedure. Do not point the acceptance run at production data.

### 2. Run the real upload-to-search vertical flow

- [ ] Upload a controlled document through the real Project upload API with trusted tenant/Project/partition scope.
- [ ] Observe its worker run and `INDEX_SEARCH` manifest; record provider, collection, model identity, dimensions, point counts/checksums, run/version IDs, and real SearchUnit locators without recording secrets.
- [ ] Query that corpus through `/search`, `/search/stream`, and the `search_context` agent tool; verify each response resolves the same source/version/SearchUnit/block/page/anchor where applicable.

### 3. Verify ACL, enrichment, citations, and split navigation

- [ ] Use an allowed principal and a distinct denied principal/partition; verify no forbidden evidence is returned before or after mapping/partition revocation, including a query started after revoke.
- [ ] Exercise enrichment disabled, lagging, and failing while confirming Search readiness/retrieval stays available.
- [ ] Verify canonical stream emits only the finalized citation-validated answer, assistant history preserves locator provenance, absent locators fail closed, and exact identifiers are covered by retrieved evidence.
- [ ] Open a split SearchUnit citation in a real browser and verify it resolves the correct version/block/page/anchor (or document any missing FE locator support).

### 4. Focused regressions and handoff

- [x] Updated stale `test_agent_tools.py` SearchContext cases to exercise the current canonical SearchUnit reader contract: principal passthrough, locator fail-closed filtering, citation metadata/adapter propagation, and stable numbering for multiple verified sections. These use mocked reader results and are local regression evidence only.
- [x] API focused suites pass: `test_agent_tools.py` **14 passed**; the Checkpoint A and routing/Checkpoint C focused group **134 passed**. FE focused tests previously run for this task pass **39 passed**.
- [x] Complete the full API suite and record its failures separately from focused results: **903 passed, 25 failed, 1 skipped, 8 errors, 108 warnings** (`pytest -q --tb=no`, 746.29 seconds). Failing/error cases are in ask-stream, document lifecycle/parsing/resume, DSH integration, experience, hardening, OCTX, model setup/settings, and unit suites; the changed SearchContext tests pass. The result is not attributed to the base branch because that full suite was not run before this task's test-fixture updates.
- [x] Update the Checkpoint A lines in `SAG/tasks/todo.md` with this task's evidence link and keep externally blocked acceptance unchecked.
- [x] Inspect the final diff/status and review the changes for correctness, architecture, security, and test intent; no actionable finding remains in this test/documentation-only diff.

## Acceptance evidence status

| Requirement | Current status |
|---|---|
| Actual provider/corpus/runtime named | Not run; runtime/corpus not supplied |
| Real Qdrant upload → `/search` → `/search/stream` → `search_context` | Not run; existing E2E uses Qdrant/engine doubles |
| Trusted principal and Project-to-Source mapping confirmed | Not confirmed; upload and search principal contracts are distinct; auto-created Project Source lacks a confirmed mapping in repository code |
| Post-revocation no-forbidden-evidence proof | Not run |
| Locator exactness and fail-closed behavior | Covered in local tests; no real-corpus or browser proof |
| Enrichment independence / canonical stream behavior | Covered by local focused tests; no production provider proof |
| Split-SearchUnit FE navigation | Not verified in a real browser |

## Local validation on prior base `64ec5b4` (2026-10-06)

- Code changes in this follow-up are test-only; task evidence and `SAG/tasks/todo.md` were updated. No runtime implementation, schema, migration, or deployment configuration changed.
- `tests/test_agent_tools.py`: **14 passed** after replacing obsolete graph/enrichment expectations with the canonical SearchUnit reader contract. The focused three SearchContext regressions passed **3/3** first; Ruff on this file passed.
- Focused API regression set: **134 passed** across ingestion, ACL, SearchUnit retrieval, traceability, stream, agent, Checkpoint C publish/rollback, and query routing.
- Focused FE regression set: **39 passed** across API search stream, citation presentation, conversation runtime, and search state.
- Full API suite: **903 passed, 25 failed, 1 skipped, 8 errors, 108 warnings** in 746.29 seconds. Failures include `test_ask_stream` (4), document cleanup/parsing/priority/resume (12), DSH integration (2), experience, hardening, OCTX runtime, quick model setup, settings, and units (2); the 8 errors are in document priority/resume tests. Since the full suite was not run against the unchanged base first, these are recorded as current-suite failures, not asserted to be pre-existing.
- Ruff passes for `test_agent_tools.py`, `test_acl_runtime.py`, and `test_agentic.py`. A broader Ruff run including `test_checkpoint_a_ingestion.py` reports 40 findings in that long test file; no broad cleanup was included in this task.
- These suites use local fixtures, test databases, and service doubles. They do not identify or validate the missing staging provider, corpus, trusted principal, secret-store setup, Project-to-Source approval, or embedding identity. Production/staging acceptance remains open.

## Fresh branch and validation update (2026-10-06)

- The task spec above was already present in this folder before research resumed. The work was moved safely to a fresh branch after fetching and fast-forwarding `main` from `64ec5b4` to `e2de5e7`; the prior local changes and unrelated Checkpoint C research file were restored without conflicts.
- PR #22 changes the Phase 8 incremental tree publisher, its tests/evidence, and `SAG/tasks/todo.md`. Its changed-file list contains no Checkpoint A upload, identity, SearchUnit reader, ACL, or citation implementation changes, so the earlier A contract findings remain applicable to the inspected code.
- On `feat/Thang-checkpoint-a-production-e2e-acceptance-be-api` at `e2de5e7`, the focused API set passes **170 tests** across ingestion, ACL, SearchUnit retrieval, traceability, stream, agent tools, Checkpoint C publish/rollback/incremental update, and query routing. This includes the incremental-update tests added by PR #22.
- The focused FE suite passes **39 tests**; Ruff passes for `test_acl_runtime.py`, `test_agent_tools.py`, and `test_agentic.py`.
- Ruff reports 40 diagnostics in `test_checkpoint_a_ingestion.py` on both the working tree and `HEAD`; none are on the lines changed for this task. No broad cleanup of this legacy test file was included.
- The full API suite was not rerun on `e2de5e7`. The recorded full-suite result (**903 passed, 25 failed, 1 skipped, 8 errors, 108 warnings**) belongs to the earlier `64ec5b4` base and is not presented as a current-base result.
- No staging profile, corpus, trusted principal, secret-store configuration, owner-approved Project-to-Source mapping, or embedding identity was supplied. No live service was accessed; production acceptance remains open.
