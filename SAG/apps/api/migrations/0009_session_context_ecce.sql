-- Chặng 2 (SAG-ECCE): Derived provenance units, bi-temporal decision ledger,
-- and multi-view graph edges for Zero-Token Session Context Management.
-- Additive migration: does not alter `threads`, `messages`, or migrations 0001..0008.
-- Rollback script: `SAG/apps/api/scripts/0009_session_context_ecce_down.sql`.

BEGIN;

CREATE TABLE IF NOT EXISTS session_units (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(256) NOT NULL,
    project_id VARCHAR(256) NOT NULL,
    thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    owner_key VARCHAR(64) NULL,
    unit_kind VARCHAR(16) NOT NULL DEFAULT 'TURN',
    ordinal INTEGER NOT NULL,
    episode_id VARCHAR(64) NOT NULL,
    episode_ordinal INTEGER NOT NULL DEFAULT 0,
    window_id VARCHAR(64) NULL,
    message_from_id VARCHAR(32) NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    message_to_id VARCHAR(32) NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    role VARCHAR(16) NULL,
    epistemic_status VARCHAR(24) NOT NULL DEFAULT 'FACT',
    topic_slot_key VARCHAR(128) NULL,
    is_anaphoric_or_short BOOLEAN NOT NULL DEFAULT FALSE,
    security_partition_ids_json JSON NOT NULL DEFAULT '[]',
    cited_source_ids_json JSON NOT NULL DEFAULT '[]',
    entity_mentions_json JSON NOT NULL DEFAULT '[]',
    sentences_json JSON NOT NULL DEFAULT '[]',
    term_freqs_json JSON NOT NULL DEFAULT '{}',
    doc_word_len INTEGER NOT NULL DEFAULT 1,
    token_count INTEGER NOT NULL DEFAULT 0,
    content_hash VARCHAR(64) NOT NULL,
    embedding_model_signature VARCHAR(128) NOT NULL DEFAULT 'deterministic-ngram-v1',
    qdrant_point_id VARCHAR(36) NULL,
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_session_units_thread_kind_ordinal
        UNIQUE (thread_id, unit_kind, ordinal),
    CONSTRAINT ck_session_units_tenant_id
        CHECK (length(trim(tenant_id)) > 0),
    CONSTRAINT ck_session_units_project_id
        CHECK (length(trim(project_id)) > 0),
    CONSTRAINT ck_session_units_unit_kind
        CHECK (unit_kind IN ('TURN', 'WINDOW', 'EPISODE')),
    CONSTRAINT ck_session_units_ordinal_nonnegative
        CHECK (ordinal >= 0),
    CONSTRAINT ck_session_units_episode_id
        CHECK (length(trim(episode_id)) > 0),
    CONSTRAINT ck_session_units_episode_ordinal_nonnegative
        CHECK (episode_ordinal >= 0),
    CONSTRAINT ck_session_units_epistemic_status
        CHECK (epistemic_status IN (
            'FACT', 'PROPOSED', 'CONSIDERING', 'CONFIRMED',
            'SUPERSEDED', 'NEGATED', 'OPEN_ISSUE'
        )),
    CONSTRAINT ck_session_units_doc_word_len_nonnegative
        CHECK (doc_word_len >= 0),
    CONSTRAINT ck_session_units_token_count_nonnegative
        CHECK (token_count >= 0),
    CONSTRAINT ck_session_units_content_hash_len
        CHECK (length(content_hash) = 64),
    CONSTRAINT ck_session_units_embedding_sig
        CHECK (length(trim(embedding_model_signature)) > 0),
    CONSTRAINT ck_session_units_valid_window
        CHECK (valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS ix_session_units_project_thread
    ON session_units (tenant_id, project_id, thread_id, unit_kind, ordinal);
CREATE INDEX IF NOT EXISTS ix_session_units_thread_episode
    ON session_units (thread_id, episode_id, ordinal);
CREATE INDEX IF NOT EXISTS ix_session_units_message_from
    ON session_units (message_from_id);
CREATE INDEX IF NOT EXISTS ix_session_units_message_to
    ON session_units (message_to_id);
CREATE INDEX IF NOT EXISTS ix_session_units_temporal
    ON session_units (thread_id, epistemic_status, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS session_decision_ledger (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(256) NOT NULL,
    project_id VARCHAR(256) NOT NULL,
    thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    session_unit_id VARCHAR(36) NOT NULL REFERENCES session_units(id) ON DELETE CASCADE,
    ledger_kind VARCHAR(24) NOT NULL DEFAULT 'DECISION',
    topic_slot_key VARCHAR(128) NOT NULL,
    turn_ordinal INTEGER NOT NULL,
    epistemic_status VARCHAR(24) NOT NULL,
    summary_text TEXT NOT NULL,
    entity_keys_json JSON NOT NULL DEFAULT '[]',
    superseded_entity_keys_json JSON NOT NULL DEFAULT '[]',
    companion_unit_ids_json JSON NOT NULL DEFAULT '[]',
    supersedes_id VARCHAR(36) NULL REFERENCES session_decision_ledger(id) ON DELETE SET NULL,
    superseded_by_id VARCHAR(36) NULL REFERENCES session_decision_ledger(id) ON DELETE SET NULL,
    supersedes_ids_json JSON NOT NULL DEFAULT '[]',
    security_partition_ids_json JSON NOT NULL DEFAULT '[]',
    cited_source_ids_json JSON NOT NULL DEFAULT '[]',
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_to TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_session_decision_unit_slot
        UNIQUE (session_unit_id, topic_slot_key),
    CONSTRAINT ck_session_decision_tenant_id
        CHECK (length(trim(tenant_id)) > 0),
    CONSTRAINT ck_session_decision_project_id
        CHECK (length(trim(project_id)) > 0),
    CONSTRAINT ck_session_decision_ledger_kind
        CHECK (ledger_kind IN ('DECISION', 'OPEN_TASK', 'CONSTRAINT', 'OPEN_ISSUE')),
    CONSTRAINT ck_session_decision_topic_slot_key
        CHECK (length(trim(topic_slot_key)) > 0),
    CONSTRAINT ck_session_decision_turn_ordinal_nonnegative
        CHECK (turn_ordinal >= 0),
    CONSTRAINT ck_session_decision_epistemic_status
        CHECK (epistemic_status IN (
            'FACT', 'PROPOSED', 'CONSIDERING', 'CONFIRMED',
            'SUPERSEDED', 'NEGATED', 'OPEN_ISSUE'
        )),
    CONSTRAINT ck_session_decision_valid_window
        CHECK (valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS ix_session_decision_thread_slot_status
    ON session_decision_ledger (thread_id, topic_slot_key, epistemic_status, turn_ordinal);
CREATE INDEX IF NOT EXISTS ix_session_decision_project_status
    ON session_decision_ledger (tenant_id, project_id, thread_id, epistemic_status);
CREATE INDEX IF NOT EXISTS ix_session_decision_supersedes
    ON session_decision_ledger (supersedes_id);
CREATE INDEX IF NOT EXISTS ix_session_decision_superseded_by
    ON session_decision_ledger (superseded_by_id);

CREATE TABLE IF NOT EXISTS session_graph_edges (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(256) NOT NULL,
    project_id VARCHAR(256) NOT NULL,
    thread_id VARCHAR(32) NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    source_unit_id VARCHAR(36) NOT NULL REFERENCES session_units(id) ON DELETE CASCADE,
    target_unit_id VARCHAR(36) NULL REFERENCES session_units(id) ON DELETE CASCADE,
    source_node_type VARCHAR(24) NOT NULL,
    source_node_key VARCHAR(256) NOT NULL,
    target_node_type VARCHAR(24) NOT NULL,
    target_node_key VARCHAR(256) NOT NULL,
    edge_type VARCHAR(32) NOT NULL,
    weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    metadata_json JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_session_graph_edges_provenance
        UNIQUE (source_unit_id, source_node_key, target_node_key, edge_type),
    CONSTRAINT ck_session_graph_edges_tenant_id
        CHECK (length(trim(tenant_id)) > 0),
    CONSTRAINT ck_session_graph_edges_project_id
        CHECK (length(trim(project_id)) > 0),
    CONSTRAINT ck_session_graph_edges_source_node_type
        CHECK (source_node_type IN ('UNIT', 'SENTENCE', 'ENTITY', 'EPISODE', 'SOURCE')),
    CONSTRAINT ck_session_graph_edges_target_node_type
        CHECK (target_node_type IN ('UNIT', 'SENTENCE', 'ENTITY', 'EPISODE', 'SOURCE')),
    CONSTRAINT ck_session_graph_edges_source_node_key
        CHECK (length(trim(source_node_key)) > 0),
    CONSTRAINT ck_session_graph_edges_target_node_key
        CHECK (length(trim(target_node_key)) > 0),
    CONSTRAINT ck_session_graph_edges_edge_type
        CHECK (edge_type IN (
            'ENTITY_MENTION', 'SENTENCE_IN_UNIT', 'ADJACENT_IN_EPISODE',
            'SUPERSEDES', 'ALIAS_CANDIDATE', 'CITES_DOCUMENT_SOURCE'
        )),
    CONSTRAINT ck_session_graph_edges_weight_nonnegative
        CHECK (weight >= 0.0)
);

CREATE INDEX IF NOT EXISTS ix_session_graph_edges_thread_type
    ON session_graph_edges (thread_id, edge_type);
CREATE INDEX IF NOT EXISTS ix_session_graph_edges_source_node
    ON session_graph_edges (thread_id, source_node_key);
CREATE INDEX IF NOT EXISTS ix_session_graph_edges_target_node
    ON session_graph_edges (thread_id, target_node_key);
CREATE INDEX IF NOT EXISTS ix_session_graph_edges_source_unit
    ON session_graph_edges (source_unit_id);
CREATE INDEX IF NOT EXISTS ix_session_graph_edges_target_unit
    ON session_graph_edges (target_unit_id);

COMMIT;
