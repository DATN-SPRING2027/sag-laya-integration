"""Bounded lexical query analysis for retrieval."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sag_api.services.search_index_service import COMMON_STOPWORDS


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value) if value else ""


_DMY_DATE_RE = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[/-](0?[1-9]|1[0-2])[/-]((?:19|20)\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})[/-](0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])\b")


def _extract_bridged_date_groups(text: str) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for m in _DMY_DATE_RE.finditer(text):
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dmy = f"{dd:02d}/{mm:02d}/{yyyy:04d}"
        iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
        group = _unique_values((iso, dmy, m.group(0)))
        if group and group not in seen:
            seen.add(group)
            groups.append(group)
    for m in _ISO_DATE_RE.finditer(text):
        yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
        dmy = f"{dd:02d}/{mm:02d}/{yyyy:04d}"
        group = _unique_values((dmy, iso, m.group(0)))
        if group and group not in seen:
            seen.add(group)
            groups.append(group)
    return tuple(groups)


def _extract_bridged_date_tokens(text: str) -> list[str]:
    out: list[str] = []
    for group in _extract_bridged_date_groups(text):
        out.extend(group[:2])
    return out

_CHINESE_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CONTINUOUS_NON_CHINESE_RUN_RE = re.compile(
    r"[\u3040-\u30ff\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]+"
)
_ANY_CONTINUOUS_SCRIPT_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]"
)
_LEXICAL_PART_RE = re.compile(
    r"[^\W_](?:[^\W_]|[_.+-]){1,31}|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u0e00-\u0eff\u1780-\u17ff\u1000-\u109f]+",
    re.UNICODE,
)
_LEGACY_TERM_RE = re.compile(
    r"[^\W_](?:[^\W_]|[_.+-]){1,31}|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u0e00-\u0eff\u1780-\u17ff\u1000-\u109f]{2,16}",
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
    r"\b(?:"
    r"ERR[_-][A-Z0-9_-]+"
    r"|HTTP[_ -]?\d{3}"
    r"|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r"|[A-Z]{2,}(?:-[A-Z0-9]+)+"
    r"|[A-Za-z]{2,}[_-]\d+[A-Za-z0-9._-]*"
    r"|[A-Z]{2,}\d{2,}[A-Z0-9]*"
    r"|v\d+(?:\.\d+)+"
    r")\b"
)
_NON_IDENTIFIER_TEMPORAL_RE = re.compile(r"^(?:FY|Q[1-4])(?:[-_]?\d{2,4})?$", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?:"
    r"(?<![\w$:/])/[A-Za-z0-9._~:/?#[\]@!$&'()*+,;=%-]+"
    r"|[A-Za-z]:\\[^\s]+"
    r"|\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
    r"|\b[\w-]+(?:\.[\w-]+){1,}"
    r")"
)
_NON_PATH_BRAND_NAMES = frozenset(
    {
        "node.js",
        "next.js",
        "vue.js",
        "nuxt.js",
        "react.js",
        "express.js",
        "nest.js",
        "three.js",
        "chart.js",
        "d3.js",
        "anime.js",
        "alpine.js",
        "ember.js",
        "svelte.js",
        "solid.js",
        "angular.js",
        "electron.js",
        "bun.js",
        "deno.js",
        "socket.io",
        "asp.net",
        "vb.net",
        "ml.net",
    }
)
_KNOWN_FILE_EXTENSIONS = frozenset(
    {
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "mjs",
        "cjs",
        "md",
        "mdx",
        "txt",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "env",
        "sql",
        "sh",
        "bash",
        "zsh",
        "ps1",
        "bat",
        "cmd",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "csv",
        "tsv",
        "html",
        "htm",
        "xml",
        "css",
        "scss",
        "less",
        "svg",
        "png",
        "jpg",
        "jpeg",
        "webp",
        "gif",
        "go",
        "rs",
        "java",
        "kt",
        "rb",
        "php",
        "c",
        "cc",
        "cpp",
        "h",
        "hpp",
        "cs",
        "swift",
        "dart",
        "proto",
        "graphql",
        "gql",
        "tf",
        "hcl",
        "lock",
        "log",
        "bak",
        "gz",
        "tar",
        "zip",
        "whl",
        "dockerfile",
        "mk",
        "cmake",
        "gradle",
        "properties",
        "ipynb",
        "rst",
        "tex",
        "bib",
    }
)


def _is_valid_path_term(cleaned_path: str) -> bool:
    if len(cleaned_path) < 3:
        return False
    if _DMY_DATE_RE.fullmatch(cleaned_path) or _ISO_DATE_RE.fullmatch(cleaned_path):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)*(?:\.[xX*]+)?(?:[^\W\d_]+|%)?", cleaned_path):
        return False
    if re.fullmatch(r"[A-Za-z](?:\.[A-Za-z])+", cleaned_path):
        return False
    if re.fullmatch(r"[A-Z]{1,4}(?:\.[A-Z]{1,4})+", cleaned_path):
        return False
    if cleaned_path.lower() in _NON_PATH_BRAND_NAMES:
        return False
    if cleaned_path.startswith("/") or re.match(r"^[A-Za-z]:\\", cleaned_path):
        return any(ch.isalnum() for ch in cleaned_path)
    if "/" in cleaned_path:
        segments = [seg for seg in cleaned_path.split("/") if seg]
        if len(segments) < 2:
            return False
        last_ext = segments[-1].rsplit(".", 1)[-1].lower() if "." in segments[-1] else ""
        if last_ext in _KNOWN_FILE_EXTENSIONS:
            return True
        return len(segments) >= 3 and any(any(ch.isalpha() for ch in seg) for seg in segments)
    parts = cleaned_path.split(".")
    if len(parts) < 2:
        return False
    if parts[-1].lower() in _KNOWN_FILE_EXTENSIONS:
        return True
    return len(parts) >= 3 and all(re.fullmatch(r"[a-z][a-z0-9_-]*", p) for p in parts)
_STRUCTURAL_TEMPORAL_RE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:fy|q)[1-4]?\d{2,4}\b|\bq[1-4]\b|\bv\d+(?:\.\d+)+\b|\d{4}年|\d{1,2}月\d{1,2}日)",
    re.IGNORECASE,
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
    clauses: tuple[str, ...] = ()
    multi_hop: bool = False
    anchor_groups: tuple[tuple[str, ...], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "exact_terms": list(self.exact_terms),
            "identifier_terms": list(self.identifier_terms),
            "path_terms": list(self.path_terms),
            "temporal_cues": list(self.temporal_cues),
            "relation_cues": list(self.relation_cues),
            "global_cues": list(self.global_cues),
            "clauses": list(self.clauses),
            "multi_hop": self.multi_hop,
            "anchor_groups": [list(group) for group in self.anchor_groups],
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
    return "".join(re.findall(r"[^\W_]+", _nfc(value).lower(), flags=re.UNICODE))


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


_CLAUSE_SPLIT_RE = re.compile(r"[?？]\s+(?=\S)|[;；]\s*")


def decompose_query_clauses(
    query: str,
    *,
    subqueries: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Return structured router sub-queries when available, else split only on structural punctuation."""
    text = _nfc(query).strip()
    if not text:
        return ()
    if subqueries is not None:
        cleaned = list(
            dict.fromkeys(
                item
                for raw in subqueries
                if (item := _nfc(str(raw)).strip())
            )
        )
        if len(cleaned) >= 2:
            return tuple(cleaned[:4])
    parts = [part.strip(" ,;?？；") for part in _CLAUSE_SPLIT_RE.split(text) if part and part.strip(" ,;?？；")]
    if len(parts) <= 1:
        return (text,)
    valid = [p for p in parts if len(p) >= 8 or (_ANY_CONTINUOUS_SCRIPT_RE.search(p) and len(p) >= 4)]
    return tuple(valid) if len(valid) >= 2 else (text,)


