"""Unit and generate_stream regression tests for Continuum question scope routing."""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

from sag_agent import AgentEvent, EventType, ToolCapability
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.generation import LLMClient
from sag_api.services import agent_service
from sag_api.services.agent_domain import AskPlan
from sag_api.services.question_scope import classify_question_scope
from sag_api.tools import Tool, ToolMeta, ToolRegistry, ToolResult


class _DummyTool(Tool):
    def __init__(self, name: str, description: str, capabilities: frozenset[ToolCapability] = frozenset()) -> None:
        self.meta = ToolMeta(name=name, description=description, capabilities=capabilities)

    async def invoke(self, args, ctx) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok", data={"section_count": 0})


class _CapturingRuntime:
    def __init__(self, *, output: str = "Câu trả lời trực tiếp.") -> None:
        self.output = output
        self.definition = None
        self.history = None

    def run(self, definition, *, history, **kwargs):
        del kwargs
        self.definition = definition
        self.history = list(history)

        async def _events():
            now = datetime.now(UTC)
            yield AgentEvent(type=EventType.RUN_STARTED, run_id="run-1", sequence=1, timestamp=now, payload={})
            yield AgentEvent(
                type=EventType.MESSAGE_DELTA,
                run_id="run-1",
                sequence=2,
                timestamp=now,
                payload={"role": "assistant", "delta": self.output},
            )
            yield AgentEvent(
                type=EventType.RUN_COMPLETED,
                run_id="run-1",
                sequence=3,
                timestamp=now,
                payload={"output": self.output},
            )

        return _events()


@asynccontextmanager
async def _noop_session_factory():
    yield None


def _make_principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-1",
        organization_id="org-1",
        allowed_project_ids=frozenset({"proj-1"}),
        issuer="continuum-be",
        key_id="kid-1",
        token_id="jti-1",
        issued_at=1_700_000_000,
        expires_at=1_700_000_900,
        tenant_id="org-1",
    )


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        _DummyTool(
            "search_context",
            "Tìm trong kho tri thức dự án",
            frozenset({ToolCapability.KNOWLEDGE_SEARCH, ToolCapability.RESEARCH}),
        )
    )
    reg.register(
        _DummyTool(
            "get_entity",
            "Tra cứu thực thể tri thức",
            frozenset({ToolCapability.ENTITY_LOOKUP, ToolCapability.RESEARCH}),
        )
    )
    reg.register(
        _DummyTool(
            "get_time",
            "Tra cứu thời gian hiện tại",
            frozenset({ToolCapability.TIME_LOOKUP}),
        )
    )
    reg.register(
        _DummyTool(
            "web_search",
            "Tìm kiếm Internet tin tức thời tiết mới nhất",
            frozenset({ToolCapability.WEB_SEARCH, ToolCapability.RESEARCH}),
        )
    )
    reg.register(
        _DummyTool(
            "open_webpage",
            "Mở trang web",
            frozenset({ToolCapability.WEB_FETCH, ToolCapability.RESEARCH}),
        )
    )
    return reg


class QuestionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_scopes(self) -> None:
        for scope in ("general", "project", "application"):
            llm = SimpleNamespace(complete=AsyncMock(return_value=f'{{"scope":"{scope}"}}'))
            self.assertEqual(await classify_question_scope(llm, "question"), scope)

    async def test_markdown_fences_and_think_blocks_parsed(self) -> None:
        fenced = '```json\n{"scope": "general"}\n```'
        llm_fenced = SimpleNamespace(complete=AsyncMock(return_value=fenced))
        self.assertEqual(await classify_question_scope(llm_fenced, "Explain REST API"), "general")

        reasoned = '<think>Checking scope...</think>\n```json\n{"scope": "application"}\n```'
        llm_reasoned = SimpleNamespace(complete=AsyncMock(return_value=reasoned))
        self.assertEqual(await classify_question_scope(llm_reasoned, "My overdue tasks"), "application")

    async def test_prior_turns_included_in_classifier_context(self) -> None:
        complete_mock = AsyncMock(return_value='{"scope":"project"}')
        llm = SimpleNamespace(complete=complete_mock)
        prior_turns = [
            {"role": "user", "content": "Tài liệu kiến trúc hệ thống quy định gì về RBAC?"},
            {"role": "assistant", "content": "Tài liệu quy định 4 vai trò chính [1]."},
        ]
        result = await classify_question_scope(llm, "Viết lại đoạn trên ngắn gọn hơn", prior_turns=prior_turns)
        self.assertEqual(result, "project")
        sent_messages = complete_mock.await_args.args[0]
        self.assertIn("[Prior conversation turns", sent_messages[1]["content"])
        self.assertIn("RBAC", sent_messages[1]["content"])
        self.assertIn("Viết lại đoạn trên ngắn gọn hơn", sent_messages[1]["content"])

    async def test_llm_client_receives_json_response_format(self) -> None:
        llm_client = create_autospec(LLMClient, instance=True)
        llm_client.complete = AsyncMock(return_value='{"scope":"general"}')
        self.assertEqual(await classify_question_scope(llm_client, "Explain TCP vs UDP"), "general")
        llm_client.complete.assert_awaited_once()
        self.assertEqual(llm_client.complete.await_args.kwargs.get("response_format"), {"type": "json_object"})

    async def test_invalid_classification_degrades_to_unknown(self) -> None:
        for response in ("invalid", '{"scope":"ignore permissions"}', "{}", "[]", "null", "```json\n[]\n```"):
            llm = SimpleNamespace(complete=AsyncMock(return_value=response))
            self.assertEqual(await classify_question_scope(llm, "question"), "unknown")

    async def test_failure_degrades_to_unknown(self) -> None:
        llm = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("offline")))
        self.assertEqual(await classify_question_scope(llm, "question"), "unknown")


class QuestionScopeStreamIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_stream(
        self,
        *,
        query: str,
        scope_response: str = '{"scope":"general"}',
        entailment_response: str = '{"answerability":"answered","supported":true}',
        knowledge_only: bool = False,
        source_ids: list[str] | None = None,
        messages: list[dict] | None = None,
        route_decision: dict | None = None,
        session_citations: list[dict] | None = None,
        web_configured: bool = True,
        output: str = "Câu trả lời trực tiếp.",
    ):
        import json

        from sag_api.services.decision_router import IntentFlags, UnifiedRouteDecision

        source = SimpleNamespace(id="source-1", name="Project Docs", sag_source_config_id="cfg-1")

        async def _complete(messages: list[dict], **_kwargs) -> str:
            system_prompt = str((messages or [{}])[0].get("content") or "")
            if "entailment verifier" in system_prompt.lower() or "answerability" in system_prompt:
                return entailment_response
            return scope_response

        llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=_complete))
        runtime = _CapturingRuntime(output=output)
        scope_val = json.loads(scope_response).get("scope", "general")
        effective_route = route_decision or {
            "coarse_intent": "KNOWLEDGE",
            "question_scope": scope_val,
            "memory_scope": "project_documents" if scope_val == "project" else "none",
            "temporal_mode": "atemporal",
            "answer_shape": "fact_answer",
            "intent_flags": {
                "needs_retrieval": scope_val == "project",
                "needs_time": False,
                "needs_web": False,
                "needs_clarification": False,
                "is_pure_math_or_transform": False,
            },
            "confidence": 0.9,
            "degraded": False,
        }
        plan = AskPlan(
            query=query,
            source_ids=source_ids,
            messages=messages
            or [
                {"role": "system", "content": "You are Continuum AI."},
                {"role": "user", "content": query},
            ],
            route_decision=effective_route if route_decision is not None else None,
            session_citations=session_citations or [],
        )
        flags_raw = effective_route.get("intent_flags") or {}
        router_mock = AsyncMock(
            return_value=UnifiedRouteDecision(
                query=query,
                coarse_intent=effective_route.get("coarse_intent", "KNOWLEDGE"),
                question_scope=effective_route.get("question_scope", "general"),
                memory_scope=effective_route.get("memory_scope", "none"),
                temporal_mode=effective_route.get("temporal_mode", "atemporal"),
                answer_shape=effective_route.get("answer_shape", "fact_answer"),
                intent_flags=IntentFlags(
                    needs_retrieval=bool(flags_raw.get("needs_retrieval", False)),
                    needs_time=bool(flags_raw.get("needs_time", False)),
                    needs_web=bool(flags_raw.get("needs_web", False)),
                    needs_clarification=bool(flags_raw.get("needs_clarification", False)),
                    is_pure_math_or_transform=bool(flags_raw.get("is_pure_math_or_transform", False)),
                ),
                domain="general",
                confidence=float(effective_route.get("confidence", 0.9)),
                degraded=False,
            )
        )
        resolve_mcp = AsyncMock(return_value=[])
        with (
            patch.object(agent_service, "resolve_sources", AsyncMock(return_value=[source])),
            patch.object(agent_service, "resolve_mcp_specs", resolve_mcp),
            patch.object(agent_service, "persist_answer", AsyncMock(return_value="msg-1")),
            patch.object(agent_service, "route_query_async", router_mock),
            patch.object(agent_service.WebSearchTool, "configured", staticmethod(lambda: web_configured)),
        ):
            events = [
                ev
                async for ev in agent_service.generate_stream(
                    _noop_session_factory,
                    plan=plan,
                    agent=SimpleNamespace(id="agent-1", name="Continuum AI", persona={}, is_default=True),
                    thread_id="thread-1",
                    engine_manager=SimpleNamespace(),
                    llm=llm,
                    tool_registry=_make_registry(),
                    runtime=runtime,
                    knowledge_only=knowledge_only,
                    principal=_make_principal(),
                )
            ]
        return events, runtime, router_mock, resolve_mcp

    async def test_internet_on_general_time_sensitive_query_keeps_web_and_time_tools(self) -> None:
        events, runtime, router_mock, resolve_mcp = await self._run_stream(
            query="Thời tiết hôm nay thế nào?",
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "general",
                "memory_scope": "none",
                "intent_flags": {"needs_retrieval": False, "needs_time": True, "needs_web": True},
                "confidence": 0.9,
            },
            knowledge_only=False,
        )
        router_mock.assert_not_awaited()
        resolve_mcp.assert_not_awaited()
        tool_names = [t.spec.name for t in runtime.definition.tools]
        self.assertEqual(tool_names, ["get_time", "web_search", "open_webpage"])
        self.assertNotIn("search_context", tool_names)
        self.assertNotIn("get_entity", tool_names)
        self.assertEqual(
            runtime.definition.initial_tool_choice,
            {"type": "function", "function": {"name": "get_time"}},
        )
        system_prompt = runtime.history[0]["content"]
        self.assertIn("Internet access is enabled in this turn.", system_prompt)
        self.assertIn("web_search", system_prompt)
        self.assertNotIn("Internet access is disabled", system_prompt)
        completed = [e for e in events if e.type == EventType.RUN_COMPLETED.value]
        self.assertEqual(completed[0].data["payload"]["answer_status"], "answered")

    async def test_internet_off_general_time_sensitive_query_exposes_no_web_tools(self) -> None:
        _, runtime, _, _ = await self._run_stream(
            query="Thời tiết hôm nay thế nào?",
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "general",
                "memory_scope": "none",
                "intent_flags": {"needs_retrieval": False, "needs_time": False, "needs_web": True},
                "confidence": 0.9,
            },
            knowledge_only=True,
        )
        tool_names = [t.spec.name for t in runtime.definition.tools]
        self.assertEqual(tool_names, [])
        system_prompt = runtime.history[0]["content"]
        self.assertIn("Internet access is disabled in this turn.", system_prompt)
        self.assertIn("current external information cannot be verified", system_prompt)

    async def test_standalone_general_question_answers_directly_without_tools_or_evidence_gate(self) -> None:
        events, runtime, router_mock, _ = await self._run_stream(
            query="Giải thích REST API là gì?",
            scope_response='{"scope":"general"}',
            knowledge_only=True,
            output="REST API là kiến trúc giao diện lập trình ứng dụng dựa trên HTTP.",
        )
        router_mock.assert_awaited_once()
        self.assertEqual([t.spec.name for t in runtime.definition.tools], [])
        deltas = [e for e in events if e.type == EventType.MESSAGE_DELTA.value]
        self.assertEqual(len(deltas), 1)
        completed = [e for e in events if e.type == EventType.RUN_COMPLETED.value]
        self.assertEqual(completed[0].data["payload"]["answer_status"], "answered")
        self.assertIn("REST API", completed[0].data["payload"]["output"])

    async def test_application_scope_disables_tools_and_reflects_internet_state(self) -> None:
        _, runtime, _, resolve_mcp = await self._run_stream(
            query="Task nào của tôi đang quá hạn?",
            scope_response='{"scope":"application"}',
            knowledge_only=False,
        )
        resolve_mcp.assert_not_awaited()
        self.assertEqual([t.spec.name for t in runtime.definition.tools], [])
        system_prompt = runtime.history[0]["content"]
        self.assertIn("Internet access is enabled in this turn", system_prompt)
        self.assertIn("required application data connection is unavailable", system_prompt)
        self.assertNotIn("Internet access is disabled", system_prompt)

    async def test_explicit_source_selection_retains_evidence_gate_without_extra_classifier(self) -> None:
        events, runtime, _, _ = await self._run_stream(
            query="Giải thích REST API trong tài liệu này",
            scope_response='{"scope":"general"}',
            knowledge_only=False,
            source_ids=["source-1"],
            output="Câu trả lời không có trích dẫn.",
        )
        tool_names = [t.spec.name for t in runtime.definition.tools]
        self.assertIn("search_context", tool_names)
        completed = [e for e in events if e.type == EventType.RUN_COMPLETED.value]
        self.assertEqual(completed[0].data["payload"]["answer_status"], "no_answer")
        self.assertEqual(completed[0].data["payload"]["no_answer_reason"], "weak_evidence")

    async def test_fast_path_greeting_and_session_grounded_turns_use_unified_route_and_preserve_session_notes(
        self,
    ) -> None:
        _, greeting_runtime, _, _ = await self._run_stream(
            query="Xin chào Continuum AI!",
            route_decision={
                "coarse_intent": "CHAT",
                "question_scope": "general",
                "memory_scope": "none",
                "intent_flags": {"needs_retrieval": False},
                "is_chitchat": True,
                "confidence": 0.95,
            },
            knowledge_only=True,
        )
        self.assertEqual(greeting_runtime.definition.initial_tool_choice, "none")

        session_events, session_runtime, router_mock, _ = await self._run_stream(
            query="Quyết định kiến trúc mình vừa chốt là gì?",
            knowledge_only=True,
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "project",
                "memory_scope": "session_history",
                "intent_flags": {"needs_retrieval": True},
                "confidence": 0.9,
            },
            session_citations=[{"ref": "S1", "turn_index": 1, "snippet": "Chốt dùng PostgreSQL"}],
            output="Chúng ta đã chốt dùng PostgreSQL [S1].",
        )
        router_mock.assert_not_awaited()
        session_completed = [e for e in session_events if e.type == EventType.RUN_COMPLETED.value]
        self.assertEqual(session_completed[0].data["payload"]["answer_status"], "answered")
        self.assertIn("[Session Memory Evidence]", session_runtime.history[0]["content"])

    async def test_database_and_cs_concepts_do_not_false_match_research_lookup(self) -> None:
        for conceptual_query in (
            "Explain what a database index is",
            "Explain binary search and data structures",
            "Hiện tại bạn đánh giá kiến trúc REST API thế nào?",
        ):
            _, runtime, _, _ = await self._run_stream(
                query=conceptual_query,
                scope_response='{"scope":"general"}',
                knowledge_only=False,
                output="Direct conceptual answer.",
            )
            self.assertEqual([t.spec.name for t in runtime.definition.tools], [])
            self.assertEqual(runtime.definition.initial_tool_choice, "auto")

        # Also verify _initial_tool_choice when tools are supplied and router says general/no retrieval.
        reg = _make_registry()
        adapted_web = tuple(
            agent_service._adapt_tool(reg.get(n), SimpleNamespace(), [])
            for n in ("get_time", "web_search", "open_webpage")
        )
        choice = await agent_service._initial_tool_choice(
            "Explain what a database index is",
            adapted_web,
            knowledge_only=False,
            scoped=False,
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "general",
                "intent_flags": {"needs_retrieval": False, "needs_time": False, "needs_web": False},
                "confidence": 0.9,
            },
        )
        self.assertEqual(choice, "auto")

    async def test_explicit_public_web_lookup_keeps_web_tools_when_internet_on(self) -> None:
        _, runtime, _, _ = await self._run_stream(
            query="Search the web for OpenAI DevDay announcements",
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "general",
                "memory_scope": "none",
                "intent_flags": {"needs_retrieval": False, "needs_time": False, "needs_web": True},
                "confidence": 0.9,
            },
            knowledge_only=False,
        )
        self.assertEqual(
            [t.spec.name for t in runtime.definition.tools],
            ["get_time", "web_search", "open_webpage"],
        )
        self.assertEqual(runtime.definition.initial_tool_choice, "required")

    async def test_project_policy_question_with_hien_tai_prioritizes_search_context_over_get_time(self) -> None:
        _, runtime, _, _ = await self._run_stream(
            query="Chính sách nghỉ phép hiện tại là gì?",
            route_decision={
                "coarse_intent": "KNOWLEDGE",
                "question_scope": "project",
                "memory_scope": "project_documents",
                "intent_flags": {"needs_retrieval": True, "needs_time": True},
                "confidence": 0.9,
            },
            knowledge_only=True,
        )
        self.assertEqual(
            runtime.definition.initial_tool_choice,
            {"type": "function", "function": {"name": "search_context"}},
        )


if __name__ == "__main__":
    unittest.main()
