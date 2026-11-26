"""Deterministic VI/EN point-in-time `as_of` date parser (pure stdlib, linear-time O(N))."""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import UTC, datetime, timedelta

_MAX_QUERY_CHARS = 4096
_MIN_YEAR = 1970
_MAX_YEAR = 2100

_WS_RE = re.compile(r"\s+")

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

# Exclusive-before prefix checked over the bounded window immediately preceding a match.
_BEFORE_PREFIX_RE = re.compile(
    r"(?:^|[\s,(;:])(?:"
    r"trước(?: (?:khi|ngày|tháng|quý|năm))?"
    r"|truoc(?: (?:khi|ngay|thang|quy|nam))?"
    r"|(?:before|prior to|earlier than|preceding)(?: (?:the )?(?:year|month|quarter|day))?"
    r") ?$",
    re.IGNORECASE,
)

# False-positive guards
_TRAILING_HARD_UNIT_RE = re.compile(
    r"^ ?(?:%|(?:vnđ|vnd|đồng|dong|usd|eur|gbp|jpy|px|kb|mb|gb|tb|ms|km|kg|cm|mm|m2)(?![A-Za-zÀ-ỹ0-9_]))",
    re.IGNORECASE,
)
_TRAILING_COUNT_UNIT_RE = re.compile(
    r"^ (?:triệu|trieu|tỷ|ty|tỉ|ti|nghìn|nghin|ngàn|ngan|"
    r"tokens?|người|nguoi|users?|items?|requests?|cái|chiếc)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_TRAILING_LEGAL_SUFFIX_RE = re.compile(
    r"^ ?/ ?(?:NĐ|ND|TT|QĐ|QD|CV|NQ|TB|HD|QH|CP|BTC|BCT|TTg|BNV|BLĐTBXH|BYT)\b",
    re.IGNORECASE,
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
    re.IGNORECASE,
)

# Tier 1 — Exact full dates
_ISO_DATE_RE = re.compile(
    rf"(?<![A-Za-z0-9_/-])({_YEAR_PATTERN})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])(?![A-Za-z0-9_/-])",
    re.IGNORECASE,
)
_VN_VERBOSE_DATE_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_])(?:ngày|ngay) (0?[1-9]|[12]\d|3[01]) (?:tháng|thang) "
    rf"(0?[1-9]|1[0-2]) (?:năm|nam) ({_YEAR_PATTERN})(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_VN_NUMERIC_DATE_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_/-])(?<!\d\.)(?:(ngày|ngay) )?"
    rf"(0?[1-9]|[12]\d|3[01])([/.-])(0?[1-9]|1[0-2])\3({_YEAR_PATTERN})"
    r"(?![A-Za-zÀ-ỹ0-9_/-])(?!\.\d)",
    re.IGNORECASE,
)
_EN_MDY_DATE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])({_EN_MONTH_PATTERN})\.? "
    rf"(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,? ({_YEAR_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_EN_DMY_DATE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)? "
    rf"(?:of )?({_EN_MONTH_PATTERN})\.?,? ({_YEAR_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# Tier 2 — Quarter-Year
_VN_QUARTER_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_])(?:quý|quy) ?([1-4])(?: (?:năm|nam) | ?[/.-] ?| )({_YEAR_PATTERN})(?![A-Za-zÀ-ỹ0-9_/-])",
    re.IGNORECASE,
)
_EN_QUARTER_FIRST_RE = re.compile(
    rf"(?<![A-Za-z0-9_/-])q([1-4])(?: ?[/.-] ?| ?)({_YEAR_PATTERN})(?![A-Za-z0-9_/-])",
    re.IGNORECASE,
)
_EN_YEAR_QUARTER_RE = re.compile(
    rf"(?<![A-Za-z0-9_/-])({_YEAR_PATTERN})(?: ?[-/] ?| ?)q([1-4])(?![A-Za-z0-9_/-])",
    re.IGNORECASE,
)