def extract_salient_query_terms(
    query: str,
    *,
    entities: Iterable[str] | None = None,
) -> frozenset[str]:
    """Identify proper nouns, CamelCase symbols, acronyms, and alphanumeric identifiers language-agnostically."""
    text = _nfc(query).strip()
    salient: set[str] = set()
    for ent in entities or ():
        ent_nfc = _nfc(str(ent)).strip()
        if not ent_nfc:
            continue
        ent_norm = normalize_lexical_text(ent_nfc)
        if len(ent_norm) >= 2 and ent_norm not in COMMON_STOPWORDS:
            salient.add(ent_norm)
        for tok in re.findall(r"[^\W_]+", ent_nfc, flags=re.UNICODE):
            tok_norm = normalize_lexical_text(tok)
            if len(tok_norm) >= 2 and tok_norm not in COMMON_STOPWORDS:
                salient.add(tok_norm)
    if not text:
        return frozenset(salient)
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    for idx, tok in enumerate(tokens):
        norm = normalize_lexical_text(tok)
        if len(norm) < 2 or norm in COMMON_STOPWORDS:
            continue
        has_alpha = any(c.isalpha() for c in tok)
        has_digit = any(c.isdigit() for c in tok)
        has_lower = any(c.islower() for c in tok)
        has_internal_upper = any(unicodedata.category(c) in {"Lu", "Lt"} for c in tok[1:])
        is_all_caps_acronym = has_alpha and not has_lower
        is_alnum_ident = has_alpha and has_digit
        is_mid_proper = idx > 0 and unicodedata.category(tok[0]) in {"Lu", "Lt"} and any(c.islower() for c in tok[1:])
        if has_internal_upper or is_all_caps_acronym or is_alnum_ident or is_mid_proper:
            salient.add(norm)
    return frozenset(salient)


