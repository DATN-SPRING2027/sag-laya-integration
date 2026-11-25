"""Regression tests for Milestone P1 Commit 2: search synthesis fail-closed & structured answerability."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.sag.dto import RetrievedSection
from sag_api.schemas.search import SearchResponse
from sag_api.services.evidence_service import no_answer_text
from sag_api.services.retrieval_service import (
    stream_synthesize_search_answer,
    synthesize_search_answer,
)


def _traceable_section() -> RetrievedSection:
    return RetrievedSection(
        chunk_id="su-1",
        search_unit_id="su-1",
        block_from_id="b-1",
        block_to_id="b-1",
        section_path="Security > Tokens",
        heading="Token TTL",
        content="Session access tokens expire after 24 hours by policy.",
        score=0.90,
        raw_dense_score=0.82,
        rank=0,
        source_id="src-1",
        source_config_id="cfg-1",
        canonical_evidence_verified=True,
        document_id="doc-1",
        document_version_id="ver-1",
        document_name="security.md",
        version_no=1,
        page_from=1,
        page_to=1,
        anchor="p1-b1",
    )


def _source() -> SimpleNamespace:
    return SimpleNamespace(id="src-1", name="Security Docs", sag_source_config_id="cfg-1")


@pytest.mark.asyncio
async def test_synthesize_search_answer_unconfigured_or_error_returns_extractive_fallback_degraded(monkeypatch):
    """S5-03 / S4-05: Unconfigured or failing LLM must return extractive_fallback + degraded=True, never answered."""
    sec = _traceable_section()
    src = _source()
    monkeypatch.setattr(
        "sag_api.services.retrieval_service.resolve_traceable_evidence",
        AsyncMock(return_value=[sec]),
    )

    res_unconf = await synthesize_search_answer(
        "What is the token TTL?", [sec], llm=None, sources=[src]
    )
    assert res_unconf.status == "extractive_fallback"
    assert res_unconf.degraded is True
    assert res_unconf.degraded_reason == "llm_unconfigured"

    failing_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(side_effect=RuntimeError("provider timeout")),
    )
    res_err = await synthesize_search_answer(
        "What is the token TTL?", [sec], llm=failing_llm, sources=[src]
    )
    assert res_err.status == "extractive_fallback"
    assert res_err.degraded is True
    assert res_err.degraded_reason == "llm_error:RuntimeError"


@pytest.mark.asyncio
async def test_stream_synthesize_search_answer_error_emits_delta_before_completed(monkeypatch):
    """S5-03: Stream synthesis error must emit summary.delta before completed and mark extractive_fallback."""
    sec = _traceable_section()
    src = _source()
    monkeypatch.setattr(
        "sag_api.services.retrieval_service.resolve_traceable_evidence",
        AsyncMock(return_value=[sec]),
    )

    async def failing_stream(_messages, **_kwargs):
        raise RuntimeError("upstream reset")
        yield ""  # pragma: no cover

    failing_llm = SimpleNamespace(configured=True, stream_complete=failing_stream)
    updates = [
        u
        async for u in stream_synthesize_search_answer(
            "What is the token TTL?", [sec], llm=failing_llm, sources=[src]
        )
    ]
    assert [u.kind for u in updates] == ["delta", "completed"]
    assert updates[0].text
    assert updates[1].status == "extractive_fallback"
    assert updates[1].degraded is True
    assert updates[1].degraded_reason == "llm_error:RuntimeError"


@pytest.mark.asyncio
async def test_synthesize_search_answer_structured_answerability_insufficient_evidence_and_localized_vi(monkeypatch):
    """S5-04 / S4-05: Structured insufficient_evidence triggers Vietnamese no_answer."""
    sec = _traceable_section()
    src = _source()
    monkeypatch.setattr(
        "sag_api.services.retrieval_service.resolve_traceable_evidence",
        AsyncMock(return_value=[sec]),
    )

    llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(
            return_value=json.dumps(
                {
                    "answerability": "insufficient_evidence",
                    "answer": "Tài liệu không nêu hạn mức giao dịch.",
                },
                ensure_ascii=False,
            )
        ),
    )
    vi_query = "Hạn mức giao dịch token tối đa mỗi ngày là bao nhiêu?"
    ans = await synthesize_search_answer(vi_query, [sec], llm=llm, sources=[src])
    assert ans.status == "no_answer"
    assert ans.no_answer_reason == "insufficient_evidence"
    assert ans.citations == []
    assert ans.text == no_answer_text(query=vi_query)
    assert "bằng chứng" in ans.text

    # Verify SearchResponse accepts insufficient_evidence and degraded fields (schemas/search.py:130)
    resp = SearchResponse(
        query=vi_query,
        sections=[],
        summary=ans.text,
        answer_status=ans.status,
        no_answer_reason=ans.no_answer_reason,
        degraded=ans.degraded,
        degraded_reason=ans.degraded_reason,
        stats={},
    )
    assert resp.no_answer_reason == "insufficient_evidence"


@pytest.mark.asyncio
async def test_synthesize_search_answer_rejects_uncited_answered_json_fail_closed(monkeypatch):
    """S5-03 / S5-04: answerability=answered without valid [n] citations fails closed to no_answer."""
    sec = _traceable_section()
    src = _source()
    monkeypatch.setattr(
        "sag_api.services.retrieval_service.resolve_traceable_evidence",
        AsyncMock(return_value=[sec]),
    )

    uncited_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(
            return_value=json.dumps(
                {"answerability": "answered", "answer": "Token TTL is 24 hours."}
            )
        ),
    )
    res_uncited = await synthesize_search_answer(
        "What is the token TTL?", [sec], llm=uncited_llm, sources=[src]
    )
    assert res_uncited.status == "no_answer"
    assert res_uncited.no_answer_reason == "weak_evidence"
    assert res_uncited.citations == []

    cited_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(
            return_value=(
                '<think>checking token TTL\n'
                '{"answerability": "answered", "answer": "Token TTL is 24 hours [1]."}'
            )
        ),
    )
    res_cited = await synthesize_search_answer(
        "What is the token TTL?", [sec], llm=cited_llm, sources=[src]
    )
    assert res_cited.status == "answered"
    assert res_cited.text == "Token TTL is 24 hours [1]."
    assert len(res_cited.citations) == 1

    bad_json_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(return_value="Not valid JSON output"),
    )
    res_bad = await synthesize_search_answer(
        "What is the token TTL?", [sec], llm=bad_json_llm, sources=[src]
    )
    assert res_bad.status == "extractive_fallback"
    assert res_bad.degraded is True
    assert res_bad.degraded_reason == "invalid_structured_output"
