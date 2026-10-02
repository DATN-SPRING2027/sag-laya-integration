"""Search SSE returns stable evidence first and a validated canonical answer last."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import UpstreamError
from sag_api.generation import LLMClient
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.retrieval_service import stream_synthesize_search_answer


class SearchEngine:
    def __init__(self, *, content="骑手技能包括路线规划和异常处理。", score=0.91):
        self.retrieval_calls = 0
        self.content = content
        self.score = score
        self.chunk_id = uuid.uuid4().hex

    async def provision(self, *_args):
        return None

    async def search_many(self, targets, query, *, strategy=None, top_k=None):
        self.retrieval_calls += 1
        return SearchOutcome(
            query=query,
            sections=[
                RetrievedSection(
                    chunk_id=self.chunk_id,
                    heading="骑手技能证据",
                    content=self.content,
                    score=self.score,
                    source_config_id=targets[0][0],
                )
            ],
            stats={"strategy": strategy, "top_k": top_k},
        )

    async def graph_for_sections(self, *_args, **_kwargs):
        raise AssertionError("P4 global retrieval must not project graph fields")

    async def search_event_scores(self, *_args, **_kwargs):
        raise AssertionError("P4 global retrieval must not recall graph events")


class EmptySearchEngine(SearchEngine):
    async def search_many(self, _targets, query, *, strategy=None, top_k=None):
        self.retrieval_calls += 1
        return SearchOutcome(query=query, sections=[], stats={"strategy": strategy, "top_k": top_k})


class StreamingLLM:
    configured = True

    def __init__(self, deltas: list[str]):
        self.deltas = deltas
        self.stream_calls = 0

    async def stream_complete(self, _messages):
        self.stream_calls += 1
        for delta in self.deltas:
            await asyncio.sleep(0)
            yield delta

    async def complete(self, _messages):  # pragma: no cover - protocol guard
        raise AssertionError("stream endpoint must not call complete()")


class FailingStreamingLLM(StreamingLLM):
    async def stream_complete(self, _messages):
        yield "未完成的内容"
        raise UpstreamError("模型连接中断")


def _events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in re.split(r"\r?\n\r?\n", body):
        event = ""
        data: list[str] = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event and data:
            events.append((event, json.loads("\n".join(data))))
    return events


async def _auth_and_source(client: httpx.AsyncClient) -> tuple[dict[str, str], str]:
    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"search-stream-{uuid.uuid4().hex}@t.com",
            "password": "password123",
        },
    )
    assert registered.status_code == 201, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    source = await client.post(
        "/api/v1/sources",
        headers=headers,
        json={"name": "流式搜索测试源"},
    )
    assert source.status_code == 201, source.text
    return headers, source.json()["id"]


async def _seed_traceable_search_unit(source_id: str, chunk_id: str = "chunk-1") -> None:
    from sag_api.core.db import SessionLocal
    from sag_api.db.models import CanonicalBlock, Document, DocumentVersion, SearchUnit, Source
    from sag_api.enums import DocumentStatus

    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        document_id = uuid.uuid4().hex
        version_id = uuid.uuid4().hex
        block_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        session.add(
            Document(
                id=document_id,
                source_id=source_id,
                filename="manual.pdf",
                content_type="application/pdf",
                size_bytes=100,
                storage_path="/tmp/manual.pdf",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=1,
                file_hash=uuid.uuid4().hex,
                status="SEARCH_READY",
                search_status="SEARCH_READY",
                search_ready_at=now,
                metadata_json={},
            )
        )
        await session.flush()
        session.add(
            CanonicalBlock(
                id=block_id,
                document_version_id=version_id,
                ordinal=0,
                block_type="paragraph",
                page_from=2,
                page_to=2,
                section_path="Approval",
                source_anchor="section:approval",
                normalized_text="Approved evidence block.",
                content_hash=uuid.uuid4().hex,
            )
        )
        await session.flush()
        session.add(
            SearchUnit(
                id=chunk_id,
                document_version_id=version_id,
                block_from_id=block_id,
                block_to_id=block_id,
                security_partition_id="project-1",
                content_hash=uuid.uuid4().hex,
                token_count=8,
                page_from=2,
                page_to=2,
                section_path="Approval",
            )
        )
        await session.commit()


async def _search(
    llm,
    *,
    request_overrides: dict | None = None,
    route_result: dict | None = None,
    engine: SearchEngine | None = None,
    traceable: bool = True,
) -> list[tuple[str, dict]]:
    from sag_api.api.v1 import search as search_api
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app

    engine = engine or SearchEngine()
    route_result = route_result or {
        "coarse_intent": "KNOWLEDGE",
        "is_chitchat": False,
        "need_retrieval": True,
        "suggested_strategy": "vector",
        "confidence": 0.99,
        "model": "fake",
        "fallback_used": False,
        "fallback_reason": None,
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(search_api, "route_query", lambda query, context=None: {**route_result, "query": query})
    app.dependency_overrides[get_engine_manager] = lambda: engine
    try:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            app.state.llm = llm
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                headers, source_id = await _auth_and_source(client)
                if traceable:
                    await _seed_traceable_search_unit(source_id, engine.chunk_id)
                response = await client.post(
                    "/api/v1/search/stream",
                    headers=headers,
                    json={
                        "query": "骑手技能",
                        "source_ids": [source_id],
                        **(request_overrides or {}),
                    },
                )
                assert response.status_code == 200, response.text
                assert response.headers["content-type"].startswith("text/event-stream")
                return _events(response.text)
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_search_stream_emits_only_canonical_citation_validated_answer():
    llm = StreamingLLM(["骑手", "需要规划能力", " [1]"])

    events = await _search(llm)

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "completed",
    ]
    initial = events[0][1]
    assert initial["summary"] == ""
    assert initial["sections"][0]["chunk_id"]
    assert initial["events"] == []
    assert initial["entities"] == []
    assert initial["relations"] == []
    assert [payload["delta"] for name, payload in events if name == "summary.delta"] == ["骑手需要规划能力 [1]"]
    completed = events[-1][1]
    assert completed["summary"] == "骑手需要规划能力 [1]"
    assert completed["sections"] == initial["sections"]
    assert completed["answer_status"] == "answered"
    citation = completed["citations"][0]
    assert citation["source_id"]
    assert citation["document_id"]
    assert citation["document_version_id"]
    assert citation["chunk_id"] == initial["sections"][0]["chunk_id"]
    assert citation["page_from"] == 2
    assert citation["page_to"] == 2
    assert citation["anchor"] == "section:approval"
    assert llm.stream_calls == 1


@pytest.mark.asyncio
async def test_search_stream_replaces_invalid_citations_with_grounded_fallback():
    events = await _search(StreamingLLM(["不存在的引用 [9]"]))

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "completed",
    ]
    assert "[9]" not in events[1][1]["delta"]
    canonical = events[-1][1]["summary"]
    assert "骑手技能包括路线规划和异常处理" in canonical
    assert "[1]" in canonical
    assert "[9]" not in canonical


@pytest.mark.asyncio
async def test_search_stream_provider_failure_completes_with_grounded_fallback():
    events = await _search(FailingStreamingLLM([]))

    assert [name for name, _payload in events] == ["result", "completed"]
    completed = events[-1][1]
    assert "[1]" in completed["summary"]
    assert completed["answer_status"] == "answered"
    assert completed["no_answer_reason"] is None
    assert completed["citations"][0]["chunk_id"]
    assert completed["citations"][0]["source_id"]


@pytest.mark.asyncio
async def test_search_stream_discards_partial_provider_output_after_failure():
    class PartialThenFailingLLM:
        configured = True

        async def stream_complete(self, _messages):
            yield "未完成的内容 [9]"
            raise RuntimeError("provider disconnected")

    events = await _search(PartialThenFailingLLM())

    assert [name for name, _payload in events] == ["result", "completed"]
    completed = events[-1][1]
    assert "未完成的内容" not in completed["summary"]
    assert "[9]" not in completed["summary"]
    assert "[1]" in completed["summary"]
    assert completed["answer_status"] == "answered"
    assert completed["no_answer_reason"] is None
    assert completed["citations"][0]["chunk_id"]
    assert completed["citations"][0]["source_id"]


@pytest.mark.asyncio
async def test_search_stream_skips_retrieval_for_high_confidence_chat():
    llm = StreamingLLM(["không được gọi"])
    events = await _search(
        llm,
        request_overrides={"query": "Xin chào"},
        route_result={
            "coarse_intent": "CHAT",
            "is_chitchat": True,
            "need_retrieval": False,
            "suggested_strategy": "vector",
            "confidence": 0.99,
            "model": "fake",
            "fallback_used": False,
            "fallback_reason": None,
        },
    )

    assert [name for name, _payload in events] == ["result", "completed"]
    assert events[0][1]["query"] == "Xin chào"
    assert events[0][1]["sections"] == []
    assert events[0][1]["stats"]["query_route"]["retrieval"] == "skipped"


@pytest.mark.asyncio
async def test_search_stream_returns_no_answer_for_weak_legacy_evidence_without_calling_llm():
    llm = StreamingLLM(["must not appear"])
    events = await _search(llm, traceable=False)

    assert [name for name, _payload in events] == ["result", "completed"]
    completed = events[-1][1]
    assert completed["answer_status"] == "no_answer"
    assert completed["no_answer_reason"] == "weak_evidence"
    assert completed["citations"] == []
    assert "足够且可追溯" in completed["summary"]
    assert llm.stream_calls == 0


@pytest.mark.asyncio
async def test_search_stream_returns_no_answer_for_empty_evidence():
    llm = StreamingLLM(["must not appear"])
    events = await _search(llm, engine=EmptySearchEngine())

    assert [name for name, _payload in events] == ["result", "completed"]
    completed = events[-1][1]
    assert completed["answer_status"] == "no_answer"
    assert completed["no_answer_reason"] == "empty_evidence"
    assert completed["citations"] == []
    assert llm.stream_calls == 0


@pytest.mark.asyncio
async def test_exact_identifier_survives_rrf_single_retriever_score_and_ambiguous_route():
    llm = StreamingLLM(["XK-204 is approved [1]"])
    events = await _search(
        llm,
        request_overrides={"query": "XK-204"},
        engine=SearchEngine(content="Release identifier XK-204 is approved.", score=0.5),
        route_result={
            "coarse_intent": "AMBIGUOUS",
            "is_chitchat": False,
            "need_retrieval": True,
            "suggested_strategy": "multi",
            "confidence": 0.41,
            "model": "fake",
            "fallback_used": False,
            "fallback_reason": "ambiguous_context",
        },
    )

    completed = events[-1][1]
    assert completed["answer_status"] == "answered"
    assert "XK-204 is approved [1]" in completed["summary"]
    assert completed["stats"]["query_route"]["retrieval"] == "required"


@pytest.mark.asyncio
async def test_search_stream_emits_terminal_error_when_completion_cannot_be_saved(monkeypatch):
    from sag_api.services import universe_service

    async def fail_save(*_args, **_kwargs):
        raise UpstreamError("探索保存失败")

    monkeypatch.setattr(universe_service, "save_exploration", fail_save)
    events = await _search(
        StreamingLLM(["有效答案 [1]"]),
        request_overrides={"save_exploration": True},
    )

    assert [name for name, _payload in events] == [
        "result",
        "summary.delta",
        "error",
    ]
    assert events[-1][1] == {
        "code": "upstream_error",
        "message": "探索保存失败",
    }


@pytest.mark.asyncio
async def test_search_answer_stream_propagates_cancellation_and_closes_provider():
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingLLM:
        configured = True

        async def stream_complete(self, _messages):
            try:
                yield "部分"
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    sections = [
        RetrievedSection(
            chunk_id="chunk-1",
            heading="骑手技能",
            content="骑手需要路线规划能力。",
            score=0.9,
            source_id="engine-source",
            source_config_id="source-1",
            document_id="document-1",
            document_version_id="version-1",
            page_from=1,
            page_to=1,
            anchor="section:rider",
        )
    ]
    source = SimpleNamespace(id="source-public", name="手册", sag_source_config_id="source-1")

    async def consume() -> None:
        async for _update in stream_synthesize_search_answer(
            "骑手技能",
            sections,
            llm=BlockingLLM(),
            sources=[source],
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_llm_plain_text_stream_closes_upstream_on_cancellation(monkeypatch):
    entered = asyncio.Event()

    class ProviderStream:
        closed = False

        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="部分"))])
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    provider_stream = ProviderStream()

    async def fake_completion(**_kwargs):
        return provider_stream

    monkeypatch.setattr("sag_api.generation.llm._litellm_completion", fake_completion)
    llm = LLMClient(
        Settings(
            _env_file=None,
            llm_api_key="test-key",
            llm_model="test-model",
            llm_temperature=0,
            llm_max_tokens=128,
            llm_extra_body=None,
        )
    )

    async def consume() -> None:
        async for _delta in llm.stream_complete([{"role": "user", "content": "test"}]):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider_stream.closed is True
