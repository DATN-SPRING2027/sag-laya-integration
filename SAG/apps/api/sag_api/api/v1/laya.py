"""Laya Decision Model API endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from sag_api.services.laya_router import route_query

router = APIRouter(prefix="/laya", tags=["laya"])


class LayaRouteRequest(BaseModel):
    query: str = Field(..., description="Câu hỏi hoặc tin nhắn cần phân loại")
    context: dict[str, Any] | None = Field(default=None, description="Ngữ cảnh bổ sung nếu có")


class LayaRouteResponse(BaseModel):
    query: str
    is_chitchat: bool = Field(..., description="Có phải câu chào hỏi / tán gẫu không")
    need_retrieval: bool = Field(..., description="Có cần tra cứu cơ sở tri thức SAG không")
    suggested_strategy: str = Field(..., description="Chiến lược tìm kiếm đề xuất: vector hoặc multi")
    domain: str = Field(..., description="Chủ đề: technical, business_hr, hoặc general")
    confidence: float = Field(..., description="Độ tin cậy của mô hình")
    latency_ms: float = Field(..., description="Thời gian suy luận (mili-giây)")
    model: str = Field(..., description="Checkpoint được Laya Router sử dụng")


@router.post("/route", response_model=LayaRouteResponse, summary="Định tuyến ý định câu hỏi qua Laya Decision Model")
async def route_user_query(body: LayaRouteRequest) -> LayaRouteResponse:
    """Sử dụng mô hình quyết định Laya (~33ms) để phân loại intent câu hỏi người dùng trước khi tra cứu."""
    result = route_query(body.query, body.context)
    return LayaRouteResponse(**result)
