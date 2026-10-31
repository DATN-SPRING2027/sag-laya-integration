# AI Chat Mind Map V1.1 — Project Snapshot Persistence Database Schema

## Scope and provenance

Thang requested server-side Project Mind Map persistence on 2026-10-31 and confirmed option A1 (`"Lưu trên Server theo Project (mọi thành viên trong Project mở lên đều thấy ngay 0s loading; chỉ gen lại khi bấm Tạo lại)"`) and option A2 (Studio UI/UX redesign).
This isolated database branch `feat/Thang-ai-chat-mind-map-persistence-db` is created fresh from `origin/main` (`aada453`). Application/API (`sag-laya-integration` `-be-api`), BFF (`DATN_BE`), and Frontend (`DATN_FE`) are delivered on separate branches/PRs per `AGENTS.md`.

## Current contracts and security boundaries

- `SAG` owns canonical AI Chat artifacts (`threads`, `messages`, `answer_feedback`, `answer_shares`) and search/ACL metadata (`documents`, `document_versions`, `source_project_mappings`, `search_units`).
- Runtime `sag_api/core/db.py:init_db` creates newly registered SQLAlchemy tables on startup in dev/embedded setups, while numbered SQL files under `SAG/apps/api/migrations/` (`0001..0010`) and manual rollback scripts under `SAG/apps/api/scripts/` (`*_down.sql`) serve as the reviewed PostgreSQL schema mechanism.
- A persisted Mind Map snapshot belongs to `(tenant_id, project_id, partition_scope_hash, topic_key)`:
  - `partition_scope_hash` is the SHA-256 of sorted `principal.allowed_partition_ids`, preventing cross-partition disclosure while sharing snapshots among authorized members of the same Project partition.
  - `topic_key` is `""` for the full-project overview or casefolded trimmed `topic` (`<= 200` chars).
  - `used_document_version_ids` (`JSON` array) records every `DocumentVersion.id` used when synthesizing the snapshot so application reads can verify `saved_used_ids.issubset(live_authorized_ready_ids)` in O(1) — immediately invalidating snapshots if any contributing document version is revoked, deleted, superseded, or removed from partition, while continuing to serve snapshots in 0ms when new documents are added (`hasSourceUpdates = True`).
- Table existence alone is never authorization; BFF token + project principal + live `ready_docs` checks remain enforced in the service layer.

## Planned files and test matrix

- `SAG/apps/api/sag_api/db/models/mind_map.py`: `ProjectMindMapSnapshot` (`__tablename__ = "project_mind_maps"`) with non-empty scope checks, valid `generation_mode` / `fallback_reason` checks, `UniqueConstraint("tenant_id", "project_id", "partition_scope_hash", "topic_key")`, and `ix_project_mind_maps_project_updated` index.
- `SAG/apps/api/sag_api/db/models/__init__.py`: register and export `ProjectMindMapSnapshot`.
- `SAG/apps/api/migrations/0011_project_mind_map_snapshots.sql`: additive PostgreSQL DDL (`CREATE TABLE IF NOT EXISTS project_mind_maps ...`).
- `SAG/apps/api/scripts/0011_project_mind_map_snapshots_down.sql`: manual owner-approved rollback script.
- `SAG/apps/api/tests/test_mind_map_schema.py`: RED-first SQLite persistence/constraint/uniqueness tests, PostgreSQL DDL compilation, and `0011` up/down SQL parity verification.

## Impact and rollback

- **Database impact**: Adds one additive table `project_mind_maps` plus its unique constraint and lookup index. Does not modify existing tables, migrations (`0001..0010`), ingestion pipelines, or search indexes.
- **Migration order**: Apply `0011_project_mind_map_snapshots.sql` before deploying the dependent `-be-api` snapshot read/upsert service (`init_db()` also creates the table idempotently in local development).
- **Rollback**: Revert dependent API code first; data in `project_mind_maps` is strictly derived/regenerable synthesis state, and `scripts/0011_project_mind_map_snapshots_down.sql` requires explicit owner approval before manual execution.

## Results

Verified on isolated database branch `feat/Thang-ai-chat-mind-map-persistence-db`:
- `pytest tests/test_mind_map_schema.py tests/test_answer_actions_schema.py -q --tb=short`: **34 passed** (confirmed RED `ImportError` prior to model creation, caught and hardened SQL three-valued logic `IS NOT NULL` check for `outline_fallback` `fallback_reason`, verified uniqueness on `(tenant_id, project_id, partition_scope_hash, topic_key)`, PostgreSQL DDL compilation, and `0011_project_mind_map_snapshots.sql` / `0011_project_mind_map_snapshots_down.sql` round-trip parity).
- `ruff check sag_api/db/models/mind_map.py sag_api/db/models/__init__.py tests/test_mind_map_schema.py`: **All checks passed**.
