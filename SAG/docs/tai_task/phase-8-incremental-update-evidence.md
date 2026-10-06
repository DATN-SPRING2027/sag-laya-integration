# Phase 8 — Incremental update/subtree evidence

**Date:** 2026-10-06
**Scope:** DATN-58/DATN-59 tree update and targeted subtree build lane. DATN-35 / `ROUTING_READY` is accepted as the completed dependency provided by the task.

## Changes

- Added an idempotent base+delta update path. Replayed Knowledge Unit IDs are skipped when their canonical features match; reusing an ID with changed features or scope fails closed.
- Updated the affected leaf and its ancestor membership/profile summaries without changing tree topology during normal assignment. The snapshot manifest now records tree/delta membership, assignment counts, drift signals, hysteresis state, and rebuild targets.
- Rebuilds only drift-selected subtrees. Sibling branches retain their existing node objects and IDs. A sufficiently persistent new partition builds only its own root. Stable matching sorts ties deterministically and records split/merge/supersedes lineage.
- Added a service-level ingest-delta coordinator over caller-provided Knowledge Units. No production normal-ingestion caller supplies this contract yet; DATN-58/59 producer integration remains open. The update tests call the service directly.
- Cross-partition graph edges are dropped from the incremental manifest, matching the Phase 6 builder.
- Subtree quality results accumulate across the entire rebuild batch, so a later passing build cannot clear an earlier failure.

## Checks run

From `SAG/apps/api`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_checkpoint_c_incremental.py tests\test_checkpoint_c_incremental_update.py -q
# 21 passed

.\.venv\Scripts\python.exe -m ruff check sag_api\services\incremental_tree_service.py tests\test_checkpoint_c_incremental_update.py
# All checks passed
```

`git diff --check` also passed.

The focused regressions cover base+delta assignment, retry idempotency, selected subtree/partition rebuilds, accumulated subtree quality failures, cross-partition edge exclusion, stable sibling nodes, and lineage matching. They exercise the service contract directly; no normal-ingestion producer or production publish integration is established here. Coordinator tests use SQLite and mocked Qdrant.

## Boundary

This record covers the update and subtree-build service contract over `KnowledgeUnitInput`. DATN-35 completion is taken from the task dependency, not revalidated here. Wiring DATN-58/59 into normal ingestion, production ACL issuer/JWKS, Project→Source mapping, staging cross-tenant checks, publish/snapshot/rollback acceptance, and real Qdrant verification remain outside this slice.
