# Phase 8 — Incremental update/subtree evidence

**Date:** 2026-10-05
**Scope:** DATN-58/DATN-59 tree update and targeted subtree build lane. DATN-35 / `ROUTING_READY` is accepted as the completed dependency provided by the task.

## Changes

- Added an idempotent base+delta update path. Replayed Knowledge Unit IDs are skipped when their canonical features match; reusing an ID with changed features or scope fails closed.
- Updated the affected leaf and its ancestor membership/profile summaries without changing tree topology during normal assignment. The snapshot manifest now records tree/delta membership, assignment counts, drift signals, hysteresis state, and rebuild targets.
- Rebuilds only drift-selected subtrees. Sibling branches retain their existing node objects and IDs. A sufficiently persistent new partition builds only its own root. Stable matching sorts ties deterministically and records split/merge/supersedes lineage.
- Routed the existing ingest-delta coordinator through this update lane. This task did not change the publisher, query snapshot lease, rollback, ACL resolver, or Search Unit ingestion/index path.

## Checks run

From `SAG/apps/api`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_checkpoint_c_incremental.py tests\test_checkpoint_c_incremental_update.py -q
# 19 passed

.\.venv\Scripts\python.exe -m pytest tests\test_retrieval_relevance.py tests\test_search_strategy.py tests\test_search_stream.py tests\test_search_unit_retrieval_service.py -q
# 93 passed

.\.venv\Scripts\python.exe -m ruff check sag_api\services\incremental_tree_service.py tests\test_checkpoint_c_incremental_update.py tests\test_checkpoint_c_incremental.py sag_api\services\retrieval_service.py tests\test_retrieval_relevance.py tests\test_search_strategy.py tests\test_search_stream.py tests\test_search_unit_retrieval_service.py
# All checks passed
```

`git diff --check` also passed.

The new focused regressions prove the normal path does not call the tree builder, ancestor summaries and manifest counts update, retries are idempotent, capacity and centroid drift select only the affected leaf, untouched siblings remain unchanged, new partitions build independently, and lineage matching is order-independent with split/merge evidence. The existing coordinator test uses SQLite and mocked Qdrant; it is not production publish, ACL, or real-Qdrant evidence.

## Boundary

This record covers the update and subtree-build contract over `KnowledgeUnitInput`. DATN-35 completion is taken from the task dependency, not revalidated here. Production ACL issuer/JWKS, Project→Source mapping, staging cross-tenant checks, publish/snapshot/rollback acceptance, and real Qdrant verification remain outside this slice.
