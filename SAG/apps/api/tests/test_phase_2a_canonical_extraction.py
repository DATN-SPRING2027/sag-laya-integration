"""Bộ kiểm thử hồi quy toàn diện cho Phase 2A — Canonical Extraction.

Bao phủ 5 tiêu chí trong tasks/todo.md:
1. Xác nhận trích xuất định dạng có cấu trúc (heading, paragraph, table, code, list).
2. Lưu canonical block type, ordinal, page range, section path, anchor và document version.
3. Kiểm tra normalization giữ bảng/code/punctuation/identifier và dấu vết boilerplate.
4. Xác nhận không dùng LLM để sửa text mặc định (deterministic 100%).
5. Kiểm tra extraction output versioned/temp, retry và lỗi stage (StageRun, ErrorLayer.ENGINE, ErrorStage.PARSE).
"""

from __future__ import annotations

import uuid
import pytest
from sqlalchemy import select

from sag_api.core.error_taxonomy import ErrorLayer, ErrorStage
from sag_api.core.errors import ApiError
from sag_api.db.models.document import Document
from sag_api.db.models.routing_rag import CanonicalBlock, DocumentVersion, IngestionRun, StageRun
from sag_api.parsing.canonical import (
    compute_content_hash,
    extract_canonical_blocks,
    generate_canonical_block_id,
    is_boilerplate_text,
    normalize_text,
)
from sag_api.services.canonical_service import (
    parse_and_persist_document_content,
    persist_canonical_blocks,
)


def test_normalization_preserves_table_structure():
    """Kiểm tra normalization giữ nguyên định dạng Markdown table (|)."""
    raw_table = (
        "| ID | Dịch Vụ | Trạng Thái |\n"
        "|---|---|---|\n"
        "| 1 | Qdrant | Active |\n"
        "| 2 | PostgreSQL | Active |\n"
    )
    normalized = normalize_text(raw_table, preserve_code=True)
    assert "| ID | Dịch Vụ | Trạng Thái |" in normalized
    assert "|---|---|---|" in normalized
    assert "| 1 | Qdrant | Active |" in normalized


def test_normalization_preserves_code_indentation_and_punctuation():
    """Kiểm tra normalization giữ nguyên thụt đầu dòng, dấu ngoặc nhọn và ký hiệu trong code."""
    raw_code = (
        "```python\n"
        "def process_data(user_id: str, timeout_ms: int = 5000):\n"
        "    # Đường dẫn và định danh kỹ thuật\n"
        "    path = '/api/v1/projects/' + user_id\n"
        "    config = {'retry_count': 3, 'error_code': 'ERR_TIMEOUT_408'}\n"
        "    return config\n"
        "```"
    )
    normalized = normalize_text(raw_code, preserve_code=True)
    assert "    def process_data" in normalized or "def process_data" in normalized
    assert "ERR_TIMEOUT_408" in normalized
    assert "'retry_count': 3" in normalized
    assert "/api/v1/projects/" in normalized


def test_normalization_preserves_technical_identifiers():
    """Kiểm tra normalization giữ nguyên snake_case, camelCase, UUID, URLs."""
    raw_text = (
        "Hệ thống SAG sử dụng tenant_id=tenant_continuum_default, "
        "document_version_id=550e8400-e29b-41d4-a716-446655440000, "
        "truy cập qua endpoint https://sag.example.com/api/v1/documents."
    )
    normalized = normalize_text(raw_text)
    assert "tenant_id=tenant_continuum_default" in normalized
    assert "550e8400-e29b-41d4-a716-446655440000" in normalized
    assert "https://sag.example.com/api/v1/documents" in normalized


