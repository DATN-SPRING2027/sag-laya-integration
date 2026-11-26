"""TDD Phase 1 tests: temporal retrieval, lineage MMR/dedup, graph gating, tree bypass, skew & tilt."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import Document, DocumentVersion, Source, SourceProjectMapping
from sag_api.db.models.routing_rag import CanonicalBlock, IngestionRun, SearchUnit, StageRun
from sag_api.enums import DocumentStatus
from sag_api.sag import RetrievedSection
from sag_api.services import search_unit_retrieval_service as surs
from sag_api.services.query_strategy_planner import plan_query
from sag_api.services.retrieval_plan import group_sections_by_document
from sag_api.services.retrieval_service import (
    _compute_authority_recency_tilt,
    _content_shingles,
    _jaccard,
    rerank_sections,
    select_diverse_sections,
)
from sag_api.services.search_index_service import generate_search_unit_point_id

_PROJECT_ID = "proj-p1b"
_TENANT_ID = "tenant-p1b"
_PARTITION_ID = "part-p1b"
_SOURCE_ID = "src-p1b"
_CFG_ID = "cfg-p1b"


def _principal() -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p1b",
        organization_id=_TENANT_ID,
        issuer="https://issuer.test",
        key_id="k1",
        token_id="tok-p1b",
        issued_at=1000,
        expires_at=9999999999,
        tenant_id=_TENANT_ID,
        allowed_project_ids=frozenset({_PROJECT_ID}),
        allowed_partition_ids=frozenset({_PARTITION_ID}),
    )


def _qdrant_point(version_id: str, unit_id: str, content: str, score: float) -> dict:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": generate_search_unit_point_id(f"search_units_{_PROJECT_ID}", unit_id),
        "score": score,
        "payload": {
            "search_unit_id": unit_id,
            "document_version_id": version_id,
            "project_id": _PROJECT_ID,
            "tenant_id": _TENANT_ID,
            "security_partition_id": _PARTITION_ID,
            "content_hash": digest,
            "content": content,
        },
    }


async def _seed_versioned_db(
    monkeypatch: pytest.MonkeyPatch,
    version_specs: list[dict],
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(surs, "SessionLocal", factory)
    now = datetime.now(UTC)

    async with factory() as session:
        src = Source(id=_SOURCE_ID, name="HR Policies", sag_source_config_id=_CFG_ID)
        session.add(src)
        await session.flush()
        session.add(
            SourceProjectMapping(
                id="map-p1b",
                source_id=_SOURCE_ID,
                organization_id=_TENANT_ID,
                project_id=_PROJECT_ID,
                state="CONFIRMED",
                mapping_version=1,
                confirmed_at=now,
                confirmed_by="admin",
                approval_ref="ref-p1b",
            )
        )
        added_docs: set[str] = set()
        for spec in version_specs:
            doc_id = spec.get("document_id", "doc-policy")
            if doc_id not in added_docs:
                session.add(
                    Document(
                        id=doc_id,
                        source_id=_SOURCE_ID,
                        tenant_id=_TENANT_ID,
                        project_id=_PROJECT_ID,
                        filename=spec.get("filename", "leave_policy.md"),
                        storage_path=f"/tmp/{doc_id}.md",
                        status=DocumentStatus.READY,
                        is_active=True,
                    )
                )
                added_docs.add(doc_id)
        await session.flush()

        for spec in version_specs:
            vid = spec["version_id"]
            doc_id = spec.get("document_id", "doc-policy")
            valid_to_val = spec["valid_to"]
            if callable(valid_to_val):
                valid_to_val = valid_to_val()
            session.add(
                DocumentVersion(
                    id=vid,
                    document_id=doc_id,
                    version_no=spec["version_no"],
                    file_hash=f"fh-{vid}",
                    valid_from=spec["valid_from"],
                    valid_to=valid_to_val,
                    status=spec.get("status", "READY"),
                    search_status=spec.get("search_status", "SEARCH_READY"),
                    search_ready_at=now,
                    authority_tier=spec.get("authority_tier"),
                    effective_from=spec.get("effective_from"),
                    scope_tags_json=spec.get("scope_tags_json"),
                    metadata_json={"security_partition_id": _PARTITION_ID},
                )
            )
            await session.flush()

            units = spec["units"]
            for ordinal, (unit_id, heading, content) in enumerate(units):
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                blk_id = f"blk-{unit_id}"
                session.add(
                    CanonicalBlock(
                        id=blk_id,
                        document_version_id=vid,
                        ordinal=ordinal,
                        block_type="paragraph",
                        page_from=1,
                        page_to=1,
                        section_path=heading,
                        source_anchor=f"{vid}-b{ordinal}",
                        normalized_text=content,
                        content_hash=digest,
                    )
                )
                await session.flush()
                session.add(
                    SearchUnit(
                        id=unit_id,
                        document_version_id=vid,
                        block_from_id=blk_id,
                        block_to_id=blk_id,
                        security_partition_id=_PARTITION_ID,
                        content_hash=digest,
                        token_count=max(1, len(content.split())),
                        page_from=1,
                        page_to=1,
                        section_path=heading,
                    )
                )

            run = IngestionRun(
                id=f"run-{vid}",
                document_version_id=vid,
                tenant_id=_TENANT_ID,
                project_id=_PROJECT_ID,
                idempotency_key=f"idem-{vid}",
                payload_hash=f"ph-{vid}",
                status="COMPLETED",
                attempt_count=spec.get("attempt_count", 1),
                started_at=now,
            )
            session.add(run)
            await session.flush()
            checksum = "b" * 64
            session.add(
                StageRun(
                    id=f"sr-{vid}",
                    run_id=run.id,
                    stage="INDEX_SEARCH",
                    status="SUCCESS",
                    created_at=now,
                    metrics_json={
                        "manifest_verified": True,
                        "collection_name": f"search_units_{_PROJECT_ID}",
                        "search_unit_count": len(units),
                        "pg_count": len(units),
                        "qdrant_count": len(units),
                        "qdrant_indexed_count": len(units),
                        "manifest_checksum": checksum,
                        "qdrant_checksum": checksum,
                        "attempt_count": spec.get("attempt_count", 1),
                    },
                )
            )
        await session.commit()
    return engine, factory, src


def _make_mock_qdrant_client(points_by_version: dict[str, list[dict]]) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/count"):
            total = sum(len(pts) for pts in points_by_version.values())
            return httpx.Response(200, json={"result": {"count": total}})
        if request.url.path.endswith("/points/query/batch"):
            import json

            body = json.loads(request.content)
            searches = body.get("searches", [])
            results = []
            for search_req in searches:
                must = (search_req.get("filter") or {}).get("must") or []
                allowed_vids: set[str] | None = None
                for cond in must:
                    if cond.get("key") == "document_version_id":
                        match = cond.get("match") or {}
                        if "any" in match:
                            allowed_vids = set(match["any"])
                        elif "value" in match:
                            allowed_vids = {match["value"]}
                matched_points = []
                for vid, pts in points_by_version.items():
                    if allowed_vids is None or vid in allowed_vids:
                        matched_points.extend(pts)
                results.append({"points": matched_points})
            return httpx.Response(200, json={"result": results})
        raise AssertionError(f"Unexpected Qdrant request: {request.method} {request.url.path}")

    return httpx.AsyncClient(
        base_url="https://qdrant.invalid",
        transport=httpx.MockTransport(respond),
    )


@pytest.mark.asyncio
async def test_pinned_reference_time_consistent_across_all_3_calls_no_503_on_mid_request_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 3 _load_current_ready_versions calls receive identical pinned reference_time;
    a version expiring mid-request causes zero 503 and survives rerank."""
    t_start = datetime.now(UTC)
    expiring_holder: list[datetime] = []

    def _compute_expiring_valid_to() -> datetime:
        dt = datetime.now(UTC) + timedelta(milliseconds=400)
        expiring_holder.append(dt)
        return dt

    content = "Quy định nghỉ phép năm tiêu chuẩn cho nhân viên chính thức là 15 ngày."
    db_engine, _factory, src = await _seed_versioned_db(
        monkeypatch,
        [
            {
                "version_id": "v-expiring",
                "version_no": 1,
                "valid_from": t_start - timedelta(days=30),
                "valid_to": _compute_expiring_valid_to,
                "authority_tier": 1,
                "effective_from": t_start - timedelta(days=30),
                "scope_tags_json": ["HR", "Vietnam"],
                "units": [("u-exp", "Nghỉ phép", content)],
            }
        ],
    )
    expiring_valid_to = expiring_holder[0]

    real_loader = surs._load_current_ready_versions
    recorded_calls: list[dict] = []

    async def spy_loader(
        session,
        sources,
        principal,
        *,
        version_ids=None,
        include_superseded=False,
        reference_time=None,
    ):
        recorded_calls.append(
            {
                "version_ids": version_ids,
                "include_superseded": include_superseded,
                "reference_time": reference_time,
            }
        )
        # Wait past expiring_valid_to before Call #2 so wall_now > valid_to!
        if len(recorded_calls) == 2:
            wait_s = max(0.05, (expiring_valid_to - datetime.now(UTC)).total_seconds() + 0.08)
            await asyncio.sleep(wait_s)
            assert datetime.now(UTC) > expiring_valid_to
        return await real_loader(
            session,
            sources,
            principal,
            version_ids=version_ids,
            include_superseded=include_superseded,
            reference_time=reference_time,
        )

    monkeypatch.setattr(surs, "_load_current_ready_versions", spy_loader)
    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    try:
        async with _make_mock_qdrant_client(
            {"v-expiring": [_qdrant_point("v-expiring", "u-exp", content, 0.92)]}
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Quy định nghỉ phép năm cho nhân viên chính thức là bao nhiêu ngày?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Quy định nghỉ phép năm cho nhân viên chính thức là bao nhiêu ngày?",
                    temporal_mode="current_effective",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
    finally:
        await db_engine.dispose()

    assert len(recorded_calls) == 3
    ref_times = [c["reference_time"] for c in recorded_calls]
    assert all(isinstance(rt, datetime) for rt in ref_times)
    assert ref_times[0] == ref_times[1] == ref_times[2]
    assert [c["include_superseded"] for c in recorded_calls] == [False, False, False]
    assert len(outcome.sections) == 1
    assert outcome.sections[0].document_version_id == "v-expiring"
    assert outcome.sections[0].content == content
    assert outcome.sections[0].authority_tier == 1
    assert outcome.sections[0].scope_tags == ("hr", "vietnam")


@pytest.mark.asyncio
async def test_past_point_exact_time_travel_unparseable_fallback_and_ready_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact time-travel on [valid_from, valid_to), unparseable fallback, metadata hydration,
    content immutability, REVOKED exclusion, and distinct ready_documents count."""
    v1_from = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    v2_from = datetime(2025, 6, 15, 10, 0, 0, tzinfo=UTC)  # mid-day publish
    v_inf = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    c1 = "Chính sách nghỉ phép năm 2024 quy định nhân viên được hưởng 12 ngày phép có lương."
    c2 = "Chính sách nghỉ phép năm 2025 quy định nhân viên được hưởng 18 ngày phép có lương."
    c_rev = "Chính sách nghỉ phép bản thu hồi không còn hiệu lực sử dụng."

    db_engine, factory, src = await _seed_versioned_db(
        monkeypatch,
        [
            {
                "document_id": "doc-policy",
                "version_id": "v1",
                "version_no": 1,
                "valid_from": v1_from,
                "valid_to": v2_from,
                "authority_tier": 2,
                "effective_from": v1_from,
                "scope_tags_json": ["HR", "VN"],
                "units": [("u-v1", "Nghỉ phép V1", c1)],
            },
            {
                "document_id": "doc-policy",
                "version_id": "v2",
                "version_no": 2,
                "valid_from": v2_from,
                "valid_to": v_inf,
                "authority_tier": 1,
                "effective_from": v2_from,
                "scope_tags_json": ["HR", "Global"],
                "units": [("u-v2", "Nghỉ phép V2", c2)],
            },
            {
                "document_id": "doc-revoked",
                "version_id": "v-revoked",
                "version_no": 1,
                "valid_from": v1_from,
                "valid_to": v_inf,
                "status": "REVOKED",
                "search_status": "SEARCH_READY",
                "units": [("u-rev", "Revoked", c_rev)],
            },
        ],
    )

    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    qdrant_points = {
        "v1": [_qdrant_point("v1", "u-v1", c1, 0.90)],
        "v2": [_qdrant_point("v2", "u-v2", c2, 0.91)],
        "v-revoked": [_qdrant_point("v-revoked", "u-rev", c_rev, 0.95)],
    }

    try:
        # Verify REVOKED version is excluded directly by _load_current_ready_versions
        async with factory() as session:
            loaded_all = await surs._load_current_ready_versions(
                session,
                [src],
                _principal(),
                include_superseded=True,
                reference_time=datetime.now(UTC),
            )
        assert set(loaded_all) == {"v1", "v2"}
        assert "v-revoked" not in loaded_all
        assert loaded_all["v2"].document_id == "doc-policy"
        assert loaded_all["v2"].authority_tier == 1
        assert loaded_all["v2"].effective_from == v2_from
        assert loaded_all["v2"].scope_tags == ("global", "hr")

        async with _make_mock_qdrant_client(qdrant_points) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))

            # 1. Exact date 15/06/2025 -> end-of-day UTC matches mid-day v2 ONLY
            out_day = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách nghỉ phép ngày 15/06/2025 là bao nhiêu ngày?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách nghỉ phép ngày 15/06/2025 là bao nhiêu ngày?",
                    temporal_mode="past_point",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.document_version_id for s in out_day.sections] == ["v2"]
            assert out_day.sections[0].content == c2
            expected_c2_hash = hashlib.sha256(out_day.sections[0].content.encode("utf-8")).hexdigest()
            assert expected_c2_hash == out_day.sections[0].content_hash
            assert out_day.sections[0].authority_tier == 1
            assert out_day.sections[0].effective_from == v2_from
            assert out_day.sections[0].scope_tags == ("global", "hr")
            assert out_day.sections[0].is_superseded is False

            # 2. Exclusive before 15/06/2025 -> matches v1 ONLY
            out_before = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách nghỉ phép trước ngày 15/06/2025 là bao nhiêu ngày?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách nghỉ phép trước ngày 15/06/2025 là bao nhiêu ngày?",
                    temporal_mode="past_point",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.document_version_id for s in out_before.sections] == ["v1"]
            assert out_before.sections[0].is_superseded is True
            assert out_before.sections[0].authority_tier == 2
            assert out_before.sections[0].scope_tags == ("hr", "vn")

            # 3. Year 2024 -> matches v1 ONLY
            out_2024 = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách nghỉ phép năm 2024 là bao nhiêu ngày?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách nghỉ phép năm 2024 là bao nhiêu ngày?",
                    temporal_mode="past_point",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert [s.document_version_id for s in out_2024.sections] == ["v1"]

            # 4. Unparseable past_point -> safe fallback keeps BOTH v1 and v2
            out_fallback = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách nghỉ phép hồi xưa quy định thế nào?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách nghỉ phép hồi xưa quy định thế nào?",
                    temporal_mode="past_point",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert {s.document_version_id for s in out_fallback.sections} == {"v1", "v2"}

            # 5. change_lineage: ready_versions=2, ready_documents=1 (same document_id)
            out_lineage = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách nghỉ phép thay đổi như thế nào giữa các phiên bản?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách nghỉ phép thay đổi như thế nào giữa các phiên bản?",
                    temporal_mode="change_lineage",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
            assert out_lineage.stats["ready_versions"] == 2
            assert out_lineage.stats["ready_documents"] == 1
            assert {s.document_version_id for s in out_lineage.sections} == {"v1", "v2"}
    finally:
        await db_engine.dispose()


def test_change_lineage_600_char_single_number_diff_survives_rerank_mmr_dedup_and_grouping() -> None:
    """Two ~650-char versions of the same document differing by one number (Jaccard >= 0.98)
    MUST both survive rerank, select_diverse_sections, and group_sections_by_document in change_lineage."""
    base_prefix = (
        "Điều 4. Quy định chi tiết về hạn mức thanh toán công tác phí và lưu trú dành cho toàn bộ "
        "cán bộ nhân viên làm việc toàn thời gian tại các chi nhánh trong nước và văn phòng đại diện "
        "khu vực phía Bắc, miền Trung và phía Nam. Hồ sơ thanh toán bắt buộc bao gồm hóa đơn tài chính "
        "hợp lệ, giấy đề nghị thanh toán có chữ ký xác nhận của trưởng bộ phận trực tiếp quản lý và "
        "phê duyệt của phòng kế toán trong vòng mười lăm ngày làm việc kể từ ngày kết thúc chuyến công tác. "
        "Trường hợp phát sinh chi phí vượt định mức quy định, người đi công tác phải lập tờ trình giải trình "
        "chi tiết lý do khách quan trước khi trình giám đốc tài chính xem xét và phê duyệt bằng văn bản chính thức. "
        "Mọi khoản tạm ứng chưa hoàn chứng từ quá ba mươi ngày làm việc sẽ được khấu trừ theo quy chế quản trị "
        "tài chính nội bộ của doanh nghiệp trong kỳ lương liền kề. Định mức hỗ trợ tối đa là "
    )
    base_suffix = " USD mỗi ngày làm việc cho toàn bộ các hạng mục lưu trú, di chuyển nội thành và công tác phí."
    text_v1 = f"{base_prefix}100{base_suffix}"
    text_v2 = f"{base_prefix}200{base_suffix}"
    assert len(text_v1) >= 600

    sec_v1 = RetrievedSection(
        chunk_id="su-v1",
        search_unit_id="su-v1",
        document_id="doc-travel",
        document_version_id="ver-1",
        document_name="travel_policy.md",
        heading="Điều 4. Công tác phí",
        content=text_v1,
        score=0.93,
        raw_dense_score=0.88,
        rank=0,
        source_id="src-1",
        source_config_id="cfg-1",
        is_superseded=True,
        valid_to=datetime(2025, 6, 15, tzinfo=UTC),
    )
    sec_v2 = RetrievedSection(
        chunk_id="su-v2",
        search_unit_id="su-v2",
        document_id="doc-travel",
        document_version_id="ver-2",
        document_name="travel_policy.md",
        heading="Điều 4. Công tác phí",
        content=text_v2,
        score=0.94,
        raw_dense_score=0.89,
        rank=1,
        source_id="src-1",
        source_config_id="cfg-1",
        is_superseded=False,
        valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    )
    sim = _jaccard(_content_shingles(sec_v1), _content_shingles(sec_v2))
    assert sim >= 0.98, f"Expected near-duplicate Jaccard >= 0.98, got {sim:.4f}"

    query = "Định mức công tác phí thay đổi như thế nào?"
    reranked = rerank_sections(
        query,
        [sec_v2, sec_v1],
        limit=None,
        temporal_mode="change_lineage",
    )
    assert {s.document_version_id for s in reranked.sections} == {"ver-1", "ver-2"}

    # In change_lineage, both versions survive select_diverse_sections (even with per_document_limit=1)
    selected_lineage, _ = select_diverse_sections(
        query,
        reranked.sections,
        limit=5,
        per_document_limit=1,
        temporal_mode="change_lineage",
    )
    assert {s.document_version_id for s in selected_lineage} == {"ver-1", "ver-2"}

    # In change_lineage, both versions survive group_sections_by_document
    grouped_lineage = group_sections_by_document(
        reranked.sections,
        limit=5,
        temporal_mode="change_lineage",
    )
    assert {s.document_version_id for s in grouped_lineage} == {"ver-1", "ver-2"}

    # Outside change_lineage, near-duplicate collapse & single-doc grouping remain intact
    sec_dup_same = sec_v2.model_copy(update={"chunk_id": "su-v2-copy", "search_unit_id": "su-v2-copy"})
    selected_normal, stats_normal = select_diverse_sections(
        query,
        [sec_v2, sec_dup_same],
        limit=5,
        temporal_mode="current_effective",
    )
    assert len(selected_normal) == 1
    assert stats_normal["deduplicated_count"] == 1


@pytest.mark.asyncio
async def test_expand_multi_hop_gates_supersedes_in_both_modes_and_post_guard_drops_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit B5: SUPERSEDES edges blocked when allows_superseded=False for BOTH lineage_only=True
    and lineage_only=False; traversed when allows_superseded=True; post-expansion guard drops leaks."""
    src = SimpleNamespace(id=_SOURCE_ID, name="HR", sag_source_config_id=_CFG_ID)
    t_past = datetime(2025, 1, 1, tzinfo=UTC)
    t_inf = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    ver_v1 = surs._ReadyVersion(
        document_version_id="ver-1",
        document_id="doc-1",
        source_id=_SOURCE_ID,
        project_id=_PROJECT_ID,
        tenant_id=_TENANT_ID,
        partition_id=_PARTITION_ID,
        source=src,
        valid_to=t_past,
        is_superseded=True,
    )
    ver_v2 = surs._ReadyVersion(
        document_version_id="ver-2",
        document_id="doc-1",
        source_id=_SOURCE_ID,
        project_id=_PROJECT_ID,
        tenant_id=_TENANT_ID,
        partition_id=_PARTITION_ID,
        source=src,
        valid_to=t_inf,
        is_superseded=False,
    )
    sec_v1 = RetrievedSection(
        chunk_id="u-v1",
        search_unit_id="u-v1",
        document_id="doc-1",
        document_version_id="ver-1",
        document_name="policy.md",
        heading="Policy V1",
        content="Old leave quota is 12 days.",
        score=0.0,
        rank=0,
        source_id=_SOURCE_ID,
        source_config_id=_CFG_ID,
        valid_to=t_past,
        is_superseded=True,
        canonical_evidence_verified=True,
        page_from=1,
        page_to=1,
        anchor="v1-0",
    )
    sec_v2 = RetrievedSection(
        chunk_id="u-v2",
        search_unit_id="u-v2",
        document_id="doc-1",
        document_version_id="ver-2",
        document_name="policy.md",
        heading="Policy V2",
        content="Current leave quota is 18 days.",
        score=0.92,
        rank=0,
        source_id=_SOURCE_ID,
        source_config_id=_CFG_ID,
        valid_to=t_inf,
        is_superseded=False,
        canonical_evidence_verified=True,
        page_from=1,
        page_to=1,
        anchor="v2-0",
    )

    k_index = surs._ScopeKnowledgeIndex(
        ku_by_version={},
        ku_by_id={},
        edges_by_ku={},
        ent_exact_versions={},
        hydrated_su_by_version={
            "ver-1": [(sec_v1, 0, 0)],
            "ver-2": [(sec_v2, 0, 0)],
        },
        lineage_by_version_ordinal={
            ("ver-2", 0): [("ver-1", 0, 0, 0.88, "SUPERSEDES")],
        },
    )
    current_versions = {"ver-1": ver_v1, "ver-2": ver_v2}

    # 1. current_effective (allows_superseded=False) with lineage_only=True -> ZERO leak
    exp_lin_false = await surs._expand_multi_hop_sections(
        seed_sections=[sec_v2],
        unit_ordinals={"u-v2": (0, 0)},
        current_versions=current_versions,
        existing_unit_ids={"u-v2"},
        sources=[src],
        limit=8,
        lineage_only=True,
        preloaded_k_index=k_index,
        allows_superseded=False,
    )
    assert exp_lin_false == []

    # 2. current_effective (allows_superseded=False) with lineage_only=False -> ZERO leak!
    exp_mh_false = await surs._expand_multi_hop_sections(
        seed_sections=[sec_v2],
        unit_ordinals={"u-v2": (0, 0)},
        current_versions=current_versions,
        existing_unit_ids={"u-v2"},
        sources=[src],
        limit=8,
        lineage_only=False,
        preloaded_k_index=k_index,
        allows_superseded=False,
    )
    assert exp_mh_false == []

    # 3. change_lineage (allows_superseded=True) -> traverses SUPERSEDES and yields ver-1
    exp_lin_true = await surs._expand_multi_hop_sections(
        seed_sections=[sec_v2],
        unit_ordinals={"u-v2": (0, 0)},
        current_versions=current_versions,
        existing_unit_ids={"u-v2"},
        sources=[src],
        limit=8,
        lineage_only=True,
        preloaded_k_index=k_index,
        allows_superseded=True,
    )
    assert [s.document_version_id for s in exp_lin_true] == ["ver-1"]

    # 4. Defensive post-expansion guard in _retrieve_search_unit_sections drops leaked superseded section
    c_active = "Chính sách bảo mật hiện hành yêu cầu xác thực đa yếu tố bắt buộc."
    db_engine, _factory, db_src = await _seed_versioned_db(
        monkeypatch,
        [
            {
                "document_id": "doc-sec",
                "version_id": "v-cur",
                "version_no": 2,
                "valid_from": t_past,
                "valid_to": t_inf,
                "units": [("u-cur", "Security", c_active)],
            }
        ],
    )
    monkeypatch.setattr(surs, "capture_routing_decisions", AsyncMock(return_value={}))
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))
    monkeypatch.setattr(
        surs,
        "_expand_multi_hop_sections",
        AsyncMock(return_value=[sec_v1.model_copy(update={"score": 0.90})]),
    )
    try:
        async with _make_mock_qdrant_client(
            {"v-cur": [_qdrant_point("v-cur", "u-cur", c_active, 0.92)]}
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [db_src],
                "Chính sách bảo mật hiện hành yêu cầu gì?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách bảo mật hiện hành yêu cầu gì?",
                    temporal_mode="current_effective",
                ),
                candidate_limit=10,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
    finally:
        await db_engine.dispose()

    assert [s.document_version_id for s in outcome.sections] == ["v-cur"]


