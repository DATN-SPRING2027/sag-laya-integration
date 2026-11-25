"""LLM-as-judge:比较两组检索结果对回答给定问题谁更有帮助。

结论:pairwise winner + 简短理由。复用现有 `LLMClient.complete`,不引第二个模型配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sag_api.core.logging import get_logger
from sag_api.generation.json_parser import decode_json_object
from sag_api.sag import RetrievedSection

log = get_logger("eval.judge")

_EXCERPT_LIMIT = 400  # 每条 section 塞给 judge 的最大字符,避免 prompt 撑爆
_MAX_SECTIONS_PER_SIDE = 8


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """Pairwise 结果。winner 归一化到 'A' | 'B' | 'tie'。"""

    winner: str
    reason: str
    raw: str
    a_label: str
    b_label: str

    def to_dict(self) -> dict[str, str]:
        return {
            "winner": self.winner,
            "reason": self.reason,
            "raw": self.raw,
            "a_label": self.a_label,
            "b_label": self.b_label,
        }


def _clip(text: str, limit: int = _EXCERPT_LIMIT) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_sections(sections: list[RetrievedSection]) -> str:
    trimmed = sections[:_MAX_SECTIONS_PER_SIDE]
    if not trimmed:
        return "(empty)"
    lines: list[str] = []
    for index, section in enumerate(trimmed, 1):
        heading = _clip(section.heading, 120) or "(Untitled)"
        content = _clip(section.content)
        lines.append(f"[{index}] Heading: {heading}\n    Excerpt: {content}")
    return "\n".join(lines)


def build_pairwise_prompt(
    query: str,
    a_label: str,
    a_sections: list[RetrievedSection],
    b_label: str,
    b_sections: list[RetrievedSection],
) -> list[dict[str, str]]:
    """Build blind chat messages (Option 1 / Option 2) without leaking strategy names or scores."""
    del a_label, b_label
    system = (
        "You are a retrieval quality evaluator. Given a user question and two anonymized sets of retrieved evidence "
        "(Option 1 and Option 2), determine which option better supports an accurate answer to the question. "
        "Evaluate only the provided evidence; do not introduce external knowledge. "
        'Output strictly in JSON: {"winner": "Option 1" | "Option 2" | "tie", "reason": "One-sentence explanation"}. '
        "Prefer higher coverage, stronger relevance, and less noise; choose tie if neither option can answer."
    )
    user = (
        f"Question: {query.strip()}\n\n"
        f"=== Option 1 ===\n{_format_sections(a_sections)}\n\n"
        f"=== Option 2 ===\n{_format_sections(b_sections)}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_verdict(text: str) -> tuple[str, str] | None:
    """Extract normalized option winner ('1' | '2' | 'tie') and reason; return None on parse/schema error."""
    data = decode_json_object(text or "")
    if not isinstance(data, dict):
        return None
    raw_winner = " ".join(str(data.get("winner", "")).strip().upper().split())
    if raw_winner in {"OPTION 1", "OPTION_1", "1", "A"}:
        winner = "1"
    elif raw_winner in {"OPTION 2", "OPTION_2", "2", "B"}:
        winner = "2"
    elif raw_winner == "TIE":
        winner = "tie"
    else:
        return None
    reason = str(data.get("reason", "") or "").strip() or "judge did not provide a reason"
    return winner, reason


async def _run_judge_pass(
    llm: Any,
    query: str,
    first_label: str,
    first_sections: list[RetrievedSection],
    second_label: str,
    second_sections: list[RetrievedSection],
) -> tuple[str, str, str] | None:
    messages = build_pairwise_prompt(query, first_label, first_sections, second_label, second_sections)
    try:
        raw = await llm.complete(messages, response_format={"type": "json_object"})
    except Exception as error:  # noqa: BLE001
        log.warning("LLM judge 调用失败:%s", error)
        return None
    parsed = _parse_verdict(raw)
    if parsed is None:
        log.warning("LLM judge returned invalid/unparseable JSON verdict; returning None")
        return None
    winner, reason = parsed
    return winner, reason, raw


async def judge_pairwise(
    llm: Any,
    query: str,
    a_label: str,
    a_sections: list[RetrievedSection],
    b_label: str,
    b_sections: list[RetrievedSection],
) -> JudgeVerdict | None:
    """Run blind bidirectional A/B + B/A pairwise judge; return None on error, bad JSON, or position bias.

    None semantics: callers must treat the pair as unscored/invalid rather than coercing to tie.
    """
    if llm is None or not getattr(llm, "configured", False):
        return None

    forward = await _run_judge_pass(llm, query, a_label, a_sections, b_label, b_sections)
    if forward is None:
        return None
    reverse = await _run_judge_pass(llm, query, b_label, b_sections, a_label, a_sections)
    if reverse is None:
        return None

    fwd_choice, fwd_reason, fwd_raw = forward
    rev_choice, _rev_reason, rev_raw = reverse

    fwd_mapped = {"1": "A", "2": "B", "tie": "tie"}[fwd_choice]
    rev_mapped = {"1": "B", "2": "A", "tie": "tie"}[rev_choice]
    if fwd_mapped != rev_mapped:
        log.warning(
            "LLM judge disagreed across swapped A/B order (forward=%s, reverse=%s); returning None",
            fwd_mapped,
            rev_mapped,
        )
        return None

    return JudgeVerdict(
        winner=fwd_mapped,
        reason=fwd_reason,
        raw=f"forward: {fwd_raw}\nreverse: {rev_raw}",
        a_label=a_label,
        b_label=b_label,
    )
