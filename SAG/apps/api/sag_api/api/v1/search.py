from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import (
    get_current_user,
    get_current_user_or_connector,
    get_engine_manager,
    get_llm,
)
from sag_api.core.error_taxonomy import ErrorCode
from sag_api.core.errors import ApiError, ValidationError
from sag_api.core.logging import get_logger
from sag_api.db.models import Source, User
from sag_api.enums import SEARCH_STRATEGIES, normalize_search_strategy
from sag_api.generation import LLMClient
from sag_api.sag import EngineManager, RetrievedSection, SearchOutcome
from sag_api.schemas.insight import EntityOut, GraphRelationOut
from sag_api.schemas.search import (
    EvalCompareRequest,
    EvalCompareResponse,
    EvalJudgeOut,
    EvalStrategyResultOut,
    GlobalSearchRequest,
    SearchEventOut,
    SearchRequest,
    SearchResponse,
    SearchSourceHitOut,
    SectionOut,
)
from sag_api.services.eval.llm_judge import judge_pairwise
from sag_api.services.laya_router import CHAT_HIGH_CONFIDENCE, route_query
from sag_api.services.query_analysis import analyze_query
from sag_api.services.retrieval_service import (
    EventScoreMap,
    recall_event_scores,
    retrieve_relevant_sections,
    stream_synthesize_search_answer,
    synthesize_search_answer,
)
from sag_api.services.source_service import get_source, search_source_candidates

router = APIRouter(prefix="/sources/{source_id}/search", tags=["search"])
global_router = APIRouter(prefix="/search", tags=["search"])
log = get_logger("search")


class _EventGraphFields(TypedDict):
    events: list[SearchEventOut]
    entities: list[EntityOut]
    relations: list[GraphRelationOut]


@dataclass(slots=True)
class _QueryRoutePlan:
    strategy: str
    need_retrieval: bool
    trace: dict[str, Any]


def _query_feature_reason_codes(features: Any) -> list[str]:
    reasons: list[str] = []
    if features.exact_terms:
        reasons.append("EXACT_PHRASE")
    if features.identifier_terms:
        reasons.append("EXACT_IDENTIFIER")
    if features.path_terms:
        reasons.append("EXACT_PATH_OR_SYMBOL")
    if features.temporal_cues:
        reasons.append("TEMPORAL_CUE")
    if features.relation_cues:
        reasons.append("RELATION_CUE")
    if features.global_cues:
        reasons.append("GLOBAL_CUE")
    if features.multi_hop:
        reasons.append("MULTI_HOP_CUE")
    return reasons


def _build_query_route(
    query: str,
    source_ids: list[str] | None,
    requested_strategy: str | None,
) -> _QueryRoutePlan:
    analysis = analyze_query(query, segmentation_enabled=settings.search_chinese_segmentation_enabled)
    route_error = False
    try:
        laya = route_query(query)
    except Exception:  # noqa: BLE001 - Laya must never break retrieval
        laya = {
            "coarse_intent": "AMBIGUOUS",
            "is_chitchat": False,
            "need_retrieval": True,
            "suggested_strategy": "multi",
            "confidence": 0.0,
            "model": "fallback",
            "fallback_used": True,
            "fallback_reason": "laya_route_error",
            "reason_codes": ["LAYA_ROUTE_ERROR"],
        }
        route_error = True

    confidence = laya.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    coarse_intent = str(laya.get("coarse_intent") or "").upper()
    if not coarse_intent:
        if bool(laya.get("is_chitchat")) and confidence >= CHAT_HIGH_CONFIDENCE:
            coarse_intent = "CHAT"
        elif bool(laya.get("need_retrieval", True)):
            coarse_intent = "KNOWLEDGE"
        else:
            coarse_intent = "AMBIGUOUS"
    if coarse_intent not in {"CHAT", "KNOWLEDGE", "COMMAND", "AMBIGUOUS"}:
        coarse_intent = "AMBIGUOUS"
    if coarse_intent == "CHAT" and confidence < CHAT_HIGH_CONFIDENCE:
        coarse_intent = "AMBIGUOUS"
    need_retrieval = coarse_intent != "CHAT"

    configured_strategy = normalize_search_strategy(settings.search_strategy)
    suggested_strategy = normalize_search_strategy(str(laya.get("suggested_strategy") or configured_strategy))
    if suggested_strategy not in SEARCH_STRATEGIES:
        suggested_strategy = configured_strategy if configured_strategy in SEARCH_STRATEGIES else "vector"
    effective_strategy = normalize_search_strategy(requested_strategy or suggested_strategy)
    if effective_strategy not in SEARCH_STRATEGIES:
        effective_strategy = configured_strategy if configured_strategy in SEARCH_STRATEGIES else "vector"

    fallback_used = bool(laya.get("fallback_used")) or route_error
    fallback_reason = laya.get("fallback_reason")
    if route_error:
        fallback_reason = "laya_route_error"
    reason_codes = [str(value) for value in (laya.get("reason_codes") or [])]
    if requested_strategy:
        reason_codes.append("EXPLICIT_STRATEGY")
    reason_codes.extend(code for code in _query_feature_reason_codes(analysis.features) if code not in reason_codes)
    trace = {
        "version": "query-flow-v1",
        "query_original": query,
        "scope_source_ids": list(source_ids) if source_ids is not None else None,
        "coarse_intent": coarse_intent,
        "confidence": round(confidence, 4),
        "model": laya.get("model", "fallback"),
        "suggested_strategy": suggested_strategy,
        "requested_strategy": requested_strategy or "auto",
        "effective_strategy": effective_strategy,
        "retrieval": "skipped" if not need_retrieval else "fallback" if fallback_used else "required",
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "reason_codes": reason_codes,
        "query_analysis": {
            "normalized_phrase": analysis.normalized_phrase,
            "lookup_terms": list(analysis.lookup_terms),
            "features": analysis.features.as_dict(),
        },
    }
    return _QueryRoutePlan(
        strategy=effective_strategy,
        need_retrieval=need_retrieval,
        trace=trace,
    )


