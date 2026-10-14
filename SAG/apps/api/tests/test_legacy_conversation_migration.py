from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import Agent, Message, Thread
from sag_api.enums import MessageRole
from scripts.migrate_legacy_conversations import migrate, read_mapping


def test_owner_mapping_requires_sha256_owner_keys(tmp_path):
    mapping_file = tmp_path / "owners.json"
    mapping_file.write_text('{"agents":{"agent-1":"wrong"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid row ID or owner key"):
        read_mapping(mapping_file)


@pytest.mark.asyncio
async def test_migration_is_dry_run_by_default_and_copies_only_mapped_attachments(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_key = "a" * 64
    attachment_id = "b" * 32 + ".png"
    legacy_attachments = tmp_path / "attachments"
    legacy_attachments.mkdir()
    (legacy_attachments / attachment_id).write_bytes(b"legacy image")
    async with session_factory() as session:
        session.add(
            Agent(id="agent-1", name="Legacy", avatar="L", persona={}, is_default=False)
        )
        session.add(Thread(id="thread-1", agent_id="agent-1", title="Old thread"))
        session.add(
            Message(
                id="message-1", thread_id="thread-1", role=MessageRole.USER,
                content="old message", attachments=[{"id": attachment_id}],
            )
        )
        await session.commit()

    dry_run = await migrate(
        {"agent-1": owner_key},
        {"thread-1": owner_key},
        apply=False,
        session_factory=session_factory,
        upload_dir=str(tmp_path),
    )
    assert dry_run["status"] == "dry_run"
    assert dry_run["attachments_to_copy"] == 1
    assert not (legacy_attachments / owner_key / attachment_id).exists()

    applied = await migrate(
        {"agent-1": owner_key},
        {"thread-1": owner_key},
        apply=True,
        session_factory=session_factory,
        upload_dir=str(tmp_path),
    )
    assert applied["status"] == "applied"
    assert (legacy_attachments / owner_key / attachment_id).read_bytes() == b"legacy image"
    async with session_factory() as session:
        agent = await session.get(Agent, "agent-1")
        thread = await session.get(Thread, "thread-1")
        assert agent.owner_key == thread.owner_key == owner_key

    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_rejects_thread_owner_that_differs_from_private_agent(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(Agent(id="agent-1", owner_key="a" * 64, name="Private", avatar="P", persona={}))
        session.add(Thread(id="thread-1", agent_id="agent-1", title="Old thread"))
        await session.commit()

    with pytest.raises(ValueError, match="does not match its private agent"):
        await migrate(
            {},
            {"thread-1": "b" * 64},
            apply=True,
            session_factory=session_factory,
            upload_dir=str(tmp_path),
        )

    async with session_factory() as session:
        thread = await session.get(Thread, "thread-1")
        assert thread.owner_key is None
    await engine.dispose()
