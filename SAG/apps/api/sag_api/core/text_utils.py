"""Shared Unicode NFC normalization, tiktoken estimation, and multilingual splitting utilities."""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from sag_api.core.logging import get_logger

log = get_logger("text_utils")

_CONTINUOUS_SCRIPT_CHAR_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
    r"\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]"
)

_VIETNAMESE_D_MAP = str.maketrans({"đ": "d", "Đ": "D"})

# Protected URL / email spans that must never be split at internal dots, colons, or query marks.
_PROTECTED_SPAN_RE = re.compile(
    r"(?:https?://[^\s<>\]\)`\"']+|www\.[^\s<>\]\)`\"']+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
)

_LEGAL_PREFIX_BEFORE_DOT_RE = re.compile(
    r"(?:^|\s)(?:điều|khoản|mục|chương|phần|điểm|art|article|sec|section|para|clause)\s+[A-Za-z0-9]+$",
    re.IGNORECASE,
)

_TOKEN_BEFORE_DOT_RE = re.compile(r"([A-Za-zÀ-ỹ]+(?:\.[A-Za-zÀ-ỹ]+)*)$")

_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "tp",
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "eg",
        "e.g",
        "ie",
        "i.e",
        "fig",
        "eq",
        "no",
        "vol",
        "pp",
        "inc",
        "ltd",
        "corp",
        "co",
        "gs",
        "pgs",
        "ts",
        "ths",
        "ks",
        "bs",
        "cn",
        "q",
        "p",
        "tx",
        "tt",
        "u.s",
        "u.k",
        "jan",
        "feb",
        "mar",
        "apr",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
    }
)

# Clause split: ASCII punctuation requires trailing whitespace so numbers/times/URLs stay intact.
_MULTILINGUAL_CLAUSE_SPLIT_RE = re.compile(
    r"(?:(?<=[,:;])\s+)|(?:(?<=[，、：；])\s*)|(?:\s+(?=[\(\[（]))"
)

_CLOSING_PUNCT = frozenset({"\"", "'", "”", "’", ")", "]", "}"})


def normalize_text(
    text: str,
    *,
    preserve_code: bool = False,
    strip: bool = True,
) -> str:
    """Normalize text to Unicode NFC with Unix newlines and control-char filtering.

    Applies NFC normalization and CRLF/CR -> LF conversion BEFORE the preserve_code
    early return so code/table/list blocks never retain NFD or CRLF.
    """
    if not text:
        return ""

    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        ch
        for ch in normalized
        if ch in ("\n", "\t") or not unicodedata.category(ch).startswith("C")
    )

    if preserve_code:
        return cleaned.rstrip() if strip else cleaned

    collapsed = re.sub(r"[^\S\n]+", " ", cleaned)
    collapsed = re.sub(r" *\n *", "\n", collapsed)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip() if strip else collapsed


def slugify_ascii(
    value: str,
    *,
    max_length: int = 80,
    default: str = "document",
) -> str:
    """Convert Unicode text (including Vietnamese đ/Đ) into an ASCII lowercase slug."""
    mapped = normalize_text(value or "", preserve_code=True, strip=False).translate(_VIETNAMESE_D_MAP)
    ascii_text = unicodedata.normalize("NFKD", mapped).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").casefold()
    return slug[:max_length] or default


_TIKTOKEN_FALLBACK_LOCK = threading.Lock()
_tiktoken_fallback_warned = False
_tiktoken_fallback_count = 0
_tiktoken_fallback_reason: str | None = None


def _record_tiktoken_fallback(stage: str, exc: Exception) -> None:
    global _tiktoken_fallback_warned, _tiktoken_fallback_count, _tiktoken_fallback_reason
    reason = f"{stage}:{type(exc).__name__}"
    should_warn = False
    with _TIKTOKEN_FALLBACK_LOCK:
        _tiktoken_fallback_count += 1
        _tiktoken_fallback_reason = reason
        if not _tiktoken_fallback_warned:
            _tiktoken_fallback_warned = True
            should_warn = True
    if should_warn:
        log.warning(
            "tiktoken cl100k_base unavailable (%s); using offline UTF-8 heuristic token estimation",
            reason,
        )