# Tier 3 — Month-Year
_VN_MONTH_YEAR_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_])(?:tháng|thang) (0?[1-9]|1[0-2])"
    rf"(?: (?:năm|nam) | ?[/.-] ?| )({_YEAR_PATTERN})(?![A-Za-zÀ-ỹ0-9_/-])",
    re.IGNORECASE,
)
_NUMERIC_MONTH_YEAR_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_/-])(0?[1-9]|1[0-2])/({_YEAR_PATTERN})(?![A-Za-zÀ-ỹ0-9_/-])",
    re.IGNORECASE,
)
_ISO_YEAR_MONTH_RE = re.compile(
    rf"(?<![A-Za-z0-9_/-])({_YEAR_PATTERN})-(0[1-9]|1[0-2])(?![A-Za-z0-9_/-])",
    re.IGNORECASE,
)
_EN_MONTH_YEAR_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])({_EN_MONTH_PATTERN})\.?,? ({_YEAR_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# Tier 4 — Cued Year ONLY (never bare naked 4-digit numbers)
_CUED_YEAR_RE = re.compile(
    rf"(?<![A-Za-zÀ-ỹ0-9_])(năm|nam|in|of|during|year|as of|by|until|before|prior to|"
    rf"trước|truoc|tính đến|tinh den|vào|vao|cuối|cuoi|đầu|dau) ({_YEAR_PATTERN})(?![A-Za-zÀ-ỹ0-9_/-])",
    re.IGNORECASE,
)
_EXPLICIT_YEAR_CUES = frozenset({"năm", "nam", "year"})

# Tier 5 — Relative expressions anchored to reference_time in UTC
_REL_YESTERDAY_RE = re.compile(
    r"(?<![A-Za-zÀ-ỹ0-9_])(?:hôm qua|hom qua|yesterday)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_REL_LAST_WEEK_RE = re.compile(
    r"(?<![A-Za-zÀ-ỹ0-9_])(?:tuần trước|tuan truoc|(?:last|previous) week)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_REL_LAST_MONTH_RE = re.compile(
    r"(?<![A-Za-zÀ-ỹ0-9_])(?:tháng trước|thang truoc|(?:last|previous) month)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_REL_LAST_QUARTER_RE = re.compile(
    r"(?<![A-Za-zÀ-ỹ0-9_])(?:quý trước|quy truoc|(?:last|previous) quarter)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)
_REL_LAST_YEAR_RE = re.compile(
    r"(?<![A-Za-zÀ-ỹ0-9_])(?:năm ngoái|nam ngoai|năm trước|nam truoc|(?:last|previous) year)(?![A-Za-zÀ-ỹ0-9_])",
    re.IGNORECASE,
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _day_bounds(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    if not (_MIN_YEAR <= year <= _MAX_YEAR):
        raise ValueError("year out of range")
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


def _has_before_prefix(text: str, pos: int) -> bool:
    if pos <= 0:
        return False
    prefix = text[max(0, pos - 36) : pos]
    return bool(_BEFORE_PREFIX_RE.search(prefix))


def _resolve_boundary(
    text: str,
    prefix_pos: int,
    period_start: datetime,
    period_end: datetime,
) -> datetime:
    if _has_before_prefix(text, prefix_pos):
        return period_start - timedelta(microseconds=1)
    return period_end


def _overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < s_end and end > s_start for s_start, s_end in spans)


def _enclosing_tokens_have_phone_digits(text: str, start: int, end: int) -> bool:
    tok_start = text.rfind(" ", 0, start)
    tok_start = 0 if tok_start < 0 else tok_start + 1
    tok_end = text.find(" ", end)
    tok_end = len(text) if tok_end < 0 else tok_end
    for token in text[tok_start:tok_end].split(" "):
        if sum(ch.isdigit() for ch in token) >= 10:
            return True
    return False


def _is_false_positive_match(
    text: str,
    start: int,
    end: int,
    *,
    explicit_calendar_noun: bool = False,
) -> bool:
    if _enclosing_tokens_have_phone_digits(text, start, end):
        return True
    after = text[end : min(len(text), end + 24)]
    if _TRAILING_HARD_UNIT_RE.search(after):
        return True
    if _TRAILING_LEGAL_SUFFIX_RE.search(after):
        return True
    if not explicit_calendar_noun and _TRAILING_COUNT_UNIT_RE.search(after):
        return True
    if not explicit_calendar_noun and start > 0:
        before = text[max(0, start - 32) : start]
        if _LEADING_ID_PREFIX_RE.search(before):
            return True
    return False


def extract_as_of_datetime(
    query: str,
    *,
    reference_time: datetime | None = None,
) -> datetime | None:
    """Deterministically extract a UTC point-in-time `as_of` boundary from a VI/EN query.

    Returns `None` if no valid temporal point expression is found or if false-positive
    guards reject the token. Linear-time O(N), ReDoS-safe, bounded to first 4096 chars.
    """
    raw = unicodedata.normalize("NFC", (query or "")[:_MAX_QUERY_CHARS])
    text = _WS_RE.sub(" ", raw).strip()
    if not text:
        return None

    ref = _as_utc(reference_time) if reference_time is not None else datetime.now(UTC)
    invalid_spans: list[tuple[int, int]] = []

    # Tier 1 — Exact full date
    for m in _ISO_DATE_RE.finditer(text):
        start, end = m.span()
        if _is_false_positive_match(text, start, end):
            continue
        try:
            p_start, p_end = _day_bounds(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            invalid_spans.append((start, end))
            continue
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _VN_VERBOSE_DATE_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        try:
            p_start, p_end = _day_bounds(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            invalid_spans.append((start, end))
            continue
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _VN_NUMERIC_DATE_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        has_ngay = m.group(1) is not None
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=has_ngay):
            continue
        try:
            p_start, p_end = _day_bounds(int(m.group(5)), int(m.group(4)), int(m.group(2)))
        except ValueError:
            invalid_spans.append((start, end))
            continue
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _EN_MDY_DATE_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        month_num = _EN_MONTHS.get(m.group(1).lower())
        if month_num is None:
            continue
        try:
            p_start, p_end = _day_bounds(int(m.group(3)), month_num, int(m.group(2)))
        except ValueError:
            invalid_spans.append((start, end))
            continue
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _EN_DMY_DATE_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        month_num = _EN_MONTHS.get(m.group(2).lower())
        if month_num is None:
            continue
        try:
            p_start, p_end = _day_bounds(int(m.group(3)), month_num, int(m.group(1)))
        except ValueError:
            invalid_spans.append((start, end))
            continue
        return _resolve_boundary(text, start, p_start, p_end)

    # Tier 2 — Quarter-Year
    for m in _VN_QUARTER_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        p_start, p_end = _quarter_bounds(int(m.group(2)), int(m.group(1)))
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _EN_QUARTER_FIRST_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end):
            continue
        p_start, p_end = _quarter_bounds(int(m.group(2)), int(m.group(1)))
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _EN_YEAR_QUARTER_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end):
            continue
        p_start, p_end = _quarter_bounds(int(m.group(1)), int(m.group(2)))
        return _resolve_boundary(text, start, p_start, p_end)

    # Tier 3 — Month-Year
    for m in _VN_MONTH_YEAR_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        p_start, p_end = _month_bounds(int(m.group(2)), int(m.group(1)))
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _NUMERIC_MONTH_YEAR_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end):
            continue
        p_start, p_end = _month_bounds(int(m.group(2)), int(m.group(1)))
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _ISO_YEAR_MONTH_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end):
            continue
        p_start, p_end = _month_bounds(int(m.group(1)), int(m.group(2)))
        return _resolve_boundary(text, start, p_start, p_end)

    for m in _EN_MONTH_YEAR_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=True):
            continue
        month_num = _EN_MONTHS.get(m.group(1).lower())
        if month_num is None:
            continue
        p_start, p_end = _month_bounds(int(m.group(2)), month_num)
        return _resolve_boundary(text, start, p_start, p_end)

    # Tier 4 — Cued Year ONLY
    for m in _CUED_YEAR_RE.finditer(text):
        start, end = m.span()
        if _overlaps_any(start, end, invalid_spans):
            continue
        cue_norm = m.group(1).lower()
        is_cal_noun = cue_norm in _EXPLICIT_YEAR_CUES
        if _is_false_positive_match(text, start, end, explicit_calendar_noun=is_cal_noun):
            continue
        p_start, p_end = _year_bounds(int(m.group(2)))
        # Check prefix up to the year token start so "before 2025" / "trước 2025" / "trước năm 2025"
        # are all recognized as exclusive-before boundaries.
        return _resolve_boundary(text, m.start(2), p_start, p_end)

    # Tier 5 — Relative expressions anchored to ref in UTC
    m_yest = _REL_YESTERDAY_RE.search(text)
    if m_yest is not None:
        target_day = ref.date() - timedelta(days=1)
        p_start, p_end = _day_bounds(target_day.year, target_day.month, target_day.day)
        return _resolve_boundary(text, m_yest.start(), p_start, p_end)

    m_week = _REL_LAST_WEEK_RE.search(text)
    if m_week is not None:
        prev_sun = ref.date() - timedelta(days=ref.weekday() + 1)
        prev_mon = prev_sun - timedelta(days=6)
        p_start = datetime(prev_mon.year, prev_mon.month, prev_mon.day, 0, 0, 0, 0, tzinfo=UTC)
        p_end = datetime(prev_sun.year, prev_sun.month, prev_sun.day, 23, 59, 59, 999999, tzinfo=UTC)
        return _resolve_boundary(text, m_week.start(), p_start, p_end)

    m_month = _REL_LAST_MONTH_RE.search(text)
    if m_month is not None:
        prev_y, prev_m = (ref.year - 1, 12) if ref.month == 1 else (ref.year, ref.month - 1)
        p_start, p_end = _month_bounds(prev_y, prev_m)
        return _resolve_boundary(text, m_month.start(), p_start, p_end)

    m_quarter = _REL_LAST_QUARTER_RE.search(text)
    if m_quarter is not None:
        cur_q = (ref.month - 1) // 3 + 1
        prev_y, prev_q = (ref.year - 1, 4) if cur_q == 1 else (ref.year, cur_q - 1)
        p_start, p_end = _quarter_bounds(prev_y, prev_q)
        return _resolve_boundary(text, m_quarter.start(), p_start, p_end)

    m_year = _REL_LAST_YEAR_RE.search(text)
    if m_year is not None:
        p_start, p_end = _year_bounds(ref.year - 1)
        return _resolve_boundary(text, m_year.start(), p_start, p_end)

    return None
