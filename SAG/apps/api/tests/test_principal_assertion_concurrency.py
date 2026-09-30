from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sag_api.core import principal_assertion
from sag_api.core.errors import AuthError, ServiceUnavailableError
from sag_api.core.principal_assertion import PrincipalAssertionVerifier


@pytest.fixture
def signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid="known-key", use="sig", alg="RS256")
    return private_key, jwk


def _token(private_key, *, kid="known-key", **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://continuum.test",
        "aud": "sag-api",
        "sub": "user-1",
        "orgId": "org-1",
        "allowedProjectIds": ["project-1"],
        "iat": now,
        "exp": now + 60,
        "jti": "concurrency-test",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _verifier(loader, **kwargs):
    return PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=loader,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_unknown_kid_refresh_does_not_hold_cache_lock_or_block_fresh_known_key(signing_key):
    private_key, jwk = signing_key
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            await release.wait()
        return {"keys": [jwk]}

    verifier = _verifier(loader)
    await verifier.verify(_token(private_key))
    refresh = asyncio.create_task(verifier.verify(_token(private_key, kid="unknown-key")))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(verifier._lock.acquire(), timeout=0.2)
        verifier._lock.release()
        principal = await asyncio.wait_for(verifier.verify(_token(private_key)), timeout=0.2)
        assert principal.key_id == "known-key"
    finally:
        release.set()
        with pytest.raises(AuthError):
            await refresh
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelling_one_waiter_keeps_shared_expired_cache_refresh(signing_key):
    private_key, jwk = signing_key
    started, release = asyncio.Event(), asyncio.Event()
    now = [time.time()]
    calls = 0

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            await release.wait()
        return {"keys": [jwk]}

    verifier = _verifier(loader, clock=lambda: now[0], cache_ttl_seconds=1)
    token = _token(private_key)
    await verifier.verify(token)
    now[0] += 2
    first = asyncio.create_task(verifier.verify(token))
    await asyncio.wait_for(started.wait(), timeout=1)
    others = [asyncio.create_task(verifier.verify(token)) for _ in range(8)]
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    try:
        release.set()
        principals = await asyncio.wait_for(asyncio.gather(*others), timeout=1)
        assert all(principal.key_id == "known-key" for principal in principals)
        assert calls == 2
    finally:
        release.set()
        await asyncio.gather(*others, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_cache_outage_is_shared_fails_closed_and_can_retry(signing_key):
    private_key, jwk = signing_key
    started, release = asyncio.Event(), asyncio.Event()
    now = [time.time()]
    calls = 0
    unavailable = True

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls > 1 and unavailable:
            started.set()
            await release.wait()
            raise httpx.ConnectError("offline")
        return {"keys": [jwk]}

    verifier = _verifier(loader, clock=lambda: now[0], cache_ttl_seconds=1)
    token = _token(private_key)
    await verifier.verify(token)
    now[0] += 2
    waiters = [asyncio.create_task(verifier.verify(token)) for _ in range(8)]
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=1)
    assert all(isinstance(result, ServiceUnavailableError) for result in results)
    assert calls == 2
    unavailable = False
    assert (await verifier.verify(token)).key_id == "known-key"
    assert calls == 3


@pytest.mark.asyncio
async def test_refresh_has_total_deadline_and_retries_after_timeout(signing_key, monkeypatch):
    private_key, jwk = signing_key
    monkeypatch.setattr(principal_assertion, "JWKS_FETCH_TIMEOUT_SECONDS", 0.05)
    calls = 0

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return {"keys": [jwk]}

    verifier = _verifier(loader)
    with pytest.raises(ServiceUnavailableError):
        await asyncio.wait_for(verifier.verify(_token(private_key)), timeout=0.5)
    assert (await verifier.verify(_token(private_key))).key_id == "known-key"
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelled_loader_does_not_poison_the_next_refresh(signing_key):
    private_key, jwk = signing_key
    calls = 0

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return {"keys": [jwk]}

    verifier = _verifier(loader)
    with pytest.raises(asyncio.CancelledError):
        await verifier.verify(_token(private_key))
    assert (await verifier.verify(_token(private_key))).key_id == "known-key"
    assert calls == 2


@pytest.mark.asyncio
async def test_unknown_kid_cooldown_never_reuses_an_expired_cache(signing_key):
    private_key, jwk = signing_key
    now = [time.time()]
    calls = 0

    async def loader(_url):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise httpx.ConnectError("offline")
        return {"keys": [jwk]}

    verifier = _verifier(loader, clock=lambda: now[0], cache_ttl_seconds=1)
    token = _token(private_key)
    await verifier.verify(token)
    now[0] += 0.9
    with pytest.raises(ServiceUnavailableError):
        await verifier.verify(_token(private_key, kid="unknown-key"))
    assert (await verifier.verify(token)).key_id == "known-key"
    with pytest.raises(AuthError):
        await verifier.verify(_token(private_key, kid="other-unknown-key"))
    assert calls == 2
    now[0] += 0.2
    with pytest.raises(ServiceUnavailableError):
        await verifier.verify(token)
    assert calls == 3


@pytest.mark.asyncio
async def test_jwks_lifespan_reuses_client_and_cancels_refresh_on_shutdown(signing_key, monkeypatch):
    private_key, jwk = signing_key
    started, cancelled = asyncio.Event(), asyncio.Event()
    clients = []
    calls = 0
    original_client = httpx.AsyncClient

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200, json={"keys": [jwk]})

    def client_factory(**kwargs):
        client = original_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(principal_assertion.settings, "principal_assertion_issuer", "https://continuum.test")
    monkeypatch.setattr(
        principal_assertion.settings, "principal_assertion_jwks_url", "https://continuum.test/.well-known/jwks.json"
    )
    async with principal_assertion.principal_assertion_lifespan():
        verifier = principal_assertion.get_principal_assertion_verifier()
        await verifier.verify(_token(private_key))
        waiter = asyncio.create_task(verifier.verify(_token(private_key, kid="unknown-key")))
        await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancelled.is_set()
    assert len(clients) == 1 and clients[0].is_closed
    async with principal_assertion.principal_assertion_lifespan():
        assert principal_assertion.get_principal_assertion_verifier() is not verifier
    assert len(clients) == 2 and clients[1].is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", [["sag-api"], ["sag-api", "other-api"]])
async def test_scalar_audience_contract_rejects_audience_arrays(signing_key, audience):
    private_key, jwk = signing_key

    async def loader(_url):
        return {"keys": [jwk]}

    with pytest.raises(AuthError):
        await _verifier(loader).verify(_token(private_key, aud=audience))


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_offset", [-10, 70])
async def test_injected_clock_still_enforces_assertion_time_bounds(signing_key, clock_offset):
    private_key, jwk = signing_key
    now = time.time()

    async def loader(_url):
        return {"keys": [jwk]}

    with pytest.raises(AuthError):
        await _verifier(loader, clock=lambda: now + clock_offset).verify(_token(private_key))
