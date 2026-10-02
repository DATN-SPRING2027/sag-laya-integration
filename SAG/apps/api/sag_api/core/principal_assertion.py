"""Verify Continuum-signed assertions used to authorize SAG evidence access."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from starlette.requests import Request

from sag_api.core.config import settings
from sag_api.core.errors import AuthError, ServiceUnavailableError
from sag_api.core.logging import get_logger

log = get_logger("security.principal_assertion")
MAX_ASSERTION_BYTES = 16 * 1024
MAX_JWKS_BYTES = 256 * 1024
MAX_JWKS_KEYS = 256
JWKS_FETCH_TIMEOUT_SECONDS = 3.0
UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 1
_PRIVATE_RSA_JWK_MEMBERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
_FORBIDDEN_SCOPE_ALIASES = frozenset(
    {
        "organizationId",
        "organization_id",
        "org_id",
        "tenant_id",
        "projectId",
        "project_id",
        "projectIds",
        "project_ids",
        "allowed_project_ids",
        "allowedProjects",
        "activeProjectId",
        "active_project_id",
        "sourceId",
        "source_id",
        "sourceIds",
        "source_ids",
        "authorizedSourceIds",
        "authorized_source_ids",
        "documentId",
        "document_id",
        "documentIds",
        "document_ids",
        "allowed_partition_ids",
        "securityPartitionIds",
        "security_partition_ids",
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedPrincipal:
    subject: str
    organization_id: str
    allowed_project_ids: frozenset[str]
    issuer: str
    key_id: str
    token_id: str
    issued_at: int
    expires_at: int
    tenant_id: str = ""
    allowed_partition_ids: frozenset[str] = frozenset()


JwksLoader = Callable[[str], Awaitable[dict[str, Any]]]


async def _load_remote_jwks(url: str) -> dict[str, Any]:
    client = _jwks_client
    if client is None:
        raise ServiceUnavailableError("Principal verification key client is not initialized")
    try:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_JWKS_BYTES:
                    raise ServiceUnavailableError(
                        "Principal verification key set exceeds the configured size limit"
                    )
    except httpx.HTTPError as error:
        raise ServiceUnavailableError("Principal verification keys are unavailable") from error
    try:
        document = json.loads(body)
    except ValueError as error:
        raise ServiceUnavailableError("Principal verification key set is malformed") from error
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ServiceUnavailableError("Principal verification key set is malformed")
    if len(document["keys"]) > MAX_JWKS_KEYS:
        raise ServiceUnavailableError("Principal verification key set exceeds the configured key limit")
    return document


class PrincipalAssertionVerifier:
    """Asynchronous RS256/JWKS verifier with bounded caching and one key refresh."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        max_lifetime_seconds: int = 120,
        clock_skew_seconds: int = 5,
        cache_ttl_seconds: int = 60,
        max_project_ids: int = 256,
        jwks_loader: JwksLoader = _load_remote_jwks,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer = issuer.strip()
        self.audience = audience.strip()
        self.jwks_url = jwks_url.strip()
        self.max_lifetime_seconds = max_lifetime_seconds
        self.clock_skew_seconds = clock_skew_seconds
        self.cache_ttl_seconds = min(max(cache_ttl_seconds, 1), 60)
        self.max_project_ids = max_project_ids
        self._jwks_loader = jwks_loader
        self._clock = clock
        self._keys: dict[str, RSAPublicKey] = {}
        self._cache_expires_at = 0.0
        self._last_unknown_kid_refresh_at = float("-inf")
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[dict[str, RSAPublicKey]] | None = None

    async def verify(self, token: str) -> VerifiedPrincipal:
        if not token or len(token.encode("utf-8")) > MAX_ASSERTION_BYTES:
            raise AuthError("Principal assertion is missing or invalid")
        if not self.issuer or not self.audience or not self.jwks_url:
            raise ServiceUnavailableError("Principal assertion verification is not configured")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise AuthError("Principal assertion is malformed") from error
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AuthError("Principal assertion signing key is not allowed")
        key_id = header["kid"].strip()
        if not key_id or len(key_id) > 256 or header.get("jku") or header.get("x5u"):
            raise AuthError("Principal assertion signing key is not allowed")

        keys = await self._get_keys()
        public_key = keys.get(key_id)
        if public_key is None:
            keys = await self._get_keys(force_refresh=True)
            public_key = keys.get(key_id)
        if public_key is None:
            log.warning("principal assertion denied reason=unknown_kid")
            raise AuthError("Principal assertion signing key is unknown")

        try:
            if public_key.key_size < 2048:
                raise AuthError("Principal assertion signing key is too weak")
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["iss", "aud", "sub", "orgId", "allowedProjectIds", "iat", "exp", "jti"],
                    "strict_aud": True,
                },
                leeway=self.clock_skew_seconds,
            )
        except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as error:
            log.warning("principal assertion denied reason=verification_failed")
            raise AuthError("Principal assertion verification failed") from error

        now = int(self._clock())
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        subject = claims.get("sub")
        organization_id = claims.get("orgId")
        project_ids = claims.get("allowedProjectIds")
        tenant_id = claims.get("tenantId", "")
        partition_ids = claims.get("allowedPartitionIds", [])
        token_id = claims.get("jti")
        if _FORBIDDEN_SCOPE_ALIASES.intersection(claims):
            raise AuthError("Principal assertion contains unsupported scope aliases")
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or not isinstance(expires_at, int)
            or isinstance(expires_at, bool)
            or issued_at > now + self.clock_skew_seconds
            or expires_at <= now - self.clock_skew_seconds
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_lifetime_seconds
        ):
            raise AuthError("Principal assertion lifetime is invalid")
        if not self._valid_identifier(subject) or not self._valid_identifier(organization_id):
            raise AuthError("Principal assertion identity is invalid")
        if "tenantId" in claims and (
            not self._valid_identifier(tenant_id) or len(tenant_id) > 64
        ):
            raise AuthError("Principal assertion Tenant scope is invalid")
        if (
            not isinstance(project_ids, list)
            or len(project_ids) > self.max_project_ids
            or any(not self._valid_identifier(project_id) for project_id in project_ids)
            or len(set(project_ids)) != len(project_ids)
        ):
            raise AuthError("Principal assertion Project scope is invalid")
        if (
            not isinstance(partition_ids, list)
            or len(partition_ids) > self.max_project_ids
            or any(not self._valid_identifier(partition_id) for partition_id in partition_ids)
            or len(set(partition_ids)) != len(partition_ids)
        ):
            raise AuthError("Principal assertion partition scope is invalid")
        if not self._valid_identifier(token_id):
            raise AuthError("Principal assertion token identifier is invalid")

        principal = VerifiedPrincipal(
            subject=subject,
            organization_id=organization_id,
            allowed_project_ids=frozenset(project_ids),
            issuer=self.issuer,
            key_id=key_id,
            token_id=token_id,
            issued_at=issued_at,
            expires_at=expires_at,
            tenant_id=tenant_id,
            allowed_partition_ids=frozenset(partition_ids),
        )
        log.info(
            "principal assertion verified issuer=%s kid=%s allowed_project_count=%d",
            self.issuer,
            key_id,
            len(principal.allowed_project_ids),
        )
        return principal

    @staticmethod
    def _valid_identifier(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= 256

    async def _get_keys(self, *, force_refresh: bool = False) -> dict[str, RSAPublicKey]:
        now = self._clock()
        if not force_refresh and self._keys and now < self._cache_expires_at:
            return self._keys
        async with self._lock:
            now = self._clock()
            if not force_refresh and self._keys and now < self._cache_expires_at:
                return self._keys
            task = self._refresh_task
            if task is None:
                if (
                    force_refresh
                    and self._keys
                    and now < self._cache_expires_at
                    and now - self._last_unknown_kid_refresh_at < UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
                ):
                    return self._keys
                if force_refresh:
                    # Limit unknown-kid traffic without serving an expired cache.
                    self._last_unknown_kid_refresh_at = now
                task = self._refresh_task = asyncio.create_task(self._refresh_keys())
                # A disconnected caller must not leave an unobserved task error.
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _refresh_keys(self) -> dict[str, RSAPublicKey]:
        # Anchor TTL to refresh start, so network time does not extend trust.
        now = self._clock()
        try:
            try:
                async with asyncio.timeout(JWKS_FETCH_TIMEOUT_SECONDS):
                    document = await self._jwks_loader(self.jwks_url)
            except ServiceUnavailableError:
                raise
            except Exception as error:  # noqa: BLE001 - trust service boundary
                raise ServiceUnavailableError("Principal verification keys are unavailable") from error
            keys: dict[str, RSAPublicKey] = {}
            for item in document["keys"]:
                if not isinstance(item, dict):
                    continue
                if _PRIVATE_RSA_JWK_MEMBERS.intersection(item):
                    raise ServiceUnavailableError("Principal verification key set contains private key material")
                kid = item.get("kid")
                if (
                    isinstance(kid, str)
                    and kid
                    and item.get("kty") == "RSA"
                    and item.get("use", "sig") == "sig"
                    and item.get("alg", "RS256") == "RS256"
                    and ("key_ops" not in item or isinstance(item["key_ops"], list) and "verify" in item["key_ops"])
                ):
                    if kid in keys:
                        raise ServiceUnavailableError("Principal verification key set contains duplicate key IDs")
                    try:
                        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(item))
                    except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as error:
                        raise ServiceUnavailableError(
                            "Principal verification key set contains an invalid RSA key"
                        ) from error
                    keys[kid] = public_key
            async with self._lock:
                self._keys = keys
                self._cache_expires_at = now + self.cache_ttl_seconds
            return keys
        finally:
            async with self._lock:
                self._refresh_task = None

    async def aclose(self) -> None:
        if task := self._refresh_task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


