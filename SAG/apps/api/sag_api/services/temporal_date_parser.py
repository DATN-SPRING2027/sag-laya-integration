"""Deterministic VI/EN point-in-time `as_of` date parser (pure stdlib, linear-time O(N))."""

from __future__ import annotations

import calendar
import re
import unicodedata
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta

_MAX_QUERY_CHARS = 4096
_MIN_YEAR = 1970
_MAX_YEAR = 2100
_DAYS_IN_MONTH = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_WS_RE = re.compile(r"\s+")
_ASCII_LETTER_RE = re.compile(r"[a-z]")
_WORD_CHAR_RE = re.compile(r"[a-zà-ỹ0-9_]")

_EN_MONTHS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_EN_MONTH_PATTERN = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_YEAR_PATTERN = r"(?:19[7-9]\d|20\d{2}|2100)"

# All patterns below run against a single pre-lowercased NFC string (no re.IGNORECASE overhead).

# Exclusive-before prefix checked over the bounded window immediately preceding a match.
_BEFORE_PREFIX_RE = re.compile(
    r"(?:^|[\s,(;:])(?:"
    r"trước(?: (?:khi|ngày|tháng|quý|năm))?"
    r"|truoc(?: (?:khi|ngay|thang|quy|nam))?"
    r"|(?:before|prior to|earlier than|preceding)(?: (?:the )?(?:year|month|quarter|day))?"
    r") ?$",
)

# False-positive guards
_TRAILING_HARD_UNIT_RE = re.compile(
    r"^ ?(?:%|(?:vnđ|vnd|đồng|dong|usd|eur|gbp|jpy|px|kb|mb|gb|tb|ms|km|kg|cm|mm|m2)(?![a-zà-ỹ0-9_]))",
)
_TRAILING_COUNT_UNIT_RE = re.compile(
    r"^ (?:triệu|trieu|tỷ|ty|tỉ|ti|nghìn|nghin|ngàn|ngan|"
    r"tokens?|người|nguoi|users?|items?|requests?|cái|chiếc)(?![a-zà-ỹ0-9_])",
)
_TRAILING_LEGAL_SUFFIX_RE = re.compile(
    r"^ ?/ ?(?:nđ|nd|tt|qđ|qd|cv|nq|tb|hd|qh|cp|btc|bct|ttg|bnv|blđtbxh|byt)(?![a-zà-ỹ0-9_])",
)
_LEADING_ID_PREFIX_RE = re.compile(
    r"(?:^|[\s,(;:])(?:"
    r"số|so|no\.?|mã|ma|code|id"
    r"|nghị định|nghi dinh|thông tư|thong tu|quyết định|quyet dinh|công văn|cong van"
    r"|luật|luat|hợp đồng|hop dong|phụ lục|phu luc|bản|phiên bản|version|ver\.?"
    r"|nđ|nd|tt|qđ|qd|cv|nq"
    r"|port|cổng|cong|tcp|udp|http|err|error|rfc|iso|iec|tcvn|qcvn"
    r"|pr|issue|ticket|jira|sup|doc|clause"
    r"|điều|dieu|khoản|khoan|mục|muc|chương|chuong|trang|page|line|dòng|dong|step|bước|buoc"
    r") ?[#:\-]? ?$",
)

# Tier 1 — Exact full dates
_ISO_DATE_RE = re.compile(
    rf"(?<![a-z0-9_/-])({_YEAR_PATTERN})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?![a-z0-9_/-])",
)
# Match 1-2 digit day & month so invalid dates like "ngày 32 tháng 1 năm 2025" or
# "ngày 00 tháng 1 năm 2025" are recorded in invalid_spans instead of falling through to Tier 3/4.
_VN_VERBOSE_DATE_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_])(?:ngày|ngay) (\d{{1,2}}) (?:tháng|thang) "
    rf"(\d{{1,2}}) (?:năm|nam) ({_YEAR_PATTERN})(?![a-zà-ỹ0-9_])",
)
_VN_NUMERIC_DATE_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_/-])(?<!\d\.)"
    rf"(0?[1-9]|[12]\d|3[01])([/.-])(0?[1-9]|1[0-2])\2({_YEAR_PATTERN})"
    r"(?![a-zà-ỹ0-9_/-])(?!\.\d)",
)
_EN_MDY_DATE_RE = re.compile(
    rf"(?<![a-z0-9_-])({_EN_MONTH_PATTERN})\.? "
    rf"(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,? ({_YEAR_PATTERN})(?![a-z0-9_-])",
)
_EN_DMY_DATE_RE = re.compile(
    rf"(?<![a-z0-9_-])(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)? "
    rf"(?:of )?({_EN_MONTH_PATTERN})\.?,? ({_YEAR_PATTERN})(?![a-z0-9_-])",
)

