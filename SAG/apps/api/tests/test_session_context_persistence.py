"""Derived schema lifecycle; no raw message deletion during rollback."""

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models.agent import Agent, Message, Thread
from sag_api.db.models.session_context import SessionContextIndex, SessionMessageProvenance
from sag_api.enums import MessageRole


@pytest.mark.asyncio
async def test_derived_rows_restart_and_rollback_preserve_raw_history():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        agent = Agent(name="test", owner_key="owner")
        session.add(agent)
        await session.flush()
        thread = Thread(agent_id=agent.id, owner_key="owner")
        session.add(thread)
        await session.flush()
        message = Message(thread_id=thread.id, role=MessageRole.USER, content="original")
        session.add(message)
        await session.flush()
        ids = (thread.id, message.id)
        session.add(
            SessionContextIndex(
                id="index",
                thread_id=thread.id,
                owner_key="owner",
                scope_fingerprint="s" * 64,
                model_id="pinned",
                generation="g1",
                snapshot_digest="d" * 64,
                state="indexing",
                checkpoint={"after": message.id},
                features={},
            )
        )
        session.add(
            SessionMessageProvenance(
                message_id=message.id,
                owner_key="owner",
                content_hash="h" * 64,
                partition_fingerprint="p" * 64,
                source_ids=[],
                dependencies=[],
                origin="recorded",
            )
        )
        await session.commit()
    async with factory() as session:
        index = await session.get(SessionContextIndex, "index")
        assert index.checkpoint["after"] == ids[1] and index.state == "indexing"
        assert (await session.get(SessionMessageProvenance, ids[1])).origin == "recorded"
        await session.execute(delete(SessionContextIndex))
        await session.execute(delete(SessionMessageProvenance))
        await session.commit()
        assert (await session.get(Message, ids[1])).content == "original"
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: SessionContextIndex.__table__.drop(sync))
        await conn.run_sync(lambda sync: SessionMessageProvenance.__table__.drop(sync))
    async with factory() as session:
        assert (await session.execute(select(Message))).scalar_one().content == "original"
    await engine.dispose()


def _index(thread_id, **changes):
    values = {"id": "index", "thread_id": thread_id, "owner_key": "owner",
              "scope_fingerprint": "s" * 64, "model_id": "pinned", "generation": "g1",
              "snapshot_digest": "d" * 64, "state": "indexing", "checkpoint": {}, "features": {}}
    return SessionContextIndex(**{**values, **changes})


@pytest.fixture
async def derived_schema():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # SQLite disables FK enforcement by default. Cascades below must test
        # the actual FK contract instead of merely inspecting ORM metadata.
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            agent = Agent(name="test", owner_key="owner")
            session.add(agent)
            await session.flush()
            thread = Thread(agent_id=agent.id, owner_key="owner")
            session.add(thread)
            await session.flush()
            message = Message(thread_id=thread.id, role=MessageRole.USER, content="original")
            session.add(message)
            await session.flush()
            session.add(_index(thread.id))
            provenance = SessionMessageProvenance(message_id=message.id, owner_key="owner",
                                                 content_hash="h" * 64, origin="recorded")
            session.add(provenance)
            await session.commit()
            yield session, thread, message, provenance
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_parent", ["message", "thread"])
async def test_derived_foreign_keys_cascade_without_orphan_provenance(derived_schema, deleted_parent):
    session, thread, message, _ = derived_schema
    parent = Message if deleted_parent == "message" else Thread
    parent_id = message.id if deleted_parent == "message" else thread.id
    await session.execute(delete(parent).where(parent.id == parent_id))
    await session.commit()
    assert not (await session.scalars(select(SessionMessageProvenance))).all()
    indexes = (await session.scalars(select(SessionContextIndex))).all()
    # A message deletion needs an application-level stale-generation fence;
    # only deleting the owning thread cascades the whole derived index.
    assert len(indexes) == (1 if deleted_parent == "message" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("constraint", ["scope_model_unique", "index_state", "provenance_origin"])
async def test_derived_schema_rejects_duplicate_scope_or_invalid_states(derived_schema, constraint):
    session, thread, message, provenance = derived_schema
    message_id, thread_id = message.id, thread.id
    if constraint == "scope_model_unique":
        session.add(_index(thread_id, id="duplicate"))
    elif constraint == "index_state":
        session.add(_index(thread_id, id="invalid", scope_fingerprint="t" * 64, state="finished"))
    else:
        provenance.origin = "trusted-by-guess"
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
    assert (await session.get(Message, message_id)).content == "original"
    assert (await session.scalars(select(SessionContextIndex))).one().state == "indexing"
    assert (await session.get(SessionMessageProvenance, message_id)).origin == "recorded"


@pytest.mark.asyncio
async def test_other_model_or_scope_has_independent_derived_index(derived_schema):
    session, thread, _, _ = derived_schema
    session.add_all([_index(thread.id, id="new-model", model_id="different-revision"),
                     _index(thread.id, id="new-scope", scope_fingerprint="t" * 64)])
    await session.commit()
    assert len((await session.scalars(select(SessionContextIndex))).all()) == 3
