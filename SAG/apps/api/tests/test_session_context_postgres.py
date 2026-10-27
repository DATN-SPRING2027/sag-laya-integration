"""Opt-in native PostgreSQL QA; only the dedicated disposable local DB is accepted."""

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_session_context_runtime import Counter, Encoder, principal
from test_session_context_storage import raw

from sag_api.core.errors import NotFoundError
from sag_api.db.base import Base
from sag_api.db.models import Agent, Message, SessionContextIndex, Source, SourceProjectMapping, Thread
from sag_api.services.agent_domain import persist_answer
from sag_api.services.session_context.runtime import prepare_context
from sag_api.services.session_context.storage import invalidate
from sag_api.services.session_context.types import ContextContractError, SessionKey, SessionScope

URL = "postgresql+asyncpg://session_qa@127.0.0.1:55439/session_qa"
pytestmark = pytest.mark.skipif(
    os.environ.get("SESSION_CONTEXT_NATIVE_PG_QA") != "1", reason="explicit disposable PG QA"
)


@pytest.fixture
async def native_pg():
    engine = create_async_engine(URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        # Exact dedicated QA endpoint/user/database only; never app settings DB.
        assert await connection.scalar(text("SELECT current_database()")) == "session_qa"
        await connection.run_sync(Base.metadata.drop_all)
        derived = {"session_context_indexes", "session_message_provenance"}
        await connection.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[table for table in Base.metadata.sorted_tables if table.name not in derived]
            )
        )
    driver = await asyncpg.connect(user="session_qa", database="session_qa", host="127.0.0.1", port=55439)
    await driver.execute((Path(__file__).parents[1] / "migrations/0007_session_context.sql").read_text())
    await driver.close()
    scope = SessionScope(
        SessionKey("owner", "agent", "thread"),
        "continuum-sag-session",
        "subject",
        "org",
        "",
        frozenset({"project"}),
        frozenset(),
        frozenset(),
        None,
        time.time() + 3600,
    )
    async with factory() as session:
        session.add(Agent(id="agent", owner_key="owner", name="Native QA"))
        await session.flush()
        session.add(Thread(id="thread", agent_id="agent", owner_key="owner"))
        await session.flush()
        session.add(raw("fact", content="I confirmed BLUE-472."))
        current = raw("current", content="Which code?", ordinal=1)
        session.add(current)
        await session.commit()
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=current,
            query=current.content,
            encoder=Encoder(),
            counter=Counter(),
        )
    try:
        yield factory, scope, context
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["message_delete", "thread_delete", "index_invalidate", "source_revoke"])
async def test_native_lease_blocks_concurrent_mutation_until_publication(native_pg, mutation):
    factory, scope, context = native_pg
    if mutation == "source_revoke":
        async with factory() as session:
            session.add(Source(id="source", name="QA source", sag_source_config_id="qa-source"))
            await session.flush()
            session.add(
                SourceProjectMapping(
                    id="mapping",
                    source_id="source",
                    organization_id="org",
                    project_id="project",
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="qa",
                    approval_ref="qa",
                )
            )
            await session.commit()
    dispatched = asyncio.Event()
    writer_pids = []

    async def writer():
        async with factory() as session:
            writer_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            dispatched.set()
            if mutation == "message_delete":
                await session.execute(delete(Message).where(Message.id == "fact"))
            elif mutation == "thread_delete":
                await session.execute(delete(Thread).where(Thread.id == "thread"))
            elif mutation == "index_invalidate":
                await invalidate(session, "thread")
            else:
                await session.execute(
                    update(SourceProjectMapping)
                    .where(SourceProjectMapping.id == "mapping")
                    .values(state="REVOKED", revoked_at=datetime.now(UTC), revoked_by="qa", revocation_ref="qa")
                )
            await session.commit()

    async with context.lease(factory, principal(scope)):
        if mutation == "source_revoke":
            await context.lock_sources({"source"}, principal(scope))
        task = asyncio.create_task(writer())
        await dispatched.wait()
        # Observe the server's lock wait, rather than infer blocking from a
        # sleeping/slow client that might not have submitted its mutation yet.
        async with asyncio.timeout(5):
            while not await context.lease_session.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": writer_pids[0]}
            ):
                assert not task.done(), "mutation bypassed actual PostgreSQL shared evidence lock"
                await asyncio.sleep(0.02)
        await context.revalidate(context.lease_session, principal(scope))
        # Separate publication session can insert its own answer/provenance while
        # selected originals remain locked, with no self-deadlock.
        message_id = await asyncio.wait_for(
            persist_answer(factory, "thread", "BLUE-472", [], session_context=context, principal=principal(scope)),
            timeout=5,
        )
        assert message_id
    await asyncio.wait_for(task, timeout=5)
    if mutation == "source_revoke":
        async with context.lease(factory, principal(scope)):
            with pytest.raises(ContextContractError):
                await context.lock_sources({"source"}, principal(scope))
    else:
        async with factory() as session:
            with pytest.raises((ContextContractError, NotFoundError)) as caught:
                await context.revalidate(session, principal(scope))
            assert caught.value is not None


@pytest.mark.asyncio
async def test_native_migration_rollback_preserves_raw_chat_and_reapply(native_pg):
    factory, _, _ = native_pg
    driver = await asyncpg.connect(user="session_qa", database="session_qa", host="127.0.0.1", port=55439)
    try:
        root = Path(__file__).parents[1] / "migrations"
        await driver.execute((root / "0007_session_context.down.sql").read_text())
        assert await driver.fetchval("SELECT count(*) FROM messages") == 2
        await driver.execute((root / "0007_session_context.sql").read_text())
        async with factory() as session:
            assert await session.scalar(select(SessionContextIndex.id)) is None
            assert await session.get(Message, "fact") is not None
    finally:
        await driver.close()
