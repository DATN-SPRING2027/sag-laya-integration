"""TDD regression tests for Milestone P4 Batch B Commit 3 (S1-06, S1-07, S1-10)."""

from __future__ import annotations

import io
import struct
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1.attachments import router as attachments_router
from sag_api.api.v1.documents import router as documents_router
from sag_api.core.config import Settings
from sag_api.core.conversation_ownership import get_conversation_owner
from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user, get_engine_manager, require_path_source_access
from sag_api.core.errors import ApiError
from sag_api.core.identity import VerifiedPrincipal as LocalVerifiedPrincipal
from sag_api.core.principal_assertion import VerifiedPrincipal, require_project_read_principal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    SearchUnit,
    Source,
    SourceProjectMapping,
    User,
)
from sag_api.enums import DocumentStatus, SourceType
from sag_api.mcp.server import build_source_mcp, use_scope
from sag_api.parsing.service import _signature
from sag_api.schemas.routing_rag import ProjectDocumentVersionRef
from sag_api.services.document_service import (
    get_project_document_readiness_batch,
    handle_document_upload,
)


def _make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def _make_principal(
    *,
    tenant_id: str = "tenant-1",
    project_id: str = "proj-1",
    partition_id: str = "part-1",
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-1",
        organization_id=tenant_id,
        allowed_project_ids=frozenset({project_id}),
        issuer="continuum-bff",
        key_id="kid-1",
        token_id="jti-1",
        issued_at=1_700_000_000,
        expires_at=1_700_000_900,
        tenant_id=tenant_id,
        allowed_partition_ids=frozenset({partition_id}),
    )


