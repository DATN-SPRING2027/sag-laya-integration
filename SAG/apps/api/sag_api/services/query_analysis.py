"""Bounded lexical query analysis for retrieval."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sag_api.services.search_index_service import COMMON_STOPWORDS

_QUERY_NOISE = (
    "kho tri thức",
    "tài liệu",
    "trong tài liệu",
    "cho tôi biết",
    "tìm giúp",
    "tìm kiếm",
    "tra cứu",
    "cho hỏi",
    "về",
    "mới nhất",
    "gần đây",
    "tin tức",
    "nội dung",
    "là gì",
    "có những gì",
    "knowledge base",
    "in the document",
    "tell me",
    "search for",
    "assume that you are a public equities analyst",
    "assume that you are",
    "answer the following question by utilizing the information shown in",
    "answer the following question",
    "give a response to the question by relying on the details shown in",
    "give a response to the question",
    "i'm looking for a reference that discusses",
    "i am looking for a reference that discusses",
)
_QUERY_INSTRUCTION_TERMS = frozenset(
    {
        "như thế nào",
        "làm sao",
        "tại sao",
        "vì sao",
        "có phải",
        "có thể",
        "giới thiệu",
        "mô tả",
        "giải thích",
        "how",
        "why",
        "whether",
        "explain",
        "describe",
        "assume",
        "utilizing",
        "following",
        "question",
        "response",
        "relying",
        "shown",
    }
)
_CHINESE_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LEXICAL_PART_RE = re.compile(
    r"[^\W_](?:[^\W_]|[_.+-]){1,31}|[\u3400-\u9fff]+",
    re.UNICODE,
)
_LEGACY_TERM_RE = re.compile(
    r"[^\W_](?:[^\W_]|[_.+-]){1,31}|[\u3400-\u9fff]{2,16}",
    re.UNICODE,
)
# Letter and digit runs inside a single lexical token. A glued query such as
# "ai2027" or "gpt4" is one token to the regexes above, but the deterministic
# grep path matches literal substrings, so it can never bridge a query that
# glues letters to digits against content that separates them ("AI 2027",
# "GPT-4"). Splitting on the letter/digit boundary is the English/number
# analogue of the Chinese segmentation that lets contiguous queries match
# spaced evidence.
_ALNUM_SUBTOKEN_RE = re.compile(r"[^\W\d_]+|[0-9]+", re.UNICODE)
_EXACT_TERM_RE = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|「([^」\n]+)」|《([^》\n]+)》|`([^`\n]+)`')
_IDENTIFIER_RE = re.compile(
    r"\b(?:ERR[_-][A-Z0-9_-]+|HTTP[_ -]?\d{3}|[A-Z][A-Z0-9_]*[0-9_][A-Z0-9_]*|"
    r"[A-Z]{2,}(?:-[A-Z0-9]+)+|[A-Za-z]+[_-]\d+[A-Za-z0-9._-]*|v\d+(?:\.\d+)+)\b"
)
_PATH_RE = re.compile(
    r"(?:(?<![\w$/])/[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]+|[A-Za-z]:\\[^\s]+|\b[\w-]+(?:\.[\w-]+){1,})"
)
_RELATION_STEM_GROUPS: dict[str, str] = {
    "causes": "cause",
    "caused": "cause",
    "cause": "cause",
    "affects": "affect",
    "affected": "affect",
    "affect": "affect",
    "leads to": "lead_to",
    "led to": "lead_to",
    "results in": "result_in",
    "resulted in": "result_in",
    "mối liên hệ": "lien_he",
    "liên hệ": "lien_he",
}
_TEMPORAL_CUES = (
    "trước",
    "sau",
    "từ",
    "đến",
    "khi",
    "hiện tại",
    "lúc đó",
    "phiên bản",
    "version",
    "release",
    "năm",
    "tháng",
    "ngày",
    "before",
    "after",
    "since",
    "until",
    "during",
    "current",
    "historical",
)
_RELATION_CUES = (
    "mối liên hệ",
    "liên hệ",
    "liên quan",
    "tương tác",
    "phụ thuộc",
    "dùng bởi",
    "kết nối",
    "thuộc",
    "gây ra",
    "dẫn đến",
    "khác gì",
    "so sánh",
    "relationship",
    "connection",
    "compare",
    "versus",
    " vs ",
    "related to",
    "depends on",
    "used by",
    "causes",
    "caused",
    "cause",
    "affects",
    "affected",
    "affect",
    "leads to",
    "led to",
    "results in",
    "resulted in",
    "because of",
)
_GLOBAL_CUES = (
    "tổng quan",
    "toàn bộ",
    "các chủ đề",
    "kiến trúc chung",
    "những vấn đề chính",
    "xu hướng",
    "bức tranh",
    "overview",
)

Segmenter = Callable[[str], Iterable[str]]


@dataclass(frozen=True, slots=True)
class QueryFeatures:
    """Deterministic signals shared by query routing and lexical retrieval."""

    exact_terms: tuple[str, ...] = ()
    identifier_terms: tuple[str, ...] = ()
    path_terms: tuple[str, ...] = ()
    temporal_cues: tuple[str, ...] = ()
    relation_cues: tuple[str, ...] = ()
    global_cues: tuple[str, ...] = ()
    multi_hop: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "exact_terms": list(self.exact_terms),
            "identifier_terms": list(self.identifier_terms),
            "path_terms": list(self.path_terms),
            "temporal_cues": list(self.temporal_cues),
            "relation_cues": list(self.relation_cues),
            "global_cues": list(self.global_cues),
            "multi_hop": self.multi_hop,
        }


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    normalized_phrase: str
    scoring_terms: tuple[str, ...]
    lookup_terms: tuple[str, ...]
    chinese_segmentation_used: bool
    expanded_terms: tuple[str, ...] = ()
    features: QueryFeatures = field(default_factory=QueryFeatures)


def normalize_lexical_text(value: str) -> str:
    return "".join(re.findall(r"[^\W_]+", value.lower(), flags=re.UNICODE))


def _remove_query_noise(query: str) -> str:
    cleaned = query.strip().lower()
    for phrase in _QUERY_NOISE:
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


def _unique_values(values: Iterable[str], *, limit: int = 8) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        key = normalize_lexical_text(item)
        if not item or not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return tuple(result)


_CLAUSE_SPLIT_RE = re.compile(
    r"(?:,\s+and\s+(?=(?:what|which|who|where|why|how|when|is|are|was|were|did|does|do|should|can)\b)"
    r"|\s+and\s+(?=(?:what|which|who|where|why|how\s+(?:do|does|did|should|can|could|to|will|would|is|are|was|were))\b)"
    r"|,\s*và\s+(?=(?:ai|cái\s+gì|gì|ở\s+đâu|đâu|khi\s+nào|như\s+thế\s+nào|bao\s+nhiêu|nào|có\s+phải|là)\b)"
    r"|;\s+)",
    re.IGNORECASE,
)
_SALIENT_PROPER_TERM_RE = re.compile(
    r"\b(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[0-9]*|[A-Za-z]+\d+[A-Za-z0-9]*)\b"
)


def decompose_query_clauses(query: str) -> tuple[str, ...]:
    """Split a compound multi-clause question into independent semantic sub-queries."""
    text = query.strip()
    if not text:
        return ()
    parts = [part.strip(" ,;?") for part in _CLAUSE_SPLIT_RE.split(text) if part and part.strip(" ,;?")]
    if len(parts) <= 1:
        return (text,)
    valid = [p for p in parts if len(p) >= 8]
    return tuple(valid) if len(valid) >= 2 else (text,)


def extract_salient_query_terms(query: str) -> frozenset[str]:
    """Identify proper nouns, CamelCase symbols, acronyms, and alphanumeric identifiers from cased query."""
    text = query.strip()
    if not text:
        return frozenset()
    salient: set[str] = set()
    for match in _SALIENT_PROPER_TERM_RE.finditer(text):
        norm = normalize_lexical_text(match.group(0))
        if len(norm) >= 2 and norm not in COMMON_STOPWORDS and norm not in _QUERY_INSTRUCTION_TERMS:
            salient.add(norm)
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    for idx, tok in enumerate(tokens):
        if idx > 0 and tok[:1].isupper() and any(c.islower() for c in tok[1:]):
            norm = normalize_lexical_text(tok)
            if len(norm) >= 3 and norm not in COMMON_STOPWORDS and norm not in _QUERY_INSTRUCTION_TERMS:
                salient.add(norm)
    return frozenset(salient)


def extract_query_features(query: str) -> QueryFeatures:
    """Extract bounded, non-LLM query signals for routing and trace output."""

    text = query.strip()
    lowered = text.lower()
    exact_terms = _unique_values(
        match.group(index)
        for match in _EXACT_TERM_RE.finditer(text)
        for index in range(1, 6)
        if match.group(index)
    )
    identifier_terms = _unique_values(match.group(0) for match in _IDENTIFIER_RE.finditer(text))
    path_terms = _unique_values(
        cleaned_path
        for match in _PATH_RE.finditer(text)
        if (cleaned_path := match.group(0).rstrip(".,;:!?)]'\""))
        and len(cleaned_path) >= 3
        and not re.fullmatch(r"\d+(?:\.\d+)+(?:[a-zA-Z%]{1,4})?", cleaned_path)
    )
    temporal_cues = _unique_values(cue for cue in _TEMPORAL_CUES if cue in lowered)
    matched_relation_cues: list[str] = []
    seen_relation_stems: set[str] = set()
    for cue in _RELATION_CUES:
        cue_stripped = cue.strip()
        if re.fullmatch(r"[a-z ]+", cue_stripped):
            pattern = (
                r"(?<!root )\bcause\b"
                if cue_stripped == "cause"
                else rf"\b{re.escape(cue_stripped)}\b"
            )
            if not re.search(pattern, lowered):
                continue
        elif cue not in lowered:
            continue
        stem = _RELATION_STEM_GROUPS.get(cue_stripped, cue_stripped)
        if stem in seen_relation_stems:
            continue
        seen_relation_stems.add(stem)
        matched_relation_cues.append(cue_stripped)
    relation_cues = _unique_values(matched_relation_cues)
    global_cues = _unique_values(cue for cue in _GLOBAL_CUES if cue in lowered)
    clauses = decompose_query_clauses(text)
    single_doc_scoped = bool(
        re.match(
            r"^in\s+the\s+(?:draft\s+spec|enterprise\s+[^,]{3,45}\s+runbook|[^,]{3,45}\s+(?:runbook|playbook|spec|sop|postmortem|curriculum))\s*,",
            lowered,
        )
    )
    bridge_pattern = bool(
        (len(clauses) >= 2 and not single_doc_scoped)
        or re.search(
            r"\b(?:mother|father|spouse|wife|husband|child|director|founder|author|creator|ceo|birthplace|nationality)\s+of\s+(?:the\s+)?(?:director|author|founder|creator|producer|composer|film|movie|book|company)\b"
            r"|'s\s+(?:paternal\s+|maternal\s+)?(?:father|mother|grandfather|grandmother|spouse|wife|husband|son|daughter|founder|director)\b"
            r"|\b(?:do|were|are|did)\s+both\b|\bwhich\s+(?:film|movie|person|company|director)\b.*\bor\b"
            r"|\bwho\s+(?:was\s+born\s+(?:first|earlier|later)|is\s+(?:older|younger))\b"
            r"|,\s+and\s+(?:what|which|who|where|why|how\s+(?:do|does|did|should|can|could|to|will|would))\b"
            r"|\b(?:what|which|why|how)\b[^?]{6,}\band\s+(?:what|which|who|where|why|how\s+(?:do|does|did|should|can|to))\b"
            r"|\bwhat\b[^?]{8,},\s+[^?]{4,},\s+and\s+"
            r"|(?:^|:\s*)(?:was|were|did)\b[^?]{3,}\bor\b"
            r"|\b(?:dân số|diện tích|thủ đô|tên|năm|ai|ở đâu|bao nhiêu)\b[^?]{2,80}\b(?:nơi|tại nơi|mà tại đó)\b[^?]{2,80}\b(?:tọa lạc|đặt trụ sở|sinh ra|thành lập|nằm ở|diễn ra)\b"
            r"|\b(?:ai|người nào|diễn viên nào|cầu thủ nào|đạo diễn nào|tác giả nào|ca sĩ nào|công ty nào)\s+là\b[^?]{4,90}\b(?:cũng|đồng thời)\s+(?:đóng vai|tham gia|đạo diễn|sáng lập|viết|sản xuất|thi đấu|chơi)\b"
            r"|\b(?:ai|người nào)\s+là\s+(?:luật sư|bác sĩ|người đại diện|huấn luyện viên|đạo diễn|nhà sản xuất|tác giả|người sáng lập|vợ|chồng|cha|mẹ|con)[^?]{2,50}\bcho\s+một\s+(?:người|phụ nữ|đàn ông|công ty|tổ chức|nhóm|đội)\b"
            r"|\b(?:nữ\s+|nam\s+)?(?:diễn viên|đạo diễn|ca sĩ|nhà văn|nhà soạn nhạc|cầu thủ|vận động viên|chính trị gia|nhà khoa học)\b[^?]{3,80}\b(?:tốt nghiệp|sinh ra|học tại|giành được giải)[^?]{3,80}\b(?:đóng vai|tham gia|đạo diễn|viết|sáng tác|phát hành)\b"
            r"|^(?:tên gọi|được\s+(?:đổi tên|thành lập|ra mắt|phát hành|xây dựng|sáng lập)|sinh\s+(?:năm|ra\s+tại))[^,?]{4,70},\s*(?:chiếc\s+xe|bộ\s+phim|cuốn\s+sách|bài\s+hát|công\s+ty|thành\s+phố|quốc\s+gia|người|ai|tác\s+phẩm)\s+nào\b"
            r"|\b(?:trong\s+hai\b|cả\s+hai\s+đều\b|[^?]{3,60}\bhay\s+(?:là\s+)?[a-zà-ỹ0-9]{2,})"
            r"|\b(?:mẹ|cha|bố|vợ|chồng|con|đạo diễn|người sáng lập|nhà sáng lập|tác giả|quê hương|nơi sinh|quốc tịch|tên nhân vật)\s+của\b[^?]{2,110}\b(?:trong\s+(?:phim|bộ phim|cuộc đua|tác phẩm|vở)|đạo diễn|tác giả|người sáng lập|người chiến thắng|giải thưởng)\b"
            r"|\b(?:một|những)\s+(?:thị trấn|thành phố|quận|huyện|tỉnh|tiểu bang|quốc gia|bảo tàng|trường|đại học|công ty|bộ phim|người|nhân vật)\s+(?:ở|tại)\b[^?]{3,80},\s*có\s+(?:một|nhiều|các)\b",
            lowered,
        )
    )
    if single_doc_scoped:
        bridge_pattern = False
    multi_hop = (
        len(relation_cues) >= 2
        or bridge_pattern
        or any(
            cue in lowered
            for cue in (
                "mối liên hệ",
                "dẫn đến",
                "vì vậy",
                "từ đó",
                "relationship between",
                "difference between",
            )
        )
    )
    return QueryFeatures(
        exact_terms=exact_terms,
        identifier_terms=identifier_terms,
        path_terms=path_terms,
        temporal_cues=temporal_cues,
        relation_cues=relation_cues,
        global_cues=global_cues,
        multi_hop=multi_hop,
    )


def _is_valid_term(value: str) -> bool:
    normalized = normalize_lexical_text(value)
    return len(normalized) >= 2 and not normalized.isdigit()


def _split_alnum_terms(
    parts: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    terms: list[str] = []
    expanded_terms: list[str] = []
    keys: set[str] = set()
    for part in parts:
        subtokens = _ALNUM_SUBTOKEN_RE.findall(part)
        split = (
            part.isalnum()
            and len(subtokens) == 2
            and any(value.isalpha() for value in subtokens)
            and any(value.isdigit() for value in subtokens)
        )
        # Always preserve the full token (e.g. "3m", "fy2018", "q2") before any valid subtokens
        candidates = (part, *(s for s in subtokens if len(s) >= 2)) if split else (part,)
        for idx, candidate in enumerate(candidates):
            value = candidate.strip().lower()
            key = normalize_lexical_text(value)
            valid = len(key) >= 2
            if not valid or key in keys:
                continue
            terms.append(value)
            if split and idx > 0:
                expanded_terms.append(value)
            keys.add(key)
            if len(terms) >= 32:
                return tuple(terms), tuple(expanded_terms)
    return tuple(terms), tuple(expanded_terms)


def _lookup_terms(
    scoring_terms: tuple[str, ...],
    phrase: str,
) -> tuple[str, ...]:
    phrase_term = (phrase,) if _is_valid_term(phrase) else ()
    candidates = (*scoring_terms, *phrase_term)
    terms: list[str] = []
    keys: set[str] = set()
    for value in candidates:
        key = normalize_lexical_text(value)
        if not key or key in keys:
            continue
        terms.append(value)
        keys.add(key)
        if len(terms) >= 16:
            break
    return tuple(terms)


def _filter_informative_tokens(tokens: list[str]) -> list[str]:
    filtered = [
        value
        for value in tokens
        if normalize_lexical_text(value) not in COMMON_STOPWORDS
        and normalize_lexical_text(value) not in _QUERY_INSTRUCTION_TERMS
    ]
    return filtered if filtered else tokens


def _legacy_query_terms(
    cleaned: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = _LEGACY_TERM_RE.findall(cleaned)
    return _split_alnum_terms(_filter_informative_tokens(raw))


def _chinese_runs(cleaned: str) -> tuple[str, ...]:
    return tuple(_CHINESE_RUN_RE.findall(cleaned))


def _jieba_segment(text: str) -> Iterable[str]:
    import jieba

    return jieba.cut(text, cut_all=False)


def _segmented_terms(
    cleaned: str,
    segmenter: Segmenter,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    values: list[str] = []
    for part in _LEXICAL_PART_RE.findall(cleaned):
        if _CHINESE_RUN_RE.fullmatch(part):
            if len(part) < 2:
                continue
            values.extend(segmenter(part))
        else:
            values.append(part)
    return _split_alnum_terms(_filter_informative_tokens(values))


def analyze_query(
    query: str,
    *,
    segmentation_enabled: bool = True,
    segmenter: Segmenter | None = None,
) -> QueryAnalysis:
    cleaned = _remove_query_noise(query)
    phrase = normalize_lexical_text(cleaned)
    features = extract_query_features(query)
    legacy_terms, legacy_expanded_terms = _legacy_query_terms(cleaned)
    legacy_lookup_terms = _lookup_terms(legacy_terms, phrase)
    chinese_runs = _chinese_runs(cleaned)
    if not segmentation_enabled or not chinese_runs:
        return QueryAnalysis(
            normalized_phrase=phrase,
            scoring_terms=legacy_terms,
            lookup_terms=legacy_lookup_terms,
            chinese_segmentation_used=False,
            expanded_terms=legacy_expanded_terms,
            features=features,
        )

    try:
        scoring_terms, expanded_terms = _segmented_terms(
            cleaned,
            segmenter or _jieba_segment,
        )
    except Exception:  # noqa: BLE001 -- retrieval must survive tokenizer failure
        return QueryAnalysis(
            normalized_phrase=phrase,
            scoring_terms=legacy_terms,
            lookup_terms=legacy_lookup_terms,
            chinese_segmentation_used=False,
            expanded_terms=legacy_expanded_terms,
            features=features,
        )

    return QueryAnalysis(
        normalized_phrase=phrase,
        scoring_terms=scoring_terms,
        lookup_terms=_lookup_terms(scoring_terms, phrase),
        chinese_segmentation_used=True,
        expanded_terms=expanded_terms,
        features=features,
    )


def query_terms(query: str, *, segmentation_enabled: bool = True) -> list[str]:
    """Return bounded lookup terms for compatibility with existing callers."""

    return list(analyze_query(query, segmentation_enabled=segmentation_enabled).lookup_terms)
