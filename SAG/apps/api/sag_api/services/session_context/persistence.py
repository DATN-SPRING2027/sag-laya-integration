"""Durable multi-view persistence, cold-start hydration, lazy backfill, and Qdrant sync (Chặng 3).

Bridges in-memory `IncrementalSessionIndex` with PostgreSQL (`session_units`, `session_unit_messages`,
`session_decision_ledger`, `session_graph_edges`) and Qdrant (`session_units_{project_id}`), while
enforcing:
- Fail-closed Continuum tenant/project/source ACL boundaries;
- Data minimization (zero raw conversation text in Qdrant payloads; character spans in `sentences_json`);
- Zero HTTP re-embedding on cold-start worker restarts via Qdrant vector hydration;
- Trigger-compliant O(Delta) incremental append and atomic reconciliation on delete/revocation/drift.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.errors import ForbiddenError
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.core.sanitizer import sanitize_error_message
from sag_api.db.models.session_context import (
    SESSION_OPEN_ENDED_VALID_TO,
    SessionDecisionLedger,
    SessionGraphEdge,
    SessionUnit,
    SessionUnitMessage,
    generate_session_decision_id,
    generate_session_graph_edge_id,
    generate_session_unit_id,
    generate_session_unit_point_id,
)
from sag_api.services.search_index_service import (
    compute_sparse_bm25_vector,
    get_cached_text_embedding,
    put_cached_text_embedding,
)
from sag_api.services.session_context.extractor import (
    extract_content_terms,
    normalize_session_text,
    split_sentences_with_spans,
)
from sag_api.services.session_context.models import (
    DecisionLedgerEntry,
    EntityMention,
    EpisodeSegment,
    IndexedSentence,
    IndexedTurn,
)
from sag_api.services.session_context.substrate import (
    FALLBACK_VECTOR_DIM,
    SOFT_ALIAS_WEIGHT,
    IncrementalSessionIndex,
    deterministic_ngram_vector,
    embed_texts_with_main_cache,
    l2_normalize,
    sparse_dict_from_bm25,
)

log = get_logger("sag.session_context.persistence")

SESSION_QDRANT_PAYLOAD_INDEX_FIELDS: tuple[tuple[str, str], ...] = (
    ("tenant_id", "keyword"),
    ("project_id", "keyword"),
    ("thread_id", "keyword"),
    ("session_unit_id", "keyword"),
    ("unit_kind", "keyword"),
    ("episode_id", "keyword"),
    ("epistemic_status", "keyword"),
    ("topic_slot_key", "keyword"),
    ("security_partition_ids", "keyword"),
    ("cited_source_ids", "keyword"),
    ("entity_keys", "keyword"),
    ("embedding_model_signature", "keyword"),
    ("ordinal", "integer"),
    ("valid_from_ts", "integer"),
    ("valid_to_ts", "integer"),
)

# Process-local cache of verified Qdrant collections: (transport_tag, base_url, collection_name) -> dim
_VERIFIED_SESSION_COLLECTIONS: dict[tuple[str, str, str], int] = {}

# Optional test hook for deterministic ASGI/mock Qdrant client injection without live sockets
_CUSTOM_QDRANT_CLIENT_FACTORY: Callable[[], httpx.AsyncClient | None] | None = None


@dataclass(frozen=True, slots=True)
class SessionPersistenceScope:
    """Verified multi-tenant & project persistence scope for a conversation thread."""

    tenant_id: str
    project_id: str
    thread_id: str
    owner_key: str
    security_partition_ids: tuple[str, ...]


@dataclass(slots=True)
class PersistenceSyncStats:
    """Telemetry counters returned from cold-start hydration or O(Delta) sync."""

    status: str = "memory_only"
    units_written: int = 0
    qdrant_points_upserted: int = 0
    hydrated_turns: int = 0


def set_custom_session_qdrant_client_factory(
    factory: Callable[[], httpx.AsyncClient | None] | None,
) -> None:
    """Register or clear an injectable Qdrant AsyncClient factory (used by unit/integration tests)."""
    global _CUSTOM_QDRANT_CLIENT_FACTORY
    _CUSTOM_QDRANT_CLIENT_FACTORY = factory
    _VERIFIED_SESSION_COLLECTIONS.clear()


def clear_verified_session_collections_cache() -> None:
    """Clear process-cached Qdrant collection dimension metadata."""
    _VERIFIED_SESSION_COLLECTIONS.clear()


def session_qdrant_collection_name(project_id: str) -> str:
    """Return canonical per-project Qdrant collection name for session units."""
    safe_proj = re.sub(r"[^A-Za-z0-9_\-]", "_", str(project_id).strip())
    return f"session_units_{safe_proj}"


def compute_embedding_signature(embedder: Any | None) -> str:
    """Compute non-empty deterministic signature identifying the active embedding space."""
    if embedder is None:
        return "deterministic-ngram-v1"
    model_name = str(getattr(embedder, "_model", "") or getattr(embedder, "model", "") or "").strip()
    if model_name:
        return f"http:{model_name}"[:128]
    return f"embedder:{type(embedder).__name__}"[:128]


def _ts_to_utc(ts: float | None) -> datetime:
    if ts is None or ts <= 0:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return datetime.fromtimestamp(float(ts), tz=UTC)


def _is_open_ended(dt: datetime | None) -> bool:
    if dt is None:
        return True
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return aware.year >= 9999


def _session_dialect_name(session: AsyncSession) -> str:
    try:
        bind = session.get_bind()
        return str(bind.dialect.name or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_missing_session_tables_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "session_units" in msg and (
        "no such table" in msg or "does not exist" in msg or "undefinedtable" in msg
    )


def _open_qdrant_client_context(
    session: AsyncSession,
    explicit_client: httpx.AsyncClient | None = None,
) -> tuple[httpx.AsyncClient | None, bool]:
    """Resolve `(client, should_close)` without polluting live localhost Qdrant during SQLite tests."""
    if explicit_client is not None:
        return explicit_client, False
    if not settings.session_context_qdrant_sync_enabled:
        return None, False
    if _CUSTOM_QDRANT_CLIENT_FACTORY is not None:
        client = _CUSTOM_QDRANT_CLIENT_FACTORY()
        return client, False
    if settings.sag_vector_provider != "qdrant":
        return None, False
    if _session_dialect_name(session) == "sqlite":
        return None, False
    qdrant_url = (settings.sag_qdrant_url or "").strip()
    if not qdrant_url:
        return None, False
    headers: dict[str, str] = {}
    if settings.sag_qdrant_api_key:
        headers["api-key"] = settings.sag_qdrant_api_key
    return httpx.AsyncClient(base_url=qdrant_url, headers=headers, timeout=4.0), True


async def resolve_persistence_scope(
    session: AsyncSession,
    *,
    thread_id: str,
    principal: VerifiedPrincipal,
    owner_key: str,
) -> SessionPersistenceScope | None:
    """Resolve and verify `(tenant_id, project_id)` fail-closed against existing persisted thread rows."""
    if not settings.session_context_persistence_enabled or not thread_id:
        return None
    tenant_id = str(
        getattr(principal, "tenant_id", "") or getattr(principal, "organization_id", "") or ""
    ).strip()
    allowed_projects = sorted(
        str(p).strip()
        for p in (getattr(principal, "allowed_project_ids", ()) or ())
        if str(p).strip()
    )
    allowed_partitions = tuple(
        sorted(
            str(p).strip()
            for p in (getattr(principal, "allowed_partition_ids", ()) or ())
            if str(p).strip()
        )
    )
    if not tenant_id or not allowed_projects:
        return None

    rows = (
        await session.execute(
            select(SessionUnit.tenant_id, SessionUnit.project_id)
            .where(SessionUnit.thread_id == thread_id)
            .distinct()
        )
    ).all()
    if rows:
        distinct_scopes = {(str(r[0]), str(r[1])) for r in rows}
        for ex_tenant, ex_proj in distinct_scopes:
            if ex_tenant != tenant_id or ex_proj not in allowed_projects:
                log.warning(
                    "Blocked cross-scope session_units access thread=%s principal_tenant=%s",
                    thread_id,
                    tenant_id,
                )
                raise ForbiddenError("Session context scope mismatch for thread")
        if len(distinct_scopes) > 1:
            raise ForbiddenError("Ambiguous persisted project scopes for thread")
        project_id = next(iter(distinct_scopes))[1]
    else:
        project_id = allowed_projects[0]

    partitions = allowed_partitions if allowed_partitions else (project_id,)
    return SessionPersistenceScope(
        tenant_id=tenant_id,
        project_id=project_id,
        thread_id=thread_id,
        owner_key=owner_key,
        security_partition_ids=partitions,
    )


async def ensure_session_qdrant_collection(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    vector_dim: int,
) -> str:
    """Ensure `session_units_{project_id}` exists with matching vector_dim & filter indexes.

    Because `session_units_{project_id}` is 100% derived from raw PostgreSQL `messages`,
    any embedding dimension change safely recreates the derived collection.
    """
    collection_name = session_qdrant_collection_name(project_id)
    transport = getattr(qdrant_client, "_transport", None)
    transport_tag = (
        "http" if isinstance(transport, httpx.AsyncHTTPTransport) else f"mock:{id(transport)}"
    )
    cache_key = (transport_tag, str(qdrant_client.base_url), collection_name)
    if _VERIFIED_SESSION_COLLECTIONS.get(cache_key) == vector_dim:
        return collection_name

    needs_create = True
    info_res = await qdrant_client.get(f"/collections/{collection_name}")
    if info_res.is_success:
        cfg = info_res.json().get("result", {}).get("config", {})
        existing_vectors = cfg.get("params", {}).get("vectors", {})
        existing_dim = (
            existing_vectors.get("content_vector", {}).get("size")
            if isinstance(existing_vectors, dict)
            else None
        )
        if existing_dim == vector_dim:
            needs_create = False
        elif existing_dim and existing_dim != vector_dim:
            log.info(
                "Recreating derived Qdrant collection %s on dimension change (%s -> %s)",
                collection_name,
                existing_dim,
                vector_dim,
            )
            del_col = await qdrant_client.delete(f"/collections/{collection_name}")
            if del_col.status_code >= 400 and del_col.status_code != 404:
                del_col.raise_for_status()
            needs_create = True

    if needs_create:
        col_res = await qdrant_client.put(
            f"/collections/{collection_name}",
            json={
                "vectors": {
                    "content_vector": {
                        "size": int(vector_dim),
                        "distance": "Cosine",
                    }
                },
                "sparse_vectors": {
                    "bm25_sparse": {
                        "modifier": "idf",
                    }
                },
            },
        )
        if col_res.status_code >= 400 and col_res.status_code != 409:
            if "already exists" not in col_res.text.lower():
                raise RuntimeError(
                    f"Failed to create Qdrant collection {collection_name}: "
                    f"HTTP {col_res.status_code} - {col_res.text}"
                )

    async def _create_idx(field_name: str, field_schema: str) -> None:
        idx_res = await qdrant_client.put(
            f"/collections/{collection_name}/index?wait=true",
            json={"field_name": field_name, "field_schema": field_schema},
        )
        if idx_res.status_code >= 400 and idx_res.status_code != 409:
            if "already exists" not in idx_res.text.lower():
                raise RuntimeError(
                    f"Failed to create Qdrant index {field_name} on {collection_name}: {idx_res.text}"
                )

    await asyncio.gather(
        *(_create_idx(fname, fschema) for fname, fschema in SESSION_QDRANT_PAYLOAD_INDEX_FIELDS)
    )
    _VERIFIED_SESSION_COLLECTIONS[cache_key] = int(vector_dim)
    return collection_name


def build_session_unit_qdrant_payload(unit: SessionUnit) -> dict[str, Any]:
    """Build Qdrant metadata payload with ZERO raw conversation text (Data Minimization)."""
    entity_keys = [
        str(item["key"])
        for item in (unit.entity_mentions_json or [])
        if isinstance(item, dict) and item.get("key")
    ]
    valid_from_aware = (
        unit.valid_from if unit.valid_from.tzinfo is not None else unit.valid_from.replace(tzinfo=UTC)
    )
    valid_to_aware = (
        unit.valid_to if unit.valid_to.tzinfo is not None else unit.valid_to.replace(tzinfo=UTC)
    )
    return {
        "session_unit_id": unit.id,
        "tenant_id": unit.tenant_id,
        "project_id": unit.project_id,
        "thread_id": unit.thread_id,
        "unit_kind": unit.unit_kind,
        "ordinal": int(unit.ordinal),
        "episode_id": unit.episode_id,
        "episode_ordinal": int(unit.episode_ordinal),
        "window_id": unit.window_id,
        "message_from_id": unit.message_from_id,
        "message_to_id": unit.message_to_id,
        "role": unit.role,
        "epistemic_status": unit.epistemic_status,
        "topic_slot_key": unit.topic_slot_key,
        "security_partition_ids": list(unit.security_partition_ids_json or []),
        "cited_source_ids": list(unit.cited_source_ids_json or []),
        "entity_keys": entity_keys,
        "token_count": int(unit.token_count),
        "content_hash": unit.content_hash,
        "embedding_model_signature": unit.embedding_model_signature,
        "valid_from_ts": int(valid_from_aware.timestamp()),
        "valid_to_ts": int(valid_to_aware.timestamp()),
    }


async def upsert_session_units_to_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    items: Sequence[tuple[SessionUnit, Sequence[float], dict[str, list]]],
) -> int:
    """Upsert dense + BM25 sparse vectors for `SessionUnit` rows into `session_units_{project_id}`."""
    if not items:
        return 0
    first_vec = items[0][1]
    vector_dim = len(first_vec) if first_vec else FALLBACK_VECTOR_DIM
    collection_name = await ensure_session_qdrant_collection(
        qdrant_client,
        project_id=project_id,
        vector_dim=vector_dim,
    )
    points: list[dict[str, Any]] = []
    for unit, dense_vec, sparse_vec in items:
        point_id = unit.qdrant_point_id or generate_session_unit_point_id(collection_name, unit.id)
        points.append(
            {
                "id": point_id,
                "payload": build_session_unit_qdrant_payload(unit),
                "vector": {
                    "content_vector": [float(v) for v in dense_vec],
                    "bm25_sparse": sparse_vec,
                },
            }
        )
    res = await qdrant_client.put(
        f"/collections/{collection_name}/points?wait=true",
        json={"points": points},
    )
    res.raise_for_status()
    return len(points)


async def fetch_thread_turn_vectors_from_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    tenant_id: str,
    project_id: str,
    thread_id: str,
) -> dict[str, tuple[float, ...]]:
    """Scroll dense `TURN` vectors for a thread from Qdrant (`0` HTTP embedder calls on cold start)."""
    collection_name = session_qdrant_collection_name(project_id)
    vectors_by_unit_id: dict[str, tuple[float, ...]] = {}
    next_offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 256,
            "with_payload": ["session_unit_id", "unit_kind", "tenant_id", "thread_id"],
            "with_vector": True,
            "filter": {
                "must": [
                    {"key": "tenant_id", "match": {"value": tenant_id}},
                    {"key": "project_id", "match": {"value": project_id}},
                    {"key": "thread_id", "match": {"value": thread_id}},
                    {"key": "unit_kind", "match": {"value": "TURN"}},
                ]
            },
        }
        if next_offset is not None:
            body["offset"] = next_offset
        res = await qdrant_client.post(f"/collections/{collection_name}/points/scroll", json=body)
        if not res.is_success:
            break
        result = res.json().get("result", {})
        points = result.get("points") or []
        for pt in points:
            payload = pt.get("payload") or {}
            uid = str(payload.get("session_unit_id") or "")
            vec_field = pt.get("vector") or {}
            raw_vec = (
                vec_field.get("content_vector")
                if isinstance(vec_field, dict)
                else (vec_field if isinstance(vec_field, list) else None)
            )
            if uid and isinstance(raw_vec, list) and raw_vec:
                vectors_by_unit_id[uid] = l2_normalize(raw_vec)
        next_offset = result.get("next_page_offset")
        if next_offset is None or not points:
            break
    return vectors_by_unit_id


async def delete_thread_points_from_qdrant(
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
    thread_id: str,
) -> None:
    """Delete all Qdrant points belonging to `thread_id` in `session_units_{project_id}`."""
    collection_name = session_qdrant_collection_name(project_id)
    del_res = await qdrant_client.post(
        f"/collections/{collection_name}/points/delete?wait=true",
        json={
            "filter": {
                "must": [
                    {"key": "thread_id", "match": {"value": str(thread_id)}},
                ]
            }
        },
    )
    if del_res.status_code >= 400 and del_res.status_code != 404:
        del_res.raise_for_status()


async def reconcile_orphan_session_units(
    session: AsyncSession,
    qdrant_client: httpx.AsyncClient,
    *,
    project_id: str,
) -> dict[str, int]:
    """Identify and delete orphan Qdrant points in `session_units_{project_id}` missing from PostgreSQL."""
    collection_name = session_qdrant_collection_name(project_id)
    all_points: list[dict[str, Any]] = []
    next_offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 500,
            "with_payload": ["session_unit_id", "thread_id"],
            "with_vector": False,
        }
        if next_offset is not None:
            body["offset"] = next_offset
        res = await qdrant_client.post(f"/collections/{collection_name}/points/scroll", json=body)
        if not res.is_success:
            break
        data = res.json().get("result", {})
        points = data.get("points") or []
        all_points.extend(points)
        next_offset = data.get("next_page_offset")
        if next_offset is None or not points:
            break

    if not all_points:
        return {"scanned": 0, "orphans_deleted": 0}

    point_unit_map = {
        p["id"]: (p.get("payload") or {}).get("session_unit_id")
        for p in all_points
        if p.get("id")
    }
    valid_unit_ids: set[str] = set()
    candidate_ids = [str(uid) for uid in point_unit_map.values() if uid]
    for idx in range(0, len(candidate_ids), 500):
        batch = candidate_ids[idx : idx + 500]
        rows = (
            await session.execute(
                select(SessionUnit.id).where(
                    SessionUnit.project_id == project_id,
                    SessionUnit.id.in_(batch),
                )
            )
        ).scalars().all()
        valid_unit_ids.update(str(r) for r in rows)

    orphan_point_ids = [
        pid for pid, uid in point_unit_map.items() if not uid or str(uid) not in valid_unit_ids
    ]
    if orphan_point_ids:
        del_res = await qdrant_client.post(
            f"/collections/{collection_name}/points/delete?wait=true",
            json={"points": orphan_point_ids},
        )
        if del_res.status_code >= 400 and del_res.status_code != 404:
            raise RuntimeError(f"Failed to delete orphan session Qdrant points: {del_res.text}")
    return {"scanned": len(all_points), "orphans_deleted": len(orphan_point_ids)}


async def collect_thread_persistence_projects(
    session: AsyncSession,
    *,
    thread_id: str,
) -> list[str]:
    """Return distinct `project_id` values currently persisted for `thread_id`."""
    if not settings.session_context_persistence_enabled or not thread_id:
        return []
    try:
        rows = (
            await session.execute(
                select(SessionUnit.project_id)
                .where(SessionUnit.thread_id == thread_id)
                .distinct()
            )
        ).scalars().all()
        return sorted({str(p) for p in rows if p})
    except Exception as exc:  # noqa: BLE001
        if _is_missing_session_tables_error(exc):
            return []
        log.warning("Could not inspect persisted project scopes for thread=%s: %s", thread_id, exc)
        return []


def _expunge_derived_from_identity_map(
    session: AsyncSession,
    *,
    thread_id: str,
    only_unit_id: str | None = None,
) -> None:
    sync_sess = session.sync_session
    for obj in list(sync_sess.identity_map.values()):
        if not isinstance(
            obj,
            (SessionUnit, SessionUnitMessage, SessionDecisionLedger, SessionGraphEdge),
        ):
            continue
        if getattr(obj, "thread_id", None) != thread_id:
            continue
        if only_unit_id is not None:
            if isinstance(obj, SessionUnit) and obj.id != only_unit_id:
                continue
            if isinstance(obj, SessionUnitMessage) and obj.session_unit_id != only_unit_id:
                continue
            if isinstance(obj, SessionDecisionLedger):
                continue
            if (
                isinstance(obj, SessionGraphEdge)
                and obj.source_unit_id != only_unit_id
                and obj.target_unit_id != only_unit_id
            ):
                continue
        sync_sess.expunge(obj)


async def purge_thread_session_rows(
    session: AsyncSession,
    *,
    thread_id: str,
) -> None:
    """Delete derived PostgreSQL/SQLite session rows in child-to-parent order."""
    if not thread_id:
        return
    try:
        async with session.begin_nested():
            await session.execute(
                delete(SessionGraphEdge).where(SessionGraphEdge.thread_id == thread_id)
            )
            await session.execute(
                delete(SessionDecisionLedger).where(SessionDecisionLedger.thread_id == thread_id)
            )
            await session.execute(
                delete(SessionUnitMessage).where(SessionUnitMessage.thread_id == thread_id)
            )
            await session.execute(
                delete(SessionUnit).where(SessionUnit.thread_id == thread_id)
            )
            await session.flush()
        _expunge_derived_from_identity_map(session, thread_id=thread_id)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_session_tables_error(exc):
            raise


async def purge_thread_qdrant_points_for_projects(
    session: AsyncSession,
    *,
    thread_id: str,
    project_ids: Sequence[str],
    qdrant_client: httpx.AsyncClient | None = None,
) -> None:
    """Best-effort Qdrant cleanup for `thread_id` across `project_ids` after DB delete/purge."""
    if not project_ids or not thread_id:
        return
    client, should_close = _open_qdrant_client_context(session, qdrant_client)
    if client is None:
        return
    try:
        for proj_id in project_ids:
            try:
                await delete_thread_points_from_qdrant(
                    client,
                    project_id=proj_id,
                    thread_id=thread_id,
                )
            except Exception as q_exc:  # noqa: BLE001
                log.warning(
                    "Best-effort Qdrant thread cleanup skipped project=%s thread=%s err=%s",
                    proj_id,
                    thread_id,
                    sanitize_error_message(q_exc),
                )
    finally:
        if should_close:
            await client.aclose()


def _serialize_sentence_spans(turn: IndexedTurn) -> list[dict[str, Any]]:
    """Persist sentence character offsets `[start, end)` + entity keys (zero raw sentence strings)."""
    raw_spans = split_sentences_with_spans(turn.content)
    serialized: list[dict[str, Any]] = []
    search_cursor = 0
    for idx, sent in enumerate(turn.sentences):
        if idx < len(raw_spans):
            s_start, s_end, _ = raw_spans[idx]
        else:
            pos = turn.content.find(sent.text, search_cursor)
            if pos >= 0:
                s_start, s_end = pos, pos + len(sent.text)
                search_cursor = s_end
            else:
                s_start, s_end = 0, len(turn.content)
        serialized.append(
            {
                "sentence_id": sent.sentence_id,
                "ordinal": int(sent.ordinal),
                "start": int(max(0, s_start)),
                "end": int(min(len(turn.content), max(s_start, s_end))),
                "entity_keys": list(sent.entity_keys),
            }
        )
    return serialized


def _turn_valid_window(
    turn: IndexedTurn,
    turns_by_id: dict[str, IndexedTurn],
) -> tuple[datetime, datetime]:
    valid_from = _ts_to_utc(turn.created_at_ts)
    if turn.epistemic_status == "NEGATED":
        return valid_from, valid_from
    if turn.superseded_by_turn_id:
        sup_turn = turns_by_id.get(turn.superseded_by_turn_id)
        sup_dt = _ts_to_utc(sup_turn.created_at_ts) if sup_turn is not None else valid_from
        return valid_from, max(valid_from, sup_dt)
    return valid_from, SESSION_OPEN_ENDED_VALID_TO


def _build_turn_session_unit(
    turn: IndexedTurn,
    *,
    scope: SessionPersistenceScope,
    episode_ordinal: int,
    embedding_sig: str,
    collection_name: str,
    turns_by_id: dict[str, IndexedTurn],
) -> SessionUnit:
    unit_id = generate_session_unit_id(scope.thread_id, "TURN", turn.ordinal, turn.turn_id)
    point_id = generate_session_unit_point_id(collection_name, unit_id)
    valid_from, valid_to = _turn_valid_window(turn, turns_by_id)
    return SessionUnit(
        id=unit_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        thread_id=scope.thread_id,
        owner_key=scope.owner_key or None,
        unit_kind="TURN",
        ordinal=turn.ordinal,
        episode_id=turn.episode_id,
        episode_ordinal=episode_ordinal,
        window_id=turn.window_id,
        message_from_id=turn.turn_id,
        message_to_id=turn.turn_id,
        source_message_ids_json=[turn.turn_id],
        role=turn.role,
        epistemic_status=turn.epistemic_status,
        topic_slot_key=turn.topic_slot_key,
        is_anaphoric_or_short=bool(turn.is_anaphoric_or_short),
        security_partition_ids_json=list(scope.security_partition_ids),
        cited_source_ids_json=list(turn.cited_source_ids),
        entity_mentions_json=[
            {
                "key": m.key,
                "surface": m.surface,
                "soft_aliases": list(m.soft_aliases),
                "confidence": round(float(m.confidence), 4),
            }
            for m in turn.entities
        ],
        sentences_json=_serialize_sentence_spans(turn),
        term_freqs_json=dict(turn.term_freqs),
        doc_word_len=max(0, int(turn.doc_word_len)),
        token_count=max(0, int(turn.token_count)),
        content_hash=turn.content_hash,
        embedding_model_signature=embedding_sig,
        qdrant_point_id=point_id,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _build_span_session_unit(
    *,
    unit_kind: str,
    ordinal: int,
    episode_id: str,
    episode_ordinal: int,
    window_id: str | None,
    turn_ids: Sequence[str],
    scope: SessionPersistenceScope,
    embedding_sig: str,
    collection_name: str,
    turns_by_id: dict[str, IndexedTurn],
) -> SessionUnit:
    dedup_turn_ids = list(dict.fromkeys(str(tid) for tid in turn_ids if tid in turns_by_id))
    span_turns = [turns_by_id[tid] for tid in dedup_turn_ids]
    first_tid = dedup_turn_ids[0]
    last_tid = dedup_turn_ids[-1]
    unit_id = generate_session_unit_id(scope.thread_id, unit_kind, ordinal, first_tid)
    point_id = generate_session_unit_point_id(collection_name, unit_id)

    combined_hash = hashlib.sha256(
        "|".join(t.content_hash for t in span_turns).encode("utf-8")
    ).hexdigest()
    cited_union = sorted({src for t in span_turns for src in t.cited_source_ids})
    merged_entities: dict[str, dict[str, Any]] = {}
    for t in span_turns:
        for m in t.entities:
            merged_entities.setdefault(
                m.key,
                {
                    "key": m.key,
                    "surface": m.surface,
                    "soft_aliases": list(m.soft_aliases),
                    "confidence": round(float(m.confidence), 4),
                },
            )
    valid_from = _ts_to_utc(span_turns[0].created_at_ts)
    return SessionUnit(
        id=unit_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        thread_id=scope.thread_id,
        owner_key=scope.owner_key or None,
        unit_kind=unit_kind,
        ordinal=ordinal,
        episode_id=episode_id,
        episode_ordinal=episode_ordinal,
        window_id=window_id,
        message_from_id=first_tid,
        message_to_id=last_tid,
        source_message_ids_json=dedup_turn_ids,
        role=None,
        epistemic_status="FACT",
        topic_slot_key=None,
        is_anaphoric_or_short=False,
        security_partition_ids_json=list(scope.security_partition_ids),
        cited_source_ids_json=cited_union,
        entity_mentions_json=list(merged_entities.values()),
        sentences_json=[],
        term_freqs_json={},
        doc_word_len=sum(max(1, t.doc_word_len) for t in span_turns),
        token_count=sum(max(1, t.token_count) for t in span_turns),
        content_hash=combined_hash,
        embedding_model_signature=embedding_sig,
        qdrant_point_id=point_id,
        valid_from=valid_from,
        valid_to=SESSION_OPEN_ENDED_VALID_TO,
    )


def _build_turn_graph_edges(
    turn: IndexedTurn,
    *,
    index: IncrementalSessionIndex,
    scope: SessionPersistenceScope,
    turn_unit_ids: dict[str, str],
) -> list[SessionGraphEdge]:
    u_id = turn_unit_ids[turn.turn_id]
    dedup_edges: dict[tuple[str, str, str, str, str, str], SessionGraphEdge] = {}

    def _add_edge(
        *,
        source_unit_id: str,
        target_unit_id: str | None,
        source_node_type: str,
        source_node_key: str,
        target_node_type: str,
        target_node_key: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        s_key = source_node_key[:256].strip()
        t_key = target_node_key[:256].strip()
        if not s_key or not t_key:
            return
        prov_key = (source_unit_id, source_node_type, s_key, target_node_type, t_key, edge_type)
        if prov_key in dedup_edges:
            return
        edge_id = generate_session_graph_edge_id(
            source_unit_id,
            s_key,
            t_key,
            edge_type,
            source_node_type=source_node_type,
            target_node_type=target_node_type,
        )
        dedup_edges[prov_key] = SessionGraphEdge(
            id=edge_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            thread_id=scope.thread_id,
            source_unit_id=source_unit_id,
            target_unit_id=target_unit_id,
            source_node_type=source_node_type,
            source_node_key=s_key,
            target_node_type=target_node_type,
            target_node_key=t_key,
            edge_type=edge_type,
            weight=max(0.0, float(weight)),
            metadata_json=metadata or {},
        )

    for mention in turn.entities:
        occ = index.entity_to_turn_counts.get(mention.key, {}).get(turn.turn_id, 1)
        _add_edge(
            source_unit_id=u_id,
            target_unit_id=None,
            source_node_type="UNIT",
            source_node_key=u_id,
            target_node_type="ENTITY",
            target_node_key=mention.key,
            edge_type="ENTITY_MENTION",
            weight=float(max(1, occ)),
            metadata={"surface": mention.surface},
        )
        for alias_key in mention.soft_aliases:
            if alias_key and alias_key != mention.key:
                _add_edge(
                    source_unit_id=u_id,
                    target_unit_id=None,
                    source_node_type="ENTITY",
                    source_node_key=mention.key,
                    target_node_type="ENTITY",
                    target_node_key=alias_key,
                    edge_type="ALIAS_CANDIDATE",
                    weight=SOFT_ALIAS_WEIGHT,
                )

    for sent in turn.sentences:
        _add_edge(
            source_unit_id=u_id,
            target_unit_id=u_id,
            source_node_type="SENTENCE",
            source_node_key=sent.sentence_id,
            target_node_type="UNIT",
            target_node_key=u_id,
            edge_type="SENTENCE_IN_UNIT",
            weight=1.0,
        )
        for e_key in dict.fromkeys(sent.entity_keys):
            _add_edge(
                source_unit_id=u_id,
                target_unit_id=None,
                source_node_type="SENTENCE",
                source_node_key=sent.sentence_id,
                target_node_type="ENTITY",
                target_node_key=e_key,
                edge_type="ENTITY_MENTION",
                weight=1.0,
            )

    if turn.ordinal > 0 and turn.ordinal < len(index.turns):
        prev_turn = index.turns[turn.ordinal - 1]
        if prev_turn.episode_id == turn.episode_id and prev_turn.turn_id in turn_unit_ids:
            prev_u_id = turn_unit_ids[prev_turn.turn_id]
            _add_edge(
                source_unit_id=prev_u_id,
                target_unit_id=u_id,
                source_node_type="UNIT",
                source_node_key=prev_u_id,
                target_node_type="UNIT",
                target_node_key=u_id,
                edge_type="ADJACENT_IN_EPISODE",
                weight=0.35,
            )

    for sup_tid in turn.supersedes_turn_ids:
        old_u_id = turn_unit_ids.get(sup_tid)
        if old_u_id:
            _add_edge(
                source_unit_id=u_id,
                target_unit_id=old_u_id,
                source_node_type="UNIT",
                source_node_key=u_id,
                target_node_type="UNIT",
                target_node_key=old_u_id,
                edge_type="SUPERSEDES",
                weight=1.0,
            )

    for src_id in turn.cited_source_ids:
        _add_edge(
            source_unit_id=u_id,
            target_unit_id=None,
            source_node_type="UNIT",
            source_node_key=u_id,
            target_node_type="SOURCE",
            target_node_key=str(src_id),
            edge_type="CITES_DOCUMENT_SOURCE",
            weight=1.0,
        )

    return list(dedup_edges.values())


def _sparse_qdrant_dict_for_turn(turn: IndexedTurn) -> dict[str, list]:
    sorted_indices = sorted(turn.sparse_bm25.keys())
    return {
        "indices": sorted_indices,
        "values": [float(turn.sparse_bm25[idx]) for idx in sorted_indices],
    }


def _sparse_qdrant_dict_for_span(
    turn_ids: Sequence[str],
    *,
    index: IncrementalSessionIndex,
) -> dict[str, list]:
    joined_text = "\n".join(
        index.turns_by_id[tid].content for tid in turn_ids if tid in index.turns_by_id
    )
    avg_len = max(8.0, index.total_token_words / max(1, len(index.turns)))
    return compute_sparse_bm25_vector(
        joined_text,
        avg_doc_len=avg_len,
        corpus_doc_freqs=index.doc_freqs,
        total_corpus_docs=max(50, len(index.turns)),
    )


async def sync_session_index_to_storage(
    session: AsyncSession,
    *,
    index: IncrementalSessionIndex,
    scope: SessionPersistenceScope,
    embedding_sig: str,
    mode: str,
    start_turn_ordinal: int = 0,
    start_window_ordinal: int = 0,
    start_episode_ordinal: int = 0,
    start_decision_idx: int = 0,
    mutated_prior_turn_ids: set[str] | None = None,
    grown_episode_ordinal: int | None = None,
    qdrant_client: httpx.AsyncClient | None = None,
    commit_if_clean: bool = True,
    caller_had_pending_state: bool = False,
) -> PersistenceSyncStats:
    """Persist `IncrementalSessionIndex` (`mode='rebuild'` or `mode='delta'`) to PostgreSQL & Qdrant."""
    stats = PersistenceSyncStats(status=mode)
    collection_name = session_qdrant_collection_name(scope.project_id)
    ep_ord_by_id = {ep.episode_id: ep.ordinal for ep in index.episodes}
    turn_unit_ids = {
        t.turn_id: generate_session_unit_id(scope.thread_id, "TURN", t.ordinal, t.turn_id)
        for t in index.turns
    }
    decision_ids_by_turn: dict[str, str] = {}
    for dec in index.decision_ledger:
        u_id = turn_unit_ids.get(dec.turn_id)
        if u_id:
            decision_ids_by_turn[dec.turn_id] = generate_session_decision_id(u_id, dec.topic_slot_key)

    qdrant_upsert_batch: list[tuple[SessionUnit, Sequence[float], dict[str, list]]] = []

    async with session.begin_nested():
        if mode == "rebuild":
            await purge_thread_session_rows(session, thread_id=scope.thread_id)
            start_turn_ordinal = 0
            start_window_ordinal = 0
            start_episode_ordinal = 0
            start_decision_idx = 0
            mutated_prior_turn_ids = set()
            grown_episode_ordinal = None

        # 1. Update newly superseded prior TURN units (`epistemic_status` & `valid_to` only)
        for prior_tid in sorted(mutated_prior_turn_ids or ()):
            prior_turn = index.turns_by_id.get(prior_tid)
            if prior_turn is None or prior_turn.ordinal >= start_turn_ordinal:
                continue
            prior_uid = turn_unit_ids[prior_tid]
            _, new_valid_to = _turn_valid_window(prior_turn, index.turns_by_id)
            await session.execute(
                update(SessionUnit)
                .where(SessionUnit.id == prior_uid)
                .values(
                    epistemic_status=prior_turn.epistemic_status,
                    valid_to=new_valid_to,
                )
            )
            rebuilt_unit = _build_turn_session_unit(
                prior_turn,
                scope=scope,
                episode_ordinal=ep_ord_by_id.get(prior_turn.episode_id, 0),
                embedding_sig=embedding_sig,
                collection_name=collection_name,
                turns_by_id=index.turns_by_id,
            )
            qdrant_upsert_batch.append(
                (rebuilt_unit, prior_turn.dense_vector, _sparse_qdrant_dict_for_turn(prior_turn))
            )

        # 2. Insert new TURN units
        new_turn_units: list[SessionUnit] = []
        for turn in index.turns[start_turn_ordinal:]:
            u_row = _build_turn_session_unit(
                turn,
                scope=scope,
                episode_ordinal=ep_ord_by_id.get(turn.episode_id, 0),
                embedding_sig=embedding_sig,
                collection_name=collection_name,
                turns_by_id=index.turns_by_id,
            )
            new_turn_units.append(u_row)
            qdrant_upsert_batch.append(
                (u_row, turn.dense_vector, _sparse_qdrant_dict_for_turn(turn))
            )
        if new_turn_units:
            session.add_all(new_turn_units)

        # 3. Insert new WINDOW units
        new_window_units: list[SessionUnit] = []
        for win in index.windows[start_window_ordinal:]:
            if not win.turn_ids:
                continue
            w_row = _build_span_session_unit(
                unit_kind="WINDOW",
                ordinal=win.ordinal,
                episode_id=win.episode_id,
                episode_ordinal=ep_ord_by_id.get(win.episode_id, 0),
                window_id=win.window_id,
                turn_ids=win.turn_ids,
                scope=scope,
                embedding_sig=embedding_sig,
                collection_name=collection_name,
                turns_by_id=index.turns_by_id,
            )
            new_window_units.append(w_row)
            qdrant_upsert_batch.append(
                (
                    w_row,
                    win.dense_vector,
                    _sparse_qdrant_dict_for_span(win.turn_ids, index=index),
                )
            )
        if new_window_units:
            session.add_all(new_window_units)

        # 4. Replace grown active EPISODE unit via DELETE + INSERT (respects immutable update trigger)
        if grown_episode_ordinal is not None and 0 <= grown_episode_ordinal < start_episode_ordinal:
            grown_ep = index.episodes[grown_episode_ordinal]
            grown_ep_uid = generate_session_unit_id(
                scope.thread_id, "EPISODE", grown_ep.ordinal, grown_ep.turn_ids[0]
            )
            await session.execute(
                delete(SessionGraphEdge).where(
                    or_(
                        SessionGraphEdge.source_unit_id == grown_ep_uid,
                        SessionGraphEdge.target_unit_id == grown_ep_uid,
                    )
                )
            )
            await session.execute(
                delete(SessionUnitMessage).where(SessionUnitMessage.session_unit_id == grown_ep_uid)
            )
            await session.execute(delete(SessionUnit).where(SessionUnit.id == grown_ep_uid))
            await session.flush()
            _expunge_derived_from_identity_map(
                session,
                thread_id=scope.thread_id,
                only_unit_id=grown_ep_uid,
            )

            ep_row = _build_span_session_unit(
                unit_kind="EPISODE",
                ordinal=grown_ep.ordinal,
                episode_id=grown_ep.episode_id,
                episode_ordinal=grown_ep.ordinal,
                window_id=None,
                turn_ids=grown_ep.turn_ids,
                scope=scope,
                embedding_sig=embedding_sig,
                collection_name=collection_name,
                turns_by_id=index.turns_by_id,
            )
            session.add(ep_row)
            qdrant_upsert_batch.append(
                (
                    ep_row,
                    grown_ep.centroid,
                    _sparse_qdrant_dict_for_span(grown_ep.turn_ids, index=index),
                )
            )
            stats.units_written += 1

        # 5. Insert brand-new EPISODE units
        new_episode_units: list[SessionUnit] = []
        for ep in index.episodes[start_episode_ordinal:]:
            if not ep.turn_ids:
                continue
            ep_row = _build_span_session_unit(
                unit_kind="EPISODE",
                ordinal=ep.ordinal,
                episode_id=ep.episode_id,
                episode_ordinal=ep.ordinal,
                window_id=None,
                turn_ids=ep.turn_ids,
                scope=scope,
                embedding_sig=embedding_sig,
                collection_name=collection_name,
                turns_by_id=index.turns_by_id,
            )
            new_episode_units.append(ep_row)
            qdrant_upsert_batch.append(
                (
                    ep_row,
                    ep.centroid,
                    _sparse_qdrant_dict_for_span(ep.turn_ids, index=index),
                )
            )
        if new_episode_units:
            session.add_all(new_episode_units)

        await session.flush()
        stats.units_written += (
            len(new_turn_units) + len(new_window_units) + len(new_episode_units)
        )

        # 6. Insert new SessionDecisionLedger entries in chronological flush order (lineage-safe)
        for dec_idx in range(start_decision_idx, len(index.decision_ledger)):
            entry = index.decision_ledger[dec_idx]
            u_id = turn_unit_ids.get(entry.turn_id)
            turn_obj = index.turns_by_id.get(entry.turn_id)
            if not u_id or turn_obj is None:
                continue
            dec_id = decision_ids_by_turn[entry.turn_id]
            superseded_dec_ids = [
                decision_ids_by_turn[tid]
                for tid in entry.supersedes_turn_ids
                if tid in decision_ids_by_turn
            ]
            primary_supersedes_id = superseded_dec_ids[-1] if superseded_dec_ids else None
            valid_from = _ts_to_utc(entry.valid_from_ts)
            ledger_kind = "OPEN_ISSUE" if entry.epistemic_status == "OPEN_ISSUE" else "DECISION"
            companion_uids = [
                turn_unit_ids[cid] for cid in entry.companion_turn_ids if cid in turn_unit_ids
            ]
            dec_row = SessionDecisionLedger(
                id=dec_id,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                thread_id=scope.thread_id,
                session_unit_id=u_id,
                ledger_kind=ledger_kind,
                topic_slot_key=entry.topic_slot_key,
                turn_ordinal=entry.turn_ordinal,
                epistemic_status=(
                    "NEGATED" if turn_obj.epistemic_status == "NEGATED" else turn_obj.epistemic_status
                )
                if entry.superseded_by_turn_id is None
                else entry.epistemic_status,
                summary_text=entry.summary_text,
                entity_keys_json=list(entry.entity_keys),
                superseded_entity_keys_json=list(turn_obj.superseded_entity_keys),
                companion_unit_ids_json=companion_uids,
                supersedes_id=primary_supersedes_id,
                superseded_by_id=None,
                supersedes_ids_json=superseded_dec_ids,
                security_partition_ids_json=list(scope.security_partition_ids),
                cited_source_ids_json=list(turn_obj.cited_source_ids),
                valid_from=valid_from,
                valid_to=(
                    valid_from
                    if turn_obj.epistemic_status == "NEGATED" and entry.superseded_by_turn_id is None
                    else SESSION_OPEN_ENDED_VALID_TO
                ),
            )
            session.add(dec_row)
            await session.flush()

            # Close any prior decision entries superseded or confirmed by `entry.turn_id`
            for prior_idx in range(0, dec_idx):
                prior_entry = index.decision_ledger[prior_idx]
                if prior_entry.superseded_by_turn_id != entry.turn_id:
                    continue
                prior_dec_id = decision_ids_by_turn.get(prior_entry.turn_id)
                if not prior_dec_id:
                    continue
                prior_from = _ts_to_utc(prior_entry.valid_from_ts)
                closed_to = max(prior_from, valid_from)
                await session.execute(
                    update(SessionDecisionLedger)
                    .where(SessionDecisionLedger.id == prior_dec_id)
                    .values(
                        epistemic_status=prior_entry.epistemic_status,
                        superseded_by_id=dec_id,
                        valid_to=closed_to,
                    )
                )

        # 7. Insert SessionGraphEdge rows for newly indexed turns
        new_edges: list[SessionGraphEdge] = []
        for turn in index.turns[start_turn_ordinal:]:
            new_edges.extend(
                _build_turn_graph_edges(
                    turn,
                    index=index,
                    scope=scope,
                    turn_unit_ids=turn_unit_ids,
                )
            )
        if new_edges:
            session.add_all(new_edges)
            await session.flush()

    if commit_if_clean and not caller_had_pending_state:
        await session.commit()

    # 8. Sync derived vectors to Qdrant `session_units_{project_id}` (graceful fail-open if offline)
    client, should_close = _open_qdrant_client_context(session, qdrant_client)
    if client is not None:
        try:
            if mode == "rebuild":
                await delete_thread_points_from_qdrant(
                    client,
                    project_id=scope.project_id,
                    thread_id=scope.thread_id,
                )
            if qdrant_upsert_batch:
                stats.qdrant_points_upserted = await upsert_session_units_to_qdrant(
                    client,
                    project_id=scope.project_id,
                    items=qdrant_upsert_batch,
                )
        except Exception as q_exc:  # noqa: BLE001
            log.warning(
                "Qdrant session unit sync skipped project=%s thread=%s err=%s",
                scope.project_id,
                scope.thread_id,
                sanitize_error_message(q_exc),
            )
        finally:
            if should_close:
                await client.aclose()

    return stats


def _reconstruct_prefix_into_index(
    index: IncrementalSessionIndex,
    *,
    persisted_turn_units: Sequence[SessionUnit],
    persisted_decisions: Sequence[SessionDecisionLedger],
    project_records: Sequence[dict[str, Any]],
    hydrated_vectors: dict[str, tuple[float, ...]],
    embedder: Any | None,
) -> None:
    """Hydrate prefix `0 .. K - 1` into `IncrementalSessionIndex` without re-running NER or HTTP embedding."""
    target_mode = f"embedder:{type(embedder).__name__}" if embedder is not None else "deterministic_ngram"
    index.embedding_mode = target_mode

    unit_id_to_turn_id = {u.id: u.message_from_id for u in persisted_turn_units}
    dec_id_to_turn_id = {
        d.id: unit_id_to_turn_id[d.session_unit_id]
        for d in persisted_decisions
        if d.session_unit_id in unit_id_to_turn_id
    }

    for i, u_row in enumerate(persisted_turn_units):
        rec = project_records[i]
        t_id = str(rec["id"])
        content = normalize_session_text(str(rec["content"]))
        role = str(rec.get("role") or u_row.role or "user")
        created_at = rec.get("created_at")
        if isinstance(created_at, datetime):
            ts = created_at.timestamp()
            iso = created_at.astimezone(UTC).isoformat() if created_at.tzinfo else created_at.isoformat()
        else:
            vf = u_row.valid_from if u_row.valid_from.tzinfo else u_row.valid_from.replace(tzinfo=UTC)
            ts = vf.timestamp()
            iso = vf.isoformat()

        t_vec = hydrated_vectors.get(u_row.id)
        if t_vec is None:
            t_vec = deterministic_ngram_vector(content)

        mentions = tuple(
            EntityMention(
                key=str(m.get("key") or ""),
                surface=str(m.get("surface") or ""),
                soft_aliases=tuple(str(a) for a in (m.get("soft_aliases") or ()) if a),
                confidence=float(m.get("confidence", 0.9)),
            )
            for m in (u_row.entity_mentions_json or [])
            if isinstance(m, dict) and m.get("key")
        )

        sentences_list: list[IndexedSentence] = []
        raw_sent_meta = list(u_row.sentences_json or [])
        for s_idx, s_meta in enumerate(raw_sent_meta):
            if not isinstance(s_meta, dict):
                continue
            s_start = max(0, int(s_meta.get("start", 0)))
            s_end = min(len(content), max(s_start, int(s_meta.get("end", len(content)))))
            s_text = content[s_start:s_end].strip() or content
            cached_s_vec = get_cached_text_embedding(s_text) if embedder is not None else None
            if cached_s_vec is not None and len(cached_s_vec) == len(t_vec):
                s_vec = l2_normalize(cached_s_vec)
            elif embedder is None:
                s_vec = deterministic_ngram_vector(s_text)
            else:
                s_vec = t_vec
            sentences_list.append(
                IndexedSentence(
                    sentence_id=str(s_meta.get("sentence_id") or f"{t_id}:s{s_idx}"),
                    turn_id=t_id,
                    ordinal=int(s_meta.get("ordinal", s_idx)),
                    text=s_text,
                    entity_keys=tuple(str(k) for k in (s_meta.get("entity_keys") or ()) if k),
                    dense_vector=s_vec,
                )
            )
        if not sentences_list:
            sentences_list.append(
                IndexedSentence(
                    sentence_id=f"{t_id}:s0",
                    turn_id=t_id,
                    ordinal=0,
                    text=content,
                    entity_keys=tuple(m.key for m in mentions),
                    dense_vector=t_vec,
                )
            )

        term_freqs = {str(k): int(v) for k, v in (u_row.term_freqs_json or {}).items()}
        word_len = max(1, int(u_row.doc_word_len or 1))
        for word, tf in term_freqs.items():
            index.doc_freqs[word] = index.doc_freqs.get(word, 0) + 1
            index.term_postings.setdefault(word, []).append((t_id, tf))
        index.total_token_words += word_len
        avg_len = index.total_token_words / max(1, i + 1)

        sparse_vec = sparse_dict_from_bm25(
            content,
            avg_doc_len=avg_len,
            corpus_doc_freqs=index.doc_freqs,
            total_corpus_docs=max(50, i + 1),
        )
        content_terms = frozenset(extract_content_terms(content))

        ep_id = u_row.episode_id
        ep_obj = index.episodes_by_id.get(ep_id)
        if ep_obj is None:
            ep_obj = EpisodeSegment(
                episode_id=ep_id,
                thread_id=index.thread_id,
                ordinal=int(u_row.episode_ordinal),
                turn_ids=[t_id],
                centroid=list(t_vec),
                content_terms=set(content_terms),
                start_ts=ts,
                end_ts=ts,
            )
            index.episodes.append(ep_obj)
            index.episodes_by_id[ep_id] = ep_obj
        else:
            n_prev = len(ep_obj.turn_ids)
            ep_obj.turn_ids.append(t_id)
            if len(ep_obj.centroid) == len(t_vec):
                blended = [
                    (ep_obj.centroid[j] * n_prev + t_vec[j]) / (n_prev + 1)
                    for j in range(len(t_vec))
                ]
                ep_obj.centroid = list(l2_normalize(blended))
            ep_obj.content_terms.update(content_terms)
            ep_obj.end_ts = max(ep_obj.end_ts, ts)

        win_node = index._append_sliding_window(ep_obj, t_id)
        cited_sources = tuple(sorted(str(s) for s in (rec.get("cited_source_ids") or ()) if s))
        if cited_sources:
            index.cached_cited_source_ids.update(cited_sources)

        turn_obj = IndexedTurn(
            turn_id=t_id,
            thread_id=index.thread_id,
            ordinal=i,
            role=role,
            content=content,
            created_at_ts=ts,
            created_at_iso=iso,
            token_count=max(1, int(u_row.token_count or 1)),
            content_hash=u_row.content_hash,
            episode_id=ep_id,
            window_id=win_node.window_id,
            dense_vector=t_vec,
            sparse_bm25=sparse_vec,
            content_terms=content_terms,
            entities=mentions,
            sentences=tuple(sentences_list),
            term_freqs=term_freqs,
            doc_word_len=word_len,
            epistemic_status=u_row.epistemic_status,  # type: ignore[arg-type]
            topic_slot_key=u_row.topic_slot_key,
            is_anaphoric_or_short=bool(u_row.is_anaphoric_or_short),
            cited_source_ids=cited_sources,
        )
        index.turns.append(turn_obj)
        index.turns_by_id[t_id] = turn_obj
        index._refresh_window_representation(win_node)
        index._register_turn_entities(turn_obj)

    # Restore decision ledger & turn supersession links
    for d_idx, d_row in enumerate(persisted_decisions):
        t_id = unit_id_to_turn_id.get(d_row.session_unit_id)
        turn_obj = index.turns_by_id.get(t_id or "")
        if not t_id or turn_obj is None:
            continue
        sup_turn_ids = tuple(
            dec_id_to_turn_id[sid]
            for sid in (d_row.supersedes_ids_json or [])
            if sid in dec_id_to_turn_id
        )
        if not sup_turn_ids and d_row.supersedes_id and d_row.supersedes_id in dec_id_to_turn_id:
            sup_turn_ids = (dec_id_to_turn_id[d_row.supersedes_id],)
        sup_by_turn_id = (
            dec_id_to_turn_id.get(d_row.superseded_by_id) if d_row.superseded_by_id else None
        )
        comp_turn_ids = tuple(
            unit_id_to_turn_id[cuid]
            for cuid in (d_row.companion_unit_ids_json or [])
            if cuid in unit_id_to_turn_id
        )
        vf_aware = (
            d_row.valid_from if d_row.valid_from.tzinfo else d_row.valid_from.replace(tzinfo=UTC)
        )
        vt_ts: float | None = None
        if not _is_open_ended(d_row.valid_to):
            vt_aware = (
                d_row.valid_to if d_row.valid_to.tzinfo else d_row.valid_to.replace(tzinfo=UTC)
            )
            vt_ts = vt_aware.timestamp()

        turn_obj.superseded_entity_keys = tuple(
            str(k) for k in (d_row.superseded_entity_keys_json or []) if k
        )
        if sup_turn_ids:
            turn_obj.supersedes_turn_ids = sup_turn_ids
        if sup_by_turn_id and d_row.epistemic_status == "SUPERSEDED":
            turn_obj.superseded_by_turn_id = sup_by_turn_id

        index.decision_ledger.append(
            DecisionLedgerEntry(
                entry_id=f"{index.thread_id}:dec{d_idx}",
                topic_slot_key=d_row.topic_slot_key,
                turn_id=t_id,
                turn_ordinal=int(d_row.turn_ordinal),
                epistemic_status=d_row.epistemic_status,  # type: ignore[arg-type]
                summary_text=d_row.summary_text,
                entity_keys=tuple(str(k) for k in (d_row.entity_keys_json or []) if k),
                companion_turn_ids=comp_turn_ids,
                valid_from_ts=vf_aware.timestamp(),
                valid_to_ts=vt_ts,
                superseded_by_turn_id=sup_by_turn_id,
                supersedes_turn_ids=sup_turn_ids,
            )
        )


async def hydrate_or_backfill_cold_session_index(
    session: AsyncSession,
    *,
    index: IncrementalSessionIndex,
    project_records: Sequence[dict[str, Any]],
    principal: VerifiedPrincipal,
    owner_key: str,
    embedder: Any | None = None,
    qdrant_client: httpx.AsyncClient | None = None,
    caller_had_pending_state: bool = False,
) -> PersistenceSyncStats:
    """Cold-start entrypoint: hydrate verified prefix (`0` HTTP re-embeds), lazy-backfill, or reconcile."""
    try:
        scope = await resolve_persistence_scope(
            session,
            thread_id=index.thread_id,
            principal=principal,
            owner_key=owner_key,
        )
    except ForbiddenError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_missing_session_tables_error(exc):
            await index.append_messages(project_records, embedder=embedder)
            return PersistenceSyncStats(status="memory_only")
        raise

    if scope is None:
        await index.append_messages(project_records, embedder=embedder)
        return PersistenceSyncStats(status="memory_only")

    try:
        persisted_turn_units = list(
            (
                await session.execute(
                    select(SessionUnit)
                    .where(
                        SessionUnit.tenant_id == scope.tenant_id,
                        SessionUnit.project_id == scope.project_id,
                        SessionUnit.thread_id == scope.thread_id,
                        SessionUnit.unit_kind == "TURN",
                    )
                    .order_by(SessionUnit.ordinal.asc())
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_missing_session_tables_error(exc):
            await index.append_messages(project_records, embedder=embedder)
            return PersistenceSyncStats(status="memory_only")
        raise

    k_persisted = len(persisted_turn_units)
    n_visible = len(project_records)
    embedding_sig = compute_embedding_signature(embedder)

    # Empty visible history: purge any lingering derived state if k_persisted > 0
    if n_visible == 0:
        if k_persisted > 0:
            await purge_thread_session_rows(session, thread_id=scope.thread_id)
            if not caller_had_pending_state:
                await session.commit()
            await purge_thread_qdrant_points_for_projects(
                session,
                thread_id=scope.thread_id,
                project_ids=[scope.project_id],
                qdrant_client=qdrant_client,
            )
            return PersistenceSyncStats(status="reconciled_empty")
        return PersistenceSyncStats(status="clean_empty")

    # Verify whether `persisted_turn_units` is an exact, unbroken prefix of `project_records`
    prefix_ok = 0 < k_persisted <= n_visible
    if prefix_ok:
        for idx_i, u_row in enumerate(persisted_turn_units):
            rec = project_records[idx_i]
            expected_hash = hashlib.sha256(
                normalize_session_text(str(rec["content"])).encode("utf-8")
            ).hexdigest()
            if (
                int(u_row.ordinal) != idx_i
                or u_row.message_from_id != str(rec["id"])
                or u_row.content_hash != expected_hash
                or u_row.embedding_model_signature != embedding_sig
            ):
                prefix_ok = False
                break

    # Lazy Backfill (`k == 0`) or Reconciled Rebuild (delete / ACL revocation / model drift)
    if not prefix_ok:
        await index.append_messages(project_records, embedder=embedder)
        status_label = "lazy_backfill" if k_persisted == 0 else "reconciled_rebuild"
        sync_stats = await sync_session_index_to_storage(
            session,
            index=index,
            scope=scope,
            embedding_sig=embedding_sig,
            mode="rebuild",
            qdrant_client=qdrant_client,
            caller_had_pending_state=caller_had_pending_state,
        )
        sync_stats.status = status_label
        return sync_stats

    # Prefix `0 .. K - 1` is valid -> Hydrate from PostgreSQL + Qdrant with 0 HTTP re-embeddings!
    hydrated_vectors: dict[str, tuple[float, ...]] = {}
    if embedder is None:
        for idx_i, u_row in enumerate(persisted_turn_units):
            content_norm = normalize_session_text(str(project_records[idx_i]["content"]))
            hydrated_vectors[u_row.id] = deterministic_ngram_vector(content_norm)
    else:
        missing_units: list[tuple[SessionUnit, str]] = []
        for idx_i, u_row in enumerate(persisted_turn_units):
            content_norm = normalize_session_text(str(project_records[idx_i]["content"]))
            cached_vec = get_cached_text_embedding(content_norm)
            if cached_vec is not None:
                hydrated_vectors[u_row.id] = l2_normalize(cached_vec)
            else:
                missing_units.append((u_row, content_norm))

        if missing_units:
            client, should_close = _open_qdrant_client_context(session, qdrant_client)
            if client is not None:
                try:
                    q_vecs = await fetch_thread_turn_vectors_from_qdrant(
                        client,
                        tenant_id=scope.tenant_id,
                        project_id=scope.project_id,
                        thread_id=scope.thread_id,
                    )
                    for u_row, content_norm in missing_units:
                        q_vec = q_vecs.get(u_row.id)
                        if q_vec is not None:
                            hydrated_vectors[u_row.id] = q_vec
                            put_cached_text_embedding(content_norm, q_vec)
                except Exception as q_exc:  # noqa: BLE001
                    log.warning(
                        "Cold-start Qdrant vector hydration fallback thread=%s err=%s",
                        scope.thread_id,
                        sanitize_error_message(q_exc),
                    )
                finally:
                    if should_close:
                        await client.aclose()

            still_missing = [
                (u_row, content_norm)
                for u_row, content_norm in missing_units
                if u_row.id not in hydrated_vectors
            ]
            if still_missing:
                fallback_vecs = await embed_texts_with_main_cache(
                    [txt for _, txt in still_missing],
                    embedder=embedder,
                )
                for (u_row, _), f_vec in zip(still_missing, fallback_vecs, strict=True):
                    hydrated_vectors[u_row.id] = f_vec

    persisted_decisions = list(
        (
            await session.execute(
                select(SessionDecisionLedger)
                .where(
                    SessionDecisionLedger.tenant_id == scope.tenant_id,
                    SessionDecisionLedger.project_id == scope.project_id,
                    SessionDecisionLedger.thread_id == scope.thread_id,
                )
                .order_by(SessionDecisionLedger.turn_ordinal.asc(), SessionDecisionLedger.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    _reconstruct_prefix_into_index(
        index,
        persisted_turn_units=persisted_turn_units,
        persisted_decisions=persisted_decisions,
        project_records=project_records[:k_persisted],
        hydrated_vectors=hydrated_vectors,
        embedder=embedder,
    )

    if k_persisted == n_visible:
        return PersistenceSyncStats(
            status="hydrated_clean",
            hydrated_turns=k_persisted,
        )

    # Append ONLY the unseen tail `K .. N - 1` (`O(Delta)`!)
    prev_win_count = len(index.windows)
    prev_ep_count = len(index.episodes)
    prev_ep_last_len = len(index.episodes[-1].turn_ids) if index.episodes else 0
    prev_dec_count = len(index.decision_ledger)
    prev_turn_state = {
        t.turn_id: (t.epistemic_status, t.superseded_by_turn_id) for t in index.turns
    }

    await index.append_messages(project_records[k_persisted:], embedder=embedder)

    mutated_prior_turns = {
        tid
        for tid, old_st in prev_turn_state.items()
        if (index.turns_by_id[tid].epistemic_status, index.turns_by_id[tid].superseded_by_turn_id)
        != old_st
    }
    grown_ep_ord = (
        prev_ep_count - 1
        if prev_ep_count > 0 and len(index.episodes[prev_ep_count - 1].turn_ids) > prev_ep_last_len
        else None
    )

    delta_stats = await sync_session_index_to_storage(
        session,
        index=index,
        scope=scope,
        embedding_sig=embedding_sig,
        mode="delta",
        start_turn_ordinal=k_persisted,
        start_window_ordinal=prev_win_count,
        start_episode_ordinal=prev_ep_count,
        start_decision_idx=prev_dec_count,
        mutated_prior_turn_ids=mutated_prior_turns,
        grown_episode_ordinal=grown_ep_ord,
        qdrant_client=qdrant_client,
        caller_had_pending_state=caller_had_pending_state,
    )
    delta_stats.status = "hydrated_delta"
    delta_stats.hydrated_turns = k_persisted
    return delta_stats


async def backfill_thread_session_context(
    session: AsyncSession,
    *,
    thread_id: str,
    principal: VerifiedPrincipal,
    owner_key: str,
    embedder: Any | None = None,
    qdrant_client: httpx.AsyncClient | None = None,
) -> PersistenceSyncStats:
    """Explicitly materialize or reconcile multi-view session context persistence for a thread."""
    from sag_api.services.agent_domain import filter_messages_for_scope
    from sag_api.services.session_context.runtime import (
        _extract_visible_records,
        load_full_thread_messages_keyset,
        resolve_session_embedder,
    )

    caller_pending = bool(session.new or session.dirty or session.deleted)
    active_embedder = resolve_session_embedder(embedder)
    raw_messages = await load_full_thread_messages_keyset(session, thread_id=thread_id)
    scoped_messages = await filter_messages_for_scope(
        session,
        raw_messages,
        principal=principal,
        requested_source_ids=None,
    )
    visible_records, _ = _extract_visible_records(scoped_messages)
    temp_index = IncrementalSessionIndex(thread_id)
    return await hydrate_or_backfill_cold_session_index(
        session,
        index=temp_index,
        project_records=visible_records,
        principal=principal,
        owner_key=owner_key,
        embedder=active_embedder,
        qdrant_client=qdrant_client,
        caller_had_pending_state=caller_pending,
    )
