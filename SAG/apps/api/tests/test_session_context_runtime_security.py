"""SQLite runtime security contracts, not PostgreSQL shared-lock acceptance."""

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import test_session_context_storage as storage_fixtures
from sqlalchemy import select, text, update
from test_session_context_runtime import Counter, Encoder, principal
from test_session_context_storage import raw

from sag_api.core.config import Settings, settings
from sag_api.core.errors import ServiceUnavailableError
from sag_api.db.models import Agent, Message, SessionMessageProvenance, Source, SourceProjectMapping, Thread
from sag_api.enums import MessageRole
from sag_api.services.agent_domain import filter_messages_for_scope, prepare_ask
from sag_api.services.session_context import runtime
from sag_api.services.session_context.storage import record_provenance
from sag_api.services.session_context.types import ContextContractError

storage_db = storage_fixtures.storage_db
MARKER = {"kind": "context", "name": "session_context"}


async def prepared(factory, scope, *, mode="normal"):
    async with factory() as session:
        session.add(raw("fact", content="I confirmed dashboard BLUE-472."))
        user = raw("current", content="Which dashboard code did I confirm?", ordinal=1)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return await runtime.prepare_context(
            session, key=scope.key, principal=principal(scope), user_message=user, query=user.content,
            mode=mode, encoder=Encoder(), counter=Counter())


async def source_history(factory, scope, *, mode="normal"):
    async with factory() as session:
        session.add(Source(id="source", name="Allowed source", sag_source_config_id="source-config"))
        await session.flush()
        session.add(SourceProjectMapping(source_id="source", organization_id=scope.organization_id,
                    project_id="project", state="CONFIRMED", confirmed_at=datetime.now(UTC),
                    confirmed_by="fixture", approval_ref="fixture-grant"))
        root = raw("root", content="Source-dependent SECRET-472.")
        assistant = raw("derived", role=MessageRole.ASSISTANT, content="Derived SECRET-472.", ordinal=1)
        assistant.steps = [MARKER]
        current = raw("current", content="What was discussed?", ordinal=2)
        session.add_all([root, assistant, current])
        await session.flush()
        await record_provenance(session, root, scope=scope, source_ids=frozenset({"source"}))
        from sag_api.services.session_context.runtime import user_message_ref
        await record_provenance(session, assistant, scope=scope, dependencies=(user_message_ref(root),))
        await session.commit()
        await session.refresh(current)
        return await runtime.prepare_context(
            session, key=scope.key, principal=principal(scope), user_message=current, query=current.content,
            mode=mode, encoder=Encoder(), counter=Counter())


