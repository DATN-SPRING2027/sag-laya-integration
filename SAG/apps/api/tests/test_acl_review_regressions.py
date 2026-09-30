from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


def _principal(project_ids: frozenset[str] = frozenset({"review-project"})):
    from sag_api.core.principal_assertion import VerifiedPrincipal

    return VerifiedPrincipal(
        subject="review-user",
        organization_id="review-org",
        allowed_project_ids=project_ids,
        issuer="https://continuum.test",
        key_id="review-key",
        token_id="review-assertion",
        issued_at=0,
        expires_at=2**31,
    )


@pytest.mark.asyncio
async def test_source_create_records_requested_project_as_pending_and_rejects_out_of_scope(monkeypatch):
    from sqlalchemy import delete, select

    from sag_api.api.v1 import sources as sources_api
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Source, SourceProjectMapping, User
    from sag_api.main import app
    from sag_api.services.source_service import create_source as real_create_source

    monkeypatch.setattr(sources_api, "create_source", real_create_source)
    await init_db()
    source_name = f"review-pending-{uuid.uuid4().hex}"
    registered_email = f"{uuid.uuid4().hex}@review.test"
    created_source_ids: list[str] = []
    transport = httpx.ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                registered = await client.post(
                    "/api/v1/auth/register",
                    json={"email": registered_email, "password": "password123"},
                )
                assert registered.status_code == 201
                headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

                created = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": source_name, "project_id": "pytest-project"},
                )
                assert created.status_code == 201, created.text
                created_source_ids.append(created.json()["id"])
                assert created.json()["mapping_state"] == "PENDING"

                async with SessionLocal() as session:
                    mapping = await session.scalar(
                        select(SourceProjectMapping).where(
                            SourceProjectMapping.source_id == created_source_ids[0]
                        )
                    )
                    assert mapping is not None
                    assert mapping.state == "PENDING"
                    assert mapping.organization_id == "pytest-org"
                    assert mapping.project_id == "pytest-project"

                inaccessible = await client.get(
                    f"/api/v1/sources/{created_source_ids[0]}", headers=headers
                )
                assert inaccessible.status_code == 404

                rejected_name = f"{source_name}-out-of-scope"
                rejected = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": rejected_name, "project_id": "other-project"},
                )
                assert rejected.status_code == 403
                async with SessionLocal() as session:
                    assert await session.scalar(
                        select(Source.id).where(Source.name == rejected_name)
                    ) is None

                implicit_name = f"{source_name}-implicit-project"
                implicit = await client.post(
                    "/api/v1/sources",
                    headers=headers,
                    json={"name": implicit_name},
                )
                assert implicit.status_code == 201, implicit.text
                created_source_ids.append(implicit.json()["id"])
                assert implicit.json()["mapping_state"] == "PENDING"
                async with SessionLocal() as session:
                    mapping = await session.scalar(
                        select(SourceProjectMapping).where(
                            SourceProjectMapping.source_id == created_source_ids[-1]
                        )
                    )
                    assert mapping is not None
                    assert mapping.state == "PENDING"
                    assert mapping.project_id == "pytest-project"
    finally:
        async with SessionLocal() as session:
            await session.execute(
                delete(SourceProjectMapping).where(
                    SourceProjectMapping.source_id.in_(created_source_ids)
                )
            )
            await session.execute(delete(Source).where(Source.id.in_(created_source_ids)))
            await session.execute(delete(User).where(User.email == registered_email))
            await session.commit()


@pytest.mark.asyncio
async def test_exploration_list_batches_source_authorization_and_keeps_source_free_sessions(monkeypatch):
    from sag_api.api.v1 import universe as universe_api

    now = datetime.now(UTC)
    rows = [
        (
            SimpleNamespace(
                id="allowed", title="allowed", source_ids=["s1", "s1", "s2"], created_at=now, updated_at=now
            ),
            2,
        ),
        (SimpleNamespace(id="empty", title="empty", source_ids=[], created_at=now, updated_at=now), 0),
        (SimpleNamespace(id="denied", title="denied", source_ids=["s3"], created_at=now, updated_at=now), 1),
        (SimpleNamespace(id="partial", title="partial", source_ids=["s1", "s3"], created_at=now, updated_at=now), 1),
    ]
    authorization_calls: list[list[str]] = []

    async def list_rows(_session, _user_id, *, limit):
        assert limit == 20
        return rows

    async def authorized_ids(_session, *, principal, requested_source_ids):
        assert principal is principal_for_test
        authorization_calls.append(requested_source_ids)
        return {"s1", "s2"}

    principal_for_test = _principal()
    monkeypatch.setattr(universe_api, "list_explorations", list_rows)
    monkeypatch.setattr(universe_api, "get_authorized_source_ids", authorized_ids)

    result = await universe_api.explorations(
        limit=20,
        user=SimpleNamespace(id="user-1"),
        principal=principal_for_test,
        session=object(),
    )

    assert [item.id for item in result] == ["allowed", "empty"]
    assert len(authorization_calls) == 1
    assert set(authorization_calls[0]) == {"s1", "s2", "s3"}


