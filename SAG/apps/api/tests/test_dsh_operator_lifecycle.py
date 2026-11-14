import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.api.v1 import system
from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.errors import ApiError
from sag_api.db.models import Setting
from sag_api.services import dsh_integration_service as dsh


async def test_file_bootstrap_rotation_reconnect_and_closed_http_discovery(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Setting.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    path = tmp_path / "connector.json"
    monkeypatch.setattr(settings, "dsh_connection_file", str(path))
    monkeypatch.setattr(settings, "environment", "prod")
    try:
        async with factory() as db:
            await dsh.write_connection_file(db)
            old = json.loads(path.read_text())["accessToken"]
            assert (await dsh.authenticate_connector(db, old)).id == "service:dsh-connector"
            await dsh.regenerate_token_and_publish(db)
        async with factory() as db:
            new = json.loads(path.read_text())["accessToken"]
            assert new != old
            assert await dsh.authenticate_connector(db, old) is None
            assert (await dsh.authenticate_connector(db, new)).id == "service:dsh-connector"
        app = FastAPI()
        app.include_router(system.router, prefix="/api/v1")
        app.dependency_overrides[get_session] = lambda: SimpleNamespace()

        @app.exception_handler(ApiError)
        async def deny(_, exc):
            return JSONResponse({}, status_code=exc.status_code)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="https://localhost"
        ) as client:
            response = await client.get("/api/v1/system/dsh-connection")
            assert response.status_code == 403
    finally:
        await engine.dispose()
