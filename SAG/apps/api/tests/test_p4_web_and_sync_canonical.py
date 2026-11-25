"""TDD regression tests for Milestone P4 Batch B Commit 2 (S1-05: Web Connector & Canonical Sync)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.connectors.base import DiscoveredDoc, LocalFile
from sag_api.connectors.web import extract_web_markdown, extract_web_title
from sag_api.core.config import Settings
from sag_api.core.errors import ValidationError
from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    Source,
    SourceProjectMapping,
)
from sag_api.enums import ConnectorKind, JobStatus, JobType, SourceType
from sag_api.jobs import tasks
from sag_api.services.source_service import sync_source


def _make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def test_web_connector_fallback_preserves_markdown_structure_and_unescapes_html(monkeypatch) -> None:
    sample_html = """<!doctype html>
    <html>
      <head><title>R&amp;D Architecture &amp; Specs &#39;2025&#39;</title></head>
      <body>
        <header><nav>Top Navigation Noise</nav></header>
        <aside>Sidebar Ad Noise</aside>
        <main>
          <h1>Service Overview</h1>
          <p>Latency &lt; 50ms &amp; 99.9% uptime. Read <a href="https://example.com/guide">Guide</a>.</p>
          <ul>
            <li>Fast ingest</li>
            <li>Canonical blocks</li>
          </ul>
          <table>
            <thead><tr><th>Tier</th><th>Target</th></tr></thead>
            <tbody><tr><td>API</td><td>45ms</td></tr></tbody>
          </table>
        </main>
        <script>console.log("secret_noise")</script>
        <footer>Copyright Footer Noise</footer>
      </body>
    </html>"""

    title = extract_web_title(sample_html)
    assert title == "R&D Architecture & Specs '2025'"

    # Force trafilatura to return None so the BS4 + markdownify fallback is exercised directly
    monkeypatch.setattr("trafilatura.extract", lambda *_a, **_kw: None)
    md = extract_web_markdown(sample_html)

    assert "Top Navigation Noise" not in md
    assert "Sidebar Ad Noise" not in md
    assert "secret_noise" not in md
    assert "Copyright Footer Noise" not in md
    assert "# Service Overview" in md
    assert "Latency < 50ms & 99.9% uptime." in md
    assert "[Guide](https://example.com/guide)" in md
    assert "Fast ingest" in md
    assert "| Tier | Target |" in md
    assert "| API | 45ms |" in md


@pytest.mark.asyncio
async def test_sync_source_uses_canonical_upload_dedups_and_versions_updates(
    tmp_path, monkeypatch
) -> None:
    cfg = _make_settings()
    cfg.activate_data_dir(tmp_path / "data")
    monkeypatch.setattr("sag_api.services.document_service.settings", cfg)
    monkeypatch.setattr("sag_api.core.config.settings", cfg)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    long_url = "https://docs.example.com/" + ("segment/" * 80) + "index.html"
    page_bodies = ["# Doc v1\n\nInitial web page body."]
    enqueued_jobs: list[str] = []

    class _FakeQueue:
        async def enqueue(self, job_id: str) -> None:
            enqueued_jobs.append(job_id)

    class _FakeWebConnector:
        meta = SimpleNamespace(supports_sync=True)

        async def discover(self, _config):
            return [
                DiscoveredDoc(
                    external_id=long_url,
                    filename="docs.example.com-index.md",
                    content_type="text/markdown",
                )
            ]

        async def fetch(self, _config, doc):
            p = tmp_path / "fetched.md"
            content = page_bodies[0]
            p.write_text(content, encoding="utf-8")
            return LocalFile(
                path=str(p),
                filename=doc.filename,
                content_type="text/markdown",
                size_bytes=len(content.encode()),
            )

    monkeypatch.setattr("sag_api.connectors.registry.get", lambda _kind: _FakeWebConnector())

    async with factory() as session:
        src = Source(
            id="src-web-1",
            name="Web Docs",
            source_type=SourceType.WEB,
            connector_kind=ConnectorKind.WEB,
            sag_source_config_id="cfg-web-1",
            config={"urls": [long_url]},
        )
        mapping = SourceProjectMapping(
            source_id="src-web-1",
            organization_id="tenant-web-1",
            project_id="proj-web-1",
            state="CONFIRMED",
            confirmed_at=datetime.now(UTC),
            confirmed_by="admin-1",
            approval_ref="apr-web-1",
        )
        session.add_all([src, mapping])
        await session.commit()

        # Sync 1 -> creates Document + v1 + IngestionRun + PROCESS_DOCUMENT
        job1 = await sync_source(
            session,
            "src-web-1",
            job_queue=_FakeQueue(),
            tenant_id="tenant-web-1",
            project_id="proj-web-1",
            owner_id="user-web-1",
            security_partition_id="part-web-1",
        )
        assert (job1.payload or {}).get("project_id") == "proj-web-1"
        await tasks._sync_source_unlocked(session, job1, job_queue=_FakeQueue())

        docs = (await session.scalars(select(Document).where(Document.source_id == "src-web-1"))).all()
        assert len(docs) == 1
        doc = docs[0]
        assert doc.tenant_id == "tenant-web-1"
        assert doc.project_id == "proj-web-1"
        assert doc.logical_source_id is not None and len(doc.logical_source_id) <= 512

        vers = (
            await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.asc())
            )
        ).all()
        assert len(vers) == 1
        assert vers[0].version_no == 1
        runs = (await session.scalars(select(IngestionRun))).all()
        assert len(runs) == 1
        assert len(enqueued_jobs) == 2  # job1 + process_document job

        # Sync 2 (identical content) -> deduplicates, still 1 version
        job2 = Job(
            type=JobType.SYNC_SOURCE,
            source_id="src-web-1",
            status=JobStatus.QUEUED,
            payload={
                "tenant_id": "tenant-web-1",
                "project_id": "proj-web-1",
                "owner_id": "user-web-1",
                "security_partition_id": "part-web-1",
            },
        )
        session.add(job2)
        await session.commit()
        await tasks._sync_source_unlocked(session, job2, job_queue=_FakeQueue())
        vers_after_dup = (
            await session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))
        ).all()
        assert len(vers_after_dup) == 1

        # Sync 3 (updated content) -> allocates v2 on same Document
        page_bodies[0] = "# Doc v2\n\nUpdated web page body."
        job3 = Job(
            type=JobType.SYNC_SOURCE,
            source_id="src-web-1",
            status=JobStatus.QUEUED,
            payload={},  # verify fallback to SourceProjectMapping works too
        )
        session.add(job3)
        await session.commit()
        await tasks._sync_source_unlocked(session, job3, job_queue=_FakeQueue())
        vers_after_update = (
            await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.asc())
            )
        ).all()
        assert [v.version_no for v in vers_after_update] == [1, 2]

        # Sync 4: Source without tenant/project mapping fails closed
        unmapped_src = Source(
            id="src-web-unmapped",
            name="Unmapped Web",
            source_type=SourceType.WEB,
            connector_kind=ConnectorKind.WEB,
            sag_source_config_id="cfg-web-unmapped",
            config={"urls": ["https://example.com/a"]},
        )
        unmapped_job = Job(
            type=JobType.SYNC_SOURCE,
            source_id="src-web-unmapped",
            status=JobStatus.QUEUED,
            payload={},
        )
        session.add_all([unmapped_src, unmapped_job])
        await session.commit()
        with pytest.raises(ValidationError):
            await tasks._sync_source_unlocked(session, unmapped_job, job_queue=_FakeQueue())

    await engine.dispose()