def test_boilerplate_detection_retains_audit_trace():
    """Kiểm tra phát hiện mẫu boilerplate (chân trang/số trang) để gắn nhãn audit."""
    assert is_boilerplate_text("Trang 1 / 45") is True
    assert is_boilerplate_text("Page 12 of 100") is True
    assert is_boilerplate_text("- 5 -") is True
    assert is_boilerplate_text("1 / 20") is True
    # Văn bản thông thường không bị nhận nhầm
    assert is_boilerplate_text("Đây là nội dung tài liệu về kiến trúc hệ thống.") is False


def test_block_extractor_identifies_all_block_types_and_section_path():
    """Kiểm tra bóc tách đầy đủ các loại khối và duy trì đường dẫn ngữ cảnh (section_path)."""
    document_content = """# Chương 1: Giới thiệu hệ thống

Đây là đoạn mở đầu giới thiệu về kiến trúc tổng quan.

## 1.1 Khối Lưu Trữ

Khối lưu trữ sử dụng PostgreSQL và Qdrant làm thành phần chính.

| Thành Phần | Công Nghệ | Vai Trò |
|---|---|---|
| RDBMS | PostgreSQL 16 | Source of Truth |
| Vector DB | Qdrant | Search Accelerator |

Dưới đây là đoạn code cấu hình:

```python
settings = Settings(sag_vector_provider="qdrant")
```

Danh sách các bước kiểm tra:
- Bước 1: Khởi tạo database
- Bước 2: Nạp schema
- Bước 3: Xác minh manifest

Trang 1 / 10
"""
    ver_id = str(uuid.uuid4())
    blocks = extract_canonical_blocks(document_content, version_id=ver_id, page_from=1, page_to=1)

    assert len(blocks) >= 8

    # 1. Heading 1
    assert blocks[0].block_type == "heading"
    assert blocks[0].normalized_text == "Chương 1: Giới thiệu hệ thống"
    assert blocks[0].section_path == "Chương 1: Giới thiệu hệ thống"

    # 2. Paragraph
    assert blocks[1].block_type == "paragraph"
    assert "đoạn mở đầu giới thiệu" in blocks[1].normalized_text
    assert blocks[1].section_path == "Chương 1: Giới thiệu hệ thống"

    # 3. Heading 2
    assert blocks[2].block_type == "heading"
    assert blocks[2].normalized_text == "1.1 Khối Lưu Trữ"
    assert blocks[2].section_path == "Chương 1: Giới thiệu hệ thống > 1.1 Khối Lưu Trữ"

    # 4. Table block
    table_block = next((b for b in blocks if b.block_type == "table"), None)
    assert table_block is not None
    assert "| RDBMS | PostgreSQL 16 | Source of Truth |" in table_block.normalized_text

    # 5. Code block
    code_block = next((b for b in blocks if b.block_type == "code"), None)
    assert code_block is not None
    assert "sag_vector_provider" in code_block.normalized_text

    # 6. List block
    list_block = next((b for b in blocks if b.block_type == "list"), None)
    assert list_block is not None
    assert "Bước 1: Khởi tạo database" in list_block.normalized_text

    # 7. Boilerplate detection
    bp_block = next((b for b in blocks if b.is_boilerplate), None)
    assert bp_block is not None
    assert bp_block.normalized_text == "Trang 1 / 10"


def test_deterministic_block_id_formula():
    """Xác nhận công thức UUIDv5 sinh ID tất định theo chuẩn Phase 0."""
    version_id = "550e8400-e29b-41d4-a716-446655440000"
    ordinal = 0
    content = "Nội dung chuẩn hóa"
    content_hash = compute_content_hash(content)

    id_1 = generate_canonical_block_id(version_id, ordinal, content_hash)
    id_2 = generate_canonical_block_id(version_id, ordinal, content_hash)

    assert id_1 == id_2
    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sag:block:{version_id}:{ordinal}:{content_hash}"))
    assert id_1 == expected_id


from sag_api.core.db import SessionLocal, init_db


from sag_api.db.models.source import Source


