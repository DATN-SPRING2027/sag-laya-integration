# Research — [SAG][Phase 11] ACL- and version-safe cache boundaries

- **Date:** 2026-10-12
- **Branch:** `feat/Thang-sag-phase11-cache-be-api`
- **Base:** `origin/main` at `a222037` (PR #26 merge)
- **Status:** Research only. No code or task checklist changes made; no tests run.

## 1. Task understanding

Phase 11 is a hardening task for cache boundaries around routing and search. A
cache must never become an authorization source or make a response valid after
its ACL, data, tree, model, planner, configuration, or filter inputs change.
Answer caching stays off by default. Semantic route reuse is optional and must
remain off until route-equivalence quality and ACL isolation have measured,
owner-approved gates.

There are two distinct route outputs in the current request path:

1. **Query-flow/Laya route:** classifies chat/knowledge/command and proposes a
   retrieval strategy. A false `CHAT` result suppresses retrieval.
2. **Tree route:** chooses node membership for an already authorized
   Project/Source/document-version/partition group. A bad hit can narrow
   retrieval and reduce evidence recall.

They have different inputs and failure costs. The implementation should name
which output is being cached and measure it separately; a single “route
precision” number would hide those differences. This research does not approve
either cache for activation.

## 2. Repository and source-of-truth context

- [`SAG/README.md`](../../../README.md) describes the product as a Python API over
  PostgreSQL metadata and Qdrant vectors, with search evidence traced back to
  source chunks.
- [`SAG/apps/api/README.md`](../../../apps/api/README.md) places pure business
  behavior in `sag_api/services/` and HTTP validation/serialization in
  `sag_api/api/v1/`. It also states that Project-to-Source mappings are
  confirmed by an owner workflow and that local contract tests are not
  production ACL acceptance.
- [`SAG/tasks/plan.md`](../../../tasks/plan.md) Phase 11 requires stable cache
  keys, default-off answer caching, route-equivalence and ACL gates, latency
  measurement, invalidation/publish/rollback regressions, and runbooks. It
  lists Checkpoints B–D as dependencies.
- [`SAG/tasks/todo.md`](../../../tasks/todo.md) has Phase 10/Checkpoint D and
  Phase 11 items unchecked. Its Phase 8 evidence says the normal-ingestion
  producer for incremental tree updates is still not connected.
- [`SAG/tasks/acl-runtime-implementation-plan.md`](../../../tasks/acl-runtime-implementation-plan.md)
  explicitly says there is **no authorization-scope cache** and that runtime
  ACL acceptance still awaits production assertion/JWKS, approved mapping and
  revoke ownership, and real cross-Project/cross-Organization tests.

The repository has no Phase 11 task folder yet. This folder adds the concise
`researchtask.md` required by workspace rules and this requested `research.md`.

## 3. Current request and authorization flow

### Global search

`api/v1/search.py::_prepare_global_search` currently calls
`_build_query_route(...)` before resolving Sources with
`source_service.search_source_candidates(...)`. The request already has a
verified principal, but at the point of the Laya/query-flow call the effective
confirmed Source set and current document-version set have not yet been
materialized. The current route helper receives the query, client-requested
`source_ids`, and strategy values; it does not receive the principal or an ACL
fingerprint. There is no route-result cache today. Any future cache at this
layer must either move after current scope resolution or receive a freshly
resolved, server-derived scope before lookup.

`search_source_candidates` filters `SourceProjectMapping` to `CONFIRMED`, the
principal's Organization, and allowed Project IDs. Client `source_ids` narrow
that set. Database failures raise service-unavailable rather than widening to
global Sources. This is the current authorization boundary that a cache must
not skip.

### SearchUnit retrieval and tree routing

`search_unit_retrieval_service.retrieve_search_unit_sections` loads current
search-ready document versions and requires a successful, current `INDEX_SEARCH`
stage with a verified manifest. It groups retrieval by Project, Source,
tenant, security partition, and exact document-version IDs.

`query_routing_service.scope_fingerprint` is a canonical SHA-256 over
`project_id`, sorted `source_ids`, sorted `document_version_ids`, `tenant_id`,
and `partition_id`. `capture_routing_decisions` requests a fresh tree snapshot,
checks that returned scopes match the requested scopes, and routes only against
profiles that match the snapshot. A missing/invalid provider snapshot produces
a route fallback; retrieval then uses the global SearchUnit filter.

`tree_query_snapshot_service.acquire_tree_query_snapshot` reads the current
Project pointer, verifies the active manifest and slot, captures the
`tree_version`, `search_epoch`, slot and manifest checksum, and writes a lease
for the request. `search_unit_store.build_search_filter` always adds exact
Project, tenant, security-partition and document-version constraints; tree
version/slot/node membership are additional narrowing predicates. Citation
reads also re-check current authorization in
`search_unit_retrieval_service._get_search_unit_citation_checked`.

### Query-flow and planner

`api/v1/search.py::_build_query_route` runs Laya and the deterministic query
analysis/planner. The emitted query-flow trace is `query-flow-v1`; the planner
reports `PLANNER_VERSION = "qsp-v1"`. Laya may return a high-confidence `CHAT`
route that skips retrieval, and its failure path falls back to retrieval.
`GlobalSearchRequest` filters include `source_ids`, `strategy`,
`retrieval_mode`, and `top_k`; `save_exploration` is a side effect and must not
be bypassed by a response-cache hit. `SearchRequest` includes strategy and
`top_k`.

## 4. Cache behavior found in the current code

The inspected API code has no semantic route-decision cache, SearchUnit result
cache, or answer cache. Existing cache-like mechanisms are for other objects:

| Existing mechanism | What it retains | Why it is not a search/answer cache |
|---|---|---|
| `EngineManager` bounded LRU | SAG engine instances keyed by source configuration | It retains runtime clients/engines, not query results or evidence. |
| `services/laya_router.py` singleton | Loaded Laya model instance | It does not cache predictions; `route_query` evaluates the current query. |
| `core/principal_assertion.py` JWKS cache | Parsed signing keys, capped at 60 seconds | It verifies tokens; it does not cache Project/Source entitlements. |
| `parsing/service.py` file cache | Parsed Markdown keyed by source path and parser signature | It belongs to ingestion and must not be reused as an authorization result. |
| LiteLLM capability cache / settings `lru_cache` | Provider capability or configuration objects | Neither contains retrieval evidence or route decisions. |

The `Cache-Control: no-cache` response header currently appears on the streaming
search response. That is an HTTP response directive, not a backend answer-cache
implementation. No cache enablement flag for route or answer results exists in
`core/config.py`.

## 5. Existing version and scope boundaries

| Dimension | Current representation | Gap relevant to a future cache |
|---|---|---|
| Principal | `VerifiedPrincipal` includes subject, Organization, allowed Projects, tenant, partitions and token metadata. | No ACL/revocation epoch claim or field is present. |
| Confirmed access | `SourceProjectMapping.state`, Organization and allowed Projects are checked per source resolution. Mapping rows carry `mapping_version` and revoke audit fields. The tracked revoke/regrant flow advances that generation. | This is a per-source mapping generation, not a global identity-provider or policy epoch. Other mutation paths still need to honor the same writer contract. |
| Routing scope | `scope_fingerprint` covers tenant, Project, Source IDs, document-version IDs and partition. | This is an evidence-scope fingerprint, not a full principal ACL fingerprint: it does not include Organization, the principal's allowed-Project set, or an ACL policy epoch. Add those dimensions to a cache-specific authorization identity or prove an equivalent authoritative contract. |
| Search-ready data | Current document-version IDs plus the latest successful `INDEX_SEARCH` stage and its verified manifest. The implementation carries its current ingestion run ID, stage ID and checksum into the route key. | `ProjectSearchState.active_search_epoch` is tied to tree publish/rollback; there is no separate global SearchUnit-index epoch. Cache scope must include each current document/index attempt, not assume tree epoch covers reprocess. |
| Tree | `TreeManifest.tree_version`, checksum and `config_version`; the tree ID is derived from a prefix of its manifest checksum. Query snapshots carry full checksum, explicit config version, slot and active search epoch. | `RouteDecision` itself carries tree version, slot and epoch but not manifest details. Cache lookup is tied to the validated current snapshot and hashes its full checksum/config version. |
| Planner/query flow | `qsp-v1` and `query-flow-v1`. | `RoutingPolicy` has thresholds and limits but no explicit policy version. Bumping code/config without changing a cache version could reuse old routing. |
| Model | Laya trace includes a model label; configuration can choose a local checkpoint path or the Hugging Face repository. | The inspected settings/output do not expose an immutable model revision or checkpoint digest. An embedding/model configuration fingerprint is needed where it affects the cached artifact. |
| Request filters | Source-scoped/global request fields plus exact SearchUnit ACL and optional tree membership filters. | Cache keys must be artifact-specific: `top_k` affects results; `source_ids`, strategy and retrieval mode affect route/search; `save_exploration` must still execute as a side effect. |

## 6. Revoke, publish, rollback, and in-flight requests

- Tree publish and rollback switch the active tree/slot and increment
  `active_search_epoch` inside the pointer transaction. Existing
  `TreeSnapshotLease` rows keep an in-flight reader on its captured slot until
  release/expiry. Reusing the exact active candidate is idempotent and does not
  represent a new tree version.
- A route-cache key containing the freshly captured tree version, full manifest
  checksum, slot, and search epoch cannot match an entry from before publish or
  rollback. Do not use TTL alone as the invalidation mechanism.
- Current search resolves confirmed mappings and current versions on each
  request. A future result-cache lookup before those checks could return
  protected evidence after a mapping is revoked. Cache lookup must occur only
  after current authorization and current version capture; citation/response
  authorization must remain in force.
- An exact effective-scope fingerprint changes when its Source/version/tenant/
  Project/partition membership changes. The cache-specific epoch additionally
  hashes each confirmed source mapping's `mapping_version`, which changes on
  the tracked revoke/regrant path even when the scope later returns to the same
  IDs. The repository still has no global identity-provider/policy epoch; any
  other writer of mapping state must preserve this generation contract.
- Tree snapshot leases are consistency pins, not cache entries. A cached route
  must not reuse an old lease; it needs a fresh current pointer/epoch and a lease
  for the request's Qdrant read.

## 7. Prior evidence and its limits

- Phase 6's [`phase-6-tree-evidence.md`](../../tai_task/phase-6-tree-evidence.md)
  reports fixture routing recall 1.0 and zero fixture ACL leakage/blackhole.
  The document explicitly says these are fixture/contract values, not a
  production retrieval benchmark, and leaves corpus routing recall and runtime
  ACL leakage open. This is not semantic route-cache precision evidence.
- Phase 8's [`phase-8-incremental-update-evidence.md`](../../tai_task/phase-8-incremental-update-evidence.md)
  records local SQLite/mocked-Qdrant regression for update/subtree services,
  while normal-ingestion producer wiring, live publish/snapshot/rollback and
  production ACL checks remain open.
- The Phase 4 evidence reports 99 local fusion/retrieval/stream tests, including
  semantic paraphrase retrieval. Those tests do not measure whether a cached
  route for one query is equivalent to a fresh route for a different query.
- PR #26 (merge `a222037`) added Phase 9 scoring and demand tracking. Its
  `todo.md` evidence notes no production demand history before rollout and
  records a full API run with unrelated failures; the Phase 9 focused suite
  does not measure Phase 11 cache behavior.

These are historical local/fixture results. No production cache, revoke, or
route-equivalence acceptance is established by them.

## 8. Recommended cache contract after approval

1. **Keep answer/evidence caching off.** Do not cache generated answers or
   returned evidence in this phase unless separately approved with explicit
   freshness, citation, prompt/model, privacy, and deletion semantics.
2. **Resolve current authorization before lookup.** Use the verified principal,
   current confirmed mappings, current search-ready versions, and a fresh tree
   pointer/epoch. Missing ACL/version data means no cache hit; existing
   fail-closed search authorization behavior remains authoritative.
3. **Cache only a named artifact.** Prefer evaluating tree `RouteDecision`
   reuse separately from Laya intent/strategy reuse. Never cache a full search
   response under the route-cache gate. Do not cache fallback/error decisions.
4. **Use a versioned canonical key.** A route key should at minimum encode its
   cache-schema version; Organization/tenant/Project; effective authorized
   Source, document-version and partition set; authoritative ACL fingerprint
   and epoch; current tree version/full checksum/slot/search epoch; relevant
   search/index version; router model revision; planner, routing-policy and
   config versions; relevant request filters; and a hash of the normalized
   query or versioned query embedding. Store/trace only the key digest, not raw
   query text.
5. **Preserve per-request pins.** Even on a hit, acquire the current search/tree
   snapshot and lease before Qdrant reads; validate the cached membership against
   that exact scope and manifest. A miss/error falls back to the uncached path.
6. **Keep semantic reuse shadow-only until measured.** Compare the cached route
   with a fresh route on a versioned corpus of exact queries, paraphrases,
   near-neighbor hard negatives, multilingual cases, empty/ambiguous queries,
   and planner overrides. Report precision with sample counts and confidence
   bounds, plus candidate recall/blackhole changes and latency. Run adversarial
   cross-tenant/Project/Organization/revoke isolation tests. The task does not
   specify numeric pass thresholds; the owner must agree on those before
   enabling the optional flag.
7. **Separate freshness from eviction.** TTL/bounded capacity can reclaim old
   entries, but version/ACL key changes provide correctness. Revoke and epoch
   tests must prove a miss before relying on cleanup.

## 9. Proposed verification matrix

| Area | Fixture/change | Required observation |
|---|---|---|
| Key dimensions | Change one at a time: Organization, tenant, Project, ACL fingerprint/epoch, Source set, partition, document version, tree/checksum, search epoch, model, planner, policy/config, strategy, retrieval mode, requested Sources and result limit. | Each relevant change produces a miss; unrelated identical scope may hit only if the artifact contract permits it. |
| Revoke | Warm a route entry, revoke a Source mapping or narrow principal scope, then issue the same query. | Current authorization is resolved first; revoked Source/evidence is absent and the old key cannot hit. Missing epoch disables cache use. |
| Publish/rollback | Warm entries for tree A; publish tree B; roll back to A (new epoch); issue equivalent queries before/during/after switch. | New epoch never hits an old entry; each in-flight request remains pinned to its own leased snapshot. |
| Data/index | Replace or add a SearchUnit/document version and update search readiness/manifest. | Current data-version boundary changes the key; no old evidence is returned. |
| Cross-scope | Same query with overlapping IDs across tenants, Organizations, Projects, and partitions; include a Source that is `PENDING`/`REVOKED`. | No cross-scope hit; unauthorized source never reaches Qdrant retrieval. |
| Semantic equivalence | Exact repeats, paraphrases, close lexical/semantic negatives, Vietnamese/multilingual terms, ambiguous and multi-hop questions. | Report route-equivalence precision and retrieval coverage against an uncached baseline; keep cache disabled if the agreed gate fails or is unmeasured. |
| Defaults/failures | Fresh install/config absent; cache backend unavailable; malformed/unknown version or route fallback. | Answer cache and semantic route cache remain off by default; cache errors act as misses and never weaken ACL or current retrieval. |
| Side effects | Global search with `save_exploration=true`. | Cache reuse does not skip the requested exploration persistence. |

Do not run or claim these tests as passed until implementation and fixtures are
approved and added.

## 10. Dependencies and open decisions

### Verified blockers/dependencies

- No ACL/revocation epoch or production mapping-writer increment contract exists
  in the inspected code. `acl-runtime-implementation-plan.md` marks production
  ACL acceptance incomplete.
- The search path has a tree publish epoch but no separately named search-index
  epoch. Phase 8 producer integration is still open in `tasks/todo.md`.
- No measured route-equivalence precision dataset or agreed numeric gate was
  found. Existing tree quality fixtures are explicitly not production cache
  evidence.
- Phase 10/Checkpoint D is still open, while Phase 11 is described as depending
  on Checkpoints B–D.

These do not block documenting a safe default-off design. They do block
claiming the optional semantic route cache is enabled or production accepted.

### Decisions needed before enabling an optional route cache

1. Which artifact is the first candidate: Laya query-flow output, tree node
   `RouteDecision`, or both as separately gated caches? Recommendation: measure
   and gate them independently; begin with tree route shadow evaluation.
2. What authoritative ACL epoch/fingerprint will include Organization and
   current Project/Source/partition grants, and which approved writer increments
   it on grant/revoke?
3. What dataset, route-equivalence definition, precision/coverage minimum,
   sample size, and confidence requirement constitute the agreed enablement
   gate?
4. What measured latency benefit justifies the added cache path? The current
   code still reads the active pointer/profiles and acquires a lease per query;
   a route-result cache may need a pointer-only snapshot path to avoid most of
   that work. Phase 11 says to benchmark before fixing latency targets.
5. Is the first cache process-local and bounded, or shared? Choose only after
   the measurement and deployment requirements; a new shared service/config
   would add operational scope.

## 11. Likely implementation surfaces after research approval

- `SAG/apps/api/sag_api/services/query_routing_service.py` and
  `tree_query_snapshot_service.py`: define the artifact boundary and preserve
  fresh snapshot/lease checks.
- `SAG/apps/api/sag_api/api/v1/search.py`: place any query-flow cache after
  server-derived authorization scope or pass only fresh scope inputs.
- `SAG/apps/api/sag_api/core/config.py`: add explicit default-off feature gates
  only if the approved design needs them.
- Focused API tests for canonical keys, revoke, version/filter misses,
  publish/rollback, cross-scope isolation, defaults, and route-quality evidence.
- `SAG/tasks/todo.md` and a Phase 11 evidence record after implementation.

No schema migration is justified by this research alone. If an authoritative
ACL epoch or shared persistent cache requires schema/infra changes, split and
review that deliverable under the workspace database/configuration workflow.

## 12. Implementation update after research approval (2026-10-12)

The approved implementation adds a bounded, process-local **exact route-decision
cache**. Search still resolves current principal/source authorization and
search-ready versions, and still acquires a fresh verified tree snapshot/lease
before consulting the cache. The key hashes the exact query and current
query-scoped profile inputs as well as tenant/project, principal ACL
fingerprint, effective authorized scope, data/tree/search versions, manifest,
route scorer, planner, routing policy, and relevant scope filters. Only valid
routed decisions are retained; fallback decisions are recomputed. Cache hits
are rebound to the current request/group snapshot IDs.

The current slice derives a content-addressed `effective_acl_epoch` from the
freshly resolved authorized scopes plus their confirmed source mapping
generations. The tracked revoke/regrant workflow advances `mapping_version`, so
regranting the same source to the same Project cannot reuse a pre-revoke route
entry. This per-source generation is a cache partition only; it does not replace
the existing ACL decision or provide a global identity-provider policy epoch.
The key also carries the current verified index run/stage/manifest identity per
document version and the tree manifest's explicit config version. No database
migration was added.

`SAG_SEARCH_ROUTE_DECISION_CACHE_ENABLED` is explicit and defaults to `false`.
The cache only permits exact query/profile input identity; no semantic query
matching or answer/evidence cache was added. Semantic route-equivalence
precision remains unmeasured and no agreed threshold exists, so semantic cache
enablement is not available in this implementation. The exact path's fixture
check proves deterministic reuse and fresh per-request snapshot IDs; it is not
a corpus-level precision/quality or latency benchmark.

Implementation details and verification limits are recorded in
[`plan.md`](plan.md) and [`evidence.md`](evidence.md). The full API regression
run is limited by the local environment: the locked `litellm==1.92.0` package
could not be built without the MSVC `link.exe` linker.
