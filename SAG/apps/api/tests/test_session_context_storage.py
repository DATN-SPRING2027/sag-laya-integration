"""Actual ORM storage boundaries; SQLite is not PostgreSQL race acceptance."""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.errors import NotFoundError
from sag_api.db.base import Base
from sag_api.db.models.agent import Agent, Message, Thread
from sag_api.db.models.session_context import SessionContextIndex, SessionMessageProvenance
from sag_api.enums import MessageRole
from sag_api.services.session_context.budget import BudgetLimits, BudgetState
from sag_api.services.session_context.scope import eligible_message_ids
from sag_api.services.session_context.storage import invalidate, read_records, record_provenance
from sag_api.services.session_context.types import ContextContractError, SessionKey, SessionScope


def work():
    return BudgetState(BudgetLimits(12288, 4096, 1, 12288, 2_000_000, time.monotonic() + 30))


@pytest.fixture
async def storage_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        agent = Agent(id="agent", owner_key="owner", name="Storage test")
        session.add(agent)
        await session.flush()
        session.add_all([Thread(id="thread", agent_id=agent.id, owner_key="owner"),
                         Thread(id="other-thread", agent_id=agent.id, owner_key="other-owner")])
        await session.commit()
    scope = SessionScope(SessionKey("owner", "agent", "thread"), "continuum-sag-session", "subject", "org", "",
                         frozenset({"project"}), frozenset(), frozenset({"source"}), None, time.time() + 3600)
    try:
        yield factory, scope
    finally:
        await engine.dispose()


def raw(name, *, role=MessageRole.USER, content="I chose B.", thread_id="thread", ordinal=0):
    return Message(id=name, thread_id=thread_id, role=role, content=content,
                   created_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=ordinal))


@pytest.mark.asyncio
async def test_keyset_reads_all_old_history_and_excludes_only_current_message(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        # Equal timestamps also exercise the ID keyset tie-breaker across pages.
        session.add_all([raw(f"m{i:031d}") for i in range(260)])
        session.add_all([raw("legacy-assistant", role=MessageRole.ASSISTANT),
                         raw("private", thread_id="other-thread", content="must not be read")])
        await session.commit()
    async with factory() as session:
        records = await read_records(session, scope=scope, budget=work(), exclude_id=f"m{259:031d}")
    assert len(records) == 260 and f"m{0:031d}" in records
    assert f"m{259:031d}" not in records and "private" not in records
    assert records["legacy-assistant"].provenance == "unknown"
    assert len(eligible_message_ids(scope, records)) == 259


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer,projects,allowed", [
    ("continuum-sag-session", frozenset({"project"}), True),
    ("unknown-issuer", frozenset({"project"}), False),
    ("continuum-sag-session", frozenset(), False),
    ("continuum-sag-session", frozenset({"project", "second"}), False),
])
async def test_legacy_user_requires_verified_single_project_session_boundary(storage_db, issuer, projects, allowed):
    factory, scope = storage_db
    scope = replace(scope, issuer=issuer, project_ids=projects)
    async with factory() as session:
        session.add_all([raw("legacy-user"), raw("legacy-assistant", role=MessageRole.ASSISTANT)])
        await session.commit()
        records = await read_records(session, scope=scope, budget=work())
    assert ("legacy-user" in eligible_message_ids(scope, records)) is allowed
    assert records["legacy-assistant"].provenance == "unknown"
    assert "legacy-assistant" not in eligible_message_ids(scope, records)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["other-owner", None])
async def test_owner_mismatch_or_unmapped_legacy_thread_is_not_readable(storage_db, owner):
    factory, scope = storage_db
    async with factory() as session:
        await session.execute(update(Thread).where(Thread.id == "thread").values(owner_key=owner))
        session.add(raw("hidden"))
        await session.commit()
        with pytest.raises(NotFoundError):
            await read_records(session, scope=scope, budget=work())


@pytest.mark.asyncio
async def test_recorded_assistant_retains_source_and_message_dependencies_across_restart(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        user = raw("user")
        assistant = raw("assistant", role=MessageRole.ASSISTANT, ordinal=1)
        session.add_all([user, assistant])
        await session.flush()
        await record_provenance(session, user, scope=scope)
        records = await read_records(session, scope=scope, budget=work())
        reference = records["user"].reference
        await record_provenance(session, assistant, scope=scope, dependencies=(reference,),
                                source_ids=frozenset({"source"}))
        await session.commit()
    async with factory() as session:
        records = await read_records(session, scope=scope, budget=work())
        assert records["assistant"].dependencies == (reference,)
        assert records["assistant"].source_ids == frozenset({"source"})
        assert eligible_message_ids(scope, records) == frozenset({"user", "assistant"})
        revoked = replace(scope, source_ids=frozenset())
        assert eligible_message_ids(revoked, records) == frozenset({"user"})
        changed_project = replace(scope, project_ids=frozenset({"other-project"}))
        changed_records = await read_records(session, scope=changed_project, budget=work())
        assert not eligible_message_ids(changed_project, changed_records)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["content", "owner", "hash", "dependencies"])
