"""Authorization/snapshot seams for future recall; no production caller is wired."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import UTC

from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.services.agent_domain import get_thread
from sag_api.services.session_context.types import (
    ContextSelection,
    ResolvedEvidence,
    SessionKey,
    SessionScope,
    SessionSnapshot,
    TraceMessage,
    digest_json,
    require,
)
from sag_api.services.source_service import get_authorized_source_ids


def _check_time(scope: SessionScope, now: float | None) -> None:
    instant = time.time() if now is None else now
    require(type(instant) in (int, float) and math.isfinite(instant), "clock")
    require(instant < scope.expires_at, "principal_expired")


async def resolve_scope(
    session: AsyncSession, *, key: SessionKey, principal: VerifiedPrincipal,
    candidate_source_ids: frozenset[str], requested_source_ids: frozenset[str] | None = None,
    now: float | None = None,
) -> SessionScope:
    """Inputs come from verified server dependencies, never from a client DTO.

    This checks a thread and candidate Source dependencies; it does not infer a
    legacy message's original project stamp or validate a principal signature.
    """
    instant = time.time() if now is None else now
    require(type(instant) in (int, float) and math.isfinite(instant), "clock")
    require(principal.issued_at <= instant < principal.expires_at, "principal_expired")
    require(isinstance(candidate_source_ids, frozenset)
            and all(isinstance(item, str) and item.strip() for item in candidate_source_ids), "source_ids")
    require(requested_source_ids is None or (
        isinstance(requested_source_ids, frozenset)
        and all(isinstance(item, str) and item.strip() for item in requested_source_ids)), "source_filter")
    await get_thread(session, key.agent_id, key.thread_id, owner_key=key.owner_key)
    authorized = await get_authorized_source_ids(
        session, principal=principal, requested_source_ids=sorted(candidate_source_ids),
    )
    effective = frozenset(authorized) & candidate_source_ids
    if requested_source_ids is not None:
        effective &= requested_source_ids
    return SessionScope(key, principal.issuer, principal.subject, principal.organization_id,
                        principal.tenant_id, principal.allowed_project_ids, principal.allowed_partition_ids,
                        effective, requested_source_ids, principal.expires_at)


def make_snapshot(
    scope: SessionScope, records: Mapping[str, TraceMessage], *, generation: str,
) -> SessionSnapshot:
    """Digest immutable source rows; generation must come from the trusted store.

    A digest detects provided-row changes, not an external DB mutation after a
    read. T5/T6 must supply a transaction/fence and a complete authorized read.
    """
    require(all(isinstance(item, TraceMessage) and key == item.message_id and item.key == scope.key
                for key, item in records.items()), "snapshot_scope")
    ordered = sorted(records.values(), key=lambda item: (item.created_at, item.message_id))
    payload = [{
        "id": item.message_id, "hash": item.reference.content_hash, "role": item.role, "status": item.status,
        "at": item.created_at.astimezone(UTC).isoformat(), "provenance": item.provenance,
        "partition": item.partition_fingerprint, "sources": sorted(item.source_ids),
        "dependencies": sorted((ref.message_id, ref.content_hash) for ref in item.dependencies),
    } for item in ordered]
    return SessionSnapshot(scope.key, scope.fingerprint, generation, digest_json(payload))


def eligible_message_ids(
    scope: SessionScope, records: Mapping[str, TraceMessage], *, now: float | None = None,
) -> frozenset[str]:
    """Pre-filter metadata before embedding, ranking or evidence graph traversal.

    Leaf-first authorization excludes cycles and every dependent of an invalid
    row in O(V+E), without imposing a history-age or last-N cutoff.
    """
    _check_time(scope, now)
    partition_fingerprint = scope.partition_fingerprint
    local = {
        key for key, item in records.items()
        if key == item.message_id and item.key == scope.key
        and item.partition_fingerprint == partition_fingerprint
        and item.provenance == "recorded" and item.status == "ok" and item.source_ids <= scope.source_ids
    }
    hashes = {key: records[key].reference.content_hash for key in local}
    waiting: dict[str, int] = {}
    parents: dict[str, list[str]] = defaultdict(list)
    for key in local:
        dependencies = records[key].dependencies
        if any(ref.message_id not in local or hashes[ref.message_id] != ref.content_hash for ref in dependencies):
            continue
        waiting[key] = len(dependencies)
        for ref in dependencies:
            parents[ref.message_id].append(key)
    queue = deque(key for key, count in waiting.items() if count == 0)
    eligible = set()
    while queue:
        key = queue.popleft()
        eligible.add(key)
        for parent in parents[key]:
            waiting[parent] -= 1
            if waiting[parent] == 0:
                queue.append(parent)
    return frozenset(eligible)


def validate_selection(
    selection: ContextSelection, *, scope: SessionScope, current_snapshot: SessionSnapshot,
    records: Mapping[str, TraceMessage], now: float | None = None,
) -> tuple[ResolvedEvidence, ...]:
    """Revalidate with a newly authorized scope/read before model input or output.

    Returned evidence grounds historical statements only, not document facts or
    approval. Current rows/scope must be supplied by trusted server adapters.
    """
    _check_time(scope, now)
    current = make_snapshot(scope, records, generation=current_snapshot.generation)
    require(current_snapshot == current and selection.snapshot == current, "snapshot_changed")
    eligible = eligible_message_ids(scope, records, now=now)
    require(selection.coverage.eligible_ids == eligible, "coverage_denominator")
    if selection.state == "ready" and selection.mode == "full_session":
        require(not any(item.provenance == "unknown" or item.partition_fingerprint is None
                        for item in records.values()), "unknown_provenance")
        require(selection.coverage.traversal_complete, "incomplete_coverage")
    result = []
    for span in selection.spans:
        require(span.ref.message_id in eligible, "evidence_unavailable")
        root = records[span.ref.message_id]
        require(span.ref == root.reference and span.end <= len(root.content), "span_changed")
        require(span.ref.message_id in selection.coverage.visited_ids, "evidence_not_visited")
        visited = set()
        sources = set()
        references = set()
        stack = [root.message_id]
        while stack:
            key = stack.pop()
            if key in visited:
                continue
            visited.add(key)
            item = records[key]
            sources.update(item.source_ids)
            references.add(item.reference)
            stack.extend(ref.message_id for ref in item.dependencies)
        kind = "user_statement" if root.role == "user" else "assistant_statement"
        result.append(ResolvedEvidence(span, kind, root.content[span.start:span.end],
                                       frozenset(references), frozenset(sources)))
    return tuple(result)
