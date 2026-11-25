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
from sag_api.enums import MessageRole, MessageStatus
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
            "Summary",  # Unparseable non-JSON verifier output must fail closed for any LLM client
            "no_answer",
            "weak_evidence",
        ),
        (
            "The API rate limit is 100 requests per second [1].",
            {"answerability": "answered"},
            "no_answer",
            "weak_evidence",
        ),
        (
            "The API rate limit is 100 requests per second [1].",
            {"answerability": "answered", "supported": True, "reason": "Rate limit matches evidence."},
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
    """S5-05: Entailment verifier rejects cited abstention, fabricated [1] claims, and unparseable output."""
    source = SimpleNamespace(id="src-1", name="Docs", sag_source_config_id="cfg-1")
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[source]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "persist_answer", AsyncMock(return_value="msg-1"))
    from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision

    monkeypatch.setattr(
        agent_service,
        "route_query_async",
        AsyncMock(
            return_value=UnifiedRouteDecision(
                query="q",
                coarse_intent="KNOWLEDGE",
                question_scope="project",
                memory_scope="project_documents",
                temporal_mode="atemporal",
                answer_shape="fact_answer",
                intent_flags=IntentFlags(needs_retrieval=True),
                domain="general",
                confidence=0.9,
                degraded=False,
            )
        ),
    )

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


@pytest.mark.asyncio
async def test_entailment_verifier_runs_on_real_llm_client_for_session_grounded_scope(monkeypatch):
    """S5-05: Real LLMClient verifies session-grounded [S1] answers and fails closed on unsupported claims."""
    from sag_api.generation import llm as llm_module
    from sag_api.generation.llm import LLMClient

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
                    type=EventType.RUN_COMPLETED,
                    run_id="r-1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"output": "We chose MySQL 8 in that meeting [S1]."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-1", "sequence": 2},
                )

            return _events()

    async def fake_litellm(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "answerability": "answered",
                                "supported": False,
                                "reason": "Session snippet says PostgreSQL 18, not MySQL 8.",
                            }
                        )
                    )
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(llm_module, "_litellm_completion", fake_litellm)
    from sag_api.core.config import Settings

    real_llm = LLMClient(
        Settings(llm_provider="openai", llm_api_key="sk-test", llm_model="gpt-4o-mini")
    )
    plan = AskPlan(
        query="What DB did we choose?",
        user_message_id="u-1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        citations=[],
        route_decision={"is_chitchat": False, "confidence": 0.1, "memory_scope": "session_history"},
        session_citations=[_sess_cit("S1")],
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
    events = [
        ev
        async for ev in agent_service.generate_stream(
            _noop_session_factory,
            plan=plan,
            agent=agent,
            thread_id="t-1",
            engine_manager=SimpleNamespace(),
            llm=real_llm,
            tool_registry=ToolRegistry(),
            runtime=StubRuntime(),
            knowledge_only=True,
            principal=_principal(),
        )
    ]
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == "no_answer"
    assert completed.data["payload"]["no_answer_reason"] == "unsupported_claim"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verifier_payload", "expected_status", "expected_reason"),
    [
        (
            {"answerability": "answered", "supported": False, "reason": "Fabricated MySQL 8"},
            "no_answer",
            "unsupported_claim",
        ),
        (
            {"answerability": "insufficient_evidence", "supported": False, "reason": "Abstain"},
            "no_answer",
            "insufficient_evidence",
        ),
        (
            {"answerability": "answered", "supported": True, "reason": "Matches PostgreSQL 18"},
            "answered",
            None,
        ),
    ],
)
async def test_session_grounded_entailment_enforced_for_non_llmclient_doubles(
    monkeypatch,
    verifier_payload: dict,
    expected_status: str,
    expected_reason: str | None,
):
    """S5-05: Non-LLMClient doubles must run entailment verification on session_grounded_scope=[S1] answers."""
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
                    type=EventType.RUN_COMPLETED,
                    run_id="r-1",
                    sequence=2,
                    timestamp=2,
                    turn=1,
                    version="1",
                    payload={"output": "We chose PostgreSQL 18 in that meeting [S1]."},
                    to_dict=lambda: {"type": "run.completed", "run_id": "r-1", "sequence": 2},
                )

            return _events()

    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value=json.dumps(verifier_payload)))
    plan = AskPlan(
        query="What DB did we choose?",
        user_message_id="u-1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        citations=[],
        route_decision={"is_chitchat": False, "confidence": 0.1, "memory_scope": "session_history"},
        session_citations=[_sess_cit("S1")],
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
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
    assert llm.complete.await_count == 1
    completed = next(e for e in events if e.type == EventType.RUN_COMPLETED.value)
    assert completed.data["payload"]["answer_status"] == expected_status
    assert completed.data["payload"]["no_answer_reason"] == expected_reason


@pytest.mark.asyncio
async def test_verify_grounded_entailment_non_llmclient_unparseable_fails_closed():
    """S5-05: Unparseable non-JSON verifier output from any client fails closed to weak_evidence."""
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="NOT JSON GARBAGE"))
    status, reason = await agent_service._verify_grounded_entailment(
        llm,
        query="What is the API rate limit?",
        answer="The API rate limit is 100 requests per second [1].",
        internal_citations=[{**_doc_cit(1), "claim_level": "claim"}],
        session_citations=[],
    )
    assert (status, reason) == ("no_answer", "weak_evidence")


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_kind", ["internal", "session"])
@pytest.mark.parametrize(
    "bad_llm",
    [
        None,
        SimpleNamespace(
            configured=False,
            complete=AsyncMock(return_value='{"answerability":"answered","supported":true}'),
        ),
        SimpleNamespace(configured=True),
    ],
)
async def test_verify_grounded_entailment_missing_or_unconfigured_llm_fails_closed(
    bad_llm,
    evidence_kind: str,
):
    """S5-05: Missing, unconfigured, or non-callable verifier LLM with non-empty evidence must fail closed."""
    internal = [{**_doc_cit(1), "claim_level": "claim"}] if evidence_kind == "internal" else []
    session = [{**_sess_cit("S1"), "claim_level": "claim"}] if evidence_kind == "session" else []
    status, reason = await agent_service._verify_grounded_entailment(
        bad_llm,
        query="What is configured?",
        answer="Configured value [1]." if evidence_kind == "internal" else "Chosen value [S1].",
        internal_citations=internal,
        session_citations=session,
    )
    assert (status, reason) == ("no_answer", "weak_evidence")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_event", [EventType.RUN_FAILED, EventType.RUN_CANCELLED])
