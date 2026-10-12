# Knowledge foundation and persisted routing candidates — 2026-10-06

Branch: `feat/knowledge-foundation-routing-candidates`, based on `origin/main` at `e2de5e7`.

This slice supplies the real source-data prerequisite for Checkpoint C. It builds Knowledge Units and evidence, runs independent enrichment, calibrates a sparse graph and feeds the existing routing builder. It persists candidates with status `INACTIVE` when all gates pass and `REJECTED` when any quality/ACL gate fails. It does not implement DATN-58–60 incremental publish, active-slot switching or the KnowledgeUnit-to-SearchUnit membership bridge.

## Data and reproducibility

`knowledge_units` contains one non-heading canonical passage per version, separate from SearchUnit grouping/token windows. UUIDv5 identity includes tenant, project, partition, document version and canonical block identity. Same text in another source/version retains separate provenance. A unit freezes source/document/version/snapshot, content hash, exact block/anchor/page/section, source clocks, supersedes lineage and validity. Input/config and evidence/features checksums detect stale or corrupted derived data.

`knowledge_evidence` records E0/E1/E2 entity, alias, claim and relation candidates with exact quote/character span/block locator, extractor version, confidence and validity. These are anchored extraction candidates; confidence is not factual entailment. E0 recognizes identifiers, URLs, email, dates, versions, paths and project-local dictionary aliases, and retains quoted statements. E1 uses bounded domain/backtick patterns and explicit `CO_OCCURS` relations. It does not download a NER/RE model or globally merge entities.

Reparse invalidates `knowledge_status` before replacing canonical artifacts, retaining historical knowledge evidence. Temporal resolution also invalidates versions whose validity/supersedes lineage changes, so the worker regenerates their frozen evidence intervals. Knowledge rebuild uses a private per-version knowledge lock, with only a short final DocumentVersion fence. If source artifacts change during rebuild, the transaction rolls back. The producer reads search readiness; it never writes search readiness. Completed knowledge data uses `DATA_READY`, not serving `KNOWLEDGE_READY`.

The same PostgreSQL canonical/source artifacts and config reproduce Knowledge Units, evidence, graph and candidate checksums. Successful validated E2 response JSON and extractor identity remain in `knowledge_jobs` when derived units/evidence are deleted; rebuild revalidates/replays matching input artifacts without another model call. Changed input/config invalidates old E2 artifacts. The test suite deletes derived stores, rebuilds and compares candidate/evidence checksums.

Migration: [0003_knowledge_foundation.sql](../../apps/api/migrations/0003_knowledge_foundation.sql). It adds `knowledge_units`, `knowledge_evidence`, `knowledge_jobs`, `knowledge_queue_control`, `knowledge_graph_builds`, `knowledge_unit_edges` and `knowledge_tree_nodes`, with existing `tree_manifests` holding the immutable candidate envelope. The SQL was applied twice successfully in the isolated benchmark schema. No public application migration or active pointer update was performed for this evidence run.

## Independent queue and enrichment bounds

The API lifespan starts a separate KnowledgeWorker; it discovers active, completed search-ready document versions in batches of 32. Cyclic discovery avoids repeatedly invalid early versions starving later data. The ingestion lane does not await this worker. E2 selection is limited to difficult semantic relations or locally low-confidence/high-importance units, and a separate URL/key/model must be configured to make model calls. With empty E2 settings, deterministic E0/E1 still finishes.

| Control | Default behavior |
|---|---|
| Concurrency | 2 running knowledge jobs across workers, guarded by durable admission lock |
| Capacity | 256 dispatchable queued/running/retry jobs; disabled E2 backlog is excluded from FOUNDATION admission capacity/age checks |
| Maximum queue age | 86,400 seconds; expired work requires explicit operator retry |
| Timeout / lease | 60 seconds per job; lease includes 30-second recovery margin |
| Retry | At most 3 attempts, exponential delay from 30 seconds; expired leases are fenced |
| E2 input/output | At most 6,000 characters and 1,000 output tokens; bounded evidence arrays |
| Daily budget | 100,000 reserved tokens per tenant/UTC day |
| Conservative charge | `4 × input_char_limit + output_limit + 4096` = 29,096 tokens per attempt; timeout/retry charges are retained |

Budget reservations happen before provider calls. The dedicated HTTP client has no generation-client hidden retries. Source checksum/provenance/ACL are revalidated before sending text and under the result commit fence; stale jobs do not call the provider. Exact quote/span/block checks reject unanchored, malformed, truncated or stale E2 responses. Provider exceptions are recorded only by class; response bodies, credentials and document text do not enter job error messages. `retry --job-id` resets attempts and the max-age window but does not refund daily reservations.

Controls are exposed in `.env.example` and passed through Compose. Set `SAG_KNOWLEDGE_WORKER_ENABLED=false` to disable the independent worker. This does not disable search. Retain the additive knowledge tables when rolling the application back.