def _make_app(session, source: Source, principal: VerifiedPrincipal) -> FastAPI:
    app = FastAPI()
    app.include_router(documents_router)
    app.include_router(attachments_router)

    @app.exception_handler(ApiError)
    async def _err(_req, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    fake_user = User(id="user-1", email="u@example.com", password_hash="x", name="U")
    fake_engine = SimpleNamespace(
        get_document_markdown=lambda *_a, **_kw: None,
        list_chunk_headings=lambda *_a, **_kw: [],
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[require_path_source_access] = lambda: source
    app.dependency_overrides[require_project_read_principal] = lambda: principal
    app.dependency_overrides[get_engine_manager] = lambda: fake_engine
    app.dependency_overrides[get_conversation_owner] = lambda: "a" * 64
    return app


# ---------------------------------------------------------------------------
# S1-06: GET /{document_id}/parsed & MCP outline/read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_06_parsed_endpoint_resolves_sidecar_and_canonical_blocks_without_sag_source_id(
    tmp_path, monkeypatch
) -> None:
    cfg = _make_settings(mineru_official_api_token=None)
    cfg.activate_data_dir(tmp_path / "data")
    monkeypatch.setattr("sag_api.core.config.settings", cfg)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    principal = _make_principal()

    async with factory() as session:
        src = Source(
            id="src-p1",
            name="Canonical Source",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="cfg-p1",
            config={},
        )
        mapping = SourceProjectMapping(
            source_id="src-p1",
            organization_id="tenant-1",
            project_id="proj-1",
            state="CONFIRMED",
            confirmed_at=now,
            confirmed_by="admin",
            approval_ref="apr-1",
        )
        # Doc 1: sidecar exists, sag_source_id is None
        pdf_path = tmp_path / "arch.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%binary-garbage\n")
        sig = _signature("markitdown", cfg)
        sidecar_path = tmp_path / f"arch.pdf.parsed.{sig}.md"
        sidecar_path.write_text("<!-- PAGE 1 -->\n\n# Sidecar Title\n\nBody text.\n", encoding="utf-8")

        doc1 = Document(
            id="doc-sidecar",
            source_id="src-p1",
            tenant_id="tenant-1",
            project_id="proj-1",
            filename="arch.pdf",
            content_type="application/pdf",
            storage_path=str(pdf_path),
            status=DocumentStatus.READY,
            sag_source_id=None,
            parser_provider="markitdown",
            is_active=True,
        )
        ver1 = DocumentVersion(
            id="ver-sidecar",
            document_id="doc-sidecar",
            version_no=1,
            file_hash="1" * 64,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            valid_from=now - timedelta(minutes=1),
            valid_to=now + timedelta(days=365),
            metadata_json={"security_partition_id": "part-1"},
        )

        # Doc 2: no file/sidecar on disk, reconstruct from CanonicalBlocks
        doc2 = Document(
            id="doc-canon",
            source_id="src-p1",
            tenant_id="tenant-1",
            project_id="proj-1",
            filename="canon.pdf",
            content_type="application/pdf",
            storage_path=str(tmp_path / "missing_canon.pdf"),
            status=DocumentStatus.READY,
            sag_source_id=None,
            is_active=True,
        )
        ver2 = DocumentVersion(
            id="ver-canon",
            document_id="doc-canon",
            version_no=1,
            file_hash="2" * 64,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            valid_from=now - timedelta(minutes=1),
            valid_to=now + timedelta(days=365),
            metadata_json={"security_partition_id": "part-1"},
        )
        blk_h = CanonicalBlock(
            id="blk-h1",
            document_version_id="ver-canon",
            ordinal=0,
            block_type="heading",
            normalized_text="Canonical Heading",
            content_hash="a" * 64,
            page_from=1,
            page_to=1,
            section_path="Canonical Heading",
        )
        blk_p = CanonicalBlock(
            id="blk-p1",
            document_version_id="ver-canon",
            ordinal=1,
            block_type="paragraph",
            normalized_text="Reconstructed paragraph from CanonicalBlock.",
            content_hash="b" * 64,
            page_from=2,
            page_to=2,
            section_path="Canonical Heading",
        )
        session.add_all([src, mapping, doc1, ver1, doc2, ver2, blk_h, blk_p])
        await session.commit()

        app = _make_app(session, src, principal)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r1 = await client.get("/sources/src-p1/documents/doc-sidecar/parsed")
            assert r1.status_code == 200
            assert "# Sidecar Title" in r1.text

            r2 = await client.get("/sources/src-p1/documents/doc-canon/parsed")
            assert r2.status_code == 200
            assert "# Canonical Heading" in r2.text
            assert "Reconstructed paragraph from CanonicalBlock." in r2.text
            assert "<!-- PAGE 2 -->" in r2.text

            # Unauthorized partition must not leak CanonicalBlocks
            denied_principal = _make_principal(partition_id="other-part")
            app_denied = _make_app(session, src, denied_principal)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_denied), base_url="http://test"
            ) as denied_client:
                r_denied = await denied_client.get("/sources/src-p1/documents/doc-canon/parsed")
                assert r_denied.status_code == 404

    await engine.dispose()


