"""TDD tests for Phase 2 Wave 2.1 P2-C downstream integrations:
- MindMap ACL-aware unit supersession anti-join (unit_counts + ranked_units before row_number())
  and scope_fingerprint supersession digest invalidation
- HandoverAssist task-scoped unit supersession exclusion (DRAFT_GENERATION / SOURCE_PROPOSAL)
  and frozen QUESTION_ANSWER retention + [SUPERSEDED] header annotation
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sag_api.api.v1 import api_router
from sag_api.core.config import settings
from sag_api.core.deps import get_llm, get_session
from sag_api.core.errors import ApiError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    Job,
    KnowledgeConflictResolution,
    ProjectMindMapSnapshot,
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
)
from sag_api.schemas.handover_assist import (
    HandoverAssistResponse,
    HandoverAssistSourceInput,
)
from sag_api.schemas.mind_map import MindMapCitationOut, MindMapOut
from sag_api.services import handover_assist_service, mind_map_service

ORG_ID = "651a2b3c4d5e6f7a8b9c0d10"
PROJECT_ID = "651a2b3c4d5e6f7a8b9c0d99"
PROJECT_BETA = "651a2b3c4d5e6f7a8b9c0d88"
P_PUBLIC = PROJECT_ID
P_EXEC = "part-exec-secret"


class _FakeLLM:
    def __init__(self, response: str = "{}") -> None:
        self.configured = True
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> str:
        self.calls.append({"messages": messages, "response_format": response_format})
        return self.response


def _sha256(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8")).hexdigest()


def _bff_headers(
    *,
    token: str = "test-bff-token",
    user_id: str = "651a2b3c4d5e6f7a8b9c0d11",
    org_id: str = ORG_ID,
    project_id: str = PROJECT_ID,
    partition_id: str | None = None,
) -> dict[str, str]:
    return {
        "X-SAG-BFF-Service-Token": token,
        "X-Continuum-User-Id": user_id,
        "X-Continuum-Organization-Id": org_id,
        "X-Continuum-Tenant-Id": org_id,
        "X-Continuum-Project-Id": project_id,
        "X-Continuum-Security-Partition": partition_id or project_id,
    }


def _principal(
    *,
    org_id: str = ORG_ID,
    project_ids: frozenset[str] = frozenset({PROJECT_ID}),
    partition_ids: frozenset[str] = frozenset({P_PUBLIC}),
    conversation_scope: str | None = None,
) -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p2c",
        organization_id=org_id,
        allowed_project_ids=project_ids,
        issuer="test",
        key_id="k1",
        token_id="t1",
        issued_at=1,
        expires_at=9999999999,
        tenant_id=org_id,
        allowed_partition_ids=partition_ids,
        conversation_scope=conversation_scope,
    )


async def _ensure_source(
    session: AsyncSession,
    *,
    source_id: str,
    org_id: str = ORG_ID,
    project_id: str = PROJECT_ID,
    state: str = "CONFIRMED",
) -> None:
    if await session.get(Source, source_id) is None:
        session.add(
            Source(
                id=source_id,
                name=f"Source {source_id}",
                sag_source_config_id=f"cfg-{source_id}",
            )
        )
        await session.flush()
    spm_id = f"spm-{source_id}-{project_id}"
    if await session.get(SourceProjectMapping, spm_id) is None:
        kwargs: dict[str, Any] = {
            "id": spm_id,
            "source_id": source_id,
            "organization_id": org_id,
            "project_id": project_id,
            "state": state,
            "mapping_version": 1,
        }
        if state == "CONFIRMED":
            kwargs.update(
                confirmed_at=datetime.now(UTC) - timedelta(days=30),
                confirmed_by="admin",
                approval_ref=f"apr-{spm_id}",
            )
        session.add(SourceProjectMapping(**kwargs))
        await session.flush()


async def _seed_doc_with_units(
    session: AsyncSession,
    *,
    source_id: str,
    doc_id: str,
    ver_id: str,
    sections: list[tuple[str, str, int]],
    org_id: str = ORG_ID,
    project_id: str = PROJECT_ID,
    partition_id: str = P_PUBLIC,
    filename: str = "doc.md",
    logical_source_id: str | None = None,
    version_no: int = 1,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    search_status: str = "SEARCH_READY",
) -> tuple[DocumentVersion, list[SearchUnit]]:
    await _ensure_source(session, source_id=source_id, org_id=org_id, project_id=project_id)
    now = datetime.now(UTC)
    v_from = valid_from or (now - timedelta(days=30))
    v_to = valid_to or datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    doc = Document(
        id=doc_id,
        tenant_id=org_id,
        project_id=project_id,
        source_id=source_id,
        logical_source_id=logical_source_id,
        filename=filename,
        storage_path=f"/tmp/{doc_id}_{filename}",
        status=DocumentStatus.READY,
        is_active=True,
    )
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc_id,
        version_no=version_no,
        file_hash=_sha256(f"{doc_id}:{ver_id}"),
        status="READY",
        search_status=search_status,
        knowledge_status="READY",
        search_ready_at=now - timedelta(days=29),
        valid_from=v_from,
        valid_to=v_to,
        metadata_json={"security_partition_id": partition_id},
    )
    session.add_all([doc, ver])
    await session.flush()

    units: list[SearchUnit] = []
    for idx, (section_path, text, page_no) in enumerate(sections, start=1):
        blk_id = f"{ver_id}_blk_{idx:02d}"
        su_id = f"{ver_id}_su_{idx:02d}"
        chash = _sha256(f"{su_id}:{text}")
        blk = CanonicalBlock(
            id=blk_id,
            document_version_id=ver_id,
            ordinal=idx,
            block_type="paragraph",
            page_from=page_no,
            page_to=page_no,
            section_path=section_path,
            source_anchor=f"p{page_no}-b{idx}",
            normalized_text=text,
            content_hash=chash,
        )
        su = SearchUnit(
            id=su_id,
            document_version_id=ver_id,
            block_from_id=blk_id,
            block_to_id=blk_id,
            security_partition_id=partition_id,
            content_hash=chash,
            token_count=max(1, len(text.split())),
            page_from=page_no,
            page_to=page_no,
            section_path=section_path,
        )
        session.add_all([blk, su])
        units.append(su)
    await session.flush()
    return ver, units


async def _seed_conflict(
    session: AsyncSession,
    *,
    res_id: str,
    winner_doc_id: str,
    winner_ver_id: str,
    winner_unit_id: str,
    loser_doc_id: str,
    loser_ver_id: str,
    loser_unit_id: str,
    org_id: str = ORG_ID,
    project_id: str = PROJECT_ID,
    loser_partition: str = P_PUBLIC,
    winner_partition: str = P_PUBLIC,
    status: str = ConflictResolutionStatus.ACTIVE.value,
    resolution_mode: str = ConflictResolutionMode.AUTO_POLICY.value,
    conflict_subtype: str = ConflictSubtype.DIRECT_SUPERSEDES.value,
    effective_valid_to: datetime | None = None,
) -> KnowledgeConflictResolution:
    row = KnowledgeConflictResolution(
        id=res_id,
        tenant_id=org_id,
        project_id=project_id,
        security_partition_id=loser_partition,
        winner_security_partition_id=winner_partition,
        pair_key=compute_conflict_pair_key(
            winner_ver_id,
            winner_unit_id,
            loser_ver_id,
            loser_unit_id,
        ),
        winner_document_id=winner_doc_id,
        winner_version_id=winner_ver_id,
        winner_unit_id=winner_unit_id,
        winner_content_hash=_sha256(f"w:{res_id}"),
        loser_document_id=loser_doc_id,
        loser_version_id=loser_ver_id,
        loser_unit_id=loser_unit_id,
        loser_content_hash=_sha256(f"l:{res_id}"),
        conflict_subtype=conflict_subtype,
        status=status,
        resolution_mode=resolution_mode,
        confidence=0.95,
        effective_valid_to=effective_valid_to,
        reason="Supersession test fixture",
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def p2c_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    llm = _FakeLLM(
        json.dumps(
            {
                "tree": {
                    "id": "root",
                    "label": "Governance Map",
                    "summary": "Authoritative project policies",
                    "citationNumbers": [],
                    "children": [
                        {
                            "id": "c1",
                            "label": "Current Standard",
                            "summary": "Active rule",
                            "citationNumbers": [1],
                            "children": [],
                        }
                    ],
                }
            }
        )
    )
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_err(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)

    async def _override_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_llm] = lambda: llm

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sag.test"
    ) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            session_factory=session_factory,
            llm=llm,
            project_id=PROJECT_ID,
        )

    await engine.dispose()


# ===========================================================================
# PART A: MindMap Service (anti-join before row_number(), Seam A, fingerprint)
# ===========================================================================


@pytest.mark.asyncio
async def test_mind_map_excludes_acl_visible_superseded_units_from_counts_and_before_row_number_cap(
    p2c_env,
) -> None:
    pid = p2c_env.project_id
    async with p2c_env.session_factory() as s:
        _ver_w, units_w = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_winner",
            ver_id="ver_winner",
            filename="policy-2026.md",
            sections=[("Policy 2026", "WINNER_POLICY_2026: TLS 1.3 mandatory across all edges.", 1)],
        )
        # 17 units on doc_loser; unit #1 is actively superseded so units #2..#17 must fill all 16 slots!
        loser_sections = [
            (f"Section {i:02d}", f"LOSER_DOC_UNIT_{i:02d}: operational clause {i}.", i)
            for i in range(1, 18)
        ]
        _ver_l, units_l = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_loser_17",
            ver_id="ver_loser_17",
            filename="handbook.md",
            sections=loser_sections,
        )
        # Fully superseded doc (only 1 unit, actively superseded) -> must drop out of ready_docs!
        _ver_full, units_full = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_fully_sup",
            ver_id="ver_fully_sup",
            filename="deprecated-only.md",
            sections=[("Deprecated", "FULLY_SUPERSEDED_ONLY_UNIT: legacy TLS 1.0 rule.", 1)],
        )
        # Pending conflict doc (PENDING_REVIEW) -> must NOT be excluded!
        _ver_pend, units_pend = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_pending_c",
            ver_id="ver_pending_c",
            filename="pending-conflict.md",
            sections=[("Pending", "PENDING_CONFLICT_UNIT_STILL_VISIBLE: retention 90 days.", 1)],
        )
        await _seed_conflict(
            s,
            res_id="res-active-u1",
            winner_doc_id="doc_winner",
            winner_ver_id="ver_winner",
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_loser_17",
            loser_ver_id="ver_loser_17",
            loser_unit_id=units_l[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await _seed_conflict(
            s,
            res_id="res-active-full",
            winner_doc_id="doc_winner",
            winner_ver_id="ver_winner",
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_fully_sup",
            loser_ver_id="ver_fully_sup",
            loser_unit_id=units_full[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await _seed_conflict(
            s,
            res_id="res-pending-keep",
            winner_doc_id="doc_winner",
            winner_ver_id="ver_winner",
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_pending_c",
            loser_ver_id="ver_pending_c",
            loser_unit_id=units_pend[0].id,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
        )
        await s.commit()

        ready_docs, pending_count, _scope_fp = await mind_map_service._load_project_scope_snapshot(
            s,
            principal=_principal(),
            project_id=pid,
        )

    assert pending_count == 0
    by_doc = {d.sag_document_id: d for d in ready_docs}
    assert set(by_doc.keys()) == {"doc_winner", "doc_loser_17", "doc_pending_c"}
    assert "doc_fully_sup" not in by_doc
    assert by_doc["doc_loser_17"].total_unit_count == 16  # 17 - 1 active superseded!
    assert by_doc["doc_winner"].total_unit_count == 1
    assert by_doc["doc_pending_c"].total_unit_count == 1

    resp = await p2c_env.client.post(
        f"/api/v1/projects/{pid}/mind-map",
        headers=_bff_headers(project_id=pid),
        json={},
    )
    assert resp.status_code == 200
    body = resp.json()
    validated = MindMapOut.model_validate(body)
    assert validated.coverage.accessibleReadyDocumentCount == 3
    assert validated.coverage.evidenceUnitCount == 18  # 16 + 1 + 1
    assert validated.coverage.truncatedDocumentCount == 0

    assert len(p2c_env.llm.calls) == 1
    user_prompt = p2c_env.llm.calls[0]["messages"][1]["content"]
    assert "LOSER_DOC_UNIT_01" not in user_prompt
    assert "FULLY_SUPERSEDED_ONLY_UNIT" not in user_prompt
    # Unit #17 MUST be present, proving exclusion ran BEFORE row_number() cap of 16!
    assert "LOSER_DOC_UNIT_17" in user_prompt
    assert "WINNER_POLICY_2026" in user_prompt
    assert "PENDING_CONFLICT_UNIT_STILL_VISIBLE" in user_prompt

    # Strict extra="forbid" schema & persisted citations_json remain 100% untouched
    async with p2c_env.session_factory() as s:
        snap = await s.scalar(select(ProjectMindMapSnapshot))
        assert snap is not None
        assert len(snap.citations_json) == 1
        MindMapCitationOut.model_validate(snap.citations_json[0])
        with pytest.raises(PydanticValidationError):
            MindMapCitationOut.model_validate({**snap.citations_json[0], "isSuperseded": False})


@pytest.mark.asyncio
async def test_mind_map_cross_partition_acl_isolation_seam_a_no_side_channel(p2c_env) -> None:
    pid = p2c_env.project_id
    pub_principal = _principal(partition_ids=frozenset({P_PUBLIC}))
    exec_principal = _principal(partition_ids=frozenset({P_PUBLIC, P_EXEC}))

    async with p2c_env.session_factory() as s:
        ver_pub, units_pub = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_pub",
            ver_id="ver_pub",
            partition_id=P_PUBLIC,
            filename="public-guide.md",
            sections=[
                ("Section 1", "PUBLIC_LOSER_CLAUSE: standard SLA 24h.", 1),
                ("Section 2", "PUBLIC_LIVE_CLAUSE: escalation contacts.", 2),
            ],
        )
        ver_exec, units_exec = await _seed_doc_with_units(
            s,
            source_id="src_exec",
            doc_id="doc_exec",
            ver_id="ver_exec",
            partition_id=P_EXEC,
            filename="exec-override.md",
            sections=[("Exec Override", "EXEC_WINNER_CLAUSE: confidential SLA 4h.", 1)],
        )
        await s.commit()

        docs_pub_0, _, fp_pub_0 = await mind_map_service._load_project_scope_snapshot(
            s, principal=pub_principal, project_id=pid
        )
        docs_exec_0, _, fp_exec_0 = await mind_map_service._load_project_scope_snapshot(
            s, principal=exec_principal, project_id=pid
        )
        expected_pub_legacy_fp = _sha256(
            f"ready:{ORG_ID}:{pid}:doc_pub:ver_pub:1:{ver_pub.file_hash}"
        )
        assert fp_pub_0 == expected_pub_legacy_fp
        assert len(docs_pub_0) == 1 and docs_pub_0[0].total_unit_count == 2
        assert len(docs_exec_0) == 2

        # Seed ACTIVE cross-partition supersession: P_exec winner supersedes P_public loser
        await _seed_conflict(
            s,
            res_id="res-xpart-mm",
            winner_doc_id="doc_exec",
            winner_ver_id=ver_exec.id,
            winner_unit_id=units_exec[0].id,
            loser_doc_id="doc_pub",
            loser_ver_id=ver_pub.id,
            loser_unit_id=units_pub[0].id,
            loser_partition=P_PUBLIC,
            winner_partition=P_EXEC,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        # Public-only user: ZERO suppression AND bit-identical fingerprint (no side-channel!)
        docs_pub_1, _, fp_pub_1 = await mind_map_service._load_project_scope_snapshot(
            s, principal=pub_principal, project_id=pid
        )
        assert fp_pub_1 == fp_pub_0 == expected_pub_legacy_fp
        assert len(docs_pub_1) == 1
        assert docs_pub_1[0].total_unit_count == 2

        pub_catalog, _, _ = await mind_map_service._select_evidence_catalog(
            s,
            principal=pub_principal,
            ready_docs=docs_pub_1,
            pending_count=0,
            topic=None,
        )
        assert {ev.unit.unit_id for ev in pub_catalog} == {units_pub[0].id, units_pub[1].id}

        # Exec user ({P_public, P_exec}): loser IS excluded AND fingerprint changes!
        docs_exec_1, _, fp_exec_1 = await mind_map_service._load_project_scope_snapshot(
            s, principal=exec_principal, project_id=pid
        )
        assert fp_exec_1 != fp_exec_0
        by_doc_exec = {d.sag_document_id: d for d in docs_exec_1}
        assert by_doc_exec["doc_pub"].total_unit_count == 1
        assert by_doc_exec["doc_exec"].total_unit_count == 1

        exec_catalog, _, _ = await mind_map_service._select_evidence_catalog(
            s,
            principal=exec_principal,
            ready_docs=docs_exec_1,
            pending_count=0,
            topic=None,
        )
        assert {ev.unit.unit_id for ev in exec_catalog} == {units_pub[1].id, units_exec[0].id}


@pytest.mark.asyncio
async def test_mind_map_conflict_lifecycle_flips_has_source_updates_and_preserves_legacy_fp(
    p2c_env,
) -> None:
    pid = p2c_env.project_id
    url = f"/api/v1/projects/{pid}/mind-map"
    headers = _bff_headers(project_id=pid)

    async with p2c_env.session_factory() as s:
        ver_a, units_a = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_a",
            ver_id="ver_a",
            filename="doc-a.md",
            sections=[
                ("Rule 1", "DOC_A_UNIT_1_LOSER: old timeout 60s.", 1),
                ("Rule 2", "DOC_A_UNIT_2_KEEP: retries 3 times.", 2),
            ],
        )
        ver_b, units_b = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_b",
            ver_id="ver_b",
            filename="doc-b.md",
            sections=[("Rule 1 Updated", "DOC_B_UNIT_1_WINNER: new timeout 15s.", 1)],
        )
        await s.commit()

    expected_legacy_fp = _sha256(
        f"ready:{ORG_ID}:{pid}:"
        f"doc_a:ver_a:1:{ver_a.file_hash}|doc_b:ver_b:1:{ver_b.file_hash}"
    )

    # 1. Baseline zero conflicts -> bit-identical legacy fingerprint, hasSourceUpdates=False
    r_init = await p2c_env.client.post(url, headers=headers, json={})
    assert r_init.status_code == 200
    assert r_init.json()["scopeFingerprint"] == expected_legacy_fp

    r_get_0 = await p2c_env.client.get(url, headers=headers)
    assert r_get_0.json()["cached"] is True
    assert r_get_0.json()["hasSourceUpdates"] is False

    # 2. Conflict opens as PENDING_REVIEW -> flips hasSourceUpdates=True
    async with p2c_env.session_factory() as s:
        await _seed_conflict(
            s,
            res_id="res-life-1",
            winner_doc_id="doc_b",
            winner_ver_id=ver_b.id,
            winner_unit_id=units_b[0].id,
            loser_doc_id="doc_a",
            loser_ver_id=ver_a.id,
            loser_unit_id=units_a[0].id,
            status=ConflictResolutionStatus.PENDING_REVIEW.value,
        )
        await s.commit()

    r_get_pend = await p2c_env.client.get(url, headers=headers)
    assert r_get_pend.json()["cached"] is True
    assert r_get_pend.json()["hasSourceUpdates"] is True

    r_ref_pend = await p2c_env.client.post(url, headers=headers, json={"refresh": True})
    fp_pend = r_ref_pend.json()["scopeFingerprint"]
    assert fp_pend != expected_legacy_fp
    assert (await p2c_env.client.get(url, headers=headers)).json()["hasSourceUpdates"] is False

    # 3. Resolve conflict (PENDING_REVIEW -> ACTIVE) -> flips hasSourceUpdates=True & excludes loser
    async with p2c_env.session_factory() as s:
        kcr = await s.get(KnowledgeConflictResolution, "res-life-1")
        assert kcr is not None
        kcr.status = ConflictResolutionStatus.ACTIVE.value
        await s.commit()

    r_get_active = await p2c_env.client.get(url, headers=headers)
    assert r_get_active.json()["cached"] is True
    assert r_get_active.json()["hasSourceUpdates"] is True

    r_ref_active = await p2c_env.client.post(url, headers=headers, json={"refresh": True})
    fp_active = r_ref_active.json()["scopeFingerprint"]
    assert fp_active not in {expected_legacy_fp, fp_pend}
    assert "DOC_A_UNIT_1_LOSER" not in p2c_env.llm.calls[-1]["messages"][1]["content"]
    assert "DOC_A_UNIT_2_KEEP" in p2c_env.llm.calls[-1]["messages"][1]["content"]
    assert (await p2c_env.client.get(url, headers=headers)).json()["hasSourceUpdates"] is False

    # 4. Reopen conflict (ACTIVE -> PENDING_REVIEW) -> flips hasSourceUpdates=True & restores loser
    async with p2c_env.session_factory() as s:
        kcr = await s.get(KnowledgeConflictResolution, "res-life-1")
        assert kcr is not None
        kcr.status = ConflictResolutionStatus.PENDING_REVIEW.value
        await s.commit()

    r_get_reopen = await p2c_env.client.get(url, headers=headers)
    assert r_get_reopen.json()["hasSourceUpdates"] is True

    r_ref_reopen = await p2c_env.client.post(url, headers=headers, json={"refresh": True})
    assert r_ref_reopen.json()["scopeFingerprint"] == fp_pend
    assert "DOC_A_UNIT_1_LOSER" in p2c_env.llm.calls[-1]["messages"][1]["content"]

    # 5. Dismiss conflict -> digest returns to "none", restoring bit-identical legacy fingerprint!
    async with p2c_env.session_factory() as s:
        kcr = await s.get(KnowledgeConflictResolution, "res-life-1")
        assert kcr is not None
        kcr.status = ConflictResolutionStatus.DISMISSED_SCOPED.value
        await s.commit()

    r_get_dismissed = await p2c_env.client.get(url, headers=headers)
    assert r_get_dismissed.json()["hasSourceUpdates"] is True
    r_ref_dismissed = await p2c_env.client.post(url, headers=headers, json={"refresh": True})
    assert r_ref_dismissed.json()["scopeFingerprint"] == expected_legacy_fp


@pytest.mark.asyncio
async def test_mind_map_hidden_or_revoked_winner_revives_loser_and_all_projects_scope(p2c_env) -> None:
    async with p2c_env.session_factory() as s:
        ver_w, units_w = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_w_hide",
            ver_id="ver_w_hide",
            filename="winner.md",
            sections=[("Winner", "WINNER_TEXT", 1)],
        )
        ver_l, units_l = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_l_hide",
            ver_id="ver_l_hide",
            filename="loser.md",
            sections=[("Loser", "LOSER_REVIVED_TEXT", 1)],
        )
        await _seed_conflict(
            s,
            res_id="res-hide-1",
            winner_doc_id="doc_w_hide",
            winner_ver_id=ver_w.id,
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_l_hide",
            loser_ver_id=ver_l.id,
            loser_unit_id=units_l[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        # Also seed a second project for 'all' scope verification
        ver_b_w, units_b_w = await _seed_doc_with_units(
            s,
            source_id="src_beta",
            doc_id="doc_beta_w",
            ver_id="ver_beta_w",
            project_id=PROJECT_BETA,
            partition_id=PROJECT_BETA,
            filename="beta-w.md",
            sections=[("Beta W", "BETA_WINNER", 1)],
        )
        ver_b_l, units_b_l = await _seed_doc_with_units(
            s,
            source_id="src_beta",
            doc_id="doc_beta_l",
            ver_id="ver_beta_l",
            project_id=PROJECT_BETA,
            partition_id=PROJECT_BETA,
            filename="beta-l.md",
            sections=[("Beta L", "BETA_LOSER", 1)],
        )
        await _seed_conflict(
            s,
            res_id="res-beta-1",
            winner_doc_id="doc_beta_w",
            winner_ver_id=ver_b_w.id,
            winner_unit_id=units_b_w[0].id,
            loser_doc_id="doc_beta_l",
            loser_ver_id=ver_b_l.id,
            loser_unit_id=units_b_l[0].id,
            project_id=PROJECT_BETA,
            loser_partition=PROJECT_BETA,
            winner_partition=PROJECT_BETA,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        # Verify 'all'-projects scope suppresses losers in BOTH projects
        all_principal = _principal(
            project_ids=frozenset({PROJECT_ID, PROJECT_BETA}),
            partition_ids=frozenset({PROJECT_ID, PROJECT_BETA}),
            conversation_scope="all",
        )
        all_docs, _, _ = await mind_map_service._load_project_scope_snapshot(
            s, principal=all_principal, project_id="all"
        )
        assert {d.sag_document_id for d in all_docs} == {"doc_w_hide", "doc_beta_w"}

        # Queue a DELETE_DOCUMENT job on doc_w_hide -> winner is now hidden, loser immediately revives!
        s.add(
            Job(
                id="job-del-winner",
                source_id="src_pub",
                document_id="doc_w_hide",
                type=JobType.DELETE_DOCUMENT,
                status=JobStatus.RUNNING,
            )
        )
        await s.commit()

        docs_after_hide, _, _ = await mind_map_service._load_project_scope_snapshot(
            s, principal=_principal(), project_id=PROJECT_ID
        )
        assert {d.sag_document_id for d in docs_after_hide} == {"doc_l_hide"}
        cat_after_hide, _, _ = await mind_map_service._select_evidence_catalog(
            s, principal=_principal(), ready_docs=docs_after_hide, pending_count=0, topic=None
        )
        assert {ev.unit.unit_id for ev in cat_after_hide} == {units_l[0].id}


# ===========================================================================
# PART B: HandoverAssist Service (task-scoped exclude vs QA retain+annotate)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["DRAFT_GENERATION", "SOURCE_PROPOSAL"])
async def test_handover_draft_and_proposal_exclude_superseded_units_before_row_number_cap(
    p2c_env,
    task: str,
) -> None:
    pid = p2c_env.project_id
    logical_loser = "continuum-doc-loser-26"
    logical_winner = "continuum-doc-winner"

    async with p2c_env.session_factory() as s:
        ver_w, units_w = await _seed_doc_with_units(
            s,
            source_id="src_ho",
            doc_id="doc_ho_winner",
            ver_id="ver_ho_winner",
            logical_source_id=logical_winner,
            filename="winner-runbook.md",
            sections=[("Winner", "HO_WINNER_UNIT: use automated Helm rollout.", 1)],
        )
        # 26 units on loser doc; unit #1 is actively superseded so units #2..#26 must fill all 25 slots!
        loser_sections = [
            (f"Step {i:02d}", f"HO_LOSER_STEP_{i:02d}: runbook instruction {i}.", i)
            for i in range(1, 27)
        ]
        ver_l, units_l = await _seed_doc_with_units(
            s,
            source_id="src_ho",
            doc_id="doc_ho_loser",
            ver_id="ver_ho_loser",
            logical_source_id=logical_loser,
            filename="legacy-runbook.md",
            sections=loser_sections,
        )
        await _seed_conflict(
            s,
            res_id="res-ho-draft-1",
            winner_doc_id="doc_ho_winner",
            winner_ver_id=ver_w.id,
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_ho_loser",
            loser_ver_id=ver_l.id,
            loser_unit_id=units_l[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        truncated: set[tuple[str, int]] = set()
        sup_keys: set[tuple[str, int, str, str]] = set()
        loaded = await handover_assist_service._load_indexed_units(
            s,
            principal=_principal(),
            project_id=pid,
            task=task,
            sources=[
                HandoverAssistSourceInput(
                    kind="PROJECT_DOCUMENT",
                    id="851a2b3c4d5e6f7a8b9c0d31",
                    version=1,
                    title="Legacy Runbook",
                    logicalSourceId=logical_loser,
                )
            ],
            truncated_sources=truncated,
            superseded_unit_keys=sup_keys,
        )

    items = loaded[(logical_loser, 1)]
    loc_vals = [loc_val for _kind, loc_val, _heading, _text in items]
    assert len(items) == 25
    assert units_l[0].id not in loc_vals  # unit #1 excluded!
    assert units_l[25].id in loc_vals  # unit #26 included (row_number() ran AFTER exclusion)!
    assert truncated == set()  # source_total == 25 <= 25 -> NOT falsely truncated!
    assert sup_keys == set()

    # Full HTTP request verification
    if task == "DRAFT_GENERATION":
        p2c_env.llm.response = json.dumps(
            {
                "items": [
                    {
                        "title": "Deployment Steps",
                        "body": "Follow steps 2..26 [1].",
                        "requirement": "Confirm deployment steps.",
                        "evidenceIndices": [1],
                    }
                ]
            }
        )
    else:
        p2c_env.llm.response = json.dumps(
            {"candidates": [{"evidenceIndex": 1, "reason": "Contains authoritative steps."}]}
        )

    res = await p2c_env.client.post(
        f"/api/v1/projects/{pid}/handover/assist",
        headers=_bff_headers(project_id=pid),
        json={
            "task": task,
            "query": "deployment steps",
            "sources": [
                {
                    "kind": "PROJECT_DOCUMENT",
                    "id": "851a2b3c4d5e6f7a8b9c0d31",
                    "version": 1,
                    "title": "Legacy Runbook",
                    "logicalSourceId": logical_loser,
                    "excerpts": [],
                }
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    parsed = HandoverAssistResponse.model_validate(body)
    assert parsed.coverage.truncated is False
    assert parsed.coverage.totalEvidenceUnits == 25
    with pytest.raises(PydanticValidationError):
        HandoverAssistResponse.model_validate({**body, "supersededCount": 1})

    prompt_text = p2c_env.llm.calls[-1]["messages"][1]["content"]
    assert "HO_LOSER_STEP_01" not in prompt_text
    assert "HO_LOSER_STEP_26" in prompt_text


@pytest.mark.asyncio
async def test_handover_cross_partition_acl_isolation_seam_a_across_all_tasks(p2c_env) -> None:
    pid = p2c_env.project_id
    logical_pub = "continuum-doc-ho-pub"
    pub_principal = _principal(partition_ids=frozenset({P_PUBLIC}))
    exec_principal = _principal(partition_ids=frozenset({P_PUBLIC, P_EXEC}))

    async with p2c_env.session_factory() as s:
        ver_pub, units_pub = await _seed_doc_with_units(
            s,
            source_id="src_pub",
            doc_id="doc_ho_pub",
            ver_id="ver_ho_pub",
            partition_id=P_PUBLIC,
            logical_source_id=logical_pub,
            filename="pub.md",
            sections=[
                ("Clause 1", "PUB_LOSER_TEXT: standard threshold 50.", 1),
                ("Clause 2", "PUB_KEEP_TEXT: regular escalation.", 2),
            ],
        )
        ver_exec, units_exec = await _seed_doc_with_units(
            s,
            source_id="src_exec",
            doc_id="doc_ho_exec",
            ver_id="ver_ho_exec",
            partition_id=P_EXEC,
            logical_source_id="continuum-doc-ho-exec",
            filename="exec.md",
            sections=[("Exec", "EXEC_WINNER_TEXT: board override threshold 10.", 1)],
        )
        await _seed_conflict(
            s,
            res_id="res-ho-xpart",
            winner_doc_id="doc_ho_exec",
            winner_ver_id=ver_exec.id,
            winner_unit_id=units_exec[0].id,
            loser_doc_id="doc_ho_pub",
            loser_ver_id=ver_pub.id,
            loser_unit_id=units_pub[0].id,
            loser_partition=P_PUBLIC,
            winner_partition=P_EXEC,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        src_input = [
            HandoverAssistSourceInput(
                kind="PROJECT_DOCUMENT",
                id="851a2b3c4d5e6f7a8b9c0d31",
                version=1,
                title="Public Guide",
                logicalSourceId=logical_pub,
            )
        ]

        # 1. DRAFT_GENERATION & SOURCE_PROPOSAL:
        #    - {P_public} user keeps P_public loser (zero side-channel!)
        #    - {P_public, P_exec} user excludes P_public loser
        for task in ("DRAFT_GENERATION", "SOURCE_PROPOSAL"):
            pub_loaded = await handover_assist_service._load_indexed_units(
                s, principal=pub_principal, project_id=pid, sources=src_input, task=task
            )
            assert [r[1] for r in pub_loaded[(logical_pub, 1)]] == [units_pub[0].id, units_pub[1].id]

            exec_loaded = await handover_assist_service._load_indexed_units(
                s, principal=exec_principal, project_id=pid, sources=src_input, task=task
            )
            assert [r[1] for r in exec_loaded[(logical_pub, 1)]] == [units_pub[1].id]

        # 2. QUESTION_ANSWER:
        #    - {P_public} user retains BOTH and does NOT badge P_public loser [SUPERSEDED]
        #    - {P_public, P_exec} user retains BOTH and DOES badge P_public loser [SUPERSEDED]
        pub_sup: set[tuple[str, int, str, str]] = set()
        pub_qa = await handover_assist_service._load_indexed_units(
            s,
            principal=pub_principal,
            project_id=pid,
            sources=src_input,
            task="QUESTION_ANSWER",
            superseded_unit_keys=pub_sup,
        )
        assert [r[1] for r in pub_qa[(logical_pub, 1)]] == [units_pub[0].id, units_pub[1].id]
        assert pub_sup == set()

        exec_sup: set[tuple[str, int, str, str]] = set()
        exec_qa = await handover_assist_service._load_indexed_units(
            s,
            principal=exec_principal,
            project_id=pid,
            sources=src_input,
            task="QUESTION_ANSWER",
            superseded_unit_keys=exec_sup,
        )
        assert [r[1] for r in exec_qa[(logical_pub, 1)]] == [units_pub[0].id, units_pub[1].id]
        assert exec_sup == {(logical_pub, 1, "CHUNK", units_pub[0].id)}


@pytest.mark.asyncio
async def test_handover_question_answer_retains_and_annotates_unit_and_version_superseded(
    p2c_env,
) -> None:
    pid = p2c_env.project_id
    now = datetime.now(UTC)

    ver_sup_text = "VERSION_SUPERSEDED_RAW: legacy v1 backup cadence every 24h."
    unit_sup_text = "UNIT_SUPERSEDED_RAW: old SLA 48 hours."
    unit_live_text = "UNIT_LIVE_RAW: pager rotation weekly."
    winner_text = "WINNER_LIVE_RAW: updated SLA 4 hours."

    async with p2c_env.session_factory() as s:
        await _seed_doc_with_units(
            s,
            source_id="src_qa",
            doc_id="doc_qa_ver_old",
            ver_id="ver_qa_ver_old",
            logical_source_id="continuum-qa-ver-old",
            filename="backup-v1.md",
            valid_from=now - timedelta(days=90),
            valid_to=now - timedelta(days=5),  # Version-superseded
            sections=[("Backup", ver_sup_text, 1)],
        )
        ver_u, units_u = await _seed_doc_with_units(
            s,
            source_id="src_qa",
            doc_id="doc_qa_unit_mix",
            ver_id="ver_qa_unit_mix",
            logical_source_id="continuum-qa-unit-mix",
            filename="sla-guide.md",
            sections=[
                ("SLA Target", unit_sup_text, 1),
                ("On-Call", unit_live_text, 2),
            ],
        )
        ver_w, units_w = await _seed_doc_with_units(
            s,
            source_id="src_qa",
            doc_id="doc_qa_winner",
            ver_id="ver_qa_winner",
            logical_source_id="continuum-qa-winner",
            filename="sla-2026.md",
            sections=[("SLA 2026", winner_text, 1)],
        )
        await _seed_conflict(
            s,
            res_id="res-qa-unit-1",
            winner_doc_id="doc_qa_winner",
            winner_ver_id=ver_w.id,
            winner_unit_id=units_w[0].id,
            loser_doc_id="doc_qa_unit_mix",
            loser_ver_id=ver_u.id,
            loser_unit_id=units_u[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

    p2c_env.llm.response = json.dumps(
        {
            "answerStatus": "ANSWERED",
            "message": "Answered with superseded notes.",
            "sections": [
                {
                    "text": "Frozen handover excerpts include superseded SLA and backup rules [1][2].",
                    "evidenceIndices": [1, 2, 3, 4],
                }
            ],
        }
    )

    res = await p2c_env.client.post(
        f"/api/v1/projects/{pid}/handover/assist",
        headers=_bff_headers(project_id=pid),
        json={
            "task": "QUESTION_ANSWER",
            "query": "What are the SLA, backup, and on-call procedures?",
            "sources": [
                {
                    "kind": "PROJECT_DOCUMENT",
                    "id": "851a2b3c4d5e6f7a8b9c0d31",
                    "version": 1,
                    "title": "Backup V1",
                    "logicalSourceId": "continuum-qa-ver-old",
                    "excerpts": [],
                },
                {
                    "kind": "PROJECT_DOCUMENT",
                    "id": "851a2b3c4d5e6f7a8b9c0d32",
                    "version": 1,
                    "title": "SLA Guide",
                    "logicalSourceId": "continuum-qa-unit-mix",
                    "excerpts": [],
                },
                {
                    "kind": "PROJECT_DOCUMENT",
                    "id": "851a2b3c4d5e6f7a8b9c0d33",
                    "version": 1,
                    "title": "SLA 2026",
                    "logicalSourceId": "continuum-qa-winner",
                    "excerpts": [],
                },
            ],
        },
    )
    assert res.status_code == 200
    body = res.json()
    parsed = HandoverAssistResponse.model_validate(body)
    assert parsed.answerStatus == "ANSWERED"
    assert parsed.coverage.totalEvidenceUnits == 4
    with pytest.raises(PydanticValidationError):
        HandoverAssistResponse.model_validate({**body, "unitSuperseded": True})

    user_prompt = p2c_env.llm.calls[-1]["messages"][1]["content"]
    # Both version-superseded and unit-superseded blocks have | status=[SUPERSEDED] on header, raw content untouched!
    assert (
        f'locator=CHUNK:ver_qa_ver_old_su_01 | heading="Backup" | status=[SUPERSEDED]\n{ver_sup_text}'
        in user_prompt
    )
    assert (
        f'locator=CHUNK:{units_u[0].id} | heading="SLA Target" | status=[SUPERSEDED]\n{unit_sup_text}'
        in user_prompt
    )
    # Sibling non-superseded unit on the same version AND winner unit do NOT get [SUPERSEDED]!
    assert f'locator=CHUNK:{units_u[1].id} | heading="On-Call"\n{unit_live_text}' in user_prompt
    assert f'locator=CHUNK:{units_w[0].id} | heading="SLA 2026"\n{winner_text}' in user_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["DRAFT_GENERATION", "SOURCE_PROPOSAL", "QUESTION_ANSWER"])
async def test_handover_single_execute_fake_session_and_dual_dialect_sql(task: str) -> None:
    captured_stmts: list[Any] = []

    async def _fake_execute(stmt: Any, *_args: Any, **_kwargs: Any) -> Any:
        captured_stmts.append(stmt)
        return SimpleNamespace(all=lambda: [])

    fake_session = SimpleNamespace(execute=AsyncMock(side_effect=_fake_execute))
    out = await handover_assist_service._load_indexed_units(
        fake_session,
        principal=_principal(),
        project_id=PROJECT_ID,
        task=task,
        sources=[
            HandoverAssistSourceInput(
                kind="PROJECT_DOCUMENT",
                id="851a2b3c4d5e6f7a8b9c0d31",
                version=1,
                title="Doc",
                logicalSourceId="continuum-doc-1",
            )
        ],
    )
    assert out == {}
    assert fake_session.execute.await_count == 1
    assert len(captured_stmts) == 1

    # Verify compiled SQL includes correlated EXISTS against knowledge_conflict_resolutions in PG & SQLite
    stmt = captured_stmts[0]
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        sql = str(stmt.compile(dialect=dialect))
        assert "EXISTS" in sql.upper()
        assert "knowledge_conflict_resolutions" in sql
        if task in ("DRAFT_GENERATION", "SOURCE_PROPOSAL"):
            assert "NOT (EXISTS" in sql.upper() or "NOT EXISTS" in sql.upper()
        else:
            assert "NOT (EXISTS" not in sql.upper() and "NOT EXISTS" not in sql.upper()


# ===========================================================================
# PART C: Triad Round 2 Hardening Regressions (Fixes 6 & 7)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", [JobType.REPROCESS_DOCUMENT, JobType.DELETE_DOCUMENT])
async def test_handover_revives_loser_immediately_during_inflight_reprocess_or_delete_job(
    p2c_env,
    job_type: JobType,
) -> None:
    """Fix 6: HandoverAssist revives Doc 1 across DRAFT_GENERATION, SOURCE_PROPOSAL, and
    QUESTION_ANSWER immediately while winner Doc 2 has an in-flight REPROCESS_DOCUMENT or
    DELETE_DOCUMENT job (matching Retrieval/MindMap with zero ghost suppression)."""
    pid = p2c_env.project_id
    logical_loser = f"continuum-ho-inflight-loser-{job_type.value}"
    logical_winner = f"continuum-ho-inflight-winner-{job_type.value}"

    async with p2c_env.session_factory() as s:
        ver_w, units_w = await _seed_doc_with_units(
            s,
            source_id=f"src_inf_{job_type.value}",
            doc_id=f"doc_inf_w_{job_type.value}",
            ver_id=f"ver_inf_w_{job_type.value}",
            logical_source_id=logical_winner,
            filename="winner.md",
            sections=[("Rule", "WINNER_INFLIGHT: new rule.", 1)],
        )
        ver_l, units_l = await _seed_doc_with_units(
            s,
            source_id=f"src_inf_{job_type.value}",
            doc_id=f"doc_inf_l_{job_type.value}",
            ver_id=f"ver_inf_l_{job_type.value}",
            logical_source_id=logical_loser,
            filename="loser.md",
            sections=[
                ("Rule 1", "LOSER_UNIT_SUP: old rule.", 1),
                ("Rule 2", "LOSER_UNIT_KEEP: untouched rule.", 2),
            ],
        )
        await _seed_conflict(
            s,
            res_id=f"res-inf-{job_type.value}",
            winner_doc_id=f"doc_inf_w_{job_type.value}",
            winner_ver_id=ver_w.id,
            winner_unit_id=units_w[0].id,
            loser_doc_id=f"doc_inf_l_{job_type.value}",
            loser_ver_id=ver_l.id,
            loser_unit_id=units_l[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        src_input = [
            HandoverAssistSourceInput(
                kind="PROJECT_DOCUMENT",
                id="851a2b3c4d5e6f7a8b9c0d41",
                version=1,
                title="Loser Guide",
                logicalSourceId=logical_loser,
            )
        ]

        # Before in-flight control job: loser unit 0 is suppressed
        for task in ("DRAFT_GENERATION", "SOURCE_PROPOSAL"):
            loaded_before = await handover_assist_service._load_indexed_units(
                s, principal=_principal(), project_id=pid, sources=src_input, task=task
            )
            assert [r[1] for r in loaded_before[(logical_loser, 1)]] == [units_l[1].id]

        qa_sup_before: set[tuple[str, int, str, str]] = set()
        await handover_assist_service._load_indexed_units(
            s,
            principal=_principal(),
            project_id=pid,
            sources=src_input,
            task="QUESTION_ANSWER",
            superseded_unit_keys=qa_sup_before,
        )
        assert qa_sup_before == {(logical_loser, 1, "CHUNK", units_l[0].id)}

        # Enqueue in-flight control job on winner doc (REPROCESS sets Document.status=PENDING)
        winner_doc = await s.get(Document, f"doc_inf_w_{job_type.value}")
        assert winner_doc is not None
        if job_type == JobType.REPROCESS_DOCUMENT:
            winner_doc.status = DocumentStatus.PENDING
        s.add(
            Job(
                id=f"job-inf-{job_type.value}",
                source_id=f"src_inf_{job_type.value}",
                document_id=winner_doc.id,
                type=job_type,
                status=JobStatus.QUEUED if job_type == JobType.REPROCESS_DOCUMENT else JobStatus.RUNNING,
            )
        )
        await s.commit()

        # Immediately revived in DRAFT_GENERATION, SOURCE_PROPOSAL, AND QUESTION_ANSWER!
        for task in ("DRAFT_GENERATION", "SOURCE_PROPOSAL"):
            loaded_after = await handover_assist_service._load_indexed_units(
                s, principal=_principal(), project_id=pid, sources=src_input, task=task
            )
            assert [r[1] for r in loaded_after[(logical_loser, 1)]] == [units_l[0].id, units_l[1].id]

        qa_sup_after: set[tuple[str, int, str, str]] = set()
        qa_loaded_after = await handover_assist_service._load_indexed_units(
            s,
            principal=_principal(),
            project_id=pid,
            sources=src_input,
            task="QUESTION_ANSWER",
            superseded_unit_keys=qa_sup_after,
        )
        assert [r[1] for r in qa_loaded_after[(logical_loser, 1)]] == [units_l[0].id, units_l[1].id]
        assert qa_sup_after == set()


@pytest.mark.asyncio
async def test_mind_map_three_doc_transitive_supersession_chain_excludes_doc1_superseded_unit(
    p2c_env,
) -> None:
    """Fix 7: Under a 3-document chain across distinct confirmed sources (Doc 3 -> Doc 2 -> Doc 1),
    dropping fully-superseded Doc 2 from ready_docs MUST NOT drop Doc 2's source from
    _select_evidence_catalog's supersession filter and leak Doc 1's superseded unit."""
    pid = p2c_env.project_id
    async with p2c_env.session_factory() as s:
        ver_1, units_1 = await _seed_doc_with_units(
            s,
            source_id="src_chain_1",
            doc_id="doc_chain_1",
            ver_id="ver_chain_1",
            filename="chain-doc1.md",
            sections=[
                ("Clause A", "CHAIN_DOC1_SUPERSEDED: legacy 2023 rule.", 1),
                ("Clause B", "CHAIN_DOC1_ACTIVE: evergreen rule.", 2),
            ],
        )
        ver_2, units_2 = await _seed_doc_with_units(
            s,
            source_id="src_chain_2",
            doc_id="doc_chain_2",
            ver_id="ver_chain_2",
            filename="chain-doc2.md",
            sections=[("Clause A v2", "CHAIN_DOC2_MIDDLE: intermediate 2024 rule.", 1)],
        )
        ver_3, units_3 = await _seed_doc_with_units(
            s,
            source_id="src_chain_3",
            doc_id="doc_chain_3",
            ver_id="ver_chain_3",
            filename="chain-doc3.md",
            sections=[("Clause A v3", "CHAIN_DOC3_LATEST: final 2026 rule.", 1)],
        )
        # Doc 2 supersedes Doc 1's Clause A; Doc 3 supersedes Doc 2's sole unit
        await _seed_conflict(
            s,
            res_id="res-chain-2-over-1",
            winner_doc_id="doc_chain_2",
            winner_ver_id=ver_2.id,
            winner_unit_id=units_2[0].id,
            loser_doc_id="doc_chain_1",
            loser_ver_id=ver_1.id,
            loser_unit_id=units_1[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await _seed_conflict(
            s,
            res_id="res-chain-3-over-2",
            winner_doc_id="doc_chain_3",
            winner_ver_id=ver_3.id,
            winner_unit_id=units_3[0].id,
            loser_doc_id="doc_chain_2",
            loser_ver_id=ver_2.id,
            loser_unit_id=units_2[0].id,
            status=ConflictResolutionStatus.ACTIVE.value,
        )
        await s.commit()

        ready_docs, _, _ = await mind_map_service._load_project_scope_snapshot(
            s, principal=_principal(), project_id=pid
        )
        by_doc = {d.sag_document_id: d for d in ready_docs}
        assert set(by_doc.keys()) == {"doc_chain_1", "doc_chain_3"}
        assert "doc_chain_2" not in by_doc
        assert by_doc["doc_chain_1"].total_unit_count == 1

        catalog, coverage, _ = await mind_map_service._select_evidence_catalog(
            s,
            principal=_principal(),
            ready_docs=ready_docs,
            pending_count=0,
            topic=None,
            project_id=pid,
        )
        cat_unit_ids = {ev.unit.unit_id for ev in catalog}
        assert units_1[0].id not in cat_unit_ids  # Doc 1's superseded unit MUST stay excluded!
        assert cat_unit_ids == {units_1[1].id, units_3[0].id}
        assert coverage.evidenceUnitCount == 2

        # If Doc 2 enters DELETE_DOCUMENT in-flight, Doc 1's Clause A revives consistently
        s.add(
            Job(
                id="job-del-chain-2",
                source_id="src_chain_2",
                document_id="doc_chain_2",
                type=JobType.DELETE_DOCUMENT,
                status=JobStatus.RUNNING,
            )
        )
        await s.commit()

        ready_docs_del, _, _ = await mind_map_service._load_project_scope_snapshot(
            s, principal=_principal(), project_id=pid
        )
        by_doc_del = {d.sag_document_id: d for d in ready_docs_del}
        assert by_doc_del["doc_chain_1"].total_unit_count == 2
        catalog_del, _, _ = await mind_map_service._select_evidence_catalog(
            s,
            principal=_principal(),
            ready_docs=ready_docs_del,
            pending_count=0,
            topic=None,
            project_id=pid,
        )
        assert {ev.unit.unit_id for ev in catalog_del} == {
            units_1[0].id,
            units_1[1].id,
            units_3[0].id,
        }
