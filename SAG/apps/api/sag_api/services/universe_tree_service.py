from __future__ import annotations

import asyncio
from bisect import bisect_left, bisect_right
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.logging import get_logger
from sag_api.db.models import Document, Job
from sag_api.db.models.routing_rag import CanonicalBlock, DocumentVersion, SearchUnit
from sag_api.enums import JobStatus, JobType
from sag_api.services.knowledge_candidate_service import (
    CorpusQuery,
    build_project_routing_snapshot,
    current_units,
    routing_unit,
)
from sag_api.services.routing_tree_service import TreeBuildConfig
from sag_api.services.tree_publish_service import (
    SearchUnitAssignment,
    TreePublishResult,
    publish_tree_candidate,
)

log = get_logger("universe_tree")
_LOCAL_SCHEDULE_LOCK = asyncio.Lock()


@asynccontextmanager
async def _schedule_lock(session: AsyncSession, key: int):
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        yield
    else:
        async with _LOCAL_SCHEDULE_LOCK:
            yield


async def enqueue_project_tree_rebuild(
    session: AsyncSession,
    job_queue,
    *,
    tenant_id: str,
    project_ids: list[str],
    reason: str,
    persist_only: bool = False,
) -> Job:
    if job_queue is None:
        raise ServiceUnavailableError("Background job queue is unavailable")
    projects = sorted(set(project_ids))
    if not tenant_id.strip() or not projects or any(not item.strip() for item in projects):
        raise ValueError("Universe tree rebuild requires a tenant and at least one project")

    scope = f"{tenant_id}:{','.join(projects)}"
    lock_key = int.from_bytes(sha256(scope.encode()).digest()[:8], "big", signed=True)
    try:
        async with _schedule_lock(session, lock_key):
            queued = (
                await session.scalars(
                    select(Job)
                    .where(
                        Job.type == JobType.REBUILD_UNIVERSE_TREE,
                        Job.status == JobStatus.QUEUED,
                    )
                    .order_by(Job.created_at)
                    .with_for_update()
                )
            ).all()
            job = next(
                (
                    item
                    for item in queued
                    if (item.payload or {}).get("tenant_id") == tenant_id
                    and sorted((item.payload or {}).get("project_ids") or []) == projects
                ),
                None,
            )
            created = False
            if job is None:
                created = True
                job = Job(
                    type=JobType.REBUILD_UNIVERSE_TREE,
                    source_id=None,
                    status=JobStatus.QUEUED,
                    payload={"tenant_id": tenant_id, "project_ids": projects, "reason": reason},
                )
                session.add(job)
                await session.flush()
            if not persist_only:
                await session.commit()
    except Exception:
        await session.rollback()
        raise

    if not persist_only and job.status == JobStatus.QUEUED:
        already_scheduled = (
            not created
            and (
                job.id in getattr(job_queue, "_queued_job_ids", ())
                or job.id in getattr(job_queue, "_retry_tasks_by_job", ())
            )
        )
        if not already_scheduled:
            from sag_api.services.document_service import _enqueue_persisted_job

            await _enqueue_persisted_job(job_queue, job.id)
    return job


async def enqueue_tree_rebuild_for_version(
    session: AsyncSession,
    job_queue,
    document_version_id: str,
    *,
    reason: str,
) -> Job | None:
    now = datetime.now(UTC)
    scope = await session.execute(
        select(Document.tenant_id, Document.project_id)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            DocumentVersion.id == document_version_id,
            Document.is_active.is_(True),
            DocumentVersion.knowledge_status == "DATA_READY",
            DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
            DocumentVersion.search_ready_at.is_not(None),
            DocumentVersion.valid_from <= now,
            DocumentVersion.valid_to > now,
        )
    )
    row = scope.first()
    if row is None:
        return None
    return await enqueue_project_tree_rebuild(
        session,
        job_queue,
        tenant_id=row.tenant_id,
        project_ids=[row.project_id],
        reason=reason,
    )


