from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sag_api.api.v1 import api_router
from sag_api.core.config import settings
from sag_api.core.deps import get_llm, get_session
from sag_api.core.errors import ApiError, ServiceUnavailableError


def _build_app(llm_mock: object) -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)
    app.dependency_overrides[get_llm] = lambda: llm_mock

    async def _fake_session():
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))

    app.dependency_overrides[get_session] = _fake_session
    return app


def _bff_headers(
    *,
    token: str = "test-bff-token",
    user_id: str = "651a2b3c4d5e6f7a8b9c0d11",
    org_id: str = "651a2b3c4d5e6f7a8b9c0d10",
    project_id: str = "751a2b3c4d5e6f7a8b9c0d20",
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


@pytest.mark.asyncio
async def test_handover_assist_enforces_bff_token_and_project_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="{}"))
    app = _build_app(llm)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Wrong BFF token -> 403
        bad_token = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(token="wrong-token"),
            json={"task": "SOURCE_PROPOSAL", "query": "deployment", "sources": []},
        )
        assert bad_token.status_code == 403

        # Cross-project header mismatch -> 403
        cross_project = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d99/handover/assist",
            headers=_bff_headers(project_id="751a2b3c4d5e6f7a8b9c0d20"),
            json={"task": "SOURCE_PROPOSAL", "query": "deployment", "sources": []},
        )
        assert cross_project.status_code == 403


@pytest.mark.asyncio
async def test_handover_assist_rejects_task_note_in_official_proposal_and_qa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="{}"))
    app = _build_app(llm)
    task_note_source = {
        "kind": "TASK_NOTE",
        "id": "851a2b3c4d5e6f7a8b9c0d30",
        "version": 1,
        "title": "Task scratch note",
        "excerpts": [
            {
                "locatorKind": "SECTION",
                "locatorValue": "whatDone",
                "heading": "What Done",
                "content": "Refactored worker retry loop.",
            }
        ],
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        proposal_res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={"task": "SOURCE_PROPOSAL", "query": "worker retry", "sources": [task_note_source]},
        )
        assert proposal_res.status_code == 422

        qa_res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={
                "task": "QUESTION_ANSWER",
                "query": "How does worker retry work?",
                "sources": [task_note_source],
            },
        )
        assert qa_res.status_code == 422


