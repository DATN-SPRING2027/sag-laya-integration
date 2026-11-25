"""Regression tests for Milestone P1 Commit 6: no auto-top-3, @source pairing, entailment (S5-05, S5-10, S4-07)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_agent import EventType
from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Message, Source, SourceProjectMapping, Thread
from sag_api.enums import MessageRole
from sag_api.services import agent_service
from sag_api.services.agent_domain import AskPlan
from sag_api.services.session_context import runtime as sc_runtime
from sag_api.tools.builtin import SearchContextTool
from sag_api.tools.registry import ToolRegistry


def _principal(projects: frozenset[str] = frozenset({"project-a"})) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="test-user",
        organization_id="tenant-a",
        allowed_project_ids=projects,
        issuer="test-issuer",
        key_id="test-key",
        token_id="test-token",
        issued_at=1,
        expires_at=2,
        tenant_id="tenant-a",
        allowed_partition_ids=frozenset({"partition-a"}),
    )


def _doc_cit(n: int) -> dict:
    return {
        "n": n,
        "source_id": f"src-{n}",
        "chunk_id": f"chk-{n}",
        "document_id": f"doc-{n}",
        "document_version_id": f"ver-{n}",
        "page_from": 1,
        "page_to": 1,
        "anchor": f"p1-b{n}",
        "heading": f"Heading {n}",
        "snippet": "The API rate limit is 100 requests per second.",
    }


def _sess_cit(ref: str) -> dict:
    return {
        "ref": ref,
        "kind": "session_memory",
        "primary_message_id": f"msg-{ref}",
        "message_ids": [f"msg-{ref}"],
        "turn_ordinals": [1],
        "epistemic_status": "CONFIRMED",
        "cited_source_ids": ["src-1"],
        "snippet": "PostgreSQL 18 was selected for payments.",
    }


@asynccontextmanager
async def _noop_session_factory():
    yield SimpleNamespace()


def test_finalize_citations_no_uncited_top3_fallback():
    """S5-10: Uncited answers must NOT auto-attach top-3 sources or session refs."""
    doc_pool = [_doc_cit(1), _doc_cit(2), _doc_cit(3), _doc_cit(4)]
    text, used = agent_service._finalize_answer_citations("Uncited statement.", doc_pool)
    assert text == "Uncited statement."
    assert used == []

    sess_pool = [_sess_cit("S1"), _sess_cit("S2"), _sess_cit("S3")]
    s_text, s_used, has_explicit = agent_service._finalize_session_citations(
        "Uncited session statement.", sess_pool
    )
    assert s_text == "Uncited session statement."
    assert s_used == []
    assert has_explicit is False


@pytest.mark.asyncio
async def test_session_grounded_scope_requires_explicit_session_marker(monkeypatch):
    """S5-10: session_grounded_scope=True without [Sn] marker must fail closed to no_answer."""
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-1"))

    class StubRuntime:
        async def start(self):
            return None

        def run(self, *_args, **_kwargs):
            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="r-1",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "r-1", "sequence": 1},
                )
                yield SimpleNamespace(
                    type=EventType.MESSAGE_DELTA,
                    run_id="r-1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"role": "assistant", "delta": "We decided to use PostgreSQL 18."},
                    to_dict=lambda: {"type": "message.delta", "run_id": "r-1", "sequence": 2},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="r-1",
                    sequence=3,
                    timestamp=3,
                    turn=1,
                    version="1",
                    payload={"output": "We decided to use PostgreSQL 18."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-1", "sequence": 3},
                )

            return _events()

    plan = AskPlan(
        query="What DB did we choose earlier?",
        user_message_id="u-1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        citations=[],
        route_decision={"is_chitchat": False, "confidence": 0.1, "memory_scope": "session_history"},
        session_citations=[_sess_cit("S1")],
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="Summary"))

    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=agent,
            thread_id="t-1",
            engine_manager=SimpleNamespace(),
            llm=llm,
            tool_registry=ToolRegistry(),
            runtime=StubRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == "no_answer"
    assert completed.data["payload"]["no_answer_reason"] == "weak_evidence"
    assert completed.data["payload"]["citations"] == []


@pytest.mark.asyncio
async def test_requested_source_ids_excludes_paired_user_turns_for_out_of_scope_exchanges(monkeypatch):
    """S4-07: Filtering by @source must exclude paired user turns of excluded assistant turns."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "session_context_qdrant_sync_enabled", False)
    sc_runtime.SESSION_INDEX_CACHE.clear()

    now = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    async with factory() as session:
        src_alpha = Source(id="src-alpha", name="Alpha", sag_source_config_id="cfg-alpha")
        src_beta = Source(id="src-beta", name="Beta", sag_source_config_id="cfg-beta")
        session.add_all([src_alpha, src_beta])
        await session.flush()
        session.add_all(
            [
                SourceProjectMapping(
                    source_id="src-alpha",
                    organization_id="tenant-a",
                    project_id="proj-alpha",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                ),
                SourceProjectMapping(
                    source_id="src-beta",
                    organization_id="tenant-a",
                    project_id="proj-beta",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                ),
            ]
        )
        thread = Thread(id="thread-s407", agent_id="agent-1", owner_key="owner-1")
        session.add(thread)
        await session.flush()
        m1 = Message(
            id="m1",
            thread_id="thread-s407",
            role=MessageRole.USER,
            content="Secret Alpha token format SECRET_ALPHA_QUESTION?",
            created_at=now,
        )
        m2 = Message(
            id="m2",
            thread_id="thread-s407",
            role=MessageRole.ASSISTANT,
            content="Alpha uses SECRET_ALPHA_ANSWER [1].",
            citations=[{"n": 1, "source_id": "src-alpha", "document_id": "doc-a"}],
            created_at=now + timedelta(seconds=5),
        )
        m3 = Message(
            id="m3",
            thread_id="thread-s407",
            role=MessageRole.USER,
            content="What port does Beta listen on?",
            created_at=now + timedelta(seconds=10),
        )
        m4 = Message(
            id="m4",
            thread_id="thread-s407",
            role=MessageRole.ASSISTANT,
            content="Beta listens on port 8080 [1].",
            citations=[{"n": 1, "source_id": "src-beta", "document_id": "doc-b"}],
            created_at=now + timedelta(seconds=15),
        )
        m_ask = Message(
            id="m5",
            thread_id="thread-s407",
            role=MessageRole.USER,
            content="Recap what we discussed for @Beta",
            created_at=now + timedelta(seconds=20),
        )
        session.add_all([m1, m2, m3, m4, m_ask])
        await session.commit()

        pack = await sc_runtime.prepare_session_context_for_ask(
            session,
            thread_id="thread-s407",
            exclude_message_id="m5",
            query=m_ask.content,
            principal=_principal(frozenset({"proj-alpha", "proj-beta"})),
            owner_key="owner-1",
            requested_source_ids=["src-beta"],
            budget_tokens=1200,
        )
        assert pack is not None
        assert "m1" not in pack.selected_turn_ids
        assert "m2" not in pack.selected_turn_ids
        history_blob = json.dumps(pack.history_messages, ensure_ascii=False)
        assert "SECRET_ALPHA_QUESTION" not in history_blob
        assert "SECRET_ALPHA_ANSWER" not in history_blob
        for cit in pack.session_citations:
            assert "m1" not in cit.get("message_ids", [])
            assert "m2" not in cit.get("message_ids", [])
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_answer", "verifier_json", "expected_status", "expected_reason"),
    [
        (
            "Tài liệu [1] không đề cập đến hạn mức giao dịch.",
            {"answerability": "insufficient_evidence", "supported": False, "reason": "abstention"},
            "no_answer",
            "insufficient_evidence",
        ),
        (
            "The API rate limit is 9999 req/s [1].",
            {"answerability": "answered", "supported": False, "reason": "number mismatch"},
            "no_answer",
            "unsupported_claim",
        ),
        (
            "The API rate limit is 100 requests per second [1].",
            "Summary",  # FakeLLM / RecordingLLM compatibility
            "answered",
            None,
        ),
    ],
)
async def test_entailment_verifier_rejects_cited_abstention_and_fabricated_claim(
    monkeypatch,
    model_answer: str,
    verifier_json: dict | str,
    expected_status: str,
    expected_reason: str | None,
):
    """S5-05: Entailment verifier rejects cited abstention & fabricated [1] claims while keeping FakeLLM green."""
    source = SimpleNamespace(id="src-1", name="Docs", sag_source_config_id="cfg-1")
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-1"))
    monkeypatch.setattr(agent_service, "classify_question_scope", AsyncMock(return_value="project"))

    class StubRuntime:
        async def start(self):
            return None

        def run(self, *_args, **_kwargs):
            async def _events():
                yield SimpleNamespace(
                    type=EventType.RUN_STARTED,
                    run_id="r-1",
                    sequence=1,
                    timestamp=1,
                    turn=0,
                    version="1",
                    payload={},
                    to_dict=lambda: {"type": "run.started", "run_id": "r-1", "sequence": 1},
                )
                yield SimpleNamespace(
                    type=EventType.RUN_COMPLETED,
                    run_id="r-1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"output": model_answer},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-1", "sequence": 2},
                )

            return _events()

    reg = ToolRegistry()
    reg.register(SearchContextTool())
    plan = AskPlan(
        query="What is the API rate limit?",
        user_message_id="u-1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        citations=[_doc_cit(1)],
        route_decision={"is_chitchat": False, "confidence": 0.1},
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
    verifier_payload = verifier_json if isinstance(verifier_json, str) else json.dumps(verifier_json)
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value=verifier_payload))

    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=agent,
            thread_id="t-1",
            engine_manager=SimpleNamespace(),
            llm=llm,
            tool_registry=reg,
            runtime=StubRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == expected_status
    assert completed.data["payload"]["no_answer_reason"] == expected_reason
