"""引用溯源：chunk 原文端点 + citations 的 sag source_id 语义。"""

import hashlib
import uuid
from datetime import UTC, datetime

import httpx
import pytest


@pytest.mark.asyncio
async def test_chunk_endpoint_and_citation_refs():
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import Source
    from sag_api.generation.prompt import build_citations
    from sag_api.main import app
    from sag_api.sag.dto import RetrievedSection

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            tok = (
                await c.post(
                    "/api/v1/auth/register",
                    json={"email": "trace@x.com", "password": "password123"},
                )
            ).json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            src = (await c.post("/api/v1/sources", headers=H, json={"name": "手册"})).json()
            sid = src["id"]
            async with SessionLocal() as s:
                scid = (await s.get(Source, sid)).sag_source_config_id

            # 注入一个分块（模拟 ingest 产物）
            await app.state.engine_manager.provision(scid)
            from zleap.sag.db.models import DataSource, SourceChunk

            chunk_id = uuid.uuid4().hex
            full_text = "导出支持 Markdown / PDF / JSON。" * 30  # 远超引用预览上限
            sf = await app.state.engine_manager.get_sag_session_factory(scid)
            async with sf() as s:
                await s.merge(DataSource(id=scid, name="手册"))
                s.add(
                    SourceChunk(
                        id=chunk_id,
                        data_source_id=scid,
                        source_type="doc",
                        source_id="d1",
                        heading="导出与备份",
                        content=full_text,
                    )
                )
                await s.commit()

            # 原文端点：返回完整内容 + sag 信源标识
            r = await c.get(f"/api/v1/sources/{sid}/chunks/{chunk_id}", headers=H)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["content"] == full_text and len(body["content"]) > 240
            assert body["heading"] == "导出与备份"
            assert body["source_id"] == sid and body["source_name"] == "手册"

            # 不存在 / 跨源访问 → 404
            assert (await c.get(f"/api/v1/sources/{sid}/chunks/{uuid.uuid4().hex}", headers=H)).status_code == 404

            # citations：source_id 应为 sag 信源 id（非引擎内部 id）
            section = RetrievedSection(
                chunk_id=chunk_id,
                heading="导出与备份",
                content=full_text,
                score=0.9,
                source_id="engine-internal-id",
                source_config_id=scid,
            )
            cites = build_citations([section], {scid: {"id": sid, "name": "手册"}})
            assert cites[0]["source_id"] == sid
            assert cites[0]["source_name"] == "手册"
            assert cites[0]["snippet"].endswith("…") and len(cites[0]["snippet"]) <= 722
            assert "summary" not in cites[0]


@pytest.mark.asyncio
async def test_search_unit_locator_resolution_is_exact_and_acl_scoped():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import CanonicalBlock, Document, DocumentVersion, SearchUnit, Source
    from sag_api.enums import DocumentStatus
    from sag_api.sag import RetrievedSection
    from sag_api.services.evidence_service import has_traceable_locator, resolve_traceable_evidence

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="trace source", sag_source_config_id=uuid.uuid4().hex)
        other_source = Source(name="other source", sag_source_config_id=uuid.uuid4().hex)
        session.add_all([source, other_source])
        await session.flush()
        document_id = uuid.uuid4().hex
        version_id = uuid.uuid4().hex
        block_id = uuid.uuid4().hex
        chunk_id = uuid.uuid4().hex
        session.add(
            Document(
                id=document_id,
                source_id=source.id,
                filename="runbook.pdf",
                content_type="application/pdf",
                size_bytes=100,
                storage_path="/tmp/runbook.pdf",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=3,
                file_hash=uuid.uuid4().hex,
                status="RECEIVED",
                search_status="READY",
                search_ready_at=datetime.now(UTC),
                metadata_json={},
            )
        )
        await session.flush()
        session.add(
            CanonicalBlock(
                id=block_id,
                document_version_id=version_id,
                ordinal=4,
                block_type="paragraph",
                page_from=8,
                page_to=8,
                section_path="Deploy / Rollback",
                source_anchor="pdf-page-8-block-4",
                normalized_text="Use the approved rollback.",
                    content_hash=hashlib.sha256(b"Use the approved rollback.").hexdigest(),
            )
        )
        await session.flush()
        session.add(
            SearchUnit(
                id=chunk_id,
                document_version_id=version_id,
                block_from_id=block_id,
                block_to_id=block_id,
                security_partition_id="project-a",
                content_hash=hashlib.sha256(b"Use the approved rollback.").hexdigest(),
                token_count=6,
                page_from=8,
                page_to=8,
                section_path="Deploy / Rollback",
            )
        )
        await session.commit()
        allowed = source
        denied = other_source

    section = RetrievedSection(
        chunk_id=chunk_id,
        heading="Rollback",
        content="Use the approved rollback.",
        source_config_id=allowed.sag_source_config_id,
    )
    resolved = await resolve_traceable_evidence([section], [allowed])
    assert has_traceable_locator(resolved[0])
    assert resolved[0].document_id == document_id
    assert resolved[0].document_version_id == version_id
    assert resolved[0].page_from == 8 and resolved[0].page_to == 8
    assert resolved[0].anchor == "pdf-page-8-block-4"

    denied_result = await resolve_traceable_evidence([section], [denied])
    assert not has_traceable_locator(denied_result[0])

    mismatched_duplicate = section.model_copy(
        update={"content": "Forged content with a colliding SearchUnit ID."}
    )
    duplicate_result = await resolve_traceable_evidence(
        [mismatched_duplicate, section],
        [allowed],
    )
    assert not has_traceable_locator(duplicate_result[0])
    assert has_traceable_locator(duplicate_result[1])