# Tier 2 — Quarter-Year
_VN_QUARTER_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_])(?:quý|quy) ?([1-4])(?: (?:năm|nam) | ?[/.-] ?| )({_YEAR_PATTERN})(?![a-zà-ỹ0-9_/-])",
)
_EN_QUARTER_FIRST_RE = re.compile(
    rf"(?<![a-z0-9_/-])q([1-4])(?: ?[/.-] ?| ?)({_YEAR_PATTERN})(?![a-z0-9_/-])",
)
_EN_YEAR_QUARTER_RE = re.compile(
    rf"(?<![a-z0-9_/-])({_YEAR_PATTERN})(?: ?[-/] ?| ?)q([1-4])(?![a-z0-9_/-])",
)

# Tier 3 — Month-Year
_VN_MONTH_YEAR_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_])(?:tháng|thang) (0?[1-9]|1[0-2])"
    rf"(?: (?:năm|nam) | ?[/.-] ?| )({_YEAR_PATTERN})(?![a-zà-ỹ0-9_/-])",
)
_NUMERIC_MONTH_YEAR_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_/-])(0?[1-9]|1[0-2])/({_YEAR_PATTERN})(?![a-zà-ỹ0-9_/-])",
)
_ISO_YEAR_MONTH_RE = re.compile(
    rf"(?<![a-z0-9_/-])({_YEAR_PATTERN})-(0[1-9]|1[0-2])(?![a-z0-9_/-])",
)
_EN_MONTH_YEAR_RE = re.compile(
    rf"(?<![a-z0-9_-])({_EN_MONTH_PATTERN})\.?,? ({_YEAR_PATTERN})(?![a-z0-9_-])",
)

# Tier 4 — Cued Year ONLY (never bare naked 4-digit numbers)
_CUED_YEAR_RE = re.compile(
    rf"(?<![a-zà-ỹ0-9_])(năm|nam|in|of|during|year|as of|by|until|before|prior to|"
    rf"trước|truoc|tính đến|tinh den|vào|vao|cuối|cuoi|đầu|dau) ({_YEAR_PATTERN})(?![a-zà-ỹ0-9_/-])",
)
_EXPLICIT_YEAR_CUES = frozenset({"năm", "nam", "year"})

