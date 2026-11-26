-- Milestone P4 (S2-03, S1-07): CanonicalBlock.is_boilerplate and Document.logical_source_id VARCHAR(512).
BEGIN;

DO $$
BEGIN
    IF to_regclass('canonical_blocks') IS NOT NULL THEN
        ALTER TABLE canonical_blocks
            ADD COLUMN IF NOT EXISTS is_boilerplate BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;

    IF to_regclass('documents') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM pg_attribute
            WHERE attrelid = 'documents'::regclass
              AND attname = 'logical_source_id'
              AND attnum > 0
              AND NOT attisdropped
              AND (
                  atttypid <> 'character varying'::regtype
                  OR atttypmod < 516
              )
        ) THEN
            ALTER TABLE documents
                ALTER COLUMN logical_source_id TYPE VARCHAR(512);
        END IF;
    END IF;
END $$;

COMMIT;