def get_tiktoken_fallback_status() -> dict[str, Any]:
    with _TIKTOKEN_FALLBACK_LOCK:
        return {
            "degraded": _tiktoken_fallback_count > 0,
            "fallback_count": _tiktoken_fallback_count,
            "reason": _tiktoken_fallback_reason,
        }


def reset_tiktoken_fallback_state() -> None:
    global _tiktoken_fallback_warned, _tiktoken_fallback_count, _tiktoken_fallback_reason
    with _TIKTOKEN_FALLBACK_LOCK:
        _tiktoken_fallback_warned = False
        _tiktoken_fallback_count = 0
        _tiktoken_fallback_reason = None
    _tiktoken_encoding.cache_clear()


@lru_cache(maxsize=1)
def _tiktoken_encoding() -> Any | None:
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # noqa: BLE001
        _record_tiktoken_fallback("load", exc)
        return None


def _estimate_tokens_fallback(text: str) -> int:
    cjk_count = len(_CONTINUOUS_SCRIPT_CHAR_RE.findall(text))
    other = _CONTINUOUS_SCRIPT_CHAR_RE.sub("", text)
    return max(1, cjk_count + (len(other.encode("utf-8")) + 2) // 3)


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken cl100k_base with an offline UTF-8 Vietnamese-safe fallback."""
    if not text:
        return 0
    enc = _tiktoken_encoding()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text, disallowed_special=())))
        except Exception as exc:  # noqa: BLE001
            _record_tiktoken_fallback("encode", exc)
    return _estimate_tokens_fallback(text)


def estimate_text_tokens(texts: Sequence[str] | str | None) -> int:
    """Estimate total tokens across a string or sequence of strings."""
    if not texts:
        return 0
    if isinstance(texts, str):
        return estimate_tokens(texts)
    return sum(estimate_tokens(t) for t in texts if t)


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in _PROTECTED_SPAN_RE.finditer(text):
        start, end = match.span()
        while end > start and text[end - 1] in ".,!?;:)":
            end -= 1
        if end > start:
            ranges.append((start, end))
    return ranges


def _in_protected_range(idx: int, ranges: list[tuple[int, int]], ptr: int) -> tuple[bool, int]:
    while ptr < len(ranges) and ranges[ptr][1] <= idx:
        ptr += 1
    if ptr < len(ranges) and ranges[ptr][0] <= idx < ranges[ptr][1]:
        return True, ptr
    return False, ptr


def _is_period_boundary(text: str, dot_idx: int, seg_start: int) -> bool:
    n = len(text)
    # Ellipsis or consecutive dots: look at the end of the dot run
    end_dots = dot_idx + 1
    while end_dots < n and text[end_dots] == ".":
        end_dots += 1
    # Check closing quotes/brackets immediately after dots
    after = end_dots
    while after < n and text[after] in _CLOSING_PUNCT:
        after += 1
    if after < n and not text[after].isspace():
        return False

    # Do not split standalone list numbers at sentence start like "1. First" or "a. First"
    prefix_in_seg = text[seg_start:dot_idx].strip()
    if re.fullmatch(r"(?:\d{1,3}|[A-Za-z])", prefix_in_seg):
        return False

    # Check legal prefixes ("Điều 1.", "Khoản 2.", "Mục 3.")
    line_start = text.rfind("\n", seg_start, dot_idx)
    ctx_start = line_start + 1 if line_start >= 0 else seg_start
    before_dot = text[ctx_start:dot_idx]
    if _LEGAL_PREFIX_BEFORE_DOT_RE.search(before_dot):
        return False

    # Check abbreviations ("TP.", "Mr.", "Dr.", "Prof.", "Mrs.", "Ms.", "e.g.", "i.e.")
    tok_match = _TOKEN_BEFORE_DOT_RE.search(before_dot)
    if tok_match and tok_match.group(1).casefold() in _ABBREVIATIONS:
        return False

    return True


def split_sentence_spans_multilingual(text: str) -> list[tuple[int, int, str]]:
    """Split text into sentence spans (start, end, segment), preserving numbers, URLs, times,
    ratios, abbreviations, and newline boundaries.
    """
    if not text or not text.strip():
        return []

    n = len(text)
    ranges = _protected_ranges(text)
    range_ptr = 0
    spans: list[tuple[int, int, str]] = []
    seg_start = 0
    i = 0

    while i < n:
        ch = text[i]
        if ch == "\n":
            seg = text[seg_start:i].strip()
            if seg:
                raw_start = seg_start
                while raw_start < i and text[raw_start].isspace():
                    raw_start += 1
                raw_end = i
                while raw_end > raw_start and text[raw_end - 1].isspace():
                    raw_end -= 1
                spans.append((raw_start, raw_end, text[raw_start:raw_end]))
            while i < n and text[i] == "\n":
                i += 1
            seg_start = i
            continue

        protected, range_ptr = _in_protected_range(i, ranges, range_ptr)
        if protected:
            i += 1
            continue

        if ch in {"。", "！", "？", "；"}:
            end_idx = i + 1
            while end_idx < n and (text[end_idx] in {"。", "！", "？", "；"} or text[end_idx] in _CLOSING_PUNCT):
                end_idx += 1
            raw_start = seg_start
            while raw_start < end_idx and text[raw_start].isspace():
                raw_start += 1
            if raw_start < end_idx:
                spans.append((raw_start, end_idx, text[raw_start:end_idx]))
            i = end_idx
            while i < n and text[i] in (" ", "\t"):
                i += 1
            seg_start = i
            continue

        if ch in {"!", "?", ";"}:
            end_idx = i + 1
            while end_idx < n and (text[end_idx] in {"!", "?", ";", "."} or text[end_idx] in _CLOSING_PUNCT):
                end_idx += 1
            if end_idx >= n or text[end_idx].isspace():
                raw_start = seg_start
                while raw_start < end_idx and text[raw_start].isspace():
                    raw_start += 1
                if raw_start < end_idx:
                    spans.append((raw_start, end_idx, text[raw_start:end_idx]))
                i = end_idx
                while i < n and text[i] in (" ", "\t"):
                    i += 1
                seg_start = i
                continue

        if ch == ".":
            end_dots = i + 1
            while end_dots < n and text[end_dots] == ".":
                end_dots += 1
            if _is_period_boundary(text, i, seg_start):
                end_idx = end_dots
                while end_idx < n and text[end_idx] in _CLOSING_PUNCT:
                    end_idx += 1
                raw_start = seg_start
                while raw_start < end_idx and text[raw_start].isspace():
                    raw_start += 1
                if raw_start < end_idx:
                    spans.append((raw_start, end_idx, text[raw_start:end_idx]))
                i = end_idx
                while i < n and text[i] in (" ", "\t"):
                    i += 1
                seg_start = i
                continue
            i = end_dots
            continue

        i += 1

    if seg_start < n:
        raw_start = seg_start
        while raw_start < n and text[raw_start].isspace():
            raw_start += 1
        raw_end = n
        while raw_end > raw_start and text[raw_end - 1].isspace():
            raw_end -= 1
        if raw_start < raw_end:
            spans.append((raw_start, raw_end, text[raw_start:raw_end]))

    return spans


def split_sentences_multilingual(text: str) -> list[str]:
    """Normalize text to NFC and split into sentences while preserving numbers, URLs, times,
    ratios, abbreviations, and meaningful newlines.
    """
    normalized = normalize_text(text or "", preserve_code=True, strip=True)
    if not normalized:
        return []
    return [seg for _start, _end, seg in split_sentence_spans_multilingual(normalized)]


def split_clauses_multilingual(text: str) -> list[str]:
    """Split a long sentence into clauses without breaking decimal/thousands commas, times, ratios, or URLs."""
    normalized = normalize_text(text or "", preserve_code=True, strip=True)
    if not normalized:
        return []
    # Protect URLs/emails temporarily during regex clause splitting
    protected_tokens: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        idx = len(protected_tokens)
        protected_tokens.append(match.group(0))
        return f"\x00URL{idx}\x00"

    masked = _PROTECTED_SPAN_RE.sub(_stash, normalized)
    parts = [p.strip() for p in _MULTILINGUAL_CLAUSE_SPLIT_RE.split(masked) if p and p.strip()]
    if not protected_tokens:
        return parts
    restored: list[str] = []
    for part in parts:
        for idx, tok in enumerate(protected_tokens):
            part = part.replace(f"\x00URL{idx}\x00", tok)
        restored.append(part)
    return restored