E2 admission is disabled when URL/key/model are incomplete. Existing pending E2 work from an earlier configuration does not prevent FOUNDATION admission when E2 is disabled; the global running-job limit still applies. Enabling the provider later discovers eligible units and resumes E2 admission. Lease recovery and claim use PostgreSQL `FOR UPDATE SKIP LOCKED`; completion, failure handling and operator retry use row locks before checking state/token. A completion holding its row is skipped by recovery, preserving SUCCEEDED, attempts and the original token charge. A recovered lease rejects both late success and late failure from the old owner.

Rebuild's reuse path compares actual text, scope, ordinal, content hash and locator provenance with canonical input before trusting the cached unit checksum. Corrupted derived text/scope are restored from PostgreSQL artifacts, with the original unit identity/checksum preserved for unchanged source inputs.

## Sparse graph and candidate verification

Candidate generation uses bounded inverted postings for rare lexical terms, entities, source/section, citations and temporal buckets. It keeps at most 96 neighbor nominations per unit, with a posting window of 24; optional dense features use fixed LSH buckets. It does not scan all corpus pairs. The graph stores at most 32 undirected neighbors per unit, with fused edge threshold 0.55.

Signals are finite `[0,1]`: symmetric BM25 scaled using per-partition q05/q95, IDF-weighted entity Jaccard, structural proximity, citation overlap, interval overlap, and optional shifted cosine. Versioned weights are semantic .40, lexical .20, entity .15, structure .10, temporal .10 and citation .05. Missing signals are `null` and the observed weights are renormalized. This corpus has no KnowledgeUnit dense embeddings, so semantic absence is explicit rather than represented as zero similarity. Calibration method, quantiles, weights, config and component hashes are persisted.

The adapter calls the existing `build_routing_snapshot` and consumer `route_snapshot`. Trees and profiles are built separately per security partition. Benchmark profiles are recomputed from the query's authorized Source members before beam scoring, including source-narrowed cases. The offline consumer snapshot simulates a readable tree in memory; it never writes an ACTIVE manifest, routing slot, Qdrant routing payload or ProjectSearchState.

`knowledge-candidate.v1` freezes the builder manifest, producer input lineage, graph identity/checksum, routing policy, measured query outcomes and full node profiles. It records input/config/edge/profile/lineage component checksums. Versioned node rows include parent, depth, partition, membership, sparse/entity/temporal/dense profile fields and medoid approximation metadata. Verification checks the envelope, builder checksum, actual graph edge rows, node/profile/lineage rows, scalar manifest metrics and gate/status consistency. Tampering with an edge, node, metric or checksum fails verification. An ACL/quality failure remains REJECTED and cannot be accepted by changing its status.

## Corpus benchmark

Input: [knowledge_corpus.json](../../apps/api/tests/fixtures/knowledge_corpus.json). Output: [knowledge-corpus-results.json](knowledge-corpus-results.json). The producer reads unchanged repository Markdown source artifacts through canonical extraction; the benchmark does not start from fabricated KnowledgeUnit/edge fixtures.

The corpus represents this repository's architecture, contracts, ingestion and security content: workflow v1.1, Phase 0 contracts, Phase 6 tree evidence, ACL evidence-path inventory and Checkpoint A ingestion evidence. Three documents are public and two restricted. The report records all raw artifact SHA-256 hashes, stable source/version IDs, configs, runtime algorithm versions, quantiles, gate results and all 13 query outcomes. Gold targets are fixed exact source passages, with partition and authorized-source filters.

PostgreSQL schema: `knowledge_benchmark_20261006`. Candidate: `candidate-f5aebefc2ebe337229b69d1e`, status `INACTIVE`. Envelope SHA-256: `f5aebefc2ebe337229b69d1eddb2677a6b4d990c11b661f1943a8803b855f2c7`.

| Measurement / gate | Recorded result | Threshold |
|---|---:|---:|
| Knowledge Units | 402 (376 public, 26 restricted) | Partition-isolated |
| Persisted graph edges | 2,379 | Degree ≤ 32; weight ≥ .55 |
| Nodes / leaves / depth | 73 / 60 / 2 | Leaf size ≤ 32; depth ≤ 5 |
| Routing recall@10 | 12/13 = 0.923077 | ≥ .80 |
| Cohesion | .412156 | ≥ .15 |
| Edge cut | .587844 | ≤ .85 |
| Giant ratio | .555556 | ≤ .80 |
| ACL routing leakage | 0 | Exactly 0 |
| ACL blackhole | 0 | Exactly 0 |
| All quality gates | 10 passed | Every gate must pass |

Routing uses beam width 3 and branch-local sparse ranking, with no authorized-global escape to inflate recall. The mean routed-candidate/authorized-unit ratio is approximately .517. The missed gold case is `graph-missing`; individual returned unit IDs and selected branches are in the JSON report.

Thresholds remained fixed while choosing builder capacity: the initial `max_children=8` tree achieved 8/13 recall (.615385), below .80, and was REJECTED. The existing builder's balanced fallback scattered useful communities under that capacity. With `max_children=64`, target leaf size 8, max leaf size 32, Leiden resolution .05 and max depth 5, recall reached 12/13 and all structural gates passed. This is an exploratory calibration on a small curated same-domain corpus, not a held-out estimate of production recall.