def _with_query_route_stats(
    stats: dict[str, Any],
    plan: _QueryRoutePlan,
) -> dict[str, Any]:
    merged = dict(stats)
    trace = dict(plan.trace)
    engine_fallback = bool(merged.get("fallback_used"))
    engine_effective = merged.get("effective_strategy")
    if engine_effective:
        trace["effective_strategy"] = engine_effective
    trace["retrieval_fallback_used"] = engine_fallback
    if engine_fallback:
        trace["fallback_used"] = True
        trace["retrieval"] = "fallback"
        trace["fallback_reason"] = trace["fallback_reason"] or "retrieval_strategy_fallback"
    merged["query_route"] = trace
    return merged


def _source_hits(events: list[SearchEventOut]) -> list[SearchSourceHitOut]:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    grouped: dict[str, dict] = {}
    seen: set[tuple[str, str]] = set()
    for event in events:
        if not event.source_id:
            continue
        key = (event.source_id, event.id)
        if key in seen:
            continue
        seen.add(key)
        item = grouped.setdefault(
            event.source_id,
            {
                "source_id": event.source_id,
                "source_name": event.source_name,
                "event_hits": 0,
                "max_score": 0.0,
                "latest_event_time": None,
            },
        )
        item["event_hits"] += 1
        item["max_score"] = max(float(item["max_score"]), float(event.score or 0.0))
        if event.start_time is not None:
            event_time = utc(event.start_time)
            if item["latest_event_time"] is None or event_time > item["latest_event_time"]:
                item["latest_event_time"] = event_time
    ranked = sorted(
        grouped.values(),
        key=lambda item: (
            -int(item["event_hits"]),
            -float(item["max_score"]),
            -(item["latest_event_time"].timestamp() if item["latest_event_time"] else 0.0),
            str(item["source_id"]),
        ),
    )
    return [SearchSourceHitOut(**item) for item in ranked]


async def _event_graph_fields(
    engine_manager: EngineManager,
    sections: list[RetrievedSection],
    sources_by_config: dict[str, Source],
    *,
    event_scores: EventScoreMap | None = None,
) -> _EventGraphFields:
    event_scores = event_scores or {}
    if not sections and not event_scores:
        return {"events": [], "entities": [], "relations": []}
    graph = await engine_manager.graph_for_sections(
        sections,
        sources_by_config,
        event_limit=max(1, len(sections), len(event_scores)),
        event_scores=event_scores,
    )
    events = []
    for event in graph.events:
        source = sources_by_config.get(event.source_config_id)
        events.append(
            SearchEventOut(
                id=event.id,
                source_id=source.id if source else None,
                source_name=source.name if source else None,
                title=event.title,
                summary=event.summary,
                category=event.category,
                rank=event.rank,
                parent_id=event.parent_id,
                chunk_id=event.chunk_id,
                start_time=event.start_time,
                score=event.score,
            )
        )
    return {
        "events": events,
        "entities": [EntityOut(**entity.model_dump()) for entity in graph.entities],
        "relations": [
            GraphRelationOut(
                source_id=association.event_id,
                source_kind="event",
                target_id=association.entity_id,
                target_kind="entity",
                kind="mentions",
                weight=association.weight,
                description=association.description,
            )
            for association in graph.associations
        ],
    }