# Tier 5 — Relative expressions anchored to reference_time in UTC
_REL_YESTERDAY_RE = re.compile(
    r"(?<![a-zà-ỹ0-9_])(?:hôm qua|hom qua|yesterday)(?![a-zà-ỹ0-9_])",
)
_REL_LAST_WEEK_RE = re.compile(
    r"(?<![a-zà-ỹ0-9_])(?:tuần trước|tuan truoc|(?:last|previous) week)(?![a-zà-ỹ0-9_])",
)
_REL_LAST_MONTH_RE = re.compile(
    r"(?<![a-zà-ỹ0-9_])(?:tháng trước|thang truoc|(?:last|previous) month)(?![a-zà-ỹ0-9_])",
)
_REL_LAST_QUARTER_RE = re.compile(
    r"(?<![a-zà-ỹ0-9_])(?:quý trước|quy truoc|(?:last|previous) quarter)(?![a-zà-ỹ0-9_])",
)
_REL_LAST_YEAR_RE = re.compile(
    r"(?<![a-zà-ỹ0-9_])(?:năm ngoái|nam ngoai|năm trước|nam truoc|(?:last|previous) year)(?![a-zà-ỹ0-9_])",
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_valid_ymd(year: int, month: int, day: int) -> bool:
    if not (_MIN_YEAR <= year <= _MAX_YEAR) or not (1 <= month <= 12) or day < 1:
        return False
    max_day = _DAYS_IN_MONTH[month]
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        max_day = 29
    return day <= max_day


def _day_bounds(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    if not _is_valid_ymd(year, month, day):
        raise ValueError("date out of range")
    start = datetime(year, month, day, 0, 0, 0, 0, tzinfo=UTC)
    end = datetime(year, month, day, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end


def _month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    if not (_MIN_YEAR <= year <= _MAX_YEAR) or not (1 <= month <= 12):
        raise ValueError("month out of range")
    last_day = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, 0, 0, tzinfo=UTC)
    end = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end


def _quarter_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    if not (_MIN_YEAR <= year <= _MAX_YEAR) or not (1 <= quarter <= 4):
        raise ValueError("quarter out of range")
    start_month = (quarter - 1) * 3 + 1
    end_month = quarter * 3
    last_day = calendar.monthrange(year, end_month)[1]
    start = datetime(year, start_month, 1, 0, 0, 0, 0, tzinfo=UTC)
    end = datetime(year, end_month, last_day, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end


def _year_bounds(year: int) -> tuple[datetime, datetime]:
    if not (_MIN_YEAR <= year <= _MAX_YEAR):
        raise ValueError("year out of range")
    start = datetime(year, 1, 1, 0, 0, 0, 0, tzinfo=UTC)
    end = datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end


def _has_before_prefix(lower_text: str, pos: int) -> bool:
    if pos <= 0:
        return False
    prefix = lower_text[max(0, pos - 36) : pos]
    return bool(_BEFORE_PREFIX_RE.search(prefix))


def _preceded_by_ngay(lower_text: str, start: int) -> bool:
    if start < 5:
        return False
    if lower_text[start - 5 : start] not in ("ngày ", "ngay "):
        return False
    return start == 5 or _WORD_CHAR_RE.match(lower_text[start - 6]) is None


def _resolve_boundary(
    lower_text: str,
    prefix_pos: int,
    period_start: datetime,
    period_end: datetime,
) -> datetime:
    if _has_before_prefix(lower_text, prefix_pos):
        return period_start - timedelta(microseconds=1)
    return period_end


def _overlaps_any(start: int, end: int, starts: list[int], ends: list[int]) -> bool:
    """O(log K) overlap check against sorted disjoint invalid spans."""
    if not starts:
        return False
    idx = bisect_left(starts, end)
    return idx > 0 and ends[idx - 1] > start


def _add_invalid_span(start: int, end: int, starts: list[int], ends: list[int]) -> None:
    """Insert [start, end) into sorted disjoint span arrays (O(1) monotonic fast path)."""
    if not starts or start > ends[-1]:
        starts.append(start)
        ends.append(end)
        return
    if start == ends[-1]:
        if end > ends[-1]:
            ends[-1] = end
        return
    lo = bisect_left(ends, start)
    hi = bisect_right(starts, end)
    if lo < hi:
        if starts[lo] < start:
            start = starts[lo]
        if ends[hi - 1] > end:
            end = ends[hi - 1]
    starts[lo:hi] = [start]
    ends[lo:hi] = [end]


def _enclosing_tokens_have_phone_digits(lower_text: str, start: int, end: int) -> bool:
    tok_start = lower_text.rfind(" ", 0, start)
    tok_start = 0 if tok_start < 0 else tok_start + 1
    tok_end = lower_text.find(" ", end)
    tok_end = len(lower_text) if tok_end < 0 else tok_end
    for token in lower_text[tok_start:tok_end].split(" "):
        if sum(ch.isdigit() for ch in token) >= 10:
            return True
    return False


def _is_false_positive_match(
    lower_text: str,
    start: int,
    end: int,
    *,
    explicit_calendar_noun: bool = False,
) -> bool:
    if _enclosing_tokens_have_phone_digits(lower_text, start, end):
        return True
    after = lower_text[end : min(len(lower_text), end + 24)]
    if _TRAILING_HARD_UNIT_RE.search(after):
        return True
    if _TRAILING_LEGAL_SUFFIX_RE.search(after):
        return True
    if not explicit_calendar_noun and _TRAILING_COUNT_UNIT_RE.search(after):
        return True
    if not explicit_calendar_noun and start > 0:
        before = lower_text[max(0, start - 32) : start]
        if _LEADING_ID_PREFIX_RE.search(before):
            return True
    return False


def _relative_period(lower_text: str, ref: datetime) -> datetime | None:
    """Resolve Tier 5 relative expressions anchored to ref (UTC); return None if out of bounds."""
    if not (_MIN_YEAR <= ref.year <= _MAX_YEAR):
        return None
    try:
        m_yest = _REL_YESTERDAY_RE.search(lower_text)
        if m_yest is not None:
            target_day = ref.date() - timedelta(days=1)
            p_start, p_end = _day_bounds(target_day.year, target_day.month, target_day.day)
            return _resolve_boundary(lower_text, m_yest.start(), p_start, p_end)

        m_week = _REL_LAST_WEEK_RE.search(lower_text)
        if m_week is not None:
            prev_sun = ref.date() - timedelta(days=ref.weekday() + 1)
            prev_mon = prev_sun - timedelta(days=6)
            p_start, _ = _day_bounds(prev_mon.year, prev_mon.month, prev_mon.day)
            _, p_end = _day_bounds(prev_sun.year, prev_sun.month, prev_sun.day)
            return _resolve_boundary(lower_text, m_week.start(), p_start, p_end)

        m_month = _REL_LAST_MONTH_RE.search(lower_text)
        if m_month is not None:
            prev_y, prev_m = (ref.year - 1, 12) if ref.month == 1 else (ref.year, ref.month - 1)
            p_start, p_end = _month_bounds(prev_y, prev_m)
            return _resolve_boundary(lower_text, m_month.start(), p_start, p_end)

        m_quarter = _REL_LAST_QUARTER_RE.search(lower_text)
        if m_quarter is not None:
            cur_q = (ref.month - 1) // 3 + 1
            prev_y, prev_q = (ref.year - 1, 4) if cur_q == 1 else (ref.year, cur_q - 1)
            p_start, p_end = _quarter_bounds(prev_y, prev_q)
            return _resolve_boundary(lower_text, m_quarter.start(), p_start, p_end)

        m_year = _REL_LAST_YEAR_RE.search(lower_text)
        if m_year is not None:
            p_start, p_end = _year_bounds(ref.year - 1)
            return _resolve_boundary(lower_text, m_year.start(), p_start, p_end)
    except (ValueError, OverflowError):
        return None
    return None


def extract_as_of_datetime(
    query: str,
    *,
    reference_time: datetime | None = None,
) -> datetime | None:
    """Deterministically extract a UTC point-in-time `as_of` boundary from a VI/EN query.

    Returns `None` if no valid temporal point expression is found or if false-positive
    guards reject the token. Linear-time O(N log K), ReDoS-safe, bounded to first 4096 chars.
    """
    clipped = (query or "")[:_MAX_QUERY_CHARS]
    if not clipped:
        return None
    raw = clipped if clipped.isascii() else unicodedata.normalize("NFC", clipped)
    lower_text = _WS_RE.sub(" ", raw).strip().lower()
    if not lower_text:
        return None

    ref = _as_utc(reference_time) if reference_time is not None else datetime.now(UTC)
    invalid_starts: list[int] = []
    invalid_ends: list[int] = []

    has_dash = "-" in lower_text
    has_slash = "/" in lower_text
    has_dot = "." in lower_text
    has_ascii_alpha = _ASCII_LETTER_RE.search(lower_text) is not None

    # Tier 1 — Exact full date
    if has_dash:
        for m in _ISO_DATE_RE.finditer(lower_text):
            start, end = m.span()
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not _is_valid_ymd(year, month, day):
                _add_invalid_span(start, end, invalid_starts, invalid_ends)
                continue
            if _is_false_positive_match(lower_text, start, end):
                continue
            p_start, p_end = _day_bounds(year, month, day)
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if "ngày " in lower_text or "ngay " in lower_text:
        check_prev = bool(invalid_starts)
        for m in _VN_VERBOSE_DATE_RE.finditer(lower_text):
            start, end = m.span()
            if check_prev and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            year, month, day = int(m.group(3)), int(m.group(2)), int(m.group(1))
            if not _is_valid_ymd(year, month, day):
                _add_invalid_span(start, end, invalid_starts, invalid_ends)
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            p_start, p_end = _day_bounds(year, month, day)
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if has_slash or has_dash or has_dot:
        check_prev = bool(invalid_starts)
        for m in _VN_NUMERIC_DATE_RE.finditer(lower_text):
            start, end = m.span()
            if check_prev and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            year, month, day = int(m.group(4)), int(m.group(3)), int(m.group(1))
            if not _is_valid_ymd(year, month, day):
                _add_invalid_span(start, end, invalid_starts, invalid_ends)
                continue
            has_ngay = _preceded_by_ngay(lower_text, start)
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=has_ngay):
                continue
            p_start, p_end = _day_bounds(year, month, day)
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if has_ascii_alpha:
        check_prev = bool(invalid_starts)
        for m in _EN_MDY_DATE_RE.finditer(lower_text):
            start, end = m.span()
            if check_prev and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            month_num = _EN_MONTHS.get(m.group(1))
            if month_num is None:
                continue
            year, day = int(m.group(3)), int(m.group(2))
            if not _is_valid_ymd(year, month_num, day):
                _add_invalid_span(start, end, invalid_starts, invalid_ends)
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            p_start, p_end = _day_bounds(year, month_num, day)
            return _resolve_boundary(lower_text, start, p_start, p_end)

        check_prev = bool(invalid_starts)
        for m in _EN_DMY_DATE_RE.finditer(lower_text):
            start, end = m.span()
            if check_prev and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            month_num = _EN_MONTHS.get(m.group(2))
            if month_num is None:
                continue
            year, day = int(m.group(3)), int(m.group(1))
            if not _is_valid_ymd(year, month_num, day):
                _add_invalid_span(start, end, invalid_starts, invalid_ends)
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            p_start, p_end = _day_bounds(year, month_num, day)
            return _resolve_boundary(lower_text, start, p_start, p_end)

    # Tier 2 — Quarter-Year
    if "quý" in lower_text or "quy" in lower_text:
        for m in _VN_QUARTER_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            p_start, p_end = _quarter_bounds(int(m.group(2)), int(m.group(1)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if "q" in lower_text:
        for m in _EN_QUARTER_FIRST_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end):
                continue
            p_start, p_end = _quarter_bounds(int(m.group(2)), int(m.group(1)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

        for m in _EN_YEAR_QUARTER_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end):
                continue
            p_start, p_end = _quarter_bounds(int(m.group(1)), int(m.group(2)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

    # Tier 3 — Month-Year
    if "tháng " in lower_text or "thang " in lower_text:
        for m in _VN_MONTH_YEAR_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            p_start, p_end = _month_bounds(int(m.group(2)), int(m.group(1)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if has_slash:
        for m in _NUMERIC_MONTH_YEAR_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end):
                continue
            p_start, p_end = _month_bounds(int(m.group(2)), int(m.group(1)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if has_dash:
        for m in _ISO_YEAR_MONTH_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end):
                continue
            p_start, p_end = _month_bounds(int(m.group(1)), int(m.group(2)))
            return _resolve_boundary(lower_text, start, p_start, p_end)

    if has_ascii_alpha:
        for m in _EN_MONTH_YEAR_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=True):
                continue
            month_num = _EN_MONTHS.get(m.group(1))
            if month_num is None:
                continue
            p_start, p_end = _month_bounds(int(m.group(2)), month_num)
            return _resolve_boundary(lower_text, start, p_start, p_end)

    # Tier 4 — Cued Year ONLY
    if has_ascii_alpha:
        for m in _CUED_YEAR_RE.finditer(lower_text):
            start, end = m.span()
            if invalid_starts and _overlaps_any(start, end, invalid_starts, invalid_ends):
                continue
            is_cal_noun = m.group(1) in _EXPLICIT_YEAR_CUES
            if _is_false_positive_match(lower_text, start, end, explicit_calendar_noun=is_cal_noun):
                continue
            p_start, p_end = _year_bounds(int(m.group(2)))
            # Check prefix up to the year token start so "before 2025" / "trước 2025" / "trước năm 2025"
            # are all recognized as exclusive-before boundaries.
            return _resolve_boundary(lower_text, m.start(2), p_start, p_end)

    # Tier 5 — Relative expressions anchored to ref in UTC
    if has_ascii_alpha:
        return _relative_period(lower_text, ref)
    return None
