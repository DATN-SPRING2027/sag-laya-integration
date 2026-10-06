# [SAG][Checkpoint C] Failure injection, rollback and acceptance evidence

## Purpose

Prove Checkpoint C remains available during build and publish failures, and that rollback restores one consistent PostgreSQL/Qdrant state.

## Scope

- Inject failures in subtree build, Qdrant inactive-slot batch writes, manifest/count/checksum/quality verification, and pointer switch.
- Verify failed candidates are rejected and the previous active tree continues serving.
- Verify rollback within the retention window restores matching PostgreSQL active pointer/manifest and Qdrant slot.
- Exercise concurrent queries through publish and rollback; confirm each uses one pinned snapshot.
- Record test/evidence and update all Checkpoint C items in `SAG/tasks/todo.md`.

## Acceptance

- No injected pre-switch failure changes active state or interrupts old-tree queries.
- Rollback produces matching PostgreSQL/Qdrant state and queries resume with the restored snapshot.
- Evidence covers success, verification failure, partial Qdrant write, switch failure, rollback, and concurrent reads.

## Dependencies

- DATN-58 candidate update/subtree build contract.
- DATN-59.
- DATN-35 / `ROUTING_READY` is complete.
