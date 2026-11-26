"""TDD regression tests for Batch P6-1 Packet 2: Temporal chain, Semantic signals & Short MinHash."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source
from sag_api.enums import DocumentStatus
from sag_api.services.dedup_and_temporal_service import (
    compute_minhash_signature,
    detect_semantic_signals,
    estimate_minhash_similarity,
    resolve_temporal_supersedes,
)


@pytest_asyncio.fixture
async def p6_2_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_temporal_supersedes_equal_valid_from_does_not_self_expire(p6_2_db):
    t_same = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
    max_to = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    async with p6_2_db() as session:
        session.add(Source(id="src-eq", name="S", sag_source_config_id="cfg-eq"))
        session.add(
            Document(
                id="doc-eq",
                source_id="src-eq",
                tenant_id="t1",
                project_id="p1",
                filename="eq.pdf",
                storage_path="/tmp/eq.pdf",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

        v1 = DocumentVersion(
            id="veq-1",
            document_id="doc-eq",
            version_no=1,
            file_hash="heq1",
            status="READY",
            search_status="SEARCH_READY",
            source_published_at=t_same,
            observed_at=t_same,
            valid_from=t_same,
            valid_to=max_to,
        )
        v2 = DocumentVersion(
            id="veq-2",
            document_id="doc-eq",
            version_no=2,
            file_hash="heq2",
            status="READY",
            search_status="SEARCH_READY",
            source_published_at=t_same,
            observed_at=t_same,
            valid_from=t_same,
            valid_to=max_to,
        )
        session.add_all([v1, v2])
        await session.flush()

        out = await resolve_temporal_supersedes(session, current_version=v2, document_id="doc-eq")
        assert out["action"] == "SUPERSEDED"
        assert out["active_version_id"] == "veq-2"
        assert v2.supersedes_id == "veq-1"
        assert v1.valid_from == t_same
        assert v1.valid_to == t_same  # v1 closed/superseded
        assert v2.valid_from == t_same
        assert v2.valid_to == max_to  # v2 MUST remain active, NOT self-expired!

        # A 3rd version arriving at the exact same timestamp T must also stay active
        # even though v1 already exists with an empty interval [T, T).
        v3 = DocumentVersion(
            id="veq-3",
            document_id="doc-eq",
            version_no=3,
            file_hash="heq3",
            status="READY",
            search_status="SEARCH_READY",
            source_published_at=t_same,
            observed_at=t_same,
            valid_from=t_same,
            valid_to=max_to,
        )
        session.add(v3)
        await session.flush()

        out3 = await resolve_temporal_supersedes(session, current_version=v3, document_id="doc-eq")
        assert out3["action"] == "SUPERSEDED"
        assert out3["active_version_id"] == "veq-3"
        assert v3.supersedes_id == "veq-2"
        assert v2.valid_to == t_same
        assert v3.valid_from == t_same
        assert v3.valid_to == max_to


@pytest.mark.asyncio
async def test_resolve_temporal_supersedes_out_of_order_handles_naive_valid_to():
    """Naive valid_to on an existing DB row must not raise TypeError in OUT_OF_ORDER_ARCHIVED branch."""
    t_future = datetime(2025, 8, 1, 0, 0, 0)  # naive
    t_older = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    naive_max = datetime(9999, 12, 31, 23, 59, 59)  # naive max_valid_to!

    existing = DocumentVersion(
        id="v-existing",
        document_id="doc-tz",
        version_no=2,
        file_hash="h2",
        status="READY",
        search_status="SEARCH_READY",
        source_published_at=t_future,
        observed_at=t_future,
        valid_from=t_future,
        valid_to=naive_max,
    )
    older = DocumentVersion(
        id="v-older",
        document_id="doc-tz",
        version_no=1,
        file_hash="h1",
        status="READY",
        search_status="SEARCH_READY",
        source_published_at=t_older,
        observed_at=t_older,
        valid_from=t_older,
        valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    )

    class FakeResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [existing])

    fake_session = SimpleNamespace(
        execute=AsyncMock(return_value=FakeResult()),
        add=lambda _obj: None,
    )

    out = await resolve_temporal_supersedes(fake_session, current_version=older, document_id="doc-tz")
    assert out["action"] == "OUT_OF_ORDER_ARCHIVED"
    assert out["active_version_id"] == "v-existing"
    assert older.valid_from == t_older
    assert older.valid_to == t_future.replace(tzinfo=UTC)
    assert existing.supersedes_id == "v-older"


def test_detect_semantic_signals_no_substring_false_positives_and_live_antonyms():
    # Substring collisions MUST NOT trigger false CONTRADICTS
    false_positive_pairs = [
        (
            "The employee will come to the annual finance review",
            "The employee will report income at the annual finance review",
        ),
        (
            "Please count the items in the annual inventory report",
            "Apply a discount to the items in the annual inventory report",
        ),
        (
            "Cloud formation model architecture design document",
            "Cloud information model architecture design document",
        ),
        (
            "Spend less time on manual configuration",
            "Spend unless time on manual configuration",
        ),
    ]
    for t1, t2 in false_positive_pairs:
        assert detect_semantic_signals(t1, t2) == (False, False, False), f"False positive on ({t1!r}, {t2!r})"

    # Previously dead antonym pairs MUST trigger CONTRADICTS
    live_antonym_pairs = [
        (
            "The employee policy answer is yes for remote work",
            "The employee policy answer is no for remote work",
        ),
        (
            "Quy định cho phép nhân viên làm việc từ xa",
            "Quy định cấm nhân viên làm việc từ xa",
        ),
        (
            "Quy định cho nhân viên làm việc từ xa",
            "Quy định cấm nhân viên làm việc từ xa",
        ),
        (
            "Token signature is valid.",
            "Token signature is invalid.",
        ),
        (
            "Service is compliant with audit policy.",
            "Service is noncompliant with audit policy.",
        ),
    ]
    for t1, t2 in live_antonym_pairs:
        contra, _, sup = detect_semantic_signals(t1, t2)
        assert contra is True and sup is False, f"Missed CONTRADICTS on ({t1!r}, {t2!r})"

    # Double-negation equivalence must NOT trigger CONTRADICTS
    assert detect_semantic_signals("TLS 1.3 is not enabled.", "TLS 1.3 is disabled.") == (False, False, False)
    assert detect_semantic_signals(
        "Quy định không cho phép nhân viên làm việc từ xa",
        "Quy định cấm nhân viên làm việc từ xa",
    ) == (False, False, False)


def test_compute_minhash_signature_short_tokens_nonzero_similarity():
    s1 = compute_minhash_signature(["chính", "sách", "mới"])
    s2 = compute_minhash_signature(["chính", "sách", "cũ"])
    sim_3 = estimate_minhash_similarity(s1, s2)
    assert sim_3 >= 0.30, f"Expected non-zero similarity for 2/3 overlap, got {sim_3}"

    # Middle-token edit on 3 tokens must also retain overlap
    m1 = compute_minhash_signature(["chính", "x", "mới"])
    m2 = compute_minhash_signature(["chính", "y", "mới"])
    assert estimate_minhash_similarity(m1, m2) >= 0.30

    # 2-token partial overlap
    b1 = compute_minhash_signature(["chính", "sách"])
    b2 = compute_minhash_signature(["chính", "phủ"])
    assert estimate_minhash_similarity(b1, b2) >= 0.15

    # Still preserve sanity on disjoint short tokens
    d1 = compute_minhash_signature(["alpha", "beta", "gamma"])
    d2 = compute_minhash_signature(["delta", "epsilon", "zeta"])
    assert estimate_minhash_similarity(d1, d2) < 0.10