async def revoke_source(factory):
    async with factory() as session:
        await session.execute(update(SourceProjectMapping).where(SourceProjectMapping.source_id == "source").values(
            state="REVOKED", revoked_at=datetime.now(UTC), revoked_by="fixture", revocation_ref="fixture-revoke"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
@pytest.mark.parametrize("mutation", ["edit", "delete"])
async def test_current_query_cannot_change_between_prepare_and_input_lease(storage_db, mode, mutation):
    factory, scope = storage_db
    context = await prepared(factory, scope, mode=mode)
    async with factory() as writer:
        current = await writer.get(Message, "current")
        if mutation == "edit":
            current.content = "Replace the original query."
        else:
            await writer.delete(current)
        await writer.commit()
    with pytest.raises(ContextContractError, match="current_query_changed"):
        async with context.lease(factory, principal(scope)):
            pytest.fail("a changed input must not acquire a dispatch lease")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
async def test_scope_expiration_blocks_dispatch_even_with_prepared_context(storage_db, mode):
    factory, scope = storage_db
    context = await prepared(factory, scope, mode=mode)
    expired = replace(principal(scope), expires_at=int(time.time()) - 1)
    with pytest.raises(ContextContractError, match="principal_expired"):
        async with context.lease(factory, expired):
            pytest.fail("expired principals must not dispatch")


@pytest.mark.asyncio
async def test_publication_capture_retains_transitive_sources_and_current_query(storage_db):
    factory, scope = storage_db
    context = await source_history(factory, scope)
    async with context.lease(factory, principal(scope)):
        async with factory() as writer:
            answer = raw("published", role=MessageRole.ASSISTANT, content="Session answer.", ordinal=3)
            writer.add(answer)
            await writer.flush()
            await context.capture(writer, answer, principal=principal(scope), tool_source_ids=frozenset())
            await writer.commit()
    async with factory() as session:
        provenance = await session.get(SessionMessageProvenance, "published")
        assert provenance.source_ids == ["source"]
        assert {ref["message_id"] for ref in provenance.dependencies} == {"root", "derived", "current"}
        assert provenance.origin == "recorded" and context.lease_session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
async def test_source_revocation_blocks_lease_and_hides_transitive_history(storage_db, mode):
    factory, scope = storage_db
    context = await source_history(factory, scope, mode=mode)
    await revoke_source(factory)
    with pytest.raises(ContextContractError):
        async with context.lease(factory, principal(scope)):
            pytest.fail("revoked Source dependencies must not dispatch")
    async with factory() as session:
        assistant = await session.get(Message, "derived")
        assert assistant.citations == []  # Only transitive provenance carries the Source dependency.
        visible = await filter_messages_for_scope(session, [assistant], principal=principal(scope), owner_key="owner")
        assert "SECRET-472" not in visible[0].content
        assert visible[0].prompt_preview == "" and visible[0].steps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
async def test_output_capture_revalidates_query_after_input_lease(storage_db, mode):
    factory, scope = storage_db
    context = await prepared(factory, scope, mode=mode)
    async with context.lease(factory, principal(scope)):
        # SQLite ignores FOR SHARE; this verifies the independent output-side
        # authority check, not the lock serialization PostgreSQL must provide.
        async with factory() as writer:
            await writer.execute(update(Message).where(Message.id == "current").values(content="Changed meanwhile."))
            await writer.commit()
        async with factory() as writer:
            answer = raw("must-not-publish", role=MessageRole.ASSISTANT, ordinal=3)
            writer.add(answer)
            await writer.flush()
            with pytest.raises(ContextContractError, match="current_query_changed"):
                await context.capture(writer, answer, principal=principal(scope), tool_source_ids=frozenset())
            await writer.rollback()
    async with factory() as session:
        assert await session.get(Message, "must-not-publish") is None
        assert await session.get(SessionMessageProvenance, "must-not-publish") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
async def test_source_revoke_during_sqlite_lease_is_checked_again_before_capture(storage_db, mode):
    factory, scope = storage_db
    context = await source_history(factory, scope, mode=mode)
    async with context.lease(factory, principal(scope)):
        await revoke_source(factory)
        async with factory() as writer:
            answer = raw("must-not-publish", role=MessageRole.ASSISTANT, ordinal=3)
            writer.add(answer)
            await writer.flush()
            with pytest.raises(ContextContractError):
                await context.capture(writer, answer, principal=principal(scope), tool_source_ids=frozenset())
            await writer.rollback()
    async with factory() as session:
        assert await session.get(SessionMessageProvenance, "must-not-publish") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "full_session"])
async def test_expired_principal_at_capture_cannot_publish_after_valid_input_lease(storage_db, mode):
    factory, scope = storage_db
    context = await prepared(factory, scope, mode=mode)
    async with context.lease(factory, principal(scope)):
        async with factory() as writer:
            answer = raw("must-not-publish", role=MessageRole.ASSISTANT, ordinal=3)
            writer.add(answer)
            await writer.flush()
            expired = replace(principal(scope), expires_at=int(time.time()) - 1)
            with pytest.raises(ContextContractError, match="principal_expired"):
                await context.capture(writer, answer, principal=expired, tool_source_ids=frozenset())
            await writer.rollback()
    async with factory() as session:
        assert await session.get(Message, "must-not-publish") is None


@pytest.mark.asyncio
async def test_provenance_mutation_inside_sqlite_lease_blocks_publication(storage_db):
    factory, scope = storage_db
    context = await source_history(factory, scope)
    async with context.lease(factory, principal(scope)):
        async with factory() as writer:
            await writer.execute(update(SessionMessageProvenance).where(
                SessionMessageProvenance.message_id == "root").values(dependencies=[{"bad": "ref"}]))
            await writer.commit()
        async with factory() as writer:
            answer = raw("must-not-publish", role=MessageRole.ASSISTANT, ordinal=3)
            writer.add(answer)
            await writer.flush()
            with pytest.raises(ContextContractError):
                await context.capture(writer, answer, principal=principal(scope), tool_source_ids=frozenset())
            await writer.rollback()


