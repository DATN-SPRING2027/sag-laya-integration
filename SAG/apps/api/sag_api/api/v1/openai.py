"""OpenAI 兼容对话端点——把任意 Agent 当作一个「带引用的模型」调用。

    POST /api/v1/openai/{agent_id}/chat/completions
    Authorization: Bearer <sag JWT>

支持 stream / 非流两种；请求体沿用 OpenAI Chat Completions 结构。
检索、系统提示、防幻觉短路与站内对话完全一致，便于外部系统无缝接入。
"""

from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime, EventType
from sag_api.core.config import settings
from sag_api.core.conversation_ownership import get_conversation_owner
from sag_api.core.db import SessionLocal, get_session
from sag_api.core.deps import (
    get_agent_runtime,
    get_current_user,
    get_engine_manager,
    get_llm,
    get_tool_registry,
)
from sag_api.core.errors import ConfigurationError, UpstreamError, ValidationError
from sag_api.core.principal_assertion import VerifiedPrincipal, require_principal_assertion
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.sag import EngineManager
from sag_api.services import agent_domain as svc
from sag_api.services import agent_service
from sag_api.services.decision_router import route_query_async
from sag_api.tools import ToolRegistry

router = APIRouter(prefix="/openai", tags=["openai"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = None
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)


def _split_query(messages: list[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    """取最后一条 user 消息为本轮问题，其余 user/assistant 作为历史。"""
    last_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), None)
    if last_user is None:
        raise ValidationError("messages 中缺少 user 消息")
    query = messages[last_user].content
    history = [
        {"role": m.role, "content": m.content}
        for idx, m in enumerate(messages)
        if idx != last_user and m.role in ("user", "assistant")
    ]
    return query, history


_COMPLETE_CITATION_BRACKET_RE = re.compile(
    r"[ \t]*\[(\s*[sS]?\d+(?:\s*[,;、]\s*[sS]?\d+)*\s*)\](?!\(\s*(?:https?:|mailto:)|:\s*(?:https?:|mailto:))"
)
_INCOMPLETE_CITATION_TAIL_RE = re.compile(
    r"[ \t]*\[\s*(?:(?:[sS]?\d+(?:\s*[,;、]\s*[sS]?\d+)*\s*[,;、]\s*(?:[sS]?\d*)?)|(?:[sS]?\d*)|[sS])?\s*$"
)
_LINK_PREFIXES = ("(http:", "(https:", "(mailto:", ": http:", ": https:", ": mailto:", ":http:", ":https:", ":mailto:")


def _is_zero_math_bracket(inner: str) -> bool:
    items = [part.strip() for part in re.split(r"[,;、]", inner) if part.strip()]
    if not items:
        return False
    if any(item[:1].upper() == "S" and item[1:].isdigit() for item in items):
        return False
    return any(item.isdigit() and int(item) == 0 for item in items)


def _has_partial_link_tail(after: str) -> bool:
    if not after:
        return False
    lowered = after.lower()
    return any(prefix.startswith(lowered) for prefix in _LINK_PREFIXES)


def _find_closing_backticks(text: str, start: int, run_len: int) -> int:
    para_end = text.find("\n\n", start)
    limit = len(text) if para_end == -1 else para_end
    pos = start
    while pos < limit:
        idx = text.find("`", pos, limit)
        if idx == -1:
            return -1
        end = idx
        while end < limit and text[end] == "`":
            end += 1
        if end - idx == run_len:
            return idx
        pos = end
    return -1


def _prose_segments(text: str) -> tuple[list[tuple[int, int]], int | None]:
    """Return non-code prose spans and optional start index of an unclosed backtick span containing '['."""
    segments: list[tuple[int, int]] = []
    prose_start = 0
    index = 0
    length = len(text)
    code_fence: str | None = None
    unclosed_code_bracket_start: int | None = None

    while index < length:
        line_start = index == 0 or text[index - 1] == "\n"
        if line_start:
            fence_match = re.match(r"^( {0,3})(`{3,}|~{3,})", text[index:])
            if fence_match:
                marker = fence_match.group(2)
                nl_idx = text.find("\n", index)
                line_end = length if nl_idx == -1 else nl_idx + 1
                suffix = text[index + len(fence_match.group(0)) : line_end].strip()
                if code_fence is None:
                    if index > prose_start:
                        segments.append((prose_start, index))
                    code_fence = marker
                    prose_start = line_end
                    index = line_end
                    continue
                if (
                    code_fence[0] == marker[0]
                    and len(marker) >= len(code_fence)
                    and suffix == ""
                ):
                    code_fence = None
                    prose_start = line_end
                    index = line_end
                    continue

        if code_fence is not None:
            index += 1
            continue

        if text[index] == "`":
            run_len = 1
            while index + run_len < length and text[index + run_len] == "`":
                run_len += 1
            closing = _find_closing_backticks(text, index + run_len, run_len)
            if closing != -1:
                if index > prose_start:
                    segments.append((prose_start, index))
                span_end = closing + run_len
                prose_start = span_end
                index = span_end
                continue
            if "[" in text[index + run_len :] and unclosed_code_bracket_start is None:
                if index > prose_start:
                    segments.append((prose_start, index))
                unclosed_code_bracket_start = index
                prose_start = length
                break
            index += run_len
            continue

        index += 1

    if code_fence is None and prose_start < length:
        segments.append((prose_start, length))
    return segments, unclosed_code_bracket_start


def _split_unbuffered_citation_tail(text: str) -> tuple[str, str]:
    """Hold syntactic prose citation brackets (or incomplete prefixes) until RUN_COMPLETED resolves them."""
    if not text:
        return "", ""

    segments, unclosed_code_bracket_start = _prose_segments(text)
    for seg_start, seg_end in segments:
        segment = text[seg_start:seg_end]
        for match in _COMPLETE_CITATION_BRACKET_RE.finditer(segment):
            if _is_zero_math_bracket(match.group(1)):
                continue
            abs_start = seg_start + match.start()
            abs_end = seg_start + match.end()
            # If the bracket ends right before EOF or a partial markdown-link opener, hold it to confirm.
            if abs_end == len(text) or _has_partial_link_tail(text[abs_end:]):
                return text[:abs_start], text[abs_start:]
            return text[:abs_start], text[abs_start:]

    if unclosed_code_bracket_start is not None:
        return text[:unclosed_code_bracket_start], text[unclosed_code_bracket_start:]

    if segments and segments[-1][1] == len(text):
        seg_start, seg_end = segments[-1]
        incomplete_match = _INCOMPLETE_CITATION_TAIL_RE.search(text[seg_start:seg_end])
        if incomplete_match:
            abs_start = seg_start + incomplete_match.start()
            return text[:abs_start], text[abs_start:]

    stripped = text.rstrip(" \t")
    if len(stripped) < len(text):
        return stripped, text[len(stripped) :]
    return text, ""


def _compute_unstreamed_suffix(streamed: str, canonical: str) -> str:
    """Return only the unstreamed remainder of canonical output without duplicating or dropping text."""
    if not canonical:
        return ""
    if not streamed:
        return canonical
    if canonical.startswith(streamed):
        return canonical[len(streamed) :]
    norm_streamed = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", streamed.lstrip())
    if norm_streamed and canonical.startswith(norm_streamed):
        return canonical[len(norm_streamed) :]
    lhs = norm_streamed or streamed.lstrip()
    lcp = 0
    max_len = min(len(lhs), len(canonical))
    while lcp < max_len and lhs[lcp] == canonical[lcp]:
        lcp += 1
    return canonical[lcp:]


@router.post("/{agent_id}/chat/completions")
async def chat_completions(
    agent_id: str,
    body: ChatCompletionRequest,
    _user: User = Depends(get_current_user),
    owner_key: str = Depends(get_conversation_owner),
    principal: VerifiedPrincipal = Depends(require_principal_assertion),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
    llm: LLMClient = Depends(get_llm),
    tool_registry: ToolRegistry = Depends(get_tool_registry),
    agent_runtime: AgentRuntime = Depends(get_agent_runtime),
):
    agent = await svc.get_agent(session, agent_id, owner_key=owner_key)
    query, history = _split_query(body.messages)

    plan = svc.build_ask_context(agent=agent, query=query, history=history, user_name=_user.name)
    if not llm.configured:
        raise ConfigurationError("尚未配置 LLM，无法生成回答")

    persona = getattr(agent, "persona", None) or {}
    agent_knowledge_only = bool(
        getattr(agent, "knowledge_only", False)
        or (isinstance(persona, dict) and persona.get("knowledge_only") is True)
        or (isinstance(persona, dict) and persona.get("web_enabled") is False)
    )
    resolved_sources = await svc.resolve_sources(
        session,
        agent,
        plan.source_ids,
        principal=principal,
        include_project_sources=True,
    )

    if plan.route_decision is None:
        routed = await route_query_async(
            query,
            prior_turns=history,
            has_thread_history=bool(history),
            has_sources=bool(resolved_sources),
            web_enabled=not agent_knowledge_only,
            scoped=bool(plan.source_ids),
            llm_client=llm if settings.decision_provider == "llm" else None,
        )
        plan.route_decision = routed.to_dict()

    initial_degraded = bool((plan.route_decision or {}).get("degraded", False))
    initial_degraded_reason = (plan.route_decision or {}).get("degraded_reason")

    created = int(time.time())
    model = body.model or f"sag:{agent.name}"
    cid = f"chatcmpl-{agent_id[:12]}-{created}"

    def _events():
        # 无状态：thread_id=None → 不落库；复用同一 Agent 循环
        return agent_service.generate_stream(
            SessionLocal,
            plan=plan,
            agent=agent,
            thread_id=None,
            engine_manager=engine_manager,
            llm=llm,
            tool_registry=tool_registry,
            runtime=agent_runtime,
            knowledge_only=agent_knowledge_only,
            principal=principal,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )

    if body.stream:

        async def gen():
            streamed_parts: list[str] = []
            buffered_parts: list[str] = []
            pending_bracket = ""
            should_buffer = bool(plan.citations or plan.session_citations)
            errored = False
            final_degraded = initial_degraded
            final_degraded_reason = initial_degraded_reason
            final_citations: list[dict] = list(plan.citations)

            def chunk(delta: dict, finish: str | None = None, *, sag_meta: dict | None = None) -> str:
                payload: dict = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if sag_meta is not None:
                    payload["sag"] = sag_meta
                return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            yield chunk({"role": "assistant"})
            async for event in _events():
                payload = event.data.get("payload") or {}
                if event.type == EventType.RUN_STARTED.value:
                    if "degraded" in payload:
                        final_degraded = bool(payload.get("degraded"))
                        final_degraded_reason = payload.get("degraded_reason") or final_degraded_reason
                    if payload.get("grounding_required"):
                        should_buffer = True
                        if pending_bracket:
                            buffered_parts.append(pending_bracket)
                            pending_bracket = ""
                elif event.type == EventType.TOOL_STARTED.value:
                    if payload.get("name") in {"search_context", "get_entity"}:
                        should_buffer = True
                        if pending_bracket:
                            buffered_parts.append(pending_bracket)
                            pending_bracket = ""
                elif event.type == EventType.MESSAGE_DELTA.value:
                    delta = str(payload.get("delta") or "")
                    if not delta:
                        continue
                    if should_buffer:
                        buffered_parts.append(delta)
                    else:
                        flush_text, pending_bracket = _split_unbuffered_citation_tail(
                            pending_bracket + delta
                        )
                        if flush_text:
                            streamed_parts.append(flush_text)
                            yield chunk({"content": flush_text})
                elif event.type == EventType.RUN_COMPLETED.value:
                    canonical = str(payload.get("output") or "")
                    streamed = "".join(streamed_parts)
                    pending_bracket = ""
                    if "degraded" in payload:
                        final_degraded = bool(payload.get("degraded"))
                        final_degraded_reason = payload.get("degraded_reason") or final_degraded_reason
                    if "citations" in payload and payload["citations"] is not None:
                        final_citations = list(payload["citations"])
                    suffix = _compute_unstreamed_suffix(streamed, canonical)
                    if suffix:
                        yield chunk({"content": suffix})
                elif event.type in (EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value):
                    pending_bracket = ""
                    error = payload.get("error") or {}
                    err_msg = str(error.get("message") or "Generation failed")
                    err_code = str(error.get("code") or "stream_error")
                    yield chunk({}, finish="error")
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "error": {
                                    "message": err_msg,
                                    "type": "server_error",
                                    "code": err_code,
                                }
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    errored = True
            if not errored:
                yield chunk(
                    {},
                    finish="stop",
                    sag_meta={
                        "citations": final_citations,
                        "sources": len(final_citations),
                        "degraded": final_degraded,
                        "degraded_reason": final_degraded_reason,
                    },
                )
            yield "data: [DONE]\n\n"

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if initial_degraded:
            headers["X-SAG-Degraded"] = "true"
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    # 非流式：消费同一事件流，聚合为最终答案
    parts: list[str] = []
    final_output = ""
    completed_seen = False
    grounding_active = bool(plan.citations or plan.session_citations)
    citations = plan.citations
    usage: dict = {}
    final_degraded = initial_degraded
    final_degraded_reason = initial_degraded_reason
    async for event in _events():
        payload = event.data.get("payload") or {}
        if event.type == EventType.RUN_STARTED.value:
            if "degraded" in payload:
                final_degraded = bool(payload.get("degraded"))
                final_degraded_reason = payload.get("degraded_reason") or final_degraded_reason
            if payload.get("grounding_required"):
                grounding_active = True
        elif event.type == EventType.TOOL_STARTED.value:
            if payload.get("name") in {"search_context", "get_entity"}:
                grounding_active = True
        elif event.type == EventType.MESSAGE_DELTA.value:
            parts.append(payload["delta"])
        elif event.type == EventType.RUN_COMPLETED.value:
            completed_seen = True
            final_output = str(payload.get("output") or "")
            if "citations" in payload:
                citations = payload["citations"] if payload["citations"] is not None else []
            if "degraded" in payload:
                final_degraded = bool(payload.get("degraded"))
                final_degraded_reason = payload.get("degraded_reason") or final_degraded_reason
            usage = payload.get("usage") or {}
        elif event.type in (EventType.RUN_FAILED.value, EventType.RUN_CANCELLED.value):
            error = payload.get("error") or {}
            raise UpstreamError(error.get("message", "Generation failed"))
    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        final_output
                        if completed_seen
                        else ("" if grounding_active else "".join(parts))
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        # sag 扩展：引用溯源与降级状态（标准客户端忽略未知字段）
        "sag": {
            "citations": citations,
            "sources": len(citations),
            "degraded": final_degraded,
            "degraded_reason": final_degraded_reason,
        },
    }
