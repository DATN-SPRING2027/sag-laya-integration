"""Asynchronous database engine and session management."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sag_api.core.config import settings
from sag_api.db.base import Base


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory if it does not exist for SQLite."""
    marker = "sqlite+aiosqlite:///"
    if url.startswith(marker):
        path = url[len(marker) :]
        if path and path not in (":memory:",):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
)

# SQLite: foreign keys enabled + concurrency friendly (WAL parallel read/write, busy_timeout
# lets writes wait instead of locking immediately; 30s ceiling covers slow CI disk contention)
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# Incremental columns for existing tables (lightweight dev migrations; production uses Alembic).
# create_all only creates new tables and does not alter existing tables, so we idempotently ADD COLUMN for evolving fields.
_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    "agents": {"is_default": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "documents": {
        "progress": "INTEGER NOT NULL DEFAULT 0",
        "token_usage": "BIGINT NOT NULL DEFAULT 0",
        "error_layer": "VARCHAR(16)",
        "error_stage": "VARCHAR(16)",
        "octx_installation_id": "VARCHAR(32)",
        "octx_document_id": "VARCHAR(36)",
        "is_active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "parser_provider": "VARCHAR(16)",
        "mineru_provider": "VARCHAR(16)",
        "mineru_model": "VARCHAR(16)",
        "parser_status": "VARCHAR(16)",
        "fallback_from": "VARCHAR(16)",
        "fallback_reason": "TEXT",
        "tenant_id": "VARCHAR(64) NOT NULL DEFAULT 'tenant_continuum_default'",
        "project_id": "VARCHAR(64)",
        "owner_id": "VARCHAR(64)",
        "logical_source_id": "VARCHAR(128)",
    },
    "threads": {"archived": "BOOLEAN NOT NULL DEFAULT FALSE"},
    "messages": {
        "attachments_json": "JSON",
        "steps_json": "JSON",
        "prompt_preview": "TEXT NOT NULL DEFAULT ''",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'ok'",
        "error_json": "JSON",
    },
    "universe_dirty_sources": {"revision": "INTEGER NOT NULL DEFAULT 1"},
}

# Existing tables also need newly introduced hot-path indexes. Keep these
# idempotent for local/embedded upgrades; production deployments can express
# the same DDL in their migration runner.
_INDEX_UPGRADES = (
    "CREATE INDEX IF NOT EXISTS ix_messages_thread_created_id ON messages (thread_id, created_at, id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_source_sag_source ON documents (source_id, sag_source_id)",
    "CREATE INDEX IF NOT EXISTS ix_documents_source_active_created ON documents (source_id, is_active, created_at)",
)


async def _ensure_columns() -> None:
    from sqlalchemy import inspect as sa_inspect

    def _existing(sync_conn, table: str) -> set[str] | None:
        insp = sa_inspect(sync_conn)
        if not insp.has_table(table):
            return None
        return {c["name"] for c in insp.get_columns(table)}

    async with engine.begin() as conn:
        for table, cols in _COLUMN_UPGRADES.items():
            existing = await conn.run_sync(_existing, table)
            if existing is None:
                continue
            for col, ddl in cols.items():
                if col not in existing:
                    await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


async def _ensure_indexes() -> None:
    async with engine.begin() as conn:
        for ddl in _INDEX_UPGRADES:
            await conn.exec_driver_sql(ddl)


async def init_db() -> None:
    """Initialize tables in development (production uses Alembic). Import models to register into metadata."""
    from sag_api.db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns()
    await _ensure_indexes()


async def dispose_db() -> None:
    await engine.dispose()
