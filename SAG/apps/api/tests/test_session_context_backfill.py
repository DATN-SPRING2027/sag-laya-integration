"""Real signed session authentication around disposable backfill storage.

Only local feature inference is doubled; JWT verification, current-user checks,
Project-bound ownership, keyset reads and index writes use their real code.
"""

import time

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from test_session_context_runtime import Encoder

from sag_api.core.config import settings
from sag_api.core.conversation_ownership import conversation_owner_key
from sag_api.core.errors import AuthError, ForbiddenError, NotFoundError
from sag_api.core.principal_assertion import require_project_read_principal
from sag_api.core.security import create_access_token
from sag_api.db.base import Base
from sag_api.db.models import Agent, Message, SessionContextIndex, SessionMessageProvenance, Thread, User
from sag_api.enums import MessageRole
from scripts import backfill_session_context as job


def scoped_token(user_id="alice", *, project="project", **claims):
    return create_access_token(user_id, {
        "org_id": "org", "tenant_id": "org", "project_id": project,
        "security_partition": project, "jti": "offline-session", **claims,
    })


@pytest.fixture
async def authenticated_backfill_db(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "continuum")
    monkeypatch.setattr(settings, "secret_key", "offline-backfill-signing-secret-for-tests-only")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    token = scoped_token()
    request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})
    principal = await require_project_read_principal(request)
    owner = conversation_owner_key("alice", principal)
    async with factory() as session:
        session.add_all([User(id=user_id, email=f"{user_id}@example.invalid", password_hash="not-used-by-jwt")
                         for user_id in ("alice", "bob")])
        session.add(Agent(id="agent", name="Default", owner_key=owner, is_default=True))
        await session.flush()
        session.add(Thread(id="thread", agent_id="agent", owner_key=owner))
        await session.flush()
        session.add_all([
            Message(id="legacy-user", thread_id="thread", role=MessageRole.USER, content="User confirmed BLUE-472."),
            Message(id="legacy-assistant", thread_id="thread", role=MessageRole.ASSISTANT,
                    content="Old answer with unknown transitive evidence.", citations=[]),
        ])
        await session.commit()
    try:
        yield factory, token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_default_dry_run_authenticates_real_jwt_without_writes(authenticated_backfill_db):
    factory, token = authenticated_backfill_db
    async with factory() as session:
        report = await job.backfill(session, agent_id="agent", thread_id="thread", token=token)
        assert report == {"mode": "dry_run", "eligible_messages": 1, "raw_history_modified": False}
        assert await session.scalar(select(func.count()).select_from(SessionContextIndex)) == 0
        assert await session.scalar(select(func.count()).select_from(SessionMessageProvenance)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
        assert (await session.get(Message, "legacy-assistant")).content.startswith("Old answer")


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id,project", [("bob", "project"), ("alice", "other-project")])
async def test_backfill_rejects_cross_user_or_project_even_for_shared_default_agent(authenticated_backfill_db,
                                                                                 user_id, project):
    factory, _ = authenticated_backfill_db
    async with factory() as session:
        with pytest.raises(NotFoundError):
            await job.backfill(session, agent_id="agent", thread_id="thread",
                               token=scoped_token(user_id, project=project))
        assert await session.scalar(select(func.count()).select_from(SessionContextIndex)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["signature", "expired", "missing_project"])
async def test_backfill_rejects_invalid_or_unscoped_real_tokens(authenticated_backfill_db, invalid):
    factory, token = authenticated_backfill_db
    if invalid == "signature":
        claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        token = jwt.encode(claims, "different-offline-signing-key-for-tests-only", algorithm="HS256")
    elif invalid == "expired":
        token = scoped_token(exp=int(time.time()) - 1)
    else:
        token = create_access_token("alice", {"jti": "offline-session"})
    async with factory() as session:
        with pytest.raises((AuthError, ForbiddenError)):
            await job.backfill(session, agent_id="agent", thread_id="thread", token=token)
        assert await session.scalar(select(func.count()).select_from(SessionContextIndex)) == 0


@pytest.mark.asyncio
async def test_backfill_rejects_revoked_local_user_even_with_valid_project_session(authenticated_backfill_db):
    factory, token = authenticated_backfill_db
    async with factory() as session:
        user = await session.get(User, "alice")
        user.is_active = False
        await session.commit()
        with pytest.raises(AuthError):
            await job.backfill(session, agent_id="agent", thread_id="thread", token=token)
        assert await session.scalar(select(func.count()).select_from(SessionContextIndex)) == 0


@pytest.mark.asyncio
async def test_authenticated_backfill_apply_reuses_checkpoint_and_never_promotes_legacy_assistant(
    authenticated_backfill_db, monkeypatch,
):
    factory, token = authenticated_backfill_db

    class CountingEncoder(Encoder):
        def __init__(self):
            self.seen = []

        def featurize_many(self, texts, **kwargs):
            self.seen.extend(texts)
            return super().featurize_many(texts, **kwargs)

    encoder = CountingEncoder()
    monkeypatch.setattr(job, "configured_encoder", lambda: encoder)
    async with factory() as session:
        first = await job.backfill(session, agent_id="agent", thread_id="thread", token=token, apply=True)
    async with factory() as session:
        second = await job.backfill(session, agent_id="agent", thread_id="thread", token=token, apply=True)
        assert first["state"] == second["state"] == "ready"
        assert first["eligible_messages"] == second["eligible_messages"] == 1
        assert len(encoder.seen) == 1 and "BLUE-472" in encoder.seen[0]
        assert await session.scalar(select(func.count()).select_from(SessionContextIndex)) == 1
        assert await session.scalar(select(func.count()).select_from(SessionMessageProvenance)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)) == 2
