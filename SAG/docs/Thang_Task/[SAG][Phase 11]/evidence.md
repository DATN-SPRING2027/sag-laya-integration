# Phase 11 Implementation Evidence

Date: 2026-10-12
Branch: `feat/Thang-sag-phase11-cache-be-api`

## Implemented

- `route_cache_service.py` defines a versioned exact-query cache key. The key
  hashes the verified principal ACL fingerprint, effective authorization
  scope digest, tenant/project/source/document-version/partition scope, current
  per-source mapping generation, latest verified `INDEX_SEARCH` run/stage IDs
  and manifest checksum per document version, search epoch, tree version/config
  version/slot/full manifest checksum, route scorer version, planner trace,
  routing policy, effective scope filters, exact query digest, and current
  query-scoped profile values.
- The bounded in-process LRU stores route decisions only. Search resolves
  authorization and current ready versions first; each request still captures
  a fresh verified routing snapshot/lease. A hit receives the current group and
  request snapshot IDs. A changed ACL scope, filter, profile, tree, search
  epoch, or document version changes the key.
- `SAG_SEARCH_ROUTE_DECISION_CACHE_ENABLED` defaults to `false`. The cache uses
  exact query/profile identity only. No answer/evidence cache or semantic query
  reuse is implemented.
- The effective ACL epoch hashes current authorized scopes and
  `SourceProjectMapping.mapping_version`. The in-repository revoke/regrant
  workflow advances the mapping generation, so a return to the same scope still
  misses the prior entry. This is not a global identity-provider policy epoch
  and does not replace fresh ACL checks.

## Verification

Run from `SAG/apps/api`:

| Check | Result |
|---|---|
| `python -m pytest -q tests/test_route_cache_service.py tests/test_query_routing_service.py tests/test_search_unit_retrieval_service.py tests/test_phase_6_routing_tree.py` | 62 passed |
| Checkpoint C publish/failure/rollback/incremental modules | 46 passed |
| ACL/revoke and publish/rollback regression group | 58 passed, 6 failed during app lifespan because `litellm` is absent from the test environment |
| Full API `python -m pytest -q` | Blocked at collection in `test_knowledge_quality_persistence.py` and `test_litellm_policy.py` because `litellm` is absent |
| Ruff check on changed Python files | Passed |
| Ruff format check on new/changed route-cache and routing test files | Passed |
| `compileall` on changed Python files | Passed |
| `git diff --check` | Passed |

`uv sync --locked --no-install-project --no-install-package litellm` installed
the remaining locked dependencies. The locked `litellm==1.92.0` source build
requires the MSVC `link.exe` linker, which is unavailable in this environment.
The six failures terminate while `runtime.start()` imports `litellm`; they do
not reach the changed cache code. The full suite cannot collect two modules
that import `litellm` directly. The focused cache/routing and retrieval/tree
regressions ran successfully with the repository's conftest.

## Precision and operational limits

- Exact-key route-decision reuse passed deterministic fixtures showing the
  same inputs reuse a decision while rebinding fresh snapshot IDs. Enabled-cache
  fixtures also show misses after mapping revoke/regrant generation changes,
  same-document-version reindex attempts, and publish/rollback search-epoch
  changes. These are service/key fixtures, not a live database ACL/revoke test
  or a corpus-level route-equivalence benchmark.
- Semantic route-equivalence precision, sample count, confidence bounds, and
  ACL-isolation production metrics have **not been measured**. Semantic route
  caching remains unimplemented and disabled.
- No p50/p95/p99 or load benefit is claimed. The current path still reads the
  active tree pointer/profiles and acquires a lease before a cache lookup.
- PostgreSQL/Qdrant runtime revoke and publish/rollback behavior still needs
  staging verification. This change adds no database migration or shared
  infrastructure.