@pytest.mark.asyncio
async def test_missing_derived_schema_hides_marker_answer_and_does_not_poison_outer_transaction(storage_db):
    factory, scope = storage_db
    await source_history(factory, scope)
    async with factory() as session:
        await session.execute(text("DROP TABLE session_message_provenance"))
        await session.commit()
        assistant = await session.get(Message, "derived")
        visible = await filter_messages_for_scope(session, [assistant], principal=principal(scope), owner_key="owner")
        assert "SECRET-472" not in visible[0].content and visible[0].prompt_preview == ""
        assert (await session.scalars(select(Message).where(Message.id == "root"))).one().content


@pytest.mark.asyncio
async def test_marker_answer_requires_explicit_owner_key_even_with_current_source_access(storage_db):
    factory, scope = storage_db
    await source_history(factory, scope)
    async with factory() as session:
        assistant = await session.get(Message, "derived")
        visible = await filter_messages_for_scope(session, [assistant], principal=principal(scope))
        assert "SECRET-472" not in visible[0].content
        wrong_owner = await filter_messages_for_scope(
            session, [assistant], principal=principal(scope), owner_key="other-owner")
        assert "SECRET-472" not in wrong_owner[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["off", "shadow", "on"])
async def test_feature_modes_keep_legacy_or_use_typed_session_lane(storage_db, monkeypatch, flag):
    factory, scope = storage_db
    assert Settings(_env_file=None).session_context_mode == "off"
    monkeypatch.setattr(settings, "session_context_mode", flag)
    monkeypatch.setattr(runtime, "configured_encoder", lambda: Encoder())
    monkeypatch.setattr(runtime, "ModelTokenCounter", lambda *args, **kwargs: Counter())
    async with factory() as session:
        session.add(raw("fact", content="I confirm BLUE-472."))
        await session.commit()
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        plan = await prepare_ask(session, agent=agent, thread=thread, query="Which code?",
                                 principal=principal(scope), owner_key="owner")
        assert (plan.session_context is not None) is (flag == "on")
        assert plan.messages[-1]["content"] == "Which code?"
        if flag == "on":
            assert any(str(message["content"]).startswith("SESSION_EVIDENCE_JSON") for message in plan.messages)
        else:
            assert not any(str(message["content"]).startswith("SESSION_EVIDENCE_JSON") for message in plan.messages)
        if flag == "shadow":
            assert await session.get(SessionMessageProvenance, plan.user_message_id) is not None


@pytest.mark.asyncio
async def test_shadow_optional_model_unavailable_keeps_legacy_without_extra_paid_call(storage_db, monkeypatch):
    factory, scope = storage_db
    monkeypatch.setattr(settings, "session_context_mode", "shadow")

    async def unavailable(*args, **kwargs):
        raise ServiceUnavailableError("Optional session models unavailable.")

    class ProviderMustNotBeCalled:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("shadow must not add a paid model call")

    client = ProviderMustNotBeCalled()
    monkeypatch.setattr(runtime, "prepare_context", unavailable)
    async with factory() as session:
        session.add(raw("fact", content="Legacy history remains available."))
        await session.commit()
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        plan = await prepare_ask(session, agent=agent, thread=thread, query="Which code?",
                                 principal=principal(scope), owner_key="owner", llm=client)
        assert plan.session_context is None
        assert any(message.get("content") == "Legacy history remains available." for message in plan.messages)
        assert client.calls == 0


@pytest.mark.asyncio
async def test_shadow_does_not_swallow_authorization_contract_failure(storage_db, monkeypatch):
    factory, scope = storage_db
    monkeypatch.setattr(settings, "session_context_mode", "shadow")

    async def denied(*args, **kwargs):
        raise ContextContractError("provenance_scope")

    monkeypatch.setattr(runtime, "prepare_context", denied)
    async with factory() as session:
        agent, thread = await session.get(Agent, "agent"), await session.get(Thread, "thread")
        with pytest.raises(ContextContractError, match="provenance_scope"):
            await prepare_ask(session, agent=agent, thread=thread, query="Which code?",
                               principal=principal(scope), owner_key="owner")
