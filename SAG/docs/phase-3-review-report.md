# Phase 3 Review Report — Standalone Laya Local

> Branch: `feat/verify-laya-standalone-be-api`
> Base: `origin/main`
> Commit reviewed: `7fa1c68`
> PR: [#2](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/2)

## A. Review result

```
Review Gate: PASS
Blocking findings: None
Non-blocking findings: None
```

## B. Scope reviewed

Reviewed files:

- `SAG/apps/api/sag_api/services/laya_router.py`
- `SAG/apps/api/tests/test_laya_router.py`

The review covered local bundle mapping, routing semantics, safe fallback,
singleton/cache behavior, init failure caching, concurrency protection, API
contract, security, performance, test coverage, and scope compliance.

## C. Standalone runtime result

```
Standalone Laya Checkpoint: PASS
```

Runtime:

```
Enabled: true
Model path: C:\laya-local
Checkpoint: C:\laya-local\multilingual
Device: cpu
Library: laya==0.3.4
Torch: 2.14.0+cpu
Transformers: 5.17.0
```

The local bundle contains only the multilingual checkpoint. SAG now maps both
language routing labels to that same checkpoint and explicitly uses the
`multilingual` model key, preventing reloads when language detection changes.

## D. Routing invariants

The following contract was verified:

```
Only high-confidence chitchat → need_retrieval=false
Knowledge/factual → need_retrieval=true
Technical/project query → need_retrieval=true
Low-confidence/ambiguous → need_retrieval=true
Laya unavailable → need_retrieval=true
Laya inference exception → need_retrieval=true
```

Laya was tested standalone only. Agent integration, retrieval execution, and
final answer generation were not run.

## E. Model lifecycle

```
First request: model loaded once
Warm requests: reuse cached model
First-load latency: approximately 21.1s
Warm latency: approximately 164–204ms
Persistent init failure: cached
```

The router lock protects initialization. A failed model initialization does
not trigger a heavy model load on every subsequent request.

## F. Verification

Focused tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_laya_router.py
```

Result:

`9 passed`

Lint:

```powershell
.\.venv\Scripts\ruff.exe check sag_api/services/laya_router.py tests/test_laya_router.py
```

Result:

`All checks passed`

Full suite:

`646 passed, 1 skipped, 18 failed`

The 18 failures were classified as outside the Phase 3 diff: Windows
encoding, storage/backup, dependency-policy, LiteLLM/settings affected by
local environment configuration, and an existing agentic baseline case. No
failure occurred in `test_laya_router.py`, and none referenced the changed
Laya implementation.

## G. Security and architecture impact

```
.env tracked: NO
Secrets committed: NO
Model artifacts committed: NO
Runtime data committed: NO

Database: NO
Schema: NO
Migration: NO
Vector store: NO
Embedding: NO
Ingestion LLM: NO
Agent: NO
Retrieval: NO
Frontend: NO
Final answer generation: NO
```

## H. Git handoff and next phase

```
Commit: 7fa1c68
Push: completed
PR: #2
Merged: NO
```

Phase 4 must be created only after PR #2 is merged:

```bash
git fetch origin
create a fresh Phase 4 backend branch from origin/main
```

Next: Phase 4 — connect Laya routing to retrieval/context flow.

---

## I. Phase 3 Query Flow Integration Review

> Branch: `feat/Thang-laya-query-flow-be-api`
> Base: `origin/main`
> Scope: integrate the existing Laya router and deterministic query analysis into
> the API search flow; retrieval fusion, evidence context, citations and
> ingestion/index work are explicitly out of scope.

### Review result

```
Review Gate: PASS for the Phase 3 query-flow scope
Blocking findings: None
```

### Routing contract

| Input/result | API behavior | Trace evidence |
| --- | --- | --- |
| CHAT with confidence `>= 0.65` | Skip chunk/event retrieval and answer-generation stream | `retrieval=skipped`, `coarse_intent=CHAT` |
| KNOWLEDGE or factual Vietnamese query | Preserve the original query and run retrieval | `retrieval=required` |
| Exact identifier/path such as `ERR_TIMEOUT` | Preserve the identifier and user source scope | `query_analysis.features.identifier_terms` |
| Low-confidence or AMBIGUOUS | Keep retrieval enabled | `coarse_intent=AMBIGUOUS` |
| Laya unavailable or prediction error | Fall back to retrieval without exposing the exception | `fallback_used=true`, stable `fallback_reason` |

The response `stats.query_route` record contains the original query, selected
source scope, coarse intent, confidence, Laya model, requested/effective
strategy, deterministic query features, reason codes, retrieval/fallback state,
and retrieval-side fallback status. The original `SearchRequest` query and
source scope are not replaced by normalized lexical terms or Laya output.

### Verification

Focused API routing/search tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_laya_router.py tests/test_search_strategy.py tests/test_search_stream.py
```

Result: `34 passed`

Query-analysis and retrieval regression:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_laya_router.py tests/test_search_strategy.py tests/test_search_stream.py tests/test_query_analysis.py tests/test_retrieval_relevance.py
```

Result: `63 passed`

Lint:

```powershell
.\.venv\Scripts\ruff.exe check sag_api/services/query_analysis.py sag_api/services/laya_router.py sag_api/api/v1/search.py tests/test_laya_router.py tests/test_search_strategy.py tests/test_search_stream.py
```

Result: `All checks passed`

The full API suite completed with `612 passed, 1 skipped, 6 failed, 1 error`.
The remaining failures/errors are outside this diff and are environment or
existing-baseline issues: agentic time-tool expectation, GB18030 text
normalization, platform path/permission behavior on Windows, settings/LLM
defaults, and SQLite teardown locking. No Phase 3 focused test failed.

### Impact and handoff

```
Database: NO
Migration: NO
Schema/config contract: NO
Ingestion/index lane: NO
Retrieval fusion/evidence/citation: NO
Security: no raw Laya exception is returned in the query trace
```

Next phase may build retrieval fusion/evidence/citation on top of the stable
`query_route` trace; it must keep the Phase 3 high-confidence CHAT gate and
retrieval fallback behavior.
