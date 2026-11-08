# Knowledge Universe document revocation

## Baseline and scope

- SAG PR #67, branch `feat/Thang-universe-revoke-cache-fix-be-api`, head `7e09d79`, base `207cc23`.
- `knowledge_cluster` calls `_knowledge_snapshot`, which reloads canonical readiness in `manifest` before accepting the active tree cache. `knowledge_universe_page` checks live unit rows before returning a cached page. `revoke_project_document` commits the document and version state before clearing the writer's local caches.
- The source and version readiness checks cover confirmed source mapping, tenant, project, partition, document visibility, current search readiness and the verified index attempt. Cluster row checks add live knowledge-unit and version state. A separate reader worker must observe the committed revoke through the database even when its process-local caches stay warm.
- Independent SQLite verification at PR head used the real manifest and cluster code, two ready documents and distinct reader/writer sessions. Without local eviction, the head excluded both units of the revoked document and retained the authorized document. The base still returned both revoked units. A source-fallback scenario also rejected a source whose mapping became revoked while its document stayed active. These checks did not run against a deployed worker or PostgreSQL.

## Follow-up plan

- Strengthen `test_universe_knowledge.py` with real SQLite readiness records and a writer whose local cache eviction does not reach the reader. Keep the cached-page regression for a snapshot obtained before document deactivation. Remove the trailing blank line reported by `git diff --check`.
- The independent warm-cluster check measured one SQL statement on the base and seven on the PR head. Reusing the old manifest TTL or skipping the page liveness query would remove security checks. Reducing the SQL count would require changing the shared canonical readiness contract or introducing a database-backed invalidation scheme. Both are outside this follow-up without representative load evidence and equivalent readiness coverage.
- Run the Universe and Handover tests offline with isolated SQLite and no deployment settings. Check the complete PR diff, file list and Ruff results against the base. No schema, migration, config, provider or API contract changes are planned.

## Follow-up results

- `test_universe_knowledge.py` now warms the cache with a checksum-verified manifest and canonical readiness records. It covers an already loaded snapshot after document deactivation and a new reader after the revoke commits from a separate writer session whose process-local eviction does not reach it.
- The revised regression failed against the `207cc23` Universe modules because the stale snapshot still returned both preview units. It passed against the PR runtime with real readiness and an isolated SQLite database.
- The focused Universe and Handover suites passed 23 tests in 22.92 seconds with deployment dotenv disabled.
- The expanded local check with `test_audit_remediation.py` returned 38 passed and two baseline failures on Python 3.11.15. Both graph payload tests call `math.sumprod` through unchanged `knowledge_service.py`, which needs Python 3.12 or later for that function.
- `git diff --check origin/main` and Ruff on the edited test passed. The follow-up changes only test and research files. The measured seven warm-cluster SQL statements still describe the PR runtime. PostgreSQL latency at representative load remains unmeasured.
