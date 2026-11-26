"""Phase 2 Wave 2.1 (P2-A) TDD tests for 2-Stage Conflict Detection Engine,
Async Job (`JobType.CONFLICT_RESOLUTION`), Governance Precedence Matrix,
Fail-Closed Degraded Fallback, HITL Reversibility, KnowledgeQuality 5000 Cap,
Worker Failure Isolation & Lease/ACL Re-Verification, and All Lifecycle Cascades.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.core.config import settings
from sag_api.core.errors import ConflictError, ForbiddenError
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    KnowledgeConflictResolution,
    KnowledgeEvidence,
    KnowledgeGraphEdge,
    KnowledgeUnit,
    SearchUnit,
    Source,
    SourceProjectMapping,
)
from sag_api.db.models.conflict_resolution import compute_conflict_pair_key
from sag_api.enums import (
    ConflictResolutionMode,
    ConflictResolutionStatus,
    ConflictSubtype,
    DocumentStatus,
    JobStatus,
    JobType,
    SourceStatus,
)
from sag_api.jobs import tasks
from sag_api.jobs.control import JobDeferred
from sag_api.services import document_service, source_service
from sag_api.services.knowledge_conflict_service import (
    ConflictCandidate,
    classify_conflict_candidates,
    discover_conflict_candidates,
    enqueue_conflict_resolution_job,
    get_conflict_classifier_degraded_metrics,
    list_project_conflicts,
    persist_conflict_decisions,
    reconcile_resolutions_after_version_ready,
    reopen_conflict,
    reset_conflict_classifier_degraded_metrics,
    resolve_conflict,
    resolve_document_conflicts_service,
)
from sag_api.services.knowledge_quality_repository import (
    MAX_CONTRADICTION_CANDIDATES,
    _contradiction_signals,
)
from sag_api.services.search_index_service import generate_search_unit_point_id
from sag_api.services.source_operation_service import acquire_source_exclusive_lease
from sag_api.services.unit_supersession_overlay import load_superseded_unit_ids_for_scope

TENANT_ID = "tenant-p2a"
PROJECT_ID = "proj-p2a"
PART_A = "part-a"
PART_B = "part-b"
T_JAN = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
T_MAR = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
T_JUN = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)
T_SEP = datetime(2025, 9, 1, 0, 0, 0, tzinfo=UTC)
T_NOW = datetime(2025, 10, 1, 0, 0, 0, tzinfo=UTC)
VALID_TO_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enable_sqlite_fk(engine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


@pytest.fixture
async def p2a_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"))
    reset_conflict_classifier_degraded_metrics()
    yield factory
    reset_conflict_classifier_degraded_metrics()
    await engine.dispose()


async def _seed_doc_unit(
    session: AsyncSession,
    *,
    tag: str,
    text: str,
    source_id: str | None = None,
    document_id: str | None = None,
    logical_source_id: str | None = None,
    version_no: int = 1,
    partition_id: str = PART_A,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    source_published_at: datetime | None = None,
    scope_tags: list[str] | None = None,
    valid_from: datetime = T_JAN,
    valid_to: datetime = VALID_TO_FOREVER,
    supersedes_id: str | None = None,
    spm_state: str = "CONFIRMED",
    section_path: str = "Policy > Quota",
    is_boilerplate: bool = False,
    doc_status: DocumentStatus = DocumentStatus.READY,
    ver_status: str = "READY",
    search_status: str = "SEARCH_READY",
) -> dict[str, object]:
    src_id = source_id or f"src-{tag}"
    src = await session.get(Source, src_id)
    if src is None:
        src = Source(
            id=src_id,
            name=f"Source {tag}",
            sag_source_config_id=f"cfg-{src_id}",
            status=SourceStatus.ACTIVE,
        )
        session.add(src)
        await session.flush()
        spm_kwargs: dict[str, object] = {
            "id": f"spm-{src_id}",
            "source_id": src_id,
            "organization_id": TENANT_ID,
            "project_id": PROJECT_ID,
            "state": spm_state,
            "mapping_version": 1,
        }
        if spm_state == "CONFIRMED":
            spm_kwargs.update(
                confirmed_at=T_JAN,
                confirmed_by="admin",
                approval_ref=f"appr-{src_id}",
            )
        elif spm_state == "REVOKED":
            spm_kwargs.update(
                revoked_at=T_JAN,
                revoked_by="admin",
                revocation_ref=f"rev-{src_id}",
            )
        session.add(SourceProjectMapping(**spm_kwargs))
        await session.flush()

    doc_id = document_id or f"doc-{tag}"
    doc = await session.get(Document, doc_id)
    if doc is None:
        doc = Document(
            id=doc_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            source_id=src_id,
            logical_source_id=logical_source_id or f"logical-{tag}",
            filename=f"{tag}.md",
            storage_path=f"/tmp/{tag}.md",
            status=doc_status,
            is_active=True,
        )
        session.add(doc)
        await session.flush()

    ver_id = f"ver-{tag}"
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc.id,
        version_no=version_no,
        file_hash=_sha256(f"file-{tag}-{version_no}"),
        supersedes_id=supersedes_id,
        authority_tier=authority_tier,
        effective_from=effective_from,
        source_published_at=source_published_at,
        observed_at=T_JAN,
        valid_from=valid_from,
        valid_to=valid_to,
        scope_tags_json=scope_tags,
        status=ver_status,
        search_status=search_status,
        search_ready_at=valid_from if search_status == "SEARCH_READY" else None,
        metadata_json={"security_partition_id": partition_id},
    )
    session.add(ver)
    await session.flush()

    blk_id = f"blk-{tag}"
    blk = CanonicalBlock(
        id=blk_id,
        document_version_id=ver.id,
        ordinal=1,
        block_type="paragraph",
        page_from=1,
        page_to=1,
        section_path=section_path,
        normalized_text=text,
        content_hash=_sha256(text),
        is_boilerplate=is_boilerplate,
    )
    session.add(blk)
    await session.flush()

    unit_id = f"unit-{tag}"
    unit = SearchUnit(
        id=unit_id,
        document_version_id=ver.id,
        block_from_id=blk.id,
        block_to_id=blk.id,
        security_partition_id=partition_id,
        content_hash=_sha256(text),
        token_count=max(1, len(text.split())),
        page_from=1,
        page_to=1,
        section_path=section_path,
        text_snapshot=text,
    )
    session.add(unit)
    await session.flush()
    return {
        "source": src,
        "doc": doc,
        "ver": ver,
        "block": blk,
        "unit": unit,
        "source_id": src.id,
        "document_id": doc.id,
        "version_id": ver.id,
        "unit_id": unit.id,
    }


class _FakeLLM:
    def __init__(self, handler, *, configured: bool = True) -> None:
        self._handler = handler
        self._configured = configured
        self.calls: list[dict] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def complete(
        self,
        messages,
        *,
        response_format=None,
        temperature=None,
        max_tokens=None,
    ) -> str:
        self.calls.append(
            {
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        res = self._handler(messages, response_format)
        if isinstance(res, Exception):
            raise res
        if callable(res):
            return await res()
        return str(res)


# ---------------------------------------------------------------------------
# 1 & 2. Stage 2A Discovery: Zero Embed Calls, Strict Partition, Band & Cap, Early Exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2a_zero_embed_calls_and_strict_partition_isolation(
    p2a_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "conflict_max_pairs_per_version", 2)
    qdrant_requests: list[dict] = []

    async with p2a_db() as session:
        new_s = await _seed_doc_unit(
            session,
            tag="new",
            text="All employees receive 20 annual leave days effective immediately in Q2 policy.",
            partition_id=PART_A,
            authority_tier=1,
            effective_from=T_JUN,
        )
        # 3 valid existing docs in PART_A + 1 doc in PART_B + 1 HITL locked doc
        ex1 = await _seed_doc_unit(
            session,
            tag="ex1",
            text="All employees receive 12 annual leave days under the previous Q1 handbook.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JAN,
        )
        ex2 = await _seed_doc_unit(
            session,
            tag="ex2",
            text="Employees are entitled to 15 annual leave days per calendar year under legacy rule.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JAN,
        )
        ex3 = await _seed_doc_unit(
            session,
            tag="ex3",
            text="Standard annual paid vacation quota is 14 days for full-time staff members.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JAN,
        )
        ex_hitl = await _seed_doc_unit(
            session,
            tag="ex-hitl",
            text="Human-reviewed annual leave policy states 18 days for senior engineers.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JAN,
        )
        ex_part_b = await _seed_doc_unit(
            session,
            tag="ex-part-b",
            text="Executive partition leave policy grants 30 annual leave days confidentially.",
            partition_id=PART_B,
            authority_tier=1,
            effective_from=T_JAN,
        )
        # Seed a MANUAL_HITL resolution on (new, ex_hitl) — must be skipped in Stage 2A!
        session.add(
            KnowledgeConflictResolution(
                id="res-hitl-locked",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                winner_security_partition_id=PART_A,
                pair_key=compute_conflict_pair_key(
                    new_s["version_id"], new_s["unit_id"], ex_hitl["version_id"], ex_hitl["unit_id"]
                ),
                winner_document_id=new_s["document_id"],
                winner_version_id=new_s["version_id"],
                winner_unit_id=new_s["unit_id"],
                winner_content_hash=new_s["unit"].content_hash,
                loser_document_id=ex_hitl["document_id"],
                loser_version_id=ex_hitl["version_id"],
                loser_unit_id=ex_hitl["unit_id"],
                loser_content_hash=ex_hitl["unit"].content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
                confidence=1.0,
                reason="Human approved",
            )
        )
        await session.commit()

        collection = f"search_units_{PROJECT_ID}"
        new_pid = generate_search_unit_point_id(collection, str(new_s["unit_id"]))

        def _qdrant_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            qdrant_requests.append({"method": request.method, "path": request.url.path, "body": body})
            if request.url.path.endswith("/points"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "id": new_pid,
                                "payload": {"search_unit_id": new_s["unit_id"]},
                                "vector": {"content_vector": [0.1, 0.2, 0.3, 0.4]},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/points/query/batch"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "points": [
                                    # Below floor (0.65 < 0.72) -> filtered
                                    {
                                        "id": "p-low",
                                        "score": 0.65,
                                        "payload": {
                                            "search_unit_id": ex1["unit_id"],
                                            "document_version_id": ex1["version_id"],
                                            "document_id": ex1["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex1["unit"].content_hash,
                                        },
                                    },
                                    # Verbatim ceiling (0.998 > 0.995) -> filtered
                                    {
                                        "id": "p-verbatim",
                                        "score": 0.998,
                                        "payload": {
                                            "search_unit_id": ex2["unit_id"],
                                            "document_version_id": ex2["version_id"],
                                            "document_id": ex2["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex2["unit"].content_hash,
                                        },
                                    },
                                    # Valid hits in band [0.72, 0.995]
                                    {
                                        "id": "p-hitl",
                                        "score": 0.95,
                                        "payload": {
                                            "search_unit_id": ex_hitl["unit_id"],
                                            "document_version_id": ex_hitl["version_id"],
                                            "document_id": ex_hitl["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex_hitl["unit"].content_hash,
                                        },
                                    },
                                    # Cross-partition leak from Qdrant -> must be rejected!
                                    {
                                        "id": "p-partb",
                                        "score": 0.94,
                                        "payload": {
                                            "search_unit_id": ex_part_b["unit_id"],
                                            "document_version_id": ex_part_b["version_id"],
                                            "document_id": ex_part_b["document_id"],
                                            "security_partition_id": PART_B,
                                            "content_hash": ex_part_b["unit"].content_hash,
                                        },
                                    },
                                    {
                                        "id": "p-ex1",
                                        "score": 0.91,
                                        "payload": {
                                            "search_unit_id": ex1["unit_id"],
                                            "document_version_id": ex1["version_id"],
                                            "document_id": ex1["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex1["unit"].content_hash,
                                        },
                                    },
                                    {
                                        "id": "p-ex2",
                                        "score": 0.86,
                                        "payload": {
                                            "search_unit_id": ex2["unit_id"],
                                            "document_version_id": ex2["version_id"],
                                            "document_id": ex2["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex2["unit"].content_hash,
                                        },
                                    },
                                    {
                                        "id": "p-ex3",
                                        "score": 0.80,
                                        "payload": {
                                            "search_unit_id": ex3["unit_id"],
                                            "document_version_id": ex3["version_id"],
                                            "document_id": ex3["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": ex3["unit"].content_hash,
                                        },
                                    },
                                ]
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_qdrant_handler),
            base_url="http://qdrant.test",
        ) as qdrant:
            candidates = await discover_conflict_candidates(
                session,
                qdrant_client=qdrant,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                new_document=new_s["doc"],
                new_version=new_s["ver"],
                now_utc=T_NOW,
            )

        # Verify exact Qdrant endpoints: POST /points (with_vector=True) + POST /points/query/batch
        assert len(qdrant_requests) == 2
        assert qdrant_requests[0]["path"] == f"/collections/{collection}/points"
        assert qdrant_requests[0]["body"]["with_vector"] is True
        assert qdrant_requests[0]["body"]["ids"] == [new_pid]

        assert qdrant_requests[1]["path"] == f"/collections/{collection}/points/query/batch"
        search_spec = qdrant_requests[1]["body"]["searches"][0]
        assert search_spec["using"] == "content_vector"
        must = search_spec["filter"]["must"]
        must_not = search_spec["filter"]["must_not"]
        assert {"key": "tenant_id", "match": {"value": TENANT_ID}} in must
        assert {"key": "project_id", "match": {"value": PROJECT_ID}} in must
        assert {"key": "security_partition_id", "match": {"value": PART_A}} in must
        assert {"key": "document_id", "match": {"value": new_s["document_id"]}} in must_not
        assert {"key": "_collection_meta", "match": {"value": True}} in must_not

        # Verify MANUAL_HITL skipped, PART_B rejected, capped at conflict_max_pairs_per_version=2, sorted by cosine desc
        assert len(candidates) == 2
        assert [c.existing_unit_id for c in candidates] == [ex1["unit_id"], ex2["unit_id"]]
        assert candidates[0].cosine_similarity == pytest.approx(0.91)
        assert candidates[1].cosine_similarity == pytest.approx(0.86)


@pytest.mark.asyncio
async def test_stage2a_early_exit_on_first_document(p2a_db):
    qdrant_called = False

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal qdrant_called
        qdrant_called = True
        return httpx.Response(500)

    async with p2a_db() as session:
        only_doc = await _seed_doc_unit(
            session,
            tag="only",
            text="Single policy document in an empty security partition for early exit verification.",
            partition_id=PART_A,
        )
        await session.commit()

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_fail_if_called),
            base_url="http://qdrant.test",
        ) as qdrant:
            candidates = await discover_conflict_candidates(
                session,
                qdrant_client=qdrant,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                new_document=only_doc["doc"],
                new_version=only_doc["ver"],
                now_utc=T_NOW,
            )

        assert candidates == []
        assert qdrant_called is False


# ---------------------------------------------------------------------------
# 3. Stage 2B Governance Matrix Across All 5 ConflictSubtypes & All Auto-Supersede Gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage2b_governance_matrix_all_5_classes(p2a_db):
    async with p2a_db() as session:
        # Seed pairs for every branch in the governance matrix
        cases = [
            # (case_id, new_tier, new_eff, ex_tier, ex_eff, cos, subtype, conf, winner)
            ("supports", 1, T_JUN, 2, T_JAN, 0.88, "SUPPORTS_OR_EQUIVALENT", 0.95, "NONE"),
            ("perspective", 1, T_JUN, 2, T_JAN, 0.85, "DIFFERENT_PERSPECTIVE", 0.91, "NONE"),
            ("scoped_hi", 1, T_JUN, 2, T_JAN, 0.85, "SCOPED_EXCEPTION", 0.90, "NONE"),
            ("scoped_lo", 1, T_JUN, 2, T_JAN, 0.75, "SCOPED_EXCEPTION", 0.90, "NONE"),
            ("auto_tier", 1, T_JAN, 2, T_JUN, 0.91, "DIRECT_SUPERSEDES", 0.92, "NEW"),
            ("auto_eq_newer", 2, T_JUN, 2, T_JAN, 0.90, "DIRECT_SUPERSEDES", 0.92, "NEW"),
            ("auto_existing_wins", 2, T_JAN, 1, T_JAN, 0.89, "DIRECT_SUPERSEDES", 0.93, "EXISTING"),
            ("tier_lower_blocked", 3, T_JUN, 1, T_JAN, 0.90, "DIRECT_SUPERSEDES", 0.92, "NEW"),
            ("low_conf", 1, T_JUN, 2, T_JAN, 0.90, "DIRECT_SUPERSEDES", 0.80, "NEW"),
            ("none_tiers_no_ts", None, None, None, None, 0.90, "DIRECT_SUPERSEDES", 0.95, "NEW"),
            ("none_tiers_with_ts", None, T_JUN, None, T_JAN, 0.90, "DIRECT_SUPERSEDES", 0.95, "NEW"),
            ("one_sided_none", 1, T_JUN, None, T_JAN, 0.90, "DIRECT_SUPERSEDES", 0.95, "NEW"),
            ("hard_contra", 1, T_JUN, 2, T_JAN, 0.88, "HARD_CONTRADICTION", 0.94, "NONE"),
        ]

        candidates: list[ConflictCandidate] = []
        case_meta: dict[str, tuple[dict, dict]] = {}
        expected_llm_items: list[dict] = []

        for cid, n_tier, n_eff, e_tier, e_eff, cos, subtype, conf, winner in cases:
            new_s = await _seed_doc_unit(
                session,
                tag=f"n-{cid}",
                text=f"New clause for {cid}: annual leave limit is 20 days per year.",
                authority_tier=n_tier,
                effective_from=n_eff,
            )
            ex_s = await _seed_doc_unit(
                session,
                tag=f"e-{cid}",
                text=f"Existing clause for {cid}: annual leave limit is 12 days per year.",
                authority_tier=e_tier,
                effective_from=e_eff,
            )
            pk = compute_conflict_pair_key(
                str(new_s["version_id"]),
                str(new_s["unit_id"]),
                str(ex_s["version_id"]),
                str(ex_s["unit_id"]),
            )
            cand = ConflictCandidate(
                pair_id=cid,
                pair_key=pk,
                cosine_similarity=cos,
                new_document_id=str(new_s["document_id"]),
                new_document_name=new_s["doc"].filename,
                new_version_id=str(new_s["version_id"]),
                new_version=new_s["ver"],
                new_unit_id=str(new_s["unit_id"]),
                new_content_hash=new_s["unit"].content_hash,
                new_section_path=new_s["unit"].section_path,
                new_text=new_s["unit"].text_snapshot or "",
                new_security_partition_id=PART_A,
                existing_document_id=str(ex_s["document_id"]),
                existing_document_name=ex_s["doc"].filename,
                existing_version_id=str(ex_s["version_id"]),
                existing_version=ex_s["ver"],
                existing_unit_id=str(ex_s["unit_id"]),
                existing_content_hash=ex_s["unit"].content_hash,
                existing_section_path=ex_s["unit"].section_path,
                existing_text=ex_s["unit"].text_snapshot or "",
                existing_security_partition_id=PART_A,
            )
            candidates.append(cand)
            case_meta[cid] = (new_s, ex_s)
            expected_llm_items.append(
                {
                    "pair_id": cid,
                    "classification": subtype,
                    "confidence": conf,
                    "winner": winner,
                    "scope_difference_summary": "Applies to VN vs US" if subtype == "SCOPED_EXCEPTION" else None,
                    "reason": f"Classified as {subtype}",
                }
            )
        await session.commit()

        llm_by_id = {item["pair_id"]: item for item in expected_llm_items}

        def _llm_handler(messages, _rf):
            user_text = messages[-1]["content"]
            batch_items = [item for pid, item in llm_by_id.items() if f'"{pid}"' in user_text]
            return json.dumps({"items": batch_items})

        llm = _FakeLLM(_llm_handler)
        cls_res = await classify_conflict_candidates(
            candidates,
            llm=llm,
            project_id=PROJECT_ID,
            tenant_id=TENANT_ID,
            document_id="doc-matrix",
            document_version_id="ver-matrix",
        )
        assert cls_res.degraded is False
        assert len(cls_res.decisions) == len(cases)

        summary = await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=cls_res.decisions,
            now_utc=T_NOW,
        )
        await session.commit()

        async def _get_row(cid: str) -> KnowledgeConflictResolution | None:
            new_s, ex_s = case_meta[cid]
            pk = compute_conflict_pair_key(
                str(new_s["version_id"]),
                str(new_s["unit_id"]),
                str(ex_s["version_id"]),
                str(ex_s["unit_id"]),
            )
            return await session.scalar(
                select(KnowledgeConflictResolution).where(
                    KnowledgeConflictResolution.project_id == PROJECT_ID,
                    KnowledgeConflictResolution.pair_key == pk,
                )
            )

        # 1. Non-conflicts: 0 rows
        assert await _get_row("supports") is None
        assert await _get_row("perspective") is None
        assert await _get_row("scoped_lo") is None

        # 2. SCOPED_EXCEPTION (cos >= 0.80) -> DISMISSED_SCOPED, never supersedes
        r_scoped = await _get_row("scoped_hi")
        assert r_scoped is not None
        assert r_scoped.status == ConflictResolutionStatus.DISMISSED_SCOPED.value
        assert r_scoped.resolution_mode == ConflictResolutionMode.KEEP_BOTH_SCOPED.value
        assert r_scoped.effective_valid_to is None
        assert r_scoped.security_partition_id == PART_A
        assert r_scoped.winner_security_partition_id == PART_A

        # 3. Auto-supersede cases -> ACTIVE + AUTO_POLICY + SUPERSEDES edge
        for cid in ("auto_tier", "auto_eq_newer", "auto_existing_wins", "none_tiers_with_ts"):
            row = await _get_row(cid)
            assert row is not None, cid
            assert row.status == ConflictResolutionStatus.ACTIVE.value, cid
            assert row.resolution_mode == ConflictResolutionMode.AUTO_POLICY.value, cid
            assert row.effective_valid_to is not None, cid
            assert row.security_partition_id == PART_A
            assert row.winner_security_partition_id == PART_A
            edge = await session.scalar(
                select(KnowledgeGraphEdge).where(
                    KnowledgeGraphEdge.source_unit_id == row.winner_unit_id,
                    KnowledgeGraphEdge.target_unit_id == row.loser_unit_id,
                    KnowledgeGraphEdge.edge_type == "SUPERSEDES",
                )
            )
            assert edge is not None, cid

        # Verify symmetric orientation on auto_existing_wins
        r_ex_wins = await _get_row("auto_existing_wins")
        assert r_ex_wins is not None
        assert r_ex_wins.winner_document_id == case_meta["auto_existing_wins"][1]["document_id"]
        assert r_ex_wins.loser_document_id == case_meta["auto_existing_wins"][0]["document_id"]

        # 4. Gated-out DIRECT_SUPERSEDES & HARD_CONTRADICTION -> PENDING_REVIEW + CONTRADICTS edge (is_candidate=True)
        for cid in ("tier_lower_blocked", "low_conf", "none_tiers_no_ts", "one_sided_none", "hard_contra"):
            row = await _get_row(cid)
            assert row is not None, cid
            assert row.status == ConflictResolutionStatus.PENDING_REVIEW.value, cid
            assert row.resolution_mode == ConflictResolutionMode.UNRESOLVED.value, cid
            assert row.effective_valid_to is None, cid
            edge = await session.scalar(
                select(KnowledgeGraphEdge).where(
                    KnowledgeGraphEdge.id == row.graph_edge_id,
                    KnowledgeGraphEdge.edge_type == "CONTRADICTS",
                )
            )
            assert edge is not None, cid
            assert edge.metadata_json.get("is_candidate") is True, cid

        assert summary.auto_superseded == 4
        assert summary.pending_review == 5
        assert summary.dismissed_scoped == 1


# ---------------------------------------------------------------------------
# 4. Stage 2B Fail-Closed Degraded Fallback (llm=None & TimeoutError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["llm_none", "timeout"])
async def test_stage2b_degraded_fallback_never_auto_supersedes(p2a_db, caplog, mode: str):
    async with p2a_db() as session:
        # Tier 1 newer vs Tier 2 older — would auto-supersede if not degraded!
        new_s = await _seed_doc_unit(
            session,
            tag=f"deg-new-{mode}",
            text="Remote work allowance is strictly 4 days per week for all engineering staff.",
            authority_tier=1,
            effective_from=T_JUN,
        )
        ex_s = await _seed_doc_unit(
            session,
            tag=f"deg-ex-{mode}",
            text="Remote work allowance is strictly 2 days per week for all engineering staff.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        await session.commit()

        cand = ConflictCandidate(
            pair_id="p-deg",
            pair_key=compute_conflict_pair_key(
                str(new_s["version_id"]),
                str(new_s["unit_id"]),
                str(ex_s["version_id"]),
                str(ex_s["unit_id"]),
            ),
            cosine_similarity=0.92,
            new_document_id=str(new_s["document_id"]),
            new_document_name="new.md",
            new_version_id=str(new_s["version_id"]),
            new_version=new_s["ver"],
            new_unit_id=str(new_s["unit_id"]),
            new_content_hash=new_s["unit"].content_hash,
            new_section_path="Policy",
            new_text=new_s["unit"].text_snapshot or "",
            new_security_partition_id=PART_A,
            existing_document_id=str(ex_s["document_id"]),
            existing_document_name="old.md",
            existing_version_id=str(ex_s["version_id"]),
            existing_version=ex_s["ver"],
            existing_unit_id=str(ex_s["unit_id"]),
            existing_content_hash=ex_s["unit"].content_hash,
            existing_section_path="Policy",
            existing_text=ex_s["unit"].text_snapshot or "",
            existing_security_partition_id=PART_A,
        )

        llm = None if mode == "llm_none" else _FakeLLM(lambda _m, _rf: TimeoutError("timed out"))
        with caplog.at_level(logging.WARNING):
            cls_res = await classify_conflict_candidates(
                [cand],
                llm=llm,
                project_id=PROJECT_ID,
                tenant_id=TENANT_ID,
                document_id=str(new_s["document_id"]),
                document_version_id=str(new_s["version_id"]),
            )

        assert cls_res.degraded is True
        assert cls_res.degraded_reason is not None
        assert any("degraded=True" in rec.getMessage() for rec in caplog.records)
        metrics = get_conflict_classifier_degraded_metrics()
        assert sum(metrics.values()) >= 1
        assert len(cls_res.decisions) == 1
        assert cls_res.decisions[0].degraded_fallback is True
        assert cls_res.decisions[0].conflict_subtype == ConflictSubtype.HARD_CONTRADICTION

        await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=cls_res.decisions,
            now_utc=T_NOW,
        )
        await session.commit()

        row = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.pair_key == cand.pair_key
            )
        )
        assert row is not None
        assert row.degraded_fallback is True
        assert row.status == ConflictResolutionStatus.PENDING_REVIEW.value
        assert row.resolution_mode == ConflictResolutionMode.UNRESOLVED.value
        assert row.effective_valid_to is None


# ---------------------------------------------------------------------------
# 5. HITL List (Dual-Partition ACL + Document Filter), Resolve, Reopen & Immunity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hitl_resolve_winner_a_winner_b_keep_both_and_reopen_reversibility(p2a_db):
    async with p2a_db() as session:
        doc_a = await _seed_doc_unit(
            session,
            tag="hitl-a",
            logical_source_id="ext-logical-a",
            text="Expense cap for domestic meals is 300k VND per day.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JAN,
            section_path="Finance > Travel",
        )
        doc_b = await _seed_doc_unit(
            session,
            tag="hitl-b",
            logical_source_id="ext-logical-b",
            text="Expense cap for domestic meals is 500k VND per day.",
            partition_id=PART_A,
            authority_tier=2,
            effective_from=T_JUN,
            section_path="Finance > Travel 2025",
        )
        # Cross-partition synthetic row to prove dual-partition ACL prevents side-channel
        doc_secret = await _seed_doc_unit(
            session,
            tag="hitl-secret",
            text="Secret executive meal cap is 2M VND.",
            partition_id=PART_B,
            authority_tier=1,
            effective_from=T_JUN,
        )
        await session.commit()

        pk_ab = compute_conflict_pair_key(
            str(doc_b["version_id"]),
            str(doc_b["unit_id"]),
            str(doc_a["version_id"]),
            str(doc_a["unit_id"]),
        )
        edge = KnowledgeGraphEdge(
            id=str(uuid.uuid4()),
            project_id=PROJECT_ID,
            source_unit_id=str(doc_b["unit_id"]),
            target_unit_id=str(doc_a["unit_id"]),
            edge_type="CONTRADICTS",
            weight=0.9,
            calibrated_weight=0.9,
            metadata_json={"is_candidate": True, "conflict_subtype": "HARD_CONTRADICTION"},
        )
        session.add(edge)
        await session.flush()

        res_ab = KnowledgeConflictResolution(
            id="res-hitl-ab",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=pk_ab,
            winner_document_id=str(doc_b["document_id"]),
            winner_version_id=str(doc_b["version_id"]),
            winner_unit_id=str(doc_b["unit_id"]),
            winner_content_hash=doc_b["unit"].content_hash,
            loser_document_id=str(doc_a["document_id"]),
            loser_version_id=str(doc_a["version_id"]),
            loser_unit_id=str(doc_a["unit_id"]),
            loser_content_hash=doc_a["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.9,
            cosine_similarity=0.89,
            reason="Meal cap mismatch",
            graph_edge_id=edge.id,
        )
        res_cross = KnowledgeConflictResolution(
            id="res-hitl-cross",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_B,
            pair_key=compute_conflict_pair_key(
                str(doc_secret["version_id"]),
                str(doc_secret["unit_id"]),
                str(doc_a["version_id"]),
                str(doc_a["unit_id"]),
            ),
            winner_document_id=str(doc_secret["document_id"]),
            winner_version_id=str(doc_secret["version_id"]),
            winner_unit_id=str(doc_secret["unit_id"]),
            winner_content_hash=doc_secret["unit"].content_hash,
            loser_document_id=str(doc_a["document_id"]),
            loser_version_id=str(doc_a["version_id"]),
            loser_unit_id=str(doc_a["unit_id"]),
            loser_content_hash=doc_a["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.9,
            reason="Cross partition",
        )
        session.add_all([res_ab, res_cross])
        await session.commit()

        # 1. list_project_conflicts enforces BOTH partitions, hydrates fields & filters by document_id
        items, total = await list_project_conflicts(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
        )
        assert total == 1
        assert len(items) == 1
        item = items[0]
        assert item.id == "res-hitl-ab"
        assert item.side_a.document_id == doc_a["document_id"]
        assert item.side_a.document_name == "hitl-a.md"
        assert item.side_a.version_no == 1
        assert item.side_a.text_snapshot == doc_a["unit"].text_snapshot
        assert item.side_a.authority_tier == 2
        assert item.side_a.security_partition_id == PART_A
        assert item.side_b.document_id == doc_b["document_id"]
        assert item.side_b.section_path == "Finance > Travel 2025"

        # Filter by logical_source_id
        items_by_logical, total_logical = await list_project_conflicts(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            document_id="ext-logical-a",
        )
        assert total_logical == 1
        assert items_by_logical[0].id == "res-hitl-ab"

        # Cross-partition resolve/reopen MUST fail closed with ForbiddenError for PART_A-only principal
        with pytest.raises(ForbiddenError):
            await resolve_conflict(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                allowed_partition_ids={PART_A},
                resolution_id="res-hitl-cross",
                action="WINNER_B",
                reason="attempt cross partition",
                actor="user-1",
            )

        # 2. WINNER_B -> ACTIVE (MANUAL_HITL), CONTRADICTS is_candidate=False, SUPERSEDES created
        resolved_b = await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-hitl-ab",
            action="WINNER_B",
            reason="Confirm 2025 policy",
            actor="reviewer-1",
        )
        assert resolved_b.status == ConflictResolutionStatus.ACTIVE.value
        assert resolved_b.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value
        assert resolved_b.winner_document_id == doc_b["document_id"]
        assert resolved_b.pair_key == pk_ab
        await session.refresh(edge)
        assert edge.metadata_json["is_candidate"] is False
        sup_b = await session.scalar(
            select(KnowledgeGraphEdge).where(
                KnowledgeGraphEdge.source_unit_id == doc_b["unit_id"],
                KnowledgeGraphEdge.target_unit_id == doc_a["unit_id"],
                KnowledgeGraphEdge.edge_type == "SUPERSEDES",
            )
        )
        assert sup_b is not None

        # 3. reopen_conflict -> PENDING_REVIEW, SUPERSEDES removed, CONTRADICTS is_candidate=True
        reopened = await reopen_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-hitl-ab",
            reason="Need finance VP check",
            actor="reviewer-2",
        )
        assert reopened.status == ConflictResolutionStatus.PENDING_REVIEW.value
        assert reopened.resolution_mode == ConflictResolutionMode.UNRESOLVED.value
        assert reopened.effective_valid_to is None
        await session.refresh(edge)
        assert edge.metadata_json["is_candidate"] is True
        assert (
            await session.scalar(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
            is None
        )

        # 4. WINNER_A -> symmetric swap on SAME row, pair_key invariant, no duplicate row!
        resolved_a = await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-hitl-ab",
            action="WINNER_A",
            reason="Keep original 300k cap",
            actor="reviewer-3",
        )
        assert resolved_a.id == "res-hitl-ab"
        assert resolved_a.pair_key == pk_ab
        assert resolved_a.winner_document_id == doc_a["document_id"]
        assert resolved_a.loser_document_id == doc_b["document_id"]
        assert resolved_a.status == ConflictResolutionStatus.ACTIVE.value
        assert resolved_a.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value
        sup_a = await session.scalar(
            select(KnowledgeGraphEdge).where(
                KnowledgeGraphEdge.source_unit_id == doc_a["unit_id"],
                KnowledgeGraphEdge.target_unit_id == doc_b["unit_id"],
                KnowledgeGraphEdge.edge_type == "SUPERSEDES",
            )
        )
        assert sup_a is not None

        # 5. Re-running persist_conflict_decisions MUST NOT overwrite MANUAL_HITL row
        cand_retry = ConflictCandidate(
            pair_id="retry",
            pair_key=pk_ab,
            cosine_similarity=0.91,
            new_document_id=str(doc_b["document_id"]),
            new_document_name="hitl-b.md",
            new_version_id=str(doc_b["version_id"]),
            new_version=doc_b["ver"],
            new_unit_id=str(doc_b["unit_id"]),
            new_content_hash=doc_b["unit"].content_hash,
            new_section_path="Finance",
            new_text=doc_b["unit"].text_snapshot or "",
            new_security_partition_id=PART_A,
            existing_document_id=str(doc_a["document_id"]),
            existing_document_name="hitl-a.md",
            existing_version_id=str(doc_a["version_id"]),
            existing_version=doc_a["ver"],
            existing_unit_id=str(doc_a["unit_id"]),
            existing_content_hash=doc_a["unit"].content_hash,
            existing_section_path="Finance",
            existing_text=doc_a["unit"].text_snapshot or "",
            existing_security_partition_id=PART_A,
        )
        cls_retry = await classify_conflict_candidates(
            [cand_retry],
            llm=_FakeLLM(
                lambda _m, _rf: json.dumps(
                    {
                        "items": [
                            {
                                "pair_id": "retry",
                                "classification": "DIRECT_SUPERSEDES",
                                "confidence": 0.99,
                                "winner": "NEW",
                                "scope_difference_summary": None,
                                "reason": "Machine override attempt",
                            }
                        ]
                    }
                )
            ),
            project_id=PROJECT_ID,
            tenant_id=TENANT_ID,
        )
        persist_res = await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=cls_retry.decisions,
            now_utc=T_NOW,
        )
        await session.commit()
        assert persist_res.skipped_manual_hitl == 1
        await session.refresh(resolved_a)
        assert resolved_a.winner_document_id == doc_a["document_id"]
        assert resolved_a.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value

        # 6. KEEP_BOTH_SCOPED -> DISMISSED_SCOPED, SUPERSEDES edge removed
        kept = await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-hitl-ab",
            action="KEEP_BOTH_SCOPED",
            reason="Different regional bands",
            actor="reviewer-4",
        )
        assert kept.status == ConflictResolutionStatus.DISMISSED_SCOPED.value
        assert kept.resolution_mode == ConflictResolutionMode.KEEP_BOTH_SCOPED.value
        assert (
            await session.scalar(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
            is None
        )


# ---------------------------------------------------------------------------
# 6. KnowledgeQualityRepository Integration + 5,000 CONTRADICTS Edge Cap Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_quality_repository_contradiction_rate_and_5000_cap(p2a_db, caplog):
    async with p2a_db() as session:
        d1 = await _seed_doc_unit(
            session,
            tag="kq-1",
            text="SLA uptime guarantee is 99.9% for standard enterprise tier.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        d2 = await _seed_doc_unit(
            session,
            tag="kq-2",
            text="SLA uptime guarantee is 99.5% for standard enterprise tier.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        ku1 = KnowledgeUnit(
            id="ku-1",
            document_version_id=str(d1["version_id"]),
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            source_id=str(d1["source_id"]),
            security_partition_id=PART_A,
            ordinal=1,
            text="SLA is 99.9%",
            content_hash=_sha256("ku-1"),
            input_checksum=_sha256("ku-1-in"),
            checksum=_sha256("ku-1-chk"),
            is_current=True,
            provenance_json={"evidence_block_ids": [d1["block"].id]},
            features_json={},
            valid_from=T_JAN,
            valid_to=VALID_TO_FOREVER,
        )
        ku2 = KnowledgeUnit(
            id="ku-2",
            document_version_id=str(d2["version_id"]),
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            source_id=str(d2["source_id"]),
            security_partition_id=PART_A,
            ordinal=1,
            text="SLA is 99.5%",
            content_hash=_sha256("ku-2"),
            input_checksum=_sha256("ku-2-in"),
            checksum=_sha256("ku-2-chk"),
            is_current=True,
            provenance_json={"evidence_block_ids": [d2["block"].id]},
            features_json={},
            valid_from=T_JAN,
            valid_to=VALID_TO_FOREVER,
        )
        ev1 = KnowledgeEvidence(
            id="ev-1",
            unit_id="ku-1",
            tier="T1",
            kind="QUOTE",
            extractor_version="v1",
            confidence=0.95,
            payload_json={"quote": "99.9%", "source_anchor": "p1"},
        )
        ev2 = KnowledgeEvidence(
            id="ev-2",
            unit_id="ku-2",
            tier="T1",
            kind="QUOTE",
            extractor_version="v1",
            confidence=0.95,
            payload_json={"quote": "99.5%", "source_anchor": "p1"},
        )
        session.add_all([ku1, ku2])
        await session.flush()
        session.add_all([ev1, ev2])
        await session.commit()

        cand = ConflictCandidate(
            pair_id="kq-pair",
            pair_key=compute_conflict_pair_key(
                str(d2["version_id"]), str(d2["unit_id"]), str(d1["version_id"]), str(d1["unit_id"])
            ),
            cosine_similarity=0.92,
            new_document_id=str(d2["document_id"]),
            new_document_name="kq-2.md",
            new_version_id=str(d2["version_id"]),
            new_version=d2["ver"],
            new_unit_id=str(d2["unit_id"]),
            new_content_hash=d2["unit"].content_hash,
            new_section_path="SLA",
            new_text=d2["unit"].text_snapshot or "",
            new_security_partition_id=PART_A,
            existing_document_id=str(d1["document_id"]),
            existing_document_name="kq-1.md",
            existing_version_id=str(d1["version_id"]),
            existing_version=d1["ver"],
            existing_unit_id=str(d1["unit_id"]),
            existing_content_hash=d1["unit"].content_hash,
            existing_section_path="SLA",
            existing_text=d1["unit"].text_snapshot or "",
            existing_security_partition_id=PART_A,
        )
        cls_res = await classify_conflict_candidates(
            [cand],
            llm=_FakeLLM(
                lambda _m, _rf: json.dumps(
                    {
                        "items": [
                            {
                                "pair_id": "kq-pair",
                                "classification": "HARD_CONTRADICTION",
                                "confidence": 0.91,
                                "winner": "NONE",
                                "scope_difference_summary": None,
                                "reason": "Conflicting SLA percentages",
                            }
                        ]
                    }
                )
            ),
            project_id=PROJECT_ID,
            tenant_id=TENANT_ID,
        )
        await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=cls_res.decisions,
            now_utc=T_NOW,
        )
        await session.commit()

        row = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.pair_key == cand.pair_key
            )
        )
        assert row is not None

        # 1. PENDING_REVIEW emits ContradictionSignal
        units_by_id = {"ku-1": ku1, "ku-2": ku2}
        ev_by_unit = {"ku-1": [ev1], "ku-2": [ev2]}
        node_members = {"node-1": {"ku-1", "ku-2"}}
        sigs = await _contradiction_signals(
            session,
            project_id=PROJECT_ID,
            partition_id=PART_A,
            units_by_id=units_by_id,
            evidence_by_unit=ev_by_unit,
            node_members=node_members,
        )
        assert len(sigs["node-1"]) == 1

        # 2. After resolve_conflict -> drops to 0
        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id=row.id,
            action="WINNER_B",
            reason="Decided",
            actor="admin",
        )
        sigs_resolved = await _contradiction_signals(
            session,
            project_id=PROJECT_ID,
            partition_id=PART_A,
            units_by_id=units_by_id,
            evidence_by_unit=ev_by_unit,
            node_members=node_members,
        )
        assert len(sigs_resolved["node-1"]) == 0

        # 3. After reopen_conflict -> reappears
        await reopen_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id=row.id,
            reason="Reopen",
            actor="admin",
        )
        sigs_reopened = await _contradiction_signals(
            session,
            project_id=PROJECT_ID,
            partition_id=PART_A,
            units_by_id=units_by_id,
            evidence_by_unit=ev_by_unit,
            node_members=node_members,
        )
        assert len(sigs_reopened["node-1"]) == 1

        # 4. Fill CONTRADICTS edges up to exact MAX_CONTRADICTION_CANDIDATES (5000)
        # Create 101 synthetic SearchUnits (101*100/2 = 5050 >= 4999 distinct pairs -> total = 5000)
        extra_units: list[dict] = []
        for idx in range(101):
            extra_units.append(
                {
                    "id": f"cap-u-{idx}",
                    "document_version_id": str(d1["version_id"]),
                    "block_from_id": d1["block"].id,
                    "block_to_id": d1["block"].id,
                    "security_partition_id": PART_A,
                    "content_hash": _sha256(f"cap-{idx}"),
                    "token_count": 5,
                    "page_from": 1,
                    "page_to": 1,
                    "section_path": "Cap",
                    "text_snapshot": f"Cap text {idx}",
                }
            )
        await session.execute(insert(SearchUnit), extra_units)
        edge_rows: list[dict] = []
        needed = MAX_CONTRADICTION_CANDIDATES - 1  # 1 already exists from kq-pair
        for i in range(101):
            for j in range(i + 1, 101):
                if len(edge_rows) >= needed:
                    break
                edge_rows.append(
                    {
                        "id": f"cap-e-{len(edge_rows)}",
                        "project_id": PROJECT_ID,
                        "source_unit_id": f"cap-u-{i}",
                        "target_unit_id": f"cap-u-{j}",
                        "edge_type": "CONTRADICTS",
                        "weight": 0.8,
                        "calibrated_weight": 0.8,
                        "metadata_json": {"is_candidate": False},
                    }
                )
            if len(edge_rows) >= needed:
                break
        await session.execute(insert(KnowledgeGraphEdge), edge_rows)
        await session.commit()

        # Persist conflict #5001 -> must NOT insert edge #5001, must still persist resolution,
        # and _contradiction_signals MUST NOT raise ValueError!
        d3 = await _seed_doc_unit(
            session,
            tag="kq-3",
            text="SLA uptime guarantee is 98.0% for standard enterprise tier.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        await session.commit()
        cand_5001 = ConflictCandidate(
            pair_id="kq-5001",
            pair_key=compute_conflict_pair_key(
                str(d3["version_id"]), str(d3["unit_id"]), str(d1["version_id"]), str(d1["unit_id"])
            ),
            cosine_similarity=0.90,
            new_document_id=str(d3["document_id"]),
            new_document_name="kq-3.md",
            new_version_id=str(d3["version_id"]),
            new_version=d3["ver"],
            new_unit_id=str(d3["unit_id"]),
            new_content_hash=d3["unit"].content_hash,
            new_section_path="SLA",
            new_text=d3["unit"].text_snapshot or "",
            new_security_partition_id=PART_A,
            existing_document_id=str(d1["document_id"]),
            existing_document_name="kq-1.md",
            existing_version_id=str(d1["version_id"]),
            existing_version=d1["ver"],
            existing_unit_id=str(d1["unit_id"]),
            existing_content_hash=d1["unit"].content_hash,
            existing_section_path="SLA",
            existing_text=d1["unit"].text_snapshot or "",
            existing_security_partition_id=PART_A,
        )
        cls_5001 = await classify_conflict_candidates(
            [cand_5001],
            llm=_FakeLLM(
                lambda _m, _rf: json.dumps(
                    {
                        "items": [
                            {
                                "pair_id": "kq-5001",
                                "classification": "HARD_CONTRADICTION",
                                "confidence": 0.90,
                                "winner": "NONE",
                                "scope_difference_summary": None,
                                "reason": "Overflow check",
                            }
                        ]
                    }
                )
            ),
            project_id=PROJECT_ID,
            tenant_id=TENANT_ID,
        )
        with caplog.at_level(logging.WARNING):
            await persist_conflict_decisions(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                decisions=cls_5001.decisions,
                now_utc=T_NOW,
            )
        await session.commit()

        assert any("contradiction_edge_cap_reached=True" in r.getMessage() for r in caplog.records)
        row_5001 = await session.scalar(
            select(KnowledgeConflictResolution).where(
                KnowledgeConflictResolution.pair_key == cand_5001.pair_key
            )
        )
        assert row_5001 is not None
        assert row_5001.graph_edge_id is None

        # _contradiction_signals still runs cleanly (<= 5000 edges)!
        sigs_at_cap = await _contradiction_signals(
            session,
            project_id=PROJECT_ID,
            partition_id=PART_A,
            units_by_id=units_by_id,
            evidence_by_unit=ev_by_unit,
            node_members=node_members,
        )
        assert len(sigs_at_cap["node-1"]) == 1


# ---------------------------------------------------------------------------
# 7. Worker Failure Isolation (Never Breaks Document.status or SEARCH_READY)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_failure_isolation_never_breaks_search_ready(
    p2a_db, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from sag_api.services import knowledge_conflict_service as kcs

    monkeypatch.setattr(settings, "document_event_extraction_enabled", False)
    doc_file = tmp_path / "iso.md"
    doc_file.write_text("# Policy\n\nAll teams must ship weekly release notes on Friday.\n", encoding="utf-8")

    qdrant_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "ok", "result": []})),
        base_url="http://qdrant.test",
    )
    try:
        async with p2a_db() as session:
            # Seed an existing peer doc so post-SEARCH_READY hook attempts conflict hook
            await _seed_doc_unit(
                session,
                tag="iso-peer",
                text="All teams ship biweekly release notes on Mondays.",
                partition_id=PART_A,
            )
            src = Source(
                id="src-iso",
                name="ISO Source",
                sag_source_config_id="cfg-iso",
                status=SourceStatus.ACTIVE,
            )
            session.add(src)
            await session.flush()
            session.add(
                SourceProjectMapping(
                    id="spm-iso",
                    source_id=src.id,
                    organization_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    state="CONFIRMED",
                    confirmed_at=T_JAN,
                    confirmed_by="admin",
                    approval_ref="appr-iso",
                )
            )
            doc = Document(
                id="doc-iso",
                source_id=src.id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                filename="iso.md",
                storage_path=str(doc_file),
                status=DocumentStatus.PENDING,
                is_active=True,
            )
            session.add(doc)
            await session.flush()
            ver = DocumentVersion(
                id="ver-iso",
                document_id=doc.id,
                version_no=1,
                file_hash=_sha256("iso-v1"),
                valid_from=T_JAN,
                valid_to=VALID_TO_FOREVER,
                status="RECEIVED",
                metadata_json={"security_partition_id": PART_A},
            )
            session.add(ver)
            await session.flush()
            blk = CanonicalBlock(
                id="blk-iso",
                document_version_id=ver.id,
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Release",
                normalized_text="All teams must ship weekly release notes on Friday.",
                content_hash=_sha256("All teams must ship weekly release notes on Friday."),
            )
            session.add(blk)
            run = IngestionRun(
                id="run-iso",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                document_version_id=ver.id,
                idempotency_key="idem-iso",
                payload_hash=ver.file_hash,
                status="QUEUED",
            )
            session.add(run)
            job = Job(
                id="job-iso-proc",
                type=JobType.PROCESS_DOCUMENT,
                status=JobStatus.RUNNING,
                source_id=src.id,
                document_id=doc.id,
                payload={"run_id": run.id, "canonical_checkpoint_stage": "DEDUP_TEMPORAL"},
            )
            session.add(job)
            await session.commit()

            # Force search_index_service.run_search_indexing_stage to mark SEARCH_READY cleanly
            async def _fake_index_stage(sess, *, document_version, **_kwargs):
                unit = SearchUnit(
                    id="unit-iso",
                    document_version_id=document_version.id,
                    block_from_id="blk-iso",
                    block_to_id="blk-iso",
                    security_partition_id=PART_A,
                    content_hash=blk.content_hash,
                    token_count=10,
                    page_from=1,
                    page_to=1,
                    section_path="Release",
                    text_snapshot=blk.normalized_text,
                )
                sess.add(unit)
                document_version.search_status = "SEARCH_READY"
                document_version.search_ready_at = T_NOW
                await sess.flush()
                return [unit]

            from sag_api.services import search_index_service

            monkeypatch.setattr(search_index_service, "run_search_indexing_stage", _fake_index_stage)
            # Case A: explode inside post-SEARCH_READY hook!
            monkeypatch.setattr(
                kcs,
                "reconcile_resolutions_after_version_ready",
                AsyncMock(side_effect=RuntimeError("boom in conflict hook")),
            )

            mgr = SimpleNamespace(
                get_sag_embedding=AsyncMock(return_value=None),
                get_search_unit_qdrant_client=AsyncMock(return_value=qdrant_client),
            )
            await tasks._process_document_unlocked(session, job, engine_manager=mgr, job_queue=None)

            await session.refresh(doc)
            await session.refresh(ver)
            assert doc.status == DocumentStatus.READY
            assert ver.search_status == "SEARCH_READY"

            # Case B: Force exception inside resolve_document_conflicts worker
            assert JobType.CONFLICT_RESOLUTION in tasks.TASK_HANDLERS
            cjob = Job(
                id="job-iso-conflict",
                type=JobType.CONFLICT_RESOLUTION,
                source_id=src.id,
                document_id=doc.id,
                status=JobStatus.RUNNING,
                payload={
                    "tenant_id": TENANT_ID,
                    "project_id": PROJECT_ID,
                    "document_id": doc.id,
                    "document_version_id": ver.id,
                    "security_partition_id": PART_A,
                },
            )
            session.add(cjob)
            await session.commit()

            failing_qdrant = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _r: httpx.Response(503, json={"error": "down"})),
                base_url="http://qdrant.test",
            )
            try:
                with pytest.raises(RuntimeError):
                    await tasks.resolve_document_conflicts(
                        session,
                        cjob,
                        engine_manager=SimpleNamespace(
                            get_search_unit_qdrant_client=AsyncMock(return_value=failing_qdrant)
                        ),
                    )
            finally:
                await failing_qdrant.aclose()

            await session.refresh(doc)
            await session.refresh(ver)
            assert doc.status == DocumentStatus.READY
            assert ver.search_status == "SEARCH_READY"
    finally:
        await qdrant_client.aclose()


# ---------------------------------------------------------------------------
# 8. Worker Lease (JobDeferred) & ACL Re-Verification Inside Lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_lease_and_acl_reverification_before_write(p2a_db):
    async with p2a_db() as session:
        d_old = await _seed_doc_unit(
            session,
            tag="acl-old",
            text="Password rotation period is 90 days across all production servers.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        d_new = await _seed_doc_unit(
            session,
            tag="acl-new",
            text="Password rotation period is 30 days across all production servers.",
            authority_tier=1,
            effective_from=T_JUN,
        )
        await session.commit()

        collection = f"search_units_{PROJECT_ID}"
        new_pid = generate_search_unit_point_id(collection, str(d_new["unit_id"]))
        old_content_hash = str(d_old["unit"].content_hash)

        def _q_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/points"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "id": new_pid,
                                "payload": {"search_unit_id": d_new["unit_id"]},
                                "vector": {"content_vector": [0.5, 0.5, 0.5]},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/points/query/batch"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "points": [
                                    {
                                        "id": "p-old",
                                        "score": 0.90,
                                        "payload": {
                                            "search_unit_id": d_old["unit_id"],
                                            "document_version_id": d_old["version_id"],
                                            "document_id": d_old["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": old_content_hash,
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        cjob = Job(
            id="job-acl-lease",
            type=JobType.CONFLICT_RESOLUTION,
            source_id=str(d_new["source_id"]),
            document_id=str(d_new["document_id"]),
            status=JobStatus.RUNNING,
            payload={
                "tenant_id": TENANT_ID,
                "project_id": PROJECT_ID,
                "document_id": str(d_new["document_id"]),
                "document_version_id": str(d_new["version_id"]),
                "security_partition_id": PART_A,
            },
        )
        session.add(cjob)
        await session.commit()

        def _valid_llm(messages, _rf):
            # Extract pair_id from prompt JSON
            user_txt = messages[-1]["content"]
            parsed_in = json.loads(user_txt.split("PAIRS_JSON:\n", 1)[1].split("\nEND_PAIRS_JSON", 1)[0])
            pid = parsed_in[0]["pair_id"]
            return json.dumps(
                {
                    "items": [
                        {
                            "pair_id": pid,
                            "classification": "DIRECT_SUPERSEDES",
                            "confidence": 0.95,
                            "winner": "NEW",
                            "scope_difference_summary": None,
                            "reason": "Updated security policy",
                        }
                    ]
                }
            )

        # Case A: Exclusive lease held on source -> JobDeferred
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_q_handler), base_url="http://qdrant.test"
        ) as qdrant:
            async with acquire_source_exclusive_lease(
                p2a_db, str(d_new["source_id"]), "octx-transfer:test-lock"
            ):
                with pytest.raises(JobDeferred):
                    await resolve_document_conflicts_service(
                        session,
                        job=cjob,
                        qdrant_client=qdrant,
                        llm=_FakeLLM(_valid_llm),
                        lease_session_factory=p2a_db,
                        now_utc=T_NOW,
                    )

        # Case B: Revoke target SourceProjectMapping DURING in-flight LLM call -> writes 0 rows!
        await session.refresh(cjob)

        async def _revoke_midflight():
            async with p2a_db() as s2:
                spm = await s2.scalar(
                    select(SourceProjectMapping).where(
                        SourceProjectMapping.source_id == str(d_old["source_id"])
                    )
                )
                assert spm is not None
                spm.state = "REVOKED"
                spm.revoked_at = T_NOW
                spm.revoked_by = "admin"
                spm.revocation_ref = "rev-midflight"
                await s2.commit()

        def _midflight_llm(messages, _rf):
            user_txt = messages[-1]["content"]
            parsed_in = json.loads(user_txt.split("PAIRS_JSON:\n", 1)[1].split("\nEND_PAIRS_JSON", 1)[0])
            pid = parsed_in[0]["pair_id"]

            async def _coro():
                await _revoke_midflight()
                return json.dumps(
                    {
                        "items": [
                            {
                                "pair_id": pid,
                                "classification": "DIRECT_SUPERSEDES",
                                "confidence": 0.96,
                                "winner": "NEW",
                                "scope_difference_summary": None,
                                "reason": "Updated policy",
                            }
                        ]
                    }
                )

            return _coro

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_q_handler), base_url="http://qdrant.test"
        ) as qdrant:
            res = await resolve_document_conflicts_service(
                session,
                job=cjob,
                qdrant_client=qdrant,
                llm=_FakeLLM(_midflight_llm),
                lease_session_factory=p2a_db,
                now_utc=T_NOW,
            )
            await session.commit()

        assert res.auto_superseded == 0
        assert res.pending_review == 0
        rows = (await session.scalars(select(KnowledgeConflictResolution))).all()
        assert rows == []


# ---------------------------------------------------------------------------
# 9. Lifecycle Cascade: Delete Document & Delete Source -> Instant Revival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_delete_document_and_delete_source_instant_revival(p2a_db, tmp_path):
    qdrant_deleted_versions: list[list[str]] = []

    def _q_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/delete"):
            body = json.loads(request.content.decode("utf-8"))
            for cond in body.get("filter", {}).get("must", []):
                if cond.get("key") == "document_version_id":
                    val = cond.get("match", {})
                    qdrant_deleted_versions.append(val.get("any") or [val.get("value")])
        return httpx.Response(200, json={"status": "ok", "result": {"status": "completed"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_q_handler), base_url="http://qdrant.test"
    ) as qdrant:
        mgr = SimpleNamespace(
            get_search_unit_qdrant_client=AsyncMock(return_value=qdrant),
            delete_document_data=AsyncMock(),
            release=AsyncMock(),
        )
        async with p2a_db() as session:
            # Scenario 1: delete_document + delete_document_task on winning Doc 2
            doc1 = await _seed_doc_unit(
                session, tag="del-d1", text="Doc 1 policy says budget is 10M.", authority_tier=2, effective_from=T_JAN
            )
            doc2 = await _seed_doc_unit(
                session, tag="del-d2", text="Doc 2 policy says budget is 20M.", authority_tier=1, effective_from=T_JUN
            )
            res12 = KnowledgeConflictResolution(
                id="res-del-12",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                winner_security_partition_id=PART_A,
                pair_key=compute_conflict_pair_key(
                    str(doc2["version_id"]), str(doc2["unit_id"]), str(doc1["version_id"]), str(doc1["unit_id"])
                ),
                winner_document_id=str(doc2["document_id"]),
                winner_version_id=str(doc2["version_id"]),
                winner_unit_id=str(doc2["unit_id"]),
                winner_content_hash=doc2["unit"].content_hash,
                loser_document_id=str(doc1["document_id"]),
                loser_version_id=str(doc1["version_id"]),
                loser_unit_id=str(doc1["unit_id"]),
                loser_content_hash=doc1["unit"].content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
                confidence=0.95,
                effective_valid_to=T_JUN,
            )
            queued_cjob = Job(
                id="cjob-del-d2",
                type=JobType.CONFLICT_RESOLUTION,
                source_id=str(doc2["source_id"]),
                document_id=str(doc2["document_id"]),
                status=JobStatus.QUEUED,
                payload={"document_version_id": str(doc2["version_id"])},
            )
            session.add_all([res12, queued_cjob])
            await session.commit()

            # Before delete: doc1 unit is suppressed!
            sup_before = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[str(doc1["version_id"])],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[PART_A],
                reference_time=T_NOW,
                organization_id=TENANT_ID,
            )
            assert str(doc1["unit_id"]) in sup_before

            # Queue delete_document -> pauses active CONFLICT_RESOLUTION job
            del_job = await document_service.delete_document(
                session, doc2["source"], str(doc2["document_id"])
            )
            await session.refresh(queued_cjob)
            assert queued_cjob.status == JobStatus.PAUSED
            assert (queued_cjob.payload or {}).get("pause_requested") is True

            # Execute delete_document_task -> deletes Doc 2 & resolution, zero Qdrant touches on Doc 1
            await tasks.delete_document_task(session, del_job, engine_manager=mgr)

            assert await session.get(KnowledgeConflictResolution, "res-del-12") is None
            for batch in qdrant_deleted_versions:
                assert str(doc1["version_id"]) not in batch

            sup_after = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[str(doc1["version_id"])],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[PART_A],
                reference_time=T_NOW,
                organization_id=TENANT_ID,
            )
            assert sup_after == {}

            # Scenario 2: delete_source on winning Doc 3's source -> deletes resolution & revives Doc 1
            doc3 = await _seed_doc_unit(
                session, tag="del-d3", text="Doc 3 policy says budget is 30M.", authority_tier=1, effective_from=T_JUN
            )
            res13 = KnowledgeConflictResolution(
                id="res-del-13",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                winner_security_partition_id=PART_A,
                pair_key=compute_conflict_pair_key(
                    str(doc3["version_id"]), str(doc3["unit_id"]), str(doc1["version_id"]), str(doc1["unit_id"])
                ),
                winner_document_id=str(doc3["document_id"]),
                winner_version_id=str(doc3["version_id"]),
                winner_unit_id=str(doc3["unit_id"]),
                winner_content_hash=doc3["unit"].content_hash,
                loser_document_id=str(doc1["document_id"]),
                loser_version_id=str(doc1["version_id"]),
                loser_unit_id=str(doc1["unit_id"]),
                loser_content_hash=doc1["unit"].content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
                confidence=0.95,
                effective_valid_to=T_JUN,
            )
            session.add(res13)
            await session.commit()

            await source_service.delete_source(
                session,
                str(doc3["source_id"]),
                engine_manager=mgr,
                upload_dir=str(tmp_path),
                qdrant_client=qdrant,
            )
            assert await session.get(KnowledgeConflictResolution, "res-del-13") is None
            sup_after_src = await load_superseded_unit_ids_for_scope(
                session,
                loser_version_ids=[str(doc1["version_id"])],
                tenant_ids=[TENANT_ID],
                project_ids=[PROJECT_ID],
                allowed_partitions=[PART_A],
                reference_time=T_NOW,
                organization_id=TENANT_ID,
            )
            assert sup_after_src == {}


# ---------------------------------------------------------------------------
# 10. Lifecycle Cascade: Revoke Document -> REVOKED_WINNER + Edge Sync + Instant Revival
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_revoke_document_transitions_active_to_revoked_winner(p2a_db):
    async with p2a_db() as session:
        doc1 = await _seed_doc_unit(
            session, tag="rev-d1", text="Original travel rule is economy class.", authority_tier=2, effective_from=T_JAN
        )
        doc2 = await _seed_doc_unit(
            session, tag="rev-d2", text="Amended travel rule is business class.", authority_tier=1, effective_from=T_JUN
        )
        doc3 = await _seed_doc_unit(
            session, tag="rev-d3", text="Conflicting draft travel rule.", authority_tier=2, effective_from=T_MAR
        )
        sup_edge = KnowledgeGraphEdge(
            id="edge-rev-sup",
            project_id=PROJECT_ID,
            source_unit_id=str(doc2["unit_id"]),
            target_unit_id=str(doc1["unit_id"]),
            edge_type="SUPERSEDES",
            metadata_json={"is_candidate": False, "resolution_id": "res-rev-win"},
        )
        con_edge = KnowledgeGraphEdge(
            id="edge-rev-con",
            project_id=PROJECT_ID,
            source_unit_id=str(doc3["unit_id"]),
            target_unit_id=str(doc2["unit_id"]),
            edge_type="CONTRADICTS",
            metadata_json={"is_candidate": True, "resolution_id": "res-rev-lose"},
        )
        session.add_all([sup_edge, con_edge])
        await session.flush()

        res_win = KnowledgeConflictResolution(
            id="res-rev-win",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc2["version_id"]), str(doc2["unit_id"]), str(doc1["version_id"]), str(doc1["unit_id"])
            ),
            winner_document_id=str(doc2["document_id"]),
            winner_version_id=str(doc2["version_id"]),
            winner_unit_id=str(doc2["unit_id"]),
            winner_content_hash=doc2["unit"].content_hash,
            loser_document_id=str(doc1["document_id"]),
            loser_version_id=str(doc1["version_id"]),
            loser_unit_id=str(doc1["unit_id"]),
            loser_content_hash=doc1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
            confidence=0.95,
            effective_valid_to=T_JUN,
            graph_edge_id=sup_edge.id,
        )
        res_lose = KnowledgeConflictResolution(
            id="res-rev-lose",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc3["version_id"]), str(doc3["unit_id"]), str(doc2["version_id"]), str(doc2["unit_id"])
            ),
            winner_document_id=str(doc3["document_id"]),
            winner_version_id=str(doc3["version_id"]),
            winner_unit_id=str(doc3["unit_id"]),
            winner_content_hash=doc3["unit"].content_hash,
            loser_document_id=str(doc2["document_id"]),
            loser_version_id=str(doc2["version_id"]),
            loser_unit_id=str(doc2["unit_id"]),
            loser_content_hash=doc2["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.88,
            graph_edge_id=con_edge.id,
        )
        cjob = Job(
            id="cjob-rev-d2",
            type=JobType.CONFLICT_RESOLUTION,
            source_id=str(doc2["source_id"]),
            document_id=str(doc2["document_id"]),
            status=JobStatus.QUEUED,
            payload={"document_version_id": str(doc2["version_id"])},
        )
        session.add_all([res_win, res_lose, cjob])
        await session.commit()

        resp = await document_service.revoke_project_document(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            document_id=str(doc2["document_id"]),
            reason="superseded_by_legal_recall",
        )
        assert resp.status == "REVOKED"

        await session.refresh(res_win)
        await session.refresh(res_lose)
        await session.refresh(con_edge)
        await session.refresh(cjob)

        assert res_win.status == ConflictResolutionStatus.REVOKED_WINNER.value
        assert any(e.get("action") == "WINNER_REVOKED" for e in res_win.audit_history_json)
        assert await session.get(KnowledgeGraphEdge, "edge-rev-sup") is None
        assert res_lose.status == ConflictResolutionStatus.DISMISSED_SCOPED.value
        assert con_edge.metadata_json["is_candidate"] is False
        assert cjob.status == JobStatus.PAUSED
        assert (cjob.payload or {}).get("pause_requested") is True

        # Doc 1 unit instantly revived
        sup = await load_superseded_unit_ids_for_scope(
            session,
            loser_version_ids=[str(doc1["version_id"])],
            tenant_ids=[TENANT_ID],
            project_ids=[PROJECT_ID],
            allowed_partitions=[PART_A],
            reference_time=T_NOW,
            organization_id=TENANT_ID,
        )
        assert sup == {}

        # Reopening a conflict with a revoked winner MUST fail with ConflictError
        with pytest.raises(ConflictError):
            await reopen_conflict(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                allowed_partition_ids={PART_A},
                resolution_id="res-rev-win",
                reason="attempt reopen revoked",
                actor="reviewer-rev",
            )

        # Idempotent enqueue_conflict_resolution_job deduplicates QUEUED/RUNNING per version
        first_job = await enqueue_conflict_resolution_job(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            source_id=str(doc1["source_id"]),
            document_id=str(doc1["document_id"]),
            document_version_id=str(doc1["version_id"]),
            security_partition_id=PART_A,
        )
        dup_job = await enqueue_conflict_resolution_job(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            source_id=str(doc1["source_id"]),
            document_id=str(doc1["document_id"]),
            document_version_id=str(doc1["version_id"]),
            security_partition_id=PART_A,
        )
        assert first_job is not None
        assert dup_job is None


# ---------------------------------------------------------------------------
# 11. Lifecycle Cascade: Reprocess Preserves HITL on Hash Match & Revalidates on Change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_reprocess_preserves_hitl_on_hash_match_and_revalidates_on_change(p2a_db):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "ok"})),
        base_url="http://qdrant.test",
    ) as qdrant:
        mgr = SimpleNamespace(
            get_search_unit_qdrant_client=AsyncMock(return_value=qdrant),
            delete_document_data=AsyncMock(),
        )
        async with p2a_db() as session:
            doc1 = await _seed_doc_unit(
                session, tag="rep-d1", text="Legacy retention is 30 days.", authority_tier=2, effective_from=T_JAN
            )
            doc2 = await _seed_doc_unit(
                session, tag="rep-d2", text="Retention period is 90 days.", authority_tier=1, effective_from=T_JUN
            )
            orig_hash = doc2["unit"].content_hash
            res_hitl = KnowledgeConflictResolution(
                id="res-rep-hitl",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                winner_security_partition_id=PART_A,
                pair_key=compute_conflict_pair_key(
                    str(doc2["version_id"]), str(doc2["unit_id"]), str(doc1["version_id"]), str(doc1["unit_id"])
                ),
                winner_document_id=str(doc2["document_id"]),
                winner_version_id=str(doc2["version_id"]),
                winner_unit_id=str(doc2["unit_id"]),
                winner_content_hash=orig_hash,
                loser_document_id=str(doc1["document_id"]),
                loser_version_id=str(doc1["version_id"]),
                loser_unit_id=str(doc1["unit_id"]),
                loser_content_hash=doc1["unit"].content_hash,
                conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
                status=ConflictResolutionStatus.ACTIVE.value,
                resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
                confidence=1.0,
                effective_valid_to=T_JUN,
            )
            stale_cjob = Job(
                id="cjob-rep-stale",
                type=JobType.CONFLICT_RESOLUTION,
                source_id=str(doc2["source_id"]),
                document_id=str(doc2["document_id"]),
                status=JobStatus.QUEUED,
                payload={"document_version_id": str(doc2["version_id"])},
            )
            reproc_job = Job(
                id="job-reproc-d2",
                type=JobType.REPROCESS_DOCUMENT,
                source_id=str(doc2["source_id"]),
                document_id=str(doc2["document_id"]),
                status=JobStatus.RUNNING,
                payload={"target_document_id": str(doc2["document_id"])},
            )
            session.add_all([res_hitl, stale_cjob, reproc_job])
            await session.commit()

            # Execute reprocess cleanup (executes DELETE FROM search_units) -> HITL row survives!
            await tasks.reprocess_document_task(session, reproc_job, engine_manager=mgr)

            await session.refresh(res_hitl)
            await session.refresh(stale_cjob)
            assert stale_cjob.status == JobStatus.PAUSED
            assert (stale_cjob.payload or {}).get("pause_requested") is True
            assert res_hitl.status == ConflictResolutionStatus.ACTIVE.value
            assert res_hitl.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value

            # Simulate re-indexing producing a NEW unit ID with IDENTICAL content_hash
            ver2 = await session.get(DocumentVersion, str(doc2["version_id"]))
            assert ver2 is not None
            new_blk = CanonicalBlock(
                id="blk-rep-d2-new",
                document_version_id=ver2.id,
                ordinal=1,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Policy > Quota",
                normalized_text="Retention period is 90 days.",
                content_hash=orig_hash,
            )
            session.add(new_blk)
            await session.flush()
            new_unit = SearchUnit(
                id="unit-rep-d2-remapped",
                document_version_id=ver2.id,
                block_from_id=new_blk.id,
                block_to_id=new_blk.id,
                security_partition_id=PART_A,
                content_hash=orig_hash,
                token_count=6,
                page_from=1,
                page_to=1,
                section_path="Policy > Quota",
                text_snapshot="Retention period is 90 days.",
            )
            ver2.status = "READY"
            ver2.search_status = "SEARCH_READY"
            ver2.search_ready_at = T_NOW
            session.add(new_unit)
            await session.flush()

            rec_same = await reconcile_resolutions_after_version_ready(
                session, new_version=ver2, now_utc=T_NOW
            )
            await session.commit()
            await session.refresh(res_hitl)

            assert rec_same.carried_forward_count == 1
            assert rec_same.needs_revalidation_count == 0
            assert res_hitl.winner_unit_id == "unit-rep-d2-remapped"
            assert res_hitl.status == ConflictResolutionStatus.ACTIVE.value
            assert res_hitl.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value
            assert res_hitl.effective_valid_to == T_JUN

            # Now simulate edited clause (changed content_hash) -> NEEDS_REVALIDATION
            new_unit.content_hash = _sha256("Retention period is now 365 days.")
            new_unit.text_snapshot = "Retention period is now 365 days."
            await session.flush()

            rec_changed = await reconcile_resolutions_after_version_ready(
                session, new_version=ver2, now_utc=T_NOW
            )
            await session.commit()
            await session.refresh(res_hitl)

            assert rec_changed.needs_revalidation_count == 1
            assert res_hitl.status == ConflictResolutionStatus.NEEDS_REVALIDATION.value
            assert (
                await session.scalar(
                    select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
                )
                is None
            )


# ---------------------------------------------------------------------------
# 12. Lifecycle Cascade: Doc 2 v2 Carry-Forward, v3 Retraction, Out-of-Order v0, & Metadata PATCH Re-Eval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_doc2_v2_carry_forward_retraction_out_of_order_and_metadata_patch(p2a_db):
    async with p2a_db() as session:
        doc1 = await _seed_doc_unit(
            session, tag="cf-d1", text="Max reimbursement is 5M VND.", authority_tier=2, effective_from=T_JAN
        )
        doc2_v1 = await _seed_doc_unit(
            session,
            tag="cf-d2-v1",
            document_id="doc-cf-2",
            version_no=1,
            text="Max reimbursement is 8M VND.",
            authority_tier=1,
            effective_from=T_MAR,
            valid_from=T_MAR,
            valid_to=T_JUN,
        )
        clause_hash = doc2_v1["unit"].content_hash
        res_hitl = KnowledgeConflictResolution(
            id="res-cf-hitl",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc2_v1["version_id"]), str(doc2_v1["unit_id"]), str(doc1["version_id"]), str(doc1["unit_id"])
            ),
            winner_document_id="doc-cf-2",
            winner_version_id=str(doc2_v1["version_id"]),
            winner_unit_id=str(doc2_v1["unit_id"]),
            winner_content_hash=clause_hash,
            loser_document_id=str(doc1["document_id"]),
            loser_version_id=str(doc1["version_id"]),
            loser_unit_id=str(doc1["unit_id"]),
            loser_content_hash=doc1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
            confidence=1.0,
            effective_valid_to=T_MAR,
        )
        session.add(res_hitl)
        await session.commit()

        # 1. Doc 2 v2 keeps the same clause -> carry forward seamlessly, preserve MANUAL_HITL & effective_valid_to!
        doc2_v2 = await _seed_doc_unit(
            session,
            tag="cf-d2-v2",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-cf-2",
            version_no=2,
            text="Max reimbursement is 8M VND.",
            authority_tier=1,
            effective_from=T_JUN,
            valid_from=T_JUN,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v1["version_id"]),
        )
        rec_v2 = await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v2["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res_hitl)

        assert rec_v2.carried_forward_count == 1
        assert res_hitl.winner_version_id == doc2_v2["version_id"]
        assert res_hitl.winner_unit_id == doc2_v2["unit_id"]
        assert res_hitl.status == ConflictResolutionStatus.ACTIVE.value
        assert res_hitl.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value
        assert res_hitl.effective_valid_to == T_MAR

        # 2. Out-of-order back-dated Doc 2 v0 (valid_to < now) MUST NOT steal v2's active resolution!
        doc2_v0 = await _seed_doc_unit(
            session,
            tag="cf-d2-v0",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-cf-2",
            version_no=0,
            text="Historical old clause 2M VND.",
            authority_tier=1,
            effective_from=T_JAN,
            valid_from=T_JAN,
            valid_to=T_MAR,
        )
        rec_v0 = await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v0["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res_hitl)
        assert rec_v0.carried_forward_count == 0
        assert rec_v0.needs_revalidation_count == 0
        assert res_hitl.winner_version_id == doc2_v2["version_id"]
        assert res_hitl.status == ConflictResolutionStatus.ACTIVE.value

        # 3. Doc 2 v3 retracts/edits clause -> ACTIVE -> NEEDS_REVALIDATION (reviving Doc 1!)
        doc2_v2["ver"].valid_to = T_SEP
        doc2_v3 = await _seed_doc_unit(
            session,
            tag="cf-d2-v3",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-cf-2",
            version_no=3,
            text="Reimbursement clause removed completely; refer to general policy.",
            authority_tier=1,
            effective_from=T_SEP,
            valid_from=T_SEP,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v2["version_id"]),
        )
        rec_v3 = await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v3["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res_hitl)
        assert rec_v3.needs_revalidation_count == 1
        assert res_hitl.status == ConflictResolutionStatus.NEEDS_REVALIDATION.value

        # 4. Metadata PATCH re-evaluation:
        #    AUTO_POLICY failing precedence -> NEEDS_REVALIDATION + enqueues CONFLICT_RESOLUTION;
        #    MANUAL_HITL stays ACTIVE untouched!
        doc_loser_a = await _seed_doc_unit(
            session, tag="mp-la", text="Policy A.", authority_tier=2, effective_from=T_JAN
        )
        doc_loser_b = await _seed_doc_unit(
            session, tag="mp-lb", text="Policy B.", authority_tier=2, effective_from=T_JAN
        )
        doc_winner = await _seed_doc_unit(
            session, tag="mp-w", text="Winner Policy.", authority_tier=1, effective_from=T_JUN
        )
        res_auto = KnowledgeConflictResolution(
            id="res-mp-auto",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc_winner["version_id"]),
                str(doc_winner["unit_id"]),
                str(doc_loser_a["version_id"]),
                str(doc_loser_a["unit_id"]),
            ),
            winner_document_id=str(doc_winner["document_id"]),
            winner_version_id=str(doc_winner["version_id"]),
            winner_unit_id=str(doc_winner["unit_id"]),
            winner_content_hash=doc_winner["unit"].content_hash,
            loser_document_id=str(doc_loser_a["document_id"]),
            loser_version_id=str(doc_loser_a["version_id"]),
            loser_unit_id=str(doc_loser_a["unit_id"]),
            loser_content_hash=doc_loser_a["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
            confidence=0.95,
            effective_valid_to=T_JUN,
        )
        res_manual = KnowledgeConflictResolution(
            id="res-mp-manual",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc_winner["version_id"]),
                str(doc_winner["unit_id"]),
                str(doc_loser_b["version_id"]),
                str(doc_loser_b["unit_id"]),
            ),
            winner_document_id=str(doc_winner["document_id"]),
            winner_version_id=str(doc_winner["version_id"]),
            winner_unit_id=str(doc_winner["unit_id"]),
            winner_content_hash=doc_winner["unit"].content_hash,
            loser_document_id=str(doc_loser_b["document_id"]),
            loser_version_id=str(doc_loser_b["version_id"]),
            loser_unit_id=str(doc_loser_b["unit_id"]),
            loser_content_hash=doc_loser_b["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
            confidence=1.0,
            effective_valid_to=T_JUN,
        )
        session.add_all([res_auto, res_manual])
        await session.commit()

        fake_queue = SimpleNamespace(enqueue=AsyncMock())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "ok"})),
            base_url="http://qdrant.test",
        ) as qdrant:
            await document_service.update_document_version_metadata(
                session,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                document_id=str(doc_winner["document_id"]),
                allowed_partition_ids={PART_A},
                authority_tier=3,  # Downgrade Tier 1 -> Tier 3 (now weaker than loser Tier 2!)
                qdrant_client=qdrant,
                job_queue=fake_queue,
            )

        await session.refresh(res_auto)
        await session.refresh(res_manual)
        assert res_auto.status == ConflictResolutionStatus.NEEDS_REVALIDATION.value
        assert res_manual.status == ConflictResolutionStatus.ACTIVE.value
        assert res_manual.resolution_mode == ConflictResolutionMode.MANUAL_HITL.value

        cjobs = (
            await session.scalars(
                select(Job).where(
                    Job.document_id == str(doc_winner["document_id"]),
                    Job.type == JobType.CONFLICT_RESOLUTION,
                )
            )
        ).all()
        assert len(cjobs) == 1
        fake_queue.enqueue.assert_awaited_once_with(cjobs[0].id)


# ---------------------------------------------------------------------------
# 13. Triad Round 2 Hardening Regressions (Fixes 3, 4, 5, 8, 9, 11, 12a)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_document_conflicts_commits_inside_source_processing_lease(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Fix 3: Phase D conflict writes MUST commit BEFORE acquire_source_processing_lease releases."""
    from sag_api.db.models import OctxOperationLease

    # Use a file-backed SQLite DB so concurrent sessions (worker, lease, spy) use distinct
    # connections rather than StaticPool's single shared :memory: connection.
    db_file = tmp_path / "lease_commit_order.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    _enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    try:
        async with factory() as session:
            d_old = await _seed_doc_unit(
                session,
                tag="lease-c-old",
                text="Backup retention window is 14 days for all staging clusters.",
                authority_tier=2,
                effective_from=T_JAN,
            )
            d_new = await _seed_doc_unit(
                session,
                tag="lease-c-new",
                text="Backup retention window is 30 days for all staging clusters.",
                authority_tier=1,
                effective_from=T_JUN,
            )
            cjob = Job(
                id="job-lease-commit-order",
                type=JobType.CONFLICT_RESOLUTION,
                source_id=str(d_new["source_id"]),
                document_id=str(d_new["document_id"]),
                status=JobStatus.RUNNING,
                payload={
                    "tenant_id": TENANT_ID,
                    "project_id": PROJECT_ID,
                    "document_id": str(d_new["document_id"]),
                    "document_version_id": str(d_new["version_id"]),
                    "security_partition_id": PART_A,
                },
            )
            session.add(cjob)
            await session.commit()

            collection = f"search_units_{PROJECT_ID}"
            new_pid = generate_search_unit_point_id(collection, str(d_new["unit_id"]))
            old_hash = str(d_old["unit"].content_hash)

            def _q_handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/points"):
                    return httpx.Response(
                        200,
                        json={
                            "result": [
                                {
                                    "id": new_pid,
                                    "payload": {"search_unit_id": d_new["unit_id"]},
                                    "vector": {"content_vector": [0.7, 0.7, 0.0]},
                                }
                            ]
                        },
                    )
                if request.url.path.endswith("/points/query/batch"):
                    return httpx.Response(
                        200,
                        json={
                            "result": [
                                {
                                    "points": [
                                        {
                                            "id": "p-lease-old",
                                            "score": 0.91,
                                            "payload": {
                                                "search_unit_id": d_old["unit_id"],
                                                "document_version_id": d_old["version_id"],
                                                "document_id": d_old["document_id"],
                                                "security_partition_id": PART_A,
                                                "content_hash": old_hash,
                                            },
                                        }
                                    ]
                                }
                            ]
                        },
                    )
                return httpx.Response(404)

            def _llm_reply(messages, _rf):
                user_txt = messages[-1]["content"]
                parsed_in = json.loads(
                    user_txt.split("PAIRS_JSON:\n", 1)[1].split("\nEND_PAIRS_JSON", 1)[0]
                )
                return json.dumps(
                    {
                        "items": [
                            {
                                "pair_id": parsed_in[0]["pair_id"],
                                "classification": "DIRECT_SUPERSEDES",
                                "confidence": 0.96,
                                "winner": "NEW",
                                "scope_difference_summary": None,
                                "reason": "Higher authority supersedes older backup retention rule",
                            }
                        ]
                    }
                )

            expected_lease_key = f"source:{d_new['source_id']}:processor:{cjob.id}"
            lease_active_at_commit: list[bool] = []
            orig_commit = session.commit

            async def _spied_commit():
                async with factory() as check_s:
                    lease_row = await check_s.scalar(
                        select(OctxOperationLease).where(
                            OctxOperationLease.resource_key == expected_lease_key
                        )
                    )
                    lease_active_at_commit.append(lease_row is not None)
                await orig_commit()

            session.commit = _spied_commit  # type: ignore[method-assign]
            try:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_q_handler), base_url="http://qdrant.test"
                ) as qdrant:
                    result = await resolve_document_conflicts_service(
                        session,
                        job=cjob,
                        qdrant_client=qdrant,
                        llm=_FakeLLM(_llm_reply),
                        lease_session_factory=factory,
                        now_utc=T_NOW,
                    )
            finally:
                session.commit = orig_commit  # type: ignore[method-assign]

            assert result.auto_superseded == 1
            # Commit MUST have executed WHILE the processing lease was active!
            assert lease_active_at_commit == [True]

        # After return, lease is cleanly released AND conflict row + job progress are ALREADY committed!
        async with factory() as verify_s:
            remaining_lease = await verify_s.scalar(
                select(OctxOperationLease).where(
                    OctxOperationLease.resource_key == expected_lease_key
                )
            )
            assert remaining_lease is None
            persisted_rows = (await verify_s.scalars(select(KnowledgeConflictResolution))).all()
            assert len(persisted_rows) == 1
            assert persisted_rows[0].status == ConflictResolutionStatus.ACTIVE.value
            persisted_job = await verify_s.get(Job, cjob.id)
            assert persisted_job is not None
            assert persisted_job.progress == 1.0
            assert (persisted_job.payload or {}).get("auto_superseded") == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_carry_forward_v1_v2_v3_and_pending_winner_b_leave_zero_orphaned_supersedes_edges(
    p2a_db,
):
    """Fix 4: Carry-forward v1 -> v2 -> v3 -> retract AND PENDING_REVIEW -> WINNER_B -> v2 -> reopen
    MUST clean up prior SUPERSEDES edges and keep graph_edge_id bound to the live edge."""
    async with p2a_db() as session:
        # Scenario A: v1 -> v2 -> v3 -> retract
        doc1 = await _seed_doc_unit(
            session, tag="edge-d1", text="Legacy quota is 10 GB.", authority_tier=2, effective_from=T_JAN
        )
        doc2_v1 = await _seed_doc_unit(
            session,
            tag="edge-d2-v1",
            document_id="doc-edge-2",
            version_no=1,
            text="Updated quota is 50 GB.",
            authority_tier=1,
            effective_from=T_MAR,
            valid_from=T_MAR,
            valid_to=T_JUN,
        )
        e1 = KnowledgeGraphEdge(
            id="edge-sup-v1-init",
            project_id=PROJECT_ID,
            source_unit_id=str(doc2_v1["unit_id"]),
            target_unit_id=str(doc1["unit_id"]),
            edge_type="SUPERSEDES",
            metadata_json={"is_candidate": False, "resolution_id": "res-edge-cf"},
        )
        session.add(e1)
        await session.flush()

        res = KnowledgeConflictResolution(
            id="res-edge-cf",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc2_v1["version_id"]),
                str(doc2_v1["unit_id"]),
                str(doc1["version_id"]),
                str(doc1["unit_id"]),
            ),
            winner_document_id="doc-edge-2",
            winner_version_id=str(doc2_v1["version_id"]),
            winner_unit_id=str(doc2_v1["unit_id"]),
            winner_content_hash=doc2_v1["unit"].content_hash,
            loser_document_id=str(doc1["document_id"]),
            loser_version_id=str(doc1["version_id"]),
            loser_unit_id=str(doc1["unit_id"]),
            loser_content_hash=doc1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
            confidence=1.0,
            effective_valid_to=T_MAR,
            graph_edge_id=e1.id,
        )
        session.add(res)
        await session.commit()

        # v1 -> v2 carry-forward
        doc2_v2 = await _seed_doc_unit(
            session,
            tag="edge-d2-v2",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-edge-2",
            version_no=2,
            text="Updated quota is 50 GB.",
            authority_tier=1,
            effective_from=T_JUN,
            valid_from=T_JUN,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v1["version_id"]),
        )
        await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v2["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res)
        sup_v2 = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert len(sup_v2) == 1
        assert sup_v2[0].source_unit_id == str(doc2_v2["unit_id"])
        assert sup_v2[0].target_unit_id == str(doc1["unit_id"])
        assert res.graph_edge_id == sup_v2[0].id
        assert await session.get(KnowledgeGraphEdge, "edge-sup-v1-init") is None

        # v2 -> v3 carry-forward
        doc2_v2["ver"].valid_to = T_SEP
        doc2_v3 = await _seed_doc_unit(
            session,
            tag="edge-d2-v3",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-edge-2",
            version_no=3,
            text="Updated quota is 50 GB.",
            authority_tier=1,
            effective_from=T_SEP,
            valid_from=T_SEP,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v2["version_id"]),
        )
        await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v3["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res)
        sup_v3 = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert len(sup_v3) == 1
        assert sup_v3[0].source_unit_id == str(doc2_v3["unit_id"])
        assert res.graph_edge_id == sup_v3[0].id

        # v3 -> v4 retraction -> 0 SUPERSEDES edges remain!
        doc2_v3["ver"].valid_to = T_NOW
        doc2_v4 = await _seed_doc_unit(
            session,
            tag="edge-d2-v4",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-edge-2",
            version_no=4,
            text="Quota clause completely retracted.",
            authority_tier=1,
            effective_from=T_NOW,
            valid_from=T_NOW,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v3["version_id"]),
        )
        await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v4["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res)
        assert res.status == ConflictResolutionStatus.NEEDS_REVALIDATION.value
        assert res.graph_edge_id is None
        sup_retracted = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert sup_retracted == []

        # Scenario B: PENDING_REVIEW (with CONTRADICTS C1) -> WINNER_B -> v2 carry-forward -> reopen
        doc_b1 = await _seed_doc_unit(
            session, tag="edge-b1", text="Default TTL is 1h.", authority_tier=2, effective_from=T_JAN
        )
        doc_b2_v1 = await _seed_doc_unit(
            session,
            tag="edge-b2-v1",
            document_id="doc-edge-b2",
            version_no=1,
            text="Default TTL is 4h.",
            authority_tier=2,
            effective_from=T_MAR,
            valid_from=T_MAR,
            valid_to=T_JUN,
        )
        c1 = KnowledgeGraphEdge(
            id="edge-con-b1",
            project_id=PROJECT_ID,
            source_unit_id=str(doc_b2_v1["unit_id"]),
            target_unit_id=str(doc_b1["unit_id"]),
            edge_type="CONTRADICTS",
            metadata_json={"is_candidate": True, "resolution_id": "res-edge-b"},
        )
        session.add(c1)
        await session.flush()
        res_b = KnowledgeConflictResolution(
            id="res-edge-b",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc_b2_v1["version_id"]),
                str(doc_b2_v1["unit_id"]),
                str(doc_b1["version_id"]),
                str(doc_b1["unit_id"]),
            ),
            winner_document_id="doc-edge-b2",
            winner_version_id=str(doc_b2_v1["version_id"]),
            winner_unit_id=str(doc_b2_v1["unit_id"]),
            winner_content_hash=doc_b2_v1["unit"].content_hash,
            loser_document_id=str(doc_b1["document_id"]),
            loser_version_id=str(doc_b1["version_id"]),
            loser_unit_id=str(doc_b1["unit_id"]),
            loser_content_hash=doc_b1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.88,
            graph_edge_id=c1.id,
        )
        session.add(res_b)
        await session.commit()

        await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-edge-b",
            action="WINNER_B",
            reason="Confirm 4h TTL",
            actor="reviewer-b",
        )
        doc_b2_v2 = await _seed_doc_unit(
            session,
            tag="edge-b2-v2",
            source_id=str(doc_b2_v1["source_id"]),
            document_id="doc-edge-b2",
            version_no=2,
            text="Default TTL is 4h.",
            authority_tier=2,
            effective_from=T_JUN,
            valid_from=T_JUN,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc_b2_v1["version_id"]),
        )
        await reconcile_resolutions_after_version_ready(
            session, new_version=doc_b2_v2["ver"], now_utc=T_NOW
        )
        await session.commit()
        await session.refresh(res_b)
        sup_b_v2 = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert len(sup_b_v2) == 1
        assert sup_b_v2[0].source_unit_id == str(doc_b2_v2["unit_id"])
        assert res_b.graph_edge_id == sup_b_v2[0].id

        await reopen_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-edge-b",
            reason="Re-evaluate",
            actor="reviewer-b",
        )
        sup_b_reopen = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "SUPERSEDES")
            )
        ).all()
        assert sup_b_reopen == []


