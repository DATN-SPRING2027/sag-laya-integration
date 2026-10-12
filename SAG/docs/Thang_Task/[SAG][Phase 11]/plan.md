# Implementation Plan — [SAG][Phase 11] ACL- and version-safe cache boundaries

## Goal

Implement a bounded, exact-query route-decision cache whose hits are only
possible after current authorization and a fresh, verified tree snapshot are
available. Keep answer caching and semantic route reuse off.

## Work plan

- [x] Add a versioned SHA-256 route key containing tenant/project, verified
  principal ACL fingerprint, per-source mapping generation in the
  effective-scope epoch, source/document versions, partition, current
  `INDEX_SEARCH` run/stage/manifest identity, search/tree epoch, tree config
  version, slot, full manifest checksum, scorer/model version, planner trace,
  routing policy, effective filters, exact query digest, and query-scoped
  profile inputs.
- [x] Add a bounded process-local LRU. It stores only `RouteDecision` values;
  it stores only successful routed decisions. Cache hits still obtain a fresh
  tree snapshot/lease and rebind per-request snapshot IDs.
- [x] Add an explicit route-decision cache setting that defaults off. No answer
  cache or semantic-query matching path is added.
- [x] Add fixtures for key stability, ACL/scope changes, filter changes,
  document/tree/search version changes, publish/rollback epochs, cache bounds,
  fresh snapshot identity on a hit, and fallback non-retention.
- [x] Record test evidence and remaining rollout limits in `evidence.md` and
  `SAG/tasks/todo.md`.
- [ ] Measure route-equivalence precision on an agreed dataset and get project
  owner approval for thresholds before implementing or enabling semantic route
  reuse.
- [ ] Run live PostgreSQL/Qdrant revoke, load, latency, rollback, and operational
  dashboard/runbook verification.

## Boundary decisions

- Authorization still runs on every search before the routing snapshot/cache
  lookup. The cache never stores evidence, answers, or raw query text.
- `effective_acl_epoch` hashes the freshly resolved source/version/tenant/
  project/partition scope together with each confirmed
  `SourceProjectMapping.mapping_version`. The tracked revoke/regrant path
  advances that per-source generation. It is not a global identity-provider
  policy epoch and is not used in place of authorization.
- Current verified index boundaries include each document version's latest
  `IngestionRun.id`, `INDEX_SEARCH StageRun.id`, and verified manifest checksum.
  A reprocess of the same document version therefore changes the route key.
- The exact route cache key also hashes the verified principal claims, current
  tree `config_version`, and routing policy version/values. Missing claims,
  mapping generation, current index attempt, scope, manifest verification, or
  version fields produce a cache miss.
- Tree/search snapshot acquisition remains on every request. Therefore this
  slice establishes correctness boundaries; no latency improvement is claimed.
- No database migration or shared cache service is introduced.

## Rollback

Set `SAG_SEARCH_ROUTE_DECISION_CACHE_ENABLED=false` (the default) to bypass the
cache. Reverting the application change removes the cache path. No persisted
cache entries or database state need cleanup.