async def test_session_grounded_scope_run_failed_and_cancelled_clears_partial_output(
    monkeypatch,
    terminal_event: EventType,
):
    """S5-10: RUN_FAILED/RUN_CANCELLED in session_grounded_scope must clear partial unverified output."""
    monkeypatch.setattr(agent_service, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_service, "resolve_mcp_specs", AsyncMock(return_value=[]))
    persist = AsyncMock(return_value="msg-err-1")
    monkeypatch.setattr(agent_service, "persist_answer", persist)

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
                    payload={"role": "assistant", "delta": "UNVERIFIED_SESSION_LEAK [S1]"},
                    to_dict=lambda: {"type": "message.delta", "run_id": "r-1", "sequence": 2},
                )
                yield SimpleNamespace(
                    type=terminal_event,
                    run_id="r-1",
                    sequence=3,
                    timestamp=3,
                    turn=1,
                    version="1",
                    payload={"error": {"code": "upstream_error", "message": "aborted"}},
                    to_dict=lambda: {"type": terminal_event.value, "run_id": "r-1", "sequence": 3},
                )

            return _events()

    plan = AskPlan(
        query="What DB did we choose?",
        user_message_id="u-1",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
        citations=[],
        route_decision={"is_chitchat": False, "confidence": 0.1, "memory_scope": "session_history"},
        session_citations=[_sess_cit("S1")],
    )
    agent = SimpleNamespace(id="a-1", name="Tester", persona={}, is_default=True)
    llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value='{"answerability":"answered","supported":true}'),
    )
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
    term_ev = next(e for e in events if e.type == terminal_event.value)
    assert "UNVERIFIED_SESSION_LEAK" not in repr(term_ev.data)
    assert term_ev.data["payload"].get("partial_output", "") == ""
    persist.assert_awaited_once()
    assert persist.await_args.args[2] == ""
    assert persist.await_args.args[3] == []
    expected_status = MessageStatus.CANCELLED if terminal_event == EventType.RUN_CANCELLED else MessageStatus.FAILED
    assert persist.await_args.kwargs["status"] == expected_status


