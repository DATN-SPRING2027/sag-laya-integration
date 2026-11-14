"""Behavioral and security tests for the service-only SAG platform operations endpoint."""

from __future__ import annotations

import json
import secrets
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1 import api_router
from sag_api.core.config import settings
from sag_api.core.deps import get_session
from sag_api.core.errors import ApiError
from sag_api.core.security import create_access_token
from sag_api.db.base import Base
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType

ENDPOINT = "/api/v1/platform/ai/operations"
VALID_SERVICE_TOKEN = "s" * 40


class _DummyQdrantClient:
    def __init__(self, status_code: int = 200) -> None:
        self._status_code = status_code

    async def get(self, _path: str, timeout: float = 3.0):  # noqa: ARG002
        return SimpleNamespace(status_code=self._status_code)


class _DummyEngineManager:
    def __init__(self, status_code: int = 200) -> None:
        self._client = _DummyQdrantClient(status_code=status_code)

    async def get_search_unit_qdrant_client(self):
        return self._client


@pytest.fixture
async def platform_ops_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setattr(settings, "bff_service_token", VALID_SERVICE_TOKEN)
    monkeypatch.setattr(settings, "llm_api_key", "sk-secret-do-not-leak-llm-key")
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "Qwen/Qwen3-8B")
    monkeypatch.setattr(settings, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setattr(settings, "sag_vector_provider", "qdrant")
    monkeypatch.setattr(settings, "document_parser", "auto")
    monkeypatch.setattr(settings, "mineru_base_url", "https://mineru.net")
    monkeypatch.setattr(settings, "mineru_official_api_token", "mineru-secret-token")
    monkeypatch.setattr(settings, "job_concurrency", 4)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()

    @app.exception_handler(ApiError)
    async def _api_err(_req: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    app.include_router(api_router)
    app.state.knowledge_runtime = SimpleNamespace(ready=True)
    app.state.engine_manager = _DummyEngineManager(status_code=200)

    async def _override_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield SimpleNamespace(
                app=app,
                client=client,
                sessions=session_factory,
            )
    finally:
        await engine.dispose()


def _user_bearer_headers() -> dict[str, str]:
    token = create_access_token(
        "651a2b3c4d5e6f7a8b9c0d01",
        extra={
            "org_id": "org_alpha",
            "tenant_id": "org_alpha",
            "project_id": "proj_alpha",
            "security_partition": "proj_alpha",
            "jti": secrets.token_urlsafe(16),
        },
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_platform_operations_enforces_service_token_and_fail_closed(
    platform_ops_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c: httpx.AsyncClient = platform_ops_env.client

    # Unauthenticated -> 403
    unauth = await c.get(ENDPOINT)
    assert unauth.status_code == 403

    # Ordinary user JWT without BFF service token -> 403
    user_jwt = await c.get(ENDPOINT, headers=_user_bearer_headers())
    assert user_jwt.status_code == 403

    # Wrong service token -> 403
    wrong = await c.get(ENDPOINT, headers={"X-SAG-BFF-Service-Token": "wrong-token"})
    assert wrong.status_code == 403

    # Unconfigured service token on SAG -> 503
    monkeypatch.setattr(settings, "bff_service_token", None)
    unconfigured = await c.get(
        ENDPOINT,
        headers={"X-SAG-BFF-Service-Token": VALID_SERVICE_TOKEN},
    )
    assert unconfigured.status_code == 503


@pytest.mark.asyncio
async def test_platform_operations_rejects_query_parameters(platform_ops_env) -> None:
    c: httpx.AsyncClient = platform_ops_env.client
    res = await c.get(
        f"{ENDPOINT}?organizationId=org_alpha",
        headers={"X-SAG-BFF-Service-Token": VALID_SERVICE_TOKEN},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_platform_operations_aggregates_jobs_and_active_corpus_without_leaks(
    platform_ops_env,
) -> None:
    c: httpx.AsyncClient = platform_ops_env.client
    async with platform_ops_env.sessions() as s:
        s.add(
            Source(
                id="src_1",
                name="Confidential Client Docs",
                sag_source_config_id="cfg_1",
            )
        )
        await s.flush()
        s.add_all(
            [
                Document(
                    id="doc_ready_1",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="secret-contract.pdf",
                    storage_path="/vault/secret-contract.pdf",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc_ready_2",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="secret-board-deck.pdf",
                    storage_path="/vault/secret-board-deck.pdf",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc_proc_1",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="pending-file.pdf",
                    storage_path="/vault/pending-file.pdf",
                    status=DocumentStatus.PENDING,
                    is_active=True,
                ),
                Document(
                    id="doc_proc_2",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="loading-file.pdf",
                    storage_path="/vault/loading-file.pdf",
                    status=DocumentStatus.LOADING,
                    is_active=True,
                ),
                Document(
                    id="doc_proc_3",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="extracting-file.pdf",
                    storage_path="/vault/extracting-file.pdf",
                    status=DocumentStatus.EXTRACTING,
                    is_active=True,
                ),
                Document(
                    id="doc_proc_4",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="pausing-file.pdf",
                    storage_path="/vault/pausing-file.pdf",
                    status=DocumentStatus.PAUSING,
                    is_active=True,
                ),
                Document(
                    id="doc_paused_1",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="paused-file.pdf",
                    storage_path="/vault/paused-file.pdf",
                    status=DocumentStatus.PAUSED,
                    is_active=True,
                ),
                Document(
                    id="doc_failed_1",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="failed-file.pdf",
                    storage_path="/vault/failed-file.pdf",
                    status=DocumentStatus.FAILED,
                    error="raw upstream exception message",
                    is_active=True,
                ),
                Document(
                    id="doc_failed_2",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="delete-failed-file.pdf",
                    storage_path="/vault/delete-failed-file.pdf",
                    status=DocumentStatus.DELETE_FAILED,
                    is_active=True,
                ),
                # Must be ignored: DELETING active document
                Document(
                    id="doc_deleting",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="deleting-file.pdf",
                    storage_path="/vault/deleting-file.pdf",
                    status=DocumentStatus.DELETING,
                    is_active=True,
                ),
                # Must be ignored: revoked / inactive document
                Document(
                    id="doc_inactive",
                    tenant_id="org_secret",
                    project_id="proj_secret",
                    source_id="src_1",
                    filename="revoked-file.pdf",
                    storage_path="/vault/revoked-file.pdf",
                    status=DocumentStatus.READY,
                    is_active=False,
                ),
            ]
        )
        await s.flush()
        s.add_all(
            [
                Job(id="j_q1", type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED, source_id="src_1"),
                Job(id="j_r1", type=JobType.PROCESS_DOCUMENT, status=JobStatus.RUNNING, source_id="src_1"),
                Job(id="j_r2", type=JobType.REPROCESS_DOCUMENT, status=JobStatus.RUNNING, source_id="src_1"),
                Job(id="j_p1", type=JobType.PROCESS_DOCUMENT, status=JobStatus.PAUSED, source_id="src_1"),
                Job(id="j_s1", type=JobType.PROCESS_DOCUMENT, status=JobStatus.SUCCEEDED, source_id="src_1"),
                Job(id="j_s2", type=JobType.PROCESS_DOCUMENT, status=JobStatus.SUCCEEDED, source_id="src_1"),
                Job(id="j_s3", type=JobType.SYNC_SOURCE, status=JobStatus.SUCCEEDED, source_id="src_1"),
                Job(
                    id="j_f1",
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.FAILED,
                    source_id="src_1",
                    error="stacktrace apiKey=sk-leak",
                ),
            ]
        )
        await s.commit()

    res = await c.get(
        ENDPOINT,
        headers={"X-SAG-BFF-Service-Token": VALID_SERVICE_TOKEN},
    )
    assert res.status_code == 200
    assert res.headers.get("cache-control") == "no-store"

    body = res.json()
    assert body == {
        "status": "ready",
        "db_ready": True,
        "knowledge_runtime_ready": True,
        "qdrant_ready": True,
        "runtime": {
            "llm_configured": True,
            "llm_provider": "openai",
            "llm_model": "Qwen/Qwen3-8B",
            "retrieval_model": "Qwen/Qwen3-Embedding-0.6B",
            "vector_store": "qdrant",
            "parser_provider": "mineru",
            "worker_concurrency": 4,
        },
        "jobs": {
            "queued": 1,
            "running": 2,
            "paused": 1,
            "succeeded": 3,
            "failed": 1,
        },
        "corpus": {
            "ready": 2,
            "processing": 4,
            "paused": 1,
            "failed": 2,
        },
    }

    serialized = json.dumps(body)
    for forbidden in (
        "secret-contract.pdf",
        "org_secret",
        "proj_secret",
        "sk-secret",
        "sk-leak",
        "mineru-secret-token",
        "raw upstream exception",
        "http://",
        "https://",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_platform_operations_sanitizes_unsafe_config_strings_and_reports_degraded(
    platform_ops_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "https://user:pass@internal.llm/v1")
    monkeypatch.setattr(settings, "llm_model", "api_key=should-redact")
    monkeypatch.setattr(settings, "embedding_model", "model with spaces\ninjected")
    platform_ops_env.app.state.knowledge_runtime = SimpleNamespace(ready=False)
    platform_ops_env.app.state.engine_manager = _DummyEngineManager(status_code=503)

    c: httpx.AsyncClient = platform_ops_env.client
    res = await c.get(
        ENDPOINT,
        headers={"X-SAG-BFF-Service-Token": VALID_SERVICE_TOKEN},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["db_ready"] is True
    assert body["knowledge_runtime_ready"] is False
    assert body["qdrant_ready"] is False
    assert body["runtime"]["llm_provider"] is None
    assert body["runtime"]["llm_model"] is None
    assert body["runtime"]["retrieval_model"] is None
    assert body["runtime"]["vector_store"] == "qdrant"
