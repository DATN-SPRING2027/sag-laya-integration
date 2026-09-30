"""ACL-redacted history keeps conversation roles without changing stored evidence."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import Agent, Message, Thread
from sag_api.enums import MessageRole, MessageStatus
from sag_api.schemas.agent import MessageOut
from sag_api.services import agent_domain


def _message(role, content, *, index=0, **kwargs):
    return Message(
        id=f"{index:032x}",
        thread_id="history-thread",
        role=role,
        content=content,
        created_at=datetime(2026, 9, 30, tzinfo=UTC) + timedelta(seconds=index),
        citations=kwargs.pop("citations", []),
        attachments=kwargs.pop("attachments", []),
        steps=kwargs.pop("steps", []),
        prompt_preview=kwargs.pop("prompt_preview", ""),
        status=kwargs.pop("status", MessageStatus.OK),
        error=kwargs.pop("error", None),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_revoked_answer_preserves_roles_in_message_api_and_llm_history(monkeypatch):
    from sag_api.api.v1.agents import messages as message_api

    async def authorized_source_ids(_session, *, principal, requested_source_ids):
        return set(requested_source_ids) & {"allowed-source"}

    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", authorized_source_ids)
    principal = SimpleNamespace(allowed_project_ids=frozenset({"project-1"}))
    revoked = _message(
        MessageRole.ASSISTANT,
        "REVOKED_SECRET_ANSWER",
        index=2,
        citations=[{"source_id": "revoked-source", "chunk_id": "REVOKED_SECRET_CHUNK"}],
        attachments=[{"id": "REVOKED_SECRET_ATTACHMENT"}],
        steps=[{"kind": "thinking", "content": "REVOKED_SECRET_TRACE"}],
        prompt_preview="REVOKED_SECRET_PROMPT",
        status=MessageStatus.FAILED,
        error={"details": "REVOKED_SECRET_ERROR"},
    )
    turns = [
        _message(MessageRole.USER, "first question", index=1),
        revoked,
        _message(MessageRole.USER, "next question", index=3),
        _message(
            MessageRole.ASSISTANT,
            "allowed answer",
            index=4,
            citations=[{"source_id": "allowed-source", "chunk_id": "allowed-chunk"}],
            prompt_preview="assistant: REVOKED_SECRET_ANSWER; user: next question",
        ),
        _message(MessageRole.USER, "current question", index=5),
    ]
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Agent(id="history-agent", name="history", avatar="", persona={}))
            session.add(Thread(id="history-thread", agent_id="history-agent", title="history"))
            session.add_all(turns)
            await session.commit()

            page = await message_api(
                "history-agent", "history-thread", limit=40, cursor=None,
                _user=None, principal=principal, session=session,
            )
            assert [item.role.value for item in page.items] == [
                "user", "assistant", "user", "assistant", "user",
            ]
            redacted = page.items[1]
            assert redacted.id == revoked.id
            assert redacted.thread_id == revoked.thread_id
            assert redacted.created_at == revoked.created_at
            assert redacted.content and redacted.content != revoked.content
            assert redacted.citations == redacted.steps == redacted.attachments == []
            assert redacted.prompt_preview == ""
            assert redacted.error is None
            assert redacted.status == MessageStatus.OK
            assert page.items[3].content == "allowed answer"
            assert page.items[3].citations == turns[3].citations
            assert page.items[3].prompt_preview == ""
            assert "REVOKED_SECRET" not in page.model_dump_json()

            history = await agent_domain._history(
                session, "history-thread", exclude_id=turns[-1].id,
                principal=principal, requested_source_ids=None,
            )
            assert [item["role"] for item in history] == ["user", "assistant", "user", "assistant"]
            assert [item["content"] for item in history] == [
                "first question", redacted.content, "next question", "allowed answer",
            ]
            plan = agent_domain.build_ask_context(
                agent=SimpleNamespace(name="history", persona={}),
                query="current question", history=history,
            )
            assert [item["role"] for item in plan.messages if item["role"] != "system"] == [
                "user", "assistant", "user", "assistant", "user",
            ]
            assert sum(item["content"] == "current question" for item in plan.messages) == 1
            assert not session.dirty
            await session.commit()
            await session.refresh(revoked)
            assert revoked.content == "REVOKED_SECRET_ANSWER"
            assert revoked.citations[0]["chunk_id"] == "REVOKED_SECRET_CHUNK"
            assert revoked.steps[0]["content"] == "REVOKED_SECRET_TRACE"
            assert revoked.attachments == [{"id": "REVOKED_SECRET_ATTACHMENT"}]
            assert revoked.prompt_preview == "REVOKED_SECRET_PROMPT"
            assert revoked.error == {"details": "REVOKED_SECRET_ERROR"}
            assert revoked.status == MessageStatus.FAILED
            await session.refresh(turns[3])
            assert turns[3].prompt_preview == "assistant: REVOKED_SECRET_ANSWER; user: next question"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("citations", [[], [{"source_id": "allowed-source", "chunk_id": "allowed"}]])
async def test_visible_answers_omit_frozen_nested_prompt_snapshot(monkeypatch, citations):
    async def authorized_source_ids(_session, *, principal, requested_source_ids):
        return set(requested_source_ids)

    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", authorized_source_ids)
    original = _message(
        MessageRole.ASSISTANT, "visible answer", citations=citations,
        prompt_preview="assistant: REVOKED_SECRET_ANSWER",
    )
    visible = await agent_domain.filter_messages_for_scope(None, [original], principal=SimpleNamespace())
    assert len(visible) == 1
    assert visible[0].content == original.content
    assert visible[0].citations == citations
    assert visible[0].prompt_preview == ""
    assert inspect(visible[0]).transient
    assert "REVOKED_SECRET" not in MessageOut.model_validate(visible[0]).model_dump_json()
    assert original.prompt_preview == "assistant: REVOKED_SECRET_ANSWER"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_ids", [[], ["other-source"]])
async def test_requested_scope_redacts_otherwise_authorized_history(monkeypatch, requested_ids):
    async def authorized_source_ids(_session, *, principal, requested_source_ids):
        return set(requested_source_ids)

    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", authorized_source_ids)
    original = _message(
        MessageRole.ASSISTANT, "scoped answer",
        citations=[{"source_id": "allowed-source", "chunk_id": "chunk"}],
    )
    result = await agent_domain.filter_messages_for_scope(
        None, [original], principal=SimpleNamespace(), requested_source_ids=requested_ids,
    )
    assert len(result) == 1
    assert result[0].content != original.content
    assert inspect(result[0]).transient
    assert MessageOut.model_validate(result[0]).citations == []
    assert original.content == "scoped answer"


@pytest.mark.asyncio
async def test_assistant_provenance_parsed_once_without_message_id_collisions(monkeypatch):
    original_parser = agent_domain._message_source_ids
    calls = []

    def parse(message):
        calls.append(message)
        return original_parser(message)

    async def authorized_source_ids(_session, *, principal, requested_source_ids):
        assert requested_source_ids == ["allowed-source", "revoked-source"]
        return {"allowed-source"}

    monkeypatch.setattr(agent_domain, "_message_source_ids", parse)
    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", authorized_source_ids)
    # Duplicate transient IDs must not combine the provenance of distinct messages.
    allowed = _message(
        MessageRole.ASSISTANT, "allowed answer",
        citations=[{"source_id": "allowed-source", "chunk_id": "allowed"}],
    )
    revoked = _message(
        MessageRole.ASSISTANT, "revoked answer",
        citations=[{"source_id": "revoked-source", "chunk_id": "revoked"}],
    )
    allowed.id = revoked.id = None
    user = _message(MessageRole.USER, "question")
    result = await agent_domain.filter_messages_for_scope(
        None, [user, allowed, revoked], principal=SimpleNamespace(),
    )
    assert calls == [allowed, revoked]
    assert result[0] is user
    assert result[1].content == allowed.content
    assert result[1].citations == allowed.citations
    assert result[1] is not allowed
    assert result[2].content != revoked.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"citations": [{"chunk_id": "legacy-chunk"}]},
        {"citations": "malformed-citations"},
        {"citations": ["malformed-citation"]},
        {"citations": [{"kind": "internal", "snippet": "unknown source evidence"}]},
        {"citations": {}},
        {"steps": [{"name": "search_context"}]},
        {"steps": [{"name": "get_entity", "details": {"scope": "knowledge", "sources": []}}]},
        {"steps": [{"name": "search_context", "details": {"scope": "knowledge", "sources": [{}]}}]},
        {"steps": "malformed-steps"},
        {"steps": {}},
    ],
)
async def test_ambiguous_or_malformed_knowledge_provenance_is_redacted(monkeypatch, metadata):
    async def no_authorized_sources(_session, **_kwargs):
        return set()

    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", no_authorized_sources)
    original = _message(MessageRole.ASSISTANT, "HIDDEN_EVIDENCE", **metadata)
    visible = await agent_domain.filter_messages_for_scope(None, [original], principal=SimpleNamespace())
    assert len(visible) == 1
    assert visible[0].role == MessageRole.ASSISTANT
    assert "HIDDEN_EVIDENCE" not in MessageOut.model_validate(visible[0]).model_dump_json()
    assert original.content == "HIDDEN_EVIDENCE"