@pytest.mark.asyncio
async def test_handover_assist_source_proposal_only_returns_authorized_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")
    llm_payload = json.dumps(
        {
            "candidates": [
                {
                    "evidenceIndex": 1,
                    "reason": "Covers production Kubernetes deployment runbook owned by departing lead.",
                },
                {
                    # Out-of-range hallucinated index must be dropped, never fabricated
                    "evidenceIndex": 99,
                    "reason": "Hallucinated source",
                },
            ]
        }
    )
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value=llm_payload))
    app = _build_app(llm)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={
                "task": "SOURCE_PROPOSAL",
                "query": "kubernetes deployment runbook",
                "sources": [
                    {
                        "kind": "PROJECT_DOCUMENT",
                        "id": "851a2b3c4d5e6f7a8b9c0d31",
                        "version": 3,
                        "title": "K8s Deployment Runbook",
                        "excerpts": [
                            {
                                "locatorKind": "SECTION",
                                "locatorValue": "rollout",
                                "heading": "Rollout Procedure",
                                "content": "Run helm upgrade with canary values and verify readiness probes.",
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["task"] == "SOURCE_PROPOSAL"
        assert len(body["candidates"]) == 1
        assert body["candidates"][0]["source"] == {
            "kind": "PROJECT_DOCUMENT",
            "id": "851a2b3c4d5e6f7a8b9c0d31",
            "version": 3,
        }


@pytest.mark.asyncio
async def test_handover_assist_draft_generation_validates_citations_strictly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")
    llm_payload = json.dumps(
        {
            "items": [
                {
                    "title": "Release Rollout & Rollback Ownership",
                    "body": "Use canary rollout via Helm and rollback immediately on readiness failure [1].",
                    "requirement": "Confirm ability to execute canary rollout and rollback procedure in staging.",
                    "evidenceIndices": [1, 999],
                }
            ]
        }
    )
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value=llm_payload))
    app = _build_app(llm)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={
                "task": "DRAFT_GENERATION",
                "query": "Summarize deployment ownership",
                "sources": [
                    {
                        "kind": "KNOWLEDGE_OBJECT",
                        "id": "851a2b3c4d5e6f7a8b9c0d32",
                        "version": 2,
                        "title": "Canary Rollout Standard",
                        "excerpts": [
                            {
                                "locatorKind": "SECTION",
                                "locatorValue": "sec-canary",
                                "heading": "Canary Rollout",
                                "content": "Helm canary rollout uses 10% traffic weight before full promotion.",
                            }
                        ],
                    }
                ],
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["task"] == "DRAFT_GENERATION"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["sources"] == [{"kind": "KNOWLEDGE_OBJECT", "id": "851a2b3c4d5e6f7a8b9c0d32", "version": 2}]
        assert item["citations"] == [
            {
                "source": {"kind": "KNOWLEDGE_OBJECT", "id": "851a2b3c4d5e6f7a8b9c0d32", "version": 2},
                "locator": {"kind": "SECTION", "value": "sec-canary"},
            }
        ]


@pytest.mark.asyncio
async def test_handover_assist_question_answer_fails_closed_on_insufficient_evidence_and_llm_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "bff_service_token", "test-bff-token")

    # 1. Empty sources -> INSUFFICIENT_EVIDENCE without calling LLM or inventing sections
    llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="{}"))
    app = _build_app(llm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        empty_res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={"task": "QUESTION_ANSWER", "query": "What is the production DB password?", "sources": []},
        )
        assert empty_res.status_code == 200
        empty_body = empty_res.json()
        assert empty_body["answerStatus"] == "INSUFFICIENT_EVIDENCE"
        assert empty_body["sections"] == []
        llm.complete.assert_not_awaited()

    # 2. Grounded ANSWERED response with verified citations
    answered_llm = SimpleNamespace(
        configured=True,
        complete=AsyncMock(
            return_value=json.dumps(
                {
                    "answerStatus": "ANSWERED",
                    "message": "Found grounded guidance in the authorized handover sources.",
                    "sections": [
                        {
                            "text": "Staging validation requires verifying readiness probes for 15 minutes [1].",
                            "evidenceIndices": [1],
                        }
                    ],
                }
            )
        ),
    )
    app_answered = _build_app(answered_llm)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_answered), base_url="http://test"
    ) as client:
        ans_res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={
                "task": "QUESTION_ANSWER",
                "query": "How long must readiness probes be verified in staging?",
                "sources": [
                    {
                        "kind": "PROJECT_DOCUMENT",
                        "id": "851a2b3c4d5e6f7a8b9c0d31",
                        "version": 3,
                        "title": "Staging Validation Guide",
                        "excerpts": [
                            {
                                "locatorKind": "PAGE",
                                "locatorValue": "4",
                                "heading": "Readiness Verification",
                                "content": "Verify readiness probes for 15 minutes in staging before promotion.",
                            }
                        ],
                    }
                ],
            },
        )
        assert ans_res.status_code == 200
        ans_body = ans_res.json()
        assert ans_body["answerStatus"] == "ANSWERED"
        assert len(ans_body["sections"]) == 1
        assert ans_body["sections"][0]["citations"] == [
            {
                "source": {"kind": "PROJECT_DOCUMENT", "id": "851a2b3c4d5e6f7a8b9c0d31", "version": 3},
                "locator": {"kind": "PAGE", "value": "4"},
            }
        ]

    # 3. Unconfigured / unavailable LLM -> propagates 503/500 error, NEVER fakes success
    failing_llm = SimpleNamespace(
        configured=False,
        complete=AsyncMock(side_effect=ServiceUnavailableError("LLM provider timeout")),
    )
    app_failing = _build_app(failing_llm)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_failing), base_url="http://test"
    ) as client:
        fail_res = await client.post(
            "/api/v1/projects/751a2b3c4d5e6f7a8b9c0d20/handover/assist",
            headers=_bff_headers(),
            json={
                "task": "DRAFT_GENERATION",
                "query": "Generate handover draft",
                "sources": [
                    {
                        "kind": "PROJECT_DOCUMENT",
                        "id": "851a2b3c4d5e6f7a8b9c0d31",
                        "version": 3,
                        "title": "Staging Validation Guide",
                        "excerpts": [
                            {
                                "locatorKind": "PAGE",
                                "locatorValue": "4",
                                "heading": "Readiness Verification",
                                "content": "Verify readiness probes for 15 minutes.",
                            }
                        ],
                    }
                ],
            },
        )
        assert fail_res.status_code in {500, 503}