@pytest.mark.asyncio
async def test_s1_06_mcp_outline_and_read_support_canonical_documents(tmp_path, monkeypatch) -> None:
    cfg = _make_settings(mineru_official_api_token=None)
    monkeypatch.setattr("sag_api.core.config.settings", cfg)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("sag_api.core.db.SessionLocal", factory)

    now = datetime.now(UTC)
    principal = _make_principal()

    async with factory() as session:
        src = Source(
            id="src-mcp-1",
            name="MCP Source",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="cfg-mcp-1",
            config={},
        )
        mapping = SourceProjectMapping(
            source_id="src-mcp-1",
            organization_id="tenant-1",
            project_id="proj-1",
            state="CONFIRMED",
            confirmed_at=now,
            confirmed_by="admin",
            approval_ref="apr-mcp",
        )
        bin_pdf = tmp_path / "binary.pdf"
        bin_pdf.write_bytes(b"%PDF-1.4\n\x00\xff\xfeRAW_BINARY_BYTES\n")
        sig = _signature("markitdown", cfg)
        sidecar = tmp_path / f"binary.pdf.parsed.{sig}.md"
        sidecar.write_text("# Parsed PDF Line 1\nSecond parsed line\n", encoding="utf-8")

        doc = Document(
            id="doc-mcp-1",
            source_id="src-mcp-1",
            tenant_id="tenant-1",
            project_id="proj-1",
            filename="binary.pdf",
            content_type="application/pdf",
            storage_path=str(bin_pdf),
            status=DocumentStatus.READY,
            sag_source_id=None,
            parser_provider="markitdown",
            is_active=True,
        )
        ver = DocumentVersion(
            id="ver-mcp-1",
            document_id="doc-mcp-1",
            version_no=1,
            file_hash="3" * 64,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            valid_from=now - timedelta(minutes=1),
            valid_to=now + timedelta(days=365),
            metadata_json={"security_partition_id": "part-1"},
        )
        blk = CanonicalBlock(
            id="blk-mcp-h1",
            document_version_id="ver-mcp-1",
            ordinal=0,
            block_type="heading",
            normalized_text="Section One",
            content_hash="c" * 64,
            page_from=1,
            page_to=1,
            section_path="Section One",
        )
        session.add_all([src, mapping, doc, ver, blk])
        await session.commit()

        mcp = build_source_mcp()
        with use_scope(SimpleNamespace(), [src], principal):
            outline_res = await mcp._tool_manager.call_tool("outline", {"document_id": "doc-mcp-1"})
            outline_text = "".join(getattr(p, "text", str(p)) for p in outline_res)
            assert "Section One" in outline_text
            assert "blk-mcp-h1" in outline_text

            read_res = await mcp._tool_manager.call_tool("read", {"document_id": "doc-mcp-1"})
            read_text = "".join(getattr(p, "text", str(p)) for p in read_res)
            assert "Parsed PDF Line 1" in read_text
            assert "RAW_BINARY_BYTES" not in read_text

    await engine.dispose()


# ---------------------------------------------------------------------------
# S1-07: Attachment magic-byte & pixel-bomb verification + logical_source_id <= 512
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_07_attachments_rejects_spoofed_corrupt_and_bomb_images(
    tmp_path, monkeypatch
) -> None:
    cfg = _make_settings()
    cfg.activate_data_dir(tmp_path / "data")
    monkeypatch.setattr("sag_api.api.v1.attachments.settings", cfg)

    app = FastAPI()
    app.include_router(attachments_router)

    @app.exception_handler(ApiError)
    async def _err(_req, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.dependency_overrides[get_conversation_owner] = lambda: "b" * 64

    # Real valid tiny PNG
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color=(10, 20, 30)).save(buf, format="PNG")
    valid_png = buf.getvalue()

    # Bomb PNG header (10000x10000 = 100M pixels > 36M)
    ihdr = struct.pack(">IIBBBBB", 10000, 10000, 8, 2, 0, 0, 0)
    bomb_png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Valid PNG succeeds
        ok = await client.post(
            "/attachments",
            files={"file": ("ok.png", valid_png, "image/png")},
        )
        assert ok.status_code == 201

        # 2. Spoofed HTML disguised as .png fails 422
        spoof = await client.post(
            "/attachments",
            files={"file": ("evil.png", b"<html><script>alert(1)</script></html>", "image/png")},
        )
        assert spoof.status_code == 422

        # 3. JPEG magic bytes uploaded with .png extension fails 422
        jpg_buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(jpg_buf, format="JPEG")
        mismatch = await client.post(
            "/attachments",
            files={"file": ("wrong.png", jpg_buf.getvalue(), "image/png")},
        )
        assert mismatch.status_code == 422

        # 4. Pixel-bomb PNG fails 422
        bomb = await client.post(
            "/attachments",
            files={"file": ("bomb.png", bomb_png, "image/png")},
        )
        assert bomb.status_code == 422


@pytest.mark.asyncio
async def test_s1_07_handle_document_upload_bounds_long_logical_source_id(
    tmp_path, monkeypatch
) -> None:
    cfg = _make_settings()
    cfg.activate_data_dir(tmp_path / "data")
    monkeypatch.setattr("sag_api.services.document_service.settings", cfg)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    very_long_logical = "source:abc:" + ("very_long_segment_" * 45)
    assert len(very_long_logical) > 512

    async with factory() as session:
        res = await handle_document_upload(
            session,
            tenant_id="tenant-long",
            project_id="proj-long",
            owner_id="user-long",
            security_partition_id="part-long",
            client_token="tok-long-1",
            file_bytes=b"# Heading\n\nBody content.\n",
            original_filename="long.md",
            content_type="text/markdown",
            logical_source_id=very_long_logical,
        )
        doc = await session.get(Document, res.document_id)
        assert doc is not None
        assert doc.logical_source_id is not None
        assert len(doc.logical_source_id) <= 512

    await engine.dispose()


