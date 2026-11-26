from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from sag_api.enums import SearchStrategy
from sag_api.schemas.insight import EntityOut, GraphRelationOut

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


def _normalize_as_of_value(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("as_of cannot be a boolean")
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, 999999, tzinfo=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if _DATE_ONLY_RE.match(raw):
            parsed_date = date.fromisoformat(raw)
            return datetime(
                parsed_date.year,
                parsed_date.month,
                parsed_date.day,
                23,
                59,
                59,
                999999,
                tzinfo=UTC,
            )
        iso = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
        parsed_dt = datetime.fromisoformat(iso)
        return parsed_dt.replace(tzinfo=UTC) if parsed_dt.tzinfo is None else parsed_dt.astimezone(UTC)
    raise ValueError("as_of must be an ISO-8601 date or datetime")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    strategy: SearchStrategy | None = None
    top_k: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional explicit result limit; defaults to 7-axis RetrievalPlan cardinality "
            "(5 for standard passage Q&A, up to 50 for document listing)."
        ),
    )
    as_of: datetime | date | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _validate_as_of(cls, value: object) -> datetime | None:
        return _normalize_as_of_value(value)


class GlobalSearchRequest(BaseModel):
    """工作空间级搜索：默认有界候选分区，可传 source_ids 收窄（如 @某信源）。"""

    query: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] | None = Field(default=None, max_length=256)
    top_k: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional explicit result limit; defaults to 7-axis RetrievalPlan cardinality "
            "(5 for standard passage Q&A, up to 50 for document listing)."
        ),
    )
    strategy: SearchStrategy | None = None
    retrieval_mode: (
        Literal[
            "EXACT",
            "LOCAL_FACTUAL",
            "ENTITY_RELATIONAL",
            "TEMPORAL",
            "GLOBAL_TOPIC",
            "MULTI_HOP",
        ]
        | None
    ) = None
    as_of: datetime | date | None = None
    save_exploration: bool = False

    @field_validator("as_of", mode="before")
    @classmethod
    def _validate_as_of(cls, value: object) -> datetime | None:
        return _normalize_as_of_value(value)


class SectionOut(BaseModel):
    chunk_id: str | None
    search_unit_id: str | None = None
    block_from_id: str | None = None
    block_to_id: str | None = None
    section_path: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    document_name: str | None = None
    version_no: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    anchor: str | None = None
    heading: str
    content: str
    score: float = Field(
        description=(
            "Hybrid relevance score in [0, 1] computed as "
            "min(1.0, 0.56 * fused_rrf + 0.32 * lexical_score + 0.12 * relative_semantic_score). "
            "This is not raw cosine similarity or a calibrated relevance probability."
        )
    )
    rank: int
    source_id: str | None
    source_name: str | None = None


class SearchEventOut(BaseModel):
    id: str
    document_id: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    title: str
    summary: str = ""
    category: str = ""
    rank: int = 0
    parent_id: str | None = None
    chunk_id: str | None = None
    start_time: datetime | None = None
    score: float = 0.0


class SearchSourceHitOut(BaseModel):
    source_id: str
    source_name: str | None = None
    event_hits: int = 0
    max_score: float = 0.0
    latest_event_time: datetime | None = None


class SearchCitationOut(BaseModel):
    kind: Literal["internal"] = "internal"
    n: int
    document_id: str
    document_version_id: str
    document_name: str = ""
    version_no: int | None = None
    chunk_id: str
    search_unit_id: str | None = None
    block_from_id: str | None = None
    block_to_id: str | None = None
    section_path: str | None = None
    page_from: int
    page_to: int
    anchor: str
    heading: str = ""
    snippet: str = ""
    score: float = 0.0
    source_id: str
    source_name: str | None = None


class SearchResponse(BaseModel):
    query: str
    sections: list[SectionOut]
    events: list[SearchEventOut] = Field(default_factory=list)
    entities: list[EntityOut] = Field(default_factory=list)
    relations: list[GraphRelationOut] = Field(default_factory=list)
    source_hits: list[SearchSourceHitOut] = Field(default_factory=list)
    summary: str = ""
    citations: list[SearchCitationOut] = Field(default_factory=list)
    answer_status: Literal["pending", "answered", "extractive_fallback", "no_answer", "skipped"] = "skipped"
    no_answer_reason: Literal[
        "empty_evidence",
        "weak_evidence",
        "context_budget",
        "insufficient_evidence",
        "unsupported_claim",
    ] | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    exploration_id: str | None = None
    stats: dict[str, Any]


class EvalCompareRequest(BaseModel):
    """/search/eval-compare 请求体:同一 query,多策略并排跑。"""

    query: str = Field(min_length=1, max_length=4000)
    strategies: list[SearchStrategy] = Field(min_length=2, max_length=4)
    source_ids: list[str] | None = Field(default=None, max_length=256)
    top_k: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional explicit result limit; defaults to adaptive RetrievalPlan cardinality "
            "(5–50) over search_top_k (default 8) initial candidates."
        ),
    )
    judge: bool = True  # 前端可临时关掉;是否真的调 LLM 还看后台 settings 开关


class EvalStrategyResultOut(BaseModel):
    strategy: SearchStrategy
    sections: list[SectionOut]
    stats: dict[str, Any]
    error: str | None = None


class EvalJudgeOut(BaseModel):
    a_strategy: SearchStrategy
    b_strategy: SearchStrategy
    winner: str  # "A" | "B" | "tie"
    reason: str


class EvalCompareResponse(BaseModel):
    query: str
    results: list[EvalStrategyResultOut]
    judges: list[EvalJudgeOut] = Field(default_factory=list)
    judge_enabled: bool
    judge_reason: str | None = None  # 例:LLM 未配置 / 后台 flag 关闭
