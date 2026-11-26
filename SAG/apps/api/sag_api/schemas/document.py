from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, computed_field, field_validator

from sag_api.enums import DocumentStatus, normalize_authority_tier, normalize_scope_tags

_SENTINEL_EFFECTIVE_CUTOFF = datetime(9999, 1, 1, 0, 0, 0, tzinfo=UTC)


class MessageItem(BaseModel):
    text: str
    author: str | None = None
    role: str | None = None
    ts: str | None = None
    thread: str | None = None


class IngestRequest(BaseModel):
    """统一写入：文本或一批消息，二选一。"""

    project_id: str | None = None
    logical_source_id: str | None = None
    text: str | None = None
    title: str | None = None
    messages: list[MessageItem] | None = Field(default=None)
    source_published_at: datetime | None = None
    authority_tier: int | None = Field(default=None, ge=1, le=4)
    effective_from: datetime | None = None
    scope_tags: list[str] | None = Field(default=None, max_length=16)

    @field_validator("authority_tier", mode="before")
    @classmethod
    def _validate_authority_tier(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("authority_tier cannot be a boolean")
        if isinstance(value, (int, str)):
            return normalize_authority_tier(value)
        raise ValueError("authority_tier must be an integer between 1 and 4")

    @field_validator("effective_from", "source_published_at", mode="before")
    @classmethod
    def _reject_bool_timestamps(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("timestamp field cannot be a boolean")
        return value

    @field_validator("effective_from", mode="after")
    @classmethod
    def _normalize_effective_from(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if dt >= _SENTINEL_EFFECTIVE_CUTOFF:
            raise ValueError("effective_from must be earlier than 9999-01-01T00:00:00Z")
        return dt

    @field_validator("source_published_at", mode="after")
    @classmethod
    def _normalize_source_published_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @field_validator("scope_tags", mode="before")
    @classmethod
    def _validate_scope_tags(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("scope_tags must be a list of strings")
        if len(value) > 16:
            raise ValueError("scope_tags cannot exceed 16 tags")
        normalized = normalize_scope_tags(value)
        return normalized or None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    filename: str
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    event_count: int
    progress: int
    token_usage: int
    token_usage_breakdown: dict | None = None
    error: str | None
    error_layer: str | None = None
    error_stage: str | None = None
    parser_provider: str | None = None
    mineru_provider: str | None = None
    mineru_model: str | None = None
    parser_status: str | None = None
    fallback_from: str | None = None
    fallback_reason: str | None = None
    octx_installation_id: str | None = Field(default=None, exclude=True)
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def original_file_available(self) -> bool:
        """OCTX packages can carry parsed concepts without original attachment bytes."""
        return self.octx_installation_id is None

    @field_validator("error", mode="before")
    @classmethod
    def redact_storage_error(cls, value: object, info: ValidationInfo) -> object:
        """Keep SQL and local storage details in server logs, not in the UI."""
        if not isinstance(value, str):
            return value
        normalized = value.lower()
        sql_markers = (
            "sqlite3.",
            "sqlalchemy.exc",
            "[sql:",
            "[parameters:",
            "sqlalche.me/e/",
        )
        if not any(marker in normalized for marker in sql_markers):
            return value
        if info.data.get("status") == DocumentStatus.DELETE_FAILED:
            return "文档删除失败，请重试；若仍失败，请查看服务日志。"
        if "foreign key constraint failed" in normalized:
            return "信息源初始化未完成，文档尚未入库，请重试。"
        return "文档入库失败，请重试；若仍失败，请查看服务日志。"
