from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sag_api.core.errors import AuthError
from sag_api.core.principal_assertion import PrincipalAssertionVerifier
from sag_api.db.models import Source, SourceProjectMapping
from sag_api.services.source_service import get_authorized_source, search_source_candidates


def _key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key-1", "use": "sig", "alg": "RS256"})
    return private_key, public_jwk


def _token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://continuum.test",
        "aud": "sag-api",
        "sub": "user-1",
        "orgId": "org-1",
        "allowedProjectIds": ["project-1"],
        "iat": now,
        "exp": now + 60,
        "jti": "assertion-1",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key-1"})


async def _jwks(public_jwk):
    return {"keys": [public_jwk]}


@pytest.mark.asyncio
async def test_verifier_accepts_only_configured_signed_project_scope():
    private_key, public_jwk = _key_pair()
    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=lambda _url: _jwks(public_jwk),
    )

    principal = await verifier.verify(_token(private_key))

    assert principal.subject == "user-1"
    assert principal.organization_id == "org-1"
    assert principal.allowed_project_ids == frozenset({"project-1"})


@pytest.mark.asyncio
async def test_principal_dependency_rejects_duplicate_assertion_headers():
    from starlette.requests import Request

    from sag_api.core.principal_assertion import require_principal_assertion

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-sag-principal-assertion", b"first"),
                (b"x-sag-principal-assertion", b"second"),
            ],
        }
    )

    with pytest.raises(AuthError):
        await require_principal_assertion(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://untrusted.test"},
        {"aud": "another-service"},
        {"orgId": ""},
        {"allowedProjectIds": "project-1"},
        {"allowedProjectIds": ["project-1", "project-1"]},
        {"tenantId": "other-org"},
        {"organization_id": "other-org"},
        {"source_ids": ["source-1"]},
        {"allowed_project_ids": ["other-project"]},
        {"exp": int(time.time()) - 10},
        {"iat": int(time.time()) + 3600},
    ],
)
async def test_verifier_rejects_invalid_assertion_claims(claims):
    private_key, public_jwk = _key_pair()
    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=lambda _url: _jwks(public_jwk),
    )

    with pytest.raises(AuthError):
        await verifier.verify(_token(private_key, **claims))


@pytest.mark.asyncio
async def test_verifier_rejects_invalid_signature():
    _trusted_private_key, public_jwk = _key_pair()
    attacker_private_key, _attacker_public_jwk = _key_pair()
    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=lambda _url: _jwks(public_jwk),
    )

    with pytest.raises(AuthError):
        await verifier.verify(_token(attacker_private_key))


@pytest.mark.asyncio
async def test_unknown_key_refreshes_once_then_denies():
    private_key, public_jwk = _key_pair()
    calls = 0

    async def jwks_loader(_url):
        nonlocal calls
        calls += 1
        return await _jwks(public_jwk)

    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=jwks_loader,
    )
    token = jwt.encode(
        {
            "iss": "https://continuum.test",
            "aud": "sag-api",
            "sub": "user-1",
            "orgId": "org-1",
            "allowedProjectIds": ["project-1"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "unknown-key-assertion",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "rotated-key-not-published"},
    )

    with pytest.raises(AuthError):
        await verifier.verify(token)
    assert calls == 2


@pytest.mark.asyncio
async def test_key_rotation_refreshes_jwks_for_a_new_kid():
    _old_private_key, old_public_jwk = _key_pair()
    new_private_key, new_public_jwk = _key_pair()
    new_public_jwk["kid"] = "rotated-key-2"
    calls = 0

    async def jwks_loader(_url):
        nonlocal calls
        calls += 1
        return await _jwks(old_public_jwk if calls == 1 else new_public_jwk)

    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=jwks_loader,
    )
    token = jwt.encode(
        {
            "iss": "https://continuum.test",
            "aud": "sag-api",
            "sub": "user-1",
            "orgId": "org-1",
            "allowedProjectIds": ["project-1"],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": "rotated-assertion",
        },
        new_private_key,
        algorithm="RS256",
        headers={"kid": "rotated-key-2"},
    )

    principal = await verifier.verify(token)

    assert principal.key_id == "rotated-key-2"
    assert calls == 2


