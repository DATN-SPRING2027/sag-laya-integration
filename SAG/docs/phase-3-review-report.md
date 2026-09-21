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
