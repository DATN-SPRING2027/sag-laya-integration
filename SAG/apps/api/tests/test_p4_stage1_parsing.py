"""TDD regression tests for Milestone P4 Batch B Commit 1 (S1-01, S1-02, S1-03, S1-04, S1-08)."""

from __future__ import annotations

import codecs
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import openpyxl
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import Settings
from sag_api.core.errors import ValidationError
from sag_api.db.base import Base
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType, SourceType
from sag_api.parsing.local_worker import (
    _decode_rtf_bytes,
    _extract_pdf_streaming,
    _extract_pptx_streaming,
    _extract_xlsx_streaming,
    _LocalParseValidationError,
    _sanitize_docx_to_temp_file,
)
from sag_api.parsing.mineru import (
    _inject_page_markers_from_content_list,
    _normalize_anchor_line,
    _synthesize_markdown_from_content_list,
)
from sag_api.parsing.service import (
    _fallback_marker_path,
    _signature,
    prepare_document,
)
from sag_api.services.document_service import reprocess_document


def _make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        **overrides,
    )


def _create_zip(path: str, files: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def _build_minimal_pdf(page_streams: list[bytes]) -> bytes:
    """Build a valid minimal multi-page PDF with Helvetica Type1 font and raw content streams."""
    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3: Font
    num_pages = len(page_streams)
    page_ids = [4 + i * 2 for i in range(num_pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {num_pages} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for idx, stream in enumerate(page_streams):
        page_obj_id = 4 + idx * 2
        content_obj_id = page_obj_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_id} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj_id, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# S1-04: MinerU anchor normalization, monotonic fallback markers, synthesis
# ---------------------------------------------------------------------------


def test_mineru_anchor_preserves_decimals_and_normalizes_nfc() -> None:
    assert _normalize_anchor_line("2025.01 Q1 Report") == "2025.01 q1 report"
    assert _normalize_anchor_line("1.5 Million Users") == "1.5 million users"
    assert _normalize_anchor_line("3.14 Pi Constant") == "3.14 pi constant"
    assert _normalize_anchor_line("## 1. Overview") == "overview"
    assert _normalize_anchor_line("> 2) Second Item") == "second item"
    # NFD Vietnamese must normalize to NFC before casefold
    nfd = "Tie\u0302\u0301ng Vie\u0323\u0302t"
    nfc = "Ti\u1ebfng Vi\u1ec7t"
    assert _normalize_anchor_line(nfd) == _normalize_anchor_line(nfc)


def test_mineru_inject_page_markers_inserts_monotonic_fallback_outside_tables_and_fences() -> None:
    full_md = (
        "# Page 1 Heading\n\n"
        "Intro line on page one.\n\n"
        "```python\n"
        "def inside_fence():\n"
        "    return 42\n"
        "```\n\n"
        "| ColA | ColB |\n"
        "| --- | --- |\n"
        "| R1 | V1 |\n"
        "| R2 | V2 |\n\n"
        "## Page 3 Heading\n\n"
        "Final paragraph on page three."
    )
    content_list = [
        {"type": "text", "text": "Page 1 Heading", "page_idx": 0},
        # Page 2 has text that fails anchor matching (e.g. OCR image caption absent from full.md)
        {"type": "image", "img_caption": ["Unmatched Diagram Caption"], "page_idx": 1},
        {"type": "text", "text": "Page 3 Heading", "page_idx": 2},
    ]
    result = _inject_page_markers_from_content_list(full_md, content_list)
    assert "<!-- PAGE 1 -->" in result
    assert "<!-- PAGE 2 -->" in result
    assert "<!-- PAGE 3 -->" in result

    p1 = result.index("<!-- PAGE 1 -->")
    p2 = result.index("<!-- PAGE 2 -->")
    p3 = result.index("<!-- PAGE 3 -->")
    assert p1 < p2 < p3

    # PAGE 2 must NOT be placed inside the code fence or mid-table
    fence_start = result.index("```python")
    fence_end = result.index("```", fence_start + 3) + 3
    assert not (fence_start < p2 < fence_end)
    assert "| R1 | V1 |\n\n<!-- PAGE 2 -->" not in result


def test_mineru_synthesize_markdown_includes_image_captions_and_markdown_tables() -> None:
    content_list = [
        {"type": "text", "text": "Architecture", "text_level": 1, "page_idx": 0},
        {
            "type": "image",
            "img_path": "images/arch.png",
            "img_caption": ["Figure 1: System Topology"],
            "img_footnote": ["Source: Internal Spec"],
            "page_idx": 0,
        },
        {
            "type": "table",
            "table_caption": "Latency Metrics",
            "table_body": "<table><tr><th>Tier</th><th>P95</th></tr><tr><td>API</td><td>45ms</td></tr></table>",
            "table_footnote": "Measured in ms.",
            "page_idx": 1,
        },
    ]
    md = _synthesize_markdown_from_content_list(content_list)
    assert "<!-- PAGE 1 -->" in md
    assert "<!-- PAGE 2 -->" in md
    assert "Figure 1: System Topology" in md
    assert "Source: Internal Spec" in md
    assert "| Tier | P95 |" in md
    assert "| API | 45ms |" in md
    assert "<table>" not in md
    assert "Measured in ms." in md


# ---------------------------------------------------------------------------
# S1-03: Fallback unpinning on /reprocess & GC sidecar verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_03_reprocess_ignores_fallback_pin_and_clears_marker(
    tmp_path, monkeypatch
) -> None:
    cfg = _make_settings(
        document_parser="auto",
        mineru_official_api_token="test-mineru-token-123456",
    )
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

    mineru_sig = _signature("mineru", cfg)
    markitdown_sig = _signature("markitdown", cfg)
    fallback_md = tmp_path / "doc.pdf.parsed.markitdown.md"
    fallback_md.write_text("# Fallback Content\n", encoding="utf-8")
    marker_path = _fallback_marker_path(str(pdf_path), mineru_sig, cfg)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write("markitdown\n")

    # Normal prepare_document uses fallback cache
    cached_prep = await prepare_document(str(pdf_path), cfg)
    assert cached_prep.provider == "markitdown"
    assert cached_prep.fallback_from == "mineru"
    assert cached_prep.path == f"{pdf_path}.parsed.{markitdown_sig}.md"

    # With ignore_fallback_cache=True (manual reprocess), MinerU is retried and marker is cleared
    parse_mock = AsyncMock(return_value="<!-- PAGE 1 -->\n\n# MinerU Recovered\n")
    monkeypatch.setattr("sag_api.parsing.service.MinerUClient", lambda _s: SimpleNamespace(parse=parse_mock))

    stale_state = {
        "provider": "mineru",
        "signature": mineru_sig,
        "status": "fallback_done",
        "fallback": {
            "provider": "markitdown",
            "signature": markitdown_sig,
            "cache_path": str(fallback_md),
        },
    }
    retried = await prepare_document(
        str(pdf_path),
        cfg,
        state=stale_state,
        ignore_fallback_cache=True,
    )
    parse_mock.assert_awaited_once()
    assert retried.provider == "mineru"
    assert retried.fallback_from is None
    assert not os.path.exists(marker_path)


@pytest.mark.asyncio
async def test_s1_03_reprocess_document_sets_ignore_fallback_cache_flag(tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    fake_queue = SimpleNamespace(
        begin_source_maintenance=lambda *_a, **_kw: None,
        enqueue=AsyncMock(),
    )

    async with factory() as session:
        src = Source(
            id="src-reproc-1",
            name="Src",
            source_type=SourceType.DOCUMENT,
            sag_source_config_id="cfg-reproc-1",
            config={},
        )
        doc = Document(
            id="doc-reproc-1",
            source_id="src-reproc-1",
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=10,
            storage_path=str(tmp_path / "report.pdf"),
            status=DocumentStatus.FAILED,
            is_active=True,
        )
        prev_job = Job(
            id="job-prev-1",
            type=JobType.PROCESS_DOCUMENT,
            source_id="src-reproc-1",
            document_id="doc-reproc-1",
            status=JobStatus.FAILED,
            payload={"document_parser": {"status": "fallback_done"}},
        )
        session.add_all([src, doc, prev_job])
        await session.commit()

        new_job = await reprocess_document(session, src, doc.id, job_queue=fake_queue)
        assert (new_job.payload or {}).get("ignore_fallback_cache") is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_s1_03_missing_source_sidecar_recovery_verifies_provider_and_state(tmp_path) -> None:
    missing_pdf = tmp_path / "deleted.pdf"
    cfg_markitdown = _make_settings(mineru_official_api_token=None)
    cfg_mineru = _make_settings(
        document_parser="mineru",
        mineru_official_api_token="valid-token-12345",
    )

    mineru_sig = _signature("mineru", cfg_mineru)
    mineru_sidecar = tmp_path / f"deleted.pdf.parsed.{mineru_sig}.md"
    mineru_sidecar.write_text("# MinerU Cached\n", encoding="utf-8")

    # 1. MarkItDown-only settings must NOT recover a MinerU sidecar
    with pytest.raises(ValidationError):
        await prepare_document(str(missing_pdf), cfg_markitdown)

    # 2. MinerU settings with mismatched state signature must fail closed
    with pytest.raises(ValidationError):
        await prepare_document(
            str(missing_pdf),
            cfg_mineru,
            state={"provider": "mineru", "signature": "mineru-old-sig"},
        )

    # 3. MinerU settings with mismatched key_fingerprint must fail closed
    with pytest.raises(ValidationError):
        await prepare_document(
            str(missing_pdf),
            cfg_mineru,
            state={
                "provider": "mineru",
                "signature": mineru_sig,
                "base_url": cfg_mineru.mineru_base_url,
                "key_fingerprint": "wrong-fingerprint",
            },
        )

    recovered_ok = await prepare_document(str(missing_pdf), cfg_mineru)
    assert recovered_ok.provider == "mineru"
    assert recovered_ok.cached is True
    assert recovered_ok.path == str(mineru_sidecar)


# ---------------------------------------------------------------------------
# S1-01: PDF extraction via pdfplumber (tables, blank pages, scanned fail-closed)
# ---------------------------------------------------------------------------


def test_s1_01_pdfplumber_extracts_tables_without_duplication_and_marks_blank_pages(
    tmp_path,
) -> None:
    # Page 1: prose + 2x2 grid table with explicit lines
    p1_stream = (
        b"BT /F1 12 Tf 72 720 Td (Quarterly Summary) Tj ET\n"
        b"1 w\n"
        # Table box x: 72..372, y: 500..560, mid x=222, mid y=530
        b"72 560 m 372 560 l S\n"
        b"72 530 m 372 530 l S\n"
        b"72 500 m 372 500 l S\n"
        b"72 500 m 72 560 l S\n"
        b"222 500 m 222 560 l S\n"
        b"372 500 m 372 560 l S\n"
        b"BT /F1 11 Tf 80 540 Td (Metric) Tj ET\n"
        b"BT /F1 11 Tf 230 540 Td (Value) Tj ET\n"
        b"BT /F1 11 Tf 80 510 Td (Revenue) Tj ET\n"
        b"BT /F1 11 Tf 230 510 Td (99M) Tj ET\n"
    )
    # Page 2: blank / scanned page with no text
    p2_stream = b""
    # Page 3: prose
    p3_stream = b"BT /F1 12 Tf 72 720 Td (Conclusion Paragraph) Tj ET\n"

    pdf_path = tmp_path / "mixed.pdf"
    pdf_path.write_bytes(_build_minimal_pdf([p1_stream, p2_stream, p3_stream]))

    md = _extract_pdf_streaming(str(pdf_path))
    assert "<!-- PAGE 1 -->" in md
    assert "<!-- PAGE 2 -->" in md
    assert "<!-- PAGE 3 -->" in md
    assert "Quarterly Summary" in md
    assert "| Metric | Value |" in md
    assert "| Revenue | 99M |" in md
    # Table cell should appear in the markdown table, not duplicated as separate prose
    assert md.count("Revenue") == 1
    assert "Conclusion Paragraph" in md

    # Wholly blank / scanned PDF must fail closed
    blank_pdf = tmp_path / "scanned.pdf"
    blank_pdf.write_bytes(_build_minimal_pdf([b"", b""]))
    with pytest.raises(_LocalParseValidationError):
        _extract_pdf_streaming(str(blank_pdf))


# ---------------------------------------------------------------------------
# S1-02: DOCX alt-text preservation when stripping word/media/*
# ---------------------------------------------------------------------------


def test_s1_02_docx_preserves_alt_text_while_removing_dangling_rids(tmp_path) -> None:
    rels_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1"
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        Target="media/arch.png"/>
      <Relationship Id="rId2"
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        Target="media/vml.png"/>
    </Relationships>"""
    doc_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
                xmlns:v="urn:schemas-microsoft-com:vml"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <w:body>
        <w:p>
          <w:r><w:t>Before image.</w:t></w:r>
          <w:drawing>
            <wp:inline>
              <wp:docPr id="1" name="Picture 1" descr="System Topology Diagram" title="Topology"/>
              <w:blip r:embed="rId1"/>
            </wp:inline>
          </w:drawing>
          <w:pict>
            <v:shape><v:imagedata r:id="rId2" alt="Legacy Flow Chart"/></v:shape>
          </w:pict>
        </w:p>
      </w:body>
    </w:document>"""
    docx_path = _create_zip(
        str(tmp_path / "with_alt.docx"),
        {
            "word/document.xml": doc_xml,
            "word/_rels/document.xml.rels": rels_xml,
            "word/media/arch.png": b"\x89PNG\r\n\x1a\n" + b"0" * 64,
            "word/media/vml.png": b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        },
    )
    sanitized = _sanitize_docx_to_temp_file(docx_path)
    try:
        with zipfile.ZipFile(sanitized, "r") as zf:
            xml_bytes = zf.read("word/document.xml")
            assert b"rId1" not in xml_bytes
            assert b"rId2" not in xml_bytes
            assert b"System Topology Diagram" in xml_bytes
            assert b"Legacy Flow Chart" in xml_bytes
    finally:
        if sanitized != docx_path and os.path.exists(sanitized):
            os.remove(sanitized)


# ---------------------------------------------------------------------------
# S1-08: PPTX .rels + numeric notes, XLSX col truncation, RTF codepage
# ---------------------------------------------------------------------------


def test_s1_08_pptx_rels_numeric_notes_xlsx_col_truncation_and_rtf_codepages(tmp_path) -> None:
    # 1. PPTX notes mapped via .rels to notesSlide7.xml + numeric note preserved + sldNum excluded
    slide1_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:sp>
        <p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:txBody><a:p><a:r><a:t>Budget Review</a:t></a:r></a:p></p:txBody>
      </p:sp></p:spTree></p:cSld>
    </p:sld>"""
    slide1_rels = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1"
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
        Target="../notesSlides/notesSlide7.xml"/>
    </Relationships>"""
    notes7_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
             xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr>
          <p:txBody><a:p><a:r><a:t>42</a:t></a:r></a:p></p:txBody>
        </p:sp>
        <p:sp>
          <p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>
          <p:txBody><a:p><a:r><a:t>99</a:t></a:r></a:p></p:txBody>
        </p:sp>
      </p:spTree></p:cSld>
    </p:notes>"""
    pptx_path = _create_zip(
        str(tmp_path / "mapped_notes.pptx"),
        {
            "ppt/slides/slide1.xml": slide1_xml,
            "ppt/slides/_rels/slide1.xml.rels": slide1_rels,
            "ppt/notesSlides/notesSlide7.xml": notes7_xml,
        },
    )
    pptx_md = _extract_pptx_streaming(pptx_path)
    assert "Budget Review" in pptx_md
    assert "42" in pptx_md
    assert "99" not in pptx_md

    # 2. XLSX column truncation marker
    xlsx_path = str(tmp_path / "wide.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Wide"
    ws.append(["C1", "C2", "C3", "C4", "C5"])
    ws.append([1, 2, 3, 4, 5])
    wb.save(xlsx_path)
    xlsx_md = _extract_xlsx_streaming(xlsx_path, max_rows=10, max_cols=3)
    assert "| C1 | C2 | C3 |" in xlsx_md
    assert "truncated at 3 columns" in xlsx_md

    # 3. RTF Vietnamese cp1258 + UTF-8 ansicpg65001 + \ucN skip count
    # "Tiếng Việt" in cp1258: b"Ti\xea\xecng Vi\xea\xf2t"
    vi_bytes = codecs.encode("Ti\u1ebfng Vi\u1ec7t", "cp1258", errors="ignore")
    # Also test combining tone mark explicitly: b"Ti\\'ea\\'ecng Vi\\'ea\\'f2t"
    rtf_1258 = (
        br"{\rtf1\ansi\ansicpg1258\uc1 {\fonttbl{\f0 Arial;}}"
        br"Xin ch\'e0o Ti\'ea\'ecng Vi\'ea\'f2t \u272?i\'ea\uc2\u8364??X}"
    )
    decoded_1258 = _decode_rtf_bytes(rtf_1258)
    assert "Tiếng Việt" in decoded_1258
    assert "Đ" in decoded_1258
    assert "€" in decoded_1258
    assert "X" in decoded_1258
    assert "?X" not in decoded_1258
    assert vi_bytes  # sanity

    utf8_word = "Phân tích dữ liệu".encode()
    hex_utf8 = "".join(f"\\'{b:02x}" for b in utf8_word).encode("ascii")
    rtf_65001 = br"{\rtf1\ansi\ansicpg65001 " + hex_utf8 + b"}"
    decoded_65001 = _decode_rtf_bytes(rtf_65001)
    assert "Phân tích dữ liệu" in decoded_65001