@pytest.mark.asyncio
async def test_tree_routing_bypassed_when_allows_superseded_runs_global_only_at_full_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When allows_superseded=True (change_lineage / past_point), capture_routing_decisions
    is NOT called, _query_group runs global_only at full candidate_limit, and routing trace
    emits reason_code='TEMPORAL_HISTORY_BYPASS_TREE'."""
    t_past = datetime(2024, 1, 1, tzinfo=UTC)
    t_inf = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
    content = "Chính sách thưởng quý thay đổi qua các năm 2024 và 2025."
    db_engine, _factory, src = await _seed_versioned_db(
        monkeypatch,
        [
            {
                "document_id": "doc-bonus",
                "version_id": "v-bonus",
                "version_no": 1,
                "valid_from": t_past,
                "valid_to": t_inf,
                "units": [("u-bonus", "Bonus", content)],
            }
        ],
    )

    routing_spy = AsyncMock(side_effect=AssertionError("Tree routing MUST be bypassed when allows_superseded=True"))
    captured_query_limits: list[tuple[int, object]] = []
    real_query_group = surs._query_group

    async def spy_query_group(group, *, query, limit, store, query_vector, route=None, sub_sparse_queries=()):
        captured_query_limits.append((limit, route))
        return await real_query_group(
            group,
            query=query,
            limit=limit,
            store=store,
            query_vector=query_vector,
            route=route,
            sub_sparse_queries=sub_sparse_queries,
        )

    monkeypatch.setattr(surs, "capture_routing_decisions", routing_spy)
    monkeypatch.setattr(surs, "_query_group", spy_query_group)
    monkeypatch.setattr(surs, "_query_vector", AsyncMock(return_value=[1.0, 0.0]))

    try:
        async with _make_mock_qdrant_client(
            {"v-bonus": [_qdrant_point("v-bonus", "u-bonus", content, 0.90)]}
        ) as client:
            engine = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=client))
            outcome = await surs._retrieve_search_unit_sections(
                engine,
                [src],
                "Chính sách thưởng quý thay đổi như thế nào?",
                principal=_principal(),
                top_k=5,
                query_strategy_plan=plan_query(
                    "Chính sách thưởng quý thay đổi như thế nào?",
                    temporal_mode="change_lineage",
                ),
                candidate_limit=16,
                deadline=asyncio.get_running_loop().time() + 5.0,
            )
    finally:
        await db_engine.dispose()

    routing_spy.assert_not_called()
    assert captured_query_limits == [(16, None)]
    grp_trace = outcome.stats["routing"]["groups"][0]
    assert grp_trace["reason_code"] == "TEMPORAL_HISTORY_BYPASS_TREE"
    assert grp_trace["fallback_reason"] == "temporal_superseded_scope"
    assert grp_trace["escape_trigger"] == "temporal_history_global_only"


def test_stage_belongs_to_current_attempt_utc_skew_and_attempt_count() -> None:
    base = datetime(2025, 6, 15, 12, 0, 10, 500000, tzinfo=UTC)
    naive_base = base.replace(tzinfo=None)

    # 1. SQLite whole-second truncation skew (<= 1s) with mixed naive/aware UTC -> accepted
    stage_skew_ok = SimpleNamespace(
        created_at=datetime(2025, 6, 15, 12, 0, 10, 0),  # naive, 500ms earlier
        metrics_json={},
    )
    assert surs._stage_belongs_to_current_attempt(base, stage_skew_ok) is True
    assert surs._stage_belongs_to_current_attempt(naive_base, stage_skew_ok) is True

    # 2. Boundary exact 1.0s earlier -> accepted; > 1.0s earlier -> rejected
    stage_exact_1s = SimpleNamespace(created_at=base - timedelta(seconds=1), metrics_json={})
    stage_old = SimpleNamespace(created_at=base - timedelta(seconds=1, milliseconds=10), metrics_json={})
    assert surs._stage_belongs_to_current_attempt(base, stage_exact_1s) is True
    assert surs._stage_belongs_to_current_attempt(base, stage_old) is False

    # 3. 0ms retry invalidation via attempt_count mismatch in metrics_json
    stage_attempt1 = SimpleNamespace(created_at=base, metrics_json={"attempt_count": 1})
    assert surs._stage_belongs_to_current_attempt(base, stage_attempt1, attempt_count=2) is False
    assert surs._stage_belongs_to_current_attempt(base, stage_attempt1, attempt_count=1) is True

    # 4. Legacy stage without attempt_count still passes timestamp check
    stage_legacy = SimpleNamespace(created_at=base, metrics_json={"manifest_verified": True})
    assert surs._stage_belongs_to_current_attempt(base, stage_legacy, attempt_count=2) is True

    # 5. Missing timestamps fail closed
    assert surs._stage_belongs_to_current_attempt(None, stage_exact_1s) is False
    assert surs._stage_belongs_to_current_attempt(base, SimpleNamespace(created_at=None, metrics_json={})) is False


def test_post_ood_rerank_tilt_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
    query = "Chính sách bảo mật mật khẩu doanh nghiệp"

    base_text = "Chính sách bảo mật mật khẩu doanh nghiệp quy định độ dài tối thiểu 12 ký tự."
    sec_none = RetrievedSection(
        chunk_id="c-none",
        document_id="d-none",
        document_version_id="v-none",
        heading="Password Policy",
        content=base_text,
        score=0.80,
        raw_dense_score=0.80,
        rank=0,
    )
    sec_t1_fresh = sec_none.model_copy(
        update={
            "chunk_id": "c-t1",
            "document_id": "d-t1",
            "document_version_id": "v-t1",
            "authority_tier": 1,
            "effective_from": ref,
        }
    )
    sec_t4_ancient = sec_none.model_copy(
        update={
            "chunk_id": "c-t4",
            "document_id": "d-t4",
            "document_version_id": "v-t4",
            "authority_tier": 4,
            "effective_from": ref - timedelta(days=10000),
        }
    )

    # 1. None metadata produces exact 0.0 tilt
    assert _compute_authority_recency_tilt(sec_none, reference_time=ref, temporal_mode="current_effective") == 0.0

    # 2. Max spread <= 0.04 at default settings, and respects authority_rerank_max_delta & enable flag
    t1_tilt = _compute_authority_recency_tilt(sec_t1_fresh, reference_time=ref, temporal_mode="current_effective")
    t4_tilt = _compute_authority_recency_tilt(sec_t4_ancient, reference_time=ref, temporal_mode="current_effective")
    assert t1_tilt > 0.0
    assert t4_tilt < 0.0
    assert (t1_tilt - t4_tilt) <= 0.04 + 1e-12

    monkeypatch.setattr(settings, "authority_rerank_max_delta", 0.02)
    t1_half = _compute_authority_recency_tilt(sec_t1_fresh, reference_time=ref, temporal_mode="current_effective")
    t4_half = _compute_authority_recency_tilt(sec_t4_ancient, reference_time=ref, temporal_mode="current_effective")
    assert (t1_half - t4_half) <= 0.02 + 1e-12
    monkeypatch.setattr(settings, "authority_rerank_max_delta", 0.04)

    monkeypatch.setattr(settings, "temporal_authority_rerank_enabled", False)
    assert _compute_authority_recency_tilt(sec_t1_fresh, reference_time=ref, temporal_mode="current_effective") == 0.0
    monkeypatch.setattr(settings, "temporal_authority_rerank_enabled", True)

    # 3. Disabled (0.0) in change_lineage and past_point
    assert _compute_authority_recency_tilt(sec_t1_fresh, reference_time=ref, temporal_mode="change_lineage") == 0.0
    assert _compute_authority_recency_tilt(sec_t1_fresh, reference_time=ref, temporal_mode="past_point") == 0.0

    # 4. OOD candidate stays filtered even if Tier 1 + fresh; raw_dense_score is NEVER mutated
    sec_ood_t1 = sec_t1_fresh.model_copy(
        update={
            "chunk_id": "c-ood",
            "document_id": "d-ood",
            "document_version_id": "v-ood",
            "heading": "Cafeteria",
            "content": "Menu trưa hôm nay gồm cơm tấm sườn bì chả và canh chua.",
            "score": 0.10,
            "raw_dense_score": 0.10,
        }
    )
    res = rerank_sections(
        query,
        [sec_t4_ancient, sec_t1_fresh, sec_ood_t1],
        limit=5,
        temporal_mode="current_effective",
        reference_time=ref,
    )
    assert [s.chunk_id for s in res.sections] == ["c-t1", "c-t4"]
    assert res.sections[0].raw_dense_score == 0.80
    assert res.sections[1].raw_dense_score == 0.80

    # 5. Legacy None-metadata score is bit-identical with tilt enabled vs disabled
    res_enabled = rerank_sections(
        query,
        [sec_none],
        limit=5,
        temporal_mode="current_effective",
        reference_time=ref,
    )
    monkeypatch.setattr(settings, "temporal_authority_rerank_enabled", False)
    res_disabled = rerank_sections(
        query,
        [sec_none],
        limit=5,
        temporal_mode="current_effective",
        reference_time=ref,
    )
    monkeypatch.setattr(settings, "temporal_authority_rerank_enabled", True)
    assert res_enabled.sections[0].score == res_disabled.sections[0].score

    # 6. Saturation tie-break: two candidates both saturating score == 1.0 break tie by tilt
    # Put c-t4 first in both semantic and lexical lists (stronger lexical/sem score -> both saturate 1.0)
    sat_text = "Chính sách bảo mật mật khẩu doanh nghiệp: Chính sách bảo mật mật khẩu doanh nghiệp."
    sat_t4 = sec_t4_ancient.model_copy(
        update={
            "chunk_id": "a-sat-t4",
            "document_id": "d-sat-t4",
            "content": sat_text,
            "score": 1.0,
            "raw_dense_score": 0.99,
        }
    )
    sat_t1 = sec_t1_fresh.model_copy(
        update={
            "chunk_id": "z-sat-t1",
            "document_id": "d-sat-t1",
            "content": sat_text,
            "score": 1.0,
            "raw_dense_score": 0.99,
        }
    )
    res_sat = rerank_sections(
        query,
        [sat_t4, sat_t1],
        lexical=[sat_t4, sat_t1],
        limit=5,
        temporal_mode="current_effective",
        reference_time=ref,
    )
    assert res_sat.sections[0].chunk_id == "z-sat-t1"
    assert res_sat.sections[1].chunk_id == "a-sat-t4"
