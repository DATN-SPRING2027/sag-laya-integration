"""TDD tests for Milestone P4 Commit 1: DB schema & migration 0014 (S2-03, S1-07)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from sag_api.core import db as db_module
from sag_api.core.db import _COLUMN_UPGRADES
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.db.models.routing_rag import CanonicalBlock

API_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0014_PATH = API_ROOT / "migrations" / "0014_parsing_vietnamese_schema.sql"


def test_p4_models_and_column_upgrades_parity():
    cb_ddl = str(CreateTable(CanonicalBlock.__table__).compile(dialect=postgresql.dialect()))
    assert "is_boilerplate" in cb_ddl

    doc_ddl = str(CreateTable(Document.__table__).compile(dialect=postgresql.dialect()))
    assert "logical_source_id VARCHAR(512)" in doc_ddl

    assert _COLUMN_UPGRADES["documents"]["logical_source_id"] == "VARCHAR(512)"
    assert _COLUMN_UPGRADES["canonical_blocks"]["is_boilerplate"] == "BOOLEAN NOT NULL DEFAULT FALSE"


def test_migration_0014_sql_structure_and_guards():
    assert MIGRATION_0014_PATH.is_file(), "migrations/0014_parsing_vietnamese_schema.sql must exist"
    sql_text = MIGRATION_0014_PATH.read_text(encoding="utf-8")
    upper = sql_text.upper()

    assert "BEGIN;" in upper
    assert "COMMIT;" in upper
    assert "to_regclass('canonical_blocks') IS NOT NULL" in sql_text
    assert "ADD COLUMN IF NOT EXISTS is_boilerplate BOOLEAN NOT NULL DEFAULT FALSE" in sql_text
    assert "to_regclass('documents') IS NOT NULL" in sql_text
    assert "atttypmod < 516" in sql_text
    assert "ALTER COLUMN logical_source_id TYPE VARCHAR(512)" in sql_text


def test_ensure_document_schema_widths_checks_logical_source_id():
    import inspect as py_inspect

    src = py_inspect.getsource(db_module._ensure_document_schema_widths)
    assert "logical_source_id" in src
    assert "516" in src
    assert "ALTER TABLE documents ALTER COLUMN logical_source_id TYPE VARCHAR(512)" in src


@pytest.mark.asyncio
async def test_init_db_upgrades_legacy_canonical_blocks_is_boilerplate(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy_p4.db"
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with legacy_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[t for name, t in Base.metadata.tables.items() if name != "canonical_blocks"],
            )
        )
        await conn.exec_driver_sql(
            """
            CREATE TABLE canonical_blocks (
                id VARCHAR(36) PRIMARY KEY,
                document_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                block_type VARCHAR(32) NOT NULL,
                page_from INTEGER NOT NULL,
                page_to INTEGER NOT NULL,
                section_path VARCHAR(512) NOT NULL,
                source_anchor VARCHAR(256),
                normalized_text TEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                dedup_signature_json JSON,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    sessions = async_sessionmaker(legacy_engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Source(id="src-1", name="S1", sag_source_config_id="cfg-1"))
        await session.flush()
        session.add(
            Document(
                id="doc-1",
                source_id="src-1",
                filename="test.md",
                storage_path="/tmp/test.md",
                logical_source_id="source:src-1:" + ("x" * 200),
            )
        )
        await session.flush()
        now_dt = datetime.now(UTC)
        session.add(
            DocumentVersion(
                id="ver-1",
                document_id="doc-1",
                version_no=1,
                file_hash="a" * 64,
                valid_from=now_dt,
            )
        )
        await session.commit()

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    await db_module.init_db()

    async with legacy_engine.begin() as conn:
        cols = await conn.run_sync(
            lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns("canonical_blocks")}
        )
        assert "is_boilerplate" in cols
        await conn.execute(
            text(
                """
                INSERT INTO canonical_blocks
                    (id, document_version_id, ordinal, block_type, page_from, page_to,
                     section_path, normalized_text, content_hash)
                VALUES
                    ('blk-1', 'ver-1', 0, 'paragraph', 1, 1, 'Intro', 'Hello', 'h1')
                """
            )
        )

    async with sessions() as session:
        blk = (await session.execute(select(CanonicalBlock).where(CanonicalBlock.id == "blk-1"))).scalar_one()
        assert blk.is_boilerplate is False
        doc = (await session.execute(select(Document).where(Document.id == "doc-1"))).scalar_one()
        assert doc.logical_source_id is not None and len(doc.logical_source_id) > 128

    await legacy_engine.dispose()
