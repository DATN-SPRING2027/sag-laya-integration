"""TDD regression tests for Milestone P4 Batch B Commit 4 (S1-12: Secret Sanitizer & U+FFFD Tracking)."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.core.errors import UpstreamError
from sag_api.core.sanitizer import (
    decode_bytes_safely,
    inspect_replacement_chars,
    redact_parser_reason,
    sanitize_error_message,
)
from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    Source,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType, SourceType
from sag_api.jobs import tasks
from sag_api.parsing.mineru import _markdown_from_zip, download_mineru_result_markdown
from sag_api.services import search_index_service


def _make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def test_s1_12_sanitizer_redacts_modern_tokens_jwt_aws_and_query_keys() -> None:
    raw = (
        "Failed calling https://svc:topsecret@api.example.com/v1?access_key=abc123&secret_key=xyz789 "
        "&private_key=priv11&session_token=sess22 "
        "ghp_1234567890abcdefghij1234567890 "
        "github_pat_11AAAAAA0123456789_ABCDEFGHIJK "
        "AIzaSyDummyGoogleApiKey1234567890123 "
        "xoxb-1234567890-1234567890-abcdef123456 "
        "AKIAIOSFODNN7EXAMPLE "
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "Bearer my-bearer-secret sk-testsecret123456"
    )
    sanitized = sanitize_error_message(raw, max_length=1000)
    for leak in (
        "topsecret",
        "abc123",
        "xyz789",
        "priv11",
        "sess22",
        "ghp_1234567890",
        "github_pat_11AAAAAA",
        "AIzaSyDummyGoogleApiKey",
        "xoxb-1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "my-bearer-secret",
        "sk-testsecret123456",
    ):
        assert leak not in sanitized

    redacted_reason = redact_parser_reason("MinerU failed at https://internal.example.com/hook?token=secret999")
    assert redacted_reason is not None
    assert "secret999" not in redacted_reason
    assert "https://internal.example.com" not in redacted_reason
    assert "[URL REDACTED]" in redacted_reason
    assert redact_parser_reason("   ") is None
    assert redact_parser_reason(None) is None


def test_s1_12_replacement_char_inspection_and_decode_bytes_safely() -> None:
    count, ratio = inspect_replacement_chars("abc\ufffddef\ufffd")
    assert count == 2
    assert ratio == pytest.approx(2 / 8)

    # Non-utf-8 declared encoding that actually holds valid UTF-8 Vietnamese should recover cleanly
    vi_utf8 = "Tiếng Việt chuẩn Unicode".encode()
    recovered_text, r_count, _ = decode_bytes_safely(
        vi_utf8, encoding="ascii", context="test_utf8_recovery"
    )
    assert recovered_text == "Tiếng Việt chuẩn Unicode"
    assert r_count == 0

    # Severe U+FFFD corruption (> max(8, len // 100)) raises ValueError
    corrupted = b"\xff\xfe\xfd" * 12
    with pytest.raises(ValueError, match="U\\+FFFD"):
        decode_bytes_safely(corrupted, encoding="utf-8", context="corrupted_stream", raise_on_severe=True)


@pytest.mark.asyncio
async def test_s1_12_mineru_rejects_severely_corrupted_utf8_and_tasks_tracks_replacement_chars(
    tmp_path, monkeypatch
) -> None:
    # 1. MinerU zip with severely corrupted full.md raises UpstreamError
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("full.md", b"# Broken\n" + (b"\xff\xfe\xfd" * 20))
    with pytest.raises(UpstreamError):
        _markdown_from_zip(buf.getvalue(), 10 * 1024 * 1024)

    # 2. MinerU direct download with severely corrupted bytes raises UpstreamError
    monkeypatch.setattr("sag_api.parsing.mineru._assert_public_host", AsyncMock(return_value=None))

    def _handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"# Corrupted\n" + (b"\xff\xfe" * 20),
            headers={"content-type": "text/markdown; charset=utf-8"},
        )

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(
        "sag_api.parsing.mineru.httpx.AsyncClient",
        lambda **kw: orig_client(transport=httpx.MockTransport(_handler), **kw),
    )
    with pytest.raises(UpstreamError):
        await download_mineru_result_markdown(
            "https://cdn-mineru.openxlab.org.cn/res.md",
            request_timeout=5.0,
            result_limit=1024 * 1024,
        )

    # 3. tasks._process_document_unlocked tracks U+FFFD count & ratio in DocumentVersion & StageRun
    cfg = _make_settings(mineru_official_api_token=None)
    cfg.activate_data_dir(tmp_path / "data")
    monkeypatch.setattr(tasks, "settings", cfg)

    md_path = tmp_path / "minor_corrupt.md"
    # 2 replacement chars in a 1000+ char doc -> tracked & warned, not fatal
    md_text = "# Title\n\nValid Vietnamese content.\n\n" + ("word " * 200) + "\ufffd\ufffd\n"
    md_path.write_text(md_text, encoding="utf-8")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "SessionLocal", factory)

    async def _fake_index(session, *, document_version, **kwargs):
        document_version.search_status = "SEARCH_READY"
        return [SimpleNamespace(id="unit-1")]

    monkeypatch.setattr(search_index_service, "run_search_indexing_stage", _fake_index)
    manager = SimpleNamespace(
        get_sag_embedding=AsyncMock(return_value=None),
        process_document=AsyncMock(),
    )

    async with factory() as session:
        src = Source(
            id="src-enc-1",
            name="Enc Src",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="cfg-enc-1",
            config={},
        )
        doc = Document(
            id="doc-enc-1",
            source_id="src-enc-1",
            tenant_id="tenant-1",
            project_id="proj-1",
            filename="minor_corrupt.md",
            content_type="text/markdown",
            size_bytes=len(md_text.encode()),
            storage_path=str(md_path),
            status=DocumentStatus.PENDING,
            is_active=True,
        )
        ver = DocumentVersion(
            id="ver-enc-1",
            document_id="doc-enc-1",
            version_no=1,
            file_hash="e" * 64,
            status="RECEIVED",
            search_status="PENDING",
            metadata_json={"security_partition_id": "part-1"},
        )
        run = IngestionRun(
            id="run-enc-1",
            tenant_id="tenant-1",
            project_id="proj-1",
            document_version_id="ver-enc-1",
            idempotency_key="idem-enc-1",
            payload_hash="e" * 64,
            current_stage="RECEIVE",
            status="QUEUED",
        )
        job = Job(
            id="job-enc-1",
            type=JobType.PROCESS_DOCUMENT,
            source_id="src-enc-1",
            document_id="doc-enc-1",
            status=JobStatus.QUEUED,
            payload={"run_id": "run-enc-1", "project_id": "proj-1"},
        )
        session.add_all([src, doc, ver, run, job])
        await session.commit()

        await tasks._process_document_unlocked(session, job, engine_manager=manager)

        await session.refresh(ver)
        stage_run = await session.scalar(
            select(StageRun).where(StageRun.run_id == "run-enc-1", StageRun.stage == "PARSE")
        )
        assert ver.metadata_json is not None
        assert ver.metadata_json.get("replacement_char_count") == 2
        assert ver.metadata_json.get("replacement_char_ratio", 0) > 0
        assert stage_run is not None
        assert stage_run.metrics_json is not None
        assert stage_run.metrics_json.get("replacement_char_count") == 2

    await engine.dispose()