@pytest.mark.asyncio
async def test_unknown_kid_refresh_is_rate_limited():
    _known_private_key, known_public_jwk = _key_pair()
    unknown_private_key, _unknown_public_jwk = _key_pair()
    now = int(time.time())
    calls = 0

    async def jwks_loader(_url):
        nonlocal calls
        calls += 1
        return await _jwks(known_public_jwk)

    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=jwks_loader,
        clock=lambda: now,
    )

    def unknown_token(kid: str) -> str:
        return jwt.encode(
            {
                "iss": "https://continuum.test",
                "aud": "sag-api",
                "sub": "user-1",
                "orgId": "org-1",
                "allowedProjectIds": ["project-1"],
                "iat": now,
                "exp": now + 60,
                "jti": f"assertion-{kid}",
            },
            unknown_private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

    for kid in ("unknown-a", "unknown-b"):
        with pytest.raises(AuthError):
            await verifier.verify(unknown_token(kid))

    assert calls == 2


@pytest.mark.asyncio
async def test_jwks_outage_fails_closed_as_dependency_error():
    from sag_api.core.errors import ServiceUnavailableError

    async def unavailable(_url):
        raise httpx.ConnectError("offline")

    private_key, _public_jwk = _key_pair()
    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=unavailable,
    )

    with pytest.raises(ServiceUnavailableError):
        await verifier.verify(_token(private_key))


@pytest.mark.asyncio
async def test_jwks_rejects_private_signing_key_material():
    from sag_api.core.errors import ServiceUnavailableError

    private_key, _public_jwk = _key_pair()
    private_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key))
    private_jwk.update({"kid": "test-key-1", "use": "sig", "alg": "RS256", "key_ops": ["verify"]})
    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=lambda _url: _jwks(private_jwk),
    )

    with pytest.raises(ServiceUnavailableError):
        await verifier.verify(_token(private_key))


@pytest.mark.asyncio
async def test_jwks_cache_is_hard_capped_at_sixty_seconds():
    _, public_jwk = _key_pair()
    now = [1_000]
    calls = 0

    async def jwks_loader(_url):
        nonlocal calls
        calls += 1
        return await _jwks(public_jwk)

    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        cache_ttl_seconds=300,
        jwks_loader=jwks_loader,
        clock=lambda: now[0],
    )

    await verifier._get_keys()
    now[0] += 61
    await verifier._get_keys()

    assert calls == 2


