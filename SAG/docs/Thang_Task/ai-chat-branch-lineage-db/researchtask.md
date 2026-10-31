# Research Task — AI Chat Branch Lineage Schema (`0012_ai_chat_branch_lineage`)

## 1. Context & Goal

Deliverable 1 (`feat/Thang-ai-chat-branch-lineage-db`) introduces the additive PostgreSQL and SQLAlchemy schema required for **Option 3 — Bi-Temporal Conversation Lineage (`Branch in new chat`)** without duplicating `messages`, `session_units`, or Qdrant points.

## 2. Inspected Files

- `SAG/apps/api/sag_api/db/base.py` (`Base`, `IDMixin`, `TimestampMixin`, `UTCDateTime`)
- `SAG/apps/api/sag_api/db/models/agent.py` (`Agent`, `Thread`, `Message`)
- `SAG/apps/api/sag_api/db/models/session_context.py` (PostgreSQL + SQLite `DDL` trigger conventions)
- `SAG/apps/api/sag_api/core/db.py` (`_COLUMN_UPGRADES`, `_INDEX_UPGRADES`, `_ensure_columns`, `_ensure_indexes`)
- `SAG/apps/api/migrations/0010_ai_answer_actions.sql` and `SAG/apps/api/scripts/0010_ai_answer_actions_down.sql`
- `SAG/apps/api/tests/test_answer_actions_schema.py` and `SAG/apps/api/tests/test_session_context_schema.py`

## 3. Schema Design & Database Invariants

### Additive Columns on `threads`
1. `parent_thread_id VARCHAR(32) NULL REFERENCES threads(id) ON DELETE RESTRICT`
2. `branch_point_message_id VARCHAR(32) NULL REFERENCES messages(id) ON DELETE RESTRICT`
   - Modeled in SQLAlchemy with `use_alter=True, name="fk_threads_branch_point_message_id"` so `Base.metadata.create_all` / `drop_all` handles the `threads <-> messages` cycle cleanly on PostgreSQL while emitting the inline FK constraint on SQLite (`PRAGMA foreign_keys=ON`).
3. `deleted_by_owner_at TIMESTAMP WITH TIME ZONE NULL` (`UTCDateTime()` in ORM)
   - Supports **Tombstone Reference-Counted GC**: when an owner deletes a parent thread that still has child branches, `deleted_by_owner_at` hides the parent thread from `list_threads`/`get_thread` while preserving backing `messages`, `session_units`, and Qdrant vectors until the last descendant is deleted.

### Constraints, Triggers & Indexes
- `ck_threads_branch_pair`: `(parent_thread_id IS NULL AND branch_point_message_id IS NULL) OR (parent_thread_id IS NOT NULL AND branch_point_message_id IS NOT NULL)`
- `ck_threads_no_self_parent`: `parent_thread_id IS NULL OR parent_thread_id <> id`
- `trg_threads_branch_scope_ins` / `trg_threads_branch_lineage_immutable_upd` (PostgreSQL trigger function + SQLite `DDL` triggers):
  1. Verifies `parent_thread_id` shares `(agent_id, owner_key)` with the child thread.
  2. Verifies `branch_point_message_id` belongs physically to `parent_thread_id` (`Direct Origin Pointer` invariant).
  3. Rejects any `UPDATE` that mutates `parent_thread_id` or `branch_point_message_id`, making lineage cycles (`A -> B -> A`) mathematically impossible in storage.
- Indexes (preserving space before `(` in `_INDEX_UPGRADES` for `core/db.py` parser compatibility):
  - `ix_threads_parent_thread_id ON threads (parent_thread_id)`
  - `ix_threads_branch_point_message_id ON threads (branch_point_message_id)`
  - `ix_threads_owner_visible ON threads (agent_id, owner_key, archived, updated_at DESC) WHERE deleted_by_owner_at IS NULL`

## 4. Migration & Rollback Considerations

- **Forward Migration:** `SAG/apps/api/migrations/0012_ai_chat_branch_lineage.sql` — purely additive (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, idempotent constraint & trigger installation). Existing threads remain root threads (`NULL` lineage columns, `deleted_by_owner_at IS NULL`) with zero downtime or backfill required.
- **Manual Rollback:** `SAG/apps/api/scripts/0012_ai_chat_branch_lineage_down.sql` — drops triggers, indexes, constraints, and the 3 columns on `threads` (must only run after rolling back API routes and exporting/handling any branched threads).
