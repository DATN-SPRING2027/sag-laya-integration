"""Decision Router API endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from sag_api.core.config import settings
from sag_api.core.deps import get_current_user
from sag_api.db.models import User
from sag_api.services.decision_router import route_query_async

router = APIRouter(prefix="/decision", tags=["decision"])


class DecisionRouteRequest(BaseModel):
    query: str = Field(..., description="Câu hỏi hoặc tin nhắn cần phân loại")
    context: dict[str, Any] | None = Field(default=None, description="Ngữ cảnh bổ sung nếu có")


class DecisionRouteResponse(BaseModel):
    query: str
    coarse_intent: str = Field(default="AMBIGUOUS", description="CHAT | KNOWLEDGE | COMMAND | AMBIGUOUS")
    question_scope: str = Field(default="unknown", description="general | project | application | unknown")
    is_chitchat: bool = Field(..., description="Có phải câu chào hỏi / tán gẫu không")
    need_retrieval: bool = Field(..., description="Có cần tra cứu cơ sở tri thức SAG không")
    suggested_strategy: str = Field(..., description="Chiến lược tìm kiếm đề xuất: vector hoặc multi")
    domain: str = Field(..., description="Chủ đề: technical, business_hr, hoặc general")
    memory_scope: str = Field(
        default="project_documents",
        description="Phạm vi bằng chứng: none, session_history, project_documents, hybrid_both, full_session_audit",
    )
    temporal_mode: str = Field(
        default="atemporal",
        description="Chế độ thời gian: current_effective, change_lineage, past_point, atemporal",
    )
    answer_shape: str = Field(
        default="fact_answer",
        description=(
            "Hình thái câu trả lời: fact_answer, multi_facet_compare, "
            "document_list, exhaustive_count, summarize_set, open_document"
        ),
    )
    intent_flags: dict[str, bool] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    confidence: float = Field(..., description="Độ tin cậy của mô hình")
    latency_ms: float = Field(..., description="Thời gian suy luận (mili-giây)")
    model: str = Field(..., description="Decision model used for classification")
    degraded: bool = False
    degraded_reason: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    order_mode: str = "relevance"
    explicit_count: int | None = None
    target_unit: str = "passage"
    is_catalog_query: bool = False
    retrieval_mode_hint: str = "auto"
    subqueries: list[str] = Field(default_factory=list)
    topic_slot_key: str | None = None
    hierarchy_bias: str = "none"
    entities: list[str] = Field(default_factory=list)


@router.post("/route", response_model=DecisionRouteResponse, summary="Định tuyến ý định câu hỏi qua Decision Router")
async def route_user_query(
    body: DecisionRouteRequest,
    request: Request,
    _user: User = Depends(get_current_user),
) -> DecisionRouteResponse:
    """Classify via unified async structured-output router."""
    llm = getattr(request.app.state, "llm", None)
    decision = await route_query_async(
        body.query,
        context=body.context,
        llm_client=llm if settings.decision_provider == "llm" else None,
    )
    return DecisionRouteResponse(**decision.to_dict())