@dataclass(slots=True)
class _PreparedGlobalSearch:
    sources: list[Source]
    outcome: SearchOutcome
    response: SearchResponse


async def _prepare_global_search(
    session: AsyncSession,
    engine_manager: EngineManager,
    body: GlobalSearchRequest,
) -> _PreparedGlobalSearch:
    route_plan = _build_query_route(body.query, body.source_ids, body.strategy)
    if not route_plan.need_retrieval:
        stats = _with_query_route_stats(
            {
                "sources": 0,
                "requested_strategy": route_plan.strategy,
                "effective_strategy": None,
                "fallback_used": False,
            },
            route_plan,
        )
        outcome = SearchOutcome(query=body.query, sections=[], stats=stats)
        return _PreparedGlobalSearch(
            sources=[],
            outcome=outcome,
            response=SearchResponse(query=body.query, sections=[], stats=stats),
        )

    sources = await search_source_candidates(session, body.source_ids)
    # Retrieval and answer generation can be long-running. End the read-only
    # transaction as soon as source identity has been materialized so an SSE
    # request never occupies a pooled database connection while waiting on the
    # engine or model. SessionLocal uses expire_on_commit=False.
    await session.commit()
    if not sources:
        stats = _with_query_route_stats({"sources": 0}, route_plan)
        outcome = SearchOutcome(query=body.query, sections=[], stats=stats)
        return _PreparedGlobalSearch(
            sources=[],
            outcome=outcome,
            response=SearchResponse(query=body.query, sections=[], stats=stats),
        )

    refs = {source.sag_source_config_id: source for source in sources}
    outcome, event_scores = await asyncio.gather(
        retrieve_relevant_sections(
            engine_manager,
            sources,
            body.query,
            strategy=route_plan.strategy,
            top_k=body.top_k,
        ),
        recall_event_scores(
            engine_manager,
            body.query,
            refs,
            limit=body.top_k,
        ),
    )
    graph_fields = await _event_graph_fields(
        engine_manager,
        outcome.sections,
        refs,
        event_scores=event_scores,
    )
    stats = _with_query_route_stats(
        {
            **outcome.stats,
            "event_candidates": len(event_scores),
            "event_hits": len(graph_fields["events"]),
            "event_recall": "vector+chunk" if event_scores else "chunk",
        },
        route_plan,
    )

    section_outputs = []
    for section in outcome.sections:
        source = refs.get(section.source_config_id or "")
        section_outputs.append(
            SectionOut(
                **{
                    **section.model_dump(),
                    "source_id": source.id if source else None,
                },
                source_name=source.name if source else None,
            )
        )

    return _PreparedGlobalSearch(
        sources=sources,
        outcome=SearchOutcome(query=body.query, sections=outcome.sections, stats=stats),
        response=SearchResponse(
            query=body.query,
            sections=section_outputs,
            **graph_fields,
            source_hits=_source_hits(graph_fields["events"]),
            stats=stats,
        ),
    )


async def _complete_global_search(
    session: AsyncSession,
    user: User,
    body: GlobalSearchRequest,
    prepared: _PreparedGlobalSearch,
    summary: str,
) -> SearchResponse:
    exploration_id = None
    if body.save_exploration and prepared.sources:
        from sag_api.services.universe_service import save_exploration

        response = prepared.response
        section_refs = [
            {
                "n": index,
                "chunk_id": item.chunk_id,
                "heading": item.heading,
                "score": item.score,
                "source_id": item.source_id,
                "source_name": item.source_name,
            }
            for index, item in enumerate(response.sections, 1)
        ]
        exploration, _step = await save_exploration(
            session,
            user_id=user.id,
            query=prepared.outcome.query,
            source_ids=[source.id for source in prepared.sources],
            summary=summary,
            events=[item.model_dump(mode="json") for item in response.events],
            entities=[item.model_dump(mode="json") for item in response.entities],
            relations=[item.model_dump(mode="json") for item in response.relations],
            evidence=section_refs,
        )
        exploration_id = exploration.id

    return prepared.response.model_copy(update={"summary": summary, "exploration_id": exploration_id})


def _sse(event: str, payload: dict) -> dict[str, str]:
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


