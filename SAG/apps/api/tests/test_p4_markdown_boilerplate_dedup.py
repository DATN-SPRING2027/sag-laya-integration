"""TDD tests for Milestone P4 Commit 4: Markdown parser, boilerplate, heading-only units, numbers, and superseded."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source, SourceProjectMapping
from sag_api.db.models.routing_rag import CanonicalBlock, IngestionRun, StageRun
from sag_api.enums import DocumentStatus
from sag_api.parsing.canonical import extract_canonical_blocks, is_boilerplate_text
from sag_api.sag.dto import RetrievedSection
from sag_api.services import dedup_and_temporal_service, retrieval_service, search_index_service
from sag_api.services.canonical_service import persist_canonical_blocks
from sag_api.services.search_unit_retrieval_service import _load_current_ready_versions_checked


def test_markdown_structural_parser_frontmatter_setext_fences_tables_and_boilerplate():
    md = (
        "---\n"
        "title: Policy Spec\n"
        "document_id: DOC-42\n"
        "status: active\n"
        "---\n\n"
        "Setext Title\n"
        "============\n\n"
        "Setext Sub\n"
        "----------\n\n"
        "~~~python\n"
        "def tilde_fn():\n"
        "    return 1\n"
        "~~~\n\n"
        "ColA | ColB\n"
        "--- | ---\n"
        "1 | 2\n\n"
        "<table><tr><th>Item</th><th>Val</th></tr><tr><td>Alpha</td><td>1,500</td></tr></table>\n\n"
        "3 / 4\n\n"
        "Page 3 / 10\n\n"
        "```bash\n"
        "echo unclosed\n\n"
        "## After Unclosed Fence\n\n"
        "Body after unclosed fence.\n"
    )
    blocks = extract_canonical_blocks(md, version_id="v1")
    texts = [b.normalized_text for b in blocks]
    types = [b.block_type for b in blocks]

    # Frontmatter must NOT be emitted as a paragraph block
    assert not any("document_id: DOC-42" in t for t in texts)

    # Setext headings
    assert any(b.block_type == "heading" and b.normalized_text == "Setext Title" for b in blocks)
    assert any(b.block_type == "heading" and b.normalized_text == "Setext Sub" for b in blocks)

    # Tilde fence & safely closed backtick fence (does not swallow "## After Unclosed Fence")
    code_blocks = [b for b in blocks if b.block_type == "code"]
    assert len(code_blocks) == 2
    assert "def tilde_fn" in code_blocks[0].normalized_text
    assert code_blocks[1].normalized_text.strip().endswith("```")
    assert any(b.block_type == "heading" and b.normalized_text == "After Unclosed Fence" for b in blocks)
    assert any(b.block_type == "paragraph" and "Body after unclosed fence." in b.normalized_text for b in blocks)

    # Pipe table without outer pipes & MinerU HTML table
    table_blocks = [b for b in blocks if b.block_type == "table"]
    assert len(table_blocks) == 2
    assert "ColA | ColB" in table_blocks[0].normalized_text
    assert "Alpha" in table_blocks[1].normalized_text and "1,500" in table_blocks[1].normalized_text
    assert not any(
        "Alpha" in b.normalized_text for b in blocks if b.block_type == "paragraph"
    )
    assert "table" in types

    # Boilerplate fraction protection: "3 / 4" is NOT boilerplate, "Page 3 / 10" IS boilerplate
    assert is_boilerplate_text("3 / 4") is False
    assert is_boilerplate_text("Page 3 / 10") is True
    frac_blocks = [b for b in blocks if b.normalized_text == "3 / 4"]
    page_blocks = [b for b in blocks if b.normalized_text == "Page 3 / 10"]
    assert frac_blocks and frac_blocks[0].is_boilerplate is False
    assert page_blocks and page_blocks[0].is_boilerplate is True

    # section_path clamped <= 512 chars
    deep_md = "# " + ("A" * 300) + "\n\n## " + ("B" * 300) + "\n\nBody text."
    deep_blocks = extract_canonical_blocks(deep_md, version_id="v2")
    assert all(len(b.section_path) <= 512 for b in deep_blocks)


def test_hyphenated_page_footer_boilerplate_not_hijacked_by_list_parser():
    def _to_canonical_blocks(extracted, version_id: str) -> list[CanonicalBlock]:
        return [
            CanonicalBlock(
                id=f"{version_id}-blk-{b.ordinal}",
                document_version_id=version_id,
                ordinal=b.ordinal,
                block_type=b.block_type,
                page_from=b.page_from,
                page_to=b.page_to,
                section_path=b.section_path or "Root",
                normalized_text=b.normalized_text,
                content_hash=b.content_hash,
                is_boilerplate=b.is_boilerplate,
            )
            for b in extracted
        ]

    # Case 1: standalone hyphenated page footer "- 5 -"
    blocks_standalone = extract_canonical_blocks("- 5 -\n\nNội dung thật.", version_id="v-dash-1")
    dash_blocks = [b for b in blocks_standalone if "- 5 -" in b.normalized_text]
    assert len(dash_blocks) == 1
    assert dash_blocks[0].is_boilerplate is True
    assert dash_blocks[0].block_type == "paragraph"
    assert any(b.normalized_text == "Nội dung thật." and not b.is_boilerplate for b in blocks_standalone)

    units_standalone = search_index_service.build_search_units_from_blocks(
        _to_canonical_blocks(blocks_standalone, "v-dash-1"),
        document_version_id="v-dash-1",
        security_partition_id="part-1",
    )
    assert len(units_standalone) == 1
    assert "- 5 -" not in getattr(units_standalone[0], "_text_content", "")
    assert "Nội dung thật." in getattr(units_standalone[0], "_text_content", "")

    # Case 2: list immediately followed by hyphenated page footer "- 5 -"
    blocks_after_list = extract_canonical_blocks("- item 1\n- 5 -\n\nNội dung thật.", version_id="v-dash-2")
    list_blocks = [b for b in blocks_after_list if b.block_type == "list"]
    assert len(list_blocks) == 1
    assert list_blocks[0].normalized_text == "- item 1"
    dash_blocks_2 = [b for b in blocks_after_list if b.normalized_text == "- 5 -"]
    assert len(dash_blocks_2) == 1
    assert dash_blocks_2[0].is_boilerplate is True

    units_after_list = search_index_service.build_search_units_from_blocks(
        _to_canonical_blocks(blocks_after_list, "v-dash-2"),
        document_version_id="v-dash-2",
        security_partition_id="part-1",
    )
    combined_units_text = "\n".join(getattr(u, "_text_content", "") for u in units_after_list)
    assert "- item 1" in combined_units_text
    assert "Nội dung thật." in combined_units_text
    assert "- 5 -" not in combined_units_text

    # Case 3: boilerplate followed by Setext underline or immediate body line without blank line
    blocks_tight = extract_canonical_blocks("Page 2 of 10\n---\n\n- 5 -\nNội dung thật.", version_id="v-dash-3")
    assert all(b.block_type != "heading" for b in blocks_tight)
    bp_texts = [b.normalized_text for b in blocks_tight if b.is_boilerplate]
    assert "Page 2 of 10" in bp_texts
    assert "- 5 -" in bp_texts
    assert any(b.normalized_text == "Nội dung thật." and not b.is_boilerplate for b in blocks_tight)



@pytest.mark.asyncio
async def test_canonical_persist_boilerplate_and_search_units_filter_and_no_heading_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            session.add(Source(id="src-bp", name="S", sag_source_config_id="cfg-bp"))
            await session.flush()
            session.add(
                Document(
                    id="doc-bp",
                    source_id="src-bp",
                    tenant_id="t1",
                    project_id="p1",
                    filename="bp.md",
                    storage_path="/tmp/bp.md",
                    status=DocumentStatus.READY,
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id="ver-bp",
                    document_id="doc-bp",
                    version_no=1,
                    file_hash="h-bp",
                    valid_from=now,
                    status="READY",
                )
            )
            await session.flush()

            extracted = extract_canonical_blocks(
                "# Lead Section With Long Title " + ("X " * 30) + "\n\n"
                "Body paragraph one with concrete facts about deployment.\n\n"
                "Page 2 / 5\n\n"
                "## Trailing Empty Heading\n",
                version_id="ver-bp",
            )
            persisted = await persist_canonical_blocks(session, "ver-bp", extracted)
            await session.commit()

            rows = (
                await session.scalars(
                    select(CanonicalBlock)
                    .where(CanonicalBlock.document_version_id == "ver-bp")
                    .order_by(CanonicalBlock.ordinal)
                )
            ).all()
            bp_rows = [r for r in rows if "Page 2 / 5" in r.normalized_text]
            assert len(bp_rows) == 1
            assert bp_rows[0].is_boilerplate is True

            # Build SearchUnits with tight max_tokens_per_unit so lead heading + body > budget
            units = search_index_service.build_search_units_from_blocks(
                persisted,
                document_version_id="ver-bp",
                security_partition_id="part-1",
                max_tokens_per_unit=15,
            )
            assert units
            unit_texts = [getattr(u, "_text_content", "") for u in units]
            assert not any("Page 2 / 5" in t for t in unit_texts)

            # No SearchUnit may consist solely of heading blocks
            by_id = {b.id: b for b in persisted}
            for u in units:
                if u.block_from_id == u.block_to_id:
                    assert by_id[u.block_from_id].block_type != "heading"
                else:
                    span_types = {
                        b.block_type
                        for b in persisted
                        if by_id[u.block_from_id].ordinal <= b.ordinal <= by_id[u.block_to_id].ordinal
                    }
                    assert span_types - {"heading"}, f"Heading-only SearchUnit emitted: {u.section_path}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dedup_vietnamese_decimals_dead_code_and_valid_from_filter():
    assert not hasattr(dedup_and_temporal_service, "_THOUSANDS_COMMA_RE")
    assert not hasattr(dedup_and_temporal_service, "register_relation_candidate")

    # 1,500 (1.5 in Vietnamese/EU) must NOT equal 1500
    contra, _, _ = dedup_and_temporal_service.detect_semantic_signals(
        "Hệ số điều chỉnh là 1,500.",
        "Hệ số điều chỉnh là 1500.",
    )
    assert contra is True

    # 1,5 vs 2,5 must detect quantitative change
    contra_dec, _, _ = dedup_and_temporal_service.detect_semantic_signals(
        "Tỉ lệ lỗi là 1,5%.",
        "Tỉ lệ lỗi là 2,5%.",
    )
    assert contra_dec is True

    # Future valid_from version must NOT be compared in run_dedup_and_temporal_stage
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            session.add(Source(id="s-dedup", name="S", sag_source_config_id="cfg-dedup"))
            await session.flush()
            session.add(
                Document(
                    id="d-cur",
                    source_id="s-dedup",
                    tenant_id="t1",
                    project_id="p1",
                    filename="cur.md",
                    storage_path="/tmp/cur.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                )
            )
            session.add(
                Document(
                    id="d-fut",
                    source_id="s-dedup",
                    tenant_id="t1",
                    project_id="p1",
                    filename="fut.md",
                    storage_path="/tmp/fut.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                )
            )
            await session.flush()
            v_cur = DocumentVersion(
                id="v-cur",
                document_id="d-cur",
                version_no=1,
                file_hash="h-cur",
                valid_from=now - timedelta(hours=1),
                status="READY",
            )
            v_fut = DocumentVersion(
                id="v-fut",
                document_id="d-fut",
                version_no=1,
                file_hash="h-fut",
                valid_from=now + timedelta(days=5),
                valid_to=now + timedelta(days=30),
                status="READY",
            )
            session.add_all([v_cur, v_fut])
            await session.flush()
            text_shared = "Shared exact paragraph text for dedup test."
            chash = hashlib.sha256(text_shared.encode()).hexdigest()
            session.add_all(
                [
                    CanonicalBlock(
                        id="b-cur",
                        document_version_id="v-cur",
                        ordinal=0,
                        block_type="paragraph",
                        page_from=1,
                        page_to=1,
                        section_path="Root",
                        normalized_text=text_shared,
                        content_hash=chash,
                    ),
                    CanonicalBlock(
                        id="b-fut",
                        document_version_id="v-fut",
                        ordinal=0,
                        block_type="paragraph",
                        page_from=1,
                        page_to=1,
                        section_path="Root",
                        normalized_text=text_shared,
                        content_hash=chash,
                    ),
                ]
            )
            await session.commit()

            summary = await dedup_and_temporal_service.run_dedup_and_temporal_stage(
                session,
                document_version=v_cur,
                document_id="d-cur",
                project_id="p1",
            )
            assert summary["exact_block_matches"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_superseded_filtering_no_fallback_and_valid_to_propagation():
    # 1. rerank_sections must NOT fall back to superseded results when query does not allow superseded
    sup_sec = RetrievedSection(
        chunk_id="u-sup",
        heading="Old Spec",
        content="Retention period is 30 days.",
        score=0.95,
        raw_dense_score=0.95,
        valid_to=datetime.now(UTC) - timedelta(days=1),
        is_superseded=True,
    )
    res_blocked = retrieval_service.rerank_sections(
        "What is the retention period?",
        [sup_sec],
        limit=5,
    )
    assert res_blocked.sections == []

    # Without explicit router temporal_mode, superseded keywords in query text must still fail closed
    res_still_blocked = retrieval_service.rerank_sections(
        "What was the previous superseded retention period?",
        [sup_sec],
        limit=5,
    )
    assert res_still_blocked.sections == []

    res_allowed = retrieval_service.rerank_sections(
        "What was the retention period before the change?",
        [sup_sec],
        limit=5,
        temporal_mode="change_lineage",
    )
    assert len(res_allowed.sections) == 1

    # 2. _load_current_ready_versions_checked respects include_superseded and propagates valid_to / is_superseded
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            now = datetime.now(UTC)
            src = Source(id="s-sup", name="S", sag_source_config_id="cfg-sup")
            session.add(src)
            await session.flush()
            session.add(
                SourceProjectMapping(
                    id="map-sup",
                    source_id="s-sup",
                    organization_id="org-1",
                    project_id="p1",
                    state="CONFIRMED",
                    mapping_version=1,
                    confirmed_at=now,
                    confirmed_by="u1",
                    approval_ref="ref-1",
                )
            )
            session.add(
                Document(
                    id="d-sup",
                    source_id="s-sup",
                    tenant_id="org-1",
                    project_id="p1",
                    filename="policy.md",
                    storage_path="/tmp/policy.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id="v-old",
                    document_id="d-sup",
                    version_no=1,
                    file_hash="h-old",
                    valid_from=now - timedelta(days=10),
                    valid_to=now - timedelta(days=1),
                    status="READY",
                    search_status="SEARCH_READY",
                    search_ready_at=now - timedelta(days=10),
                    metadata_json={"security_partition_id": "part-1"},
                )
            )
            await session.flush()
            blk = CanonicalBlock(
                id="blk-old",
                document_version_id="v-old",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Policy",
                normalized_text="Old policy text",
                content_hash="ch-old",
            )
            session.add(blk)
            await session.flush()
            from sag_api.db.models.routing_rag import SearchUnit

            session.add(
                SearchUnit(
                    id="su-old",
                    document_version_id="v-old",
                    block_from_id="blk-old",
                    block_to_id="blk-old",
                    security_partition_id="part-1",
                    content_hash="ch-old",
                    token_count=3,
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                )
            )
            run = IngestionRun(
                id="run-old",
                document_version_id="v-old",
                tenant_id="org-1",
                project_id="p1",
                idempotency_key="idem-old",
                payload_hash="ph-old",
                status="COMPLETED",
                started_at=now - timedelta(days=10),
            )
            session.add(run)
            await session.flush()
            checksum = "a" * 64
            session.add(
                StageRun(
                    id="sr-old",
                    run_id="run-old",
                    stage="INDEX_SEARCH",
                    status="SUCCESS",
                    metrics_json={
                        "manifest_verified": True,
                        "collection_name": "search_units_p1",
                        "search_unit_count": 1,
                        "pg_count": 1,
                        "qdrant_count": 1,
                        "qdrant_indexed_count": 1,
                        "manifest_checksum": checksum,
                        "qdrant_checksum": checksum,
                    },
                )
            )
            await session.commit()

            principal = VerifiedPrincipal(
                subject="u1",
                organization_id="org-1",
                issuer="https://issuer.test",
                key_id="k1",
                token_id="tok-1",
                issued_at=1000,
                expires_at=9999999999,
                tenant_id="org-1",
                allowed_project_ids=frozenset({"p1"}),
                allowed_partition_ids=frozenset({"part-1"}),
            )
            no_sup = await _load_current_ready_versions_checked(
                session, [src], principal, include_superseded=False
            )
            assert no_sup == {}

            with_sup = await _load_current_ready_versions_checked(
                session, [src], principal, include_superseded=True
            )
            assert "v-old" in with_sup
            assert with_sup["v-old"].is_superseded is True
            assert with_sup["v-old"].valid_to is not None
    finally:
        await engine.dispose()
