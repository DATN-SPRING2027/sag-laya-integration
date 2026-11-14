"""Continuum platform authorization, separate from project and connector sessions."""

import hashlib
import hmac
import re
from time import time
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import Request

from sag_api.core.config import settings
from sag_api.core.errors import ForbiddenError, ServiceUnavailableError
from sag_api.core.logging import get_logger

log = get_logger("operator")
OPERATIONS = {
    ("PUT", "/preferences"): ("preferences-update", "sag.preferences.write"),
    ("PUT", "/model-config"): ("model-update", "sag.ai.config.write"),
    ("POST", "/model-setup/302"): ("setup-302", "sag.ai.config.write"),
    ("POST", "/model-config/mineru/302"): ("mineru-302", "sag.ai.config.write"),
    ("POST", "/model-config/test"): ("provider-test", "sag.ai.provider.test"),
    ("PUT", "/dsh/settings"): ("dsh-settings", "sag.dsh.settings.write"),
    ("GET", "/dsh/export"): ("dsh-export", "sag.dsh.credential.export"),
    ("POST", "/dsh/regenerate"): ("dsh-rotate", "sag.dsh.credential.rotate"),
}


async def current_decision(assertion: str, claims: dict) -> None:
    url = settings.operator_decision_url or ""
    peer = urlsplit(url)
    secret = settings.operator_decision_credential or ""
    if peer.scheme != "https" or not peer.hostname or peer.username or peer.password or len(secret) < 32:
        raise ServiceUnavailableError("Operator IAM decision unavailable")
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            result = await client.post(
                url, headers={"Authorization": f"Bearer {secret}"}, json={"assertion": assertion}
            )
        if result.status_code != 200:
            raise ForbiddenError("Operator IAM decision denied")
        decision = result.json()
    except (ValueError, httpx.HTTPError):
        raise ServiceUnavailableError("Operator IAM decision unavailable") from None
    if (
        not isinstance(decision, dict)
        or decision.get("allowed") is not True
        or any(decision.get(k) != claims[k] for k in ("jti", "action", "instance", "environment"))
        or time() >= min(claims["exp"], claims["mfa_until"])
    ):
        raise ForbiddenError("Operator IAM decision denied")


async def require_platform_configuration_operator(request: Request) -> str:
    path = request.url.path.removeprefix(request.scope.get("root_path") or "").removeprefix("/api/v1/system")
    operation = OPERATIONS.get((request.method, path))
    if not operation or settings.auth_mode != "continuum":
        raise ForbiddenError("Operator access denied")
    action, permission = operation
    credentials = request.headers.getlist("x-sag-operator-service")
    assertions = request.headers.getlist("x-sag-operator-assertion")
    expected = settings.operator_bff_credential or ""
    if (
        len(expected) < 32
        or len(credentials) != 1
        or len(assertions) != 1
        or len(assertions[0]) > 8192
        or not hmac.compare_digest(expected.encode(), credentials[0].encode())
    ):
        raise ForbiddenError("Operator access denied")
    if not all(
        (
            settings.operator_public_key,
            settings.operator_issuer,
            settings.operator_instance,
            settings.operator_environment,
        )
    ):
        raise ServiceUnavailableError("Operator IAM trust unavailable")
    token = assertions[0]
    try:
        if jwt.get_unverified_header(token).get("typ") != "sag-operator+jwt":
            raise ValueError
        claims = jwt.decode(
            token,
            settings.operator_public_key.replace("\\n", "\n"),
            algorithms=["RS256"],
            issuer=settings.operator_issuer,
            audience="sag-platform-operator",
            options={"strict_aud": True, "require": ["exp", "iat", "iss", "aud", "sub", "sid", "jti"]},
        )
        if (
            type(claims["exp"]) is not int
            or type(claims["iat"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= 300
            or not re.fullmatch(r"[0-9a-f]{24}", claims["sub"])
            or not re.fullmatch(r"[a-f0-9-]{36}", claims["jti"])
            or claims.get("instance") != settings.operator_instance
            or claims.get("environment") != settings.operator_environment
            or claims.get("client_id") != "continuum-bff"
            or claims.get("action") != action
            or claims.get("permission") != permission
            or claims.get("cnf") != {"credential_sha256": hashlib.sha256(expected.encode()).hexdigest()}
        ):
            raise ValueError
    except (jwt.PyJWTError, TypeError, ValueError):
        raise ForbiddenError("Operator access denied") from None
    if action in {"dsh-export", "dsh-rotate"}:
        if type(claims.get("mfa_until")) is not int or claims["mfa_until"] <= time():
            raise ForbiddenError("Verified recent MFA required")
        await current_decision(token, claims)
    log.info(
        "operator_authorized actor=%s service=continuum-bff instance=%s environment=%s action=%s decision_id=%s",
        claims["sub"],
        settings.operator_instance,
        settings.operator_environment,
        action,
        claims["jti"],
    )
    return claims["sub"]
