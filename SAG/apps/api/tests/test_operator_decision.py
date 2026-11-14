from hashlib import sha256
from time import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from sag_api.core import operator
from sag_api.core.config import settings
from sag_api.core.errors import ApiError, ForbiddenError


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signed(monkeypatch, key):
    credential = "fixture-service-credential".ljust(40, "0")
    for field, value in {
        "auth_mode": "continuum",
        "operator_bff_credential": credential,
        "operator_instance": "fixture",
        "operator_environment": "test",
        "operator_issuer": "fixture-iam",
        "operator_public_key": key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
    }.items():
        monkeypatch.setattr(settings, field, value)

    def headers(action, permission, **changes):
        now = int(time())
        claims = {
            "iss": "fixture-iam",
            "aud": "sag-platform-operator",
            "sub": "a" * 24,
            "sid": str(uuid4()),
            "jti": str(uuid4()),
            "iat": now,
            "exp": now + 60,
            "action": action,
            "permission": permission,
            "client_id": "continuum-bff",
            "instance": "fixture",
            "environment": "test",
            "cnf": {"credential_sha256": sha256(credential.encode()).hexdigest()},
            "mfa_until": now + 60,
            **changes,
        }
        token = jwt.encode(claims, key, algorithm="RS256", headers={"typ": "sag-operator+jwt"})
        return {"x-sag-operator-service": credential, "x-sag-operator-assertion": token}

    return headers


@pytest.mark.parametrize(("route", "grant"), list(operator.OPERATIONS.items()))
async def test_authorized_route_and_scope(route, grant, signed, monkeypatch):
    method, suffix = route
    path = "/api/v1/system" + suffix
    app = FastAPI()
    calls = []

    @app.api_route(path, methods=[method])
    async def business(subject: str = Depends(operator.require_platform_configuration_operator)):
        calls.append(subject)
        return {"ok": True}

    @app.exception_handler(ApiError)
    async def denied(_, exc):
        return JSONResponse({}, status_code=exc.status_code)

    decision = AsyncMock()
    monkeypatch.setattr(operator, "current_decision", decision)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://fixture") as client:

        async def send(headers):
            return (await client.request(method, path, headers=headers)).status_code

        assert await send(signed(*grant)) == 200
        for changes in [
            {"instance": "other"},
            {"environment": "other"},
            {"aud": "browser"},
            {"exp": int(time()) + 310},
            {"client_id": "other"},
        ]:
            assert await send(signed(*grant, **changes)) == 403
        assert await send(signed(grant[0], "platform.ai.operations.read")) == 403
        assert await send({**signed(*grant), "x-sag-operator-service": "wrong"}) == 403
        if grant[0] in {"dsh-export", "dsh-rotate"}:
            decision.assert_awaited_once()
            decision.side_effect = ForbiddenError("revoked")
            assert await send(signed(*grant)) == 403
        else:
            decision.assert_not_awaited()
        assert calls == ["a" * 24]
