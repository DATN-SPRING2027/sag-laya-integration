"""Opt-in live integration tests against PostgreSQL, Qdrant, MinerU, and configured LLM/router.

Disabled by default offline. Run with:
    SAG_RUN_LIVE_INTEGRATION=1 pytest -m integration -v --tb=short
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("SAG_RUN_LIVE_INTEGRATION") != "1",
        reason="set SAG_RUN_LIVE_INTEGRATION=1 to run live integration tests",
    ),
]

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


class _LiveQdrantTestEmbedder:
    """Deterministic unit-norm embedder for live Qdrant dense+sparse integration checks."""

    model = "live-integration-embed-64"
    base_url = "http://live-integration.local"

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for txt in texts:
            digest = hashlib.sha256(txt.encode("utf-8")).digest()
            raw = [
                ((digest[i % len(digest)] / 255.0) * 2.0 - 1.0)
                for i in range(self._dim)
            ]
            norm = sum(x * x for x in raw) ** 0.5 or 1.0
            vectors.append([x / norm for x in raw])
        return vectors


def _minimal_pdf_bytes(text_line: str = "SAG Live Integration Probe") -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text_line}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        (
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        ),
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        (
            f"5 0 obj << /Length {len(stream)} >> stream\n".encode("latin-1")
            + stream
            + b"\nendstream endobj\n"
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(
        (
            f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("latin-1")
    )
    return bytes(out)


@pytest.mark.asyncio
async def test_live_postgres_migrations_0001_to_0014() -> None:
    from sag_api.core.config import Settings
    from sag_api.db import models as _models  # noqa: F401
    from sag_api.db.base import Base

    db_url_raw = os.getenv("SAG_TEST_DATABASE_URL") or Settings().database_url
    if not db_url_raw.startswith("postgresql+asyncpg"):
        pytest.skip("PostgreSQL database_url not configured for live integration test")

    schema_name = f"sag_it_{uuid4().hex[:12]}"
    base_url = make_url(db_url_raw)
    base_query = dict(base_url.query)
    base_query.pop("search_path", None)
    clean_url = base_url.set(query=base_query)

    admin_engine = create_async_engine(clean_url, echo=False, future=True)
    scoped_engine = create_async_engine(
        clean_url,
        echo=False,
        future=True,
        connect_args={"server_settings": {"search_path": schema_name}},
    )
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

        async with scoped_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        migration_files = sorted(_MIGRATIONS_DIR.glob("00*.sql"))
        assert len(migration_files) >= 14
        async with scoped_engine.connect() as conn:
            raw_conn = await conn.get_raw_connection()
            asyncpg_conn = raw_conn.driver_connection
            await asyncpg_conn.execute(f'SET search_path TO "{schema_name}"')
            for sql_file in migration_files:
                await asyncpg_conn.execute(sql_file.read_text(encoding="utf-8"))

        async with scoped_engine.connect() as conn:
            su_cols = set(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'search_units'"
                        ),
                        {"schema": schema_name},
                    )
                ).scalars()
            )
            assert {"char_start", "char_end", "text_snapshot"}.issubset(su_cols)

            cb_cols = set(
                (
                    await conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = 'canonical_blocks'"
                        ),
                        {"schema": schema_name},
                    )
                ).scalars()
            )
            assert "is_boilerplate" in cb_cols

            ls_len = await conn.scalar(
                text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'documents' "
                    "AND column_name = 'logical_source_id'"
                ),
                {"schema": schema_name},
            )
            assert ls_len == 512

            fk_rows = (
                await conn.execute(
                    text(
                        "SELECT a.attname, c.confdeltype "
                        "FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "JOIN pg_namespace n ON n.oid = t.relnamespace "
                        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey) "
                        "WHERE n.nspname = :schema AND t.relname = 'search_units' "
                        "AND c.contype = 'f' AND a.attname IN ('block_from_id', 'block_to_id')"
                    ),
                    {"schema": schema_name},
                )
            ).all()
            assert len(fk_rows) == 2
            assert all(str(deltype) == "c" for _col, deltype in fk_rows)
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_live_qdrant_dense_bm25_meta_and_delete_source() -> None:
    from sag_api.core.config import Settings, settings
    from sag_api.db.models.routing_rag import CanonicalBlock, SearchUnit
    from sag_api.sag.search_unit_store import (
        SearchUnitQdrantStore,
        build_search_filter,
        build_sparse_query_vector,
    )
    from sag_api.services.search_index_service import (
        build_embedding_model_signature,
        collection_meta_point_id,
        delete_document_points_from_qdrant,
        ensure_qdrant_collection_and_indexes,
        index_search_units_to_qdrant,
    )

    cfg = Settings()
    headers = {"api-key": cfg.sag_qdrant_api_key} if cfg.sag_qdrant_api_key else {}
    project_id = f"it_{uuid4().hex[:12]}"
    collection_name = f"search_units_{project_id}"
    embedder = _LiveQdrantTestEmbedder(dim=64)
    emb_sig = build_embedding_model_signature(embedder, settings_obj=settings, dimensions=64)

    async with httpx.AsyncClient(
        base_url=cfg.sag_qdrant_url.rstrip("/"), headers=headers, timeout=15.0
    ) as client:
        try:
            await ensure_qdrant_collection_and_indexes(
                client,
                collection_name,
                vector_dim=64,
                embedding_signature=emb_sig,
                embedding_model=embedder.model,
                embedding_base_url=embedder.base_url,
            )

            info_res = await client.get(f"/collections/{collection_name}")
            info_res.raise_for_status()
            params = info_res.json()["result"]["config"]["params"]
            assert params["vectors"]["content_vector"]["size"] == 64
            assert params["sparse_vectors"]["bm25_sparse"]["modifier"] == "idf"

            meta_res = await client.get(
                f"/collections/{collection_name}/points/{collection_meta_point_id(collection_name)}",
                params={"with_payload": "true", "with_vector": "false"},
            )
            meta_res.raise_for_status()
            meta_payload = meta_res.json()["result"]["payload"]
            assert meta_payload["_collection_meta"] is True
            assert meta_payload["payload_only"] is True
            assert meta_payload["embedding_signature"] == emb_sig

            doc_id = f"doc-{uuid4().hex[:8]}"
            ver_id = f"ver-{uuid4().hex[:8]}"
            blk_id = f"blk-{uuid4().hex[:8]}"
            text_vi = "Điều 15 tầng 2 giá trị 1,500 triệu đồng cho hợp đồng bảo trì."
            blk = CanonicalBlock(
                id=blk_id,
                document_version_id=ver_id,
                ordinal=0,
                block_type="paragraph",
                normalized_text=text_vi,
                content_hash=hashlib.sha256(text_vi.encode("utf-8")).hexdigest(),
                page_from=2,
                page_to=2,
                section_path="Điều 15",
            )
            unit = SearchUnit(
                id=f"su-{uuid4().hex[:8]}",
                document_version_id=ver_id,
                security_partition_id="public",
                block_from_id=blk_id,
                block_to_id=blk_id,
                char_start=0,
                char_end=len(text_vi),
                text_snapshot=text_vi,
                token_count=16,
                page_from=2,
                page_to=2,
                section_path="Điều 15",
                content_hash=blk.content_hash,
                qdrant_point_id=str(uuid4()),
            )
            ver = type(
                "_Ver",
                (),
                {
                    "id": ver_id,
                    "document_id": doc_id,
                    "version_no": 1,
                    "valid_from": datetime.now(UTC),
                    "valid_to": None,
                },
            )()

            upserted = await index_search_units_to_qdrant(
                client,
                project_id=project_id,
                tenant_id="tenant-it",
                version=ver,
                units=[unit],
                embedder=embedder,
                blocks_by_id={blk_id: blk},
                source_id="src-it",
                document_id=doc_id,
            )
            assert upserted == 1

            store = SearchUnitQdrantStore(client)
            flt = build_search_filter(
                project_id=project_id,
                tenant_id="tenant-it",
                partition_id="public",
                document_version_ids=[ver_id],
            )
            flt["must_not"] = [{"key": "payload_only", "match": {"value": True}}]

            query_vec = (await embedder.batch_generate([f"Điều 15\n\n{text_vi}"]))[0]
            dense_hits = await store.search(
                collection_name,
                vector_name="content_vector",
                query=query_vec,
                search_filter=flt,
                limit=5,
            )
            assert len(dense_hits) == 1
            assert dense_hits[0].payload["search_unit_id"] == unit.id

            sparse_q = build_sparse_query_vector("Điều 15 tầng 2 giá trị 1,500 triệu")
            sparse_hits = await store.search(
                collection_name,
                vector_name="bm25_sparse",
                query=sparse_q,
                search_filter=flt,
                limit=5,
            )
            assert len(sparse_hits) == 1
            assert sparse_hits[0].payload["search_unit_id"] == unit.id

            await delete_document_points_from_qdrant(
                client, project_id=project_id, document_version_ids=[ver_id]
            )
            remaining = await store.count(collection_name, search_filter=flt)
            assert remaining == 0
        finally:
            await client.delete(f"/collections/{collection_name}")


@pytest.mark.asyncio
async def test_live_mineru_parse_if_configured(tmp_path: Path) -> None:
    from sag_api.core.config import Settings
    from sag_api.parsing.mineru_official import OfficialMinerUClient

    cfg = Settings()
    if not cfg.mineru_configured:
        pytest.skip("SAG_MINERU_OFFICIAL_API_TOKEN is not configured")

    pdf_path = tmp_path / "live_probe.pdf"
    pdf_path.write_bytes(_minimal_pdf_bytes())
    md = await OfficialMinerUClient(cfg).parse(str(pdf_path))
    assert md.strip(), "MinerU returned empty Markdown"


@pytest.mark.asyncio
async def test_live_decision_router_and_llm_structured() -> None:
    from sag_api.core.config import Settings
    from sag_api.generation.json_parser import decode_json_object
    from sag_api.generation.llm import LLMClient
    from sag_api.services.decision_router import route_query_async

    cfg = Settings()
    router_ready = bool(
        (cfg.decision_provider == "siliconflow" and cfg.siliconflow_api_key)
        or (cfg.decision_provider == "openai_compatible" and cfg.decision_api_key)
        or (cfg.decision_provider == "llm" and cfg.llm_configured)
    )
    if not router_ready and not cfg.llm_configured:
        pytest.skip("Neither decision router nor LLM provider API key is configured")

    if router_ready:
        decision = await route_query_async("Điều 15 của quy chế nghỉ phép quy định gì?")
        assert decision.degraded is False
        assert decision.question_scope in {"project", "general", "application", "unknown"}

    if cfg.llm_configured:
        cfg.llm_max_tokens = min(cfg.llm_max_tokens, 64)
        cfg.llm_timeout_ms = min(cfg.llm_timeout_ms, 20_000)
        cfg.llm_max_retries = 0
        raw = await LLMClient(cfg).complete(
            [{"role": "user", "content": 'Return only a JSON object: {"status": "ok"}'}],
            response_format={"type": "json_object"},
        )
        parsed = decode_json_object(raw)
        assert isinstance(parsed, dict)
        assert parsed.get("status") == "ok"
