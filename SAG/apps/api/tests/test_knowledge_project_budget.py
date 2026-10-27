from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, KnowledgeJob, KnowledgeQueueControl, Source
from sag_api.jobs.knowledge import QueuePolicy, claim_job


@pytest_asyncio.fixture
async def budget_db():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Source(id='source', name='Source', sag_source_config_id='budget-test'))
        await session.flush()
        for index, project in enumerate(('project-a', 'project-a', 'project-b'), 1):
            session.add(Document(id=f'd{index}', source_id='source', tenant_id='tenant',
                                 project_id=project, filename='notes.md', storage_path='unused'))
            await session.flush()
            session.add(DocumentVersion(id=f'v{index}', document_id=f'd{index}', version_no=1,
                                        file_hash='hash'))
            await session.flush()
            session.add(KnowledgeJob(id=f'j{index}', document_version_id=f'v{index}', tenant_id='tenant',
                                     kind='E2', input_checksum='input', priority=4-index,
                                     available_at=datetime.now(UTC), created_at=datetime.now(UTC)))
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_unlimited_default_tracks_without_blocking_tenant(budget_db):
    policy = QueuePolicy()
    assert policy.daily_tokens == 0
    async with budget_db() as session:
        session.add(KnowledgeQueueControl(id='tenant:' + 'tenant:' + datetime.now(UTC).date().isoformat(),
                                          tokens_reserved=1_000_000))
        for expected in ('j1', 'j2', 'j3'):
            job = await claim_job(session, policy, e2_enabled=True)
            assert job.id == expected
            job.status = 'SUCCEEDED'
            await session.flush()
        total = await session.scalar(select(KnowledgeQueueControl).where(
            KnowledgeQueueControl.id.like('tenant:%')))
        assert total.tokens_reserved > 1_000_000


@pytest.mark.asyncio
async def test_project_cap_does_not_block_other_project_and_resets_next_day(budget_db):
    policy = QueuePolicy(daily_tokens=6000 * 4 + 1000 + 4096)
    now = datetime.now(UTC)
    async with budget_db() as session:
        first = await claim_job(session, policy, e2_enabled=True, now=now)
        assert first.id == 'j1'
        first.status = 'SUCCEEDED'
        await session.flush()
        other = await claim_job(session, policy, e2_enabled=True, now=now)
        assert other.id == 'j3'
        blocked = await session.get(KnowledgeJob, 'j2')
        assert blocked.error_code == 'DAILY_BUDGET'
        other.status = 'SUCCEEDED'
        await session.flush()
        # Cross midnight without hitting queue max age.
        next_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        released = await claim_job(session, policy, e2_enabled=True, now=next_day)
        assert released.id == 'j2'


@pytest.mark.asyncio
async def test_disabling_cap_releases_already_deferred_jobs(budget_db):
    async with budget_db() as session:
        job = await session.get(KnowledgeJob, 'j1')
        job.error_code = 'DAILY_BUDGET'
        job.available_at = datetime.now(UTC) + timedelta(days=1)
        await session.flush()
        released = await claim_job(session, QueuePolicy(daily_tokens=0), e2_enabled=True)
        assert released.id == 'j1'
        assert released.error_code is None


@pytest.mark.asyncio
async def test_scope_mismatch_cannot_charge_or_call_provider(budget_db):
    async with budget_db() as session:
        job = await session.get(KnowledgeJob, 'j1')
        job.tenant_id = 'wrong-tenant'
        await session.flush()
        claimed = await claim_job(session, QueuePolicy(), e2_enabled=True)
        assert claimed.id == 'j2'
        assert job.status == 'FAILED'
        assert job.error_code == 'INVALID_PROJECT_SCOPE'
        assert job.attempts == 0
