-- Disable session-context readers/jobs first. Raw messages and threads are preserved.
BEGIN;
DROP TABLE session_context_indexes;
DROP TABLE session_message_provenance;
COMMIT;