async def _search_unit_assignments(
    session: AsyncSession,
    *,
    tenant_id: str,
    project_id: str,
    units,
) -> list[SearchUnitAssignment]:
    version_ids = sorted({unit.document_version_id for unit in units})
    block_rows = (
        await session.execute(
            select(CanonicalBlock.document_version_id, CanonicalBlock.id, CanonicalBlock.ordinal).where(
                CanonicalBlock.document_version_id.in_(version_ids)
            )
        )
    ).all()
    ordinal_by_block = {(version_id, block_id): ordinal for version_id, block_id, ordinal in block_rows}
    units_by_version: dict[str, list[tuple[int, str, str]]] = {}
    for unit in units:
        evidence_ids = unit.provenance_json.get("evidence_block_ids", [])
        if len(evidence_ids) != 1:
            raise ValueError("KnowledgeUnit must point to exactly one canonical block")
        ordinal = ordinal_by_block.get((unit.document_version_id, evidence_ids[0]))
        if ordinal is None:
            raise ValueError("KnowledgeUnit canonical block is missing")
        units_by_version.setdefault(unit.document_version_id, []).append(
            (ordinal, unit.id, unit.security_partition_id)
        )
    for version_units in units_by_version.values():
        version_units.sort()
    ordinals_by_version = {
        version_id: [item[0] for item in version_units]
        for version_id, version_units in units_by_version.items()
    }

    block_from = aliased(CanonicalBlock)
    block_to = aliased(CanonicalBlock)
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(
                SearchUnit,
                Document.tenant_id,
                Document.project_id,
                Document.source_id,
                block_from.ordinal,
                block_to.ordinal,
            )
            .join(DocumentVersion, DocumentVersion.id == SearchUnit.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(block_from, block_from.id == SearchUnit.block_from_id)
            .join(block_to, block_to.id == SearchUnit.block_to_id)
            .where(
                SearchUnit.document_version_id.in_(version_ids),
                Document.is_active.is_(True),
                DocumentVersion.knowledge_status == "DATA_READY",
                DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
                DocumentVersion.search_ready_at.is_not(None),
                DocumentVersion.valid_from <= now,
                DocumentVersion.valid_to > now,
            )
            .order_by(SearchUnit.document_version_id, SearchUnit.id)
        )
    ).all()

    assignments: list[SearchUnitAssignment] = []
    mapped_knowledge_ids: set[str] = set()
    for unit, actual_tenant, actual_project, source_id, first_ordinal, last_ordinal in rows:
        if actual_tenant != tenant_id or actual_project != project_id or first_ordinal > last_ordinal:
            raise ValueError("SearchUnit scope differs from the project tree candidate")
        version_units = units_by_version.get(unit.document_version_id, [])
        ordinals = ordinals_by_version.get(unit.document_version_id, [])
        first = bisect_left(ordinals, first_ordinal)
        last = bisect_right(ordinals, last_ordinal)
        knowledge_unit_ids = tuple(sorted({item[1] for item in version_units[first:last]}))
        if not knowledge_unit_ids:
            continue
        if any(
            partition_id != unit.security_partition_id
            for ordinal, _unit_id, partition_id in version_units[first:last]
        ):
            raise ValueError("SearchUnit spans multiple security partitions")
        assignments.append(
            SearchUnitAssignment(
                knowledge_unit_ids=knowledge_unit_ids,
                search_unit_id=unit.id,
                source_id=source_id,
                document_version_id=unit.document_version_id,
                partition_id=unit.security_partition_id,
            )
        )
        mapped_knowledge_ids.update(knowledge_unit_ids)

    if mapped_knowledge_ids != {unit.id for unit in units}:
        raise ValueError("Current KnowledgeUnits do not map completely to ready SearchUnits")
    return assignments


def _build_production_tree_config(unit_count: int) -> TreeBuildConfig:
    """Size-tiered non-vacuous routing tree quality gates (SAG-RET-002)."""
    if unit_count < 16:
        return TreeBuildConfig(
            min_cluster_size=1,
            min_partition_units=1,
            max_giant_ratio=1.0,
            min_cohesion=0.0,
            max_edge_cut=1.0,
            min_routing_recall_at_k=0.0,
        )
    return TreeBuildConfig(
        min_cluster_size=2,
        target_cluster_size=8,
        max_cluster_size=32,
        min_partition_units=2,
        max_giant_ratio=0.55,
        min_cohesion=0.10,
        max_edge_cut=0.85,
        min_routing_recall_at_k=0.50,
    )


