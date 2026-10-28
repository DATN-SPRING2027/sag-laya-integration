"""Schema, migration (`up -> down -> up`), constraint, and cascade tests for Chặng 2."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    Agent,
    Message,
    SessionDecisionLedger,
    SessionGraphEdge,
    SessionUnit,
    Thread,
)
from sag_api.db.models.session_context import (
    SESSION_OPEN_ENDED_VALID_TO,
    generate_session_decision_id,
    generate_session_graph_edge_id,
    generate_session_unit_id,
    generate_session_unit_point_id,
)
from sag_api.enums import MessageRole

API_ROOT = Path(__file__).resolve().parents[1]
UP_SQL_PATH = API_ROOT / "migrations" / "0009_session_context_ecce.sql"
DOWN_SQL_PATH = API_ROOT / "scripts" / "0009_session_context_ecce_down.sql"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_fk_sqlite_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _split_sql_statements(raw_sql: str) -> list[str]:
    cleaned_lines = [
        line for line in raw_sql.splitlines() if not line.strip().startswith("--")
    ]
    body = "\n".join(cleaned_lines)
    stmts: list[str] = []
    for chunk in body.split(";"):
        stmt = chunk.strip()
        if not stmt or stmt.upper() in {"BEGIN", "COMMIT"}:
            continue
        stmts.append(stmt)
    return stmts


def test_deterministic_uuidv5_generators() -> None:
    u1 = generate_session_unit_id("thr_01", "TURN", 0, "msg_01")
    u1_repeat = generate_session_unit_id("thr_01", "TURN", 0, "msg_01")
    u2 = generate_session_unit_id("thr_01", "TURN", 1, "msg_02")
    u_win = generate_session_unit_id("thr_01", "WINDOW", 0, "msg_01")

    assert u1 == u1_repeat
    assert u1 != u2
    assert u1 != u_win
    assert len(u1) == 36
    assert uuid.UUID(u1).version == 5

    pt1 = generate_session_unit_point_id("session_units_proj_a", u1)
    assert pt1 == generate_session_unit_point_id("session_units_proj_a", u1)
    assert pt1 != generate_session_unit_point_id("session_units_proj_b", u1)
    assert uuid.UUID(pt1).version == 5

    d1 = generate_session_decision_id(u1, "slot:database")
    assert d1 == generate_session_decision_id(u1, "slot:database")
    assert d1 != generate_session_decision_id(u1, "slot:cache")
    assert uuid.UUID(d1).version == 5

    e1 = generate_session_graph_edge_id(u1, f"unit:{u1}", "entity:postgresql", "ENTITY_MENTION")
    assert e1 == generate_session_graph_edge_id(u1, f"unit:{u1}", "entity:postgresql", "ENTITY_MENTION")
    assert e1 != generate_session_graph_edge_id(u1, f"unit:{u1}", "entity:postgresql", "SUPERSEDES")
    assert uuid.UUID(e1).version == 5


@pytest.mark.asyncio
async def test_sql_migration_up_down_up_and_orm_metadata_parity() -> None:
    assert UP_SQL_PATH.is_file(), f"Missing forward migration: {UP_SQL_PATH}"
    assert DOWN_SQL_PATH.is_file(), f"Missing rollback script: {DOWN_SQL_PATH}"

    up_stmts = _split_sql_statements(UP_SQL_PATH.read_text(encoding="utf-8"))
    down_stmts = _split_sql_statements(DOWN_SQL_PATH.read_text(encoding="utf-8"))

    engine = _create_fk_sqlite_engine()
    try:
        # 1. Create only parent truth tables (`agents`, `threads`, `messages`)
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[Agent.__table__, Thread.__table__, Message.__table__],
            )
            await conn.execute(
                text(
                    "INSERT INTO agents (id, name, avatar, is_default, persona_json) "
                    "VALUES ('ag1', 'Test Agent', '', 0, '{}')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO threads (id, owner_key, agent_id, title, archived) "
                    "VALUES ('thr1', 'continuum:u1', 'ag1', 'Session 1', 0)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO messages (id, thread_id, attachments_json, steps_json, "
                    "role, content, citations_json, prompt_preview, status) "
                    "VALUES ('msg1', 'thr1', '[]', '[]', 'user', 'Chốt PostgreSQL 18', '[]', '', 'ok')"
                )
            )

        # 2. Apply `0009_session_context_ecce.sql` twice (idempotency check) and compare columns/indexes with ORM
        for _ in range(2):
            async with engine.begin() as conn:
                for stmt in up_stmts:
                    await conn.exec_driver_sql(stmt)

        async with engine.connect() as conn:
            for model_cls in (SessionUnit, SessionDecisionLedger, SessionGraphEdge):
                table_name = model_cls.__tablename__
                sql_cols = await conn.run_sync(
                    lambda sync_conn, t=table_name: {c["name"] for c in inspect(sync_conn).get_columns(t)}
                )
                orm_cols = {col.name for col in model_cls.__table__.columns}
                assert sql_cols == orm_cols, f"Column drift in {table_name}: {sql_cols ^ orm_cols}"

                sql_indexes = await conn.run_sync(
                    lambda sync_conn, t=table_name: {
                        idx["name"] for idx in inspect(sync_conn).get_indexes(t) if idx.get("name")
                    }
                )
                orm_indexes = {idx.name for idx in model_cls.__table__.indexes if idx.name}
                assert orm_indexes.issubset(sql_indexes), (
                    f"Missing index in {table_name}: {orm_indexes - sql_indexes}"
                )

        # 3. Apply `0009_session_context_ecce_down.sql` -> derived tables removed, raw `threads`/`messages` preserved!
        async with engine.begin() as conn:
            for stmt in down_stmts:
                await conn.exec_driver_sql(stmt)

        async with engine.connect() as conn:
            remaining_tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            assert "session_units" not in remaining_tables
            assert "session_decision_ledger" not in remaining_tables
            assert "session_graph_edges" not in remaining_tables
            msg_count = (await conn.execute(text("SELECT COUNT(*) FROM messages WHERE id = 'msg1'"))).scalar_one()
            assert msg_count == 1

        # 4. Re-apply `0009_session_context_ecce.sql` (`up -> down -> up` round-trip)
        async with engine.begin() as conn:
            for stmt in up_stmts:
                await conn.exec_driver_sql(stmt)
            restored_tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
            assert {"session_units", "session_decision_ledger", "session_graph_edges"}.issubset(restored_tables)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cascade_cleanup_on_message_and_thread_delete_and_set_null_lineage() -> None:
    engine = _create_fk_sqlite_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            agent = Agent(owner_key="continuum:thang", name="ECCE Schema Agent")
            session.add(agent)
            await session.flush()

            thread = Thread(agent_id=agent.id, owner_key="continuum:thang", title="DB Lineage Session")
            session.add(thread)
            await session.flush()

            m0 = Message(thread_id=thread.id, role=MessageRole.USER, content="Đề xuất dùng MongoDB, anh Minh đồng ý.")
            m1 = Message(thread_id=thread.id, role=MessageRole.USER, content="Bỏ MongoDB, chốt dùng PostgreSQL 18.")
            m2 = Message(thread_id=thread.id, role=MessageRole.ASSISTANT, content="Đã ghi nhận PostgreSQL 18 [1].")
            session.add_all([m0, m1, m2])
            await session.flush()

            u0 = SessionUnit(
                id=generate_session_unit_id(thread.id, "TURN", 0, m0.id),
                tenant_id="org_01",
                project_id="proj_01",
                thread_id=thread.id,
                owner_key=thread.owner_key,
                unit_kind="TURN",
                ordinal=0,
                episode_id=f"ep:{thread.id}:0",
                episode_ordinal=0,
                window_id=f"win:{thread.id}:0:0",
                message_from_id=m0.id,
                message_to_id=m0.id,
                role="user",
                epistemic_status="SUPERSEDED",
                topic_slot_key="slot:database",
                content_hash=_sha256(m0.content),
                token_count=12,
                valid_from=now,
                valid_to=now + timedelta(minutes=5),
            )
            u1 = SessionUnit(
                id=generate_session_unit_id(thread.id, "TURN", 1, m1.id),
                tenant_id="org_01",
                project_id="proj_01",
                thread_id=thread.id,
                owner_key=thread.owner_key,
                unit_kind="TURN",
                ordinal=1,
                episode_id=f"ep:{thread.id}:0",
                episode_ordinal=0,
                window_id=f"win:{thread.id}:0:0",
                message_from_id=m1.id,
                message_to_id=m1.id,
                role="user",
                epistemic_status="CONFIRMED",
                topic_slot_key="slot:database",
                content_hash=_sha256(m1.content),
                token_count=11,
                valid_from=now + timedelta(minutes=5),
                valid_to=SESSION_OPEN_ENDED_VALID_TO,
            )
            u2 = SessionUnit(
                id=generate_session_unit_id(thread.id, "TURN", 2, m2.id),
                tenant_id="org_01",
                project_id="proj_01",
                thread_id=thread.id,
                owner_key=thread.owner_key,
                unit_kind="TURN",
                ordinal=2,
                episode_id=f"ep:{thread.id}:0",
                episode_ordinal=0,
                window_id=f"win:{thread.id}:0:0",
                message_from_id=m2.id,
                message_to_id=m2.id,
                role="assistant",
                epistemic_status="FACT",
                security_partition_ids_json=["part:proj_01"],
                cited_source_ids_json=["src_spec_01"],
                content_hash=_sha256(m2.content),
                token_count=9,
                valid_from=now + timedelta(minutes=6),
                valid_to=SESSION_OPEN_ENDED_VALID_TO,
            )
            session.add_all([u0, u1, u2])
            await session.flush()

            d0 = SessionDecisionLedger(
                id=generate_session_decision_id(u0.id, "slot:database"),
                tenant_id="org_01",
                project_id="proj_01",
                thread_id=thread.id,
                session_unit_id=u0.id,
                ledger_kind="DECISION",
                topic_slot_key="slot:database",
                turn_ordinal=0,
                epistemic_status="SUPERSEDED",
                summary_text=m0.content,
                entity_keys_json=["mongodb"],
                valid_from=now,
                valid_to=now + timedelta(minutes=5),
            )
            session.add(d0)
            await session.flush()

            d1 = SessionDecisionLedger(
                id=generate_session_decision_id(u1.id, "slot:database"),
                tenant_id="org_01",
                project_id="proj_01",
                thread_id=thread.id,
                session_unit_id=u1.id,
                ledger_kind="DECISION",
                topic_slot_key="slot:database",
                turn_ordinal=1,
                epistemic_status="CONFIRMED",
                summary_text=m1.content,
                entity_keys_json=["postgresql"],
                superseded_entity_keys_json=["mongodb"],
                supersedes_id=d0.id,
                supersedes_ids_json=[d0.id],
                valid_from=now + timedelta(minutes=5),
                valid_to=SESSION_OPEN_ENDED_VALID_TO,
            )
            session.add(d1)
            await session.flush()
            d0.superseded_by_id = d1.id

            edges = [
                SessionGraphEdge(
                    id=generate_session_graph_edge_id(u0.id, "entity:anh_minh", "entity:minh_be", "ALIAS_CANDIDATE"),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread.id,
                    source_unit_id=u0.id,
                    target_unit_id=None,
                    source_node_type="ENTITY",
                    source_node_key="entity:anh_minh",
                    target_node_type="ENTITY",
                    target_node_key="entity:minh_be",
                    edge_type="ALIAS_CANDIDATE",
                    weight=0.65,
                ),
                SessionGraphEdge(
                    id=generate_session_graph_edge_id(u1.id, f"unit:{u1.id}", f"unit:{u0.id}", "SUPERSEDES"),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread.id,
                    source_unit_id=u1.id,
                    target_unit_id=u0.id,
                    source_node_type="UNIT",
                    source_node_key=f"unit:{u1.id}",
                    target_node_type="UNIT",
                    target_node_key=f"unit:{u0.id}",
                    edge_type="SUPERSEDES",
                    weight=1.0,
                ),
                SessionGraphEdge(
                    id=generate_session_graph_edge_id(u1.id, f"unit:{u1.id}", f"unit:{u2.id}", "ADJACENT_IN_EPISODE"),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread.id,
                    source_unit_id=u1.id,
                    target_unit_id=u2.id,
                    source_node_type="UNIT",
                    source_node_key=f"unit:{u1.id}",
                    target_node_type="UNIT",
                    target_node_key=f"unit:{u2.id}",
                    edge_type="ADJACENT_IN_EPISODE",
                    weight=0.85,
                ),
            ]
            session.add_all(edges)
            await session.commit()

            m0_id, u1_id, u2_id, d1_id, thread_id = m0.id, u1.id, u2.id, d1.id, thread.id

            # 1. Delete single message `m0`: `u0`, `d0`, alias edge on `u0`, AND `SUPERSEDES` edge targeting `u0`
            # must be cascade-deleted automatically, while `d1` remains intact with `supersedes_id == None`!
            await session.execute(text("DELETE FROM messages WHERE id = :mid"), {"mid": m0_id})
            await session.commit()
            session.expire_all()

            surviving_units = (await session.scalars(select(SessionUnit.id))).all()
            assert set(surviving_units) == {u1_id, u2_id}

            surviving_decisions = (await session.scalars(select(SessionDecisionLedger))).all()
            assert len(surviving_decisions) == 1
            assert surviving_decisions[0].id == d1_id
            assert surviving_decisions[0].supersedes_id is None

            surviving_edges = (await session.scalars(select(SessionGraphEdge))).all()
            assert [e.edge_type for e in surviving_edges] == ["ADJACENT_IN_EPISODE"]

            # 2. Delete thread -> 100% of remaining derived units, decisions, and edges cascade-delete!
            await session.execute(text("DELETE FROM threads WHERE id = :tid"), {"tid": thread_id})
            await session.commit()
            session.expire_all()

            assert (await session.scalars(select(SessionUnit.id))).all() == []
            assert (await session.scalars(select(SessionDecisionLedger.id))).all() == []
            assert (await session.scalars(select(SessionGraphEdge.id))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_check_and_unique_constraints_enforced() -> None:
    engine = _create_fk_sqlite_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 28, 7, 0, tzinfo=UTC)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            agent = Agent(owner_key="continuum:thang", name="Constraint Agent")
            session.add(agent)
            await session.flush()
            thread = Thread(agent_id=agent.id, owner_key="continuum:thang", title="Constraint Thread")
            session.add(thread)
            await session.flush()
            msg = Message(thread_id=thread.id, role=MessageRole.USER, content="Kiểm tra ràng buộc")
            session.add(msg)
            await session.commit()
            thread_id, msg_id = thread.id, msg.id

        # 1. Invalid `epistemic_status` rejected
        async with session_factory() as session:
            session.add(
                SessionUnit(
                    id=str(uuid.uuid4()),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread_id,
                    unit_kind="TURN",
                    ordinal=0,
                    episode_id="ep:0",
                    message_from_id=msg_id,
                    message_to_id=msg_id,
                    epistemic_status="INVALID_STATE",
                    content_hash=_sha256("x"),
                    valid_from=now,
                    valid_to=SESSION_OPEN_ENDED_VALID_TO,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        # 2. `valid_to < valid_from` rejected
        async with session_factory() as session:
            session.add(
                SessionUnit(
                    id=str(uuid.uuid4()),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread_id,
                    unit_kind="TURN",
                    ordinal=0,
                    episode_id="ep:0",
                    message_from_id=msg_id,
                    message_to_id=msg_id,
                    epistemic_status="FACT",
                    content_hash=_sha256("x"),
                    valid_from=now,
                    valid_to=now - timedelta(seconds=1),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        # 3. Duplicate `(thread_id, unit_kind, ordinal)` rejected & negative graph weight rejected
        valid_unit_id = generate_session_unit_id(thread_id, "TURN", 0, msg_id)
        async with session_factory() as session:
            session.add(
                SessionUnit(
                    id=valid_unit_id,
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread_id,
                    unit_kind="TURN",
                    ordinal=0,
                    episode_id="ep:0",
                    message_from_id=msg_id,
                    message_to_id=msg_id,
                    epistemic_status="FACT",
                    content_hash=_sha256("ok"),
                    valid_from=now,
                    valid_to=SESSION_OPEN_ENDED_VALID_TO,
                )
            )
            await session.commit()

        async with session_factory() as session:
            session.add(
                SessionUnit(
                    id=str(uuid.uuid4()),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread_id,
                    unit_kind="TURN",
                    ordinal=0,
                    episode_id="ep:0",
                    message_from_id=msg_id,
                    message_to_id=msg_id,
                    epistemic_status="FACT",
                    content_hash=_sha256("dup"),
                    valid_from=now,
                    valid_to=SESSION_OPEN_ENDED_VALID_TO,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

        async with session_factory() as session:
            session.add(
                SessionGraphEdge(
                    id=str(uuid.uuid4()),
                    tenant_id="org_01",
                    project_id="proj_01",
                    thread_id=thread_id,
                    source_unit_id=valid_unit_id,
                    source_node_type="UNIT",
                    source_node_key=f"unit:{valid_unit_id}",
                    target_node_type="ENTITY",
                    target_node_key="entity:pg",
                    edge_type="ENTITY_MENTION",
                    weight=-0.1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