Independent runs with `PYTHONHASHSEED=1` and `2` produced the same candidate version/checksum, including persisted graph and profile checksums. Each command also verifies committed rows in a new transaction and rebuilds with reversed query order. Raw file bytes, clocks, scope, config and algorithm versions are part of the reproducibility inputs; a checkout with different line endings or source revisions intentionally produces a different source/candidate checksum.

## Reproduction and operator commands

Run from `SAG/apps/api`, with the existing Python environment and configured `SAG_DATABASE_URL`. PostgreSQL benchmarks require an explicit isolated `knowledge_benchmark_*` schema; the script rejects an unscoped benchmark. The schema is retained for inspection.

```powershell
$env:PYTHONHASHSEED = '1'
.venv\Scripts\python.exe -m scripts.knowledge_candidates --schema knowledge_benchmark_20261006 --output ../../docs/tai_task/knowledge-corpus-results.json benchmark --input tests/fixtures/knowledge_corpus.json
$env:PYTHONHASHSEED = '2'
.venv\Scripts\python.exe -m scripts.knowledge_candidates --schema knowledge_benchmark_20261006 benchmark --input tests/fixtures/knowledge_corpus.json
.venv\Scripts\python.exe -m scripts.knowledge_candidates --schema knowledge_benchmark_20261006 verify --tree-version candidate-f5aebefc2ebe337229b69d1e
```

Apply migration 0003 after existing 0001/0002 for an application database before enabling this worker. `rebuild --tenant <tenant> --project <project>` rebuilds knowledge from existing canonical artifacts. `candidate --tenant <tenant> --project <project> --input <json>` builds an inactive candidate from that store; its JSON contains `tree_config`, optional `graph_config`/`routing_policy`, and `queries` matching `CorpusQuery` (query ID/text, target unit ID, partition ID, authorized Source IDs and k). `verify --tree-version <version>` checks persisted rows; `retry --job-id <id>` requeues a failed/expired job. Candidate config defaults are for operator tuning, not a claim of corpus-calibrated production thresholds.

## Verification and remaining boundaries

Focused ingestion, canonical/dedup/temporal/search indexing, routing builder/consumer, candidate persistence and existing Checkpoint C regressions: **169 passed, 2 skipped**. The skips require PostgreSQL independent row locks: concurrent search/reparse and recovery versus completion. The new knowledge suite also passed **26 tests against real PostgreSQL** in disposable `knowledge_test_*` schemas; keep the unrelated global API test database on the normal temporary SQLite fixture when running it. Ruff checks on new modules and the config/DB/model-registration/lifespan integration files, formatting on new Python modules, compileall and `git diff --check` passed. Compose YAML and new-file whitespace checks passed.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_knowledge_foundation.py tests/test_phase_2a_canonical_extraction.py tests/test_phase_2b_dedup_and_temporal.py tests/test_phase_2c_search_indexing.py tests/test_checkpoint_a_ingestion.py tests/test_phase_6_routing_tree.py tests/test_checkpoint_c_incremental_update.py tests/test_checkpoint_c_publish.py tests/test_checkpoint_c_failure_rollback.py tests/test_query_routing_service.py tests/test_search_unit_retrieval_service.py tests/test_search_unit_store.py -q --tb=short
# Resolve the configured PG URL in the parent; the child keeps the shared API fixture on temporary SQLite.
.venv\Scripts\python.exe -c "import os,subprocess,sys; from sag_api.core.config import settings; task_env=os.environ.copy(); task_env['SAG_KNOWLEDGE_TEST_DATABASE_URL']=settings.database_url; task_env.pop('SAG_DATABASE_URL',None); raise SystemExit(subprocess.call([sys.executable,'-m','pytest','tests/test_knowledge_foundation.py','-q','--tb=short'],env=task_env))"
```

The suite covers stable distinct units and whitespace-anchor fallback, exact evidence, temporal version changes requeueing knowledge and updating historical evidence validity, rebuild after derived-store deletion, cached E2 replay and stale pre-call denial, calibration/degree caps, checksum tampering, failed quality/ACL rejection, tenant/project/partition/source isolation and revocation, queue budget/concurrency/backpressure/age/lease/retry, failure/error sanitization, and simultaneous workers. A PostgreSQL test pauses knowledge build and completes another session's SEARCH_READY/canonical reparse within three seconds; resuming the stale knowledge build rolls it back while preserving search readiness.

Checkpoint A's locator test had a baseline fixture whose hardcoded returned text did not match the indexed SearchUnit hash. It now reads the actual mock index payload and additionally verifies that mismatched text is denied; runtime checksum protection remains intact.

Production E1 specialized-model accuracy, paid/live E2 provider behavior, KnowledgeUnit dense embeddings, held-out business corpus quality, signed-principal staging ACL/revocation, Qdrant publication and incremental active-slot switching are not asserted by this evidence. Those follow-ups remain open in Phase 5/6 and DATN-58–60; this change establishes reproducible inactive candidates and search-independent enrichment.
