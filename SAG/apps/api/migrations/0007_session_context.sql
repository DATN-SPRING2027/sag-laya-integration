-- Derived only. Apply after 0006; no raw chat or owner backfill.
BEGIN;
CREATE TABLE session_context_indexes (
    id VARCHAR(64) PRIMARY KEY,
    thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    owner_key VARCHAR(64) NOT NULL,
    scope_fingerprint VARCHAR(64) NOT NULL,
    model_id VARCHAR(512) NOT NULL,
    generation VARCHAR(64) NOT NULL,
    snapshot_digest VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'indexing',
    checkpoint_json JSON NOT NULL DEFAULT '{}',
    features_json JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_session_context_scope UNIQUE(thread_id, owner_key, scope_fingerprint, model_id),
    CONSTRAINT ck_session_context_state CHECK (state IN ('indexing','ready','stale'))
);
CREATE INDEX ix_session_context_indexes_thread_id ON session_context_indexes(thread_id);
CREATE TABLE session_message_provenance (
    message_id VARCHAR(32) PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    owner_key VARCHAR(64) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    partition_fingerprint VARCHAR(64),
    source_ids_json JSON NOT NULL DEFAULT '[]',
    dependencies_json JSON NOT NULL DEFAULT '[]',
    origin VARCHAR(16) NOT NULL DEFAULT 'unknown',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_session_provenance_origin CHECK (origin IN ('recorded','unknown'))
);
COMMIT;
