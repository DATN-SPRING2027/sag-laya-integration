"""Unified Unicode BM25 tokenizer shared across index, query, and session memory."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import regex

from sag_api.core.text_utils import normalize_text

BM25_STEM_WEIGHT: float = 0.55

COMMON_STOPWORDS: frozenset[str] = frozenset(
    {
        # English function words & question particles
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
        "by", "from", "up", "about", "into", "over", "after", "is", "are", "was", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did", "can", "could",
        "this", "that", "these", "those", "it", "its", "as",
        "what", "when", "where", "who", "whom", "whose", "which", "why", "how",
        # Vietnamese function words, prepositions, conjunctions & question particles
        "là", "và", "của", "cho", "với", "trong", "được", "có", "đã", "đang", "sẽ",
        "các", "những", "một", "này", "đó", "tại", "về", "từ", "theo", "khi", "như",
        "bởi", "vì", "nếu", "thì", "mà", "để", "đến", "tới", "ra", "vào", "lên", "xuống",
        "cũng", "vẫn", "đều", "chỉ", "rất", "quá", "lắm", "rồi", "hay", "hoặc", "nhưng",
        "gì", "nào", "ai", "đâu", "sao", "thế", "bao", "nhiêu", "không", "chưa", "phải",
    }
)

_UNICODE_TOKEN_RE = regex.compile(r"[\p{L}\d]+")
_GLUED_ALNUM_SPLIT_RE = regex.compile(r"\p{L}+|\d+")
_CONTINUOUS_SCRIPT_RUN_RE = regex.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u0e00-\u0e7f\u0e80-\u0eff\u1780-\u17ff\u1000-\u109f]+"
)


@dataclass(frozen=True, slots=True)
class BM25TokenResult:
    tokens: list[str]
    expanded_tokens: list[str]
    proper_tokens: frozenset[str]


def expand_continuous_script_tokens(tokens: list[str]) -> list[str]:
    """Split unspaced CJK/Kana/Thai/Lao/Khmer/Myanmar runs into overlapping character bigrams."""
    out: list[str] = []
    for tok in tokens:
        if not _CONTINUOUS_SCRIPT_RUN_RE.search(tok):
            out.append(tok)
            continue
        last_end = 0
        for match in _CONTINUOUS_SCRIPT_RUN_RE.finditer(tok):
            if match.start() > last_end:
                prefix = tok[last_end : match.start()]
                if prefix:
                    out.append(prefix)
            run = match.group(0)
            if len(run) == 1:
                out.append(run)
            else:
                for pos in range(len(run) - 1):
                    out.append(run[pos : pos + 2])
            last_end = match.end()
        if last_end < len(tok):
            suffix = tok[last_end:]
            if suffix:
                out.append(suffix)
    return out


def token_hash_bucket(token: str) -> int:
    """Deterministic 1M-bucket MD5 hash index for Qdrant sparse vectors."""
    return int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000


def english_stem(word: str) -> str | None:
    """Safe secondary ASCII English stemmer aligned across index and query."""
    if not (word.isascii() and word.isalpha()):
        return None
    stem = (
        word[:-3]
        if len(word) >= 7 and word.endswith("ing")
        else word[:-2]
        if len(word) >= 6 and word.endswith("ed") and not word.endswith("eed")
        else word[:-1]
        if len(word) >= 5 and word.endswith("s") and not word.endswith(("ss", "us", "is"))
        else None
    )
    return stem if stem and len(stem) >= 4 else None


def tokenize_bm25_tokens(
    text: str,
    *,
    filter_stopwords: bool = True,
) -> BM25TokenResult:
    """Tokenize NFC Unicode text for BM25 index, query, and session scoring.

    - Preserves Vietnamese/Unicode diacritics via NFC normalization before regex matching.
    - Does NOT rewrite R&D -> rd and does NOT strip commas in 1,500 (protects Vietnamese decimals).
    - Splits glued Unicode alphanumeric tokens (điều15 -> điều, 15; tầng2 -> tầng, 2; v2 -> v, 2).
    - Applies unified COMMON_STOPWORDS filtering (falling back to raw tokens if all are stopwords).
    """
    nfc_text = normalize_text(text or "", preserve_code=True)
    if not nfc_text:
        return BM25TokenResult(tokens=[], expanded_tokens=[], proper_tokens=frozenset())

    orig_tokens = _UNICODE_TOKEN_RE.findall(nfc_text)
    if not orig_tokens:
        return BM25TokenResult(tokens=[], expanded_tokens=[], proper_tokens=frozenset())

    proper_tokens = frozenset(
        tok.lower()
        for idx, tok in enumerate(orig_tokens)
        if idx > 0 and any(ch.isupper() for ch in tok) and len(tok) >= 2
    )

    lower_tokens = [tok.lower() for tok in orig_tokens]
    if _CONTINUOUS_SCRIPT_RUN_RE.search(nfc_text):
        lower_tokens = expand_continuous_script_tokens(lower_tokens)

    if filter_stopwords:
        filtered = [tok for tok in lower_tokens if tok not in COMMON_STOPWORDS]
        active_tokens = filtered if filtered else lower_tokens
    else:
        active_tokens = lower_tokens

    expanded_tokens: list[str] = list(active_tokens)
    for tok in active_tokens:
        if tok.isalnum() and not tok.isalpha() and not tok.isdigit():
            for sub in _GLUED_ALNUM_SPLIT_RE.findall(tok):
                if sub == tok:
                    continue
                if filter_stopwords and sub in COMMON_STOPWORDS:
                    continue
                expanded_tokens.append(sub)

    return BM25TokenResult(
        tokens=active_tokens,
        expanded_tokens=expanded_tokens,
        proper_tokens=proper_tokens,
    )