@pytest.mark.asyncio
async def test_source_candidates_are_filtered_by_confirmed_org_and_project_scope():
    from sag_api.core.db import SessionLocal, init_db

    await init_db()
    ids = [
        "acl-a-confirmed",
        "acl-b-other-project",
        "acl-c-other-org",
        "acl-d-pending",
        "acl-e-unmapped",
        "acl-f-revoked",
    ]
    principal_key, _ = _key_pair()
    principal = await PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=lambda _url: _jwks(
            {
                **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(principal_key.public_key())),
                "kid": "test-key-1",
                "use": "sig",
                "alg": "RS256",
            }
        ),
    ).verify(_token(principal_key))

    async with SessionLocal() as session:
        try:
            session.add_all(
                [
                    Source(
                        id=ids[0],
                        name="authorized",
                        sag_source_config_id="acl-a-config",
                    ),
                    Source(
                        id=ids[1],
                        name="other project",
                        sag_source_config_id="acl-b-config",
                    ),
                    Source(
                        id=ids[2],
                        name="other org",
                        sag_source_config_id="acl-c-config",
                    ),
                    Source(
                        id=ids[3],
                        name="pending",
                        sag_source_config_id="acl-d-config",
                    ),
                    Source(
                        id=ids[4],
                        name="unmapped",
                        sag_source_config_id="acl-e-config",
                    ),
                    Source(
                        id=ids[5],
                        name="revoked",
                        sag_source_config_id="acl-f-config",
                    ),
                ]
            )
            session.add_all(
                [
                    SourceProjectMapping(
                        source_id=ids[0],
                        organization_id="org-1",
                        project_id="project-1",
                        state="CONFIRMED",
                        confirmed_at=datetime.now(UTC),
                        confirmed_by="security-owner",
                        approval_ref="test-approval",
                    ),
                    SourceProjectMapping(
                        source_id=ids[1],
                        organization_id="org-1",
                        project_id="project-2",
                        state="CONFIRMED",
                        confirmed_at=datetime.now(UTC),
                        confirmed_by="security-owner",
                        approval_ref="test-approval",
                    ),
                    SourceProjectMapping(
                        source_id=ids[2],
                        organization_id="org-2",
                        project_id="project-1",
                        state="CONFIRMED",
                        confirmed_at=datetime.now(UTC),
                        confirmed_by="security-owner",
                        approval_ref="test-approval",
                    ),
                    SourceProjectMapping(
                        source_id=ids[3],
                        organization_id="org-1",
                        project_id="project-1",
                        state="PENDING",
                    ),
                    SourceProjectMapping(
                        source_id=ids[5],
                        organization_id="org-1",
                        project_id="project-1",
                        state="REVOKED",
                        mapping_version=2,
                        revoked_at=datetime.now(UTC),
                        revoked_by="security-owner",
                        revocation_ref="test-revoke",
                    ),
                ]
            )
            await session.commit()
            all_candidates = await search_source_candidates(session, principal=principal)
            narrowed = await search_source_candidates(
                session,
                principal=principal,
                requested_source_ids=[ids[1], ids[0], ids[2]],
            )
            denied = await get_authorized_source(session, principal=principal, source_id=ids[2])

            assert {source.id for source in all_candidates} == {ids[0]}
            assert [source.id for source in narrowed] == [ids[0]]
            assert denied is None
        finally:
            for source_id in ids:
                source = await session.get(Source, source_id)
                if source is not None:
                    await session.delete(source)
            await session.commit()


@pytest.mark.asyncio
async def test_empty_project_scope_and_mapping_failures_never_fall_back_to_global_sources():
    from sqlalchemy.exc import SQLAlchemyError

    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.core.principal_assertion import VerifiedPrincipal
    from sag_api.services.source_service import search_source_candidates

    class UnexpectedDatabaseCall:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("empty Project scope must short-circuit before querying mappings")

    principal = VerifiedPrincipal(
        subject="user-1",
        organization_id="org-1",
        allowed_project_ids=frozenset(),
        issuer="https://continuum.test",
        key_id="test-key",
        token_id="empty-scope",
        issued_at=1,
        expires_at=2,
    )
    assert await search_source_candidates(UnexpectedDatabaseCall(), principal=principal) == []

    class FailedMappingDatabase:
        async def execute(self, *_args, **_kwargs):
            raise SQLAlchemyError("mapping store unavailable")

    scoped_principal = VerifiedPrincipal(
        subject=principal.subject,
        organization_id=principal.organization_id,
        allowed_project_ids=frozenset({"project-1"}),
        issuer=principal.issuer,
        key_id=principal.key_id,
        token_id=principal.token_id,
        issued_at=principal.issued_at,
        expires_at=principal.expires_at,
    )
    with pytest.raises(ServiceUnavailableError):
        await search_source_candidates(FailedMappingDatabase(), principal=scoped_principal)


