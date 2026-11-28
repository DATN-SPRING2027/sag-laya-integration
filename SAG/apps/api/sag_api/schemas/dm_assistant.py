"""Strict Pydantic v2 schemas for internal Continuum 1-on-1 DM Assistant synthesis."""

from __future__ import annotations

import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

DmAssistantQueryMode = Literal["TIME_WINDOW", "KEYWORD", "HYBRID"]
DmAssistantStatus = Literal["ANSWERED", "INSUFFICIENT_EVIDENCE"]

DM_REF_ID_PATTERN = r"^M[1-9][0-9]{0,2}$"
DM_REF_ID_RE = re.compile(DM_REF_ID_PATTERN)

MAX_DM_QUESTION_CHARS = 2_000
MAX_DM_TIMEZONE_CHARS = 64
MAX_DM_COVERAGE_HINT_CHARS = 500
MAX_DM_MESSAGES = 80
MAX_DM_SENDER_LABEL_CHARS = 120
MAX_DM_MESSAGE_TEXT_CHARS = 1_200
MAX_DM_TOTAL_CONTEXT_CHARS = 24_000
MAX_DM_ANSWER_CHARS = 4_000
MAX_DM_CITED_REFS = 20
MAX_DM_MODEL_CHARS = 120


def _strip_if_str(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _reject_numeric_timestamp(value: object) -> object:
    if isinstance(value, (bool, int, float)):
        raise ValueError("Timestamp must be an ISO-8601 string with timezone offset")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("Timestamp must be a non-empty ISO-8601 string")
        return stripped
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DmAssistantMessageInput(StrictModel):
    refId: str = Field(min_length=2, max_length=4, pattern=DM_REF_ID_PATTERN)
    senderLabel: str = Field(min_length=1, max_length=MAX_DM_SENDER_LABEL_CHARS)
    sentAtIso: AwareDatetime
    text: str = Field(min_length=1, max_length=MAX_DM_MESSAGE_TEXT_CHARS)

    @field_validator("refId")
    @classmethod
    def _validate_ref_id_exact(cls, value: str) -> str:
        if value != value.strip() or not DM_REF_ID_RE.fullmatch(value):
            raise ValueError("refId must match ^M[1-9][0-9]{0,2}$")
        return value

    @field_validator("senderLabel", "text", mode="before")
    @classmethod
    def _trim_text_fields(cls, value: object) -> object:
        return _strip_if_str(value)

    @field_validator("sentAtIso", mode="before")
    @classmethod
    def _validate_sent_at_type(cls, value: object) -> object:
        return _reject_numeric_timestamp(value)


class DmAssistantAnswerRequest(StrictModel):
    question: str = Field(min_length=1, max_length=MAX_DM_QUESTION_CHARS)
    timezone: str = Field(min_length=1, max_length=MAX_DM_TIMEZONE_CHARS)
    referenceNowIso: AwareDatetime
    queryMode: DmAssistantQueryMode
    partialCoverage: bool = False
    coverageHint: str | None = Field(default=None, max_length=MAX_DM_COVERAGE_HINT_CHARS)
    messages: list[DmAssistantMessageInput] = Field(max_length=MAX_DM_MESSAGES)

    @field_validator("question", "timezone", mode="before")
    @classmethod
    def _trim_required_strings(cls, value: object) -> object:
        return _strip_if_str(value)

    @field_validator("coverageHint", mode="before")
    @classmethod
    def _trim_optional_coverage_hint(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("referenceNowIso", mode="before")
    @classmethod
    def _validate_reference_now_type(cls, value: object) -> object:
        return _reject_numeric_timestamp(value)

    @field_validator("timezone")
    @classmethod
    def _validate_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def _validate_messages_uniqueness_and_budget(self) -> DmAssistantAnswerRequest:
        seen_refs: set[str] = set()
        total_chars = 0
        for message in self.messages:
            if message.refId in seen_refs:
                raise ValueError(f"Duplicate message refId: {message.refId}")
            seen_refs.add(message.refId)
            total_chars += len(message.text)
        if total_chars > MAX_DM_TOTAL_CONTEXT_CHARS:
            raise ValueError(
                f"Total message text length ({total_chars}) exceeds hard limit of "
                f"{MAX_DM_TOTAL_CONTEXT_CHARS} characters"
            )
        return self


class DmAssistantUsageOut(StrictModel):
    inputTokens: int = Field(default=0, ge=0)
    cachedTokens: int = Field(default=0, ge=0)
    outputTokens: int = Field(default=0, ge=0)
    totalTokens: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)


class DmAssistantAnswerResponse(StrictModel):
    status: DmAssistantStatus
    answerText: str = Field(min_length=1, max_length=MAX_DM_ANSWER_CHARS)
    citedRefIds: list[str] = Field(default_factory=list, max_length=MAX_DM_CITED_REFS)
    coverageNote: str | None = Field(default=None, max_length=MAX_DM_COVERAGE_HINT_CHARS)
    model: str | None = Field(default=None, max_length=MAX_DM_MODEL_CHARS)
    usage: DmAssistantUsageOut

    @model_validator(mode="after")
    def _validate_response_invariants(self) -> DmAssistantAnswerResponse:
        seen: set[str] = set()
        for ref_id in self.citedRefIds:
            if ref_id != ref_id.strip() or not DM_REF_ID_RE.fullmatch(ref_id):
                raise ValueError(f"Invalid citedRefId format: {ref_id}")
            if ref_id in seen:
                raise ValueError(f"Duplicate citedRefId: {ref_id}")
            seen.add(ref_id)
        if self.status == "ANSWERED" and not self.citedRefIds:
            raise ValueError("ANSWERED status requires at least one citedRefId")
        if self.status == "INSUFFICIENT_EVIDENCE" and self.citedRefIds:
            raise ValueError("INSUFFICIENT_EVIDENCE status must not include citedRefIds")
        return self
