import time
from types import SimpleNamespace

import pytest
import test_session_context_storage as storage_fixtures
from test_session_context_storage import raw

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models import Message
from sag_api.services.session_context.graph import TextFeatures, text_hash
from sag_api.services.session_context.runtime import prepare_context
from sag_api.services.session_context.storage import invalidate
from sag_api.services.session_context.types import ContextContractError

storage_db = storage_fixtures.storage_db


class Encoder:
    model_id = "pinned-test"
    embedding_dimension = 2

    def featurize_many(self, texts, **kwargs):
        return tuple(self.featurize(t, **kwargs) for t in texts)

    def featurize(self, text, **kwargs):
        return TextFeatures(text_hash(text), self.model_id, (1.0, 0.1))


class Counter:
    def count(self, messages, tools=None):
        return sum(len(str(m)) / 4 for m in messages).__ceil__() + len(str(tools or []))


def principal(scope):
    return VerifiedPrincipal(
        scope.subject,
        scope.organization_id,
        scope.project_ids,
        scope.issuer,
        "key",
        "token",
        int(time.time()) - 1,
        int(scope.expires_at),
    )


@pytest.mark.asyncio
async def test_context_reads_old_fact_beyond_200_and_keeps_current_query_once(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add_all([raw(f"m{i:03}", content="Filler status note.", ordinal=i) for i in range(220)])
        await session.commit()
        oldest = await session.get(Message, "m000")
        oldest.content = "I confirm dashboard code BLUE-472."
        await session.commit()
        user = raw("current", content="Which dashboard code did I confirm?", ordinal=221)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=user.content,
            encoder=Encoder(),
            counter=Counter(),
        )
        assert any(item.span.ref.message_id == "m000" for item in context.resolved)
        messages = context.messages(SimpleNamespace(name="Test", persona={}), user.content)
        assert messages[-1]["content"] == user.content and str(messages).count(user.content) == 1
        assert "BLUE-472" in str(messages)


@pytest.mark.asyncio
async def test_fresh_dispatch_validation_rejects_deleted_original(storage_db):
    factory, scope = storage_db
    async with factory() as session:
        session.add(raw("fact", content="Dashboard code BLUE-472."))
        user = raw("current", content="Dashboard code?", ordinal=1)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=user.content,
            encoder=Encoder(),
            counter=Counter(),
        )
    async with factory() as writer:
        await writer.delete(await writer.get(Message, "fact"))
        await invalidate(writer, "thread")
        await writer.commit()
    async with factory() as reader:
        with pytest.raises(ContextContractError):
            await context.revalidate(reader, principal(scope))


@pytest.mark.asyncio
async def test_typed_session_evidence_answers_with_web_off_and_persists_transitive_provenance(storage_db, monkeypatch):
    import json

    from sag_api.core.config import Settings
    from sag_api.db.models import Agent, SessionMessageProvenance
    from sag_api.generation.llm import LLMClient
    from sag_api.sag import EngineManager
    from sag_api.services.agent_domain import AskPlan, filter_messages_for_scope
    from sag_api.services.agent_service import generate_stream
    from sag_api.services.session_context import model as model_module
    from sag_api.tools.registry import ToolRegistry

    factory, scope = storage_db

    async def complete(settings, **kwargs):
        assert settings.llm_max_retries == 0
        assert kwargs["messages"][-1]["content"] == "Which dashboard code?"
        block = next(
            m["content"] for m in kwargs["messages"] if str(m.get("content", "")).startswith("SESSION_EVIDENCE_JSON")
        )
        data = json.loads(block.split("\n", 1)[1])
        chosen = next(x for x in data if "BLUE-472" in x["text"])
        output = json.dumps({"answer": "BLUE-472", "supports": [{"id": chosen["id"], "quote": "BLUE-472"}]})

        async def chunks():
            yield {
                "choices": [{"delta": {"content": output}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            }

        return chunks()

    monkeypatch.setattr(model_module, "session_completion", complete)
    async with factory() as session:
        session.add(raw("fact", content="I confirm dashboard code BLUE-472."))
        user = raw("current", content="Which dashboard code?", ordinal=1)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        context = await prepare_context(
            session,
            key=scope.key,
            principal=principal(scope),
            user_message=user,
            query=user.content,
            encoder=Encoder(),
            counter=Counter(),
        )
        agent = await session.get(Agent, "agent")
        plan = AskPlan(
            query=user.content,
            messages=context.messages(agent, user.content),
            user_message_id=user.id,
            session_context=context,
        )
    cfg = Settings(_env_file=None, llm_api_key="offline-test", llm_model="Qwen/Qwen3-8B")
    events = [
        event
        async for event in generate_stream(
            factory,
            plan=plan,
            agent=agent,
            thread_id="thread",
            engine_manager=EngineManager(cfg),
            llm=LLMClient(cfg),
            tool_registry=ToolRegistry(),
            knowledge_only=True,
            principal=principal(scope),
        )
    ]
    terminal = next(e.data["payload"] for e in events if e.type == "run.completed")
    assert terminal["output"] == "BLUE-472" and terminal["answer_status"] == "answered"
    assert terminal["session_accounting"]["logical_calls"] == 1
    async with factory() as session:
        assistant = await session.get(Message, terminal["message_id"])
        provenance = await session.get(SessionMessageProvenance, assistant.id)
        assert {ref["message_id"] for ref in provenance.dependencies} == {"fact", "current"}
        visible = await filter_messages_for_scope(session, [assistant], principal=principal(scope), owner_key="owner")
        assert visible[0].content == "BLUE-472"
        await session.delete(await session.get(Message, "fact"))
        await invalidate(session, "thread")
        await session.commit()
        visible = await filter_messages_for_scope(session, [assistant], principal=principal(scope), owner_key="owner")
        assert "BLUE-472" not in visible[0].content
