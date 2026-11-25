-- Milestone P3 (S2-01, S2-08): SearchUnit slice/snapshot columns and ON DELETE CASCADE FKs.
BEGIN;

DO $$
DECLARE
    fk RECORD;
BEGIN
    IF to_regclass('search_units') IS NOT NULL THEN
        ALTER TABLE search_units ADD COLUMN IF NOT EXISTS char_start INTEGER;
        ALTER TABLE search_units ADD COLUMN IF NOT EXISTS char_end INTEGER;
        ALTER TABLE search_units ADD COLUMN IF NOT EXISTS text_snapshot TEXT;

        IF to_regclass('canonical_blocks') IS NOT NULL THEN
            FOR fk IN
                SELECT
                    c.conname,
                    local_col.attname AS column_name
                FROM pg_constraint c
                CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS keys(local_attnum, target_attnum)
                JOIN pg_attribute local_col
                    ON local_col.attrelid = c.conrelid AND local_col.attnum = keys.local_attnum
                WHERE c.contype = 'f'
                  AND c.conrelid = 'search_units'::regclass
                  AND c.confrelid = 'canonical_blocks'::regclass
                  AND c.confdeltype <> 'c'
                  AND local_col.attname IN ('block_from_id', 'block_to_id')
            LOOP
                EXECUTE format(
                    'ALTER TABLE search_units DROP CONSTRAINT %I',
                    fk.conname
                );
                EXECUTE format(
                    'ALTER TABLE search_units ADD CONSTRAINT %I FOREIGN KEY (%I) REFERENCES canonical_blocks(id) ON DELETE CASCADE',
                    fk.conname,
                    fk.column_name
                );
            END LOOP;
        END IF;
    END IF;
END $$;

COMMIT;