async def test_changed_or_corrupt_recorded_metadata_is_not_reauthorized_as_legacy(storage_db, corruption):
    factory, scope = storage_db
    async with factory() as session:
        message = raw("recorded")
        session.add(message)
        await session.flush()
        await record_provenance(session, message, scope=scope)
        await session.commit()
        if corruption == "content":
            await session.execute(update(Message).where(Message.id == message.id).values(content="Changed."))
        else:
            values = {"owner_key": "other-owner"} if corruption == "owner" else (
                {"content_hash": "0" * 64} if corruption == "hash" else {"dependencies": [{"bad": "ref"}]})
            await session.execute(update(SessionMessageProvenance).where(
                SessionMessageProvenance.message_id == message.id).values(**values))
        await session.commit()
    async with factory() as session:
        records = await read_records(session, scope=scope, budget=work())
    assert records["recorded"].provenance == "unknown"
    assert "recorded" not in eligible_message_ids(scope, records)


@pytest.mark.asyncio
async def test_deleting_dependency_blocks_assistant_even_when_cached_feature_still_exists(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        user, assistant = raw("user"), raw("assistant", role=MessageRole.ASSISTANT, ordinal=1)
        session.add_all([user, assistant])
        await session.flush()
        await record_provenance(session, user, scope=scope)
        reference = (await read_records(session, scope=scope, budget=work()))["user"].reference
        await record_provenance(session, assistant, scope=scope, dependencies=(reference,))
        await session.commit()
        await session.execute(delete(Message).where(Message.id == "user"))
        await session.commit()
    async with factory() as session:
        records = await read_records(session, scope=scope, budget=work())
    assert set(records) == {"assistant"}
    assert records["assistant"].dependencies == (reference,)
    assert not eligible_message_ids(scope, records)


@pytest.mark.asyncio
async def test_invalidate_changes_generation_and_discards_stale_features_only_for_target_thread(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        for name, thread in (("target", scope.key.thread_id), ("other", "other-thread")):
            session.add(SessionContextIndex(id=name, thread_id=thread, owner_key="owner",
                        scope_fingerprint="s" * 64, model_id="pinned", generation="before", snapshot_digest="d" * 64,
                        state="ready", checkpoint={"done": 10}, features={"deleted-message": {"vector": [1.0]}}))
        await session.commit()
        await invalidate(session, scope.key.thread_id)
        await session.commit()
    async with factory() as session:
        rows = {row.id: row for row in await session.scalars(select(SessionContextIndex))}
        assert rows["target"].generation != "before" and rows["target"].state == "stale"
        assert rows["target"].features == {}
        assert rows["other"].generation == "before" and rows["other"].state == "ready"
        assert rows["other"].features


@pytest.mark.asyncio
async def test_provenance_writer_cannot_stamp_a_message_from_another_thread(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        outsider = raw("outside", thread_id="other-thread")
        session.add(outsider)
        await session.flush()
        with pytest.raises((ContextContractError, NotFoundError)):
            await record_provenance(session, outsider, scope=scope)


@pytest.mark.asyncio
async def test_revalidation_refreshes_held_orm_content_after_another_session_mutates_it(storage_db):
    factory, scope = storage_db
    async with factory() as writer:
        message = raw("recorded", content="old authorized text")
        writer.add(message)
        await writer.flush()
        await record_provenance(writer, message, scope=scope)
        await writer.commit()
    async with factory() as reader:
        # Keep strong references: SQLAlchemy's identity map otherwise uses weak
        # references and a test might accidentally hide the stale-row problem.
        held_message = await reader.get(Message, "recorded")
        held_provenance = await reader.get(SessionMessageProvenance, "recorded")
        assert held_message.content == "old authorized text" and held_provenance.origin == "recorded"
        async with factory() as writer:
            await writer.execute(update(Message).where(Message.id == "recorded").values(content="new changed text"))
            await writer.commit()
        records = await read_records(reader, scope=scope, budget=work())
        assert records["recorded"].content == "new changed text"
        assert records["recorded"].provenance == "unknown"
        assert "recorded" not in eligible_message_ids(scope, records)


@pytest.mark.asyncio
async def test_revalidation_does_not_trust_cached_thread_owner_after_reassignment(storage_db):
    factory, scope = storage_db
    async with factory() as reader:
        held_thread = await reader.get(Thread, scope.key.thread_id)
        assert held_thread.owner_key == scope.key.owner_key
        async with factory() as writer:
            await writer.execute(update(Thread).where(Thread.id == scope.key.thread_id).values(owner_key="other-owner"))
            await writer.commit()
        with pytest.raises(NotFoundError):
            await read_records(reader, scope=scope, budget=work())
