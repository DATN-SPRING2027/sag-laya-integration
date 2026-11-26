"""TDD regression tests for Milestone P5 Commit 5: narrowing broad except Exception swallows."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.errors import UpstreamError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Source
from sag_api.db.models.document import Document
from sag_api.enums import ConnectorKind, DocumentStatus, SourceStatus, SourceType


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-1",
        organization_id="org-1",
        allowed_project_ids=frozenset({"proj-1"}),
        issuer="continuum",
        key_id="kid-1",
        token_id="tok-1",
        issued_at=1700000000,
        expires_at=1700000120,
        tenant_id="tenant-1",
        allowed_partition_ids=frozenset({"public"}),
    )


# ---------------------------------------------------------------------------
# 1. knowledge_service._load_sag_engine_entities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_sag_engine_entities_returns_empty_only_on_missing_table(monkeypatch):
    from sag_api.services import knowledge_service

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        src = Source(
            id="src-ks-1",
            name="KS Source",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="cfg-ks-1",
            status=SourceStatus.ACTIVE,
        )
        doc = Document(
            id="doc-ks-1",
            source_id="src-ks-1",
            filename="ks.pdf",
            content_type="application/pdf",
            size_bytes=10,
            storage_path="/tmp/ks.pdf",
            status=DocumentStatus.READY,
            tenant_id="tenant-1",
            project_id="proj-1",
            logical_source_id="ls-ks-1",
            sag_source_id="sag-src-1",
            is_active=True,
        )
        session.add_all([src, doc])
        await session.commit()

        try:
            # Case A: SQLite "no such table" -> returns ()
            entities = await knowledge_service._load_sag_engine_entities(session, doc.id)
            assert entities == ()

            # Case B: Postgres UndefinedTable 42P01 -> returns ()
            pg_orig = SimpleNamespace(pgcode="42P01")
            pg_err = OperationalError("SELECT", {}, Exception('relation "entity" does not exist'))
            pg_err.orig = pg_orig
            monkeypatch.setattr(session, "scalars", AsyncMock(side_effect=pg_err))
            assert await knowledge_service._load_sag_engine_entities(session, doc.id) == ()

            # Case C: Non-missing-table OperationalError (e.g. connection refused) MUST propagate
            conn_err = OperationalError("SELECT", {}, Exception("connection refused"))
            monkeypatch.setattr(session, "scalars", AsyncMock(side_effect=conn_err))
            with pytest.raises(SQLAlchemyError, match="connection refused"):
                await knowledge_service._load_sag_engine_entities(session, doc.id)

            # Case D: Programming error (RuntimeError / TypeError) MUST propagate
            monkeypatch.setattr(
                session, "scalars", AsyncMock(side_effect=RuntimeError("bug in query"))
            )
            with pytest.raises(RuntimeError, match="bug in query"):
                await knowledge_service._load_sag_engine_entities(session, doc.id)
        finally:
            await session.close()

    await engine.dispose()


# ---------------------------------------------------------------------------
# 2. mcp/server.py (_legacy_entity, _legacy_grep, get_chunk)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_legacy_tools_narrow_exceptions_and_propagate_programming_errors(
    monkeypatch, caplog
):
    from sag_api.mcp import server as mcp_server

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    monkeypatch.setattr("sag_api.core.db.SessionLocal", session_factory)

    async with session_factory() as session:
        src = Source(
            id="src-mcp-legacy",
            name="Legacy Source",
            source_type=SourceType.DOCUMENT,
            connector_kind=ConnectorKind.FILE_UPLOAD,
            sag_source_config_id="cfg-mcp-legacy",
            status=SourceStatus.ACTIVE,
        )
        session.add(src)
        await session.commit()

        mcp = mcp_server.build_source_mcp()
        principal = _principal()

        # Operational/DB/HTTP error degrades cleanly with warning log
        failing_mgr = SimpleNamespace(
            list_entities=AsyncMock(side_effect=httpx.ConnectError("qdrant down")),
            grep_chunks=AsyncMock(side_effect=TimeoutError("grep timed out")),
            get_chunk=AsyncMock(side_effect=UpstreamError("upstream failed")),
        )
        with caplog.at_level(logging.WARNING):
            with mcp_server.use_scope(failing_mgr, [src], principal):
                ent_res = await mcp._tool_manager.call_tool("get_entity", {"name": "Alice"})
                grep_res = await mcp._tool_manager.call_tool("grep", {"pattern": "needle"})
                chunk_res = await mcp._tool_manager.call_tool("get_chunk", {"chunk_id": "c-1"})
        assert "Không tìm thấy" in "".join(getattr(p, "text", str(p)) for p in ent_res)
        assert "Không tìm thấy" in "".join(getattr(p, "text", str(p)) for p in grep_res)
        assert "Không tìm thấy" in "".join(getattr(p, "text", str(p)) for p in chunk_res)
        assert any("MCP legacy" in rec.message for rec in caplog.records)

        # Programming errors (TypeError / KeyError / AttributeError) MUST propagate
        buggy_mgr = SimpleNamespace(
            list_entities=AsyncMock(side_effect=TypeError("bad arg type")),
            grep_chunks=AsyncMock(side_effect=KeyError("missing_key")),
            get_chunk=AsyncMock(side_effect=AttributeError("no_such_attr")),
        )
        with mcp_server.use_scope(buggy_mgr, [src], principal):
            with pytest.raises(Exception) as exc_ent:
                await mcp._tool_manager.call_tool("get_entity", {"name": "Alice"})
            assert isinstance(exc_ent.value.__cause__ or exc_ent.value, TypeError)

            with pytest.raises(Exception) as exc_grep:
                await mcp._tool_manager.call_tool("grep", {"pattern": "needle"})
            assert isinstance(exc_grep.value.__cause__ or exc_grep.value, KeyError)

            with pytest.raises(Exception) as exc_chunk:
                await mcp._tool_manager.call_tool("get_chunk", {"chunk_id": "c-1"})
            assert isinstance(exc_chunk.value.__cause__ or exc_chunk.value, AttributeError)

    await engine.dispose()


# ---------------------------------------------------------------------------
# 3. search_index_service.py (dimension ValueError, transport warning, AttributeError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_index_ensure_collection_propagates_dim_mismatch_and_narrows_transport(
    caplog,
):
    from sag_api.services.search_index_service import ensure_qdrant_collection_and_indexes

    # A. Dimension mismatch ValueError MUST propagate immediately
    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/collections/col_dim_err":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 768}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, json={"result": {}})

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(mismatch_handler)
    ) as client:
        with pytest.raises(ValueError, match="dimension mismatch"):
            await ensure_qdrant_collection_and_indexes(client, "col_dim_err", vector_dim=1024)

    # B. Transport error on GET + IDF PATCH logs warning (not silent swallow)
    def patch_fail_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/collections/col_patch_warn":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 1024}},
                                "sparse_vectors": {"bm25_sparse": {}},
                            }
                        },
                        "payload_schema": {
                            "project_id": {},
                            "source_id": {},
                            "document_id": {},
                            "document_version_id": {},
                            "security_partition_id": {},
                            "tenant_id": {},
                            "is_active": {},
                            "payload_only": {},
                            "tree_node_ids": {},
                            "valid_from_ts": {},
                            "valid_to_ts": {},
                        },
                    }
                },
            )
        if request.method == "PATCH":
            raise httpx.ConnectError("patch connection dropped")
        return httpx.Response(200, json={"result": {}})

    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(patch_fail_handler)
        ) as client:
            await ensure_qdrant_collection_and_indexes(client, "col_patch_warn", vector_dim=1024)
    assert any("IDF modifier patch" in rec.message for rec in caplog.records)

    # C. Programming error inside GET handler (e.g. TypeError) MUST NOT be swallowed
    def type_error_handler(request: httpx.Request) -> httpx.Response:
        raise TypeError("unexpected programming error in mock")

    async with httpx.AsyncClient(
        base_url="http://qdrant.local", transport=httpx.MockTransport(type_error_handler)
    ) as client:
        with pytest.raises(TypeError, match="unexpected programming error"):
            await ensure_qdrant_collection_and_indexes(client, "col_type_err", vector_dim=1024)


# ---------------------------------------------------------------------------
# 4. retrieval_service._lexical_sections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lexical_sections_narrows_errors_and_propagates_programming_bugs(caplog):
    from sag_api.services.query_analysis import analyze_query
    from sag_api.services.retrieval_service import _lexical_sections

    src = SimpleNamespace(sag_source_config_id="cfg-lex", id="src-lex")
    analysis = analyze_query("policy")

    # Operational error -> logs warning and returns empty
    timeout_engine = SimpleNamespace(grep_chunks=AsyncMock(side_effect=TimeoutError("grep timeout")))
    with caplog.at_level(logging.WARNING):
        rows, exact_terms = await _lexical_sections(
            timeout_engine, [src], analysis=analysis, candidate_limit=10
        )
    assert rows == []
    assert exact_terms == set()
    assert any("grep_chunks failed" in rec.message for rec in caplog.records)

    # Programming error -> propagates
    buggy_engine = SimpleNamespace(grep_chunks=AsyncMock(side_effect=KeyError("bad_field")))
    with pytest.raises(KeyError, match="bad_field"):
        await _lexical_sections(buggy_engine, [src], analysis=analysis, candidate_limit=10)


# ---------------------------------------------------------------------------
# 5. query_routing_service.capture_routing_decisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_routing_decisions_narrows_provider_errors_and_propagates_bugs():
    from sag_api.services.query_routing_service import capture_routing_decisions

    group = SimpleNamespace(
        project_id="proj-1",
        source=SimpleNamespace(id="src-1"),
        versions=("ver-1",),
        tenant_id="tenant-1",
        partition_id="public",
        acl_mapping_version=1,
        search_index_versions=(),
    )

    # TimeoutError -> TREE_PROVIDER_ERROR fallback
    mgr_timeout = SimpleNamespace(get_routing_snapshot=AsyncMock(side_effect=TimeoutError("slow")))
    decisions = await capture_routing_decisions(
        mgr_timeout, [group], query="hello", planner_trace={}
    )
    assert len(decisions) == 1
    assert next(iter(decisions.values())).reason_code == "TREE_PROVIDER_ERROR"

    # Programming error (TypeError / KeyError) -> propagates
    mgr_bug = SimpleNamespace(get_routing_snapshot=AsyncMock(side_effect=TypeError("bad kwarg")))
    with pytest.raises(TypeError, match="bad kwarg"):
        await capture_routing_decisions(mgr_bug, [group], query="hello", planner_trace={})


# ---------------------------------------------------------------------------
# 6. tree_query_snapshot_service._resolve_scope_query_vector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_scope_query_vector_narrows_exceptions(caplog):
    from sag_api.services.tree_query_snapshot_service import _resolve_scope_query_vector

    async def timeout_coro():
        raise TimeoutError("embed timed out")

    with caplog.at_level(logging.WARNING):
        res = await _resolve_scope_query_vector(timeout_coro())
    assert res is None
    assert any("Scope query vector unavailable" in rec.message for rec in caplog.records)

    async def buggy_coro():
        raise AttributeError("broken vector object")

    with pytest.raises(AttributeError, match="broken vector object"):
        await _resolve_scope_query_vector(buggy_coro())


# ---------------------------------------------------------------------------
# 7. handover_assist_service._load_indexed_units
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handover_load_indexed_units_catches_only_sqlalchemy_error(caplog):
    from sag_api.schemas.handover_assist import HandoverAssistSourceInput
    from sag_api.services.handover_assist_service import _load_indexed_units

    principal = _principal()
    src_inputs = [
        HandoverAssistSourceInput(
            kind="PROJECT_DOCUMENT",
            id="a" * 24,
            version=1,
            title="Doc A",
            logicalSourceId="ls-1",
        )
    ]

    # SQLAlchemyError -> logs warning and returns {}
    db_err_session = SimpleNamespace(
        execute=AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("no such table")))
    )
    with caplog.at_level(logging.WARNING):
        out = await _load_indexed_units(
            db_err_session,
            principal=principal,
            project_id="proj-1",
            sources=src_inputs,
        )
    assert out == {}
    assert any("SearchUnit enrichment unavailable" in rec.message for rec in caplog.records)

    # Programming error (RuntimeError / KeyError) -> propagates
    bug_session = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("logic bug")))
    with pytest.raises(RuntimeError, match="logic bug"):
        await _load_indexed_units(
            bug_session,
            principal=principal,
            project_id="proj-1",
            sources=src_inputs,
        )


# ---------------------------------------------------------------------------
# 8. main.py warm_task shutdown helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_drain_warm_manifest_task_distinguishes_cancel_vs_exception(caplog):
    from sag_api.main import _drain_warm_manifest_task

    # Cancelled task -> silent
    async def never_finishes():
        await asyncio.sleep(10)

    t1 = asyncio.create_task(never_finishes())
    await asyncio.sleep(0)
    t1.cancel()
    with caplog.at_level(logging.DEBUG, logger="sag_api.app"):
        await _drain_warm_manifest_task(t1)
    assert not any("Warm manifest cache task ended" in rec.message for rec in caplog.records)

    # Exception in warm task -> logged at debug
    async def fails():
        raise RuntimeError("warm failed")

    t2 = asyncio.create_task(fails())
    await asyncio.sleep(0)
    with caplog.at_level(logging.DEBUG):
        await _drain_warm_manifest_task(t2)
    assert any("Warm manifest cache task ended" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 9. Secondary §4.4 swallows
# ---------------------------------------------------------------------------


def test_config_builder_extract_postgres_search_path_narrows_to_argument_error(monkeypatch):
    from sag_api.sag import config_builder

    assert config_builder.extract_postgres_search_path("://not-a-valid-url") is None

    monkeypatch.setattr(
        config_builder, "make_url", MagicMock(side_effect=TypeError("unexpected type bug"))
    )
    with pytest.raises(TypeError, match="unexpected type bug"):
        config_builder.extract_postgres_search_path("postgresql+asyncpg://localhost/db")


@pytest.mark.asyncio
async def test_search_unit_retrieval_hydrate_siblings_propagates_key_error(monkeypatch):
    from sag_api.sag import RetrievedSection
    from sag_api.services import search_unit_retrieval_service as surs

    versions = {"v-1": SimpleNamespace(partition_id="public")}
    sec = RetrievedSection(
        chunk_id="su-1",
        search_unit_id="su-1",
        heading="H1",
        content="C1",
        score=0.9,
        rank=0,
        document_version_id="v-1",
    )
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(surs, "SessionLocal", lambda: ctx)

    # SQLAlchemyError -> degrades cleanly to []
    monkeypatch.setattr(
        surs,
        "_get_or_build_scope_knowledge_index",
        AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("db down"))),
    )
    assert (
        await surs._hydrate_sibling_sections_for_top_versions(
            sections=[sec], current_versions=versions, sources=[], limit=2
        )
        == []
    )

    # KeyError -> propagates
    monkeypatch.setattr(
        surs, "_get_or_build_scope_knowledge_index", AsyncMock(side_effect=KeyError("bad_key"))
    )
    with pytest.raises(KeyError, match="bad_key"):
        await surs._hydrate_sibling_sections_for_top_versions(
            sections=[sec], current_versions=versions, sources=[], limit=2
        )


def test_search_index_service_has_no_b010_setattr_and_uses_direct_assignment():
    import pathlib

    src = pathlib.Path("sag_api/services/search_index_service.py").read_text(encoding="utf-8")
    assert "setattr(version" not in src
    assert "setattr(document_version" not in src
    assert "except AttributeError:" in src