@pytest.mark.asyncio
async def test_human_hitl_keep_both_scoped_protected_from_automated_overwrite(p2a_db):
    """Fix 5: Human-resolved KEEP_BOTH_SCOPED decisions MUST be locked identically to
    MANUAL_HITL across discovery, persist_conflict_decisions, and pair-key collision."""
    from sag_api.services.knowledge_conflict_service import (
        ClassifiedConflictDecision,
        _handle_pair_key_collision,
    )

    async with p2a_db() as session:
        doc_a = await _seed_doc_unit(
            session,
            tag="kbs-a",
            text="EMEA travel stipend is 100 EUR per day for all team members.",
            authority_tier=2,
            effective_from=T_JAN,
        )
        doc_b = await _seed_doc_unit(
            session,
            tag="kbs-b",
            text="APAC travel stipend is 120 USD per day for all team members.",
            authority_tier=1,
            effective_from=T_JUN,
        )
        pk = compute_conflict_pair_key(
            str(doc_b["version_id"]),
            str(doc_b["unit_id"]),
            str(doc_a["version_id"]),
            str(doc_a["unit_id"]),
        )
        row = KnowledgeConflictResolution(
            id="res-kbs-human",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=pk,
            winner_document_id=str(doc_b["document_id"]),
            winner_version_id=str(doc_b["version_id"]),
            winner_unit_id=str(doc_b["unit_id"]),
            winner_content_hash=doc_b["unit"].content_hash,
            loser_document_id=str(doc_a["document_id"]),
            loser_version_id=str(doc_a["version_id"]),
            loser_unit_id=str(doc_a["unit_id"]),
            loser_content_hash=doc_a["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.89,
            evidence_json={"winner_authority_tier": 2, "loser_authority_tier": 2},
        )
        session.add(row)
        await session.commit()

        resolved = await resolve_conflict(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            resolution_id="res-kbs-human",
            action="KEEP_BOTH_SCOPED",
            reason="Distinct regions EMEA vs APAC",
            actor="human-reviewer-42",
        )
        assert resolved.status == ConflictResolutionStatus.DISMISSED_SCOPED.value
        assert resolved.resolution_mode == ConflictResolutionMode.KEEP_BOTH_SCOPED.value
        assert resolved.resolved_by == "human-reviewer-42"

        # 1. Stage 2A discovery skips the pair even after metadata snapshot drift!
        doc_b["ver"].authority_tier = 1
        await session.commit()
        collection = f"search_units_{PROJECT_ID}"
        new_pid = generate_search_unit_point_id(collection, str(doc_b["unit_id"]))
        old_hash = str(doc_a["unit"].content_hash)

        def _q_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/points"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "id": new_pid,
                                "payload": {"search_unit_id": doc_b["unit_id"]},
                                "vector": {"content_vector": [1.0, 0.0]},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/points/query/batch"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "points": [
                                    {
                                        "id": "p-kbs-a",
                                        "score": 0.92,
                                        "payload": {
                                            "search_unit_id": doc_a["unit_id"],
                                            "document_version_id": doc_a["version_id"],
                                            "document_id": doc_a["document_id"],
                                            "security_partition_id": PART_A,
                                            "content_hash": old_hash,
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_q_handler), base_url="http://qdrant.test"
        ) as qdrant:
            cands = await discover_conflict_candidates(
                session,
                qdrant_client=qdrant,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                new_document=doc_b["doc"],
                new_version=doc_b["ver"],
                now_utc=T_NOW,
            )
        assert cands == []

        # 2. persist_conflict_decisions MUST NOT overwrite human KEEP_BOTH_SCOPED!
        cand = ConflictCandidate(
            pair_id="kbs-overwrite-attempt",
            pair_key=pk,
            cosine_similarity=0.93,
            new_document_id=str(doc_b["document_id"]),
            new_document_name="kbs-b.md",
            new_version_id=str(doc_b["version_id"]),
            new_version=doc_b["ver"],
            new_unit_id=str(doc_b["unit_id"]),
            new_content_hash=doc_b["unit"].content_hash,
            new_section_path="Travel",
            new_text=doc_b["unit"].text_snapshot or "",
            new_security_partition_id=PART_A,
            existing_document_id=str(doc_a["document_id"]),
            existing_document_name="kbs-a.md",
            existing_version_id=str(doc_a["version_id"]),
            existing_version=doc_a["ver"],
            existing_unit_id=str(doc_a["unit_id"]),
            existing_content_hash=doc_a["unit"].content_hash,
            existing_section_path="Travel",
            existing_text=doc_a["unit"].text_snapshot or "",
            existing_security_partition_id=PART_A,
        )
        decision = ClassifiedConflictDecision(
            candidate=cand,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES,
            confidence=0.99,
            winner="NEW",
            scope_difference_summary=None,
            reason="Automated overwrite attempt",
        )
        persist_res = await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=[decision],
            now_utc=T_NOW,
        )
        await session.commit()
        await session.refresh(resolved)
        assert persist_res.skipped_manual_hitl == 1
        assert resolved.status == ConflictResolutionStatus.DISMISSED_SCOPED.value
        assert resolved.resolution_mode == ConflictResolutionMode.KEEP_BOTH_SCOPED.value
        assert resolved.resolved_by == "human-reviewer-42"

        # 3. _handle_pair_key_collision protects human KEEP_BOTH_SCOPED against AUTO_POLICY remap
        doc_c = await _seed_doc_unit(
            session, tag="kbs-c", text="Other policy text for collision test.", authority_tier=1, effective_from=T_JUN
        )
        auto_row = KnowledgeConflictResolution(
            id="res-kbs-auto-incoming",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc_c["version_id"]), str(doc_c["unit_id"]), str(doc_a["version_id"]), str(doc_a["unit_id"])
            ),
            winner_document_id=str(doc_c["document_id"]),
            winner_version_id=str(doc_c["version_id"]),
            winner_unit_id=str(doc_c["unit_id"]),
            winner_content_hash=doc_c["unit"].content_hash,
            loser_document_id=str(doc_a["document_id"]),
            loser_version_id=str(doc_a["version_id"]),
            loser_unit_id=str(doc_a["unit_id"]),
            loser_content_hash=doc_a["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.AUTO_POLICY.value,
            confidence=0.95,
            resolved_by="system:auto_policy",
        )
        session.add(auto_row)
        await session.flush()

        can_update = await _handle_pair_key_collision(
            session, row=auto_row, new_pair_key=pk, now_utc=T_NOW
        )
        assert can_update is False
        assert auto_row.status == ConflictResolutionStatus.SUPERSEDED_BY_UPDATE.value
        assert await session.get(KnowledgeConflictResolution, "res-kbs-human") is not None


