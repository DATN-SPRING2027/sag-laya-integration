"""Global retrieval over ACL-scoped, manifest-verified Phase 2C SearchUnits."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal
from sag_api.core.errors import ServiceUnavailableError
from sag_api.core.logging import get_logger
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    SearchUnit,
    SourceProjectMapping,
    StageRun,
)
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.sag.search_unit_store import (
    SearchIndexUnavailable,
    SearchUnitHit,
    SearchUnitQdrantStore,
    build_search_filter,
    build_sparse_query_vector,
)
from sag_api.services.query_routing_service import RouteDecision, capture_routing_decisions
from sag_api.services.query_strategy_planner import QueryStrategyPlan, plan_query, promote_multi_hop

log = get_logger("search_units")


class SearchableSource(Protocol):
    id: str
    name: str
    sag_source_config_id: str


@dataclass(frozen=True, slots=True)
class _ReadyVersion:
    document_version_id: str
    source_id: str
    project_id: str
    tenant_id: str
    partition_id: str
    source: SearchableSource


@dataclass(frozen=True, slots=True)
class _SearchGroup:
    project_id: str
    tenant_id: str
    partition_id: str
    source: SearchableSource
    versions: tuple[str, ...]

    @property
    def scope_key(self) -> tuple[str, str, str, str]:
        return (self.project_id, self.source.id, self.tenant_id, self.partition_id)


def _verified_manifest(metrics: Any, *, project_id: str) -> bool:
    if not isinstance(metrics, dict) or metrics.get("manifest_verified") is not True:
        return False
    expected_collection = f"search_units_{project_id}"
    if metrics.get("collection_name") != expected_collection:
        return False
    count_names = (
        "search_unit_count",
        "pg_count",
        "qdrant_count",
        "qdrant_indexed_count",
    )
    counts = [metrics.get(name) for name in count_names]
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    unit_count, pg_count, qdrant_count, indexed_count = counts
    checksum = metrics.get("manifest_checksum")
    qdrant_checksum = metrics.get("qdrant_checksum")
    checksums_are_sha256 = all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in (checksum, qdrant_checksum)
    )
    return (
        unit_count > 0
        and unit_count == pg_count == qdrant_count == indexed_count
        and checksums_are_sha256
        and checksum == qdrant_checksum
    )


def _stage_belongs_to_current_attempt(
    attempt_started_at: datetime | None,
    stage: StageRun,
) -> bool:
    """A retry invalidates older SUCCESS manifests as soon as its run starts."""
    return attempt_started_at is not None and stage.created_at >= attempt_started_at


async def _load_current_ready_versions(
    session: AsyncSession,
    sources: list[SearchableSource],
    principal: VerifiedPrincipal,
) -> dict[str, _ReadyVersion]:
    try:
        return await _load_current_ready_versions_checked(session, sources, principal)
    except SQLAlchemyError as error:
        log.warning("canonical scope lookup unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical search authorization is unavailable") from error


async def _load_current_ready_versions_checked(
    session: AsyncSession,
    sources: list[SearchableSource],
    principal: VerifiedPrincipal,
) -> dict[str, _ReadyVersion]:
    """Recheck mappings and collect only versions from each version's latest verified attempt."""
    if not sources or not principal.tenant_id or not principal.allowed_partition_ids:
        return {}

    source_by_id = {source.id: source for source in sources}
    source_ids = sorted(source_by_id)
    partition_ids = sorted(principal.allowed_partition_ids)
    partition_expression = DocumentVersion.metadata_json["security_partition_id"].as_string()
    current_time = datetime.now(UTC)
    hidden_document_ids = await _hidden_reprocess_document_ids(session, source_ids)

    scoped_rows = (
        await session.execute(
            select(
                SearchUnit.document_version_id,
                Document.source_id,
                Document.project_id,
                Document.tenant_id,
                SearchUnit.security_partition_id,
            )
            .join(DocumentVersion, DocumentVersion.id == SearchUnit.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(SourceProjectMapping, SourceProjectMapping.source_id == Document.source_id)
            .where(
                Document.source_id.in_(source_ids),
                Document.project_id.in_(principal.allowed_project_ids),
                Document.project_id == SourceProjectMapping.project_id,
                Document.tenant_id == principal.tenant_id,
                SourceProjectMapping.organization_id == principal.organization_id,
                SourceProjectMapping.state == "CONFIRMED",
                Document.is_active.is_(True),
                Document.status.notin_([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
                Document.id.notin_(hidden_document_ids) if hidden_document_ids else True,
                DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
                DocumentVersion.valid_from <= current_time,
                DocumentVersion.search_ready_at.is_not(None),
                DocumentVersion.valid_to > current_time,
                SearchUnit.security_partition_id.in_(partition_ids),
                SearchUnit.security_partition_id == partition_expression,
            )
            .distinct()
        )
    ).all()
    if not scoped_rows:
        return {}

    version_ids = sorted({str(row.document_version_id) for row in scoped_rows})
    ranked_runs = (
        select(
            IngestionRun.id.label("run_id"),
            IngestionRun.document_version_id.label("document_version_id"),
            IngestionRun.project_id.label("project_id"),
            IngestionRun.tenant_id.label("tenant_id"),
            IngestionRun.started_at.label("started_at"),
            func.row_number()
            .over(
                partition_by=IngestionRun.document_version_id,
                order_by=(IngestionRun.created_at.desc(), IngestionRun.id.desc()),
            )
            .label("attempt_rank"),
        )
        .where(IngestionRun.document_version_id.in_(version_ids))
        .subquery()
    )
    latest_runs = (
        await session.execute(
            select(
                ranked_runs.c.run_id,
                ranked_runs.c.document_version_id,
                ranked_runs.c.project_id,
                ranked_runs.c.tenant_id,
                ranked_runs.c.started_at,
            ).where(ranked_runs.c.attempt_rank == 1)
        )
    ).all()
    run_by_version = {str(row.document_version_id): row for row in latest_runs}
    run_ids = [row.run_id for row in latest_runs]
    if not run_ids:
        return {}

    stage_rows = (
        await session.execute(
            select(StageRun)
            .where(StageRun.run_id.in_(run_ids), StageRun.stage == "INDEX_SEARCH")
            .order_by(StageRun.created_at.desc(), StageRun.id.desc())
        )
    ).scalars().all()
    stage_by_run: dict[str, StageRun] = {}
    for stage in stage_rows:
        stage_by_run.setdefault(stage.run_id, stage)

    versions: dict[str, _ReadyVersion] = {}
    for row in scoped_rows:
        version_id = str(row.document_version_id)
        run = run_by_version.get(version_id)
        source = source_by_id.get(str(row.source_id))
        if run is None or source is None:
            continue
        if str(run.project_id) != str(row.project_id) or str(run.tenant_id) != str(row.tenant_id):
            continue
        stage = stage_by_run.get(run.run_id)
        if (
            stage is None
            or stage.status != "SUCCESS"
            or not _stage_belongs_to_current_attempt(run.started_at, stage)
            or not _verified_manifest(stage.metrics_json, project_id=str(row.project_id))
        ):
            continue
        versions[version_id] = _ReadyVersion(
            document_version_id=version_id,
            source_id=str(row.source_id),
            project_id=str(row.project_id),
            tenant_id=str(row.tenant_id),
            partition_id=str(row.security_partition_id),
            source=source,
        )
    return versions


async def _hidden_reprocess_document_ids(session: AsyncSession, source_ids: list[str]) -> set[str]:
    """Honor the delete/reprocess visibility barrier used by legacy retrieval."""
    if not source_ids:
        return set()
    control_types = [JobType.DELETE_DOCUMENT, JobType.REPROCESS_DOCUMENT]
    hidden_ids = set(
        (
            await session.scalars(
                select(Job.document_id).where(
                    Job.source_id.in_(source_ids),
                    Job.type.in_(control_types),
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    Job.document_id.is_not(None),
                )
            )
        ).all()
    )
    failed_ids = set(
        (
            await session.scalars(
                select(Job.document_id)
                .join(Document, Document.id == Job.document_id)
                .where(
                    Job.source_id.in_(source_ids),
                    Job.type.in_(control_types),
                    Job.status == JobStatus.FAILED,
                    Document.status.in_([DocumentStatus.FAILED, DocumentStatus.DELETE_FAILED]),
                    Job.document_id.is_not(None),
                )
            )
        ).all()
    )
    return hidden_ids | failed_ids


def _groups(versions: dict[str, _ReadyVersion]) -> list[_SearchGroup]:
    grouped: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    source_by_scope: dict[tuple[str, str, str, str], SearchableSource] = {}
    for version in versions.values():
        key = (version.project_id, version.source_id, version.tenant_id, version.partition_id)
        grouped[key].append(version.document_version_id)
        source_by_scope[key] = version.source
    return [
        _SearchGroup(
            project_id=key[0],
            tenant_id=key[2],
            partition_id=key[3],
            source=source_by_scope[key],
            versions=tuple(sorted(set(version_ids))),
        )
        for key, version_ids in sorted(grouped.items())
    ]


async def _query_group(
    group: _SearchGroup,
    *,
    query: str,
    limit: int,
    store: SearchUnitQdrantStore,
    query_vector: list[float],
    route: RouteDecision | None = None,
) -> tuple[_SearchGroup, list[SearchUnitHit], list[SearchUnitHit]]:
    collection = f"search_units_{group.project_id}"
    search_filter = build_search_filter(
        project_id=group.project_id,
        tenant_id=group.tenant_id,
        partition_id=group.partition_id,
        document_version_ids=list(group.versions),
        tree_version=route.tree_version if route and route.routed else None,
        routing_slot=route.routing_slot if route and route.routed else None,
        membership_node_ids=list(route.membership_node_ids) if route and route.routed else None,
    )
    try:
        vector = [float(value) for value in query_vector]
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("invalid embedding vector")
        semantic_task = asyncio.create_task(
            store.search(
                collection,
                vector_name="content_vector",
                query=vector,
                search_filter=search_filter,
                limit=limit,
            ),
        )
        lexical_task = asyncio.create_task(
            store.search(
                collection,
                vector_name="bm25_sparse",
                query=build_sparse_query_vector(query),
                search_filter=search_filter,
                limit=limit,
            ),
        )
        try:
            semantic, lexical = await asyncio.gather(semantic_task, lexical_task)
        except BaseException:
            semantic_task.cancel()
            lexical_task.cancel()
            await asyncio.gather(semantic_task, lexical_task, return_exceptions=True)
            raise
    except asyncio.CancelledError:
        raise
    except SearchIndexUnavailable:
        raise
    except Exception as error:  # noqa: BLE001 - upstream details may contain private URLs or credentials
        log.warning("canonical hybrid query failed error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical search query failed") from error
    return group, semantic, lexical


async def _query_vector(engine_manager: Any, group: _SearchGroup, query: str) -> list[float]:
    try:
        embedding = await engine_manager.get_sag_embedding(group.source.sag_source_config_id, group.source)
        vector = [float(value) for value in await embedding.generate(query)]
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("invalid embedding vector")
        return vector
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - upstream details may contain private URLs or credentials
        log.warning("canonical query embedding unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Search embedding is unavailable") from error


def _fair_rank_merge(lists: list[list[RetrievedSection]]) -> list[RetrievedSection]:
    """Merge incomparable per-scope scores by deterministic rank interleaving."""
    result: list[RetrievedSection] = []
    seen: set[tuple[str, str]] = set()
    max_length = max((len(items) for items in lists), default=0)
    for rank in range(max_length):
        for items in lists:
            if rank >= len(items):
                continue
            section = items[rank]
            key = (section.source_config_id or "", section.chunk_id or "")
            if key in seen:
                continue
            seen.add(key)
            result.append(section)
    return result


def _bounded_candidate_unit_ids(
    hits: dict[
        tuple[str, str, str, str, str, str],
        tuple[_SearchGroup, SearchUnitHit, float, int, str],
    ],
    *,
    limit: int,
) -> list[str]:
    """Apply a stable RRF/rank candidate budget with round-robin scope coverage.

    This only bounds canonical hydration and reranking work; its preliminary RRF
    ordering is not an answerability or confidence signal.
    """
    if limit <= 0 or not hits:
        return []
    by_scope: dict[tuple[str, str, str, str], dict[str, float]] = defaultdict(dict)
    for key, (_group, _hit, _score, rank, _origin) in hits.items():
        _channel, project_id, source_id, tenant_id, partition_id, unit_id = key
        scope = (project_id, source_id, tenant_id, partition_id)
        rrf = 1.0 / (60 + rank + 1)
        by_scope[scope][unit_id] = by_scope[scope].get(unit_id, 0.0) + rrf

    ranked_by_scope = {
        scope: sorted(units.items(), key=lambda item: (-item[1], item[0]))
        for scope, units in sorted(by_scope.items())
    }
    selected: list[str] = []
    selected_ids: set[str] = set()
    cursors = {scope: 0 for scope in ranked_by_scope}
    while len(selected) < limit:
        advanced = False
        for scope, candidates in ranked_by_scope.items():
            cursor = cursors[scope]
            if cursor >= len(candidates):
                continue
            unit_id = candidates[cursor][0]
            cursors[scope] = cursor + 1
            advanced = True
            if unit_id not in selected_ids:
                selected.append(unit_id)
                selected_ids.add(unit_id)
                if len(selected) >= limit:
                    break
        if not advanced:
            break
    return selected


def _failure_code(error: BaseException) -> str:
    return "QUERY_TIMEOUT" if isinstance(error, TimeoutError) else "QUERY_PATH_FAILED"


def _structural_coverage_status(
    selected_count: int,
    diversity_stats: dict[str, Any],
) -> str:
    if selected_count <= 0:
        return "empty_evidence"
    if not diversity_stats.get("required_anchor_count"):
        return "unknown"
    if diversity_stats.get("anchor_coverage") == "missing_required_anchor":
        return "weak"
    return "structurally_sufficient"


def _routing_trace(
    groups: list[_SearchGroup],
    decisions: dict[tuple[str, str, str, str], RouteDecision],
    verified_origins: dict[tuple[str, str, str, str], dict[str, set[str]]],
    *,
    strategy_plan: QueryStrategyPlan,
    local_failures: dict[tuple[str, str, str, str], str],
    escape_failures: dict[tuple[str, str, str, str], str],
    coverage: dict[str, Any],
    graph_skip_reason: str | None,
    selection_fallback_reason: str | None = None,
) -> dict[str, Any]:
    routed_groups: list[dict[str, Any]] = []
    request_snapshot_ids: set[str] = set()
    fallback_used = selection_fallback_reason is not None
    for group in groups:
        route = decisions.get(group.scope_key)
        origins = verified_origins.get(group.scope_key, {})
        local_ids = origins.get("branch_local", set())
        escape_ids = origins.get("global_escape", set())
        global_ids = escape_ids | origins.get("global_only", set())
        fallback_used = fallback_used or (
            not (route and route.routed)
            or group.scope_key in local_failures
            or bool(escape_ids - local_ids)
            or group.scope_key in escape_failures
        )
        if route and route.request_snapshot_id:
            request_snapshot_ids.add(route.request_snapshot_id)
        selected_nodes = list(route.selected_nodes) if route else []
        routed_groups.append(
            {
                "project_id": group.project_id,
                "source_id": group.source.id,
                "partition_id": group.partition_id,
                "snapshot_id": route.snapshot_id if route else None,
                "request_snapshot_id": route.request_snapshot_id if route else None,
                "tree_version": route.tree_version if route else None,
                "routing_slot": route.routing_slot if route else None,
                "search_epoch": route.search_epoch if route else None,
                "selected_nodes": selected_nodes,
                "membership_count": len(route.membership_node_ids) if route else 0,
                "entropy": route.entropy if route else None,
                "margin": route.margin if route else None,
                "broad_route": route.broad_route if route else False,
                "reason_code": route.reason_code if route else "TREE_SNAPSHOT_UNAVAILABLE",
                "fallback_reason": route.fallback_reason if route else "routing_snapshot_unavailable",
                "branch_local_candidates": len(local_ids),
                "global_candidates": len(global_ids),
                "escape_recovered_candidates": len(escape_ids - local_ids),
                "escape_attempted": True,
                "escape_trigger": (
                    "tree_route_unavailable"
                    if not route or not route.routed
                    else "local_path_failed"
                    if group.scope_key in local_failures
                    else "local_path_empty"
                    if not local_ids
                    else "reserved_parallel_global_escape"
                ),
                "local_failure": local_failures.get(group.scope_key),
                "escape_failure": escape_failures.get(group.scope_key),
                # This is a retrieval-path event, not a corpus-level recall metric.
                "blackhole_detected": bool(
                    route and route.routed and group.scope_key not in local_failures and not local_ids and global_ids
                ),
            }
        )
    return {
        "planner": strategy_plan.as_trace(),
        "request_snapshot_id": next(iter(request_snapshot_ids)) if len(request_snapshot_ids) == 1 else None,
        "groups": routed_groups,
        "coverage": coverage,
        "selection_fallback_reason": selection_fallback_reason,
        "multi_hop": {
            "eligible": strategy_plan.multi_hop_eligible,
            "effective": strategy_plan.primary_strategy.value,
            "graph_expansion_attempted": False,
            "graph_expansion_skip_reason": graph_skip_reason,
        },
        "fallback_used": fallback_used,
    }


def _empty_routing_trace(strategy_plan: QueryStrategyPlan, reason: str) -> dict[str, Any]:
    return {
        "planner": strategy_plan.as_trace(),
        "request_snapshot_id": None,
        "groups": [],
        "coverage": {"status": "unknown", "reason": reason},
        "multi_hop": {
            "eligible": strategy_plan.multi_hop_eligible,
            "effective": strategy_plan.primary_strategy.value,
            "graph_expansion_attempted": False,
            "graph_expansion_skip_reason": "phase5_adapter_unavailable" if strategy_plan.multi_hop_eligible else None,
        },
        "fallback_used": True,
    }


async def retrieve_search_unit_sections(
    engine_manager: Any,
    sources: list[SearchableSource],
    query: str,
    *,
    principal: VerifiedPrincipal | None,
    top_k: int | None = None,
    query_strategy_plan: QueryStrategyPlan | None = None,
) -> SearchOutcome:
    """Query only currently authorized versions with a verified Phase 2C manifest."""
    from sag_api.services.retrieval_service import rerank_sections, select_diverse_sections

    requested_limit = max(1, min(int(top_k or settings.search_top_k), 50))
    candidate_limit = min(50, max(requested_limit * 3, requested_limit + 8))
    strategy_plan = query_strategy_plan or plan_query(query)
    retrieval_stats = {
        "requested_top_k": requested_limit,
        "candidate_top_k": candidate_limit,
        "candidate_unit_pool_limit": settings.search_unit_candidate_limit,
    }
    if principal is None or not sources or not principal.tenant_id or not principal.allowed_partition_ids:
        return SearchOutcome(
            query=query,
            sections=[],
            stats={
                "canonical_index": True,
                "ready_versions": 0,
                **retrieval_stats,
                "routing": _empty_routing_trace(strategy_plan, "no_authorized_scope"),
            },
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(settings.search_source_timeout))
    try:
        async with asyncio.timeout_at(deadline):
            async with SessionLocal() as session:
                ready_versions = await _load_current_ready_versions(session, sources, principal)
    except TimeoutError as error:
        log.warning("canonical search authorization exceeded query budget")
        raise ServiceUnavailableError("Canonical search exceeded the query budget") from error
    groups = _groups(ready_versions)
    if not groups:
        return SearchOutcome(
            query=query,
            sections=[],
            stats={
                "canonical_index": True,
                "ready_versions": 0,
                **retrieval_stats,
                "routing": _empty_routing_trace(strategy_plan, "no_verified_search_ready_group"),
            },
        )

    decisions: dict[tuple[str, str, str, str], RouteDecision] = {}
    local_failures: dict[tuple[str, str, str, str], str] = {}
    escape_failures: dict[tuple[str, str, str, str], str] = {}
    try:
        async with asyncio.timeout_at(deadline):
            client = await engine_manager.get_search_unit_qdrant_client()
            store = SearchUnitQdrantStore(client)
            semaphore = asyncio.Semaphore(max(1, settings.search_source_concurrency))
            snapshot_budget = min(
                float(settings.search_tree_snapshot_timeout),
                max(
                    0.05,
                    deadline - loop.time() - float(settings.search_tree_escape_reserve_seconds),
                ),
            )
            decisions = await capture_routing_decisions(
                engine_manager,
                groups,
                query=query,
                planner_trace=strategy_plan.as_trace(),
                timeout_seconds=snapshot_budget,
            )

            async def bounded(group: _SearchGroup):
                async with semaphore:
                    route = decisions.get(group.scope_key)
                    vector = await _query_vector(engine_manager, group, query)
                    if route is None or not route.routed:
                        result = await _query_group(
                            group,
                            query=query,
                            limit=candidate_limit,
                            store=store,
                            query_vector=vector,
                        )
                        return [(group, route, "global_only", result[1], result[2])], None, None

                    # Each bounded path gets the same candidate cap. The tree path has a
                    # shorter deadline so it cannot consume the reserved global-escape time.
                    now = loop.time()
                    remaining = max(0.1, deadline - now)
                    local_reserve = min(
                        float(settings.search_tree_escape_reserve_seconds),
                        remaining / 2,
                    )
                    local_deadline = deadline - local_reserve
                    escape_deadline = deadline - 0.05

                    async def query_local():
                        async with asyncio.timeout_at(local_deadline):
                            return await _query_group(
                                group,
                                query=query,
                                limit=candidate_limit,
                                store=store,
                                query_vector=vector,
                                route=route,
                            )

                    async def query_escape():
                        async with asyncio.timeout_at(escape_deadline):
                            return await _query_group(
                                group,
                                query=query,
                                limit=candidate_limit,
                                store=store,
                                query_vector=vector,
                            )

                    escape_task = asyncio.create_task(query_escape())
                    local_task = asyncio.create_task(query_local())
                    local_result, escape_result = await asyncio.gather(local_task, escape_task, return_exceptions=True)
                    local_failure = _failure_code(local_result) if isinstance(local_result, BaseException) else None
                    escape_failure = _failure_code(escape_result) if isinstance(escape_result, BaseException) else None
                    if isinstance(local_result, asyncio.CancelledError):
                        raise local_result
                    if isinstance(escape_result, asyncio.CancelledError):
                        raise escape_result
                    if isinstance(local_result, BaseException) and isinstance(escape_result, BaseException):
                        raise escape_result
                    path_results = []
                    if isinstance(local_result, BaseException):
                        local_result = None
                    else:
                        path_results.append((group, route, "branch_local", local_result[1], local_result[2]))
                    if isinstance(escape_result, BaseException):
                        escape_result = None
                    else:
                        path_results.append((group, route, "global_escape", escape_result[1], escape_result[2]))
                    return path_results, local_failure, escape_failure

            group_tasks = [asyncio.create_task(bounded(group)) for group in groups]
            try:
                path_groups = await asyncio.gather(*group_tasks)
            except BaseException:
                for task in group_tasks:
                    task.cancel()
                await asyncio.gather(*group_tasks, return_exceptions=True)
                raise
        channel_results = [item for path_group, _local_failure, _escape_failure in path_groups for item in path_group]
        local_failures = {
            group.scope_key: local_failure
            for group, (_paths, local_failure, _escape_failure) in zip(groups, path_groups, strict=True)
            if local_failure is not None
        }
        escape_failures = {
            group.scope_key: escape_failure
            for group, (_paths, _local_failure, escape_failure) in zip(groups, path_groups, strict=True)
            if escape_failure is not None
        }
    except asyncio.CancelledError:
        raise
    except TimeoutError as error:
        log.warning("canonical routed retrieval exhausted request budget")
        raise ServiceUnavailableError("Canonical search exceeded the query budget") from error
    except SearchIndexUnavailable as error:
        log.warning("canonical search index unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical search index is unavailable") from error
    except httpx.HTTPError as error:
        log.warning("canonical search client unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical search index is unavailable") from error

    hits: dict[
        tuple[str, str, str, str, str, str],
        tuple[_SearchGroup, SearchUnitHit, float, int, str],
    ] = {}
    hit_variants: dict[
        tuple[str, str, str, str, str, str],
        list[tuple[str, SearchUnitHit]],
    ] = defaultdict(list)
    for group, _route, origin, semantic_hits, lexical_hits in channel_results:
        for channel_hits, channel in ((semantic_hits, "dense"), (lexical_hits, "sparse")):
            max_score = max((max(0.0, hit.score) for hit in channel_hits), default=0.0)
            for rank, hit in enumerate(channel_hits):
                unit_id = str(hit.payload.get("search_unit_id") or "")
                if (
                    not unit_id
                    or hit.payload.get("project_id") != group.project_id
                    or hit.payload.get("tenant_id") != group.tenant_id
                    or hit.payload.get("security_partition_id") != group.partition_id
                    or hit.payload.get("document_version_id") not in group.versions
                ):
                    continue
                key = (group.project_id, group.source.id, group.tenant_id, group.partition_id, unit_id)
                normalized_score = max(0.0, hit.score) / max_score if max_score > 0.0 else 0.0
                hit_key = (channel, *key)
                hit_variants[hit_key].append((origin, hit))
                candidate = (group, hit, normalized_score, rank, origin)
                previous = hits.get(hit_key)
                if previous is None or (rank, -normalized_score, origin) < (
                    previous[3],
                    -previous[2],
                    previous[4],
                ):
                    # A unit seen by both paths retains the best rank in this channel;
                    # it does not receive a second RRF vote.
                    hits[hit_key] = candidate

    candidate_units_before_cap = len({key[-1] for key in hits})
    candidate_unit_ids = _bounded_candidate_unit_ids(
        hits,
        limit=settings.search_unit_candidate_limit,
    )
    selected_candidate_ids = set(candidate_unit_ids)
    hits = {key: value for key, value in hits.items() if key[-1] in selected_candidate_ids}
    retrieval_stats.update(
        {
            "candidate_units_before_cap": candidate_units_before_cap,
            "candidate_units_after_cap": len(candidate_unit_ids),
            "candidate_units_truncated": candidate_units_before_cap - len(candidate_unit_ids),
        }
    )
    if not candidate_unit_ids:
        effective_plan = promote_multi_hop(
            strategy_plan,
            coverage_reason="NO_BRANCH_EVIDENCE" if strategy_plan.multi_hop_eligible else None,
        )
        routing = _routing_trace(
            groups,
            decisions,
            {},
            strategy_plan=effective_plan,
            local_failures=local_failures,
            escape_failures=escape_failures,
            coverage={"status": "unknown", "reason": "no_retrieval_candidates"},
            graph_skip_reason="phase5_adapter_unavailable"
            if effective_plan.primary_strategy.value == "MULTI_HOP"
            else None,
        )
        return SearchOutcome(
            query=query,
            sections=[],
            stats={
                "canonical_index": True,
                "ready_versions": len(ready_versions),
                **retrieval_stats,
                "routing": routing,
            },
        )

    # Recheck Source mappings, active documents, partitions, and latest attempts after Qdrant returns.
    try:
        async with asyncio.timeout_at(deadline):
            async with SessionLocal() as session:
                current_versions = await _load_current_ready_versions(session, sources, principal)
                start_block = aliased(CanonicalBlock)
                end_block = aliased(CanonicalBlock)
                rows = (
                    await session.execute(
                        select(
                            SearchUnit,
                            Document.id,
                            Document.filename,
                            Document.source_id,
                            DocumentVersion.version_no,
                            start_block.source_anchor,
                            start_block.ordinal,
                            end_block.ordinal,
                        )
                        .join(DocumentVersion, DocumentVersion.id == SearchUnit.document_version_id)
                        .join(Document, Document.id == DocumentVersion.document_id)
                        .join(start_block, start_block.id == SearchUnit.block_from_id)
                        .join(end_block, end_block.id == SearchUnit.block_to_id)
                        .where(
                            SearchUnit.id.in_(candidate_unit_ids),
                            start_block.document_version_id == SearchUnit.document_version_id,
                            end_block.document_version_id == SearchUnit.document_version_id,
                            Document.source_id.in_([source.id for source in sources]),
                            Document.is_active.is_(True),
                        )
                    )
                ).all()
    except SQLAlchemyError as error:
        log.warning("canonical evidence lookup unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical search evidence is unavailable") from error
    except TimeoutError as error:
        log.warning("canonical evidence hydration exceeded query budget")
        raise ServiceUnavailableError("Canonical search exceeded the query budget") from error

    unit_by_id: dict[str, tuple[SearchUnit, dict[str, Any]]] = {}
    for unit, document_id, filename, source_id, version_no, anchor, start_ordinal, end_ordinal in rows:
        version = current_versions.get(str(unit.document_version_id))
        if (
            version is None
            or version.source_id != str(source_id)
            or version.partition_id != str(unit.security_partition_id)
            or start_ordinal > end_ordinal
            or int(unit.page_from or 0) < 1
            or int(unit.page_to or 0) < int(unit.page_from or 0)
            or not isinstance(anchor, str)
            or not anchor.strip()
        ):
            continue
        unit_by_id[str(unit.id)] = (
            unit,
            {
                "document_id": str(document_id),
                "document_version_id": str(unit.document_version_id),
                "document_name": str(filename or ""),
                "version_no": int(version_no),
                "source_id": str(source_id),
                "source_name": version.source.name,
                "page_from": int(unit.page_from),
                "page_to": int(unit.page_to),
                "anchor": anchor.strip(),
                "section_path": unit.section_path,
                "block_from_id": unit.block_from_id,
                "block_to_id": unit.block_to_id,
                "search_unit_id": unit.id,
                "content_hash": unit.content_hash,
                "project_id": version.project_id,
                "tenant_id": version.tenant_id,
                "security_partition_id": version.partition_id,
            },
        )

    sections_by_key: dict[tuple[str, str, str, str, str], RetrievedSection] = {}
    verified_origins: dict[tuple[str, str, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    ranked_by_channel_and_scope: dict[
        tuple[str, tuple[str, str, str, str]], list[RetrievedSection]
    ] = defaultdict(list)
    semantic_candidate_count = 0
    lexical_candidate_count = 0
    for channel, project_id, source_id, tenant_id, partition_id, unit_id in sorted(hits):
        hit_key = (channel, project_id, source_id, tenant_id, partition_id, unit_id)
        group, hit, score, rank, origin = hits[hit_key]
        resolved = unit_by_id.get(unit_id)
        if resolved is None:
            continue
        unit, locator = resolved
        payload = hit.payload
        if (
            payload.get("search_unit_id") != unit.id
            or payload.get("document_version_id") != unit.document_version_id
            or payload.get("project_id") != locator["project_id"]
            or payload.get("tenant_id") != locator["tenant_id"]
            or payload.get("security_partition_id") != locator["security_partition_id"]
            or payload.get("content_hash") != unit.content_hash
        ):
            continue
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != unit.content_hash:
            continue
        from sag_api.services.search_index_service import generate_search_unit_point_id

        if hit.point_id != generate_search_unit_point_id(f"search_units_{project_id}", unit.id):
            continue
        section_key = (project_id, source_id, tenant_id, partition_id, unit_id)
        scope_key = (project_id, source_id, tenant_id, partition_id)
        for variant_origin, variant in hit_variants.get(hit_key, []):
            variant_payload = variant.payload
            variant_content = variant_payload.get("content")
            if (
                variant.point_id == generate_search_unit_point_id(f"search_units_{project_id}", unit.id)
                and variant_payload.get("search_unit_id") == unit.id
                and variant_payload.get("document_version_id") == unit.document_version_id
                and variant_payload.get("project_id") == locator["project_id"]
                and variant_payload.get("tenant_id") == locator["tenant_id"]
                and variant_payload.get("security_partition_id") == locator["security_partition_id"]
                and variant_payload.get("content_hash") == unit.content_hash
                and isinstance(variant_content, str)
                and hashlib.sha256(variant_content.encode("utf-8")).hexdigest() == unit.content_hash
            ):
                verified_origins[scope_key][variant_origin].add(unit_id)
        section = sections_by_key.get(section_key)
        if section is None:
            scope_source = group.source
            section = RetrievedSection(
                chunk_id=unit.id,
                search_unit_id=unit.id,
                block_from_id=unit.block_from_id,
                block_to_id=unit.block_to_id,
                section_path=unit.section_path,
                heading=unit.section_path or locator["document_name"],
                content=content,
                score=0.0,
                rank=0,
                source_id=scope_source.id,
                source_config_id=scope_source.sag_source_config_id,
                content_hash=unit.content_hash,
                canonical_evidence_verified=True,
                **{
                    name: value
                    for name, value in locator.items()
                    if name
                    not in {
                        "source_id",
                        "source_name",
                        "project_id",
                        "tenant_id",
                        "security_partition_id",
                        "search_unit_id",
                        "content_hash",
                        "block_from_id",
                        "block_to_id",
                        "section_path",
                    }
                },
            )
            sections_by_key[section_key] = section
        interleaved_rank = rank if origin == "global_only" else rank * 2 + int(origin != "branch_local")
        ranked_by_channel_and_scope[(channel, scope_key)].append(
            section.model_copy(update={"score": score, "rank": interleaved_rank})
        )
        if channel == "dense":
            semantic_candidate_count += 1
        else:
            lexical_candidate_count += 1

    semantic_lists = [
        sorted(
            ranked_by_channel_and_scope.get(("dense", group.scope_key), []),
            key=lambda section: (section.rank, section.chunk_id or ""),
        )
        for group in groups
    ]
    lexical_lists = [
        sorted(
            ranked_by_channel_and_scope.get(("sparse", group.scope_key), []),
            key=lambda section: (section.rank, section.chunk_id or ""),
        )
        for group in groups
    ]
    semantic = _fair_rank_merge(semantic_lists)
    lexical = _fair_rank_merge(lexical_lists)
    selection_fallback_reason = None
    rerank_remaining = deadline - loop.time()
    if rerank_remaining <= settings.search_rerank_min_remaining_seconds:
        selection_fallback_reason = "rerank_budget_exhausted"
        fallback_pool = _fair_rank_merge([semantic, lexical])
        selected = fallback_pool[:requested_limit]
        rerank_stats = {
            "candidates": len(fallback_pool),
            "relevant": None,
            "filtered_irrelevant": None,
        }
        coverage = {
            "status": "unknown",
            "reason": selection_fallback_reason,
            "canonical_locator_verified": bool(selected)
            and all(
                section.canonical_evidence_verified
                and section.document_id
                and section.document_version_id
                and section.page_from is not None
                and section.anchor
                for section in selected
            ),
            "semantic_answerability": "unknown",
        }
        selection_method = "latency_fallback_rank_interleave"
    else:
        fused = rerank_sections(query, semantic, lexical=lexical, limit=candidate_limit)
        rerank_stats = {
            "candidates": fused.candidate_count,
            "relevant": fused.relevant_count,
            "filtered_irrelevant": fused.filtered_count,
        }
        selected, diversity_stats = select_diverse_sections(
            query,
            fused.sections,
            limit=requested_limit,
        )
        coverage = {
            **diversity_stats,
            "status": _structural_coverage_status(len(selected), diversity_stats),
            "canonical_locator_verified": bool(selected)
            and all(
                section.canonical_evidence_verified
                and section.document_id
                and section.document_version_id
                and section.page_from is not None
                and section.anchor
                for section in selected
            ),
            "semantic_answerability": "unknown",
        }
        selection_method = "authorized_content_dedup_mmr"
    coverage_reason = (
        "NO_BRANCH_EVIDENCE"
        if coverage["status"] == "empty_evidence"
        else "MISSING_REQUIRED_QUERY_ANCHOR"
        if coverage["status"] == "weak"
        else None
    )
    effective_plan = promote_multi_hop(strategy_plan, coverage_reason=coverage_reason)
    graph_skip_reason = "phase5_adapter_unavailable" if effective_plan.primary_strategy.value == "MULTI_HOP" else None
    routing = _routing_trace(
        groups,
        decisions,
        verified_origins,
        strategy_plan=effective_plan,
        local_failures=local_failures,
        escape_failures=escape_failures,
        coverage=coverage,
        graph_skip_reason=graph_skip_reason,
        selection_fallback_reason=selection_fallback_reason,
    )
    return SearchOutcome(
        query=query,
        sections=selected,
        stats={
            "canonical_index": True,
            "ready_versions": len(ready_versions),
            **retrieval_stats,
            "semantic_candidates": semantic_candidate_count,
            "lexical_candidates": lexical_candidate_count,
                **rerank_stats,
            "fusion_method": "rank_interleave_fallback" if selection_fallback_reason else "rrf",
            "selection_method": selection_method,
            "cross_scope_merge": "rank_interleave",
            "coverage": coverage,
            "routing": routing,
            "selection_fallback_reason": selection_fallback_reason,
        },
    )


async def get_search_unit_citation(
    session: AsyncSession,
    *,
    source: SearchableSource,
    principal: VerifiedPrincipal,
    search_unit_id: str,
    engine_manager: Any,
) -> dict[str, Any] | None:
    try:
        return await _get_search_unit_citation_checked(
            session,
            source=source,
            principal=principal,
            search_unit_id=search_unit_id,
            engine_manager=engine_manager,
        )
    except SQLAlchemyError as error:
        log.warning("canonical citation authorization unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical citation authorization is unavailable") from error


async def _get_search_unit_citation_checked(
    session: AsyncSession,
    *,
    source: SearchableSource,
    principal: VerifiedPrincipal,
    search_unit_id: str,
    engine_manager: Any,
) -> dict[str, Any] | None:
    """Read an exact canonical citation target, rechecking ACL and index provenance.

    ``None`` means the identifier is not a SearchUnit and may be handled by the
    existing legacy chunk reader. A known SearchUnit outside this caller's
    scope raises a generic 404 so it can never fall through to legacy lookup.
    Historical versions remain readable while retained and authorized; their
    immutable version and latest verified search manifest are checked here.
    """
    from sag_api.core.errors import NotFoundError

    exists = await session.scalar(select(SearchUnit.id).where(SearchUnit.id == search_unit_id).limit(1))
    if exists is None:
        return None
    if not principal.tenant_id or not principal.allowed_partition_ids or not principal.allowed_project_ids:
        raise NotFoundError("原文分块不存在")

    start_block = aliased(CanonicalBlock)
    end_block = aliased(CanonicalBlock)
    partition_expression = DocumentVersion.metadata_json["security_partition_id"].as_string()
    row = (
        await session.execute(
            select(
                SearchUnit,
                Document.id,
                Document.filename,
                Document.source_id,
                Document.project_id,
                Document.tenant_id,
                DocumentVersion.version_no,
                start_block.source_anchor,
                start_block.ordinal,
                end_block.ordinal,
            )
            .join(DocumentVersion, DocumentVersion.id == SearchUnit.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(start_block, start_block.id == SearchUnit.block_from_id)
            .join(end_block, end_block.id == SearchUnit.block_to_id)
            .join(SourceProjectMapping, SourceProjectMapping.source_id == Document.source_id)
            .where(
                SearchUnit.id == search_unit_id,
                Document.source_id == source.id,
                Document.project_id.in_(principal.allowed_project_ids),
                Document.tenant_id == principal.tenant_id,
                SourceProjectMapping.project_id == Document.project_id,
                SourceProjectMapping.organization_id == principal.organization_id,
                SourceProjectMapping.state == "CONFIRMED",
                Document.is_active.is_(True),
                Document.status.notin_([DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED]),
                DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
                DocumentVersion.search_ready_at.is_not(None),
                SearchUnit.security_partition_id.in_(principal.allowed_partition_ids),
                SearchUnit.security_partition_id == partition_expression,
                start_block.document_version_id == SearchUnit.document_version_id,
                end_block.document_version_id == SearchUnit.document_version_id,
            )
            .limit(1)
        )
    ).first()
    if row is None:
        raise NotFoundError("原文分块不存在")

    unit, document_id, filename, source_id, project_id, tenant_id, version_no, anchor, start_ordinal, end_ordinal = row
    if document_id in await _hidden_reprocess_document_ids(session, [source.id]):
        raise NotFoundError("原文分块不存在")
    if (
        start_ordinal > end_ordinal
        or int(unit.page_from or 0) < 1
        or int(unit.page_to or 0) < int(unit.page_from or 0)
        or not isinstance(anchor, str)
        or not anchor.strip()
    ):
        raise NotFoundError("原文分块不存在")

    latest_run = (
        await session.execute(
            select(IngestionRun)
            .where(IngestionRun.document_version_id == unit.document_version_id)
            .order_by(IngestionRun.created_at.desc(), IngestionRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_run is None or latest_run.project_id != project_id or latest_run.tenant_id != tenant_id:
        raise NotFoundError("原文分块不存在")
    latest_index_stage = (
        await session.execute(
            select(StageRun)
            .where(StageRun.run_id == latest_run.id, StageRun.stage == "INDEX_SEARCH")
            .order_by(StageRun.created_at.desc(), StageRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        latest_index_stage is None
        or latest_index_stage.status != "SUCCESS"
        or not _stage_belongs_to_current_attempt(
            latest_run.started_at,
            latest_index_stage,
        )
        or not _verified_manifest(latest_index_stage.metrics_json, project_id=project_id)
    ):
        raise NotFoundError("原文分块不存在")

    collection = f"search_units_{project_id}"
    try:
        client = await engine_manager.get_search_unit_qdrant_client()
        point = await SearchUnitQdrantStore(client).get_point(
            collection,
            _point_id(collection, unit.id),
        )
    except SearchIndexUnavailable as error:
        log.warning("canonical citation index unavailable error_type=%s", type(error).__name__)
        raise ServiceUnavailableError("Canonical citation index is unavailable") from error
    if point is None:
        raise NotFoundError("原文分块不存在")

    payload = point.payload
    content = payload.get("content")
    if (
        point.point_id != _point_id(collection, unit.id)
        or payload.get("search_unit_id") != unit.id
        or payload.get("document_version_id") != unit.document_version_id
        or payload.get("project_id") != project_id
        or payload.get("tenant_id") != tenant_id
        or payload.get("security_partition_id") != unit.security_partition_id
        or payload.get("content_hash") != unit.content_hash
        or not isinstance(content, str)
        or not content.strip()
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != unit.content_hash
    ):
        raise NotFoundError("原文分块不存在")

    return {
        "chunk_id": unit.id,
        "search_unit_id": unit.id,
        "block_from_id": unit.block_from_id,
        "block_to_id": unit.block_to_id,
        "document_id": str(document_id),
        "document_version_id": str(unit.document_version_id),
        "document_name": str(filename or ""),
        "version_no": int(version_no),
        "page_from": int(unit.page_from),
        "page_to": int(unit.page_to),
        "anchor": anchor.strip(),
        "section_path": unit.section_path,
        "heading": unit.section_path or str(filename or ""),
        "content": content,
        "rank": 0,
        "source_id": source_id,
        "source_name": source.name,
    }


def _point_id(collection: str, unit_id: str) -> str:
    from sag_api.services.search_index_service import generate_search_unit_point_id

    return generate_search_unit_point_id(collection, unit_id)
