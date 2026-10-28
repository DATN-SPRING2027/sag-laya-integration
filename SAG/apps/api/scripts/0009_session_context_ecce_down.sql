-- Rollback script for `migrations/0009_session_context_ecce.sql`.
-- Drops derived SAG-ECCE session context tables and indexes in reverse dependency order.
-- Raw conversation history in `threads` and `messages` remains 100% intact.

BEGIN;

DROP INDEX IF EXISTS ix_session_graph_edges_target_unit;
DROP INDEX IF EXISTS ix_session_graph_edges_source_unit;
DROP INDEX IF EXISTS ix_session_graph_edges_target_node;
DROP INDEX IF EXISTS ix_session_graph_edges_source_node;
DROP INDEX IF EXISTS ix_session_graph_edges_thread_type;
DROP TABLE IF EXISTS session_graph_edges;

DROP INDEX IF EXISTS ix_session_decision_superseded_by;
DROP INDEX IF EXISTS ix_session_decision_supersedes;
DROP INDEX IF EXISTS ix_session_decision_project_status;
DROP INDEX IF EXISTS ix_session_decision_thread_slot_status;
DROP TABLE IF EXISTS session_decision_ledger;

DROP INDEX IF EXISTS ix_session_units_temporal;
DROP INDEX IF EXISTS ix_session_units_message_to;
DROP INDEX IF EXISTS ix_session_units_message_from;
DROP INDEX IF EXISTS ix_session_units_thread_episode;
DROP INDEX IF EXISTS ix_session_units_project_thread;
DROP TABLE IF EXISTS session_units;

COMMIT;