@pytest.mark.asyncio
async def test_blocked_document_statuses_reject_paused_and_failed_documents(p2a_db):
    """Fix 8: _BLOCKED_DOCUMENT_STATUSES in knowledge_conflict_service blocks PAUSED/FAILED docs."""
    from sag_api.services.knowledge_conflict_service import has_active_cross_document_peer

    async with p2a_db() as session:
        doc_ready = await _seed_doc_unit(
            session,
            tag="blk-ready",
            text="Ready active document rule for status checks.",
            doc_status=DocumentStatus.READY,
        )
        doc_paused = await _seed_doc_unit(
            session,
            tag="blk-paused",
            text="Paused document rule that must be ignored.",
            doc_status=DocumentStatus.PAUSED,
        )
        doc_failed = await _seed_doc_unit(
            session,
            tag="blk-failed",
            text="Failed document rule that must be ignored.",
            doc_status=DocumentStatus.FAILED,
        )
        await session.commit()

        # Peer check ignores PAUSED and FAILED docs
        has_peer = await has_active_cross_document_peer(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            exclude_document_id=str(doc_ready["document_id"]),
            now_utc=T_NOW,
        )
        assert has_peer is False

        # Conflict involving PAUSED or FAILED doc is hidden from default list and blocked on resolve/reopen
        for tag, bad_doc in (("paused", doc_paused), ("failed", doc_failed)):
            row = KnowledgeConflictResolution(
                id=f"res-blk-{tag}",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                security_partition_id=PART_A,
                winner_security_partition_id=PART_A,
                pair_key=compute_conflict_pair_key(
                    str(bad_doc["version_id"]),
                    str(bad_doc["unit_id"]),
                    str(doc_ready["version_id"]),
                    str(doc_ready["unit_id"]),
                ),
                winner_document_id=str(bad_doc["document_id"]),
                winner_version_id=str(bad_doc["version_id"]),
                winner_unit_id=str(bad_doc["unit_id"]),
                winner_content_hash=bad_doc["unit"].content_hash,
                loser_document_id=str(doc_ready["document_id"]),
                loser_version_id=str(doc_ready["version_id"]),
                loser_unit_id=str(doc_ready["unit_id"]),
                loser_content_hash=doc_ready["unit"].content_hash,
                conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
                status=ConflictResolutionStatus.PENDING_REVIEW.value,
                resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
                confidence=0.85,
            )
            session.add(row)
        await session.commit()

        items, total = await list_project_conflicts(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            allowed_partition_ids={PART_A},
            include_inactive=False,
        )
        assert items == []
        assert total == 0

        for res_id in ("res-blk-paused", "res-blk-failed"):
            with pytest.raises(ConflictError):
                await resolve_conflict(
                    session,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    allowed_partition_ids={PART_A},
                    resolution_id=res_id,
                    action="WINNER_B",
                    reason="cannot win with paused/failed doc",
                    actor="reviewer-blk",
                )
            with pytest.raises(ConflictError):
                await reopen_conflict(
                    session,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    allowed_partition_ids={PART_A},
                    resolution_id=res_id,
                    reason="cannot reopen paused/failed doc",
                    actor="reviewer-blk",
                )


