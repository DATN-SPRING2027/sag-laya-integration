"""TDD tests for Phase 1 Wave 1.0 Foundation:
- Migration 0015 (temporal authority on document_versions & knowledge_conflict_resolutions)
- Dual-dialect ORM / core/db.py parity & legacy SQLite upgrade
- StageRun.created_at microsecond clock-skew fix
- AuthorityTier, Conflict*, JobType.CONFLICT_RESOLUTION & scope-tag helpers
- Config flags & bounds validators
- RetrievedSection additive DTO fields & SectionOut BFF compatibility
- Pure citation-safe temporal metadata formatter
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from sag_api.core import db as db_module
from sag_api.core.config import Settings
from sag_api.core.db import _COLUMN_UPGRADES, _INDEX_UPGRADES
from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    DocumentVersion,
    IngestionRun,
    KnowledgeGraphEdge,
    Source,
    StageRun,
)
from sag_api.db.models.routing_rag import CanonicalBlock, SearchUnit
from sag_api.enums import JobType
from sag_api.sag.dto import RetrievedSection
from sag_api.schemas.search import SectionOut

API_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0015_PATH = (
    API_ROOT / "migrations" / "0015_temporal_authority_and_unit_supersessions.sql"
)


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


# ---------------------------------------------------------------------------
# 1. Migration 0015 SQL structure & idempotency
# ---------------------------------------------------------------------------


def test_migration_0015_sql_structure_idempotency_and_constraints():
    assert MIGRATION_0015_PATH.is_file(), (
        "migrations/0015_temporal_authority_and_unit_supersessions.sql must exist"
    )
    sql = MIGRATION_0015_PATH.read_text(encoding="utf-8")
    upper = sql.upper()

    assert "BEGIN;" in upper
    assert "COMMIT;" in upper
    assert "to_regclass('document_versions') IS NOT NULL" in sql
    assert "ADD COLUMN IF NOT EXISTS authority_tier INTEGER" in sql
    assert "ADD COLUMN IF NOT EXISTS effective_from TIMESTAMP WITH TIME ZONE" in sql
    assert "ADD COLUMN IF NOT EXISTS scope_tags_json JSON" in sql
    assert "ck_document_versions_authority_tier" in sql
    assert "authority_tier IS NULL OR (authority_tier >= 1 AND authority_tier <= 4)" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_document_versions_effective_authority" in sql

    assert "CREATE TABLE IF NOT EXISTS knowledge_conflict_resolutions" in sql
    assert (
        "winner_document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "loser_document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "winner_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "loser_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE"
        in sql
    )
    assert (
        "graph_edge_id VARCHAR(36) REFERENCES knowledge_graph_edges(id) ON DELETE SET NULL"
        in sql
    )

    # Seam A / B8: NO hard FK to search_units anywhere in migration 0015.
    assert "search_units" not in sql.lower()

    # Exact columns required by PROJECT.md
    for col in (
        "winner_security_partition_id VARCHAR(64) NOT NULL",
        "pair_key VARCHAR(64) NOT NULL",
        "winner_unit_id VARCHAR(36) NOT NULL",
        "winner_content_hash VARCHAR(64) NOT NULL",
        "loser_unit_id VARCHAR(36) NOT NULL",
        "loser_content_hash VARCHAR(64) NOT NULL",
        "cosine_similarity DOUBLE PRECISION",
        "effective_valid_to TIMESTAMP WITH TIME ZONE",
        "scope_difference_summary TEXT",
        "degraded_fallback BOOLEAN NOT NULL DEFAULT FALSE",
        "evidence_json JSON NOT NULL DEFAULT '{}'",
        "audit_history_json JSON NOT NULL DEFAULT '[]'",
    ):
        assert col in sql, f"missing column DDL in 0015: {col}"

    # Constraints & exact enum sets from PROJECT.md
    assert "CONSTRAINT uq_kcr_project_pair UNIQUE (project_id, pair_key)" in sql
    assert "CONSTRAINT ck_kcr_distinct_units CHECK (winner_unit_id <> loser_unit_id)" in sql
    assert "CONSTRAINT ck_kcr_distinct_versions CHECK (winner_version_id <> loser_version_id)" in sql
    assert "CONSTRAINT ck_kcr_confidence_range CHECK (confidence >= 0.0 AND confidence <= 1.0)" in sql
    assert "CONSTRAINT ck_kcr_status CHECK" in sql
    assert "CONSTRAINT ck_kcr_resolution_mode CHECK" in sql
    assert "CONSTRAINT ck_kcr_conflict_subtype CHECK" in sql

    for status_val in (
        "'PENDING_REVIEW'",
        "'ACTIVE'",
        "'DISMISSED_SCOPED'",
        "'REVOKED_WINNER'",
        "'NEEDS_REVALIDATION'",
        "'SUPERSEDED_BY_UPDATE'",
    ):
        assert status_val in sql
    for mode_val in ("'AUTO_POLICY'", "'MANUAL_HITL'", "'UNRESOLVED'", "'KEEP_BOTH_SCOPED'"):
        assert mode_val in sql
    for subtype_val in (
        "'DIRECT_SUPERSEDES'",
        "'HARD_CONTRADICTION'",
        "'SCOPED_EXCEPTION'",
        "'DIFFERENT_PERSPECTIVE'",
        "'SUPPORTS_OR_EQUIVALENT'",
    ):
        assert subtype_val in sql

    # All 7 indexes on knowledge_conflict_resolutions
    for idx_stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_kcr_project_pair",
        "CREATE INDEX IF NOT EXISTS idx_kcr_project_status_partition",
        "CREATE INDEX IF NOT EXISTS idx_kcr_loser_unit_status",
        "CREATE INDEX IF NOT EXISTS idx_kcr_winner_version_status",
        "CREATE INDEX IF NOT EXISTS idx_kcr_loser_version_status",
        "CREATE INDEX IF NOT EXISTS idx_kcr_winner_document",
        "CREATE INDEX IF NOT EXISTS idx_kcr_loser_document",
    ):
        assert idx_stmt in sql, f"missing index DDL in 0015: {idx_stmt}"


# ---------------------------------------------------------------------------
# 2. ORM PostgreSQL dialect DDL parity & model exports
# ---------------------------------------------------------------------------


def test_orm_pg_ddl_parity_and_exports():
    from sag_api.db import models as models_pkg
    from sag_api.db.models.conflict_resolution import (
        KnowledgeConflictResolution,
        compute_conflict_pair_key,
    )

    assert hasattr(models_pkg, "KnowledgeConflictResolution")
    assert models_pkg.KnowledgeConflictResolution is KnowledgeConflictResolution
    assert "knowledge_conflict_resolutions" in Base.metadata.tables

    # Document must stay untouched (OCTX B10 + DocumentOut B9)
    doc_cols = set(Document.__table__.columns.keys())
    assert "authority_tier" not in doc_cols
    assert "effective_from" not in doc_cols
    assert "scope_tags_json" not in doc_cols

    # DocumentVersion PG DDL
    dv_ddl = str(CreateTable(DocumentVersion.__table__).compile(dialect=postgresql.dialect()))
    assert "authority_tier INTEGER" in dv_ddl
    assert "effective_from TIMESTAMP WITH TIME ZONE" in dv_ddl
    assert "scope_tags_json JSON" in dv_ddl
    assert "ck_document_versions_authority_tier" in dv_ddl
    dv_index_names = {idx.name for idx in DocumentVersion.__table__.indexes}
    assert "idx_document_versions_effective_authority" in dv_index_names

    # KnowledgeConflictResolution PG DDL
    kcr_ddl = str(
        CreateTable(KnowledgeConflictResolution.__table__).compile(dialect=postgresql.dialect())
    )
    for snippet in (
        "winner_security_partition_id VARCHAR(64) NOT NULL",
        "pair_key VARCHAR(64) NOT NULL",
        "winner_document_id VARCHAR(36) NOT NULL",
        "loser_document_id VARCHAR(36) NOT NULL",
        "winner_unit_id VARCHAR(36) NOT NULL",
        "loser_unit_id VARCHAR(36) NOT NULL",
        "cosine_similarity",
        "scope_difference_summary TEXT",
        "degraded_fallback BOOLEAN",
        "graph_edge_id VARCHAR(36)",
        "evidence_json JSON",
        "audit_history_json JSON",
        "uq_kcr_project_pair",
        "ck_kcr_distinct_units",
        "ck_kcr_distinct_versions",
        "ck_kcr_confidence_range",
        "ck_kcr_status",
        "ck_kcr_resolution_mode",
        "ck_kcr_conflict_subtype",
    ):
        assert snippet in kcr_ddl, f"missing in ORM PG DDL: {snippet}"

    # Verify FKs: CASCADE to documents & document_versions, SET NULL to knowledge_graph_edges, NO FK to search_units
    fk_targets = {
        next(iter(fk.column_keys)): (fk.referred_table.name, fk.ondelete)
        for fk in KnowledgeConflictResolution.__table__.foreign_key_constraints
    }
    assert fk_targets["winner_document_id"] == ("documents", "CASCADE")
    assert fk_targets["loser_document_id"] == ("documents", "CASCADE")
    assert fk_targets["winner_version_id"] == ("document_versions", "CASCADE")
    assert fk_targets["loser_version_id"] == ("document_versions", "CASCADE")
    assert fk_targets["graph_edge_id"] == ("knowledge_graph_edges", "SET NULL")
    assert "winner_unit_id" not in fk_targets
    assert "loser_unit_id" not in fk_targets
    assert all(tbl != "search_units" for tbl, _ in fk_targets.values())

    # compute_conflict_pair_key symmetry & sha256 format
    k1 = compute_conflict_pair_key("v1", "u1", "v2", "u2")
    k2 = compute_conflict_pair_key("v2", "u2", "v1", "u1")
    expected = hashlib.sha256(b"v1:u1|v2:u2").hexdigest()
    assert k1 == k2 == expected
    assert len(k1) == 64


# ---------------------------------------------------------------------------
# 3. core/db.py upgrades & _ensure_indexes parser invariant
# ---------------------------------------------------------------------------


def test_core_db_upgrades_and_ensure_indexes_parser_invariant():
    assert "document_versions" in _COLUMN_UPGRADES
    dv_cols = _COLUMN_UPGRADES["document_versions"]
    assert dv_cols["authority_tier"] == "INTEGER"
    assert dv_cols["effective_from"] == "TIMESTAMP WITH TIME ZONE"
    assert dv_cols["scope_tags_json"] == "JSON"

    expected_indexes = {
        "idx_document_versions_effective_authority": "document_versions",
        "uq_kcr_project_pair": "knowledge_conflict_resolutions",
        "idx_kcr_project_status_partition": "knowledge_conflict_resolutions",
        "idx_kcr_loser_unit_status": "knowledge_conflict_resolutions",
        "idx_kcr_winner_version_status": "knowledge_conflict_resolutions",
        "idx_kcr_loser_version_status": "knowledge_conflict_resolutions",
        "idx_kcr_winner_document": "knowledge_conflict_resolutions",
        "idx_kcr_loser_document": "knowledge_conflict_resolutions",
    }

    parsed_indexes: dict[str, str] = {}
    for ddl in _INDEX_UPGRADES:
        # Mirror core/db.py:_ensure_indexes exact parsing logic
        assert " ON " in ddl, f"missing ' ON ' in index DDL: {ddl}"
        assert "IF NOT EXISTS " in ddl, f"missing 'IF NOT EXISTS ' in index DDL: {ddl}"
        table = ddl.split(" ON ", 1)[1].split(" ", 1)[0]
        idx_name = ddl.split("IF NOT EXISTS ", 1)[1].split(" ON ", 1)[0].strip()
        assert "(" not in table and ")" not in table, (
            f"single-space ' ON <table> (' invariant violated in: {ddl}"
        )
        assert f" ON {table} (" in ddl, f"expected single space before '(' in: {ddl}"
        parsed_indexes[idx_name] = table

    for idx_name, expected_table in expected_indexes.items():
        assert parsed_indexes.get(idx_name) == expected_table, (
            f"index {idx_name} missing or wrong table in _INDEX_UPGRADES"
        )


# ---------------------------------------------------------------------------
# 4. Legacy SQLite upgrade via init_db() + Seam A / B8 cascade behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_db_legacy_sqlite_upgrade_and_search_unit_fk_independence(
    tmp_path, monkeypatch
):
    from sag_api.db.models.conflict_resolution import (
        KnowledgeConflictResolution,
        compute_conflict_pair_key,
    )

    db_path = tmp_path / "legacy_0015.db"
    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_fk(legacy_engine)

    # Create pre-0015 schema: omit knowledge_conflict_resolutions and create legacy document_versions
    async with legacy_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    t
                    for name, t in Base.metadata.tables.items()
                    if name not in {"document_versions", "knowledge_conflict_resolutions"}
                ],
            )
        )
        await conn.exec_driver_sql(
            """
            CREATE TABLE document_versions (
                id VARCHAR(36) PRIMARY KEY,
                document_id VARCHAR(36) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                version_no INTEGER NOT NULL,
                file_hash VARCHAR(64) NOT NULL,
                supersedes_id VARCHAR(36) REFERENCES document_versions(id) ON DELETE SET NULL,
                source_published_at DATETIME,
                observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ingested_at DATETIME,
                valid_from DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                valid_to DATETIME NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'RECEIVED',
                search_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                knowledge_status VARCHAR(32) NOT NULL DEFAULT 'NOT_STARTED',
                search_ready_at DATETIME,
                knowledge_ready_at DATETIME,
                metadata_json JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    monkeypatch.setattr(db_module, "engine", legacy_engine)
    await db_module.init_db()

    # Verify upgraded columns & tables & indexes
    async with legacy_engine.begin() as conn:
        dv_cols = await conn.run_sync(
            lambda sc: {c["name"] for c in inspect(sc).get_columns("document_versions")}
        )
        assert {"authority_tier", "effective_from", "scope_tags_json"}.issubset(dv_cols)
        has_kcr = await conn.run_sync(
            lambda sc: inspect(sc).has_table("knowledge_conflict_resolutions")
        )
        assert has_kcr is True
        dv_idx = await conn.run_sync(
            lambda sc: {i["name"] for i in inspect(sc).get_indexes("document_versions")}
        )
        assert "idx_document_versions_effective_authority" in dv_idx
        kcr_idx = await conn.run_sync(
            lambda sc: {
                i["name"] for i in inspect(sc).get_indexes("knowledge_conflict_resolutions")
            }
        )
        assert {
            "uq_kcr_project_pair",
            "idx_kcr_project_status_partition",
            "idx_kcr_loser_unit_status",
            "idx_kcr_winner_version_status",
            "idx_kcr_loser_version_status",
            "idx_kcr_winner_document",
            "idx_kcr_loser_document",
        }.issubset(kcr_idx)

    # Seed two documents, versions, blocks, search_units, a graph edge, and a conflict resolution
    sessions = async_sessionmaker(legacy_engine, expire_on_commit=False)
    now_dt = datetime.now(UTC)
    async with sessions() as session:
        session.add(Source(id="src-1", name="S1", sag_source_config_id="cfg-1"))
        await session.flush()
        # OCTX-style Document & DocumentVersion inserts without new fields (B10)
        session.add_all(
            [
                Document(id="doc-w", source_id="src-1", filename="w.md", storage_path="/tmp/w.md"),
                Document(id="doc-l", source_id="src-1", filename="l.md", storage_path="/tmp/l.md"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DocumentVersion(
                    id="ver-w",
                    document_id="doc-w",
                    version_no=1,
                    file_hash="a" * 64,
                    valid_from=now_dt,
                ),
                DocumentVersion(
                    id="ver-l",
                    document_id="doc-l",
                    version_no=1,
                    file_hash="b" * 64,
                    valid_from=now_dt,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CanonicalBlock(
                    id="blk-w",
                    document_version_id="ver-w",
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                    normalized_text="New policy text",
                    content_hash="1" * 64,
                ),
                CanonicalBlock(
                    id="blk-l",
                    document_version_id="ver-l",
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                    normalized_text="Old policy text",
                    content_hash="2" * 64,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                SearchUnit(
                    id="unit-w",
                    document_version_id="ver-w",
                    block_from_id="blk-w",
                    block_to_id="blk-w",
                    security_partition_id="part-1",
                    content_hash="1" * 64,
                    token_count=10,
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                ),
                SearchUnit(
                    id="unit-l",
                    document_version_id="ver-l",
                    block_from_id="blk-l",
                    block_to_id="blk-l",
                    security_partition_id="part-1",
                    content_hash="2" * 64,
                    token_count=10,
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                ),
            ]
        )
        await session.flush()
        session.add(
            KnowledgeGraphEdge(
                id="edge-1",
                project_id="proj-1",
                source_unit_id="unit-w",
                target_unit_id="unit-l",
                edge_type="SUPERSEDES",
            )
        )
        await session.flush()
        kcr = KnowledgeConflictResolution(
            id="kcr-1",
            tenant_id="t1",
            project_id="proj-1",
            security_partition_id="part-1",
            winner_security_partition_id="part-1",
            pair_key=compute_conflict_pair_key("ver-w", "unit-w", "ver-l", "unit-l"),
            winner_document_id="doc-w",
            winner_version_id="ver-w",
            winner_unit_id="unit-w",
            winner_content_hash="1" * 64,
            loser_document_id="doc-l",
            loser_version_id="ver-l",
            loser_unit_id="unit-l",
            loser_content_hash="2" * 64,
            conflict_subtype="DIRECT_SUPERSEDES",
            status="ACTIVE",
            resolution_mode="AUTO_POLICY",
            confidence=0.92,
            cosine_similarity=0.88,
            graph_edge_id="edge-1",
        )
        session.add(kcr)
        await session.commit()

    # Verify OCTX defaults on DocumentVersion
    async with sessions() as session:
        ver_w = await session.get(DocumentVersion, "ver-w")
        assert ver_w is not None
        assert ver_w.authority_tier is None
        assert ver_w.effective_from is None
        assert ver_w.scope_tags_json is None

    # DELETE FROM search_units under PRAGMA foreign_keys=ON MUST succeed and NOT delete KCR!
    # (Also triggers KnowledgeGraphEdge CASCADE delete -> sets kcr.graph_edge_id = NULL via ON DELETE SET NULL!)
    async with sessions() as session:
        fk_on = (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
        assert fk_on == 1
        await session.execute(delete(SearchUnit))
        await session.commit()

    async with sessions() as session:
        remaining = await session.get(KnowledgeConflictResolution, "kcr-1")
        assert remaining is not None, "KCR row must survive DELETE FROM search_units"
        assert remaining.graph_edge_id is None, "graph_edge_id must ON DELETE SET NULL"
        assert remaining.degraded_fallback is False
        assert remaining.audit_history_json == []
        assert remaining.evidence_json == {}

    # DELETE FROM documents MUST cascade and delete KCR!
    async with sessions() as session:
        await session.execute(delete(Document).where(Document.id == "doc-w"))
        await session.commit()

    async with sessions() as session:
        gone = await session.get(KnowledgeConflictResolution, "kcr-1")
        assert gone is None, "DELETE FROM documents must cascade delete KCR"

    await legacy_engine.dispose()


# ---------------------------------------------------------------------------
# 5. CHECK & UNIQUE constraints enforcement on fresh SQLite DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_and_unique_constraints_enforced(tmp_path):
    from sag_api.db.models.conflict_resolution import (
        KnowledgeConflictResolution,
        compute_conflict_pair_key,
    )

    db_path = tmp_path / "constraints_0015.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_fk(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessions = async_sessionmaker(eng, expire_on_commit=False)
    now_dt = datetime.now(UTC)

    async with sessions() as session:
        session.add(Source(id="s1", name="S1", sag_source_config_id="c1"))
        await session.flush()
        session.add_all(
            [
                Document(id="d1", source_id="s1", filename="1.md", storage_path="/tmp/1.md"),
                Document(id="d2", source_id="s1", filename="2.md", storage_path="/tmp/2.md"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DocumentVersion(
                    id="v1",
                    document_id="d1",
                    version_no=1,
                    file_hash="a" * 64,
                    valid_from=now_dt,
                    authority_tier=1,
                    effective_from=now_dt,
                    scope_tags_json=["phòng-kế-toán", "vn"],
                ),
                DocumentVersion(
                    id="v2",
                    document_id="d2",
                    version_no=1,
                    file_hash="b" * 64,
                    valid_from=now_dt,
                    authority_tier=4,
                ),
            ]
        )
        await session.commit()

    # Verify authority_tier / effective_from (UTC-aware) / scope_tags_json round-trip
    async with sessions() as session:
        loaded_v1 = await session.get(DocumentVersion, "v1")
        assert loaded_v1 is not None
        assert loaded_v1.authority_tier == 1
        assert loaded_v1.effective_from is not None
        assert loaded_v1.effective_from.tzinfo == UTC
        assert loaded_v1.scope_tags_json == ["phòng-kế-toán", "vn"]

    # Invalid authority_tier=0 and 5 rejected
    for bad_tier in (0, 5):
        async with sessions() as session:
            session.add(
                DocumentVersion(
                    id=f"v-bad-{bad_tier}",
                    document_id="d1",
                    version_no=10 + bad_tier,
                    file_hash="c" * 64,
                    valid_from=now_dt,
                    authority_tier=bad_tier,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    def _base_kcr(**overrides) -> KnowledgeConflictResolution:
        defaults = dict(
            id="kcr-base",
            tenant_id="t1",
            project_id="p1",
            security_partition_id="sp1",
            winner_security_partition_id="sp1",
            pair_key=compute_conflict_pair_key("v1", "u1", "v2", "u2"),
            winner_document_id="d1",
            winner_version_id="v1",
            winner_unit_id="u1",
            winner_content_hash="1" * 64,
            loser_document_id="d2",
            loser_version_id="v2",
            loser_unit_id="u2",
            loser_content_hash="2" * 64,
            conflict_subtype="HARD_CONTRADICTION",
            status="PENDING_REVIEW",
            resolution_mode="UNRESOLVED",
            confidence=0.8,
        )
        defaults.update(overrides)
        return KnowledgeConflictResolution(**defaults)

    # Bad CHECK cases
    invalid_cases = [
        {"id": "k-same-unit", "winner_unit_id": "u1", "loser_unit_id": "u1"},
        {"id": "k-same-ver", "winner_version_id": "v1", "loser_version_id": "v1"},
        {"id": "k-conf-high", "confidence": 1.5},
        {"id": "k-conf-neg", "confidence": -0.1},
        {"id": "k-bad-status", "status": "REJECTED"},
        {"id": "k-bad-mode", "resolution_mode": "HUMAN_APPROVED"},
        {"id": "k-bad-subtype", "conflict_subtype": "SUPERSEDES_CROSS_DOC"},
    ]
    for case in invalid_cases:
        async with sessions() as session:
            session.add(_base_kcr(**case))
            with pytest.raises(IntegrityError):
                await session.commit()

    # Valid insert + duplicate (project_id, pair_key) rejected
    async with sessions() as session:
        session.add(_base_kcr(id="k-ok-1"))
        await session.commit()

    async with sessions() as session:
        session.add(_base_kcr(id="k-dup-pair"))
        with pytest.raises(IntegrityError):
            await session.commit()

    await eng.dispose()


# ---------------------------------------------------------------------------
# 6. Enums & pure helpers
# ---------------------------------------------------------------------------


def test_enums_and_helpers():
    from sag_api.enums import (
        AUTHORITY_TIER_LABELS,
        AuthorityTier,
        ConflictResolutionMode,
        ConflictResolutionStatus,
        ConflictSubtype,
        authority_tier_label,
        normalize_authority_tier,
        normalize_scope_tags,
    )

    assert JobType.CONFLICT_RESOLUTION == "conflict_resolution"

    assert int(AuthorityTier.POLICY_BINDING) == 1
    assert int(AuthorityTier.OFFICIAL_SPEC) == 2
    assert int(AuthorityTier.OPERATIONAL_GUIDE) == 3
    assert int(AuthorityTier.INFORMAL_DRAFT) == 4
    assert AUTHORITY_TIER_LABELS == {
        1: "Chính thức",
        2: "Quy chuẩn",
        3: "Hướng dẫn",
        4: "Nháp",
    }

    # normalize_authority_tier accepted forms
    assert normalize_authority_tier(None) is None
    assert normalize_authority_tier("") is None
    assert normalize_authority_tier("   ") is None
    assert normalize_authority_tier(AuthorityTier.POLICY_BINDING) == 1
    assert normalize_authority_tier(2) == 2
    assert normalize_authority_tier("T1") == 1
    assert normalize_authority_tier("policy_binding") == 1
    assert normalize_authority_tier("Chính Thức") == 1
    assert normalize_authority_tier(unicodedata.normalize("NFD", "Chính thức")) == 1
    assert normalize_authority_tier("chinh_thuc") == 1
    assert normalize_authority_tier("ADR") == 2
    assert normalize_authority_tier("quy chuẩn") == 2
    assert normalize_authority_tier("Runbook") == 3
    assert normalize_authority_tier("hướng dẫn") == 3
    assert normalize_authority_tier("memo") == 4
    assert normalize_authority_tier("Nháp") == 4

    # normalize_authority_tier rejected forms
    for bad in (True, False, 0, 5, -1, "tier_5", "unknown", 2.5):
        with pytest.raises(ValueError):
            normalize_authority_tier(bad)  # type: ignore[arg-type]

    # authority_tier_label never emits "[" or "]"
    assert authority_tier_label(None) is None
    assert authority_tier_label(True) is None  # type: ignore[arg-type]
    for t in (1, 2, 3, 4):
        full = authority_tier_label(t)
        compact = authority_tier_label(t, compact=True)
        assert full == AUTHORITY_TIER_LABELS[t]
        assert compact == f"T{t}"
        assert "[" not in full and "]" not in full
        assert "[" not in compact and "]" not in compact

    # normalize_scope_tags
    assert normalize_scope_tags(None) == []
    assert normalize_scope_tags([]) == []
    assert normalize_scope_tags(()) == []
    assert normalize_scope_tags(set()) == []

    nfd_vn = unicodedata.normalize("NFD", "Phòng Kế Toán")
    nfc_vn = unicodedata.normalize("NFC", "phòng-kế-toán")
    tags = normalize_scope_tags([nfd_vn, "  VN/HCM  ", "vn/hcm", "dept:hr.v2"])
    assert tags == sorted(["dept:hr.v2", nfc_vn, "vn/hcm"])

    # Exactly 16 tags allowed; 17 rejected
    assert len(normalize_scope_tags([f"tag-{i:02d}" for i in range(16)])) == 16
    with pytest.raises(ValueError):
        normalize_scope_tags([f"tag-{i:02d}" for i in range(17)])

    # Invalid tokens & types rejected
    for bad_tags in (
        "bare-string",
        123,
        [123],
        [""],
        ["   "],
        ["bad!token"],
        ["bracket[1]"],
        ["x" * 65],
    ):
        with pytest.raises(ValueError):
            normalize_scope_tags(bad_tags)

    # Conflict enums exact values
    assert [e.value for e in ConflictSubtype] == [
        "DIRECT_SUPERSEDES",
        "HARD_CONTRADICTION",
        "SCOPED_EXCEPTION",
        "DIFFERENT_PERSPECTIVE",
        "SUPPORTS_OR_EQUIVALENT",
    ]
    assert [e.value for e in ConflictResolutionStatus] == [
        "PENDING_REVIEW",
        "ACTIVE",
        "DISMISSED_SCOPED",
        "REVOKED_WINNER",
        "NEEDS_REVALIDATION",
        "SUPERSEDED_BY_UPDATE",
    ]
    assert [e.value for e in ConflictResolutionMode] == [
        "AUTO_POLICY",
        "MANUAL_HITL",
        "UNRESOLVED",
        "KEEP_BOTH_SCOPED",
    ]


# ---------------------------------------------------------------------------
# 7. StageRun.created_at microsecond precision & explicit override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_run_created_at_microsecond_default_and_explicit_override(tmp_path):
    db_path = tmp_path / "stage_run_clock.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _enable_sqlite_fk(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessions = async_sessionmaker(eng, expire_on_commit=False)
    now_dt = datetime.now(UTC)
    old_ts = datetime(2024, 1, 2, 3, 4, 5, 654321, tzinfo=UTC)

    async with sessions() as session:
        session.add(Source(id="s1", name="S1", sag_source_config_id="c1"))
        await session.flush()
        session.add(Document(id="d1", source_id="s1", filename="1.md", storage_path="/tmp/1.md"))
        await session.flush()
        session.add(
            DocumentVersion(
                id="v1", document_id="d1", version_no=1, file_hash="a" * 64, valid_from=now_dt
            )
        )
        await session.flush()
        run_started = datetime.now(UTC)
        run = IngestionRun(
            id="run-1",
            tenant_id="t1",
            project_id="p1",
            document_version_id="v1",
            idempotency_key="idem-1",
            payload_hash="p" * 64,
            started_at=run_started,
        )
        session.add(run)
        await session.flush()

        # Default created_at must be >= run.started_at within the same wall-clock second
        stage_auto = StageRun(
            id="st-auto",
            run_id="run-1",
            stage="INDEX_SEARCH",
            status="SUCCEEDED",
        )
        # Explicit created_at override must be preserved verbatim
        stage_explicit = StageRun(
            id="st-explicit",
            run_id="run-1",
            stage="PARSE",
            status="SUCCEEDED",
            created_at=old_ts,
        )
        session.add_all([stage_auto, stage_explicit])
        await session.commit()

    async with sessions() as session:
        loaded_run = await session.get(IngestionRun, "run-1")
        loaded_auto = await session.get(StageRun, "st-auto")
        loaded_explicit = await session.get(StageRun, "st-explicit")
        assert loaded_run is not None and loaded_run.started_at is not None
        assert loaded_auto is not None
        assert loaded_explicit is not None
        assert loaded_auto.created_at >= loaded_run.started_at
        assert loaded_auto.created_at - loaded_run.started_at < timedelta(seconds=5)
        assert loaded_explicit.created_at == old_ts

    await eng.dispose()


# ---------------------------------------------------------------------------
# 8. Config flags & bounds validators
# ---------------------------------------------------------------------------


def test_config_conflict_and_authority_flags():
    cfg = Settings(_env_file=None)
    assert cfg.temporal_authority_rerank_enabled is True
    assert cfg.authority_rerank_max_delta == pytest.approx(0.04)
    assert cfg.conflict_resolution_job_enabled is True
    assert cfg.conflict_vector_top_k == 5
    assert cfg.conflict_vector_min_cosine == pytest.approx(0.72)
    assert cfg.conflict_vector_max_cosine == pytest.approx(0.995)
    assert cfg.conflict_auto_supersede_min_confidence == pytest.approx(0.85)
    assert cfg.conflict_max_pairs_per_version == 50
    assert cfg.conflict_classifier_batch_size == 8
    assert cfg.conflict_classifier_timeout_seconds == pytest.approx(20.0)

    # authority_rerank_max_delta > 0.04 rejected
    with pytest.raises(ValidationError):
        Settings(_env_file=None, authority_rerank_max_delta=0.05)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, authority_rerank_max_delta=-0.01)

    # min_cosine >= max_cosine rejected
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            conflict_vector_min_cosine=0.85,
            conflict_vector_max_cosine=0.85,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            conflict_vector_min_cosine=0.90,
            conflict_vector_max_cosine=0.80,
        )

    # Valid custom bounds accepted
    custom = Settings(
        _env_file=None,
        authority_rerank_max_delta=0.02,
        conflict_vector_min_cosine=0.65,
        conflict_vector_max_cosine=0.95,
    )
    assert custom.authority_rerank_max_delta == pytest.approx(0.02)
    assert custom.conflict_vector_min_cosine == pytest.approx(0.65)
    assert custom.conflict_vector_max_cosine == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# 9. Temporal metadata formatter
# ---------------------------------------------------------------------------


def test_temporal_metadata_formatter():
    from sag_api.services.temporal_metadata_format import format_evidence_metadata_suffix

    # Legacy unset inputs -> ""
    assert format_evidence_metadata_suffix(None) == ""
    assert format_evidence_metadata_suffix({}) == ""
    assert format_evidence_metadata_suffix(RetrievedSection(content="hello")) == ""
    # Non-superseded with valid_to only still returns ""
    assert (
        format_evidence_metadata_suffix(
            RetrievedSection(
                content="hello",
                valid_to=datetime(2025, 12, 31, tzinfo=UTC),
                is_superseded=False,
            )
        )
        == ""
    )

    # Tier 1 + effective_from
    sec_t1 = RetrievedSection(
        content="Policy body",
        authority_tier=1,
        effective_from=datetime(2025, 6, 15, 0, 0, tzinfo=UTC),
    )
    out_t1 = format_evidence_metadata_suffix(sec_t1)
    assert out_t1 == "(Chính thức · Hiệu lực 2025-06-15)"
    assert len(out_t1) <= 50
    assert "[" not in out_t1 and "]" not in out_t1

    # Dict parity (including ISO string date)
    out_dict = format_evidence_metadata_suffix(
        {"authority_tier": 1, "effective_from": "2025-06-15T00:00:00Z"}
    )
    assert out_dict == out_t1

    # Superseded + valid_to fallback (< 9999)
    out_sup_vt = format_evidence_metadata_suffix(
        RetrievedSection(
            is_superseded=True,
            valid_to=datetime(2025, 6, 15, tzinfo=UTC),
        )
    )
    assert out_sup_vt == "(Bản cũ · Hết HL 2025-06-15)"
    assert len(out_sup_vt) <= 50
    assert "[" not in out_sup_vt and "]" not in out_sup_vt

    # Superseded with 9999 sentinel omits Hết HL
    out_sup_sentinel = format_evidence_metadata_suffix(
        {"is_superseded": True, "valid_to": datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)}
    )
    assert out_sup_sentinel == "(Bản cũ)"

    # SUPERSEDED_BY_UNIT conflict_status produces Bản cũ even when is_superseded=False
    out_sup_unit = format_evidence_metadata_suffix(
        {
            "conflict_status": "SUPERSEDED_BY_UNIT",
            "authority_tier": 3,
            "effective_from": datetime(2024, 1, 1, tzinfo=UTC),
        }
    )
    assert out_sup_unit.startswith("(Bản cũ · Hướng dẫn · ")
    assert "2024-01-01" in out_sup_unit
    assert len(out_sup_unit) <= 50
    assert "[" not in out_sup_unit and "]" not in out_sup_unit

    # ACTIVE_CONFLICT produces Xung đột
    out_conflict = format_evidence_metadata_suffix(
        RetrievedSection(
            conflict_status="ACTIVE_CONFLICT",
            authority_tier=2,
            effective_from=datetime(2025, 6, 15, tzinfo=UTC),
        )
    )
    assert out_conflict.startswith("(Xung đột · Quy chuẩn · ")
    assert "2025-06-15" in out_conflict
    assert len(out_conflict) <= 50
    assert "[" not in out_conflict and "]" not in out_conflict

    # Citation regex collision guard across all tier/status combos
    citation_re = re.compile(r"\[\d+\]")
    for tier in (None, 1, 2, 3, 4):
        for status in (None, "ACTIVE_CONFLICT", "SUPERSEDED_BY_UNIT"):
            for sup in (False, True):
                rendered = format_evidence_metadata_suffix(
                    {
                        "authority_tier": tier,
                        "effective_from": datetime(2025, 6, 15, tzinfo=UTC),
                        "is_superseded": sup,
                        "conflict_status": status,
                    }
                )
                assert len(rendered) <= 50
                assert "[" not in rendered and "]" not in rendered
                assert not citation_re.search(rendered)


# ---------------------------------------------------------------------------
# 10. RetrievedSection DTO & SectionOut BFF compatibility
# ---------------------------------------------------------------------------


def test_retrieved_section_additive_fields_and_section_out_compat():
    # Default legacy section
    legacy = RetrievedSection(chunk_id="c1", heading="H", content="Exact body", score=0.9, rank=1)
    assert legacy.authority_tier is None
    assert legacy.effective_from is None
    assert legacy.scope_tags == ()
    assert legacy.conflict_status is None

    # from_section preserves additive fields and leaves content byte-immutable
    eff = datetime(2025, 6, 15, tzinfo=UTC)
    raw = {
        "chunk_id": "c2",
        "heading": "Leave Policy",
        "content": "Annual leave is 15 days.",
        "score": 0.91,
        "rank": 1,
        "authority_tier": 1,
        "effective_from": eff,
        "scope_tags": ["vn", "hr"],
        "conflict_status": "ACTIVE_CONFLICT",
    }
    sec = RetrievedSection.from_section(raw)
    assert sec.content == "Annual leave is 15 days."
    assert sec.authority_tier == 1
    assert sec.effective_from == eff
    assert sec.scope_tags == ("vn", "hr")
    assert sec.conflict_status == "ACTIVE_CONFLICT"

    # Re-passing RetrievedSection through from_section preserves everything
    sec_roundtrip = RetrievedSection.from_section(sec)
    assert sec_roundtrip.authority_tier == 1
    assert sec_roundtrip.effective_from == eff
    assert sec_roundtrip.scope_tags == ("vn", "hr")
    assert sec_roundtrip.conflict_status == "ACTIVE_CONFLICT"

    # SectionOut(**{**sec.model_dump(), "source_id": "s1"}) validates cleanly without BFF drift
    out = SectionOut(**{**sec.model_dump(), "source_id": "s1"})
    assert out.chunk_id == "c2"
    assert out.content == "Annual leave is 15 days."
    assert out.source_id == "s1"
    assert not hasattr(out, "authority_tier")
    assert not hasattr(out, "conflict_status")