# ---------------------------------------------------------------------------
# S1-10: Readiness does not trust stale document.chunk_count; version-scoped SearchUnits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_10_readiness_ignores_document_chunk_count_and_counts_version_units() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    async with factory() as session:
        src = Source(
            id="src-r1",
            name="Ready Src",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="cfg-r1",
            config={},
        )
        mapping = SourceProjectMapping(
            source_id="src-r1",
            organization_id="tenant-1",
            project_id="proj-1",
            state="CONFIRMED",
            confirmed_at=now,
            confirmed_by="admin",
            approval_ref="apr-r1",
        )
        # Document has chunk_count=0 (canonical Phase 2C never updates document.chunk_count),
        # while v1 is SEARCH_READY with 2 SearchUnits and v2 is SEARCH_READY with 1 SearchUnit.
        doc = Document(
            id="doc-r1",
            source_id="src-r1",
            tenant_id="tenant-1",
            project_id="proj-1",
            filename="r.md",
            storage_path="/tmp/r.md",
            status=DocumentStatus.READY,
            chunk_count=0,
            is_active=True,
        )
        v1 = DocumentVersion(
            id="ver-r1-1",
            document_id="doc-r1",
            version_no=1,
            file_hash="a" * 64,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=30),
            metadata_json={"security_partition_id": "part-1"},
        )
        v2 = DocumentVersion(
            id="ver-r1-2",
            document_id="doc-r1",
            version_no=2,
            file_hash="b" * 64,
            status="READY",
            search_status="SEARCH_READY",
            search_ready_at=now,
            valid_from=now - timedelta(minutes=1),
            valid_to=now + timedelta(days=30),
            metadata_json={"security_partition_id": "part-1"},
        )
        b1 = CanonicalBlock(
            id="blk-r1",
            document_version_id="ver-r1-1",
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="v1 body",
            content_hash="1" * 64,
        )
        b2 = CanonicalBlock(
            id="blk-r2",
            document_version_id="ver-r1-2",
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Root",
            normalized_text="v2 body",
            content_hash="2" * 64,
        )
        u1a = SearchUnit(
            id="unit-r1-a",
            document_version_id="ver-r1-1",
            block_from_id="blk-r1",
            block_to_id="blk-r1",
            security_partition_id="part-1",
            content_hash="1" * 64,
            token_count=4,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
        u1b = SearchUnit(
            id="unit-r1-b",
            document_version_id="ver-r1-1",
            block_from_id="blk-r1",
            block_to_id="blk-r1",
            security_partition_id="part-1",
            content_hash="2" * 64,
            token_count=4,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
        u2a = SearchUnit(
            id="unit-r2-a",
            document_version_id="ver-r1-2",
            block_from_id="blk-r2",
            block_to_id="blk-r2",
            security_partition_id="part-1",
            content_hash="3" * 64,
            token_count=4,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
        session.add_all([src, mapping, doc, v1, v2, b1, b2, u1a, u1b, u2a])
        await session.commit()

        principal = LocalVerifiedPrincipal(
            user_id="user-1",
            tenant_id="tenant-1",
            allowed_projects=frozenset({"proj-1"}),
            allowed_partitions=frozenset({"part-1"}),
        )
        res = await get_project_document_readiness_batch(
            session,
            project_id="proj-1",
            documents=[
                ProjectDocumentVersionRef(document_id="doc-r1", version_no=1),
                ProjectDocumentVersionRef(document_id="doc-r1", version_no=2),
            ],
            principal=principal,
        )
        assert res.items[0].search_ready is True
        assert res.items[0].chunk_count == 2
        assert res.items[1].search_ready is True
        assert res.items[1].chunk_count == 1

    await engine.dispose()
