-- Temporal authority metadata on document_versions (Phase 1)
-- and cross-document unit-level conflict resolution overlay table (Phase 2).
-- Dual-dialect compatible with SAG ORM (PostgreSQL 16 + SQLite via core/db.py).
-- 100% idempotent (safe to run before or after SQLAlchemy Base.metadata.create_all).
BEGIN;

DO $$
BEGIN
    IF to_regclass('document_versions') IS NOT NULL THEN
        ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS authority_tier INTEGER;
        ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS effective_from TIMESTAMP WITH TIME ZONE;
        ALTER TABLE document_versions ADD COLUMN IF NOT EXISTS scope_tags_json JSON;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'ck_document_versions_authority_tier'
              AND conrelid = 'document_versions'::regclass
        ) THEN
            ALTER TABLE document_versions
                ADD CONSTRAINT ck_document_versions_authority_tier
                CHECK (authority_tier IS NULL OR (authority_tier >= 1 AND authority_tier <= 4));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_document_versions_effective_authority
    ON document_versions (document_id, effective_from, authority_tier);

CREATE TABLE IF NOT EXISTS knowledge_conflict_resolutions (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    security_partition_id VARCHAR(64) NOT NULL,
    winner_security_partition_id VARCHAR(64) NOT NULL,
    pair_key VARCHAR(64) NOT NULL,
    winner_document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    winner_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    winner_unit_id VARCHAR(36) NOT NULL,
    winner_content_hash VARCHAR(64) NOT NULL,
    loser_document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    loser_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    loser_unit_id VARCHAR(36) NOT NULL,
    loser_content_hash VARCHAR(64) NOT NULL,
    conflict_subtype VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING_REVIEW',
    resolution_mode VARCHAR(32) NOT NULL DEFAULT 'UNRESOLVED',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    cosine_similarity DOUBLE PRECISION,
    effective_valid_to TIMESTAMP WITH TIME ZONE,
    scope_difference_summary TEXT,
    reason TEXT NOT NULL DEFAULT '',
    degraded_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    graph_edge_id VARCHAR(36) REFERENCES knowledge_graph_edges(id) ON DELETE SET NULL,
    evidence_json JSON NOT NULL DEFAULT '{}',
    audit_history_json JSON NOT NULL DEFAULT '[]',
    resolved_by VARCHAR(128),
    resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_kcr_project_pair UNIQUE (project_id, pair_key),
    CONSTRAINT ck_kcr_distinct_units CHECK (winner_unit_id <> loser_unit_id),
    CONSTRAINT ck_kcr_distinct_versions CHECK (winner_version_id <> loser_version_id),
    CONSTRAINT ck_kcr_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT ck_kcr_status CHECK (
        status IN (
            'PENDING_REVIEW',
            'ACTIVE',
            'DISMISSED_SCOPED',
            'REVOKED_WINNER',
            'NEEDS_REVALIDATION',
            'SUPERSEDED_BY_UPDATE'
        )
    ),
    CONSTRAINT ck_kcr_resolution_mode CHECK (
        resolution_mode IN (
            'AUTO_POLICY',
            'MANUAL_HITL',
            'UNRESOLVED',
            'KEEP_BOTH_SCOPED'
        )
    ),
    CONSTRAINT ck_kcr_conflict_subtype CHECK (
        conflict_subtype IN (
            'DIRECT_SUPERSEDES',
            'HARD_CONTRADICTION',
            'SCOPED_EXCEPTION',
            'DIFFERENT_PERSPECTIVE',
            'SUPPORTS_OR_EQUIVALENT'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_kcr_project_pair
    ON knowledge_conflict_resolutions (project_id, pair_key);
CREATE INDEX IF NOT EXISTS idx_kcr_project_status_partition
    ON knowledge_conflict_resolutions (tenant_id, project_id, status, security_partition_id);
CREATE INDEX IF NOT EXISTS idx_kcr_loser_unit_status
    ON knowledge_conflict_resolutions (project_id, loser_unit_id, status);
CREATE INDEX IF NOT EXISTS idx_kcr_winner_version_status
    ON knowledge_conflict_resolutions (winner_version_id, status);
CREATE INDEX IF NOT EXISTS idx_kcr_loser_version_status
    ON knowledge_conflict_resolutions (loser_version_id, status);
CREATE INDEX IF NOT EXISTS idx_kcr_winner_document
    ON knowledge_conflict_resolutions (winner_document_id);
CREATE INDEX IF NOT EXISTS idx_kcr_loser_document
    ON knowledge_conflict_resolutions (loser_document_id);

COMMIT;
