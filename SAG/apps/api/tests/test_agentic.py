"""Agentic 基建：默认工具、全局证据编号、历史压缩、token 估算。全离线。"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from sag_agent import AgentTool, ToolResult, ToolSpec
from sag_api.generation.prompt import build_agent_messages, build_prompt_preview, estimate_tokens
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.agent_domain import compress_history
from sag_api.services.agent_service import (
    _append_current_scene,
    _build_external_citations,
    _enabled_tool_names,
    _finalize_answer_citations,
    _fit_agent_context,
    _initial_tool_choice,
)
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool


class _A:
    def __init__(self, is_default=False, tools=None):
        self.is_default = is_default
        self.persona = {"tools": tools} if tools is not None else {}


def test_default_agent_gets_builtin_tools():
    assert _enabled_tool_names(_A(is_default=True)) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=False)) == [
        "get_time",
        "web_search",
        "open_webpage",
    ]
    assert _enabled_tool_names(_A(is_default=True, tools=["echo"])) == [
        "get_time",
        "search_context",
        "get_entity",
        "web_search",
        "open_webpage",
        "echo",
    ]
    assert _enabled_tool_names(_A(is_default=True), knowledge_only=True) == [
        "get_time",
        "search_context",
        "get_entity",
    ]


def test_estimate_tokens_cjk_aware():
    assert estimate_tokens("你好世界") == 4  # CJK 每字 1
    assert estimate_tokens("abcdefgh") == 2  # ASCII 每 4 字符 1
    assert estimate_tokens("") == 0


def test_agent_context_budget_counts_prompt_schemas_reserves_and_keeps_tool_turns_atomic():
    messages = [
        {"role": "system", "content": "SAG system instructions."},
        {"role": "user", "content": "old history " * 400},
        {"role": "user", "content": "Find XK-204."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search_context", "arguments": "{\"query\":\"XK-204\"}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "search_context",
            "tool_call_id": "call-1",
            "content": "[1] XK-204 approved. Locator: version=v1 page=2 anchor=approval.",
        },
    ]
    schemas = [{"name": "search_context", "parameters": {"query": "string"}}]
    window = 500
    output_reserve = 80

    fitted, evidence_budget = _fit_agent_context(
        messages,
        schemas,
        context_window_tokens=window,
        reserved_output_tokens=output_reserve,
    )

    fitted_tokens = estimate_tokens(json.dumps({"messages": fitted, "tools": schemas}, separators=(",", ":")))
    assert fitted[0]["role"] == "system"
    assert any(message.get("content") == "Find XK-204." for message in fitted)
    assert not any("old history" in str(message.get("content")) for message in fitted)
    assert any(message.get("tool_call_id") == "call-1" for message in fitted)
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in fitted)
    assert fitted_tokens + evidence_budget + output_reserve * 2 <= window


def test_agent_prompt_uses_static_timezone_rule_and_time_tool_guidance():
    messages = build_agent_messages(
        "测试助手",
        {},
        "现在几点",
        timezone="Asia/Shanghai",
    )
    system = messages[0]["content"]
    assert "Asia/Shanghai" in system
    assert "get_time" in system
    assert "当前日期和时间是动态事实" in system
    assert "绝对日期、合适的时间窗口和查询对象" in system
    assert "不得沿用旧对话、模型记忆或用户示例中的年份" in system


def test_agent_prompt_guides_clarification_progress_and_delivery_in_both_languages():
    zh = build_agent_messages("测试助手", {}, "推荐一下")[0]["content"]
    en = build_agent_messages(
        "Test Assistant",
        {},
        "Recommend something",
        language="en",
        timezone="UTC",
    )[0]["content"]

    assert "实质改变结论或交付物" in zh
    assert "合理默认值" in zh
    assert "可直接使用的结果" in zh
    assert "不要输出冗长的内部思维过程" in zh
    assert "官方公告、产品文档、原始数据" in zh
    assert "至少两个相互独立的来源交叉核验" in zh
    assert "关键外部事实就必须在对应论断附近附上可点击的直接来源" in zh
    assert "get_entity 做实体消歧和形成后续检索词" in zh
    assert "寒暄、致谢、告别、身份询问应直接回答，不调用检索" in zh
    assert "不能用搜索代替澄清" in zh
    assert "materially change the conclusion or deliverable" in en
    assert "reasonable assumptions and proceed" in en
    assert "directly usable result" in en
    assert "Do not expose lengthy hidden reasoning" in en
    assert "first-party announcements, product documentation, original data" in en
    assert "at least two independent sources" in en
    assert "clickable direct source near each key external claim" in en
    assert "get_entity only to disambiguate entities" in en
    assert "Answer greetings, thanks, farewells, and identity questions directly" in en
    assert "search is not a substitute for clarification" in en


def test_search_context_description_has_explicit_non_retrieval_boundary():
    description = SearchContextTool.meta.description

    assert "仅当回答依赖已挂载知识库" in description
    assert "不要用于寒暄、致谢、身份询问、纯创作、简单计算" in description
    assert "不能用检索代替澄清" in description


def test_agent_messages_keep_system_history_and_current_user_separate():
    messages = build_agent_messages(
        "测试助手",
        {},
        "当前问题",
        history=[
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
        ],
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "历史回答" not in messages[0]["content"]
    preview = build_prompt_preview(messages)
    assert "【系统指令】" in preview
    assert "【历史 · 助手】\n历史回答" in preview
    assert "【当前问题】\n当前问题" in preview


def test_dynamic_scene_stays_inside_single_system_message():
    messages = build_agent_messages(
        "测试助手",
        {},
        "查知识库",
        history=[{"role": "assistant", "content": "历史回答"}],
    )
    with_scene = _append_current_scene(messages, ["限定本地知识库", "限定产品资料"])

    assert [message["role"] for message in with_scene] == ["system", "assistant", "user"]
    assert sum(message["role"] == "system" for message in with_scene) == 1
    assert "【当前场景】" in with_scene[0]["content"]
    assert "限定本地知识库" in with_scene[0]["content"]
    assert with_scene[1]["content"] == "历史回答"
    assert messages[0]["content"] != with_scene[0]["content"]


def test_initial_tool_policy_anchors_time_and_preserves_clarification(monkeypatch):
    from sag_api.services import laya_router

    monkeypatch.setattr(
        laya_router,
        "route_query",
        lambda _query: {"is_chitchat": False, "confidence": 0.0},
    )

    async def execute(arguments, context):
        return ToolResult(content="ok")

    search = AgentTool(
        ToolSpec(name="search_context", description="检索知识库"),
        execute,
    )
    clock = AgentTool(ToolSpec(name="get_time", description="查询时间"), execute)
    tools = (clock, search)

    named_time = {"type": "function", "function": {"name": "get_time"}}
    named_search = {"type": "function", "function": {"name": "search_context"}}

    assert (
        _initial_tool_choice(
            "最近 Agent 有什么发展？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "最近 ChatGPT 有哪些更新？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "过去三个月 ChatGPT 有哪些更新？",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "What changed last week?",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == named_time
    )
    assert (
        _initial_tool_choice(
            "你好",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "你好",
            tools,
            knowledge_only=False,
            scoped=True,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "推荐一下",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "最近怎么样",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "上周怎么样",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "(2 + 3) * 4 = ?",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "总结知识库里的发布流程",
            tools,
            knowledge_only=True,
            scoped=False,
        )
        == named_search
    )
    assert (
        _initial_tool_choice(
            "请搜索并核实这项数据",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "required"
    )
    assert (
        _initial_tool_choice(
            "你好",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "none"
    )
    assert (
        _initial_tool_choice(
            "把这段话润色一下",
            tools,
            knowledge_only=False,
            scoped=False,
        )
        == "auto"
    )


@pytest.mark.parametrize(
    "query",
    ["你好！", "Hello", "在吗？", "谢谢你", "你是谁呀？", "What's your name?"],
)
def test_high_confidence_social_intents_disable_tools(query):
    async def execute(arguments, context):
        return ToolResult(content="ok")

    tools = (
        AgentTool(ToolSpec(name="get_time", description="查询时间"), execute),
        AgentTool(ToolSpec(name="search_context", description="检索知识库"), execute),
    )

    assert (
        _initial_tool_choice(query, tools, knowledge_only=True, scoped=False)
        == "none"
    )


def test_answer_citations_are_canonical_and_traceable():
    def citation(n, *, chunk_id, source_id, heading):
        return {
            "n": n,
            "chunk_id": chunk_id,
            "source_id": source_id,
            "heading": heading,
            "document_id": f"doc-{n}",
            "document_version_id": f"version-{n}",
            "page_from": 1,
            "page_to": 1,
            "anchor": f"section-{n}",
        }

    citations = [
        citation(1, chunk_id="chunk-1", source_id="source-1", heading="一"),
        citation(2, chunk_id=None, source_id="source-1", heading="不可打开"),
        citation(3, chunk_id="chunk-3", source_id="source-3", heading="三"),
    ]

    answer, used = _finalize_answer_citations("结论 [1]，虚构 [9]，坏引用 [2]。", citations)
    assert answer == "结论 [1]，虚构，坏引用。"
    assert [citation["n"] for citation in used] == [1]
    assert used[0]["kind"] == "internal"
    assert used[0]["mapped"] is True
    assert used[0]["claim_level"] == "claim"

    uncited, fallback = _finalize_answer_citations("模型忘了引用。", citations)
    assert uncited == "模型忘了引用。"
    assert [citation["n"] for citation in fallback] == [1, 3]
    assert all(citation["kind"] == "internal" for citation in fallback)
    assert all(citation["mapped"] is False for citation in fallback)
    assert all(citation["claim_level"] == "run" for citation in fallback)

    external_link = "外部来源 [9](https://example.com/release)。"
    preserved, none = _finalize_answer_citations(external_link, [])
    assert preserved == external_link
    assert none == []


def test_external_citations_are_safe_deduplicated_bounded_and_mapping_aware():
    references = [
        {
            "title": "Official release",
            "url": "HTTPS://Example.COM/release#details",
            "source": "OpenAI",
            "description": "  Product   update details.  ",
        },
        {"title": "duplicate", "url": "https://example.com/release"},
        {"title": "bad scheme", "url": "javascript:alert(1)"},
        {"title": "credentials", "url": "https://user:secret@example.com/private"},
        {"title": "whitespace", "url": "https://example.com/a b"},
        *[
            {"title": f"Result {index}", "url": f"https://source{index}.example/article"}
            for index in range(20)
        ],
    ]

    citations = _build_external_citations(
        "结论见 https://example.com/release。",
        references,
        start_n=6,
    )

    assert len(citations) == 12
    assert citations[0] == {
        "kind": "external",
        "n": 6,
        "url": "https://example.com/release",
        "title": "Official release",
        "source": "OpenAI",
        "mapped": True,
        "claim_level": "claim",
        "summary": "Product update details.",
        "snippet": "Product update details.",
    }
    assert citations[1]["n"] == 7
    assert citations[1]["mapped"] is False
    assert citations[1]["claim_level"] == "run"
    assert not any("javascript:" in citation["url"] for citation in citations)
    assert not any("secret" in citation["url"] for citation in citations)


@pytest.mark.asyncio
async def test_search_tool_uses_global_citation_offset(monkeypatch):
    from sag_api.services import search_unit_retrieval_service

    routing_trace = {"request_snapshot_id": "snapshot-test", "fallback_used": True}

    async def retrieve_canonical(_engine, sources, query, *, principal, top_k=None, query_strategy_plan=None):
        assert query_strategy_plan is not None
        assert principal.tenant_id == "tenant-c1"
        source = sources[0]
        return SearchOutcome(
            query=query,
            stats={"routing": routing_trace},
            sections=[
                RetrievedSection(
                    heading="标题",
                    content="内容",
                    chunk_id="c1",
                    search_unit_id="c1",
                    source_id=source.id,
                    source_config_id=source.sag_source_config_id,
                    score=0.9,
                    canonical_evidence_verified=True,
                    content_hash=hashlib.sha256("内容".encode()).hexdigest(),
                    document_id="doc-c1",
                    document_version_id="version-c1",
                    page_from=1,
                    page_to=1,
                    anchor="section-c1",
                    block_from_id="block-a",
                    block_to_id="block-a",
                    section_path="章节一",
                )
            ],
        )

    monkeypatch.setattr(search_unit_retrieval_service, "retrieve_search_unit_sections", retrieve_canonical)

    class _EM:
        async def graph_for_sections(self, *_args, **_kwargs):
            raise AssertionError("search_context must not depend on knowledge enrichment")

    class _Src:
        sag_source_config_id = "scid"
        id = "sid"
        name = "源"

    ctx = ToolContext(
        engine_manager=_EM(),
        sources=[_Src()],
        principal=SimpleNamespace(tenant_id="tenant-c1", allowed_partition_ids=frozenset({"p-c1"})),
        citation_offset=3,
        evidence_token_budget=10_000,
    )
    result = await SearchContextTool().invoke({"query": "q"}, ctx)
    assert "[4]" in result.content  # 编号从 offset+1 开始
    assert result.citations[0]["n"] == 4
    assert result.citations[0]["document_version_id"] == "version-c1"
    assert result.citations[0]["block_from_id"] == "block-a"
    assert result.citations[0]["section_path"] == "章节一"
    assert result.data["evidence_status"] == "sufficient"
    assert result.data["routing"] == routing_trace
    assert "_graph" in result.data


@pytest.mark.asyncio
async def test_agent_tool_event_details_include_search_routing_trace():
    from sag_api.services.agent_service import _adapt_tool
    from sag_api.tools.base import ToolMeta
    from sag_api.tools.base import ToolResult as HostToolResult

    trace = {"request_snapshot_id": "snapshot-test", "fallback_used": True}

    class HostSearchTool:
        meta = ToolMeta(
            name="search_context",
            description="search",
            parameters={"type": "object", "properties": {}},
        )

        async def invoke(self, _arguments, _context):
            return HostToolResult(
                content="No supported evidence.",
                data={"section_count": 0, "routing": trace, "no_answer_reason": "empty_evidence"},
            )

    host_context = ToolContext(engine_manager=None)
    tool = _adapt_tool(HostSearchTool(), host_context, [])
    runtime_context = SimpleNamespace(
        cancellation=SimpleNamespace(raise_if_cancelled=lambda: None),
    )

    result = await tool.executor({"query": "question"}, runtime_context)

    assert result.details["routing"] == trace
    assert host_context.search_context_no_answer_reason == "empty_evidence"


@pytest.mark.asyncio
async def test_compress_history_trims_without_llm():
    history = [{"role": "user", "content": "字" * 200} for _ in range(10)]
    out = await compress_history(history, llm=None, budget_tokens=500)
    assert out and out == history[-len(out) :]  # 尾部保留
    assert sum(estimate_tokens(m["content"]) for m in out) <= 500 + 200
    # 预算内不动
    same = await compress_history(history[:2], llm=None, budget_tokens=10_000)
    assert same == history[:2]


@pytest.mark.asyncio
async def test_grounded_agent_stream_emits_only_the_canonical_answer_delta(monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    from sag_agent import AgentEvent, EventType
    from sag_api.services import agent_service
    from sag_api.services.evidence_service import no_answer_text

    now = datetime.now(UTC)

    class Handle:
        done = True

        def __aiter__(self):
            async def events():
                yield AgentEvent(EventType.RUN_STARTED, "run-1", 1, now)
                yield AgentEvent(
                    EventType.MESSAGE_DELTA,
                    "run-1",
                    2,
                    now,
                    payload={"role": "assistant", "delta": "Unsupported answer [9]"},
                )
                yield AgentEvent(
                    EventType.MESSAGE_COMPLETED,
                    "run-1",
                    3,
                    now,
                    payload={"role": "assistant", "message": {"role": "assistant"}},
                )
                yield AgentEvent(
                    EventType.RUN_COMPLETED,
                    "run-1",
                    4,
                    now,
                    payload={"output": "Unsupported answer [9]"},
                )

            return events()

    class Runtime:
        def run(self, *_args, **_kwargs):
            return Handle()

    class SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    @asynccontextmanager
    async def no_mcp_tools(_specs):
        yield SimpleNamespace(tools=[], warnings=[])

    async def no_sources(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent_service, "_enabled_tool_names", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(agent_service, "resolve_sources", no_sources)
    monkeypatch.setattr(agent_service, "open_agent_mcp_tools", no_mcp_tools)

    streamed = [
        event
        async for event in agent_service.generate_stream(
            SessionFactory(),
            plan=SimpleNamespace(
                citations=[],
                source_ids=None,
                messages=[{"role": "user", "content": "What is the fact?"}],
                query="What is the fact?",
                user_message_id="user-1",
            ),
            agent=SimpleNamespace(name="Test", id="agent-1", persona={}),
            thread_id=None,
            engine_manager=SimpleNamespace(),
            llm=SimpleNamespace(),
            tool_registry=SimpleNamespace(),
            runtime=Runtime(),
            knowledge_only=True,
        )
    ]

    deltas = [event for event in streamed if event.type == EventType.MESSAGE_DELTA.value]
    completed = next(event for event in streamed if event.type == EventType.RUN_COMPLETED.value)
    canonical_answer = completed.data["payload"]["output"]

    assert len(deltas) == 1
    assert deltas[0].data == {
        "type": EventType.MESSAGE_DELTA.value,
        "run_id": "run-1",
        "sequence": 4,
        "payload": {"role": "assistant", "delta": no_answer_text()},
    }
    assert deltas[0].data["payload"]["delta"] == no_answer_text()
    assert deltas[0].data["payload"]["delta"] == canonical_answer
    assert [event.type for event in streamed[-2:]] == [EventType.MESSAGE_DELTA.value, EventType.RUN_COMPLETED.value]
    assert [event.data["sequence"] for event in streamed[-2:]] == [4, 5]