@pytest.mark.asyncio
async def test_source_scope_id_lookup_batches_large_requests():
    from sag_api.services.source_service import get_authorized_source_ids

    class ScalarResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class RecordingSession:
        def __init__(self):
            self.batches: list[list[str]] = []

        async def execute(self, statement):
            values = list(statement._where_criteria[-1].right.value)
            self.batches.append(values)
            return ScalarResult(values)

    session = RecordingSession()
    requested = [f"source-{index:04}" for index in range(1100)]

    authorized = await get_authorized_source_ids(
        session,
        principal=_principal(),
        requested_source_ids=requested,
    )

    assert authorized == set(requested)
    assert [len(batch) for batch in session.batches] == [500, 500, 100]


@pytest.mark.asyncio
async def test_verifier_caches_parsed_rsa_key_and_allows_reusing_bearer_assertion(monkeypatch):
    from sag_api.core.errors import AuthError
    from sag_api.core.principal_assertion import PrincipalAssertionVerifier

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "review-key", "use": "sig", "alg": "RS256"})
    weak_private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(weak_private_key.public_key()))
    weak_jwk.update({"kid": "weak-review-key", "use": "sig", "alg": "RS256"})
    original_from_jwk = jwt.algorithms.RSAAlgorithm.from_jwk
    parse_calls = 0

    def count_key_parses(value):
        nonlocal parse_calls
        parse_calls += 1
        return original_from_jwk(value)

    monkeypatch.setattr(jwt.algorithms.RSAAlgorithm, "from_jwk", staticmethod(count_key_parses))

    async def load_jwks(_url):
        return {"keys": [public_jwk, weak_jwk]}

    verifier = PrincipalAssertionVerifier(
        issuer="https://continuum.test",
        audience="sag-api",
        jwks_url="https://continuum.test/.well-known/jwks.json",
        jwks_loader=load_jwks,
    )
    now = int(time.time())
    claims = {
        "iss": "https://continuum.test",
        "aud": "sag-api",
        "sub": "user-1",
        "orgId": "org-1",
        "allowedProjectIds": ["project-1"],
        "iat": now,
        "exp": now + 60,
        "jti": "reusable-bearer-assertion",
    }
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "review-key"},
    )

    first = await verifier.verify(token)
    second = await verifier.verify(token)

    assert first == second
    assert parse_calls == 2

    weak_token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "weak-review-key"},
    )
    with pytest.raises(AuthError, match="too weak"):
        await verifier.verify(weak_token)
    assert parse_calls == 2


@pytest.mark.asyncio
async def test_acl_csv_apply_checks_sources_in_bounded_batches(capsys):
    from sag_api.db.models import Source
    from scripts.source_project_acl import _apply

    class ScalarResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class RecordingSession:
        def __init__(self):
            self.source_batches: list[list[str]] = []
            self.rolled_back = False

        async def execute(self, statement):
            if statement.column_descriptions[0]["entity"] is Source:
                values = list(statement._where_criteria[-1].right.value)
                self.source_batches.append(values)
                return ScalarResult([SimpleNamespace(id=value) for value in values])
            return ScalarResult([])

        async def rollback(self):
            self.rolled_back = True

    session = RecordingSession()
    rows = [
        {
            "source_id": f"source-{index:04}",
            "organization_id": "org-1",
            "project_id": "project-1",
            "approval_ref": "approved-test",
        }
        for index in range(1100)
    ]

    await _apply(session, rows, "a" * 64, "test-owner", write=False)

    assert [len(batch) for batch in session.source_batches] == [500, 500, 100]
    assert session.rolled_back
    assert "mode=DRY_RUN" in capsys.readouterr().out


def test_authorized_source_query_relies_on_unique_current_mapping_index():
    from sag_api.services.source_service import _authorized_source_statement

    statement = _authorized_source_statement(_principal())
    sql = str(statement.compile()).upper()

    assert "JOIN SOURCE_PROJECT_MAPPINGS" in sql
    assert "GROUP BY" not in sql


@pytest.mark.asyncio
async def test_source_create_rejects_ambiguous_implicit_project_before_persisting():
    from sag_api.core.errors import ValidationError
    from sag_api.schemas.source import SourceCreate
    from sag_api.services.source_service import create_source

    with pytest.raises(ValidationError, match="project_id is required"):
        await create_source(
            object(),
            SourceCreate(name="ambiguous-project"),
            engine_manager=object(),
            principal=_principal(frozenset({"project-a", "project-b"})),
        )