_verifier: PrincipalAssertionVerifier | None = None
_verifier_config: tuple[str | None, str, str | None, int, int, int, int] | None = None
_jwks_client: httpx.AsyncClient | None = None
_managed_verifiers: set[PrincipalAssertionVerifier] | None = None


@asynccontextmanager
async def principal_assertion_lifespan() -> AsyncIterator[None]:
    """Reuse JWKS connections and stop shared refresh tasks before closing them."""
    global _jwks_client, _verifier, _verifier_config, _managed_verifiers
    _verifier = _verifier_config = None
    managed_verifiers: set[PrincipalAssertionVerifier] = set()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(JWKS_FETCH_TIMEOUT_SECONDS, connect=1.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        _jwks_client = client
        _managed_verifiers = managed_verifiers
        try:
            yield
        finally:
            await asyncio.gather(*(verifier.aclose() for verifier in managed_verifiers))
            _jwks_client = _managed_verifiers = None
            _verifier = _verifier_config = None


def get_principal_assertion_verifier() -> PrincipalAssertionVerifier:
    global _verifier, _verifier_config
    current_config = (
        settings.principal_assertion_issuer,
        settings.principal_assertion_audience,
        settings.principal_assertion_jwks_url,
        settings.principal_assertion_max_lifetime_seconds,
        settings.principal_assertion_clock_skew_seconds,
        settings.principal_assertion_jwks_cache_ttl_seconds,
        settings.principal_assertion_max_project_ids,
    )
    if current_config != _verifier_config:
        _verifier_config = current_config
        issuer, audience, jwks_url, lifetime, skew, ttl, max_projects = current_config
        _verifier = PrincipalAssertionVerifier(
            issuer=issuer or "",
            audience=audience,
            jwks_url=jwks_url or "",
            max_lifetime_seconds=lifetime,
            clock_skew_seconds=skew,
            cache_ttl_seconds=ttl,
            max_project_ids=max_projects,
        )
        if _managed_verifiers is not None:
            _managed_verifiers.add(_verifier)
    return _verifier


async def require_principal_assertion(request: Request) -> VerifiedPrincipal:
    assertions = request.headers.getlist("X-SAG-Principal-Assertion")
    if len(assertions) != 1 or not assertions[0]:
        raise AuthError("Missing Continuum principal assertion")
    return await get_principal_assertion_verifier().verify(assertions[0])
