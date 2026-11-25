"""Shared, regex-free `<think>` stripper, markdown fence extractor, and JSON raw decoder."""

from __future__ import annotations

import json
from typing import Any

_OPEN_THINK = "<think>"
_CLOSE_THINK = "</think>"


def strip_think_blocks(raw_text: str) -> str:
    """Strip one or more `<think>...</think>` blocks, handling truncated/unclosed blocks safely."""
    text = (raw_text or "").strip()
    if not text:
        return ""
    while True:
        lower = text.lower()
        start = lower.find(_OPEN_THINK)
        if start == -1:
            return text
        end = lower.find(_CLOSE_THINK, start + len(_OPEN_THINK))
        if end == -1:
            before = text[:start].strip()
            if "{" in before or "[" in before:
                return before
            after = text[start + len(_OPEN_THINK) :].strip()
            return after if ("{" in after or "[" in after) else before
        text = (text[:start] + " " + text[end + len(_CLOSE_THINK) :]).strip()


def extract_fenced_blocks_and_outside(text: str) -> tuple[list[str], str]:
    """Scan lines for ```...``` fenced blocks without greedy regexes.

    Returns ``(fenced_bodies, outside_text)``. Unclosed trailing fences are
    treated as a candidate body if they start after a ``` marker.
    """
    if not text:
        return [], ""
    fenced_bodies: list[str] = []
    outside_lines: list[str] = []
    in_fence = False
    current_body: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```"):
                in_fence = True
                current_body = []
                rest = stripped[3:].strip()
                if rest.endswith("```") and len(rest) >= 3:
                    inner = rest[:-3].strip()
                    if inner.lower().startswith("json"):
                        first_ws = inner.find(" ")
                        inner = inner[first_ws + 1 :].strip() if first_ws != -1 else ""
                    if inner:
                        fenced_bodies.append(inner)
                    in_fence = False
                    current_body = []
                elif "{" in rest or "[" in rest:
                    obj_idx = rest.find("{")
                    arr_idx = rest.find("[")
                    start_idx = min(i for i in (obj_idx, arr_idx) if i != -1)
                    current_body.append(rest[start_idx:])
                continue
            outside_lines.append(line)
        else:
            if stripped.startswith("```"):
                body = "\n".join(current_body).strip()
                if body:
                    fenced_bodies.append(body)
                in_fence = False
                current_body = []
            else:
                current_body.append(line)

    if in_fence and current_body:
        body = "\n".join(current_body).strip()
        if body:
            fenced_bodies.append(body)

    return fenced_bodies, "\n".join(outside_lines).strip()


def strip_markdown_fences(text: str) -> list[str]:
    """Return candidate JSON strings from markdown fences plus cleaned text."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    fenced_bodies, outside = extract_fenced_blocks_and_outside(cleaned)
    candidates: list[str] = []
    for body in fenced_bodies:
        if body and body not in candidates:
            candidates.append(body)
    if outside and outside not in candidates:
        candidates.append(outside)
    if cleaned not in candidates:
        candidates.append(cleaned)
    return candidates


def decode_json_values(
    raw_text: str,
    *,
    expected_type: type | tuple[type, ...] | None = None,
) -> list[Any]:
    """Decode all JSON values in ``raw_text`` using ``json.JSONDecoder.raw_decode``."""
    cleaned = strip_think_blocks(raw_text)
    if not cleaned:
        return []

    decoder = json.JSONDecoder()
    results: list[Any] = []

    def _matches(val: Any) -> bool:
        return expected_type is None or isinstance(val, expected_type)

    def _scan(candidate: str) -> None:
        idx = 0
        length = len(candidate)
        while idx < length:
            next_obj = candidate.find("{", idx)
            next_arr = candidate.find("[", idx)
            if next_obj == -1 and next_arr == -1:
                break
            if next_obj == -1:
                pos = next_arr
            elif next_arr == -1:
                pos = next_obj
            else:
                pos = min(next_obj, next_arr)
            try:
                value, end_pos = decoder.raw_decode(candidate, pos)
            except json.JSONDecodeError:
                idx = pos + 1
                continue
            if _matches(value):
                results.append(value)
            idx = max(end_pos, pos + 1)

    for candidate in strip_markdown_fences(cleaned):
        try:
            direct = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            _scan(candidate)
        else:
            if _matches(direct):
                results.append(direct)
        if results:
            return results

    return results


def decode_json_object(raw_text: str) -> dict[str, Any] | None:
    """Decode the first JSON object (`dict`) from ``raw_text``, or return ``None``."""
    values = decode_json_values(raw_text, expected_type=dict)
    return values[0] if values else None