def _build_synthetic_probe_queries(units: list, *, max_probes: int = 16) -> list[CorpusQuery]:
    """Generate deterministic medoid/sparse-term probe queries so routing recall gates are non-vacuous."""
    if len(units) < 16:
        return []
    sources_by_partition: dict[str, tuple[str, ...]] = {}
    for unit in units:
        part = str(unit.security_partition_id)
        existing = set(sources_by_partition.get(part, ()))
        existing.add(str(unit.source_id))
        sources_by_partition[part] = tuple(sorted(existing))
    step = max(1, len(units) // max_probes)
    probes: list[CorpusQuery] = []
    for idx in range(0, len(units), step):
        if len(probes) >= max_probes:
            break
        unit = units[idx]
        features = getattr(unit, "features_json", None) or {}
        sparse = features.get("sparse") or []
        top_terms = [
            str(term).strip()
            for term, _weight in sorted(sparse, key=lambda pair: -float(pair[1]))[:6]
            if str(term).strip()
        ]
        if not top_terms:
            top_terms = [str(e).strip() for e in (features.get("entities") or [])[:4] if str(e).strip()]
        query_text = " ".join(top_terms).strip()
        if not query_text:
            continue
        auth_sources = sources_by_partition.get(str(unit.security_partition_id), (str(unit.source_id),))
        probes.append(
            CorpusQuery(
                query_id=f"probe-{unit.id[:16]}",
                query=query_text,
                target_unit_id=str(unit.id),
                partition_id=str(unit.security_partition_id),
                authorized_source_ids=auth_sources,
                k=10,
            )
        )
    return probes


async def _try_incremental_tree_update(
    session: AsyncSession,
    engine_manager,
    *,
    tenant_id: str,
    project_id: str,
    units: list,
    config: TreeBuildConfig,
) -> TreePublishResult | None:
    """Attempt Checkpoint C incremental delta update when an ACTIVE tree exists and delta is small (SAG-RET-002)."""
    from sag_api.db.models import ProjectSearchState, TreeManifest
    from sag_api.services.incremental_tree_service import (
        reconstruct_snapshot_from_manifest,
        update_tree_incrementally,
    )

    state = await session.scalar(
        select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
    )
    if state is None or not state.active_tree_version:
        return None
    manifest_rec = await session.get(TreeManifest, state.active_tree_version)
    if manifest_rec is None or manifest_rec.status != "ACTIVE" or not isinstance(manifest_rec.manifest_json, dict):
        return None
    existing_ids = set(manifest_rec.manifest_json.get("unit_ids") or ())
    if not existing_ids:
        return None
    current_by_id = {str(u.id): u for u in units}
    # Require pure additive delta (no deleted units) and bounded delta ratio.
    if not existing_ids.issubset(current_by_id.keys()):
        return None
    delta_ids = sorted(current_by_id.keys() - existing_ids)
    max_delta = max(24, int(len(existing_ids) * 0.15))
    if not delta_ids or len(delta_ids) > max_delta:
        return None
    try:
        base_snapshot = reconstruct_snapshot_from_manifest(
            manifest_rec.manifest_json,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        new_unit_inputs = [routing_unit(current_by_id[uid]) for uid in delta_ids]
        update = update_tree_incrementally(
            base_snapshot,
            new_unit_inputs,
            new_edges=[],
            config=config,
        )
        if not update.snapshot.publishable:
            log.info(
                "Incremental tree update not publishable for project=%s; falling back to full rebuild",
                project_id,
            )
            return None
        assignments = await _search_unit_assignments(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            units=units,
        )
        await session.commit()
        qdrant_client = await engine_manager.get_search_unit_qdrant_client()
        return await publish_tree_candidate(
            update.snapshot,
            search_unit_assignments=assignments,
            qdrant_client=qdrant_client,
        )
    except Exception as exc:  # noqa: BLE001 - safe fallback to full rebuild
        log.info(
            "Incremental tree update skipped for project=%s (%s); running full rebuild",
            project_id,
            type(exc).__name__,
        )
        return None


async def rebuild_project_universe_tree(
    session: AsyncSession,
    engine_manager,
    *,
    tenant_id: str,
    project_id: str,
) -> TreePublishResult | None:
    units_preview = await current_units(session, tenant_id, project_id, allow_empty=True)
    config = _build_production_tree_config(len(units_preview))

    if units_preview:
        incremental_result = await _try_incremental_tree_update(
            session,
            engine_manager,
            tenant_id=tenant_id,
            project_id=project_id,
            units=units_preview,
            config=config,
        )
        if incremental_result is not None:
            return incremental_result

    probe_queries = _build_synthetic_probe_queries(units_preview)
    built = await build_project_routing_snapshot(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        queries=probe_queries,
        config=config,
        allow_empty=True,
    )
    if built is None:
        from sag_api.db.models import ProjectSearchState, TreeManifest
        from sag_api.services.knowledge_service import knowledge_lock

        await knowledge_lock(session, "project-input:" + tenant_id + ":" + project_id)
        if await current_units(session, tenant_id, project_id, allow_empty=True):
            raise ServiceUnavailableError("Project changed while clearing its empty snapshot")
        state = await session.scalar(select(ProjectSearchState).where(
            ProjectSearchState.project_id == project_id).with_for_update())
        if state is not None and state.active_tree_version:
            previous = await session.get(TreeManifest, state.active_tree_version)
            if previous:
                previous.status = "INACTIVE"
            state.previous_tree_version = state.active_tree_version
            state.active_tree_version = None
            state.active_search_epoch += 1
            state.last_switched_at = datetime.now(UTC)
        await session.commit()
        return None
    snapshot, units, _graph, _benchmark = built
    if not snapshot.publishable:
        failed_gates = [name for name, ok in snapshot.quality_gates.items() if not ok]
        log.warning(
            "Routing tree candidate rejected by quality gates for project=%s (failed=%s); preserving prior ACTIVE slot",
            project_id,
            ",".join(failed_gates),
        )
        await session.commit()
        return None
    assignments = await _search_unit_assignments(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        units=units,
    )
    await session.commit()
    qdrant_client = await engine_manager.get_search_unit_qdrant_client()
    return await publish_tree_candidate(
        snapshot,
        search_unit_assignments=assignments,
        qdrant_client=qdrant_client,
    )