@router.post("", response_model=SearchResponse)
async def search(
    source_id: str,
    body: SearchRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
) -> SearchResponse:
    source = await get_source(session, source_id)
    refs = {source.sag_source_config_id: source}
    route_plan = _build_query_route(body.query, [source_id], body.strategy)
    if route_plan.need_retrieval:
        outcome, event_scores = await asyncio.gather(
            retrieve_relevant_sections(
                engine_manager,
                [source],
                body.query,
                strategy=route_plan.strategy,
                top_k=body.top_k,
            ),
            recall_event_scores(
                engine_manager,
                body.query,
                refs,
                limit=body.top_k,
            ),
        )
        stats = _with_query_route_stats(
            {
                **outcome.stats,
                "event_candidates": len(event_scores),
            },
            route_plan,
        )
        outcome = SearchOutcome(query=body.query, sections=outcome.sections, stats=stats)
    else:
        event_scores = {}
        stats = _with_query_route_stats(
            {
                "sources": 0,
                "requested_strategy": route_plan.strategy,
                "effective_strategy": None,
                "fallback_used": False,
            },
            route_plan,
        )
        outcome = SearchOutcome(query=body.query, sections=[], stats=stats)
    for section in outcome.sections:
        section.source_config_id = section.source_config_id or source.sag_source_config_id
    graph_fields = await _event_graph_fields(
        engine_manager,
        outcome.sections,
        refs,
        event_scores=event_scores,
    )
    # 对外 source_id = sag 信源 id（可路由 / 取原文），不泄漏引擎内部 id
    return SearchResponse(
        query=body.query,
        sections=[
            SectionOut(**{**s.model_dump(), "source_id": source.id}, source_name=source.name) for s in outcome.sections
        ],
        **graph_fields,
        source_hits=_source_hits(graph_fields["events"]),
        summary=await synthesize_search_answer(
            body.query,
            outcome.sections,
            llm=llm,
        ),
        stats={
            **outcome.stats,
            "event_hits": len(graph_fields["events"]),
            "event_recall": "vector+chunk" if event_scores else "chunk",
        },
    )


@global_router.post("", response_model=SearchResponse)
async def global_search(
    request: Request,
    body: GlobalSearchRequest,
    _user: User = Depends(get_current_user_or_connector),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
) -> SearchResponse:
    """全局搜索；connector 只返回结构化证据，JWT 可生成摘要并保存探索。"""
    prepared = await _prepare_global_search(session, engine_manager, body)
    if request.state.auth_kind == "connector":
        return prepared.response.model_copy(
            update={"summary": "", "exploration_id": None},
        )
    summary = await synthesize_search_answer(
        prepared.outcome.query,
        prepared.outcome.sections,
        llm=llm,
    )
    return await _complete_global_search(
        session,
        _user,
        body,
        prepared,
        summary,
    )


