"""Regression tests for Milestone P1 Commit 4: compound citations & tool_call_id visibility (S5-01, S5-09)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_agent.runtime import CancellationToken
from sag_api.services import agent_service
from sag_api.tools.base import ToolContext, ToolMeta, ToolResult


def _cit(n: int) -> dict:
    return {
        "n": n,
        "source_id": f"src-{n}",
        "chunk_id": f"chk-{n}",
        "document_id": f"doc-{n}",
        "document_version_id": f"ver-{n}",
        "page_from": 1,
        "page_to": 1,
        "anchor": f"p1-b{n}",
        "snippet": f"Evidence {n}",
    }


def test_finalize_answer_citations_filters_compound_brackets_individually():
    """S5-01: Compound numeric brackets [1, 99] -> [1], [98, 99] -> '', [s1, 99] -> [s1]."""
    pool = [_cit(1)]
    raw = "Claim A [1, 99]. Claim B [98, 99]. Session [s1, 99]."
    text, used = agent_service._finalize_answer_citations(raw, pool)
    assert "[1]" in text
    assert "99" not in text
    assert "98" not in text
    assert "[s1]" in text
    assert [c["n"] for c in used] == [1]


@pytest.mark.asyncio
async def test_adapt_tool_and_transform_context_track_citations_by_tool_call_id(monkeypatch):
    """S5-09: Footnote [1] inside surviving call-2 prose must NOT keep dropped call-1 citation [1] visible."""
    c1 = _cit(1)
    c2 = _cit(2)
    c3 = _cit(3)

    class StubSearchTool:
        meta = ToolMeta(name="search_context", description="search")

        async def invoke(self, args, _ctx):
            if args["q"] == "first":
                return ToolResult(content="[1] First chunk", citations=[c1], data={"section_count": 1})
            return ToolResult(
                content="[2] Second chunk citing historical footnote [1].",
                citations=[c2],
                data={"section_count": 1},
            )

    class StubEntityTool:
        meta = ToolMeta(name="get_entity", description="entity")

        async def invoke(self, _args, _ctx):
            return ToolResult(content="[3] Entity chunk", citations=[c3], data={"section_count": 1})

    host_ctx = ToolContext(
        engine_manager=SimpleNamespace(),
        sources=[SimpleNamespace(id="s1", name="S1", sag_source_config_id="c1")],
    )
    citations: list[dict] = []
    adapted_search = agent_service._adapt_tool(StubSearchTool(), host_ctx, citations)
    adapted_entity = agent_service._adapt_tool(StubEntityTool(), host_ctx, citations)

    def rt_ctx(call_id: str):
        return SimpleNamespace(
            tool_call_id=call_id,
            cancellation=CancellationToken(),
            _on_progress=AsyncMock(),
        )

    await adapted_search.execute({"q": "first"}, rt_ctx("call-1"))
    await adapted_search.execute({"q": "second"}, rt_ctx("call-2"))
    await adapted_entity.execute({"name": "Alice"}, rt_ctx("call-3"))
    assert [c["n"] for c in citations] == [1, 2, 3]

    # Simulate _fit_agent_context pruning call-1 while keeping call-2 (prose contains "[1]") and call-3 (get_entity).
    fitted_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {
            "role": "tool",
            "name": "search_context",
            "tool_call_id": "call-2",
            "content": "[2] Second chunk citing historical footnote [1].",
        },
        {
            "role": "tool",
            "name": "get_entity",
            "tool_call_id": "call-3",
            "content": "[3] Entity chunk",
        },
    ]
    monkeypatch.setattr(
        agent_service,
        "_fit_agent_context",
        lambda *_args, **_kwargs: (fitted_messages, 4096),
    )

    visible = agent_service._filter_visible_citations_for_context(
        fitted_messages, citations, host_ctx
    )
    assert [c["n"] for c in visible] == [2, 3]