@pytest.mark.asyncio
async def test_global_search_and_stream_reject_missing_assertion_before_retrieval():
    from sag_api.core.deps import get_engine_manager
    from sag_api.core.principal_assertion import require_principal_assertion
    from sag_api.main import app

    calls = 0

    class NoRetrieval:
        async def search_many(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("retrieval must not run without a verified principal")

    app.dependency_overrides.pop(require_principal_assertion, None)
    app.dependency_overrides[get_engine_manager] = lambda: NoRetrieval()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"acl-{time.time_ns()}@test.invalid", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                payload = {"query": "project secrets"}

                search_response = await client.post("/api/v1/search", headers=headers, json=payload)
                stream_response = await client.post("/api/v1/search/stream", headers=headers, json=payload)

        assert search_response.status_code == 401
        assert stream_response.status_code == 401
        assert calls == 0
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)


@pytest.mark.asyncio
async def test_global_scope_is_applied_before_dense_lexical_candidate_generation(monkeypatch):
    from sag_api.api.v1 import search as search_api
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app
    from sag_api.sag.dto import SearchOutcome

    await init_db()
    authorized_id, foreign_id = "acl-route-authorized", "acl-route-foreign"
    candidates: list[list[str]] = []

    class RecordingEngine:
        async def search_many(self, targets, _query, **_kwargs):
            candidates.append([("dense", source_config_id) for source_config_id, _source in targets])
            return SearchOutcome(query="scoped", sections=[])

        async def grep_chunks(self, source_config_id, _pattern, **_kwargs):
            candidates.append([("lexical", source_config_id)])
            return []

    monkeypatch.setattr(
        search_api,
        "route_query",
        lambda query, context=None: {
            "query": query,
            "coarse_intent": "KNOWLEDGE",
            "is_chitchat": False,
            "need_retrieval": True,
            "suggested_strategy": "vector",
            "confidence": 0.99,
            "model": "test",
            "fallback_used": False,
            "fallback_reason": None,
        },
    )
    app.dependency_overrides[get_engine_manager] = lambda: RecordingEngine()
    async with SessionLocal() as session:
        sources = [
            Source(id=authorized_id, name="allowed", sag_source_config_id="acl-cfg-allowed"),
            Source(id=foreign_id, name="foreign", sag_source_config_id="acl-cfg-foreign"),
        ]
        session.add_all(sources)
        session.add_all(
            [
                SourceProjectMapping(
                    source_id=authorized_id,
                    organization_id="pytest-org",
                    project_id="pytest-project",
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="pytest-owner",
                    approval_ref="test-approval",
                ),
                SourceProjectMapping(
                    source_id=foreign_id,
                    organization_id="pytest-org",
                    project_id="other-project",
                    state="CONFIRMED",
                    confirmed_at=datetime.now(UTC),
                    confirmed_by="pytest-owner",
                    approval_ref="test-approval",
                ),
            ]
        )
        await session.commit()

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"acl-route-{time.time_ns()}@test.invalid", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                response = await client.post(
                    "/api/v1/search",
                    headers=headers,
                    json={"query": "scoped", "source_ids": [foreign_id, authorized_id]},
                )
        assert response.status_code == 200, response.text
        assert candidates == [
            [("dense", "acl-cfg-allowed")],
            [("lexical", "acl-cfg-allowed")],
        ]
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
        async with SessionLocal() as session:
            await session.delete(await session.get(Source, authorized_id))
            await session.delete(await session.get(Source, foreign_id))
            await session.commit()