@global_router.post("/stream")
async def global_search_stream(
    body: GlobalSearchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
) -> EventSourceResponse:
    """Stream a grounded summary after returning the stable retrieval result."""

    async def event_gen():
        try:
            # Run retrieval inside the response task: EventSourceResponse can
            # send keep-alive pings immediately and cancel this work as soon as
            # the browser starts a newer search or disconnects.
            prepared = await _prepare_global_search(session, engine_manager, body)
            yield _sse("result", prepared.response.model_dump(mode="json"))
            summary = ""
            async for update in stream_synthesize_search_answer(
                prepared.outcome.query,
                prepared.outcome.sections,
                llm=llm,
            ):
                if update.kind == "delta":
                    yield _sse("summary.delta", {"delta": update.text})
                else:
                    summary = update.text

            completed = await _complete_global_search(
                session,
                user,
                body,
                prepared,
                summary,
            )
            yield _sse("completed", completed.model_dump(mode="json"))
        except asyncio.CancelledError:
            # Client disconnect/new search cancellation must stop the upstream
            # model stream, not be reported as a failed search.
            raise
        except ApiError as error:
            log.warning("搜索流异常终止：%s", error.message)
            yield _sse("error", {"code": error.code, "message": error.message})
        except Exception as error:  # noqa: BLE001
            log.exception("搜索流未处理异常：%s", error)
            yield _sse(
                "error",
                {"code": ErrorCode.STREAM_ERROR, "message": "搜索生成意外中断"},
            )

    return EventSourceResponse(
        event_gen(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_one_strategy(
    engine_manager: EngineManager,
    sources: list[Source],
    query: str,
    strategy: str,
    top_k: int | None,
    source_refs: dict[str, Source],
) -> EvalStrategyResultOut:
    """一次跑一个策略,任何失败都吸掉转成 error 字段,不让 gather 把整个对比搞崩。"""
    try:
        outcome = await retrieve_relevant_sections(
            engine_manager,
            sources,
            query,
            strategy=strategy,
            top_k=top_k,
        )
    except Exception as error:  # noqa: BLE001
        log.warning("eval-compare 策略 %s 失败:%s", strategy, error)
        return EvalStrategyResultOut(
            strategy=strategy,  # type: ignore[arg-type]
            sections=[],
            stats={},
            error=getattr(error, "message", None) or str(error),
        )
    section_outputs: list[SectionOut] = []
    for section in outcome.sections:
        source = source_refs.get(section.source_config_id or "")
        section_outputs.append(
            SectionOut(
                **{
                    **section.model_dump(),
                    "source_id": source.id if source else section.source_id,
                },
                source_name=source.name if source else None,
            )
        )
    return EvalStrategyResultOut(
        strategy=strategy,  # type: ignore[arg-type]
        sections=section_outputs,
        stats=dict(outcome.stats),
    )


@global_router.post("/eval-compare", response_model=EvalCompareResponse)
async def eval_compare(
    body: EvalCompareRequest,
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
) -> EvalCompareResponse:
    """把同一 query 在多个策略下各跑一次,可选让 LLM pairwise 打分。

    - 策略里如包含当前 provider 不支持的能力,该策略结果会带 error,不影响其它策略。
    - Judge 只在 settings.eval_llm_judge_enabled 与 body.judge 同时为真且 LLM 已配置时生效。
    """
    # 去重同时保留客户端给的顺序。
    ordered_strategies: list[str] = []
    seen: set[str] = set()
    for strategy in body.strategies:
        if strategy not in seen:
            seen.add(strategy)
            ordered_strategies.append(strategy)
    if len(ordered_strategies) < 2:
        raise ValidationError("eval-compare 至少需要两个不同策略")

    sources = await search_source_candidates(session, body.source_ids)
    await session.commit()  # 释放 DB 连接;检索阶段可能长跑

    source_refs = {source.sag_source_config_id: source for source in sources}
    if not sources:
        empty_results = [
            EvalStrategyResultOut(
                strategy=strategy,  # type: ignore[arg-type]
                sections=[],
                stats={"sources": 0},
            )
            for strategy in ordered_strategies
        ]
        return EvalCompareResponse(
            query=body.query,
            results=empty_results,
            judges=[],
            judge_enabled=False,
            judge_reason="没有可检索的信源",
        )

    results = await asyncio.gather(
        *(
            _run_one_strategy(
                engine_manager,
                sources,
                body.query,
                strategy,
                body.top_k,
                source_refs,
            )
            for strategy in ordered_strategies
        )
    )

    # Pairwise judge:两两比,含 error 的一侧直接跳过。
    judge_enabled = bool(
        body.judge and settings.eval_llm_judge_enabled and llm.configured,
    )
    judge_reason: str | None = None
    if not judge_enabled:
        if not body.judge:
            judge_reason = "本次请求关闭了 judge"
        elif not settings.eval_llm_judge_enabled:
            judge_reason = "后台已关闭 eval_llm_judge_enabled"
        elif not llm.configured:
            judge_reason = "LLM 未配置,无法执行 judge"

    judges: list[EvalJudgeOut] = []
    if judge_enabled:
        raw_by_strategy = {
            result.strategy: result for result in results if result.error is None
        }
        pairs: list[tuple[str, str]] = []
        for a_index, a in enumerate(ordered_strategies):
            for b in ordered_strategies[a_index + 1 :]:
                if a in raw_by_strategy and b in raw_by_strategy:
                    pairs.append((a, b))

        async def one_pair(a: str, b: str) -> EvalJudgeOut | None:
            a_sections = [
                RetrievedSection(**section.model_dump())
                for section in raw_by_strategy[a].sections
            ]
            b_sections = [
                RetrievedSection(**section.model_dump())
                for section in raw_by_strategy[b].sections
            ]
            verdict = await judge_pairwise(
                llm,
                body.query,
                a,
                a_sections,
                b,
                b_sections,
            )
            if verdict is None:
                return None
            return EvalJudgeOut(
                a_strategy=a,  # type: ignore[arg-type]
                b_strategy=b,  # type: ignore[arg-type]
                winner=verdict.winner,
                reason=verdict.reason,
            )

        pair_verdicts = await asyncio.gather(*(one_pair(a, b) for a, b in pairs))
        judges = [verdict for verdict in pair_verdicts if verdict is not None]
        if pairs and not judges:
            judge_reason = "所有 pairwise judge 都失败,已回退空结果"

    return EvalCompareResponse(
        query=body.query,
        results=list(results),
        judges=judges,
        judge_enabled=judge_enabled,
        judge_reason=judge_reason,
    )
