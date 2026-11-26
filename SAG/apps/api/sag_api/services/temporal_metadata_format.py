"""Pure citation-safe formatter for temporal and authority evidence metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sag_api.enums import AUTHORITY_TIER_LABELS

if TYPE_CHECKING:
    from sag_api.sag.dto import RetrievedSection

_MAX_SUFFIX_LEN = 50
_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _extract_field(item: RetrievedSection | Mapping[str, Any], name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _format_iso_date(value: Any, *, exclude_sentinel: bool = False) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.astimezone(UTC) if value.tzinfo is not None else value
        if exclude_sentinel and dt.year >= 9999:
            return None
        return dt.strftime("%Y-%m-%d")
    if isinstance(value, date):
        if exclude_sentinel and value.year >= 9999:
            return None
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        normalized = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            if _ISO_DATE_PREFIX_RE.match(raw):
                year = int(raw[:4])
                if exclude_sentinel and year >= 9999:
                    return None
                return raw[:10]
            return None
        dt = parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed
        if exclude_sentinel and dt.year >= 9999:
            return None
        return dt.strftime("%Y-%m-%d")
    return None


def _sanitize_brackets(text: str) -> str:
    return text.replace("[", "").replace("]", "")


def format_evidence_metadata_suffix(
    item: RetrievedSection | Mapping[str, Any] | None,
) -> str:
    """Render a compact, bracket-free metadata suffix (<= 50 chars) or '' if unset.

    Shared verbatim across:
      1. ``tools/builtin.py:_format_sections``
      2. ``services/evidence_service.py:_search_prompt_messages``
      3. ``services/agent_service.py:_verify_grounded_entailment``
    """
    if item is None:
        return ""

    authority_tier = _extract_field(item, "authority_tier")
    effective_from = _extract_field(item, "effective_from")
    valid_to = _extract_field(item, "valid_to")
    is_superseded = bool(_extract_field(item, "is_superseded"))
    raw_conflict_status = _extract_field(item, "conflict_status")
    conflict_status = (
        raw_conflict_status.strip().upper()
        if isinstance(raw_conflict_status, str)
        else ""
    )

    # Status prefix (at most 1)
    status_part: str | None = None
    if is_superseded or conflict_status == "SUPERSEDED_BY_UNIT":
        status_part = "Bản cũ"
    elif conflict_status == "ACTIVE_CONFLICT":
        status_part = "Xung đột"

    # Authority label
    authority_part: str | None = None
    if (
        isinstance(authority_tier, int)
        and not isinstance(authority_tier, bool)
        and int(authority_tier) in AUTHORITY_TIER_LABELS
    ):
        authority_part = AUTHORITY_TIER_LABELS[int(authority_tier)]

    # Date label
    eff_date = _format_iso_date(effective_from, exclude_sentinel=False)
    valid_to_date = (
        _format_iso_date(valid_to, exclude_sentinel=True)
        if eff_date is None and is_superseded
        else None
    )

    base_parts: list[str] = []
    if status_part:
        base_parts.append(status_part)
    if authority_part:
        base_parts.append(authority_part)

    if eff_date is not None:
        full_candidate = f"({' · '.join([*base_parts, f'Hiệu lực {eff_date}'])})"
        if len(full_candidate) <= _MAX_SUFFIX_LEN:
            out = full_candidate
        else:
            out = f"({' · '.join([*base_parts, f'HL {eff_date}'])})"
    elif valid_to_date is not None:
        out = f"({' · '.join([*base_parts, f'Hết HL {valid_to_date}'])})"
    elif base_parts:
        out = f"({' · '.join(base_parts)})"
    else:
        return ""

    out = _sanitize_brackets(out)
    if len(out) > _MAX_SUFFIX_LEN:
        out = f"{out[: _MAX_SUFFIX_LEN - 1].rstrip(' ·')})"
    return out
