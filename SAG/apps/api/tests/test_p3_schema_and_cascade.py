"""TDD tests for Milestone P3 Packet 1: SearchUnit schema, migration 0013, and CASCADE FKs (S2-01, S2-08)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, event, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from sag_api.core import db as db_module
from sag_api.core.db import _COLUMN_UPGRADES
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.db.models.routing_rag import CanonicalBlock, SearchUnit

API_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0013_PATH = API_ROOT / "migrations" / "0013_index_vector_integrity.sql"


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:  # noqa: ANN001
        connection.execute("PRAGMA foreign_keys=ON")


def test_search_unit_model_and_column_upgrades_parity():
    pg_ddl = str(CreateTable(SearchUnit.__table__).compile(dialect=postgresql.dialect()))
    assert "char_start" in pg_ddl
    assert "char_end" in pg_ddl
    assert "text_snapshot" in pg_ddl

    fk_by_col = {
        next(iter(fk.column_keys)): fk for fk in SearchUnit.__table__.foreign_key_constraints
    }
    assert fk_by_col["block_from_id"].ondelete == "CASCADE"
    assert fk_by_col["block_to_id"].ondelete == "CASCADE"
    assert fk_by_col["document_version_id"].ondelete == "CASCADE"

    assert "search_units" in _COLUMN_UPGRADES
    su_upgrades = _COLUMN_UPGRADES["search_units"]
    assert su_upgrades["char_start"] == "INTEGER"
    assert su_upgrades["char_end"] == "INTEGER"
    assert su_upgrades["text_snapshot"] == "TEXT"


def test_migration_0013_sql_structure_and_guards():
    assert MIGRATION_0013_PATH.is_file(), "migrations/0013_index_vector_integrity.sql must exist"
    sql_text = MIGRATION_0013_PATH.read_text(encoding="utf-8")
    upper = sql_text.upper()

    assert "BEGIN;" in upper
    assert "COMMIT;" in upper
    assert "to_regclass('search_units') IS NOT NULL" in sql_text
    assert "ALTER TABLE search_units ADD COLUMN IF NOT EXISTS char_start INTEGER" in sql_text
    assert "ALTER TABLE search_units ADD COLUMN IF NOT EXISTS char_end INTEGER" in sql_text
    assert "ALTER TABLE search_units ADD COLUMN IF NOT EXISTS text_snapshot TEXT" in sql_text
    assert "pg_constraint" in sql_text
    assert "confdeltype <> 'c'" in sql_text
    assert "ON DELETE CASCADE" in upper
    assert "block_from_id" in sql_text
    assert "block_to_id" in sql_text


@pytest.mark.asyncio
async def test_init_db_upgrades_legacy_sqlite_search_units_and_cascades_delete(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy_p3.db"
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_fk(legacy_engine)

    # Create all current tables EXCEPT search_units, then create a legacy search_units without
    # char_start/char_end/text_snapshot and without ON DELETE CASCADE on block_from_id/block_to_id.
    async with legacy_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[t for name, t in Base.metadata.tables.items() if name != "search_units"],
            )
        )
        await conn.exec_driver_sql(
            """
            CREATE TABLE search_units (
                id VARCHAR(36) PRIMARY KEY,
                document_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
                block_from_id VARCHAR(36) NOT NULL REFERENCES canonical_blocks(id),
                block_to_id VARCHAR(36) NOT NULL REFERENCES canonical_blocks(id),
                security_partition_id VARCHAR(64) NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                token_count INTEGER NOT NULL,
                page_from INTEGER NOT NULL,
                page_to INTEGER NOT NULL,
                section_path VARCHAR(512) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.exec_driver_sql(
            "CREATE INDEX idx_search_units_version ON search_units (document_version_id)"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX idx_search_units_security ON search_units (security_partition_id)"
        )

        # Seed legacy data
        now_dt = datetime.now(UTC)
        now_iso = now_dt.isoformat()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    sessions = async_sessionmaker(legacy_engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            Source(
                id="src-1",
                name="S1",
                sag_source_config_id="cfg-1",
            )
        )
        await session.flush()
        session.add(
            Document(
                id="doc-1",
                source_id="src-1",
                filename="doc.txt",
                storage_path="local://doc.txt",
                tenant_id="t1",
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-1",
                document_id="doc-1",
                version_no=1,
                file_hash="fh-1",
                valid_from=now_dt,
            )
        )
        await session.flush()
        session.add(
            CanonicalBlock(
                id="blk-1",
                document_version_id="ver-1",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Intro",
                normalized_text="Hello world",
                content_hash="bh-1",
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO search_units ("
                "id, document_version_id, block_from_id, block_to_id, "
                "security_partition_id, content_hash, token_count, "
                "page_from, page_to, section_path, created_at"
                ") VALUES ("
                "'su-1', 'ver-1', 'blk-1', 'blk-1', 'sec-1', 'uh-1', 2, 1, 1, 'Intro', :ts"
                ")"
            ),
            {"ts": now_iso},
        )
        await session.commit()
    await legacy_engine.dispose()

    upgrade_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_fk(upgrade_engine)
    monkeypatch.setattr(db_module, "engine", upgrade_engine)
    monkeypatch.setattr(db_module, "publish_lock_engine", upgrade_engine)
    try:
        await db_module.init_db()
        # Verify idempotency on second call
        await db_module.init_db()

        async with upgrade_engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("search_units")}
            )
            assert {"char_start", "char_end", "text_snapshot"}.issubset(cols)

            indexes = await conn.run_sync(
                lambda sync_conn: {
                    idx["name"] for idx in inspect(sync_conn).get_indexes("search_units") if idx.get("name")
                }
            )
            assert {"idx_search_units_version", "idx_search_units_security"}.issubset(indexes)

            fk_rows = (await conn.exec_driver_sql("PRAGMA foreign_key_list('search_units')")).all()
            cb_fks = [row for row in fk_rows if row[2] == "canonical_blocks"]
            assert len(cb_fks) == 2
            assert all(str(row[6]).upper() == "CASCADE" for row in cb_fks)

            row = (await conn.execute(text("SELECT id, content_hash FROM search_units WHERE id = 'su-1'"))).one()
            assert row[0] == "su-1"
            assert row[1] == "uh-1"

        # Verify direct DELETE FROM canonical_blocks cascades cleanly to search_units under PRAGMA foreign_keys=ON
        async with upgrade_engine.begin() as conn:
            await conn.execute(delete(CanonicalBlock).where(CanonicalBlock.id == "blk-1"))
            remaining = await conn.scalar(select(SearchUnit.id).where(SearchUnit.id == "su-1"))
            assert remaining is None
    finally:
        await upgrade_engine.dispose()
