"""Bộ chuẩn hóa và trích xuất khối văn bản chuẩn (Canonical Block Extractor & Normalizer).

Triết lý Ponytail: Tối giản, thuần stdlib (unicodedata, re, hashlib, uuid),
không dùng LLM, bảo toàn 100% định dạng bảng, thụt lề code và các mã định danh kỹ thuật.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sag_api.core.text_utils import (
    estimate_tokens,
    normalize_text,
    split_clauses_multilingual,
    split_sentence_spans_multilingual,
)

BlockType = Literal["heading", "paragraph", "table", "code", "list", "caption"]

# Regex phát hiện boilerplate phổ biến: tiêu đề / chân trang đánh số trang
_BOILERPLATE_PATTERNS = [
    re.compile(r"^(?:trang|page|p\.?|pg\.?|tr\.?)\s*\d+(?:\s*(?:/|of)\s*\d+)?$", re.IGNORECASE),
    re.compile(r"^[-—–]\s*\d+\s*[-—–]$"),
]

# Regex nhận diện khối bảng Markdown (có hoặc không có | hai đầu) và bảng HTML MinerU
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
_HTML_TABLE_OPEN_RE = re.compile(r"^\s*<table\b", re.IGNORECASE)
_HTML_TABLE_CLOSE_RE = re.compile(r"</table>", re.IGNORECASE)

# Regex nhận diện tiêu đề Markdown (ATX và Setext) và hàng rào code
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?$")
_SETEXT_UNDERLINE_RE = re.compile(r"^(=+|-+)\s*$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")

# Regex nhận diện danh sách
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$")


def estimate_unicode_tokens(text: str) -> int:
    """Ước lượng số lượng token qua shared tiktoken cl100k_base + fallback tiếng Việt/CJK."""
    return estimate_tokens(text)


def _split_atomic_spans_with_offsets(text: str, *, max_span_tokens: int) -> list[tuple[int, int, str]]:
    """Tách văn bản thành câu tự nhiên đa ngôn ngữ kèm offset; hạ xuống mệnh đề nếu câu quá dài."""
    raw_spans = split_sentence_spans_multilingual(text)
    if not raw_spans:
        stripped = text.strip()
        return [(0, len(text), stripped)] if stripped else []

    final_spans: list[tuple[int, int, str]] = []
    for s_start, s_end, sent in raw_spans:
        if estimate_unicode_tokens(sent) <= max_span_tokens:
            final_spans.append((s_start, s_end, sent))
            continue
        clauses = split_clauses_multilingual(sent)
        if len(clauses) <= 1:
            final_spans.append((s_start, s_end, sent))
            continue
        buf: list[str] = []
        buf_toks = 0
        for cl in clauses:
            cl_toks = estimate_unicode_tokens(cl)
            if buf and buf_toks + cl_toks > max_span_tokens // 2:
                final_spans.append((s_start, s_end, " ".join(buf)))
                buf = [cl]
                buf_toks = cl_toks
            else:
                buf.append(cl)
                buf_toks += cl_toks
        if buf:
            final_spans.append((s_start, s_end, " ".join(buf)))
    return final_spans


def _split_atomic_spans(text: str, *, max_span_tokens: int) -> list[str]:
    """Tách văn bản thành các câu tự nhiên đa ngôn ngữ; chỉ hạ xuống ranh giới mệnh đề nếu 1 câu đơn quá dài."""
    return [seg for _s, _e, seg in _split_atomic_spans_with_offsets(text, max_span_tokens=max_span_tokens)]


def segment_prose_by_information_cohesion(
    text: str,
    *,
    max_block_tokens: int = 360,
    min_block_tokens: int = 48,
) -> list[str]:
    """Phân đoạn chủ đề bằng Lý thuyết Thông tin (C99 Local-Rank Self-Similarity + Dynamic Programming).

    - Không gọi AI / Embedding ($0 chi phí), không dùng từ điển hay Regex ngôn ngữ.
    - Biểu diễn mỗi câu bằng phân phối Character 3/4-grams có trọng số Lượng tin riêng (Self-Information)
      nội tại I(g) = log(1 + M / df(g)): hư từ xuất hiện đều tự động có I(g) thấp, thuật ngữ chủ đề
      cục bộ tự động có I(g) cao trên mọi hệ chữ viết.
    - Làm mượt ngữ cảnh cục bộ Hearst [0.25, 1.0, 0.25], chuẩn hóa ma trận hạng cục bộ C99 (Choi, NAACL 2000),
      và giải Quy hoạch động tìm các đoạn gắn kết nội bộ tối đa.
    """
    import math
    from collections import Counter

    cleaned = text.strip()
    if not cleaned or estimate_unicode_tokens(cleaned) <= max_block_tokens:
        return [cleaned] if cleaned else []

    span_records = _split_atomic_spans_with_offsets(cleaned, max_span_tokens=max_block_tokens)
    spans = [seg for _s, _e, seg in span_records]
    m = len(spans)
    if m <= 1:
        return [cleaned]

    span_tokens = [estimate_unicode_tokens(s) for s in spans]
    prefix_tokens = [0] * (m + 1)
    for i, t_cnt in enumerate(span_tokens):
        prefix_tokens[i + 1] = prefix_tokens[i] + t_cnt

    # 1. Trích xuất Character 3-grams & 4-grams chuẩn hóa Unicode (không phụ thuộc khoảng trắng)
    raw_counters: list[Counter[str]] = []
    df_counts: Counter[str] = Counter()
    for s in spans:
        compact = re.sub(r"\s+", " ", s.casefold()).strip()
        grams: Counter[str] = Counter()
        padded = f" {compact} "
        plen = len(padded)
        if plen >= 3:
            for k in (3, 4):
                if plen >= k:
                    for pos in range(plen - k + 1):
                        grams[padded[pos : pos + k]] += 1
        else:
            grams[compact] += 1
        raw_counters.append(grams)
        for g in grams:
            df_counts[g] += 1

    # 2. Trọng số Self-Information nội tại I(g) = log(1 + M / df(g))
    idf_map = {g: math.log(1.0 + m / df) for g, df in df_counts.items()}
    weighted_vecs: list[dict[str, float]] = []
    for grams in raw_counters:
        weighted_vecs.append({g: cnt * idf_map[g] for g, cnt in grams.items()})

    # 3. Làm mượt cửa sổ lân cận Hearst [0.25, 1.0, 0.25] để câu chuyển ý ngắn không bị tách rời
    smoothed: list[dict[str, float]] = []
    norms: list[float] = []
    for i in range(m):
        vec: dict[str, float] = dict(weighted_vecs[i])
        if i > 0:
            for g, val in weighted_vecs[i - 1].items():
                vec[g] = vec.get(g, 0.0) + 0.25 * val
        if i + 1 < m:
            for g, val in weighted_vecs[i + 1].items():
                vec[g] = vec.get(g, 0.0) + 0.25 * val
        norm = math.sqrt(math.fsum(v * v for v in vec.values())) or 1.0
        smoothed.append(vec)
        norms.append(norm)

    # 4. Ma trận tương đồng Cosine đối xứng S (M x M)
    sim = [[0.0] * m for _ in range(m)]
    for i in range(m):
        sim[i][i] = 1.0
        vi, ni = smoothed[i], norms[i]
        for j in range(i + 1, m):
            vj, nj = smoothed[j], norms[j]
            if len(vi) > len(vj):
                dot = math.fsum(val * vi[g] for g, val in vj.items() if g in vi)
            else:
                dot = math.fsum(val * vj[g] for g, val in vi.items() if g in vj)
            c = max(0.0, min(1.0, dot / (ni * nj)))
            sim[i][j] = sim[j][i] = c

    # 5. Chuẩn hóa hạng cục bộ C99 (cửa sổ bán kính r=2 -> lân cận 5x5) làm sắc nét biên chủ đề
    rank_mat = [[0.0] * m for _ in range(m)]
    radius = 2
    for i in range(m):
        r_lo, r_hi = max(0, i - radius), min(m, i + radius + 1)
        for j in range(m):
            c_lo, c_hi = max(0, j - radius), min(m, j + radius + 1)
            target = sim[i][j]
            lower_cnt = 0
            total_cnt = 0
            for ri in range(r_lo, r_hi):
                row_ri = sim[ri]
                for cj in range(c_lo, c_hi):
                    if ri == i and cj == j:
                        continue
                    total_cnt += 1
                    if row_ri[cj] < target:
                        lower_cnt += 1
            rank_mat[i][j] = (lower_cnt / total_cnt) if total_cnt > 0 else target

    # Tổng tiền tố 2D để tính mật độ gắn kết nội bộ của mọi đoạn [a, b) trong O(1)
    ps2d = [[0.0] * (m + 1) for _ in range(m + 1)]
    for i in range(m):
        row_ps = ps2d[i + 1]
        prev_ps = ps2d[i]
        r_row = rank_mat[i]
        running = 0.0
        for j in range(m):
            running += r_row[j]
            row_ps[j + 1] = prev_ps[j + 1] + running

    def _rect_sum(a: int, b: int) -> float:
        return ps2d[b][b] - ps2d[a][b] - ps2d[b][a] + ps2d[a][a]

    # 6. Quy hoạch động phân đoạn tối ưu (Optimal Topic Partitioning)
    neg_inf = -1e18
    dp = [neg_inf] * (m + 1)
    prev_cut = [0] * (m + 1)
    dp[0] = 0.0

    for b in range(1, m + 1):
        best_val = neg_inf
        best_a = b - 1
        for a in range(b - 1, -1, -1):
            seg_toks = prefix_tokens[b] - prefix_tokens[a]
            if seg_toks > max_block_tokens and b - a > 1:
                break
            cnt = b - a
            mean_cohesion = _rect_sum(a, b) / (cnt * cnt)
            # Phạt nhẹ nếu đoạn quá vụn (< min_block_tokens) để tránh cắt lẻ 1 câu chuyển ý
            short_penalty = (
                0.18 * (min_block_tokens - seg_toks)
                if seg_toks < min_block_tokens and m > 2
                else 0.0
            )
            # Chi phí mở đoạn mới nhỏ giúp ưu tiên giữ chung 1 đoạn khi mật độ gắn kết đồng nhất
            seg_score = dp[a] + (seg_toks * mean_cohesion) - short_penalty - 4.5
            if seg_score > best_val:
                best_val = seg_score
                best_a = a
        dp[b] = best_val
        prev_cut[b] = best_a

    cuts: list[tuple[int, int]] = []
    cur = m
    while cur > 0:
        a = prev_cut[cur]
        cuts.append((a, cur))
        cur = a
    cuts.reverse()

    def _join_span_range(a: int, b: int) -> str:
        parts: list[str] = []
        for idx_s in range(a, b):
            if idx_s > a:
                prev_end = span_records[idx_s - 1][1]
                cur_start = span_records[idx_s][0]
                gap = cleaned[prev_end:cur_start] if cur_start >= prev_end else ""
                if "\n\n" in gap:
                    parts.append("\n\n")
                elif "\n" in gap:
                    parts.append("\n")
                else:
                    parts.append(" ")
            parts.append(span_records[idx_s][2])
        return "".join(parts).strip()

    segments = [seg for a, b in cuts if (seg := _join_span_range(a, b))]
    return segments if segments else [cleaned]


@dataclass(frozen=True, slots=True)
class ExtractedBlock:
    ordinal: int
    block_type: BlockType
    normalized_text: str
    content_hash: str
    page_from: int = 1
    page_to: int = 1
    section_path: str = ""
    source_anchor: str | None = None
    is_boilerplate: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def is_boilerplate_text(text: str) -> bool:
    """Phát hiện tiêu đề/chân trang lặp lại (ví dụ số trang)."""
    stripped = text.strip()
    return any(pattern.match(stripped) for pattern in _BOILERPLATE_PATTERNS)


def compute_content_hash(text: str) -> str:
    """SHA-256 trên văn bản đã chuẩn hóa."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_canonical_block_id(version_id: str, ordinal: int, content_hash: str) -> str:
    """Sinh ID tất định theo công thức Phase 0:
    UUIDv5(NAMESPACE_URL, f"sag:block:{version_id}:{ordinal}:{content_hash}")
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:block:{version_id}:{ordinal}:{content_hash}"))


_PAGE_MARKER_RE = re.compile(
    r"^(?:<!--\s*(?:PAGE|page)\s+(\d+)\s*-->|<!--\s*(?:PAGE_BREAK|page\s*break)\s*-->|[\x0c\f]|---\s*(?:page\s*break|PAGE\s*BREAK)\s*---)$",
    re.IGNORECASE,
)


def _clamp_section_path(path: str, *, default: str = "Root") -> str:
    value = path if path else default
    return value[:512]


def _is_closing_fence(stripped_line: str, fence_char: str, fence_len: int) -> bool:
    return (
        len(stripped_line) >= fence_len
        and set(stripped_line) == {fence_char}
    )


def _is_pipe_table_start(lines: list[str], idx: int) -> bool:
    line = lines[idx]
    if _TABLE_ROW_RE.match(line):
        return True
    return (
        "|" in line
        and idx + 1 < len(lines)
        and bool(_TABLE_SEPARATOR_RE.match(lines[idx + 1]))
    )


def _is_setext_heading(lines: list[str], idx: int) -> re.Match[str] | None:
    if idx + 1 >= len(lines):
        return None
    line = lines[idx]
    stripped = line.strip()
    if not stripped or _PAGE_MARKER_RE.match(stripped) or is_boilerplate_text(stripped):
        return None
    if (
        _FENCE_OPEN_RE.match(line)
        or _HEADING_RE.match(stripped)
        or _HTML_TABLE_OPEN_RE.match(stripped)
        or _TABLE_ROW_RE.match(line)
        or _LIST_ITEM_RE.match(stripped)
    ):
        return None
    if "|" in line and _TABLE_SEPARATOR_RE.match(lines[idx + 1]):
        return None
    return _SETEXT_UNDERLINE_RE.match(lines[idx + 1].strip())


def _html_table_to_markdown(raw_html: str) -> str:
    """Convert MinerU HTML <table>...</table> into a clean Markdown pipe table."""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(raw_html, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(raw_html, "html.parser")

    table_el = soup.find("table") or soup
    rows: list[list[str]] = []
    for tr in table_el.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row_vals = [
            " ".join(cell.get_text(" ", strip=True).split()).replace("|", "\\|")
            for cell in cells
        ]
        if any(row_vals):
            rows.append(row_vals)

    if rows:
        col_count = max(len(r) for r in rows)
        padded = [r + [""] * (col_count - len(r)) for r in rows]
        header = "| " + " | ".join(padded[0]) + " |"
        sep = "| " + " | ".join(["---"] * col_count) + " |"
        body = ["| " + " | ".join(r) + " |" for r in padded[1:]]
        return "\n".join([header, sep, *body])

    try:
        from markdownify import markdownify

        converted = normalize_text(markdownify(raw_html, heading_style="ATX"), preserve_code=True)
        if converted:
            return converted
    except Exception:  # noqa: BLE001
        pass
    return normalize_text(raw_html, preserve_code=True)


def extract_canonical_blocks(
    content: str,
    *,
    version_id: str,
    page_from: int = 1,
    page_to: int = 1,
    document_title: str | None = None,
) -> list[ExtractedBlock]:
    """Phân tích văn bản (Markdown hoặc plain text) thành danh sách các CanonicalBlock có cấu trúc.

    Thuần deterministic, không gọi LLM, bảo toàn bảng biểu, khối mã lệnh và định danh.
    Nhận diện page markers (ví dụ: <!-- PAGE N -->, form feed \\f) để bảo toàn trang nguồn.
    """
    if not content or not content.strip():
        return []

    # Tiền xử lý form feed (\x0c, \f) trước splitlines vì str.splitlines() tự động tách và nuốt ký tự này
    normalized_content = content.replace("\x0c", "\n<!-- PAGE_BREAK -->\n").replace("\f", "\n<!-- PAGE_BREAK -->\n")
    lines = normalized_content.splitlines()
    total_lines = len(lines)
    blocks: list[ExtractedBlock] = []
    current_section_stack: list[tuple[int, str]] = []  # [(level, heading_text)]
    root_raw_title: str | None = None

    idx = 0
    first_non_empty = next((i for i, ln in enumerate(lines[:5]) if ln.strip()), None)
    if first_non_empty is not None and lines[first_non_empty].strip() == "---":
        fm_meta: dict[str, str] = {}
        fm_closed_at: int | None = None
        for f_idx in range(first_non_empty + 1, min(total_lines, first_non_empty + 80)):
            s_ln = lines[f_idx].strip()
            if s_ln in ("---", "..."):
                fm_closed_at = f_idx
                break
            if ":" in s_ln:
                k, _, v = s_ln.partition(":")
                fm_meta[k.strip().lower()] = v.strip().strip('"').strip("'")
        if fm_closed_at is not None and fm_meta:
            doc_id = fm_meta.get("document_id")
            title = fm_meta.get("title")
            status = fm_meta.get("status")
            if doc_id or title:
                parts = [
                    p
                    for p in (
                        title,
                        f"[{doc_id}]" if doc_id else None,
                        f"(Status: {status})" if status else None,
                    )
                    if p
                ]
                current_section_stack = [(0, " ".join(parts))]
                root_raw_title = title
            idx = fm_closed_at + 1
    else:
        head_chunk = "\n".join(lines[:30])
        doc_id_m = re.search(r"\*\*Document ID\*\*\s*\|\s*([A-Za-z0-9_-]+)", head_chunk, re.IGNORECASE)
        if doc_id_m:
            doc_id = doc_id_m.group(1).strip()
            status_m = re.search(r"\*\*Status\*\*\s*\|\s*([A-Za-z0-9 _-]+?)\s*\|", head_chunk, re.IGNORECASE)
            status = status_m.group(1).strip() if status_m else None
            for ln in lines[:10]:
                s_ln = ln.strip()
                if not s_ln or s_ln.startswith("|") or s_ln.startswith("*") or s_ln.startswith("#"):
                    continue
                root_raw_title = normalize_text(s_ln)
                break
            parts = [p for p in (root_raw_title, f"[{doc_id}]", f"(Status: {status})" if status else None) if p]
            current_section_stack = [(0, " ".join(parts))]

    has_h1 = any(
        ((m := _HEADING_RE.match(lines[li].strip())) and len(m.group(1)) == 1)
        or ((sm := _is_setext_heading(lines, li)) and sm.group(1).startswith("="))
        for li in range(idx, total_lines)
    )
    if not current_section_stack and not has_h1 and document_title and document_title.strip():
        clean_doc_title = normalize_text(document_title.strip())
        if clean_doc_title:
            current_section_stack = [(1, clean_doc_title)]
            root_raw_title = clean_doc_title
    ordinal = 0
    current_page = page_from

    while idx < total_lines:
        line = lines[idx]
        stripped = line.strip()

        # Bỏ qua dòng trống giữa các khối
        if not stripped:
            idx += 1
            continue

        # Nhận diện dấu ngắt trang / page marker
        page_marker_match = _PAGE_MARKER_RE.match(stripped)
        if page_marker_match:
            explicit_page = page_marker_match.group(1)
            if explicit_page:
                current_page = int(explicit_page)
            else:
                current_page += 1
            idx += 1
            continue

        # 1. Khối mã lệnh có hàng rào (``` hoặc ~~~, đóng an toàn nếu thiếu dòng đóng)
        fence_match = _FENCE_OPEN_RE.match(line)
        if fence_match and not (fence_match.group(1)[0] == "`" and "`" in fence_match.group(2)):
            fence_token = fence_match.group(1)
            fence_char = fence_token[0]
            fence_len = len(fence_token)
            close_idx: int | None = None
            for look_idx in range(idx + 1, total_lines):
                if _is_closing_fence(lines[look_idx].strip(), fence_char, fence_len):
                    close_idx = look_idx
                    break

            if close_idx is not None:
                code_lines = lines[idx : close_idx + 1]
                idx = close_idx + 1
            else:
                # Unclosed fence: stop before subsequent structural heading/page-marker and close cleanly
                code_lines = [line]
                idx += 1
                while idx < total_lines:
                    curr = lines[idx]
                    curr_s = curr.strip()
                    if (
                        _PAGE_MARKER_RE.match(curr_s)
                        or _HEADING_RE.match(curr_s)
                        or _is_setext_heading(lines, idx)
                    ):
                        break
                    code_lines.append(curr)
                    idx += 1
                code_lines.append(fence_char * fence_len)

            raw_code = "\n".join(code_lines)
            normalized_code = normalize_text(raw_code, preserve_code=True)
            chash = compute_content_hash(normalized_code)
            section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack))

            blocks.append(
                ExtractedBlock(
                    ordinal=ordinal,
                    block_type="code",
                    normalized_text=normalized_code,
                    content_hash=chash,
                    page_from=current_page,
                    page_to=current_page,
                    section_path=section_path,
                    source_anchor=f"block-{ordinal}",
                )
            )
            ordinal += 1
            continue

        # 2. Khối Tiêu đề (ATX #..###### hoặc Setext === / ---)
        heading_match = _HEADING_RE.match(stripped)
        setext_match = None if heading_match else _is_setext_heading(lines, idx)
        if heading_match or setext_match:
            if heading_match:
                level = len(heading_match.group(1))
                heading_title = normalize_text(heading_match.group(2))
                idx += 1
            else:
                assert setext_match is not None
                level = 1 if setext_match.group(1).startswith("=") else 2
                heading_title = normalize_text(stripped)
                idx += 2

            while current_section_stack and current_section_stack[-1][0] >= level:
                current_section_stack.pop()
            if not (root_raw_title and level == 1 and heading_title.lower() == root_raw_title.lower()):
                current_section_stack.append((level, heading_title))

            section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack), default="")
            chash = compute_content_hash(heading_title)

            blocks.append(
                ExtractedBlock(
                    ordinal=ordinal,
                    block_type="heading",
                    normalized_text=heading_title,
                    content_hash=chash,
                    page_from=current_page,
                    page_to=current_page,
                    section_path=section_path,
                    source_anchor=f"h{level}-{ordinal}",
                    metadata={"heading_level": level},
                )
            )
            ordinal += 1
            continue

        # 3a. Khối Bảng HTML từ MinerU (<table>...</table>)
        if _HTML_TABLE_OPEN_RE.match(stripped):
            html_lines = [line]
            closed_html = bool(_HTML_TABLE_CLOSE_RE.search(line))
            idx += 1
            while not closed_html and idx < total_lines:
                curr = lines[idx]
                html_lines.append(curr)
                idx += 1
                if _HTML_TABLE_CLOSE_RE.search(curr):
                    closed_html = True
                    break

            normalized_table = _html_table_to_markdown("\n".join(html_lines))
            if normalized_table:
                chash = compute_content_hash(normalized_table)
                section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack))
                blocks.append(
                    ExtractedBlock(
                        ordinal=ordinal,
                        block_type="table",
                        normalized_text=normalized_table,
                        content_hash=chash,
                        page_from=current_page,
                        page_to=current_page,
                        section_path=section_path,
                        source_anchor=f"tbl-{ordinal}",
                    )
                )
                ordinal += 1
            continue

        # 3b. Khối Bảng biểu Markdown (có hoặc không có | hai đầu)
        if _is_pipe_table_start(lines, idx):
            table_lines = [line]
            idx += 1
            while idx < total_lines:
                curr = lines[idx]
                curr_s = curr.strip()
                if (
                    not curr_s
                    or _PAGE_MARKER_RE.match(curr_s)
                    or _HEADING_RE.match(curr_s)
                    or _FENCE_OPEN_RE.match(curr)
                    or _HTML_TABLE_OPEN_RE.match(curr_s)
                ):
                    break
                if _TABLE_ROW_RE.match(curr) or "|" in curr:
                    table_lines.append(curr)
                    idx += 1
                else:
                    break

            raw_table = "\n".join(table_lines)
            normalized_table = normalize_text(raw_table, preserve_code=True)
            chash = compute_content_hash(normalized_table)
            section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack))

            blocks.append(
                ExtractedBlock(
                    ordinal=ordinal,
                    block_type="table",
                    normalized_text=normalized_table,
                    content_hash=chash,
                    page_from=current_page,
                    page_to=current_page,
                    section_path=section_path,
                    source_anchor=f"tbl-{ordinal}",
                )
            )
            ordinal += 1
            continue

        # 4. Khối Danh sách (List)
        if _LIST_ITEM_RE.match(stripped) and not is_boilerplate_text(stripped):
            list_lines = [line]
            idx += 1
            while idx < total_lines:
                curr = lines[idx]
                curr_stripped = curr.strip()
                if (
                    not curr_stripped
                    or _PAGE_MARKER_RE.match(curr_stripped)
                    or is_boilerplate_text(curr_stripped)
                ):
                    break
                if _LIST_ITEM_RE.match(curr_stripped) or curr.startswith(("  ", "\t")):
                    list_lines.append(curr)
                    idx += 1
                else:
                    break

            raw_list = "\n".join(list_lines)
            normalized_list = normalize_text(raw_list, preserve_code=True)
            chash = compute_content_hash(normalized_list)
            section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack))

            blocks.append(
                ExtractedBlock(
                    ordinal=ordinal,
                    block_type="list",
                    normalized_text=normalized_list,
                    content_hash=chash,
                    page_from=current_page,
                    page_to=current_page,
                    section_path=section_path,
                    source_anchor=f"list-{ordinal}",
                )
            )
            ordinal += 1
            continue

        # 5. Khối Đoạn văn bản thông thường (Paragraph)
        para_lines = [line]
        idx += 1
        while idx < total_lines and not is_boilerplate_text(stripped):
            curr = lines[idx]
            curr_stripped = curr.strip()
            if not curr_stripped:
                # Coalesce PDF/OCR-extracted table cells split by whitespace-only lines
                last_stripped = para_lines[-1].strip()
                if len(para_lines) < 45 and len(last_stripped) <= 56 and not is_boilerplate_text(last_stripped):
                    peek_idx = idx + 1
                    while peek_idx < min(total_lines, idx + 8) and not lines[peek_idx].strip():
                        peek_idx += 1
                    if peek_idx < total_lines:
                        peek_line = lines[peek_idx]
                        peek_stripped = peek_line.strip()
                        if (
                            len(peek_stripped) <= 56
                            and not is_boilerplate_text(peek_stripped)
                            and not _PAGE_MARKER_RE.match(peek_stripped)
                            and not _FENCE_OPEN_RE.match(peek_line)
                            and not _HEADING_RE.match(peek_stripped)
                            and not _is_setext_heading(lines, peek_idx)
                            and not _HTML_TABLE_OPEN_RE.match(peek_stripped)
                            and not _is_pipe_table_start(lines, peek_idx)
                            and not _LIST_ITEM_RE.match(peek_stripped)
                            and (
                                any(ch.isdigit() or ch in "$%()" for ch in (last_stripped + peek_stripped))
                                or len(last_stripped) <= 32
                                or len(peek_stripped) <= 32
                            )
                        ):
                            para_lines.append(peek_stripped)
                            idx = peek_idx + 1
                            continue
                break
            # Dừng paragraph khi gặp page break, heading, code block, hoặc table
            if (
                _PAGE_MARKER_RE.match(curr_stripped)
                or is_boilerplate_text(curr_stripped)
                or _FENCE_OPEN_RE.match(curr)
                or _HEADING_RE.match(curr_stripped)
                or _is_setext_heading(lines, idx)
                or _HTML_TABLE_OPEN_RE.match(curr_stripped)
                or _is_pipe_table_start(lines, idx)
                or _LIST_ITEM_RE.match(curr_stripped)
            ):
                break
            para_lines.append(curr)
            idx += 1

        raw_para = "\n".join(para_lines)
        normalized_para = normalize_text(raw_para)
        section_path = _clamp_section_path(" > ".join(h for _, h in current_section_stack))
        para_segments = (
            segment_prose_by_information_cohesion(normalized_para, max_block_tokens=360)
            if estimate_unicode_tokens(normalized_para) > 360
            else [normalized_para]
        )
        for seg_text in para_segments:
            if not seg_text:
                continue
            chash = compute_content_hash(seg_text)
            is_bp = is_boilerplate_text(seg_text)
            blocks.append(
                ExtractedBlock(
                    ordinal=ordinal,
                    block_type="paragraph",
                    normalized_text=seg_text,
                    content_hash=chash,
                    page_from=current_page,
                    page_to=current_page,
                    section_path=section_path,
                    source_anchor=f"p-{ordinal}",
                    is_boilerplate=is_bp,
                    metadata={"is_boilerplate": is_bp} if is_bp else {},
                )
            )
            ordinal += 1

    return blocks
