"""TDD regression tests for P2 Step 1: shared JSON/<think> decoder, StructuredLLMClient, and S5-08."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.sag import RetrievedSection


def _section(heading: str = "H1", content: str = "Evidence body") -> RetrievedSection:
    return RetrievedSection(
        section_id="sec-1",
        document_id="doc-1",
        source_id="src-1",
        heading=heading,
        content=content,
        score=0.9,
        raw_dense_score=0.85,
    )


def test_json_parser_handles_multiple_and_unclosed_think_blocks() -> None:
    from sag_api.generation.json_parser import decode_json_object, strip_think_blocks

    multi = '<THINK>first</THINK>\n{"ok": true}\n<think>second</think>'
    assert strip_think_blocks(multi).strip() == '{"ok": true}'
    assert decode_json_object(multi) == {"ok": True}

    unclosed_with_json_after = '<think>reasoning truncated before closing\n{"winner": "A", "reason": "better"}'
    assert decode_json_object(unclosed_with_json_after) == {"winner": "A", "reason": "better"}

    unclosed_with_json_before = '{"winner": "B", "reason": "clear"}\n<think>truncated tail without close'
    assert decode_json_object(unclosed_with_json_before) == {"winner": "B", "reason": "clear"}

    unclosed_only_thought = "<think>still thinking without json"
    assert decode_json_object(unclosed_only_thought) is None


def test_json_parser_raw_decode_ignores_trailing_prose_braces() -> None:
    from sag_api.generation.json_parser import decode_json_object, decode_json_values

    tricky = 'Here is the result:\n```json\n{"a": 1, "b": "ok"}\n```\nNote: placeholder {not valid json} in prose.'
    assert decode_json_object(tricky) == {"a": 1, "b": "ok"}

    multi = 'First {"x": 1} then {"y": [2, 3]}'
    assert decode_json_values(multi) == [{"x": 1}, {"y": [2, 3]}]


def test_structured_llm_client_protocol() -> None:
    from sag_api.generation.llm import StructuredLLMClient

    class _GoodStub:
        configured = True

        async def complete(
            self,
            messages: list[dict],
            *,
            response_format: dict | None = None,
            temperature: float | None = None,
            max_tokens: int | None = None,
        ) -> str:
            del messages, response_format, temperature, max_tokens
            return "{}"

    assert isinstance(_GoodStub(), StructuredLLMClient)


@pytest.mark.asyncio
async def test_llm_judge_passes_response_format_and_returns_none_on_invalid_json() -> None:
    from sag_api.services.eval.llm_judge import judge_pairwise

    for bad_raw in (
        "not json at all",
        '{"winner": "A", "reason": "broken"} trailing {prose}',
        '{"winner": "C", "reason": "unknown option"}',
        '{"reason": "missing winner"}',
        "[]",
        "<think>truncated without json",
    ):
        complete = AsyncMock(return_value=bad_raw)
        llm = SimpleNamespace(configured=True, complete=complete)
        verdict = await judge_pairwise(llm, "query?", "s1", [_section()], "s2", [_section()])
        complete.assert_awaited_once()
        assert complete.await_args.kwargs.get("response_format") == {"type": "json_object"}
        if bad_raw.startswith('{"winner": "A"') and "trailing" in bad_raw:
            # raw_decode cleanly extracts the valid first JSON object despite trailing {prose}!
            assert verdict is not None
            assert verdict.winner == "A"
        else:
            assert verdict is None


@pytest.mark.asyncio
async def test_llm_judge_accepts_valid_fenced_and_think_json() -> None:
    from sag_api.services.eval.llm_judge import judge_pairwise

    complete = AsyncMock(
        return_value='<think>comparing</think>\n```json\n{"winner": "tie", "reason": "equal"}\n```'
    )
    llm = SimpleNamespace(configured=True, complete=complete)
    verdict = await judge_pairwise(llm, "q", "a", [_section()], "b", [_section()])
    assert verdict is not None
    assert verdict.winner == "tie"
    assert verdict.reason == "equal"


@pytest.mark.asyncio
async def test_question_scope_passes_response_format_to_non_llmclient_and_fails_to_unknown() -> None:
    from sag_api.services.question_scope import classify_question_scope

    complete = AsyncMock(return_value='{"scope": "general"}')
    stub = SimpleNamespace(configured=True, complete=complete)
    scope = await classify_question_scope(stub, "What is HTTP?")
    assert scope == "general"
    assert complete.await_args.kwargs.get("response_format") == {"type": "json_object"}

    bad_llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="not-json"))
    assert await classify_question_scope(bad_llm, "query") == "unknown"

    err_llm = SimpleNamespace(configured=True, complete=AsyncMock(side_effect=RuntimeError("boom")))
    assert await classify_question_scope(err_llm, "query") == "unknown"


@pytest.mark.asyncio
async def test_mcp_external_references_and_short_snippet_without_regex() -> None:
    from sag_api.tools.mcp import _external_references, _short_snippet

    assert _short_snippet('```json\n{"url": "https://example.com"}\n```') == ""
    assert _short_snippet("A concise human-readable excerpt.") == "A concise human-readable excerpt."

    result = SimpleNamespace(
        structuredContent=None,
        structured_content=None,
        content=[
            SimpleNamespace(
                text='Summary:\n```json\n{"results": [{"url": "https://docs.example.com/a", "title": "Doc A"}]}\n```'
            )
        ],
    )
    refs = _external_references(result)
    assert len(refs) == 1
    assert refs[0]["url"] == "https://docs.example.com/a"
    assert refs[0]["title"] == "Doc A"


@pytest.mark.asyncio
async def test_no_signature_probing_in_services_when_passing_response_format() -> None:
    from sag_api.services.handover_assist_service import _complete_json
    from sag_api.services.mind_map_service import _MIND_MAP_RESPONSE_FORMAT, _call_llm_json

    async def _strict_complete(messages, *, response_format=None):
        assert response_format is not None
        return '{"ok": true}'

    llm = SimpleNamespace(configured=True, complete=_strict_complete)
    assert await _complete_json(llm, [{"role": "user", "content": "hi"}]) == '{"ok": true}'
    assert await _call_llm_json(llm, [{"role": "user", "content": "hi"}]) == '{"ok": true}'
    assert _MIND_MAP_RESPONSE_FORMAT == {"type": "json_object"}
