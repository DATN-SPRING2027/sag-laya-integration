"""Bộ chuẩn hóa và trích xuất khối văn bản chuẩn (Canonical Block Extractor & Normalizer).

Triết lý Ponytail: Tối giản, thuần stdlib (unicodedata, re, hashlib, uuid),
không dùng LLM, bảo toàn 100% định dạng bảng, thụt lề code và các mã định danh kỹ thuật.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

BlockType = Literal["heading", "paragraph", "table", "code", "list", "caption"]

# Regex phát hiện boilerplate phổ biến: tiêu đề / chân trang đánh số trang
_BOILERPLATE_PATTERNS = [
    re.compile(r"^(?:trang|page)\s+\d+(?:\s*(?:/|of)\s*\d+)?$", re.IGNORECASE),
    re.compile(r"^[-—–]\s*\d+\s*[-—–]$"),
    re.compile(r"^\d+\s*/\s*\d+$"),
]

# Regex nhận diện khối bảng Markdown: ít nhất 1 dòng chứa dấu gạch đứng |
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")

# Regex nhận diện tiêu đề Markdown
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

# Regex nhận diện danh sách
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.+)$")


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


def normalize_text(text: str, *, preserve_code: bool = False) -> str:
    """Chuẩn hóa Unicode NFC, loại bỏ ký tự điều khiển ngoại trừ tab/newline.
    
    Bảo toàn nguyên vẹn dấu chấm câu, định danh kỹ thuật, cấu trúc bảng và thụt lề code.
    """
    if not text:
        return ""

    # 1. Unicode Normalization: NFC dựng sẵn chuẩn quốc tế
    normalized = unicodedata.normalize("NFC", text)

    # 2. Xóa các ký tự điều khiển vô hình (zero-width, null), giữ \n, \t
    cleaned_chars = [
        ch for ch in normalized
        if ch in ("\n", "\t", "\r") or not unicodedata.category(ch).startswith("C")
    ]
    cleaned = "".join(cleaned_chars)

    if preserve_code:
        # Giữ nguyên cấu trúc dòng và khoảng trắng cho code
        return cleaned.rstrip()

    # 3. Chuẩn hóa xuống dòng Unix
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return cleaned.strip()


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


def extract_canonical_blocks(
    content: str,
    *,
    version_id: str,
    page_from: int = 1,
    page_to: int = 1,
) -> list[ExtractedBlock]:
    """Phân tích văn bản (Markdown hoặc plain text) thành danh sách các CanonicalBlock có cấu trúc.
    
    Thuần deterministic, không gọi LLM, bảo toàn bảng biểu, khối mã lệnh và định danh.
    Nhận diện page markers (ví dụ: <!-- PAGE N -->, form feed \f) để bảo toàn trang nguồn.
    """
    if not content or not content.strip():
        return []

    # Tiền xử lý form feed (\x0c, \f) trước splitlines vì str.splitlines() tự động tách và nuốt ký tự này
    normalized_content = content.replace("\x0c", "\n<!-- PAGE_BREAK -->\n").replace("\f", "\n<!-- PAGE_BREAK -->\n")
    lines = normalized_content.splitlines()
    blocks: list[ExtractedBlock] = []
    current_section_stack: list[tuple[int, str]] = []  # [(level, heading_text)]
    ordinal = 0
    current_page = page_from

    idx = 0
    total_lines = len(lines)

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

        # 1. Khối mã lệnh có hàng rào (Fenced Code Block)
        if stripped.startswith("```"):
            code_lines = [line]
            idx += 1
            while idx < total_lines:
                curr = lines[idx]
                code_lines.append(curr)
                if curr.strip().startswith("```"):
                    idx += 1
                    break
                idx += 1

            raw_code = "\n".join(code_lines)
            normalized_code = normalize_text(raw_code, preserve_code=True)
            chash = compute_content_hash(normalized_code)
            section_path = " > ".join(h for _, h in current_section_stack) or "Root"

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

        # 2. Khối Tiêu đề (Heading)
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            level = len(heading_match.group(1))
            heading_title = normalize_text(heading_match.group(2))

            # Điều chỉnh stack phân cấp tiêu đề
            while current_section_stack and current_section_stack[-1][0] >= level:
                current_section_stack.pop()
            current_section_stack.append((level, heading_title))

            section_path = " > ".join(h for _, h in current_section_stack)
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
            idx += 1
            continue

        # 3. Khối Bảng biểu (Markdown Table)
        if _TABLE_ROW_RE.match(line):
            table_lines = [line]
            idx += 1
            while idx < total_lines and _TABLE_ROW_RE.match(lines[idx]):
                table_lines.append(lines[idx])
                idx += 1

            raw_table = "\n".join(table_lines)
            normalized_table = normalize_text(raw_table, preserve_code=True)
            chash = compute_content_hash(normalized_table)
            section_path = " > ".join(h for _, h in current_section_stack) or "Root"

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
        if _LIST_ITEM_RE.match(stripped):
            list_lines = [line]
            idx += 1
            while idx < total_lines:
                curr = lines[idx]
                curr_stripped = curr.strip()
                if not curr_stripped or _PAGE_MARKER_RE.match(curr_stripped):
                    break
                if _LIST_ITEM_RE.match(curr_stripped) or curr.startswith(("  ", "\t")):
                    list_lines.append(curr)
                    idx += 1
                else:
                    break

            raw_list = "\n".join(list_lines)
            normalized_list = normalize_text(raw_list, preserve_code=True)
            chash = compute_content_hash(normalized_list)
            section_path = " > ".join(h for _, h in current_section_stack) or "Root"

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
        while idx < total_lines:
            curr = lines[idx]
            curr_stripped = curr.strip()
            # Dừng paragraph khi gặp dòng trống, page break, heading, code block, hoặc table
            if (
                not curr_stripped
                or _PAGE_MARKER_RE.match(curr_stripped)
                or curr_stripped.startswith("```")
                or _HEADING_RE.match(curr_stripped)
                or _TABLE_ROW_RE.match(curr)
                or _LIST_ITEM_RE.match(curr_stripped)
            ):
                break
            para_lines.append(curr)
            idx += 1

        raw_para = "\n".join(para_lines)
        normalized_para = normalize_text(raw_para)
        chash = compute_content_hash(normalized_para)
        section_path = " > ".join(h for _, h in current_section_stack) or "Root"
        is_bp = is_boilerplate_text(normalized_para)

        blocks.append(
            ExtractedBlock(
                ordinal=ordinal,
                block_type="paragraph",
                normalized_text=normalized_para,
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

