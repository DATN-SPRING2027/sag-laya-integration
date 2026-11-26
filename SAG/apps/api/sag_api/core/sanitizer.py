"""Bộ lọc và làm sạch thông tin nhạy cảm tập trung (Zero-Secret Sanitizer).

Triết lý Ponytail: Tối giản, thuần regex stdlib, không phụ thuộc thư viện ngoài.
Loại bỏ API keys, Bearer tokens, mật khẩu trong URL, query params nhạy cảm
trước khi ghi log hoặc lưu vào database.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_BEARER_TOKEN = re.compile(r"(?i)bearer\s+\S+")
_API_KEY = re.compile(r"(?i)\b(?:sk|ak)-[a-z0-9._-]{6,}\b")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_SLACK_TOKEN = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_SECRET_QUERY_KEYS = re.compile(
    r"(?i)([?&](?:token|key|signature|credential|authorization|auth|api[-_]?key|access[-_]?key|secret[-_]?key|private[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|session[-_]?token|client[-_]?secret|password|secret|x-amz-[^=]+)=)[^&#\s]+"
)
_URL_CREDENTIALS = re.compile(r"(?i)([a-z0-9+.-]+://)([^:\s/@]+:[^@\s/]+@)")
_URL = re.compile(r"https?://[^\s；，,]+")


def sanitize_error_message(
    value: object,
    *,
    max_length: int = 500,
    redact_urls: bool = False,
) -> str:
    """Tẩy rửa các khóa bí mật và thông tin nhạy cảm trong chuỗi lỗi."""
    if value is None:
        return ""
    message = " ".join(str(value).split())
    if not message:
        return ""
    # 1. Che giấu thông tin xác thực nhúng trong URL: http://user:pass@host
    message = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", message)
    # 2. Che giấu query parameters nhạy cảm: ?api-key=..., &token=...
    message = _SECRET_QUERY_KEYS.sub(r"\1[REDACTED]", message)
    # 3. Che giấu Bearer tokens
    message = _BEARER_TOKEN.sub("Bearer [REDACTED]", message)
    # 4. Che giấu các token / API keys (OpenAI, GitHub, Google, Slack, AWS, JWT)
    message = _GITHUB_TOKEN.sub("[REDACTED]", message)
    message = _GOOGLE_API_KEY.sub("[REDACTED]", message)
    message = _SLACK_TOKEN.sub("[REDACTED]", message)
    message = _AWS_ACCESS_KEY.sub("[REDACTED]", message)
    message = _JWT_TOKEN.sub("[REDACTED]", message)
    message = _API_KEY.sub("[REDACTED]", message)
    if redact_urls:
        message = _URL.sub("[URL REDACTED]", message)
    return message[:max_length]


def redact_parser_reason(value: object, *, max_length: int = 300) -> str | None:
    """Làm sạch lý do lỗi/fallback từ parser (che cả secret lẫn URL nội bộ)."""
    cleaned = sanitize_error_message(value, max_length=max_length, redact_urls=True)
    return cleaned or None


def inspect_replacement_chars(text: str) -> tuple[int, float]:
    """Đếm số ký tự lỗi mã hóa U+FFFD và tỷ lệ trên tổng độ dài văn bản."""
    if not text:
        return 0, 0.0
    count = text.count("\ufffd")
    return count, count / len(text)


def decode_bytes_safely(
    raw_bytes: bytes,
    *,
    encoding: str = "utf-8",
    context: str = "payload",
    raise_on_severe: bool = False,
) -> tuple[str, int, float]:
    """Giải mã bytes an toàn, ưu tiên phục hồi UTF-8 chuẩn và theo dõi U+FFFD."""
    enc = (encoding or "utf-8").strip().lower()
    text = raw_bytes.decode(enc, errors="replace")
    if "\ufffd" in text and enc not in {"utf-8", "utf8"}:
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
    count, ratio = inspect_replacement_chars(text)
    if count > 0:
        log.warning(
            "Replacement character U+FFFD detected context=%s count=%d ratio=%.4f",
            context,
            count,
            ratio,
        )
        if raise_on_severe and count > max(8, len(text) // 100):
            raise ValueError(
                f"Decoded content contains excessive U+FFFD replacement characters ({count} in {context})"
            )
    return text, count, ratio