@pytest.mark.asyncio
async def test_hidden_scope_placeholder_excludes_paired_user_turn(monkeypatch):
    """PROBE-8C: Assistant message hidden by HIDDEN_SCOPE_PLACEHOLDER must exclude its paired user turn."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "session_context_qdrant_sync_enabled", False)
    sc_runtime.SESSION_INDEX_CACHE.clear()

    now = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    async with factory() as session:
        src_revoked = Source(id="src-revoked", name="Revoked", sag_source_config_id="cfg-rev")
        src_ok = Source(id="src-ok", name="Allowed", sag_source_config_id="cfg-ok")
        session.add_all([src_revoked, src_ok])
        await session.flush()
        session.add_all(
            [
                SourceProjectMapping(
                    source_id="src-revoked",
                    organization_id="tenant-a",
                    project_id="proj-alpha",
                    state="REVOKED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                    revoked_at=now + timedelta(seconds=1),
                    revoked_by="u",
                    revocation_ref="rev-1",
                ),
                SourceProjectMapping(
                    source_id="src-ok",
                    organization_id="tenant-a",
                    project_id="proj-alpha",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                ),
            ]
        )
        thread = Thread(id="thread-probe8c", agent_id="agent-1", owner_key="owner-1")
        session.add(thread)
        await session.flush()
        session.add_all(
            [
                Message(
                    id="u-hidden",
                    thread_id="thread-probe8c",
                    role=MessageRole.USER,
                    content="What is the REVOKED_USER_SECRET_TOKEN?",
                    created_at=now,
                ),
                Message(
                    id="a-hidden",
                    thread_id="thread-probe8c",
                    role=MessageRole.ASSISTANT,
                    content="The token is REVOKED_ASSISTANT_SECRET [1].",
                    citations=[{"n": 1, "source_id": "src-revoked", "document_id": "doc-rev"}],
                    created_at=now + timedelta(seconds=5),
                ),
                Message(
                    id="u-visible",
                    thread_id="thread-probe8c",
                    role=MessageRole.USER,
                    content="Which port does Allowed service use?",
                    created_at=now + timedelta(seconds=10),
                ),
                Message(
                    id="a-visible",
                    thread_id="thread-probe8c",
                    role=MessageRole.ASSISTANT,
                    content="Allowed service uses port 9000 [1].",
                    citations=[{"n": 1, "source_id": "src-ok", "document_id": "doc-ok"}],
                    created_at=now + timedelta(seconds=15),
                ),
                Message(
                    id="u-ask",
                    thread_id="thread-probe8c",
                    role=MessageRole.USER,
                    content="Recap what we discussed",
                    created_at=now + timedelta(seconds=20),
                ),
            ]
        )
        await session.commit()

        pack = await sc_runtime.prepare_session_context_for_ask(
            session,
            thread_id="thread-probe8c",
            exclude_message_id="u-ask",
            query="Recap what we discussed",
            principal=_principal(frozenset({"proj-alpha"})),
            owner_key="owner-1",
            requested_source_ids=None,
            budget_tokens=1200,
        )
        assert pack is not None
        assert "u-hidden" not in pack.selected_turn_ids
        assert "a-hidden" not in pack.selected_turn_ids
        history_blob = json.dumps(pack.history_messages, ensure_ascii=False)
        assert "REVOKED_USER_SECRET_TOKEN" not in history_blob
        assert "REVOKED_ASSISTANT_SECRET" not in history_blob
        assert "Allowed service uses port 9000" in history_blob or "u-visible" in pack.selected_turn_ids
    await engine.dispose()


def test_ecce_optimizer_populates_snippet_on_session_citations():
    """S5-05: ECCE session_citations must include snippet so _verify_grounded_entailment sees evidence."""
    from sag_api.services.session_context.models import (
        IndexedTurn,
        SelectedEvidenceGroup,
    )
    from sag_api.services.session_context.optimizer import (
        _build_verbatim_session_citation,
        _format_inert_evidence_message,
    )
    from sag_api.services.session_context.substrate import IncrementalSessionIndex

    turn = IndexedTurn(
        turn_id="m-1",
        thread_id="thread-opt",
        ordinal=0,
        role="assistant",
        content="We agreed on PostgreSQL 18 for payments.",
        created_at_ts=1.0,
        created_at_iso="2026-11-01T00:00:00Z",
        token_count=10,
        content_hash="abc123def4567890" * 4,
        episode_id="ep-1",
        window_id="win-1",
        dense_vector=(1.0, 0.0),
        sparse_bm25={1: 1.0},
        content_terms=frozenset({"postgresql", "payments"}),
        entities=(),
        sentences=(),
        epistemic_status="CONFIRMED",
        topic_slot_key="payments.db",
        cited_source_ids=("src-1",),
    )
    verbatim_cit = _build_verbatim_session_citation("S1", [turn])
    assert "PostgreSQL 18" in verbatim_cit["snippet"]

    idx = IncrementalSessionIndex("thread-opt")
    idx.turns = [turn]
    idx.turns_by_id = {"m-1": turn}
    grp = SelectedEvidenceGroup(
        citation_tag="S1",
        primary_turn_id="m-1",
        included_turn_ids=("m-1",),
        closure_reason="primary_hit",
        epistemic_status="CONFIRMED",
        utility_score=1.0,
        redundancy_penalty=0.0,
        coverage_gain=1.0,
        marginal_gain_per_token=0.1,
        token_cost=12,
    )
    _msg, inert_cits = _format_inert_evidence_message(
        [grp], idx, temporal_mode="normal", partial_coverage=False
    )
    assert len(inert_cits) == 1
    assert "PostgreSQL 18" in inert_cits[0]["snippet"]


@pytest.mark.asyncio
async def test_hidden_scope_placeholder_assistant_excludes_paired_user_turn(monkeypatch):
    """PROBE-8C / S4-07: Assistant HIDDEN_SCOPE_PLACEHOLDER must also exclude preceding paired user turn."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "session_context_qdrant_sync_enabled", False)
    sc_runtime.SESSION_INDEX_CACHE.clear()

    now = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    async with factory() as session:
        src_secret = Source(id="src-secret", name="Secret", sag_source_config_id="cfg-secret")
        src_public = Source(id="src-public", name="Public", sag_source_config_id="cfg-public")
        session.add_all([src_secret, src_public])
        await session.flush()
        session.add_all(
            [
                SourceProjectMapping(
                    source_id="src-secret",
                    organization_id="tenant-a",
                    project_id="proj-secret",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                ),
                SourceProjectMapping(
                    source_id="src-public",
                    organization_id="tenant-a",
                    project_id="proj-public",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="u",
                    approval_ref="ref",
                ),
            ]
        )
        thread = Thread(id="thread-probe8c", agent_id="agent-1", owner_key="owner-1")
        session.add(thread)
        await session.flush()
        m1 = Message(
            id="m1",
            thread_id="thread-probe8c",
            role=MessageRole.USER,
            content="What is the SECRET_KEY_EXCHANGE_TOKEN?",
            created_at=now,
        )
        m2 = Message(
            id="m2",
            thread_id="thread-probe8c",
            role=MessageRole.ASSISTANT,
            content="The token is SECRET_VAL [1].",
            citations=[{"n": 1, "source_id": "src-secret", "document_id": "doc-sec"}],
            created_at=now + timedelta(seconds=5),
        )
        m3 = Message(
            id="m3",
            thread_id="thread-probe8c",
            role=MessageRole.USER,
            content="Which port does Public use?",
            created_at=now + timedelta(seconds=10),
        )
        m4 = Message(
            id="m4",
            thread_id="thread-probe8c",
            role=MessageRole.ASSISTANT,
            content="Public uses port 443 [1].",
            citations=[{"n": 1, "source_id": "src-public", "document_id": "doc-pub"}],
            created_at=now + timedelta(seconds=15),
        )
        m5 = Message(
            id="m5",
            thread_id="thread-probe8c",
            role=MessageRole.USER,
            content="Summarize our session decisions.",
            created_at=now + timedelta(seconds=20),
        )
        session.add_all([m1, m2, m3, m4, m5])
        await session.commit()

        # Caller only has access to proj-public (proj-secret is unauthorized -> m2 becomes HIDDEN_SCOPE_PLACEHOLDER)
        pack = await sc_runtime.prepare_session_context_for_ask(
            session,
            thread_id="thread-probe8c",
            exclude_message_id="m5",
            query=m5.content,
            principal=_principal(frozenset({"proj-public"})),
            owner_key="owner-1",
            requested_source_ids=None,
            budget_tokens=1200,
        )
        assert pack is not None
        assert "m1" not in pack.selected_turn_ids
        assert "m2" not in pack.selected_turn_ids
        history_blob = json.dumps(pack.history_messages, ensure_ascii=False)
        assert "SECRET_KEY_EXCHANGE_TOKEN" not in history_blob
        assert "SECRET_VAL" not in history_blob
        assert "443" in history_blob
    await engine.dispose()
