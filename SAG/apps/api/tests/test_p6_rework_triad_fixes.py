"""TDD regression tests for P6 Triad Rework (all 11 Round 1 findings)."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_agent import Agent as RuntimeAgent
from sag_agent import EventType
from sag_api.api.v1 import agents as agents_api
from sag_api.api.v1 import openai as openai_api
from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    Agent,
    CanonicalBlock,
    Document,
    DocumentVersion,
    Job,
    Message,
    SearchUnit,
    Source,
    Thread,
)
from sag_api.db.models.routing_rag import TreeManifest
from sag_api.enums import DocumentStatus, JobStatus, JobType, MessageRole, MessageStatus
from sag_api.jobs import tasks
from sag_api.parsing.canonical import _html_table_to_markdown, _parse_html_span
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services import (
    agent_service,
    answer_actions_service,
    decision_router,
    query_analysis,
    retrieval_service,
    search_index_service,
    search_unit_retrieval_service,
    tree_publish_service,
    tree_rollback_service,
)
from sag_api.services.agent_domain import AskPlan
from sag_api.services.agent_service import AgentStreamEvent
from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision
from sag_api.services.session_context import IncrementalSessionIndex
from sag_api.services.session_context import substrate as substrate_mod
from sag_api.services.tree_publish_service import PublishVerificationError, _checksum_manifest


def _principal(tenant_id: str = "tenant-rework") -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-rework",
        organization_id=tenant_id,
        allowed_project_ids=frozenset({"proj-rework"}),
        issuer="test-issuer",
        key_id="kid-rework",
        token_id="tok-rework",
        issued_at=1,
        expires_at=9999999999,
        tenant_id=tenant_id,
        allowed_partition_ids=frozenset({"part-rework"}),
    )


def _reassemble_sse(body_text: str) -> str:
    assembled: list[str] = []
    for line in body_text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            obj = json.loads(line[6:])
            delta_content = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta_content:
                assembled.append(delta_content)
    return "".join(assembled)


@pytest_asyncio.fixture
async def rework_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# Item 1 (P0): Fenced code block body preservation & forced_final default check
# ---------------------------------------------------------------------------


def test_p6_rework_01_fenced_code_blocks_preserved_and_forced_final_default_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cits = [
        {
            "n": 1,
            "chunk_id": "c1",
            "source_id": "s1",
            "document_id": "d1",
            "document_version_id": "v1",
            "page_from": 1,
            "page_to": 1,
            "anchor": "p1",
        }
    ]
    # Closed triple-backtick fence
    out_bt, used_bt = agent_service._finalize_answer_citations(
        "Before [1]\n```\n[99]\n```\nAfter [99].",
        cits,
    )
    assert out_bt == "Before [1]\n```\n[99]\n```\nAfter."
    assert [c["n"] for c in used_bt] == [1]

    # Closed tilde fence with language info string
    out_tilde, _ = agent_service._finalize_answer_citations(
        "Before [1]\n~~~python\narr = x[99]\n~~~\nAfter [99].",
        cits,
    )
    assert out_tilde == "Before [1]\n~~~python\narr = x[99]\n~~~\nAfter."

    # Multi-line fenced code block with valid and invalid brackets inside code
    out_multi, used_multi = agent_service._finalize_answer_citations(
        "See example [1]:\n```python\ndef foo(xs):\n    return xs[1] + xs[99]\n```\nDone [99].",
        cits,
    )
    assert out_multi == "See example [1]:\n```python\ndef foo(xs):\n    return xs[1] + xs[99]\n```\nDone."
    assert [c["n"] for c in used_multi] == [1]

    # Unclosed fenced code block preserves body verbatim and leaves prose before it filtered
    out_unclosed, _ = agent_service._finalize_answer_citations(
        "Intro [1] bad [99]\n```python\nval = items[99]\n",
        cits,
    )
    assert out_unclosed == "Intro [1] bad \n```python\nval = items[99]"

    # Session citation finalizer preserves fenced code block body too
    session_cits = [{"ref": "S1", "turn_id": "t1", "excerpt": "prev"}]
    out_sess, used_sess, has_exp = agent_service._finalize_session_citations(
        "Note [S1]\n```\n[S99]\n```\nBad [S99].",
        session_cits,
    )
    assert out_sess == "Note [S1]\n```\n[S99]\n```\nBad."
    assert has_exp is True
    assert [c["ref"] for c in used_sess] == ["S1"]

    # RuntimeAgent.final_instructions default comparison must work even without prefix match
    default_final = str(RuntimeAgent.__dataclass_fields__["final_instructions"].default).strip()
    monkeypatch.setattr(agent_service, "_FORCED_FINAL_PREFIX", "__NON_MATCHING_SENTINEL__")
    msg = {"role": "user", "content": default_final}
    assert agent_service._is_forced_final_message(msg, msg) is True


# ---------------------------------------------------------------------------
# Item 2 (VETO-1): Linear-time (< 10ms) ReDoS bounds & 1-char backtick streaming
# ---------------------------------------------------------------------------


def test_p6_rework_02_redos_all_regexes_under_10ms_and_backtick_streaming() -> None:
    def _measure_ms(fn) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            fn()
            best = min(best, (time.perf_counter() - t0) * 1000.0)
        return best

    # 1) query_analysis.py:213 decimal/version wildcard check
    adv_num = "1." + "x" * 2000 + "_"
    assert _measure_ms(lambda: query_analysis._is_valid_path_term(adv_num)) < 10.0
    assert _measure_ms(lambda: query_analysis.analyze_query(adv_num)) < 10.0

    # 2) query_analysis.py:86-93 _PATH_RE on hyphenated non-path
    adv_path = "a-b_" * 500 + "!"
    assert _measure_ms(lambda: query_analysis._PATH_RE.findall(adv_path)) < 10.0
    assert _measure_ms(lambda: query_analysis.analyze_query(adv_path)) < 10.0

    # 3) openai.py complete citation bracket & tail scanner
    adv_tab_bracket = "\t" * 1000 + "[" + "9" * 1000 + "!"
    assert _measure_ms(lambda: openai_api._COMPLETE_CITATION_BRACKET_RE.findall(adv_tab_bracket)) < 10.0
    assert _measure_ms(lambda: openai_api._split_unbuffered_citation_tail(adv_tab_bracket)) < 10.0

    # 4) openai.py incomplete citation tail on spaces and repeated commas
    adv_spaces_tail = " " * 1000 + "[" + " " * 1000 + "!"
    adv_comma_tail = "[1" + ", " * 500 + "!"
    assert _measure_ms(lambda: openai_api._split_unbuffered_citation_tail(adv_spaces_tail)) < 10.0
    assert _measure_ms(lambda: openai_api._split_unbuffered_citation_tail(adv_comma_tail)) < 10.0

    # 5) openai.py:223 & agent_service.py:397/452 space-before-punctuation cleanup
    adv_spaces = " " * 2000 + "a"
    assert _measure_ms(lambda: openai_api._compute_unstreamed_suffix(adv_spaces, "x")) < 10.0
    assert _measure_ms(lambda: agent_service._finalize_answer_citations(adv_spaces, [])) < 10.0

    # Functional preservation on path/identifier hygiene
    tech_feats = query_analysis.extract_query_features(
        "Inspect src/app.py /etc/nginx/conf.d config.yaml v1/auth-service.py my-service.yaml"
    )
    for expected_path in ("src/app.py", "/etc/nginx/conf.d", "config.yaml", "v1/auth-service.py", "my-service.yaml"):
        assert expected_path in tech_feats.path_terms

    non_path_feats = query_analysis.extract_query_features(
        "Tại TP.HCM dùng Node.js bản 1.2.x giá 100.5triệu tăng 12.5% trong Q1"
    )
    assert non_path_feats.path_terms == ()
    assert non_path_feats.identifier_terms == ()

    # 1-char streaming of inline code `[1, 2]` must flush as soon as closing backtick arrives
    pending = ""
    flushed_steps: list[str] = []
    stream_src = "Use `[1, 2]` now"
    for ch in stream_src:
        flushed, pending = openai_api._split_unbuffered_citation_tail(pending + ch)
        if flushed:
            flushed_steps.append(flushed)
    assert "`[1, 2]`" in "".join(flushed_steps)
    assert pending == ""

    # Unclosed lone backtick on a long non-citation span must NOT stall forever
    long_unclosed = "Use `oops " + "x" * 100 + " done"
    flushed_long, pending_long = openai_api._split_unbuffered_citation_tail(long_unclosed)
    assert "oops" in flushed_long
    assert pending_long == ""


# ---------------------------------------------------------------------------
# Item 3 (P1): OpenAI mid-stream TOOL_STARTED Turn-2 must not LCP-chop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_03_openai_midstream_tool_turn2_does_not_lcp_chop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct helper check: shared opening word "The " across unrelated turns must NOT chop "The "
    suffix = openai_api._compute_unstreamed_suffix(
        "The policy says",
        "The system requires MFA [1].",
    )
    assert suffix == "\n\nThe system requires MFA [1]."
    assert "The policy says" + suffix == "The policy says\n\nThe system requires MFA [1]."

    agent = SimpleNamespace(id="agent-rw", name="ReworkBot", persona={}, is_default=False)
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **_kwargs: AskPlan(
            query="q",
            user_message_id="u-1",
            messages=[{"role": "user", "content": "q"}],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "KNOWLEDGE", "degraded": False},
        ),
    )

    async def _run_sse(events_list: list[AgentStreamEvent]) -> str:
        async def _fake_stream(*_args, **_kwargs):
            for ev in events_list:
                yield ev

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", _fake_stream)
        req = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="q")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-rw",
            body=req,
            _user=SimpleNamespace(name="User"),
            owner_key="owner-1",
            principal=_principal(),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        chunks = [chunk async for chunk in resp.body_iterator]
        body_text = "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)
        return _reassemble_sse(body_text)

    # Case A: Shared prefix word "The " across Turn-1 and Turn-2 after TOOL_STARTED
    text_shared = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "The policy says"}}),
            AgentStreamEvent(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "search_context"}}),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "The system requires MFA [1].", "citations": []}},
            ),
        ]
    )
    assert text_shared == "The policy says\n\nThe system requires MFA [1]."

    # Case B: Disjoint Turn-1 & Turn-2 after TOOL_STARTED gets clean separator
    text_disjoint = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Let me check."}}),
            AgentStreamEvent(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "search_context"}}),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "The answer is 42 [1].", "citations": []}},
            ),
        ]
    )
    assert text_disjoint == "Let me check.\n\nThe answer is 42 [1]."

    # Case C: Canonical genuinely extends streamed prefix -> still no duplication
    text_prefix = await _run_sse(
        [
            AgentStreamEvent(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            AgentStreamEvent(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Let me check. "}}),
            AgentStreamEvent(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "search_context"}}),
            AgentStreamEvent(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Let me check. Final grounded answer [1].", "citations": []}},
            ),
        ]
    )
    assert text_prefix == "Let me check. Final grounded answer [1]."


# ---------------------------------------------------------------------------
# Item 4 (VETO-2): Zero bare `except TypeError:` mock-compat bypasses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_04_no_mock_compat_except_typeerror_around_load_ready_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src_path = Path(search_unit_retrieval_service.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    offending_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            has_load_call = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_load_current_ready_versions"
                for stmt in node.body
                for child in ast.walk(stmt)
            )
            if not has_load_call:
                continue
            for handler in node.handlers:
                if isinstance(handler.type, ast.Name) and handler.type.id == "TypeError":
                    offending_lines.append(handler.lineno)
    assert offending_lines == [], (
        f"Bare except TypeError around _load_current_ready_versions at lines {offending_lines}"
    )

    # Behavioral proof: genuine internal TypeError inside _load_current_ready_versions MUST propagate
    @asynccontextmanager
    async def _noop_session():
        yield SimpleNamespace()

    monkeypatch.setattr(search_unit_retrieval_service, "SessionLocal", _noop_session)
    monkeypatch.setattr(
        search_unit_retrieval_service,
        "_load_current_ready_versions",
        AsyncMock(side_effect=TypeError("genuine internal bug")),
    )
    src = SimpleNamespace(id="s1", name="Src", sag_source_config_id="cfg-1")
    with pytest.raises(TypeError, match="genuine internal bug"):
        await search_unit_retrieval_service.retrieve_search_unit_sections(
            SimpleNamespace(),
            [src],
            "test query",
            principal=_principal(),
        )


# ---------------------------------------------------------------------------
# Item 5 (P1): Live HTTP /ask & /regenerate forward web_enabled into route_query_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_05_http_ask_and_regenerate_forward_web_enabled_to_router(
    rework_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(settings, "session_context_qdrant_sync_enabled", False)
    monkeypatch.setattr(settings, "decision_provider", "llm")

    captured_routes: list[dict] = []

    def _dummy_decision(query: str) -> UnifiedRouteDecision:
        return UnifiedRouteDecision(
            query=query,
            coarse_intent="KNOWLEDGE",
            question_scope="project",
            memory_scope="project_documents",
            temporal_mode="atemporal",
            answer_shape="fact_answer",
            intent_flags=IntentFlags(needs_retrieval=True, needs_web=True),
            domain="general",
            confidence=0.9,
        )

    async def fake_route(query: str, **kwargs):
        captured_routes.append({"query": query, **kwargs})
        return _dummy_decision(query)

    from sag_api.services.session_context import runtime as sc_runtime

    sc_runtime.SESSION_INDEX_CACHE.clear()
    monkeypatch.setattr(sc_runtime, "route_query_async", fake_route)
    monkeypatch.setattr(decision_router, "route_query_async", fake_route)
    monkeypatch.setattr(agents_api.actions_svc, "claim_thread_generation", lambda *_a, **_k: None)
    monkeypatch.setattr(agents_api.actions_svc, "release_thread_generation", lambda *_a, **_k: None)

    stub_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value='{"turns": []}'),
    )
    principal = _principal()

    async with rework_db() as session:
        ag = Agent(id="ag-http-web", owner_key="owner-1", name="WebBot", persona={})
        session.add(ag)
        await session.flush()
        th = Thread(id="th-http-web", agent_id=ag.id, owner_key="owner-1", title="T")
        session.add(th)
        await session.flush()
        now = datetime.now(UTC)
        m_u1 = Message(
            id="m-u1",
            thread_id=th.id,
            role=MessageRole.USER,
            content="Prior topic on PostgreSQL.",
            status=MessageStatus.OK,
            created_at=now,
        )
        m_a1 = Message(
            id="m-a1",
            thread_id=th.id,
            role=MessageRole.ASSISTANT,
            content="Noted PostgreSQL.",
            reply_to_message_id="m-u1",
            status=MessageStatus.OK,
            created_at=datetime.fromtimestamp(now.timestamp() + 1, tz=UTC),
        )
        m_u2 = Message(
            id="m-u2",
            thread_id=th.id,
            role=MessageRole.USER,
            content="Latest news today?",
            status=MessageStatus.OK,
            created_at=datetime.fromtimestamp(now.timestamp() + 2, tz=UTC),
        )
        m_a2 = Message(
            id="m-a2",
            thread_id=th.id,
            role=MessageRole.ASSISTANT,
            content="Old answer.",
            reply_to_message_id="m-u2",
            status=MessageStatus.OK,
            created_at=datetime.fromtimestamp(now.timestamp() + 3, tz=UTC),
        )
        session.add_all([m_u1, m_a1, m_u2, m_a2])
        await session.commit()

        # 1) /ask endpoint forwards web_enabled=True into prepare_ask -> route_query_async
        captured_routes.clear()
        ask_body = agents_api.AskRequest(query="Latest news today?", web_enabled=True)
        await agents_api.ask(
            agent_id=ag.id,
            thread_id=th.id,
            body=ask_body,
            owner_key="owner-1",
            current_user=SimpleNamespace(name="User"),
            principal=principal,
            session=session,
            engine_manager=SimpleNamespace(),
            llm=stub_llm,
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        assert captured_routes, "route_query_async was not invoked during /ask"
        assert captured_routes[-1]["web_enabled"] is True

        # 2) /regenerate endpoint forwards web_enabled=True into prepare_regenerate -> route_query_async
        captured_routes.clear()

        class FakeRequest:
            async def body(self) -> bytes:
                return json.dumps({"web_enabled": True}).encode("utf-8")

        await agents_api.regenerate_message(
            agent_id=ag.id,
            thread_id=th.id,
            message_id=m_a2.id,
            request=FakeRequest(),
            owner_key="owner-1",
            current_user=SimpleNamespace(name="User"),
            principal=principal,
            session=session,
            engine_manager=SimpleNamespace(),
            llm=stub_llm,
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        assert captured_routes, "route_query_async was not invoked during /regenerate"
        assert captured_routes[-1]["web_enabled"] is True


# ---------------------------------------------------------------------------
# Item 6 (P1): superseded_prune_failed reconcile job actually prunes v1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_06_superseded_prune_failed_reconcile_prunes_failed_version_only(
    rework_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sag_api.services import dedup_and_temporal_service as temporal

    now = datetime.now(UTC)
    qdrant_store: dict[str, dict] = {}
    fail_v1_delete = True

    def q_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        path = request.url.path
        if request.method == "GET" and path == "/collections/search_units_proj-sup":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 4}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        },
                        "payload_schema": {},
                    }
                },
            )
        if request.method == "GET" and "/points/" in path:
            return httpx.Response(404, json={"status": {"error": "not found"}})
        if request.method == "PUT" and path.endswith("/points"):
            for pt in body.get("points", []):
                qdrant_store[str(pt["id"])] = dict(pt)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path.endswith("/points/count"):
            must = (body.get("filter") or {}).get("must") or []
            if must and must[0].get("key") == "document_version_id":
                vid = must[0].get("match", {}).get("value")
                cnt = sum(1 for pt in qdrant_store.values() if pt.get("payload", {}).get("document_version_id") == vid)
                return httpx.Response(200, json={"result": {"count": cnt}})
            return httpx.Response(200, json={"result": {"count": len(qdrant_store)}})
        if path.endswith("/points/scroll"):
            must = (body.get("filter") or {}).get("must") or []
            pts = list(qdrant_store.values())
            if must and must[0].get("key") == "document_version_id":
                vid = must[0].get("match", {}).get("value")
                pts = [pt for pt in pts if pt.get("payload", {}).get("document_version_id") == vid]
            return httpx.Response(200, json={"result": {"points": pts, "next_page_offset": None}})
        if path.endswith("/points/delete"):
            if "filter" in body:
                must = (body.get("filter") or {}).get("must") or []
                target_vids = must[0].get("match", {}).get("any", []) if must else []
                if fail_v1_delete and "v1" in target_vids:
                    return httpx.Response(503, text="qdrant temporary 503")
                for pid in list(qdrant_store.keys()):
                    if qdrant_store[pid].get("payload", {}).get("document_version_id") in target_vids:
                        del qdrant_store[pid]
                return httpx.Response(200, json={"result": {"status": "completed"}})
            for pid in body.get("points", []):
                qdrant_store.pop(str(pid), None)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    class Fixed4DEmbedder:
        model = "bge-large-en-v1.5"
        base_url = "https://api.siliconflow.com/v1"
        dimensions = 4

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            return [[0.25, 0.25, 0.25, 0.25] for _ in texts]

    async with rework_db() as session:
        session.add(Source(id="src-sup", name="S", sag_source_config_id="cfg-sup"))
        session.add_all(
            [
                Document(
                    id="doc-main",
                    source_id="src-sup",
                    tenant_id="t1",
                    project_id="proj-sup",
                    filename="policy.md",
                    storage_path="policy.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc-other",
                    source_id="src-sup",
                    tenant_id="t1",
                    project_id="proj-sup",
                    filename="other.md",
                    storage_path="other.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
            ]
        )
        await session.flush()
        v1 = DocumentVersion(
            id="v1",
            document_id="doc-main",
            version_no=1,
            file_hash="fh-v1",
            valid_from=now - timedelta(days=5),
            valid_to=now - timedelta(minutes=2),
            status="READY",
            search_status="SEARCH_READY",
        )
        v2 = DocumentVersion(
            id="v2",
            document_id="doc-main",
            version_no=2,
            file_hash="fh-v2",
            valid_from=now - timedelta(minutes=1),
            valid_to=now + timedelta(days=365),
            status="READY",
            search_status="INDEXING",
        )
        v_other_hist = DocumentVersion(
            id="v-other-hist",
            document_id="doc-other",
            version_no=1,
            file_hash="fh-oh",
            valid_from=now - timedelta(days=10),
            valid_to=now - timedelta(days=1),
            status="READY",
            search_status="SEARCH_READY",
        )
        session.add_all([v1, v2, v_other_hist])
        await session.flush()

        for bid, vid, txt in (
            ("b-v1", "v1", "Old superseded policy v1 text"),
            ("b-v2", "v2", "New active policy v2 text"),
            ("b-oh", "v-other-hist", "Other doc historical superseded version"),
        ):
            h = hashlib.sha256(txt.encode()).hexdigest()
            session.add(
                CanonicalBlock(
                    id=bid,
                    document_version_id=vid,
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Policy",
                    source_anchor=bid,
                    normalized_text=txt,
                    content_hash=h,
                )
            )
            if vid != "v2":
                su_id = f"su-{vid}"
                session.add(
                    SearchUnit(
                        id=su_id,
                        document_version_id=vid,
                        block_from_id=bid,
                        block_to_id=bid,
                        security_partition_id="proj-sup",
                        content_hash=h,
                        token_count=5,
                        page_from=1,
                        page_to=1,
                        section_path="Policy",
                    )
                )
                qdrant_store[f"pt-{vid}"] = {
                    "id": f"pt-{vid}",
                    "payload": {"search_unit_id": su_id, "document_version_id": vid},
                }
        await session.commit()

        monkeypatch.setattr(
            temporal,
            "resolve_temporal_supersedes",
            AsyncMock(return_value={"action": "SUPERSEDED", "supersedes_id": "v1"}),
        )

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(q_handler)
        ) as q_client:
            await search_index_service.run_search_indexing_stage(
                session,
                project_id="proj-sup",
                document_version=v2,
                security_partition_id="proj-sup",
                tenant_id="t1",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
            await session.commit()

            # Verify queued reconcile job has prune_superseded=True & superseded_version_id="v1"
            reconcile_job = await session.get(Job, v2._last_reconcile_job_id)
            assert reconcile_job is not None
            assert reconcile_job.type == JobType.RECONCILE_SEARCH_INDEX
            assert reconcile_job.payload["reason"] == "superseded_prune_failed"
            assert reconcile_job.payload["superseded_version_id"] == "v1"
            assert reconcile_job.payload.get("prune_superseded") is True
            assert "pt-v1" in qdrant_store

            # Now Qdrant recovers -> run reconcile_search_index
            fail_v1_delete = False
            mgr = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=q_client))
            await tasks.reconcile_search_index(session, reconcile_job, engine_manager=mgr)

            assert "pt-v1" not in qdrant_store, "Failed superseded v1 MUST be deleted by reconcile_search_index"
            assert "pt-v-other-hist" in qdrant_store, "Unrelated historical superseded version should be preserved"
            assert any(
                pt.get("payload", {}).get("document_version_id") == "v2" for pt in qdrant_store.values()
            )

            # Also verify legacy job payload (WITHOUT prune_superseded key) still prunes targeted v1
            qdrant_store["pt-v1"] = {
                "id": "pt-v1",
                "payload": {"search_unit_id": "su-v1", "document_version_id": "v1"},
            }
            legacy_job = Job(
                id="job-legacy-sup",
                type=JobType.RECONCILE_SEARCH_INDEX,
                status=JobStatus.RUNNING,
                payload={
                    "project_id": "proj-sup",
                    "reason": "superseded_prune_failed",
                    "superseded_version_id": "v1",
                },
            )
            session.add(legacy_job)
            await session.commit()
            await tasks.reconcile_search_index(session, legacy_job, engine_manager=mgr)
            assert "pt-v1" not in qdrant_store
            assert "pt-v-other-hist" in qdrant_store


# ---------------------------------------------------------------------------
# Item 7 (P1): retrieve_relevant_sections S4-01 OOD lexical guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_07_retrieve_relevant_sections_blocks_1word_ood_lexical_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "chính sách bảo mật dữ liệu khách hàng doanh nghiệp"
    good_sec = RetrievedSection(
        chunk_id="good-chunk",
        heading="Chính sách bảo mật",
        content="Chính sách bảo mật dữ liệu khách hàng doanh nghiệp yêu cầu mã hóa AES-256.",
        score=0.92,
        raw_dense_score=0.84,
        rank=0,
        source_id="src-1",
        source_config_id="cfg-1",
    )
    ood_sec = RetrievedSection(
        chunk_id="ood-chunk",
        heading="Lịch bảo tàng",
        content="Lịch mở cửa đón khách tham quan bảo tàng mỹ thuật cuối tuần.",
        score=0.35,
        raw_dense_score=0.12,
        rank=1,
        source_id="src-2",
        source_config_id="cfg-1",
    )

    # 1) Direct rerank_sections even when caller passes exact_lexical_keys on non-anchor query
    direct_res = retrieval_service.rerank_sections(
        query,
        [good_sec, ood_sec],
        lexical=[ood_sec],
        exact_lexical_keys={("cfg-1", "ood-chunk")},
        limit=5,
    )
    assert [s.chunk_id for s in direct_res.sections] == ["good-chunk"]

    # 2) Full retrieve_relevant_sections with grep_chunks returning 1-word OOD match for "khách"
    async def fake_search_many(_targets, _q, **_kw):
        return SearchOutcome(query=query, sections=[good_sec, ood_sec], stats={})

    async def fake_grep(_cfg_id, term, **_kw):
        if "khách" in term:
            return [
                {
                    "chunk_id": "ood-chunk",
                    "heading": "Lịch bảo tàng",
                    "snippet": "Lịch mở cửa đón khách tham quan bảo tàng mỹ thuật cuối tuần.",
                    "source_id": "src-2",
                }
            ]
        return []

    monkeypatch.setattr(
        retrieval_service,
        "_hidden_document_derivatives",
        AsyncMock(return_value=retrieval_service._HiddenDerivatives()),
    )
    fake_engine = SimpleNamespace(search_many=fake_search_many, grep_chunks=fake_grep)
    fake_source = SimpleNamespace(id="src-1", sag_source_config_id="cfg-1")
    outcome = await retrieval_service.retrieve_relevant_sections(
        fake_engine,
        [fake_source],
        query,
        top_k=5,
    )
    assert [s.chunk_id for s in outcome.sections] == ["good-chunk"]

    # 3) Explicit identifier anchor query MUST still pass lexical OOD rescue when chunk matches anchor
    anchor_query = "Tra cứu mã INV-2025-001"
    anchor_sec = RetrievedSection(
        chunk_id="inv-chunk",
        heading="Hóa đơn",
        content="Biên lai thanh toán mã INV-2025-001 đã duyệt.",
        score=0.30,
        raw_dense_score=0.12,
        rank=0,
        source_id="src-3",
        source_config_id="cfg-1",
    )
    anchor_res = retrieval_service.rerank_sections(
        anchor_query,
        [anchor_sec],
        lexical=[anchor_sec],
        exact_lexical_keys={("cfg-1", "inv-chunk")},
        limit=5,
    )
    assert [s.chunk_id for s in anchor_res.sections] == ["inv-chunk"]


# ---------------------------------------------------------------------------
# Item 8 (P2 twin): select_diverse_sections uses OR-grouped anchor_groups
# ---------------------------------------------------------------------------


def test_p6_rework_08_select_diverse_sections_uses_date_anchor_groups() -> None:
    query = "So sánh 15/03/2025 và 20/04/2025"
    doc_both = RetrievedSection(
        chunk_id="c-both",
        document_id="d1",
        document_name="report.pdf",
        heading="So sánh",
        content="Vào 2025-03-15 doanh thu đạt 10 tỷ; đến 20/04/2025 tăng lên 15 tỷ.",
        score=0.95,
        rank=0,
        source_id="s1",
        source_config_id="cfg1",
    )
    selected, stats = retrieval_service.select_diverse_sections(query, [doc_both], limit=3)
    assert len(selected) == 1
    assert stats["required_anchor_count"] == 2
    assert stats["covered_anchor_count"] == 2
    assert stats["anchor_coverage"] == "complete"

    doc_one = RetrievedSection(
        chunk_id="c-one",
        document_id="d2",
        document_name="partial.pdf",
        heading="Tháng 3",
        content="Vào 2025-03-15 doanh thu đạt 10 tỷ.",
        score=0.90,
        rank=0,
        source_id="s1",
        source_config_id="cfg1",
    )
    _, stats_one = retrieval_service.select_diverse_sections(query, [doc_one], limit=3)
    assert stats_one["required_anchor_count"] == 2
    assert stats_one["covered_anchor_count"] == 1
    assert stats_one["anchor_coverage"] == "missing_required_anchor"


# ---------------------------------------------------------------------------
# Item 9 (P1): Substrate preserves tenant_id on fallback & regenerate passes tenant_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_09_substrate_preserves_tenant_id_on_mid_thread_degradation(
    rework_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_extract_tenants: list[str | None] = []
    orig_extract = substrate_mod.extract_turns_structured_batch

    async def tracking_extract(*args, **kwargs):
        seen_extract_tenants.append(kwargs.get("tenant_id"))
        return await orig_extract(*args, **kwargs)

    monkeypatch.setattr(substrate_mod, "extract_turns_structured_batch", tracking_extract)

    call_no = 0

    class FlakyEmbedder:
        model = "bge-m3"
        base_url = "http://embed.local"
        dimensions = 4

        async def batch_generate(self, texts: list[str]) -> list[list[float]]:
            nonlocal call_no
            call_no += 1
            if call_no == 1:
                return [[0.5, 0.5, 0.5, 0.5] for _ in texts]
            raise RuntimeError("embedder 503 on turn 2")

    idx = IncrementalSessionIndex("thr-degrade", tenant_id="tenant-X")
    await idx.append_messages(
        [{"id": "t1", "role": "user", "content": "We picked PostgreSQL 18."}],
        embedder=FlakyEmbedder(),
        llm=None,
    )
    assert idx.tenant_id == "tenant-X"
    assert idx.embedding_mode != "deterministic-ngram-v1"

    await idx.append_messages(
        [{"id": "t2", "role": "assistant", "content": "Confirmed PostgreSQL 18."}],
        embedder=FlakyEmbedder(),
        llm=None,
    )
    assert idx.embedding_degraded is True
    assert idx.embedding_mode == "deterministic-ngram-v1"
    assert idx.tenant_id == "tenant-X", f"tenant_id wiped to {idx.tenant_id!r} on fallback!"
    assert all(t == "tenant-X" for t in seen_extract_tenants), f"Saw unexpected tenants: {seen_extract_tenants}"

    # Verify prepare_regenerate passes principal.tenant_id into IncrementalSessionIndex
    created_indexes: list[IncrementalSessionIndex] = []
    orig_cls = answer_actions_service.IncrementalSessionIndex

    class SpySessionIndex(orig_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_indexes.append(self)

    monkeypatch.setattr(answer_actions_service, "IncrementalSessionIndex", SpySessionIndex)
    monkeypatch.setattr(settings, "session_context_mode", "on")
    monkeypatch.setattr(
        decision_router,
        "route_query_async",
        AsyncMock(
            return_value=UnifiedRouteDecision(
                query="q",
                coarse_intent="KNOWLEDGE",
                question_scope="project",
                memory_scope="session_history",
                temporal_mode="atemporal",
                answer_shape="fact_answer",
                intent_flags=IntentFlags(needs_retrieval=True),
                domain="general",
                confidence=0.9,
            )
        ),
    )

    async with rework_db() as session:
        ag = Agent(id="ag-ten", owner_key="owner-1", name="Bot", persona={})
        session.add(ag)
        await session.flush()
        th = Thread(id="th-ten", agent_id=ag.id, owner_key="owner-1", title="T")
        session.add(th)
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                Message(
                    id="r-u1",
                    thread_id=th.id,
                    role=MessageRole.USER,
                    content="Hello",
                    status=MessageStatus.OK,
                    created_at=now,
                ),
                Message(
                    id="r-a1",
                    thread_id=th.id,
                    role=MessageRole.ASSISTANT,
                    content="Hi",
                    reply_to_message_id="r-u1",
                    status=MessageStatus.OK,
                    created_at=datetime.fromtimestamp(now.timestamp() + 1, tz=UTC),
                ),
                Message(
                    id="r-u2",
                    thread_id=th.id,
                    role=MessageRole.USER,
                    content="Follow up",
                    status=MessageStatus.OK,
                    created_at=datetime.fromtimestamp(now.timestamp() + 2, tz=UTC),
                ),
                Message(
                    id="r-a2",
                    thread_id=th.id,
                    role=MessageRole.ASSISTANT,
                    content="Old reply",
                    reply_to_message_id="r-u2",
                    status=MessageStatus.OK,
                    created_at=datetime.fromtimestamp(now.timestamp() + 3, tz=UTC),
                ),
            ]
        )
        await session.commit()

        await answer_actions_service.prepare_regenerate(
            session,
            agent=ag,
            thread=th,
            target_message_id="r-a2",
            principal=_principal(tenant_id="tenant-regen-42"),
            owner_key="owner-1",
            llm=None,
        )
        assert created_indexes
        assert created_indexes[-1].tenant_id == "tenant-regen-42"


# ---------------------------------------------------------------------------
# Item 10 (P2): _project_publish_lock awaits cancelled release_task before invalidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_10_project_publish_lock_no_concurrent_invalidate_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlock_started = asyncio.Event()
    unlock_cancelled = False
    invalidated = False
    closed = False
    active_ops = 0
    concurrent_op_detected = False

    class RacingConn:
        async def start(self):
            return None

        @asynccontextmanager
        async def begin(self):
            nonlocal active_ops, concurrent_op_detected
            if active_ops > 0:
                concurrent_op_detected = True
            active_ops += 1
            try:
                yield
            finally:
                active_ops -= 1

        async def scalar(self, stmt, _params=None):
            nonlocal unlock_cancelled
            sql = str(stmt)
            if "pg_try_advisory_lock" in sql:
                return True
            if "pg_advisory_unlock" in sql:
                unlock_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    unlock_cancelled = True
                    raise
            return None

        async def invalidate(self):
            nonlocal invalidated, concurrent_op_detected
            if active_ops > 0:
                concurrent_op_detected = True
            invalidated = True

        async def close(self):
            nonlocal closed
            closed = True

    monkeypatch.setattr(tree_publish_service, "engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(tree_publish_service, "publish_lock_engine", SimpleNamespace(connect=lambda: RacingConn()))

    async def _worker():
        async with tree_publish_service._project_publish_lock("proj-race"):
            pass

    task = asyncio.create_task(_worker())
    await unlock_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert unlock_cancelled is True
    assert invalidated is True
    assert closed is True
    assert concurrent_op_detected is False, "connection.invalidate() ran concurrently with active unlock!"


# ---------------------------------------------------------------------------
# Item 11 (P2): _parse_html_span non-positive clamping & rollback duplicate KU
# ---------------------------------------------------------------------------


def test_p6_rework_11_html_span_non_positive_and_rollback_duplicate_ku() -> None:
    assert _parse_html_span("0") == 1
    assert _parse_html_span("-5") == 1
    assert _parse_html_span("99999") == 100
    assert _parse_html_span("abc") == 1

    md_neg = _html_table_to_markdown(
        '<table><tr><td colspan="-5">Neg</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>'
    )
    assert md_neg.splitlines()[0].count("|") - 1 == 2

    md_zero = _html_table_to_markdown(
        '<table><tr><td colspan="0">Zero</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>'
    )
    assert md_zero.splitlines()[0].count("|") - 1 == 2

    # rowspan="0" must still honor remaining rows (2 rows total)
    md_rowspan0 = _html_table_to_markdown(
        '<table><tr><td rowspan="0">Span</td><td>R1</td></tr><tr><td>R2</td></tr></table>'
    )
    assert "Span" in md_rowspan0
    assert "R2" in md_rowspan0

    # Rollback manifest rejects duplicate and whitespace-only knowledge_unit_ids
    for bad_ku_ids in (["ku-1", "ku-1"], ["  ", "ku-2"]):
        manifest_payload: dict = {
            "project_id": "proj-rb-dup",
            "tenant_id": "t-1",
            "source_tree_version": "tv-0",
            "expected_point_count": 1,
            "quality_gates": {"recall": True},
            "routing_profiles": [
                {
                    "node_id": "leaf-1",
                    "parent_node_id": "root",
                    "partition_id": "public",
                    "level": 1,
                    "is_leaf": True,
                    "unit_ids": ["ku-1", "ku-2"],
                    "child_node_ids": [],
                }
            ],
            "source_routing_profiles": [],
            "search_unit_assignments": [
                {
                    "knowledge_unit_ids": bad_ku_ids,
                    "search_unit_id": "su-1",
                    "source_id": "src-1",
                    "document_version_id": "ver-1",
                    "partition_id": "public",
                }
            ],
            "security_partition_search_unit_counts": {"public": 1},
        }
        chk = _checksum_manifest(manifest_payload)
        tv = f"tree-{chk[:24]}"
        manifest_payload["checksum"] = chk
        manifest_payload["tree_version"] = tv
        rec = TreeManifest(
            tree_version=tv,
            project_id="proj-rb-dup",
            config_version="v1",
            node_count=1,
            leaf_count=1,
            max_leaf_size=2,
            giant_ratio=0.0,
            routing_recall_at_k=1.0,
            escape_win_rate=0.0,
            acl_blackhole_rate=0.0,
            status="INACTIVE",
            checksum=chk,
            manifest_json=json.dumps(manifest_payload),
        )
        with pytest.raises(PublishVerificationError, match="knowledge_unit_ids"):
            tree_rollback_service._prepared_candidate_from_manifest(rec, expected_status="INACTIVE")


# ---------------------------------------------------------------------------
# Item 12 (CHAL-R2-1): Non-knowledge tool mid-stream Turn-1 -> Turn-2 SSE must NOT double-stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_12_non_knowledge_tool_sse_no_double_stream(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-time", name="TimeBot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "TIME", "degraded": False},
        ),
    )

    async def fake_generate_stream(*args, **kwargs):
        yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
        yield SimpleNamespace(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "Let me check the current time."}},
        )
        yield SimpleNamespace(
            type=EventType.TOOL_STARTED.value,
            data={"payload": {"name": "get_time", "call_id": "tc-time-1"}},
        )
        yield SimpleNamespace(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "The current time is 16:50 UTC."}},
        )
        yield SimpleNamespace(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": "The current time is 16:50 UTC.", "citations": [], "degraded": False}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_generate_stream)

    body = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="What time is it?")],
        stream=True,
    )
    resp = await openai_api.chat_completions(
        agent_id="agent-time",
        body=body,
        _user=SimpleNamespace(id="u-1", name="Tester"),
        owner_key="owner-1",
        principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
        llm=SimpleNamespace(configured=True),
        tool_registry=SimpleNamespace(),
        agent_runtime=SimpleNamespace(),
    )
    chunks: list[str] = []
    async for raw in resp.body_iterator:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        for line in text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            payload = json.loads(line[len("data: ") :])
            delta = (payload.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                chunks.append(delta["content"])
    full = "".join(chunks)
    assert full.count("The current time is 16:50 UTC.") == 1
    assert "Let me check the current time." in full
    assert "Let me check the current time.\n\nThe current time is 16:50 UTC." == full


# ---------------------------------------------------------------------------
# Item 13 (CHAL-R2-2): CommonMark §4.5 line-start triple-backtick inline code is NOT a fence
# ---------------------------------------------------------------------------


def test_p6_rework_13_commonmark_line_start_triple_backtick_inline_code() -> None:
    cits = [
        {
            "n": 1,
            "chunk_id": "c1",
            "source_id": "s1",
            "document_id": "d1",
            "document_version_id": "v1",
            "page_from": 1,
            "page_to": 1,
            "anchor": "p1",
        }
    ]
    sample = "```inline``` and invalid [99] and valid [1]."
    cleaned, used = agent_service._finalize_answer_citations(sample, cits)
    assert cleaned.startswith("```inline```")
    assert "[99]" not in cleaned
    assert "[1]." in cleaned
    assert [c["n"] for c in used] == [1]

    # Twin in openai.py _split_unbuffered_citation_tail must detect [99] as prose citation tail!
    flushed, pending = openai_api._split_unbuffered_citation_tail(sample)
    assert flushed == "```inline``` and invalid"
    assert pending == " [99] and valid [1]."


# ---------------------------------------------------------------------------
# Item 14 (CHAL-R3-1): Preflight TOOL_FAILED without TOOL_STARTED must not double-stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_14_preflight_tool_failed_sse_no_double_stream(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-tf", name="ToolFailBot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "TIME", "degraded": False},
        ),
    )

    async def fake_generate_stream(*args, **kwargs):
        yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
        yield SimpleNamespace(type=EventType.TURN_STARTED.value, data={"payload": {"turn": 1}})
        yield SimpleNamespace(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "Let me check."}},
        )
        # Preflight failure (e.g. unknown_tool / parse_error): TOOL_FAILED fires WITHOUT TOOL_STARTED
        yield SimpleNamespace(
            type=EventType.TOOL_FAILED.value,
            data={"payload": {"name": "unknown_clock", "error": {"code": "unknown_tool"}}},
        )
        yield SimpleNamespace(type=EventType.TURN_STARTED.value, data={"payload": {"turn": 2}})
        yield SimpleNamespace(
            type=EventType.MESSAGE_DELTA.value,
            data={"payload": {"delta": "I could not call the clock tool."}},
        )
        yield SimpleNamespace(
            type=EventType.RUN_COMPLETED.value,
            data={"payload": {"output": "I could not call the clock tool.", "citations": [], "degraded": False}},
        )

    monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_generate_stream)

    body = openai_api.ChatCompletionRequest(
        messages=[openai_api.ChatMessage(role="user", content="Check clock")],
        stream=True,
    )
    resp = await openai_api.chat_completions(
        agent_id="agent-tf",
        body=body,
        _user=SimpleNamespace(id="u-1", name="Tester"),
        owner_key="owner-1",
        principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
        session=SimpleNamespace(),
        engine_manager=SimpleNamespace(),
        llm=SimpleNamespace(configured=True),
        tool_registry=SimpleNamespace(),
        agent_runtime=SimpleNamespace(),
    )
    chunks: list[str] = []
    async for raw in resp.body_iterator:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        for line in text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            payload = json.loads(line[len("data: ") :])
            delta = (payload.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                chunks.append(delta["content"])
    full = "".join(chunks)
    assert full.count("I could not call the clock tool.") == 1
    assert full == "Let me check.\n\nI could not call the clock tool."


# ---------------------------------------------------------------------------
# Item 15 (CHAL-R3-2): Stateful SSE markdown fence/inline-code across chunk boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_15_stateful_chunk_boundary_fences_and_split_inline_code(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-md", name="MdBot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_deltas(deltas: list[str], canonical: str) -> str:
        async def fake_stream(*args, **kwargs):
            yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
            for d in deltas:
                yield SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": d}})
            yield SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": canonical, "citations": [], "degraded": False}},
            )

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="show code")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-md",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # 1) Closing backtick fence + prose [99] in next chunk
    out1 = await _run_deltas(
        ["Here is code:\n```python\nx = 1\n", "```\nSee [99]."],
        "Here is code:\n```python\nx = 1\n```\nSee.",
    )
    assert "[99]" not in out1
    assert out1 == "Here is code:\n```python\nx = 1\n```\nSee."

    # 2) Closing tilde fence + prose [99] in next chunk
    out2 = await _run_deltas(
        ["Here:\n~~~\ny = 2\n", "~~~\nAfter [99]."],
        "Here:\n~~~\ny = 2\n~~~\nAfter.",
    )
    assert "[99]" not in out2
    assert out2 == "Here:\n~~~\ny = 2\n~~~\nAfter."

    # 3) Line-start split triple-backtick inline code
    out3 = await _run_deltas(
        ["```inline", "``` and [99]."],
        "```inline``` and.",
    )
    assert "[99]" not in out3
    assert out3 == "```inline``` and."

    # 4) Mid-line split triple-backtick inline code
    out4 = await _run_deltas(
        ["Use ", "```foo", "``` [99]."],
        "Use ```foo```.",
    )
    assert "[99]" not in out4
    assert out4 == "Use ```foo```."

    # 5) 1-char streaming across opening AND closing fence + prose [99]
    src5 = "Code:\n```py\n[99]\n```\nEnd [99]."
    out5 = await _run_deltas(list(src5), "Code:\n```py\n[99]\n```\nEnd.")
    assert out5 == "Code:\n```py\n[99]\n```\nEnd."


# ---------------------------------------------------------------------------
# Item 16 (REV-R4-1..3 / CHAL-R4-1..4): 4+-char fence split, indented backtick
# fence split, multi-line inline code state sync, and >64-char split inline code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_16_four_char_fence_indented_backtick_and_multiline_inline_code(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-md4", name="Md4Bot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_deltas(deltas: list[str], canonical: str) -> str:
        async def fake_stream(*args, **kwargs):
            yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
            for d in deltas:
                yield SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": d}})
            yield SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": canonical, "citations": [], "degraded": False}},
            )

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="code")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-md4",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # 1) 4-backtick closing fence split at 3 chars + 1-char streamed
    canon_4bt = "Intro:\n````markdown\n```py\n[99]\n```\n````\nAfter."
    out_4bt_split = await _run_deltas(
        ["Intro:\n````markdown\n```py\n[99]\n```\n", "```", "`\nAfter [99]."],
        canon_4bt,
    )
    assert out_4bt_split == canon_4bt
    out_4bt_1ch = await _run_deltas(
        list("Intro:\n````markdown\n```py\n[99]\n```\n````\nAfter [99]."),
        canon_4bt,
    )
    assert out_4bt_1ch == canon_4bt

    # 2) 4-tilde closing fence split at 3 chars
    canon_4tl = "Intro:\n~~~~\n~~~\n~~~~\nAfter."
    out_4tl = await _run_deltas(
        ["Intro:\n~~~~\n~~~\n", "~~~", "~\nAfter [99]."],
        canon_4tl,
    )
    assert out_4tl == canon_4tl

    # 3) Indented (2-space) opening backtick fence split after 2 backticks & 1-char streamed
    canon_ind = "Code:\n  ```py\nx = 1\n\nprint(1)\n```\nAfter."
    out_ind_split = await _run_deltas(
        ["Code:\n  ``", "`py\nx = 1\n\nprint(1)\n```\nAfter [99]."],
        canon_ind,
    )
    assert out_ind_split == canon_ind
    out_ind_1ch = await _run_deltas(
        list("Code:\n  ```py\nx = 1\n\nprint(1)\n```\nAfter [99]."),
        canon_ind,
    )
    assert out_ind_1ch == canon_ind

    # 4) CommonMark-valid multi-line inline code (non-fence interior lines such as 4-space indented
    #    markers or plain lines) must NOT desync fence state or leak citations
    canon_ml1 = "Example: `line1\n    ~~~\nline2 [99]`. End."
    out_ml1 = await _run_deltas(["Example: `line1\n    ~~~\nline2 [99]`.", " End [99]."], canon_ml1)
    assert out_ml1 == canon_ml1

    canon_ml2 = "Use `foo\n    ```py\nbar`\nSee."
    out_ml2 = await _run_deltas(["Use `foo\n    ```py\nbar`\n", "See [99]."], canon_ml2)
    assert out_ml2 == canon_ml2

    # 5) Long (>64 chars) line-start split triple-backtick inline code must NOT stick fence open
    long_code = "a" * 80
    canon_long = f"```{long_code}``` done."
    out_long = await _run_deltas([f"```{long_code}", "``` done [99]."], canon_long)
    assert out_long == canon_long


# ---------------------------------------------------------------------------
# Item 17 (AUD-R5 VETO): Strict O(N) backtick index on distinct run lengths
# (openai.py + twin agent_service.py) & unterminated closing fence guard
# ---------------------------------------------------------------------------


def test_p6_rework_17_strict_linear_backtick_index_and_unterminated_closing_fence() -> None:
    def _distinct_bt_runs(target_len: int) -> str:
        parts: list[str] = []
        total = 0
        k = 1
        while total < target_len:
            seg = "`" * k + " "
            parts.append(seg)
            total += len(seg)
            k += 1
        return "".join(parts)

    def _distinct_plus_double_tail(target_len: int) -> str:
        parts: list[str] = ["` "]
        total = 2
        k = 3
        while total < target_len // 2:
            seg = "`" * k + " "
            parts.append(seg)
            total += len(seg)
            k += 1
        while total < target_len:
            parts.append("`` ")
            total += 3
        return "".join(parts)

    def _best_ms(fn, *args) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            fn(*args)
            best = min(best, (time.perf_counter() - t0) * 1000.0)
        return best

    for builder in (_distinct_bt_runs, _distinct_plus_double_tail):
        s_2k = builder(2000)
        s_32k = builder(32000)
        assert _best_ms(openai_api._prose_segments, s_2k) < 10.0
        assert _best_ms(openai_api._split_unbuffered_citation_tail_stateful, s_2k) < 10.0
        assert _best_ms(agent_service._finalize_answer_citations, s_2k, []) < 10.0
        assert _best_ms(openai_api._prose_segments, s_32k) < 40.0
        assert _best_ms(openai_api._split_unbuffered_citation_tail_stateful, s_32k) < 50.0
        assert _best_ms(agent_service._finalize_answer_citations, s_32k, []) < 50.0

    # Unterminated closing fence line > 64 chars is HELD cleanly without closing or stranding fence
    flushed, pending, next_ls, next_fence = openai_api._split_unbuffered_citation_tail_stateful(
        "```" + " " * 70,
        at_line_start=True,
        code_fence="```",
    )
    assert flushed == ""
    assert pending == "```" + " " * 70
    assert next_ls is True
    assert next_fence == "```"

    # Follow-up "\nprose [99]" closes the fence and holds "[99]"
    flushed2, pending2, next_ls2, next_fence2 = openai_api._split_unbuffered_citation_tail_stateful(
        pending + "\nprose [99]",
        at_line_start=next_ls,
        code_fence=next_fence,
    )
    assert flushed2 == "```" + " " * 70 + "\nprose"
    assert pending2 == " [99]"
    assert next_ls2 is False
    assert next_fence2 is None

    # Alternative follow-up "foo\n" keeps fence open (non-whitespace suffix is not a closing fence)
    flushed3, pending3, next_ls3, next_fence3 = openai_api._split_unbuffered_citation_tail_stateful(
        pending + "foo\n",
        at_line_start=next_ls,
        code_fence=next_fence,
    )
    assert flushed3 == "```" + " " * 70 + "foo\n"
    assert pending3 == ""
    assert next_ls3 is True
    assert next_fence3 == "```"


# ---------------------------------------------------------------------------
# Item 18 (CHAL-R6 F1-F5): CRLF & >64-char closing fences, mid-paragraph
# fenced blocks, CommonMark blank lines, EOF parity, and long header/inline spans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_18_challenger_r6_crlf_long_fence_mid_para_fence_and_blank_lines(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-r6", name="R6Bot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_deltas(deltas: list[str], canonical: str) -> str:
        async def fake_stream(*args, **kwargs):
            yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
            for d in deltas:
                yield SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": d}})
            yield SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": canonical, "citations": [], "degraded": False}},
            )

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="code")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-r6",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # F1A: CRLF closing fences (backtick, tilde, indented) split at \r|\n and 1-char streamed
    for raw_text, chunks in [
        (
            "```py\r\ncode [99]\r\n```\r\nprose [99]",
            ["```py\r\ncode [99]\r\n", "```\r", "\nprose [99]"],
        ),
        (
            "~~~py\r\ncode [99]\r\n~~~\r\nprose [99]",
            ["~~~py\r\ncode [99]\r\n", "~~~\r", "\nprose [99]"],
        ),
        (
            "```py\r\ncode [99]\r\n   ```\r\nprose [99]",
            ["```py\r\ncode [99]\r\n", "   ```\r", "\nprose [99]"],
        ),
    ]:
        canon, _ = agent_service._finalize_answer_citations(raw_text, [])
        assert canon.endswith("\r\nprose")
        assert "code [99]" in canon
        assert await _run_deltas(chunks, canon) == canon
        assert await _run_deltas(list(raw_text), canon) == canon

    # Non-CommonMark trailing whitespace (NBSP) must NOT close a code fence
    nbsp_text = "```py\ncode [99]\n```\u00a0\nstill_code [99]"
    nbsp_canon, _ = agent_service._finalize_answer_citations(nbsp_text, [])
    assert nbsp_canon == nbsp_text
    assert await _run_deltas(["```py\ncode [99]\n", "```\u00a0", "\nstill_code [99]"], nbsp_canon) == nbsp_canon

    # F1B: Closing fence > 64 chars (spaces, 65 backticks, tilde) streamed 1-char & split before \n
    for closer in ("```" + " " * 70, "```" + " " * 62, "`" * 65):
        raw_long = f"```py\ncode [99]\n{closer}\nprose [99]"
        canon_long, _ = agent_service._finalize_answer_citations(raw_long, [])
        assert canon_long == f"```py\ncode [99]\n{closer}\nprose"
        assert await _run_deltas(["```py\ncode [99]\n", closer, "\nprose [99]"], canon_long) == canon_long
        assert await _run_deltas(list(raw_long), canon_long) == canon_long

    raw_tilde_long = "~~~py\ncode [99]\n" + "~~~" + " " * 70 + "\nprose [99]"
    canon_tilde_long, _ = agent_service._finalize_answer_citations(raw_tilde_long, [])
    assert canon_tilde_long == "~~~py\ncode [99]\n" + "~~~" + " " * 70 + "\nprose"
    assert await _run_deltas(list(raw_tilde_long), canon_tilde_long) == canon_tilde_long

    # F2A/B/C: Mid-paragraph fenced code blocks interrupt paragraphs (CommonMark §3 / §4.5)
    for raw_f2, expected_f2 in [
        (
            "Use ``` in markdown:\n```python\narr[99] = 1\n```\nReal prose [99]",
            "Use ``` in markdown:\n```python\narr[99] = 1\n```\nReal prose",
        ),
        (
            "Here is a stray ` backtick\n```python\nx = `hello` [99]\n```\nReal prose [99]",
            "Here is a stray ` backtick\n```python\nx = `hello` [99]\n```\nReal prose",
        ),
        (
            "Here is a stray ` backtick\n~~~python\nx = `hello` [99]\n~~~\nReal prose [99]",
            "Here is a stray ` backtick\n~~~python\nx = `hello` [99]\n~~~\nReal prose",
        ),
        (
            "Here is ` stray\n```python\ncode = 1\n```\nSee `x [99]` and prose [99]",
            "Here is ` stray\n```python\ncode = 1\n```\nSee `x [99]` and prose",
        ),
    ]:
        canon_f2, _ = agent_service._finalize_answer_citations(raw_f2, [])
        assert canon_f2 == expected_f2
        assert await _run_deltas(list(raw_f2), expected_f2) == expected_f2

    # F3: Non-LF CommonMark blank lines (\r\n\r\n, \n \n, \n\t\n) terminate inline backtick spans
    for sep in ("\r\n\r\n", "\n \n", "\n\t\n", "\n \t \r\n"):
        raw_f3 = f"Para 1 has stray ` and [99].{sep}Para 2 has stray ` end"
        expected_f3 = f"Para 1 has stray ` and.{sep}Para 2 has stray ` end"
        canon_f3, _ = agent_service._finalize_answer_citations(raw_f3, [])
        assert canon_f3 == expected_f3
        assert await _run_deltas([raw_f3], expected_f3) == expected_f3
        assert await _run_deltas(list(raw_f3), expected_f3) == expected_f3

    # F4: Exact EOF parity between openai._prose_segments(hold_incomplete=False) and agent_service
    segs_open, _, fence_open = openai_api._prose_segments("```py [99]", hold_incomplete=False)
    assert segs_open == []
    assert fence_open == "```"
    assert agent_service._finalize_answer_citations("```py [99]", [])[0] == "```py [99]"

    segs_close, _, fence_close = openai_api._prose_segments(
        "```py\ncode [99]\n```",
        hold_incomplete=False,
    )
    assert segs_close == []
    assert fence_close is None

    # F5A: >256-char opening fence header split before \n must NOT invert fence state
    raw_f5a = "```" + "a" * 260 + "\ncode [99]\n```\nprose [99]"
    canon_f5a, _ = agent_service._finalize_answer_citations(raw_f5a, [])
    assert canon_f5a == "```" + "a" * 260 + "\ncode [99]\n```\nprose"
    assert await _run_deltas(["```" + "a" * 260, "\ncode [99]\n```\nprose [99]"], canon_f5a) == canon_f5a

    # Bounded inline code span (<=64 chars) streamed 1-char preserves parity & holds prose [99]
    raw_inline60 = "`" + "a" * 60 + "` prose [99] `ok`"
    canon_inline60, _ = agent_service._finalize_answer_citations(raw_inline60, [])
    assert canon_inline60 == "`" + "a" * 60 + "` prose  `ok`"
    assert await _run_deltas(list(raw_inline60), canon_inline60) == canon_inline60


# ---------------------------------------------------------------------------
# Item 19 (REV-R7-1..3 / CHAL-R7-1..4): Single-pass state derivation (no false
# fence open/close on truncated flushed prefixes), closing backtick EOF hold,
# and unterminated backtick fence lookahead during streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_19_single_pass_sse_state_closing_bt_eof_and_unterminated_bt_fence(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(id="agent-r7", name="R7Bot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_deltas(deltas: list[str], canonical: str) -> str:
        async def fake_stream(*args, **kwargs):
            yield SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}})
            for d in deltas:
                yield SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": d}})
            yield SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": canonical, "citations": [], "degraded": False}},
            )

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="code")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-r7",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # 1) REV-R7-1 / CHAL-R7-2: Truncated non-fence ``` prefix must NEVER falsely open code_fence
    flushed_c, _, _, next_fence_c = openai_api._split_unbuffered_citation_tail_stateful("``` [99] `code`\n\n")
    assert flushed_c == "```"
    assert next_fence_c is None

    for raw_r1, splits in [
        ("```" + "a" * 80 + "``` done [99].", [list("```" + "a" * 80 + "``` done [99].")]),
        ("```" + "a" * 70 + "``` done [99].", [["```" + "a" * 70 + "`", "`` done [99]."]]),
        ("``` [99] `code`\n\nprose [88]", [list("``` [99] `code`\n\nprose [88]")]),
        ("``` [99] `\n\nNext [88]", [["``` [99] `\n\nNext ", "[88]"]]),
        (
            "``` " + "a" * 65 + " `x` [99]\nNext [88]",
            [["``` " + "a" * 65 + " `x", "` [99]\nNext [88]"]],
        ),
    ]:
        canon_r1, _ = agent_service._finalize_answer_citations(raw_r1, [])
        assert "[99]" not in canon_r1 and "[88]" not in canon_r1
        for chunks in splits:
            assert await _run_deltas(chunks, canon_r1) == canon_r1

    # 2) CHAL-R7-3: Non-closing "```\r " / "~~~\r\t" inside open fence must NOT falsely close code_fence
    flushed_cr, pending_cr, _, next_fence_cr = openai_api._split_unbuffered_citation_tail_stateful(
        "```\r ",
        at_line_start=True,
        code_fence="```",
    )
    assert flushed_cr == "```\r"
    assert pending_cr == " "
    assert next_fence_cr == "```"

    for raw_r3 in [
        "```py\ncode [99]\n```\r \nstill\n```\nprose [88]",
        "~~~py\ncode [99]\n~~~\r\t\nstill\n~~~\nprose [88]",
        "```py\ncode [99]\n   ```\r \nstill\n```\nprose [88]",
    ]:
        canon_r3, _ = agent_service._finalize_answer_citations(raw_r3, [])
        assert "code [99]" in canon_r3 and "[88]" not in canon_r3
        assert await _run_deltas(list(raw_r3), canon_r3) == canon_r3

    # 3) REV-R7-2 / CHAL-R7-1: Closing backtick run at chunk EOF must hold until next char confirms length
    segs_eof, unclosed_eof, _ = agent_service._scan_markdown_prose_segments("Stray ` [99] `", hold_incomplete=True)
    assert segs_eof == [(0, 6)]
    assert unclosed_eof == 6

    for raw_r2, custom_chunks in [
        ("Stray ` [99] ``code``", [["Stray ` [99] `", "`code``"]]),
        ("See ``a```x [99] `b` end", [["See ``a``", "`x [99] `b` end"]]),
        (
            "Here is a stray ` [99]\n```python\nx = 1\n```\nReal prose [88]",
            [["Here is a stray ` [99]\n`", "``python\nx = 1\n```\nReal prose [88]"]],
        ),
        (
            "Stray `` [99]\n```python\nx = 1\n```\nProse [88]",
            [["Stray `` [99]\n``", "`python\nx = 1\n```\nProse [88]"]],
        ),
        (
            "Here is a stray ` backtick\n```python\n# " + "x" * 70 + "\na = 1\n```\nReal prose [99]",
            [
                ["Here is a stray ` backtick\n`", "``python\n# " + "x" * 70 + "\na = 1\n```\nReal prose [99]"],
            ],
        ),
    ]:
        canon_r2, _ = agent_service._finalize_answer_citations(raw_r2, [])
        assert "[99]" not in canon_r2 and "[88]" not in canon_r2
        assert await _run_deltas(list(raw_r2), canon_r2) == canon_r2
        for chunks in custom_chunks:
            assert await _run_deltas(chunks, canon_r2) == canon_r2

    # 4) REV-R7-3 / CHAL-R7-4: Unterminated backtick fence line during streaming must NOT prematurely split paragraph
    segs_u, unclosed_u, _ = agent_service._scan_markdown_prose_segments("Check `a\n```", hold_incomplete=True)
    assert segs_u == [(0, 6)]
    assert unclosed_u == 6

    for raw_r4, chunks_r4 in [
        ("Check `a\n```b` [99] c`\n\nEnd", ["Check `a\n```", "b` [99] c`\n\nEnd"]),
        ("Use `a\n   ```b` [99] c`\n\nEnd", ["Use `a\n   ```", "b` [99] c`\n\nEnd"]),
        ("Check ```a\n```b\ncode [99]\n```\nprose [88]", ["Check ```a\n```b", "\ncode [99]\n```\nprose [88]"]),
        ("Check ```a\n```b` [99]\n\nEnd", ["Check ```a\n```b", "` [99]\n\nEnd"]),
    ]:
        canon_r4, _ = agent_service._finalize_answer_citations(raw_r4, [])
        assert await _run_deltas(chunks_r4, canon_r4) == canon_r4
        assert await _run_deltas(list(raw_r4), canon_r4) == canon_r4


# ---------------------------------------------------------------------------
# Item 20 (CHAL-R8-1 & CHAL-R8-2): Flush held inline code / closing fence at
# turn rotation without leaking citations, and handle trailing Unicode whitespace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_20_turn_rotation_eof_code_flush_and_unicode_whitespace(monkeypatch) -> None:
    agent = SimpleNamespace(id="agent-r8", name="R8Bot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_events(events: list[SimpleNamespace]) -> str:
        async def fake_stream(*args, **kwargs):
            for ev in events:
                yield ev

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="check")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-r8",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # CHAL-R8-1A: Turn 1 ending in inline code before non-knowledge tool (web_search)
    out_ws = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": "Let me inspect `config.yaml`"}},
            ),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_ws == "Let me inspect `config.yaml`\n\nDone."

    # CHAL-R8-1B: Turn 1 ending in inline code before knowledge tool (search_context)
    out_sc = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": "Let me inspect `config.yaml`"}},
            ),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "search_context"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Found setting X."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Found setting X.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_sc == "Let me inspect `config.yaml`\n\nFound setting X."

    # CHAL-R8-1C: Turn 1 with ONLY inline code (streamed_parts empty) + Turn 1 held [99] before tool
    out_only_code = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "`config.yaml`"}}),
            SimpleNamespace(type=EventType.TURN_STARTED.value, data={"payload": {}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_only_code == "`config.yaml`\n\nDone."

    out_cit_before_tool = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": "Checking [99] and `config.yaml`"}},
            ),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert "[99]" not in out_cit_before_tool
    assert out_cit_before_tool == "Checking and `config.yaml`\n\nDone."

    # CHAL-R8-2: Trailing Unicode whitespace (\u3000, \u00a0, \u2003) must NOT duplicate response 2x
    for raw_u, expected_u in [
        ("根据文档，系统支持双因素认证。\u3000", "根据文档，系统支持双因素认证。\u3000"),
        ("The system supports MFA.\u00a0", "The system supports MFA.\u00a0"),
        ("The system supports MFA.\u2003", "The system supports MFA.\u2003"),
        ("The system supports MFA\u00a0[99]", "The system supports MFA\u00a0"),
    ]:
        canon_u, _ = agent_service._finalize_answer_citations(raw_u, [])
        out_u = await _run_events(
            [
                SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
                *[
                    SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": ch}})
                    for ch in raw_u
                ],
                SimpleNamespace(
                    type=EventType.RUN_COMPLETED.value,
                    data={"payload": {"output": canon_u, "citations": [], "degraded": False}},
                ),
            ]
        )
        assert out_u == expected_u
        assert out_u.count(canon_u) == 1


# ---------------------------------------------------------------------------
# Item 21 (CHAL-R9-1..5): Newline-based turn separators, site-only citation
# whitespace cleanup, leading citation lstrip, IGNORECASE links, and post-tool
# exact-match Unicode whitespace dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_rework_21_turn_separators_markdown_indent_links_and_post_tool_unicode_ws(
    monkeypatch,
) -> None:
    agent = SimpleNamespace(id="agent-r9", name="R9Bot", persona={"web_enabled": True})
    monkeypatch.setattr(openai_api.svc, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(openai_api.svc, "resolve_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        openai_api.svc,
        "build_ask_context",
        lambda **kw: SimpleNamespace(
            source_ids=[],
            citations=[],
            session_citations=[],
            route_decision={"coarse_intent": "CHAT", "degraded": False},
        ),
    )

    async def _run_events(events: list[SimpleNamespace]) -> str:
        async def fake_stream(*args, **kwargs):
            for ev in events:
                yield ev

        monkeypatch.setattr(openai_api.agent_service, "generate_stream", fake_stream)
        body = openai_api.ChatCompletionRequest(
            messages=[openai_api.ChatMessage(role="user", content="check")],
            stream=True,
        )
        resp = await openai_api.chat_completions(
            agent_id="agent-r9",
            body=body,
            _user=SimpleNamespace(id="u-1", name="Tester"),
            owner_key="owner-1",
            principal=SimpleNamespace(tenant_id="t-1", organization_id="o-1", user_id="u-1"),
            session=SimpleNamespace(),
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(configured=True),
            tool_registry=SimpleNamespace(),
            agent_runtime=SimpleNamespace(),
        )
        out_chunks: list[str] = []
        async for raw in resp.body_iterator:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = json.loads(line[len("data: ") :])
                delta = (payload.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    out_chunks.append(delta["content"])
        return "".join(out_chunks)

    # CHAL-R9-1A: Closing code fence with trailing horizontal space must still get \n\n before Turn 2
    for tool_name in ("web_search", "search_context"):
        out_fence_ws = await _run_events(
            [
                SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
                SimpleNamespace(
                    type=EventType.MESSAGE_DELTA.value,
                    data={"payload": {"delta": "```py\nx = 1\n``` "}},
                ),
                SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": tool_name}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
                SimpleNamespace(
                    type=EventType.RUN_COMPLETED.value,
                    data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
                ),
            ]
        )
        assert out_fence_ws == "```py\nx = 1\n``` \n\nDone."
        segs_check, _, fence_check = agent_service._scan_markdown_prose_segments(
            out_fence_ws, hold_incomplete=False
        )
        assert fence_check is None
        assert segs_check and "Done." in out_fence_ws[segs_check[-1][0] : segs_check[-1][1]]

    # CHAL-R9-1B: Turn 1 ending in horizontal space (\u3000 / ' ') before Turn 2 opening fence
    out_fence_t2 = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Hello\u3000"}}),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
            SimpleNamespace(
                type=EventType.MESSAGE_DELTA.value,
                data={"payload": {"delta": "```py\nreturn [99]\n```"}},
            ),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "```py\nreturn [99]\n```", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_fence_t2 == "Hello\u3000\n\n```py\nreturn [99]\n```"

    # CHAL-R9-2: Site-only whitespace cleanup preserves 4-space code, nested lists, 2-space breaks, tables
    for raw_t1, expected_t1 in [
        ("Code [99]:\n\n    x = 1\n    y = 2", "Code:\n\n    x = 1\n    y = 2"),
        ("Steps [99]:\n- Item 1\n    - Nested 1a", "Steps:\n- Item 1\n    - Nested 1a"),
        ("Line 1 [99]\nLine 2  \nLine 3", "Line 1\nLine 2  \nLine 3"),
        ("Summary [99]:\n| A   | B   |\n| --- | --- |", "Summary:\n| A   | B   |\n| --- | --- |"),
    ]:
        out_md = await _run_events(
            [
                SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": raw_t1}}),
                SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
                SimpleNamespace(
                    type=EventType.RUN_COMPLETED.value,
                    data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
                ),
            ]
        )
        assert out_md == f"{expected_t1}\n\nDone."

    # CHAL-R9-3: Leading unverified citation in Turn 0 or mid-chain Turn 1 does not leave dangling space
    out_3turn = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Step 0."}}),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "[99] `config.yaml`"}}),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "get_time"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_3turn == "Step 0.\n\n`config.yaml`\n\nDone."

    out_t0_lead = await _run_events(
        [
            SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "[99] `config.yaml`"}}),
            SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
            SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
            SimpleNamespace(
                type=EventType.RUN_COMPLETED.value,
                data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
            ),
        ]
    )
    assert out_t0_lead == "`config.yaml`\n\nDone."

    # CHAL-R9-4: Case-insensitive Markdown links (HTTPS://, MAILTO:) preserved on turn rotation
    for link_t1 in (
        "See [99](HTTPS://example.com)",
        "Email [1](MAILTO:ops@example.com)",
        "Ref [2]: HTTPS://example.com",
    ):
        out_link = await _run_events(
            [
                SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": link_t1}}),
                SimpleNamespace(type=EventType.TOOL_STARTED.value, data={"payload": {"name": "web_search"}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": "Done."}}),
                SimpleNamespace(
                    type=EventType.RUN_COMPLETED.value,
                    data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
                ),
            ]
        )
        assert out_link == f"{link_t1}\n\nDone."

    # CHAL-R9-5: Post-tool exact-match trailing Unicode whitespace must NOT duplicate 2x
    for ws_ch in ("\u3000", "\u00a0", "\u2003"):
        out_pt = await _run_events(
            [
                SimpleNamespace(type=EventType.RUN_STARTED.value, data={"payload": {"grounding_required": False}}),
                SimpleNamespace(type=EventType.MESSAGE_DELTA.value, data={"payload": {"delta": f"Done.{ws_ch}"}}),
                SimpleNamespace(type=EventType.TOOL_FAILED.value, data={"payload": {"name": "web_search"}}),
                SimpleNamespace(
                    type=EventType.RUN_COMPLETED.value,
                    data={"payload": {"output": "Done.", "citations": [], "degraded": False}},
                ),
            ]
        )
        assert out_pt == f"Done.{ws_ch}"