@pytest.mark.asyncio
async def test_unmapped_source_is_denied_before_source_scoped_retrieval():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.deps import get_engine_manager
    from sag_api.main import app

    await init_db()
    source_id = "acl-route-unmapped"
    calls = 0

    class NoRetrieval:
        async def search_many(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("unmapped Source must not reach retrieval")

    async with SessionLocal() as session:
        session.add(Source(id=source_id, name="unmapped", sag_source_config_id="acl-cfg-unmapped"))
        await session.commit()

    app.dependency_overrides[get_engine_manager] = lambda: NoRetrieval()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"acl-p3-{time.time_ns()}@test.invalid", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                response = await client.post(
                    f"/api/v1/sources/{source_id}/search",
                    headers={"Authorization": f"Bearer {registered.json()['access_token']}"},
                    json={"query": "not searchable"},
                )
        assert response.status_code == 404
        assert calls == 0
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            if source is not None:
                await session.delete(source)
                await session.commit()


@pytest.mark.asyncio
async def test_unmapped_source_blocks_direct_evidence_routes_before_engine_access():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.deps import get_engine_manager
    from sag_api.db.models import Source
    from sag_api.main import app

    await init_db()
    source_id = "acl-direct-unmapped"
    calls = 0

    class NoEngine:
        def __getattr__(self, _name):
            async def denied(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError("unmapped Source reached an evidence engine")

            return denied

    async with SessionLocal() as session:
        session.add(Source(id=source_id, name="unmapped direct-read source", sag_source_config_id="acl-direct-config"))
        await session.commit()

    app.dependency_overrides[get_engine_manager] = lambda: NoEngine()
    paths = [
        f"/api/v1/sources/{source_id}",
        f"/api/v1/sources/{source_id}/chunks/chunk-1",
        f"/api/v1/sources/{source_id}/documents/doc-1",
        f"/api/v1/sources/{source_id}/documents/doc-1/file",
        f"/api/v1/sources/{source_id}/documents/doc-1/preview",
        f"/api/v1/sources/{source_id}/documents/doc-1/parsed",
        f"/api/v1/sources/{source_id}/grep?pattern=private",
        f"/api/v1/sources/{source_id}/documents/doc-1/read",
        f"/api/v1/sources/{source_id}/entities/Private/context",
        f"/api/v1/sources/{source_id}/entities",
        f"/api/v1/sources/{source_id}/graph",
        f"/api/v1/sources/{source_id}/mcp",
    ]
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"acl-direct-{time.time_ns()}@test.invalid", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
                responses = [await client.get(path, headers=headers) for path in paths]

        assert [response.status_code for response in responses] == [404] * len(paths)
        assert calls == 0
    finally:
        app.dependency_overrides.pop(get_engine_manager, None)
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            if source is not None:
                await session.delete(source)
                await session.commit()


@pytest.mark.asyncio
async def test_activity_uses_confirmed_scope_and_client_ids_only_narrow():
    from datetime import UTC, datetime

    from sqlalchemy import delete

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source, SourceProjectMapping
    from sag_api.enums import DocumentStatus
    from sag_api.main import app

    await init_db()
    allowed_id, foreign_id = "acl-activity-allowed", "acl-activity-foreign"
    async with SessionLocal() as session:
        sources = [
            Source(id=allowed_id, name="allowed activity", sag_source_config_id="acl-activity-allowed-config"),
            Source(id=foreign_id, name="foreign activity", sag_source_config_id="acl-activity-foreign-config"),
        ]
        session.add_all(sources)
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                SourceProjectMapping(
                    source_id=allowed_id,
                    organization_id="pytest-org",
                    project_id="pytest-project",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="pytest-owner",
                    approval_ref="test-approval",
                ),
                SourceProjectMapping(
                    source_id=foreign_id,
                    organization_id="pytest-org",
                    project_id="other-project",
                    state="CONFIRMED",
                    confirmed_at=now,
                    confirmed_by="pytest-owner",
                    approval_ref="test-approval",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Document(
                    source_id=allowed_id,
                    filename="allowed.md",
                    storage_path="/unused/allowed.md",
                    status=DocumentStatus.READY,
                ),
                Document(
                    source_id=foreign_id,
                    filename="foreign.md",
                    storage_path="/unused/foreign.md",
                    status=DocumentStatus.READY,
                ),
            ]
        )
        await session.commit()

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": f"acl-activity-{time.time_ns()}@test.invalid", "password": "password123"},
                )
                assert registered.status_code == 201, registered.text
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

                global_activity = await client.get("/api/v1/activity", headers=headers)
                narrowed_foreign = await client.get(
                    "/api/v1/activity",
                    headers=headers,
                    params={"source_ids": foreign_id},
                )
                narrowed_allowed = await client.get(
                    "/api/v1/activity",
                    headers=headers,
                    params={"source_ids": allowed_id},
                )

        global_titles = {item["title"] for item in global_activity.json()}
        assert "allowed.md" in global_titles
        assert "foreign.md" not in global_titles
        assert narrowed_foreign.json() == []
        assert [item["title"] for item in narrowed_allowed.json()] == ["allowed.md"]
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(SourceProjectMapping).where(SourceProjectMapping.source_id.in_([allowed_id, foreign_id]))
            )
            await session.execute(delete(Document).where(Document.source_id.in_([allowed_id, foreign_id])))
            await session.execute(delete(Source).where(Source.id.in_([allowed_id, foreign_id])))
            await session.commit()


