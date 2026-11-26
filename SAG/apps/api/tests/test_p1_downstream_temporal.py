"""TDD tests for Phase 1 downstream temporal/authority integrations:
- MindMap scope_fingerprint folding of DocumentVersion.authority_tier & effective_from
- HandoverAssist frozen QUESTION_ANSWER retention & [SUPERSEDED] header annotation
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
from sag_api.enums import DocumentStatus
from sag_api.schemas.handover_assist import (
    HandoverAssistResponse,
    HandoverAssistSourceInput,
    SourceRefPayload,
)
from sag_api.schemas.mind_map import MindMapCitationOut, MindMapOut
from sag_api.services import handover_assist_service, mind_map_service


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


def _bff_headers(
    *,
    token: str = "test-bff-token",
    user_id: str = "651a2b3c4d5e6f7a8b9c0d11",
    org_id: str = "org_alpha",
    project_id: str = "651a2b3c4d5e6f7a8b9c0d99",
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


def _principal(*, org_id: str = "org_alpha", project_id: str = "651a2b3c4d5e6f7a8b9c0d99") -> VerifiedPrincipal:
    return VerifiedPrincipal(
        subject="user-p1e",
        organization_id=org_id,
        allowed_project_ids=frozenset({project_id}),
        issuer="test",
        key_id="k1",
        token_id="t1",
        issued_at=1,
        expires_at=9999999999,
        tenant_id=org_id,
        allowed_partition_ids=frozenset({project_id}),
    )


async def _seed_mind_map_doc(
    session: AsyncSession,
    *,
    source_id: str,
    doc_id: str,
    ver_id: str,
    org_id: str,
    project_id: str,
    filename: str = "policy.md",
    version_no: int = 1,
    authority_tier: int | None = None,
    effective_from: datetime | None = None,
    section_path: str = "1. Policy",
    text: str = "All production deployments require change-control approval.",
) -> DocumentVersion:
    file_hash = hashlib.sha256(f"{doc_id}:{ver_id}".encode()).hexdigest()
    doc = Document(
        id=doc_id,
        tenant_id=org_id,
        project_id=project_id,
        source_id=source_id,
        logical_source_id="continuum:project_document:507f1f77bcf86cd799439021",
        filename=filename,
        storage_path=f"/tmp/{doc_id}_{filename}",
        status=DocumentStatus.READY,
        is_active=True,
    )
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc_id,
        version_no=version_no,
        file_hash=file_hash,
        status="READY",
        search_status="SEARCH_READY",
        knowledge_status="READY",
        search_ready_at=datetime.now(UTC),
        authority_tier=authority_tier,
        effective_from=effective_from,
        metadata_json={"security_partition_id": project_id},
    )
    session.add_all([doc, ver])
    await session.flush()

    blk_id = f"{ver_id}_blk_1"
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    block = CanonicalBlock(
        id=blk_id,
        document_version_id=ver_id,
        ordinal=1,
        block_type="paragraph",
        page_from=1,
        page_to=1,
        section_path=section_path,
        source_anchor="p1-b1",
        normalized_text=text,
        content_hash=content_hash,
    )
    unit = SearchUnit(
        id=f"{ver_id}_su_1",
        document_version_id=ver_id,
        block_from_id=blk_id,
        block_to_id=blk_id,
        security_partition_id=project_id,
        content_hash=content_hash,
        token_count=max(1, len(text.split())),
        page_from=1,
        page_to=1,
        section_path=section_path,
    )
    session.add_all([block, unit])
    return ver


@pytest.fixture
async def mind_map_p1_env(monkeypatch: pytest.MonkeyPatch):
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
                    "label": "Governance Overview",
                    "summary": "Grounded map of project policies",
                    "citationNumbers": [],
                    "children": [
                        {
                            "id": "c1",
                            "label": "Deployment Policy",
                            "summary": "Change-control requirement",
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

    project_id = "651a2b3c4d5e6f7a8b9c0d99"
    async with session_factory() as s:
        s.add_all(
            [
                Source(id="src_alpha", name="Alpha Docs", sag_source_config_id="cfg_a"),
                SourceProjectMapping(
                    id="spm_alpha",
                    source_id="src_alpha",
                    organization_id="org_alpha",
                    project_id=project_id,
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="admin",
                    approval_ref="apr_1",
                ),
            ]
        )
        await s.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sag.test") as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            session_factory=session_factory,
            llm=llm,
            project_id=project_id,
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_mind_map_fingerprint_bit_identical_when_authority_and_effective_from_unset(
    mind_map_p1_env,
) -> None:
    pid = mind_map_p1_env.project_id
    async with mind_map_p1_env.session_factory() as s:
        ver = await _seed_mind_map_doc(
            s,
            source_id="src_alpha",
            doc_id="doc_legacy_1",
            ver_id="ver_legacy_1",
            org_id="org_alpha",
            project_id=pid,
            authority_tier=None,
            effective_from=None,
        )
        await s.commit()

        ready_docs, pending_count, scope_fp = await mind_map_service._load_project_scope_snapshot(
            s,
            principal=_principal(project_id=pid),
            project_id=pid,
        )

    expected_legacy_fp = hashlib.sha256(
        f"ready:org_alpha:{pid}:doc_legacy_1:ver_legacy_1:1:{ver.file_hash}".encode()
    ).hexdigest()

    assert pending_count == 0
    assert len(ready_docs) == 1
    assert ready_docs[0].authority_tier is None
    assert ready_docs[0].effective_from_iso == ""
    assert scope_fp == expected_legacy_fp

    url = f"/api/v1/projects/{pid}/mind-map"
    headers = _bff_headers(project_id=pid)
    r_post = await mind_map_p1_env.client.post(url, headers=headers, json={})
    assert r_post.status_code == 200
    assert r_post.json()["scopeFingerprint"] == expected_legacy_fp

    r_get = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get.status_code == 200
    body = r_get.json()
    assert body["cached"] is True
    assert body["hasSourceUpdates"] is False
    assert body["scopeFingerprint"] == expected_legacy_fp


@pytest.mark.asyncio
async def test_mind_map_fingerprint_flips_has_source_updates_on_authority_or_effective_from_change(
    mind_map_p1_env,
) -> None:
    pid = mind_map_p1_env.project_id
    url = f"/api/v1/projects/{pid}/mind-map"
    headers = _bff_headers(project_id=pid)

    async with mind_map_p1_env.session_factory() as s:
        ver = await _seed_mind_map_doc(
            s,
            source_id="src_alpha",
            doc_id="doc_meta_1",
            ver_id="ver_meta_1",
            org_id="org_alpha",
            project_id=pid,
            authority_tier=None,
            effective_from=None,
        )
        await s.commit()
        legacy_fp = hashlib.sha256(
            f"ready:org_alpha:{pid}:doc_meta_1:ver_meta_1:1:{ver.file_hash}".encode()
        ).hexdigest()

    # Initial generation with unannotated doc
    r_init = await mind_map_p1_env.client.post(url, headers=headers, json={})
    assert r_init.status_code == 200
    assert r_init.json()["scopeFingerprint"] == legacy_fp

    # 1. Setting authority_tier=1 flips hasSourceUpdates=True on GET
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.authority_tier = 1
        await s.commit()

    r_get_tier1 = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get_tier1.status_code == 200
    b_tier1 = r_get_tier1.json()
    assert b_tier1["cached"] is True
    assert b_tier1["hasSourceUpdates"] is True

    # Refresh snapshot with authority_tier=1 -> hasSourceUpdates resets to False
    r_ref_tier1 = await mind_map_p1_env.client.post(url, headers=headers, json={"refresh": True})
    assert r_ref_tier1.status_code == 200
    fp_tier1 = r_ref_tier1.json()["scopeFingerprint"]
    assert fp_tier1 != legacy_fp

    r_get_tier1_fresh = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get_tier1_fresh.json()["hasSourceUpdates"] is False

    # 2. Updating authority_tier=1 -> 2 flips hasSourceUpdates=True again
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.authority_tier = 2
        await s.commit()

    r_get_tier2 = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get_tier2.json()["hasSourceUpdates"] is True

    r_ref_tier2 = await mind_map_p1_env.client.post(url, headers=headers, json={"refresh": True})
    fp_tier2 = r_ref_tier2.json()["scopeFingerprint"]
    assert fp_tier2 not in {legacy_fp, fp_tier1}

    # 3. Setting effective_from flips hasSourceUpdates=True
    eff_t1 = datetime(2026, 2, 15, 0, 0, tzinfo=UTC)
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.effective_from = eff_t1
        await s.commit()

    r_get_eff1 = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get_eff1.json()["hasSourceUpdates"] is True

    r_ref_eff1 = await mind_map_p1_env.client.post(url, headers=headers, json={"refresh": True})
    fp_eff1 = r_ref_eff1.json()["scopeFingerprint"]
    assert fp_eff1 not in {legacy_fp, fp_tier1, fp_tier2}

    # 4. Changing effective_from flips hasSourceUpdates=True again
    eff_t2 = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.effective_from = eff_t2
        await s.commit()

    r_get_eff2 = await mind_map_p1_env.client.get(url, headers=headers)
    assert r_get_eff2.json()["hasSourceUpdates"] is True

    # 4b. Setting effective_from alone (authority_tier=None) also produces a distinct fingerprint
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.authority_tier = None
        row.effective_from = eff_t2
        await s.commit()

        docs_eff_only, _pending_eo, fp_eff_only = await mind_map_service._load_project_scope_snapshot(
            s,
            principal=_principal(project_id=pid),
            project_id=pid,
        )
    assert docs_eff_only[0].authority_tier is None
    assert docs_eff_only[0].effective_from_iso == eff_t2.isoformat()
    assert fp_eff_only not in {legacy_fp, fp_tier1, fp_tier2, fp_eff1}

    # 5. Clearing both fields restores legacy fingerprint bit-identically & flips hasSourceUpdates=True
    async with mind_map_p1_env.session_factory() as s:
        row = await s.get(DocumentVersion, "ver_meta_1")
        assert row is not None
        row.authority_tier = None
        row.effective_from = None
        await s.commit()

        _docs, _pending, fp_cleared = await mind_map_service._load_project_scope_snapshot(
            s,
            principal=_principal(project_id=pid),
            project_id=pid,
        )
        snap = await s.scalar(select(ProjectMindMapSnapshot))
        assert snap is not None
        saved_citations = snap.citations_json

    assert fp_cleared == legacy_fp
    assert (await mind_map_p1_env.client.get(url, headers=headers)).json()["hasSourceUpdates"] is True

    # 6. Frozen MindMapOut / MindMapCitationOut (extra="forbid") and citations_json remain untouched
    validated_out = MindMapOut.model_validate(r_ref_eff1.json())
    assert len(validated_out.citations) == 1
    assert len(saved_citations) == 1
    assert "authorityTier" not in saved_citations[0]
    assert "effectiveFrom" not in saved_citations[0]
    MindMapCitationOut.model_validate(saved_citations[0])
    with pytest.raises(PydanticValidationError):
        MindMapCitationOut.model_validate({**saved_citations[0], "authorityTier": 1})


def test_handover_render_evidence_catalog_appends_superseded_only_to_header() -> None:
    src_ref = SourceRefPayload(kind="PROJECT_DOCUMENT", id="851a2b3c4d5e6f7a8b9c0d31", version=1)
    raw_content = "Legacy deployment checklist step: manual nginx reload."
    active_unit = handover_assist_service._EvidenceUnit(
        index=1,
        source=src_ref,
        title="Deployment Guide",
        locator_kind="CHUNK",
        locator_value="su-active-1",
        heading="Rollout",
        content=raw_content,
        is_superseded=False,
    )
    superseded_unit = handover_assist_service._EvidenceUnit(
        index=2,
        source=src_ref,
        title="Deployment Guide",
        locator_kind="CHUNK",
        locator_value="su-old-1",
        heading="Rollout",
        content=raw_content,
        is_superseded=True,
    )

    rendered = handover_assist_service._render_evidence_catalog([active_unit, superseded_unit])
    blocks = rendered.split("\n\n")
    assert len(blocks) == 2

    active_header, active_body = blocks[0].split("\n", 1)
    expected_legacy_header = (
        '[1] source=PROJECT_DOCUMENT:851a2b3c4d5e6f7a8b9c0d31@v1 '
        '| title="Deployment Guide" | locator=CHUNK:su-active-1 | heading="Rollout"'
    )
    assert active_header == expected_legacy_header
    assert active_body == raw_content

    sup_header, sup_body = blocks[1].split("\n", 1)
    assert sup_header == (
        '[2] source=PROJECT_DOCUMENT:851a2b3c4d5e6f7a8b9c0d31@v1 '
        '| title="Deployment Guide" | locator=CHUNK:su-old-1 | heading="Rollout" | status=[SUPERSEDED]'
    )
    # Content MUST stay completely raw (never mutated!)
    assert sup_body == raw_content


@pytest.mark.asyncio
async def test_handover_qa_retains_version_superseded_units_annotates_prompt_and_keeps_strict_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Document.__table__,
                DocumentVersion.__table__,
                CanonicalBlock.__table__,
                SearchUnit.__table__,
                SourceProjectMapping.__table__,
                Job.__table__,
                KnowledgeConflictResolution.__table__,
            ],
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id = "651a2b3c4d5e6f7a8b9c0d10"
    project_id = "751a2b3c4d5e6f7a8b9c0d20"
    now_utc = datetime.now(UTC)

    superseded_text = "Pinned v1 cutover procedure: pause queue workers for 10 minutes."
    active_text = "Current v2 monitoring policy: alert pager on 5xx > 1%."

    async with session_factory() as db:
        doc_old = Document(
            id="doc-old-cutover",
            tenant_id=org_id,
            project_id=project_id,
            logical_source_id="continuum-doc-cutover",
            filename="cutover.md",
            storage_path="/tmp/cutover.md",
            is_active=True,
        )
        ver_old_superseded = DocumentVersion(
            id="ver-cutover-v1",
            document_id=doc_old.id,
            version_no=1,
            file_hash="1" * 64,
            status="READY",
            search_status="SEARCH_READY",
            valid_from=now_utc - timedelta(days=90),
            valid_to=now_utc - timedelta(days=5),  # Superseded (valid_to <= now_utc)
        )
        ver_old_current = DocumentVersion(
            id="ver-cutover-v2",
            document_id=doc_old.id,
            version_no=2,
            file_hash="2" * 64,
            status="READY",
            search_status="SEARCH_READY",
            valid_from=now_utc - timedelta(days=5),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        doc_live = Document(
            id="doc-live-monitoring",
            tenant_id=org_id,
            project_id=project_id,
            logical_source_id="continuum-doc-monitoring",
            filename="monitoring.md",
            storage_path="/tmp/monitoring.md",
            is_active=True,
        )
        ver_live = DocumentVersion(
            id="ver-monitoring-v1",
            document_id=doc_live.id,
            version_no=1,
            file_hash="3" * 64,
            status="READY",
            search_status="READY",
            valid_from=now_utc - timedelta(days=10),
            valid_to=datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
        )
        blk_old = CanonicalBlock(
            id="blk-cutover-v1",
            document_version_id=ver_old_superseded.id,
            ordinal=0,
            block_type="paragraph",
            page_from=1,
            page_to=1,
            section_path="Cutover Steps",
            normalized_text=superseded_text,
            content_hash=hashlib.sha256(superseded_text.encode("utf-8")).hexdigest(),
        )
        su_old = SearchUnit(
            id="su-cutover-v1",
            document_version_id=ver_old_superseded.id,
            block_from_id=blk_old.id,
            block_to_id=blk_old.id,
            security_partition_id=project_id,
            content_hash=blk_old.content_hash,
            token_count=12,
            page_from=1,
            page_to=1,
            section_path="Cutover Steps",
        )
        blk_live = CanonicalBlock(
            id="blk-monitoring-v1",
            document_version_id=ver_live.id,
            ordinal=0,
            block_type="paragraph",
            page_from=2,
            page_to=2,
            section_path="Alerting",
            normalized_text=active_text,
            content_hash=hashlib.sha256(active_text.encode("utf-8")).hexdigest(),
        )
        su_live = SearchUnit(
            id="su-monitoring-v1",
            document_version_id=ver_live.id,
            block_from_id=blk_live.id,
            block_to_id=blk_live.id,
            security_partition_id=project_id,
            content_hash=blk_live.content_hash,
            token_count=12,
            page_from=2,
            page_to=2,
            section_path="Alerting",
        )
        db.add_all(
            [
                doc_old,
                ver_old_superseded,
                ver_old_current,
                doc_live,
                ver_live,
                blk_old,
                su_old,
                blk_live,
                su_live,
            ]
        )
        await db.commit()

    # 1. Verify _load_indexed_units preserves 4-tuple return contract and flags superseded unit key
    async with session_factory() as db:
        sup_keys: set[tuple[str, int, str, str]] = set()
        loaded = await handover_assist_service._load_indexed_units(
            db,
            principal=_principal(org_id=org_id, project_id=project_id),
            project_id=project_id,
            task="QUESTION_ANSWER",
            sources=[
                HandoverAssistSourceInput(
                    kind="PROJECT_DOCUMENT",
                    id="851a2b3c4d5e6f7a8b9c0d31",
                    version=1,
                    title="Cutover Runbook",
                    logicalSourceId="continuum-doc-cutover",
                ),
                HandoverAssistSourceInput(
                    kind="PROJECT_DOCUMENT",
                    id="851a2b3c4d5e6f7a8b9c0d32",
                    version=1,
                    title="Monitoring Policy",
                    logicalSourceId="continuum-doc-monitoring",
                ),
            ],
            superseded_unit_keys=sup_keys,
        )
    assert len(loaded[("continuum-doc-cutover", 1)]) == 1
    assert len(loaded[("continuum-doc-cutover", 1)][0]) == 4
    assert ("continuum-doc-cutover", 1, "CHUNK", "su-cutover-v1") in sup_keys
    assert ("continuum-doc-monitoring", 1, "CHUNK", "su-monitoring-v1") not in sup_keys

    # 2. Full HTTP QUESTION_ANSWER flow against real SQLite
    llm = _FakeLLM(
        json.dumps(
            {
                "answerStatus": "ANSWERED",
                "message": "Answered from frozen handover package.",
                "sections": [
                    {
                        "text": "The frozen v1 cutover runbook (marked superseded) pauses workers for 10 minutes [1].",
                        "evidenceIndices": [1, 2],
                    }
                ],
            }
        )
    )
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_err(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)
    app.dependency_overrides[get_llm] = lambda: llm

    async def _override_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                f"/api/v1/projects/{project_id}/handover/assist",
                headers=_bff_headers(org_id=org_id, project_id=project_id),
                json={
                    "task": "QUESTION_ANSWER",
                    "query": "What are the cutover and alerting steps?",
                    "sources": [
                        {
                            "kind": "PROJECT_DOCUMENT",
                            "id": "851a2b3c4d5e6f7a8b9c0d31",
                            "version": 1,
                            "title": "Cutover Runbook",
                            "logicalSourceId": "continuum-doc-cutover",
                            "excerpts": [],
                        },
                        {
                            "kind": "PROJECT_DOCUMENT",
                            "id": "851a2b3c4d5e6f7a8b9c0d32",
                            "version": 1,
                            "title": "Monitoring Policy",
                            "logicalSourceId": "continuum-doc-monitoring",
                            "excerpts": [],
                        },
                    ],
                },
            )
        assert res.status_code == 200
        body = res.json()
        parsed_resp = HandoverAssistResponse.model_validate(body)
        assert parsed_resp.answerStatus == "ANSWERED"
        assert len(parsed_resp.sections) == 1
        assert [c.locator.value for c in parsed_resp.sections[0].citations] == [
            "su-cutover-v1",
            "su-monitoring-v1",
        ]

        # Strict extra="forbid" schema remains completely untouched
        with pytest.raises(PydanticValidationError):
            HandoverAssistResponse.model_validate({**body, "isSuperseded": True})

        assert len(llm.calls) == 1
        system_prompt = llm.calls[0]["messages"][0]["content"]
        user_prompt = llm.calls[0]["messages"][1]["content"]

        # System prompt instructs LLM to note [SUPERSEDED] frozen excerpts
        assert "[SUPERSEDED]" in system_prompt
        assert "superseded" in system_prompt.lower()

        # User prompt retains pinned v1 with header suffix | status=[SUPERSEDED], raw content untouched
        expected_sup_block = (
            '[1] source=PROJECT_DOCUMENT:851a2b3c4d5e6f7a8b9c0d31@v1 '
            '| title="Cutover Runbook" | locator=CHUNK:su-cutover-v1 '
            '| heading="Cutover Steps" | status=[SUPERSEDED]\n'
            f"{superseded_text}"
        )
        expected_live_block = (
            '[2] source=PROJECT_DOCUMENT:851a2b3c4d5e6f7a8b9c0d32@v1 '
            '| title="Monitoring Policy" | locator=CHUNK:su-monitoring-v1 '
            '| heading="Alerting"\n'
            f"{active_text}"
        )
        assert expected_sup_block in user_prompt
        assert expected_live_block in user_prompt
        assert f"{active_text}" in user_prompt
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_handover_qa_works_with_single_execute_minimal_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserves compatibility with test_handover_assist.py's SimpleNamespace(execute=AsyncMock) double."""
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")

    execute_mock = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    llm = _FakeLLM(
        json.dumps(
            {
                "answerStatus": "ANSWERED",
                "message": "Grounded.",
                "sections": [{"text": "Verify readiness for 15 minutes [1].", "evidenceIndices": [1]}],
            }
        )
    )
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_err(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)
    app.dependency_overrides[get_llm] = lambda: llm

    async def _fake_session():
        yield SimpleNamespace(execute=execute_mock)

    app.dependency_overrides[get_session] = _fake_session

    project_id = "751a2b3c4d5e6f7a8b9c0d20"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/projects/{project_id}/handover/assist",
            headers=_bff_headers(org_id="651a2b3c4d5e6f7a8b9c0d10", project_id=project_id),
            json={
                "task": "QUESTION_ANSWER",
                "query": "How long to verify readiness?",
                "sources": [
                    {
                        "kind": "PROJECT_DOCUMENT",
                        "id": "851a2b3c4d5e6f7a8b9c0d31",
                        "version": 1,
                        "title": "Readiness Guide",
                        "logicalSourceId": "continuum-doc-readiness",
                        "excerpts": [
                            {
                                "locatorKind": "PAGE",
                                "locatorValue": "2",
                                "heading": "Checks",
                                "content": "Verify readiness for 15 minutes.",
                            }
                        ],
                    }
                ],
            },
        )
    assert res.status_code == 200
    assert res.json()["answerStatus"] == "ANSWERED"
    # Single execute() call on empty result — no extra .scalars() / .scalar() queries!
    assert execute_mock.await_count == 1
    assert "[SUPERSEDED]" in llm.calls[0]["messages"][0]["content"]
    assert "status=[SUPERSEDED]" not in llm.calls[0]["messages"][1]["content"]