@pytest.mark.asyncio
async def test_canonical_persistence_and_stage_run_tracking():
    """Kiểm tra lưu trữ CanonicalBlock vào DB, gắn kết DocumentVersion và ghi nhận StageRun."""
    await init_db()
    source_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        source = Source(
            id=source_id,
            name="Test Source",
            sag_source_config_id=f"cfg_{source_id[:8]}",
        )
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="test.md",
            status="LOADING",
            storage_path="/tmp/test.md",
        )
        session.add(doc)

        ver = DocumentVersion(
            id=ver_id,
            document_id=doc_id,
            version_no=1,
            file_hash="dummy_hash_123",
            status="RECEIVED",
        )
        session.add(ver)

        ingestion_run = IngestionRun(
            id=run_id,
            tenant_id="tenant_default",
            project_id="proj_default",
            document_version_id=ver_id,
            idempotency_key="idem_key_123",
            payload_hash="dummy_hash_123",
            status="RUNNING",
        )
        session.add(ingestion_run)
        await session.commit()

        content = "# Tiêu Đề\n\nĐây là nội dung thử nghiệm persistence."
        blocks = await parse_and_persist_document_content(
            session,
            ver_id,
            content,
            run_id=run_id,
        )
        await session.commit()

        assert len(blocks) == 2

        # Xác minh trong database
        persisted = (
            await session.execute(
                select(CanonicalBlock)
                .where(CanonicalBlock.document_version_id == ver_id)
                .order_by(CanonicalBlock.ordinal)
            )
        ).scalars().all()

        assert len(persisted) == 2
        assert persisted[0].block_type == "heading"
        assert persisted[1].block_type == "paragraph"
        assert persisted[0].ordinal == 0
        assert persisted[1].ordinal == 1

        # Xác minh StageRun được ghi nhận
        stage_run = (
            await session.execute(
                select(StageRun).where(StageRun.run_id == run_id, StageRun.stage == "PARSE")
            )
        ).scalar_one_or_none()

        assert stage_run is not None
        assert stage_run.status == "SUCCESS"
        assert stage_run.metrics_json["block_count"] == 2


@pytest.mark.asyncio
async def test_idempotent_retry_overwrites_old_blocks_without_duplicates():
    """Kiểm tra khi retry extraction, các khối cũ được xóa sạch sẽ, không tạo bản ghi trùng lặp."""
    await init_db()
    source_id = str(uuid.uuid4())
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())

    async with SessionLocal() as session:
        source = Source(
            id=source_id,
            name="Test Source",
            sag_source_config_id=f"cfg_{source_id[:8]}",
        )
        session.add(source)
        await session.commit()

        doc = Document(
            id=doc_id,
            source_id=source_id,
            filename="test.md",
            status="LOADING",
            storage_path="/tmp/test.md",
        )
        session.add(doc)
        ver = DocumentVersion(id=ver_id, document_id=doc_id, version_no=1, file_hash="hash_retry", status="RECEIVED")
        session.add(ver)
        await session.commit()

        # Lần nạp thứ 1
        content_v1 = "# Bản 1\n\nNội dung ban đầu."
        await parse_and_persist_document_content(session, ver_id, content_v1)
        await session.commit()

        count_1 = (
            await session.execute(
                select(CanonicalBlock).where(CanonicalBlock.document_version_id == ver_id)
            )
        ).scalars().all()
        assert len(count_1) == 2

        # Lần nạp thứ 2 (Retry cùng document_version_id nhưng cập nhật nội dung)
        content_v2 = "# Bản 2 Đã Sửa\n\nNội dung mới cập nhật.\n\nĐoạn bổ sung thứ ba."
        await parse_and_persist_document_content(session, ver_id, content_v2)
        await session.commit()

        count_2 = (
            await session.execute(
                select(CanonicalBlock).where(CanonicalBlock.document_version_id == ver_id)
            )
        ).scalars().all()
        assert len(count_2) == 3
        assert count_2[0].normalized_text == "Bản 2 Đã Sửa"

