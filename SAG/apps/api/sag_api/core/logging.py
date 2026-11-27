"""Lightweight logging configuration + sanitized request-ID tracing middleware."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

_CONFIGURED = False
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")

# Current request correlation ID exposed to logs and error handlers
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def sanitize_request_id(raw: str | None) -> str:
    """Validate inbound X-Request-Id against strict character allowlist to block log injection."""
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate and _REQUEST_ID_RE.fullmatch(candidate):
            return candidate
    return uuid.uuid4().hex[:16]


class _UtcTextFormatter(logging.Formatter):
    """ISO-8601 UTC plain-text formatter."""

    converter = staticmethod(time.gmtime)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class _UtcJsonFormatter(logging.Formatter):
    """Structured JSON formatter with ISO-8601 UTC timestamps for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, object] = {
            "ts": dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _use_json_logging() -> bool:
    try:
        from sag_api.core.config import settings

        if settings.log_format == "json":
            return True
        if settings.log_format == "text":
            return False
        return settings.environment == "prod"
    except Exception:  # noqa: BLE001
        return False


def configure_logging(level: str = "INFO", *, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if _use_json_logging():
        handler.setFormatter(_UtcJsonFormatter())
    else:
        handler.setFormatter(
            _UtcTextFormatter(
                fmt="%(asctime)s  %(levelname)-7s  [%(request_id)s]  %(name)s  %(message)s",
            )
        )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    # Reduce third-party noise and prevent LLM clients from dumping raw prompts at DEBUG level.
    for noisy in (
        "httpx",
        "httpcore",
        "openai",
        "sse_starlette.sse",
        "aiosqlite",
        "LiteLLM",
        "LiteLLM Router",
        "LiteLLM Proxy",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("zleap.sag.ai.openai").setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"sag.{name}")


class _RequestIdFilter(logging.Filter):
    """Inject current request correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign sanitized correlation ID per request and echo it via X-Request-Id."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        rid = sanitize_request_id(request.headers.get("x-request-id"))
        token = request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
