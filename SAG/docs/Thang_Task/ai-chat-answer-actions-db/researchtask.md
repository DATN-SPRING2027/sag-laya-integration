# AI Chat answer actions — database foundation

## Scope and provenance

Thang approved the answer-actions specification and implementation plan on
2026-10-30. This isolated database branch starts at `origin/main` `cd3b7a6`.
The application/API, backend and frontend are separate deliverables. No live
database operation is authorized by this record or has been performed here.

Verified persistence is SQLAlchemy in `apps/api/sag_api/db/models`; runtime
`core/db.py:init_db` creates new model tables and idempotently adds registered
development columns. Numbered PostgreSQL SQL scripts `migrations/0001..0009`
are the actual reviewed migration mechanism. Although the proposal named
Alembic, no Alembic environment exists; parent confirmed on 2026-10-30 to add
`0010_ai_answer_actions.sql` rather than introduce another migration system.

## Current contracts and boundaries

Canonical threads/messages are SAG-owned. Existing Message IDs are strings;
no MongoDB model or data will change. The new feedback table permits one
current vote per message/voter. The share table stores only the selected
question/answer/citations and source IDs, with a live-share uniqueness key.
Both reference messages and are deleted with their referenced content.
Nullable Message self-relations preserve old rows without guessing parents.

Database FKs and checks protect existence, allowed vote/reason values and
uniqueness. Current Organization/Project membership, exact conversation
ownership, same-thread parents, source ACL checks and platform permissions
remain application responsibilities; table existence is not authorization.

## File plan and test matrix

- `db/models/answer_actions.py`: feedback/share tables, constraints/indexes.
- `db/models/agent.py`: nullable reply/regeneration message FKs, SET NULL.
- `db/models/__init__.py`: model registration.
- `core/db.py`: nullable dev column upgrades for existing message tables.
- `migrations/0010_ai_answer_actions.sql`: additive PostgreSQL migration.
- `scripts/0010_ai_answer_actions_down.sql`: manual owner-approved rollback script.
- `tests/test_answer_actions_schema.py`: real SQLite FK/constraint tests,
  selected PostgreSQL DDL compilation and isolated migration checks.

RED before implementation: missing model imports must fail. GREEN must prove
vote uniqueness, valid vote switching, invalid rating/reason rejection,
revoked-vs-live share uniqueness, cascade removal, message-relation SET NULL,
and raw migration parity. Existing session-schema tests guard shared metadata.

## Impact and rollback

Adds two tables, scoped lookup indexes and two nullable columns/FKs. No source,
index, provider, credential, permission or existing-message data backfill.
Apply the new SQL before dependent application deployment. Runtime dev startup
can add the nullable fields but is not a substitute for production migration
review. Disabling new APIs and reverting application code leaves data intact.
Removing schema would destroy feedback/shares: export them and obtain explicit
owner approval before any destructive rollback. No downgrade is run here.

## Results

Verified on isolated worktree `feat/Thang-ai-chat-answer-actions-db`:
- `pytest tests/test_answer_actions_schema.py tests/test_session_context_schema.py -q --tb=short`: passed (`25 passed`).
- Covered unique vote per voter/message, vote switching, invalid rating/reason/scope rejection, partial live-share uniqueness + revocation re-share, invalid share scope/self-reference rejection, cascade deletion on answer/question removal with `SET NULL` on regenerated sibling versions, PostgreSQL DDL compilation, and `0010_ai_answer_actions.sql` / `0010_ai_answer_actions_down.sql` parity.
- `ruff check` passed on new schema and test files.