def extract_query_features(
    query: str,
    *,
    subqueries: Iterable[str] | None = None,
    multi_hop_hint: bool = False,
) -> QueryFeatures:
    """Extract bounded, structural query signals for routing and trace output."""

    text = _nfc(query).strip()
    exact_terms = _unique_values(
        match.group(index)
        for match in _EXACT_TERM_RE.finditer(text)
        for index in range(1, 6)
        if match.group(index)
    )
    raw_identifier_terms = _unique_values(
        match.group(0)
        for match in _IDENTIFIER_RE.finditer(text)
        if not _NON_IDENTIFIER_TEMPORAL_RE.fullmatch(match.group(0))
    )
    date_groups = _extract_bridged_date_groups(text)
    bridged_dates = [tok for group in date_groups for tok in group[:2]]
    identifier_terms = _unique_values([*raw_identifier_terms, *bridged_dates])
    path_terms = _unique_values(
        cleaned_path
        for match in _PATH_RE.finditer(text)
        if (cleaned_path := match.group(0).rstrip(".,;:!?)]'\""))
        and _is_valid_path_term(cleaned_path)
    )
    anchor_groups_list: list[tuple[str, ...]] = []
    seen_anchor_keys: set[tuple[str, ...]] = set()
    covered_singletons: set[str] = set()
    for group in date_groups:
        norm_group = tuple(dict.fromkeys(v for term in group if (v := normalize_lexical_text(term))))
        if norm_group and norm_group not in seen_anchor_keys:
            seen_anchor_keys.add(norm_group)
            covered_singletons.update(norm_group)
            anchor_groups_list.append(group)
    for term in (*exact_terms, *raw_identifier_terms, *path_terms):
        norm = normalize_lexical_text(term)
        if not norm or norm in covered_singletons or (norm,) in seen_anchor_keys:
            continue
        seen_anchor_keys.add((norm,))
        covered_singletons.add(norm)
        anchor_groups_list.append((term,))
    temporal_cues = _unique_values(m.group(0) for m in _STRUCTURAL_TEMPORAL_RE.finditer(text))
    clauses = decompose_query_clauses(text, subqueries=subqueries)
    multi_hop = bool(multi_hop_hint) or len(clauses) >= 2
    return QueryFeatures(
        exact_terms=exact_terms,
        identifier_terms=identifier_terms,
        path_terms=path_terms,
        temporal_cues=temporal_cues,
        relation_cues=(),
        global_cues=(),
        clauses=clauses if len(clauses) >= 2 else (),
        multi_hop=multi_hop,
        anchor_groups=tuple(anchor_groups_list),
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
    ]
    return filtered if filtered else tokens


def _expand_non_chinese_continuous_runs(tokens: Iterable[str]) -> list[str]:
    out: list[str] = []
    for tok in tokens:
        if not _CONTINUOUS_NON_CHINESE_RUN_RE.search(tok):
            out.append(tok)
            continue
        last_end = 0
        for match in _CONTINUOUS_NON_CHINESE_RUN_RE.finditer(tok):
            if match.start() > last_end:
                out.append(tok[last_end : match.start()])
            run = match.group(0)
            if len(run) <= 2:
                out.append(run)
            else:
                for pos in range(len(run) - 1):
                    out.append(run[pos : pos + 2])
            last_end = match.end()
        if last_end < len(tok):
            out.append(tok[last_end:])
    return out


def _legacy_query_terms(
    cleaned: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    bridged_dates = _extract_bridged_date_tokens(cleaned)
    raw = [*bridged_dates, *_expand_non_chinese_continuous_runs(_LEGACY_TERM_RE.findall(cleaned))]
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
    values: list[str] = list(_extract_bridged_date_tokens(cleaned))
    for part in _LEXICAL_PART_RE.findall(cleaned):
        if _CHINESE_RUN_RE.fullmatch(part):
            if len(part) < 2:
                continue
            values.extend(segmenter(part))
        elif _CONTINUOUS_NON_CHINESE_RUN_RE.search(part):
            values.extend(_expand_non_chinese_continuous_runs([part]))
        else:
            values.extend([part])
    return _split_alnum_terms(_filter_informative_tokens(values))


def analyze_query(
    query: str,
    *,
    segmentation_enabled: bool = True,
    segmenter: Segmenter | None = None,
    subqueries: Iterable[str] | None = None,
    multi_hop_hint: bool = False,
) -> QueryAnalysis:
    query = _nfc(query)
    cleaned = query.strip().lower()
    phrase = normalize_lexical_text(cleaned)
    features = extract_query_features(query, subqueries=subqueries, multi_hop_hint=multi_hop_hint)
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