@pytest.mark.asyncio
async def test_agent_history_keeps_unscoped_answers_but_redacts_unavailable_sources(monkeypatch):
    from types import SimpleNamespace

    from sag_api.enums import MessageRole
    from sag_api.services import agent_domain

    principal = SimpleNamespace(allowed_project_ids=frozenset({"project-1"}))
    messages = [
        SimpleNamespace(role=MessageRole.ASSISTANT, content="general answer", citations=[]),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="external answer",
            citations=[{"kind": "external", "url": "https://example.test/evidence"}],
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="authorized answer",
            citations=[{"source_id": "allowed-source", "chunk_id": "chunk-1"}],
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="revoked answer",
            citations=[{"source_id": "revoked-source", "chunk_id": "chunk-2"}],
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="revoked tool answer",
            citations=[],
            steps=[
                {
                    "kind": "tool",
                    "details": {
                        "scope": "knowledge",
                        "sources": [{"id": "revoked-source", "name": "Revoked source"}],
                    },
                }
            ],
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="ambiguous answer",
            citations=[{"chunk_id": "chunk-3"}],
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT,
            content="legacy knowledge answer",
            citations=[],
            steps=[{"kind": "tool", "name": "search_context"}],
        ),
    ]

    async def authorized_source_ids(_session, *, principal, requested_source_ids):
        del principal
        return set(requested_source_ids) & {"allowed-source"}

    monkeypatch.setattr(agent_domain, "get_authorized_source_ids", authorized_source_ids)

    visible = await agent_domain.filter_messages_for_scope(
        None,
        messages,
        principal=principal,
    )

    assert [message.content for message in visible[:3]] == [
        "general answer",
        "external answer",
        "authorized answer",
    ]
    assert len(visible) == len(messages)
    assert all(message.role == MessageRole.ASSISTANT for message in visible[3:])
    assert all(
        message.content != original.content
        for message, original in zip(visible[3:], messages[3:], strict=True)
    )
    assert all(message.citations == message.steps == [] for message in visible[3:])


@pytest.mark.asyncio
async def test_explicit_empty_agent_source_filter_never_expands_to_all_sources(monkeypatch):
    from types import SimpleNamespace

    from sag_api.schemas.agent import AskRequest
    from sag_api.services import agent_domain

    assert AskRequest(query="query").source_ids is None
    assert AskRequest(query="query", source_ids=[]).source_ids == []
    calls = []
    plan = agent_domain.build_ask_context(
        agent=SimpleNamespace(name="test-agent", persona={}),
        query="query",
        source_ids=[],
    )

    async def source_candidates(_session, *, principal, requested_source_ids=None):
        calls.append(requested_source_ids)
        return ["authorized-source"] if requested_source_ids is None else []

    monkeypatch.setattr(agent_domain, "search_source_candidates", source_candidates)
    sources = await agent_domain.resolve_sources(
        None,
        SimpleNamespace(is_default=True),
        source_ids=[],
        principal=SimpleNamespace(allowed_project_ids=frozenset({"project-1"})),
    )

    assert plan.source_ids == []
    assert sources == []
    assert calls == [[]]