@pytest.mark.asyncio
async def test_pair_key_collision_deletes_owned_contradicts_edge_and_cleans_quality(p2a_db):
    """Fix 9: Deleting a colliding row in _handle_pair_key_collision removes its owned
    CONTRADICTS edge so neither KnowledgeQuality nor the 5,000 edge cap is polluted."""
    async with p2a_db() as session:
        doc1 = await _seed_doc_unit(
            session, tag="col-d1", text="Original expense cap is 2M VND.", authority_tier=2, effective_from=T_JAN
        )
        doc2_v1 = await _seed_doc_unit(
            session,
            tag="col-d2-v1",
            document_id="doc-col-2",
            version_no=1,
            text="Expense cap is 5M VND.",
            authority_tier=1,
            effective_from=T_MAR,
            valid_from=T_MAR,
            valid_to=T_JUN,
        )
        doc2_v2 = await _seed_doc_unit(
            session,
            tag="col-d2-v2",
            source_id=str(doc2_v1["source_id"]),
            document_id="doc-col-2",
            version_no=2,
            text="Expense cap is 5M VND.",
            authority_tier=1,
            effective_from=T_JUN,
            valid_from=T_JUN,
            valid_to=VALID_TO_FOREVER,
            supersedes_id=str(doc2_v1["version_id"]),
        )
        # Seed MANUAL_HITL resolution on v1
        res_hitl = KnowledgeConflictResolution(
            id="res-col-hitl",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc2_v1["version_id"]),
                str(doc2_v1["unit_id"]),
                str(doc1["version_id"]),
                str(doc1["unit_id"]),
            ),
            winner_document_id="doc-col-2",
            winner_version_id=str(doc2_v1["version_id"]),
            winner_unit_id=str(doc2_v1["unit_id"]),
            winner_content_hash=doc2_v1["unit"].content_hash,
            loser_document_id=str(doc1["document_id"]),
            loser_version_id=str(doc1["version_id"]),
            loser_unit_id=str(doc1["unit_id"]),
            loser_content_hash=doc1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES.value,
            status=ConflictResolutionStatus.ACTIVE.value,
            resolution_mode=ConflictResolutionMode.MANUAL_HITL.value,
            confidence=1.0,
            effective_valid_to=T_MAR,
        )
        # Seed colliding automated PENDING_REVIEW row on v2 with owned CONTRADICTS edge
        col_con_edge = KnowledgeGraphEdge(
            id="edge-col-con-owned",
            project_id=PROJECT_ID,
            source_unit_id=str(doc2_v2["unit_id"]),
            target_unit_id=str(doc1["unit_id"]),
            edge_type="CONTRADICTS",
            metadata_json={"is_candidate": True, "resolution_id": "res-col-auto"},
        )
        session.add(col_con_edge)
        await session.flush()

        res_auto_col = KnowledgeConflictResolution(
            id="res-col-auto",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            winner_security_partition_id=PART_A,
            pair_key=compute_conflict_pair_key(
                str(doc2_v2["version_id"]),
                str(doc2_v2["unit_id"]),
                str(doc1["version_id"]),
                str(doc1["unit_id"]),
            ),
            winner_document_id="doc-col-2",
            winner_version_id=str(doc2_v2["version_id"]),
            winner_unit_id=str(doc2_v2["unit_id"]),
            winner_content_hash=doc2_v2["unit"].content_hash,
            loser_document_id=str(doc1["document_id"]),
            loser_version_id=str(doc1["version_id"]),
            loser_unit_id=str(doc1["unit_id"]),
            loser_content_hash=doc1["unit"].content_hash,
            conflict_subtype=ConflictSubtype.HARD_CONTRADICTION.value,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
            resolution_mode=ConflictResolutionMode.UNRESOLVED.value,
            confidence=0.80,
            graph_edge_id=col_con_edge.id,
        )
        session.add_all([res_hitl, res_auto_col])
        await session.commit()

        rec = await reconcile_resolutions_after_version_ready(
            session, new_version=doc2_v2["ver"], now_utc=T_NOW
        )
        await session.commit()
        assert rec.carried_forward_count == 1
        assert await session.get(KnowledgeConflictResolution, "res-col-auto") is None
        # Owned CONTRADICTS edge was deleted cleanly -> 0 orphaned candidate edges!
        assert await session.get(KnowledgeGraphEdge, "edge-col-con-owned") is None
        remaining_con = (
            await session.scalars(
                select(KnowledgeGraphEdge).where(KnowledgeGraphEdge.edge_type == "CONTRADICTS")
            )
        ).all()
        assert remaining_con == []


