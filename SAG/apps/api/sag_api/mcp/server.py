"""Expose SAG evidence tools through an authorization-scoped MCP request context.

HTTP scopes are constructed from verified principals and confirmed Source
mappings. Process-wide stdio is disabled because it cannot refresh that scope
for every tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypedDict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.services.query_strategy_planner import plan_query
from sag_api.services.retrieval_service import retrieve_relevant_sections
from sag_api.services.search_unit_retrieval_service import retrieve_search_unit_sections
from sag_api.services.source_service import versioned_source_ids

if TYPE_CHECKING:
    from sag_api.db.models import Document, Source
    from sag_api.sag import EngineManager


class MCPToolDetail(TypedDict):
    name: str
    label: str
    description: str


MCP_TOOL_DETAILS: tuple[MCPToolDetail, ...] = (
    {
        "name": "list_sources",
        "label": "查看信源",
        "description": "查看当前可访问的知识来源、文档数和分块数，并获取 source_id。",
    },
    {
        "name": "search",
        "label": "语义检索",
        "description": "按含义查找相关资料，适合自然语言问题、概念和模糊表述；返回证据片段及 chunk_id。",
    },
    {
        "name": "get_entity",
        "label": "查询实体",
        "description": "查找人物、组织或概念，并汇总它在资料中的相关上下文。",
    },
    {
        "name": "list_documents",
        "label": "查看文档",
        "description": "列出文档、处理状态和分块数量，并获取 document_id。",
    },
    {
        "name": "outline",
        "label": "文档大纲",
        "description": "查看指定文档的章节和分块结构，并获取 chunk_id，便于快速定位内容。",
    },
    {
        "name": "grep",
        "label": "精确查找",
        "description": "按原文字面内容查找，适合专名、编号、固定短语和代码；返回命中上下文及 chunk_id。",
    },
    {
        "name": "read",
        "label": "按行读原文",
        "description": "按行分页读取指定文档的原始文本，适合查看连续上下文。",
    },
    {
        "name": "get_chunk",
        "label": "读取分块",
        "description": "通过 chunk_id 读取一个分块的完整原文，用于核对和引用证据。",
    },
)
MCP_TOOL_NAMES = tuple(tool["name"] for tool in MCP_TOOL_DETAILS)
MCP_TOOL_LABELS = {tool["name"]: tool["label"] for tool in MCP_TOOL_DETAILS}
MCP_TOOL_DESCRIPTIONS = {tool["name"]: tool["description"] for tool in MCP_TOOL_DETAILS}
READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SourceId = Annotated[
    str,
    Field(description="可选。来自 list_sources；留空时查询全部可见信源。"),
]
DocumentId = Annotated[str, Field(description="来自 list_documents 的文档 ID。")]
ChunkId = Annotated[
    str,
    Field(description="来自 search、outline 或 grep 结果的分块 ID。"),
]


@dataclass(frozen=True)
class MCPScope:
    """一次 MCP 调用可见的信源及其暖引擎。"""

    engine_manager: EngineManager
    sources: tuple[Source, ...]
    principal: VerifiedPrincipal


_scope: contextvars.ContextVar[MCPScope | None] = contextvars.ContextVar(
    "sag_mcp_scope", default=None
)


def _require_scope() -> MCPScope:
    scope = _scope.get()
    if scope is None:
        raise RuntimeError("MCP 调用缺少知识库作用域")
    return scope


@contextlib.contextmanager
def use_scope(
    engine_manager: EngineManager,
    sources: Source | Iterable[Source],
    principal: VerifiedPrincipal,
):
    """在上下文内绑定一个信源或一组信源。"""
    if hasattr(sources, "sag_source_config_id"):
        selected = (sources,)
    else:
        selected = tuple(sources)
    token = _scope.set(MCPScope(engine_manager=engine_manager, sources=selected, principal=principal))
    try:
        yield
    finally:
        _scope.reset(token)


def _selected_sources(scope: MCPScope, source_id: str = "") -> tuple[Source, ...]:
    target = (source_id or "").strip()
    if not target:
        return scope.sources
    return tuple(source for source in scope.sources if source.id == target)


def _source_title(source: Source) -> str:
    return f"{source.name}（source_id={source.id}）"


def _sections_to_text(sections: list, sources: tuple[Source, ...]) -> str:
    if not sections:
        return "（无相关资料）"
    by_config = {source.sag_source_config_id: source for source in sources}
    by_id = {source.id: source for source in sources}
    blocks = []
    for index, section in enumerate(sections, start=1):
        heading = getattr(section, "heading", None) or "片段"
        chunk_id = getattr(section, "chunk_id", None) or ""
        tag = f"（chunk_id={chunk_id}）" if chunk_id else ""
        source = by_config.get(getattr(section, "source_config_id", None))
        source = source or by_id.get(getattr(section, "source_id", None))
        source_line = f"来源：{_source_title(source)}\n" if source and len(sources) > 1 else ""
        blocks.append(
            f"[{index}] {heading}{tag}\n{source_line}{getattr(section, 'content', '')}"
        )
    return "\n\n".join(blocks)


async def _document_in_scope(
    scope: MCPScope, document_id: str
) -> tuple[Document, Source] | None:
    from sag_api.core.db import SessionLocal
    from sag_api.services.source_service import get_authorized_document

    async with SessionLocal() as session:
        document = await get_authorized_document(
            session,
            principal=scope.principal,
            source_ids=[source.id for source in scope.sources],
            document_id=(document_id or "").strip(),
        )
    if document is None:
        return None
    source = next((item for item in scope.sources if item.id == document.source_id), None)
    return (document, source) if source is not None else None


def build_source_mcp(
    *,
    stateless_http: bool = False,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    """构造知识库 MCP server，具体作用域由 contextvar 在请求前注入。"""
    mcp = FastMCP(
        "sag-knowledge",
        instructions=(
            "SAG 知识库 MCP：检索本次已授权作用域内的信源，也可向工具传 source_id 进一步限定范围。"
            "先用 list_sources/list_documents 了解资料范围，再用 search、grep、outline、"
            "read 和 get_chunk 获取可引用证据。回答请依据 search 返回的编号证据。"
        ),
        stateless_http=stateless_http,
        transport_security=transport_security,
    )

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_sources"],
        description=MCP_TOOL_DESCRIPTIONS["list_sources"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_sources() -> str:
        scope = _require_scope()
        if not scope.sources:
            return "（知识库还没有信源）"
        return "\n".join(
            f"- {_source_title(source)}"
            for source in scope.sources
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["search"],
        description=MCP_TOOL_DESCRIPTIONS["search"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def search(
        query: Annotated[str, Field(description="要查找的问题、主题或关键词。")],
        top_k: Annotated[
            int,
            Field(description="最多返回多少条证据；默认 8，服务端会限制在 1–50。"),
        ] = 8,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可检索的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        normalized = (query or "").strip()
        if not normalized:
            return "（空查询）"
        from sag_api.core.db import SessionLocal

        async with SessionLocal() as session:
            partitioned_ids = await versioned_source_ids(session, [source.id for source in selected])
        bounded_top_k = max(1, min(top_k, 50))
        sections = []
        canonical_sources = [source for source in selected if source.id in partitioned_ids]
        legacy_sources = [source for source in selected if source.id not in partitioned_ids]
        if canonical_sources:
            canonical = await retrieve_search_unit_sections(
                scope.engine_manager,
                canonical_sources,
                normalized,
                principal=scope.principal,
                top_k=bounded_top_k,
                query_strategy_plan=plan_query(normalized),
            )
            sections.extend(canonical.sections)
        if legacy_sources:
            legacy = await retrieve_relevant_sections(
                scope.engine_manager,
                legacy_sources,
                normalized,
                top_k=bounded_top_k,
            )
            sections.extend(legacy.sections)
        sections.sort(key=lambda item: item.score, reverse=True)
        return _sections_to_text(sections[:bounded_top_k], selected)

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_entity"],
        description=MCP_TOOL_DESCRIPTIONS["get_entity"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_entity(
        name: Annotated[
            str,
            Field(description="人物、组织、概念等实体名称；支持完整名称或部分名称。"),
        ],
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        target = (name or "").strip()
        if not target:
            return "（未找到该实体）"

        from sag_api.core.db import SessionLocal

        async with SessionLocal() as session:
            partitioned_ids = await versioned_source_ids(session, [source.id for source in selected])
        matches = []
        canonical_sources = [source for source in selected if source.id in partitioned_ids]
        legacy_sources = [source for source in selected if source.id not in partitioned_ids]
        if canonical_sources:
            outcome = await retrieve_search_unit_sections(
                scope.engine_manager,
                canonical_sources,
                target,
                principal=scope.principal,
                top_k=6,
                query_strategy_plan=plan_query(target),
            )
            if outcome.sections:
                matches.append(_sections_to_text(outcome.sections, canonical_sources))
        async def _legacy_entity(source: Source) -> str | None:
            try:
                entities = await scope.engine_manager.list_entities(
                    source.sag_source_config_id, source=source, limit=200
                )
                lowered = target.lower()
                entity = next((item for item in entities if (item.name or "").lower() == lowered), None)
                entity = entity or next((item for item in entities if lowered in (item.name or "").lower()), None)
                if entity is None:
                    return None
                snippets = await scope.engine_manager.entity_context(
                    source.sag_source_config_id, entity.id, source=source, limit=6
                )
                body = "\n\n".join(snippets) if snippets else (entity.description or "")
                prefix = f"来源：{_source_title(source)}\n" if len(selected) > 1 else ""
                return f"{prefix}实体「{entity.name}」（{entity.type}）：\n{body}".strip()
            except Exception:
                return None

        legacy_matches = await asyncio.gather(*(_legacy_entity(source) for source in legacy_sources))
        matches.extend(item for item in legacy_matches if item)
        return "\n\n".join(matches) if matches else "（未找到该实体）"

    @mcp.tool(
        title=MCP_TOOL_LABELS["list_documents"],
        description=MCP_TOOL_DESCRIPTIONS["list_documents"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_documents(source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（知识库还没有信源）" if not source_id else "（信源不存在或不在当前作用域）"
        from sag_api.core.db import SessionLocal
        from sag_api.services.source_service import list_authorized_documents

        source_ids = [source.id for source in selected]
        async with SessionLocal() as session:
            documents = await list_authorized_documents(
                session, principal=scope.principal, source_ids=source_ids
            )
        documents.sort(key=lambda document: (document.created_at, document.id))
        if not documents:
            return "（知识库还没有文档）"
        by_source: dict[str, list[Document]] = {item.id: [] for item in selected}
        for document in documents:
            by_source.setdefault(document.source_id, []).append(document)
        blocks = []
        for source in selected:
            rows = by_source.get(source.id) or []
            if not rows:
                continue
            lines = []
            for document in rows:
                status = getattr(document.status, "value", document.status)
                lines.append(
                    f"- {document.filename} · id={document.id} · {status} · "
                    f"{document.chunk_count} 分块"
                )
            header = f"## {_source_title(source)}\n" if len(selected) > 1 else ""
            blocks.append(header + "\n".join(lines))
        return "\n\n".join(blocks)

    @mcp.tool(
        title=MCP_TOOL_LABELS["outline"],
        description=MCP_TOOL_DESCRIPTIONS["outline"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def outline(document_id: DocumentId) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "（未找到该文档）"
        document, source = match
        if not document.sag_source_id:
            return "（尚无大纲：文档可能仍在处理中）"
        rows = await scope.engine_manager.list_chunk_headings(
            source.sag_source_config_id,
            source=source,
            doc_sag_id=document.sag_source_id,
        )
        if not rows:
            return "（尚无大纲：文档可能仍在处理中）"
        return "\n".join(
            f"{row['rank']:>3}. {row['heading'] or '（无标题分块）'}"
            f"（chunk_id={row['chunk_id']}）"
            for row in rows
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["grep"],
        description=MCP_TOOL_DESCRIPTIONS["grep"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def grep(
        pattern: Annotated[
            str,
            Field(description="要在原文中精确查找的文字；适合专名、编号、固定短语和代码。"),
        ],
        limit: Annotated[
            int,
            Field(description="最多返回多少处匹配；默认 20，服务端会限制在 1–100。"),
        ] = 20,
        source_id: SourceId = "",
    ) -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        needle = (pattern or "").strip()
        if not needle:
            return "（空匹配串）"
        bounded_limit = max(1, min(limit, 100))

        from sag_api.core.db import SessionLocal

        async with SessionLocal() as session:
            partitioned_ids = await versioned_source_ids(session, [source.id for source in selected])
        blocks = []
        canonical_sources = [source for source in selected if source.id in partitioned_ids]
        legacy_sources = [source for source in selected if source.id not in partitioned_ids]
        if canonical_sources:
            outcome = await retrieve_search_unit_sections(
                scope.engine_manager,
                canonical_sources,
                needle,
                principal=scope.principal,
                top_k=bounded_limit,
                query_strategy_plan=plan_query(needle, requested_mode="EXACT"),
            )
            if outcome.sections:
                blocks.extend(_sections_to_text(outcome.sections, canonical_sources).split("\n\n"))

        async def _legacy_grep(source: Source) -> list[dict]:
            try:
                return await scope.engine_manager.grep_chunks(
                    source.sag_source_config_id, needle, source=source, limit=bounded_limit
                )
            except Exception:
                return []

        results = await asyncio.gather(*(_legacy_grep(source) for source in legacy_sources))
        for source, rows in zip(legacy_sources, results, strict=True):
            for row in rows:
                source_line = f"来源：{_source_title(source)}\n" if len(selected) > 1 else ""
                blocks.append(
                    f"{row['heading'] or '片段'}（chunk_id={row['chunk_id']}）\n"
                    f"{source_line}{row['snippet']}"
                )
        if not blocks:
            return "（未匹配到内容）"
        return "\n\n".join(f"[{index}] {block}" for index, block in enumerate(blocks[:bounded_limit], 1))

    @mcp.tool(
        title=MCP_TOOL_LABELS["read"],
        description=MCP_TOOL_DESCRIPTIONS["read"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def read(
        document_id: DocumentId,
        offset: Annotated[
            int,
            Field(description="从第几行开始读取；首行为 1，默认 1。"),
        ] = 1,
        limit: Annotated[
            int,
            Field(description="本次读取多少行；默认 120，服务端最多返回 500 行。"),
        ] = 120,
    ) -> str:
        scope = _require_scope()
        match = await _document_in_scope(scope, document_id)
        if match is None:
            return "（未找到该文档）"
        document, source = match
        import os

        if not document.storage_path or not os.path.isfile(document.storage_path):
            return "（原始文件不存在或已清理）"
        try:
            with open(document.storage_path, encoding="utf-8", errors="replace") as file:
                lines = file.readlines()
        except OSError:
            return "（文件读取失败）"
        start = max(0, offset - 1)
        page = lines[start : start + max(1, min(limit, 500))]
        if not page:
            return f"（超出范围：全文共 {len(lines)} 行）"
        body = "".join(f"{start + index + 1:>5}\t{line}" for index, line in enumerate(page))
        source_line = f"来源：{_source_title(source)}\n" if len(scope.sources) > 1 else ""
        return (
            f"{document.filename} · 第 {start + 1}-{start + len(page)} 行 / "
            f"共 {len(lines)} 行\n{source_line}{body}"
        )

    @mcp.tool(
        title=MCP_TOOL_LABELS["get_chunk"],
        description=MCP_TOOL_DESCRIPTIONS["get_chunk"],
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_chunk(chunk_id: ChunkId, source_id: SourceId = "") -> str:
        scope = _require_scope()
        selected = _selected_sources(scope, source_id)
        if not selected:
            return "（没有可查询的信源）" if not source_id else "（信源不存在或不在当前作用域）"
        cid = (chunk_id or "").strip()
        if not cid:
            return "（缺少 chunk_id）"

        from sag_api.core.db import SessionLocal
        from sag_api.services.search_unit_retrieval_service import get_search_unit_citation

        partitioned_ids: set[str] = set()
        found = None
        async with SessionLocal() as session:
            partitioned_ids = await versioned_source_ids(session, [source.id for source in selected])
            for source in selected:
                citation = await get_search_unit_citation(
                    session,
                    source=source,
                    principal=scope.principal,
                    search_unit_id=cid,
                    engine_manager=scope.engine_manager,
                )
                if citation is not None:
                    found = (source, citation)
                    break
        if found is None:
            legacy_sources = [source for source in selected if source.id not in partitioned_ids]
            async def _one(source: Source):
                try:
                    chunk = await scope.engine_manager.get_chunk(
                        source.sag_source_config_id, cid, source=source
                    )
                    return source, chunk
                except Exception:
                    return source, None

            results = await asyncio.gather(*(_one(source) for source in legacy_sources))
            legacy = next(((source, chunk) for source, chunk in results if chunk is not None), None)
            found = legacy
        if found is None:
            return "（未找到该分块）"
        source, chunk = found
        if isinstance(chunk, dict):
            heading = str(chunk.get("heading") or "").strip()
            content = str(chunk.get("content") or "")
            body = f"{heading}\n\n{content}".strip() if heading else content
            return f"来源：{_source_title(source)}\n\n{body}" if len(selected) > 1 else body
        heading = (chunk.heading or "").strip()
        body = f"{heading}\n\n{chunk.content}".strip() if heading else chunk.content
        return f"来源：{_source_title(source)}\n\n{body}" if len(selected) > 1 else body

    return mcp


_singleton: FastMCP | None = None


def get_source_mcp() -> FastMCP:
    """返回 stdio/进程内调用复用的 MCP server。"""
    global _singleton
    if _singleton is None:
        _singleton = build_source_mcp()
    return _singleton


async def serve_stdio(source_id: str | None = None) -> None:
    """Disable stdio until each MCP call can carry a current signed principal."""
    del source_id
    raise SystemExit(
        "SAG MCP stdio is disabled: a process-wide Source ID is not an authorization grant; "
        "use the authenticated HTTP transport with a current Continuum principal assertion."
    )


def _main() -> None:
    import os

    source_id = os.environ.get("SAG_MCP_SOURCE_ID", "").strip() or None
    asyncio.run(serve_stdio(source_id))


if __name__ == "__main__":
    _main()