@pytest.mark.asyncio
async def test_persist_conflict_decisions_sanitizes_nan_and_inf_confidence_fail_closed(p2a_db):
    """Fix 11: Non-finite / NaN confidence passed directly to persist_conflict_decisions
    clamps to 0.0 and fails closed to PENDING_REVIEW without IntegrityError or JSON NaN."""
    from sag_api.services.knowledge_conflict_service import ClassifiedConflictDecision

    async with p2a_db() as session:
        d1 = await _seed_doc_unit(
            session, tag="nan-1", text="Legacy timeout is 10 seconds.", authority_tier=2, effective_from=T_JAN
        )
        d2 = await _seed_doc_unit(
            session, tag="nan-2", text="New timeout is 20 seconds.", authority_tier=1, effective_from=T_JUN
        )
        d3 = await _seed_doc_unit(
            session, tag="nan-3", text="Alternate timeout is 30 seconds.", authority_tier=1, effective_from=T_SEP
        )
        await session.commit()

        decisions = []
        for idx, (doc_new, bad_conf) in enumerate(((d2, float("nan")), (d3, float("inf")))):
            cand = ConflictCandidate(
                pair_id=f"nan-pair-{idx}",
                pair_key=compute_conflict_pair_key(
                    str(doc_new["version_id"]),
                    str(doc_new["unit_id"]),
                    str(d1["version_id"]),
                    str(d1["unit_id"]),
                ),
                cosine_similarity=0.91,
                new_document_id=str(doc_new["document_id"]),
                new_document_name=f"nan-{idx}.md",
                new_version_id=str(doc_new["version_id"]),
                new_version=doc_new["ver"],
                new_unit_id=str(doc_new["unit_id"]),
                new_content_hash=doc_new["unit"].content_hash,
                new_section_path="Timeout",
                new_text=doc_new["unit"].text_snapshot or "",
                new_security_partition_id=PART_A,
                existing_document_id=str(d1["document_id"]),
                existing_document_name="nan-1.md",
                existing_version_id=str(d1["version_id"]),
                existing_version=d1["ver"],
                existing_unit_id=str(d1["unit_id"]),
                existing_content_hash=d1["unit"].content_hash,
                existing_section_path="Timeout",
                existing_text=d1["unit"].text_snapshot or "",
                existing_security_partition_id=PART_A,
            )
            decisions.append(
                ClassifiedConflictDecision(
                    candidate=cand,
                    conflict_subtype=ConflictSubtype.DIRECT_SUPERSEDES,
                    confidence=bad_conf,
                    winner="NEW",
                    scope_difference_summary=None,
                    reason="Synthetic non-finite confidence probe",
                )
            )

        summary = await persist_conflict_decisions(
            session,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            security_partition_id=PART_A,
            decisions=decisions,
            now_utc=T_NOW,
        )
        await session.commit()

        assert summary.auto_superseded == 0
        assert summary.pending_review == 2
        rows = (await session.scalars(select(KnowledgeConflictResolution))).all()
        assert len(rows) == 2
        for row in rows:
            assert row.confidence == 0.0
            assert row.status == ConflictResolutionStatus.PENDING_REVIEW.value
            assert row.resolution_mode == ConflictResolutionMode.UNRESOLVED.value
            assert row.degraded_fallback is True
            # Strict JSON serialization MUST succeed (zero NaN/Infinity tokens)
            serialized = json.dumps(row.audit_history_json, allow_nan=False)
            assert "NaN" not in serialized and "Infinity" not in serialized


def test_clear_project_caches_safely_logs_degraded_warning_on_failure(monkeypatch, caplog):
    """Fix 12a: _clear_project_caches_safely MUST log warning with degraded=True on exception."""
    from sag_api.api.v1 import universe as universe_api
    from sag_api.services.knowledge_conflict_service import _clear_project_caches_safely

    def _boom(_project_id: str) -> None:
        raise RuntimeError("cache eviction backend down")

    monkeypatch.setattr(universe_api, "clear_universe_caches", _boom)
    with caplog.at_level(logging.WARNING):
        _clear_project_caches_safely(PROJECT_ID)

    matching = [
        rec.getMessage()
        for rec in caplog.records
        if "degraded=True" in rec.getMessage()
        and "RuntimeError" in rec.getMessage()
        and PROJECT_ID in rec.getMessage()
    ]
    assert len(matching) == 1
