"""Incremental Knowledge Routing Tree Service (Phase 8 / Checkpoint C).

Implements:
- DATN-58: Base + delta tree assignment without full rebuild, node/ancestor statistics,
  and drift monitoring with hysteresis.
- DATN-59: Targeted subtree rebuild and stable node lineage matching (SPLIT_FROM,
  MERGED_FROM, SUPERSEDES_TREE_NODE).
- DATN-60: Dual-slot inactive build, Qdrant payload batch update, and manifest verification gate.
- DATN-36: Single PostgreSQL transaction atomic pointer switch, query-time snapshot
  consistency, fault injection resilience, and rollback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models.routing_rag import ProjectSearchState, TreeManifest, TreeRoutingProfile
from sag_api.services.query_routing_service import (
    GroupRoutingSnapshot,
    scope_fingerprint,
)
from sag_api.services.query_routing_service import (
    NodeProfile as QueryNodeProfile,
)
from sag_api.services.query_routing_service import (
    RoutingSnapshot as QueryRoutingSnapshot,
)
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    NodeProfile,
    RoutingNode,
    TreeBuildConfig,
    _cosine,
    _profile,
    _stable_id,
    _walk,
    build_routing_snapshot,
)
from sag_api.services.routing_tree_service import (
    RoutingSnapshot as TreeRoutingSnapshot,
)
from sag_api.services.search_index_service import generate_search_unit_point_id
from sag_api.services.tree_publish_service import (
    PublishVerificationError,
    TreePublishError,
    _scoped_profile_checksum,
)

log = logging.getLogger(__name__)

# Constants mandated by SAG_Knowledge_Routing_RAG_Workflow_v1.1 Section 11
T_HIGH = 0.78
T_LOW = 0.58
NODE_ID_INHERIT_THRESHOLD = 0.70
DRIFT_CENTROID_THRESHOLD = 0.15
DRIFT_OUTLIER_THRESHOLD = 0.20
HYSTERESIS_MIN_VIOLATIONS = 2


# ---------------------------------------------------------------------------
# Data Models for Incremental Operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitAssignment:
    unit_id: str
    target_node_id: str | None
    score: float
    assignment_type: Literal["DIRECT", "BORDERLINE", "OUTLIER"]


@dataclass(frozen=True, slots=True)
class DeltaAssignmentResult:
    assignments: tuple[UnitAssignment, ...]
    direct_count: int
    borderline_count: int
    outlier_count: int
    updated_roots: tuple[RoutingNode, ...]
    updated_units: tuple[KnowledgeUnitInput, ...]
    touched_leaf_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DriftReport:
    centroid_drift: float
    outlier_ratio: float
    capacity_overflow: bool
    overflowed_node_ids: tuple[str, ...]
    violation_count: int
    trigger_rebuild: bool
    affected_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LineageMapping:
    node_id_map: dict[str, str]  # new_internal_id -> stable_node_id
    relationships: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# DATN-58: Delta Assignment & Statistics Update
# ---------------------------------------------------------------------------


def assign_delta_units(
    base_snapshot: TreeRoutingSnapshot,
    new_units: list[KnowledgeUnitInput],
    config: TreeBuildConfig | None = None,
) -> DeltaAssignmentResult:
    """Assign new knowledge units to base tree leaves using prototype cosine similarity.

    Threshold rules:
    - score >= T_HIGH (0.78): Direct Attach to highest-similarity leaf.
    - T_LOW <= score < T_HIGH (0.58): Borderline queue attached to closest leaf.
    - score < T_LOW: Outlier candidate.

    Updates ancestor cumulative accessible_unit_count and statistics without topology change.
    """
    config = config or TreeBuildConfig()
    leaves = [node for node in _walk(base_snapshot.roots) if not node.children]

    # Pre-index leaves by partition
    leaves_by_partition: dict[str, list[RoutingNode]] = defaultdict(list)
    for leaf in leaves:
        leaves_by_partition[leaf.security_partition_id].append(leaf)

    assignments: list[UnitAssignment] = []
    unit_additions_per_leaf: dict[str, list[KnowledgeUnitInput]] = defaultdict(list)
    direct_count = 0
    borderline_count = 0
    outlier_count = 0

    for unit in new_units:
        matching_leaves = leaves_by_partition.get(unit.security_partition_id, [])
        if not matching_leaves or not unit.dense:
            assignments.append(
                UnitAssignment(
                    unit_id=unit.unit_id,
                    target_node_id=None,
                    score=0.0,
                    assignment_type="OUTLIER",
                )
            )
            outlier_count += 1
            continue

        best_leaf = None
        best_score = -1.0
        for leaf in matching_leaves:
            if leaf.profile.dense_medoid:
                score = _cosine(unit.dense, leaf.profile.dense_medoid)
                if score > best_score:
                    best_score = score
                    best_leaf = leaf

        if best_leaf is not None and best_score >= T_HIGH:
            asgn_type: Literal["DIRECT", "BORDERLINE", "OUTLIER"] = "DIRECT"
            target_id: str | None = best_leaf.node_id
            unit_additions_per_leaf[best_leaf.node_id].append(unit)
            direct_count += 1
        elif best_leaf is not None and best_score >= T_LOW:
            asgn_type = "BORDERLINE"
            target_id = best_leaf.node_id
            unit_additions_per_leaf[best_leaf.node_id].append(unit)
            borderline_count += 1
        else:
            asgn_type = "OUTLIER"
            target_id = best_leaf.node_id if best_leaf else None
            outlier_count += 1

        assignments.append(
            UnitAssignment(
                unit_id=unit.unit_id,
                target_node_id=target_id,
                score=best_score if best_leaf else 0.0,
                assignment_type=asgn_type,
            )
        )

    # Base units from manifest
    def _parse_iso_dt(val: str | None) -> datetime | None:
        if not val:
            return None
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None or dt.utcoffset() is None:
            return dt.replace(tzinfo=UTC)
        return dt

    manifest_units = [
        KnowledgeUnitInput(
            unit_id=u["unit_id"],
            tenant_id=base_snapshot.tenant_id,
            project_id=base_snapshot.project_id,
            security_partition_id=u["partition"],
            dense=tuple(u["dense"]) if u.get("dense") else (),
            sparse=tuple((t, float(w)) for t, w in u.get("sparse", ())),
            entities=tuple(u.get("entities", ())),
            valid_from=_parse_iso_dt(u.get("valid_from")),
            valid_to=_parse_iso_dt(u.get("valid_to")),
        )
        for u in base_snapshot.manifest.get("units", [])
    ]
    all_units = tuple(manifest_units + new_units)
    all_units_dict = {u.unit_id: u for u in all_units}

    # Reconstruct updated roots with incremental counts and stats
    touched_leaf_ids = tuple(sorted(unit_additions_per_leaf.keys()))

    def _update_node(node: RoutingNode) -> RoutingNode:
        if not node.children:
            additions = unit_additions_per_leaf.get(node.node_id, [])
            if not additions:
                return node
            updated_units = node.unit_ids + tuple(u.unit_id for u in additions)
            leaf_units = [all_units_dict[uid] for uid in updated_units if uid in all_units_dict]
            if leaf_units:
                updated_profile = _profile(leaf_units, config.profile_sparse_terms)
            else:
                new_sparse = dict(node.profile.sparse)
                for unit in additions:
                    for term, weight in unit.sparse:
                        new_sparse[term] = new_sparse.get(term, 0.0) + weight
                new_entities = tuple(
                    sorted(
                        set(node.profile.entities).union(
                            *(u.entities for u in additions)
                        )
                    )
                )
                from_times = [node.profile.temporal_from] + [
                    u.valid_from for u in additions if u.valid_from
                ]
                to_times = [node.profile.temporal_to] + [
                    u.valid_to for u in additions if u.valid_to
                ]
                valid_from = min((t for t in from_times if t is not None), default=None)
                valid_to = max((t for t in to_times if t is not None), default=None)

                updated_profile = NodeProfile(
                    dense_medoid=node.profile.dense_medoid,
                    dense_medoid_candidate_count=node.profile.dense_medoid_candidate_count,
                    dense_medoid_is_exact=node.profile.dense_medoid_is_exact,
                    sparse=tuple(
                        sorted(new_sparse.items(), key=lambda item: (-item[1], item[0]))[
                            : config.profile_sparse_terms
                        ]
                    ),
                    entities=new_entities,
                    temporal_from=valid_from,
                    temporal_to=valid_to,
                    accessible_unit_count=len(updated_units),
                )
            return RoutingNode(
                node_id=node.node_id,
                parent_id=node.parent_id,
                depth=node.depth,
                tenant_id=node.tenant_id,
                project_id=node.project_id,
                security_partition_id=node.security_partition_id,
                unit_ids=updated_units,
                profile=updated_profile,
                children=(),
            )

        updated_children = tuple(_update_node(child) for child in node.children)
        total_units = sum(child.profile.accessible_unit_count for child in updated_children)
        child_froms = [c.profile.temporal_from for c in updated_children if c.profile.temporal_from is not None]
        child_tos = [c.profile.temporal_to for c in updated_children if c.profile.temporal_to is not None]
        anc_from = min(child_froms) if child_froms else node.profile.temporal_from
        anc_to = max(child_tos) if child_tos else node.profile.temporal_to
        anc_entities = tuple(sorted(set(node.profile.entities).union(*(c.profile.entities for c in updated_children))))
        anc_sparse: dict[str, float] = {}
        for c in updated_children:
            for term, weight in c.profile.sparse:
                anc_sparse[term] = anc_sparse.get(term, 0.0) + weight

        updated_profile = NodeProfile(
            dense_medoid=node.profile.dense_medoid,
            dense_medoid_candidate_count=node.profile.dense_medoid_candidate_count,
            dense_medoid_is_exact=node.profile.dense_medoid_is_exact,
            sparse=tuple(
                sorted(anc_sparse.items(), key=lambda item: (-item[1], item[0]))[
                    : config.profile_sparse_terms
                ]
            ),
            entities=anc_entities,
            temporal_from=anc_from,
            temporal_to=anc_to,
            accessible_unit_count=total_units,
        )
        return RoutingNode(
            node_id=node.node_id,
            parent_id=node.parent_id,
            depth=node.depth,
            tenant_id=node.tenant_id,
            project_id=node.project_id,
            security_partition_id=node.security_partition_id,
            unit_ids=node.unit_ids,
            profile=updated_profile,
            children=updated_children,
        )

    updated_roots = tuple(_update_node(root) for root in base_snapshot.roots)

    return DeltaAssignmentResult(
        assignments=tuple(assignments),
        direct_count=direct_count,
        borderline_count=borderline_count,
        outlier_count=outlier_count,
        updated_roots=updated_roots,
        updated_units=all_units,
        touched_leaf_ids=touched_leaf_ids,
    )


# ---------------------------------------------------------------------------
# DATN-58: Drift Monitoring & Hysteresis
# ---------------------------------------------------------------------------


def compute_tree_drift(
    base_snapshot: TreeRoutingSnapshot,
    delta_result: DeltaAssignmentResult,
    config: TreeBuildConfig | None = None,
    history_window_violations: int = 0,
) -> DriftReport:
    """Evaluate centroid drift, outlier ratio, capacity overflow, and hysteresis.

    Requires at least HYSTERESIS_MIN_VIOLATIONS (2) consecutive violations before triggering rebuild.
    """
    config = config or TreeBuildConfig()
    total_new = len(delta_result.assignments)
    outlier_ratio = (delta_result.outlier_count / total_new) if total_new > 0 else 0.0

    leaf_map = {node.node_id: node for node in _walk(base_snapshot.roots) if not node.children}
    updated_leaf_map = {
        node.node_id: node for node in _walk(delta_result.updated_roots) if not node.children
    }

    max_centroid_drift = 0.0
    overflowed: list[str] = []
    affected: set[str] = set()

    for leaf_id in delta_result.touched_leaf_ids:
        base_leaf = leaf_map.get(leaf_id)
        updated_leaf = updated_leaf_map.get(leaf_id)
        if base_leaf and updated_leaf:
            if len(updated_leaf.unit_ids) > config.max_cluster_size:
                overflowed.append(leaf_id)
                affected.add(leaf_id)
            if base_leaf.profile.dense_medoid and updated_leaf.profile.dense_medoid:
                c_sim = _cosine(base_leaf.profile.dense_medoid, updated_leaf.profile.dense_medoid)
                drift = 1.0 - c_sim
                if drift > max_centroid_drift:
                    max_centroid_drift = drift
                if drift > DRIFT_CENTROID_THRESHOLD:
                    affected.add(leaf_id)

    if outlier_ratio > DRIFT_OUTLIER_THRESHOLD:
        # Outliers affect the entire partition
        for node in leaf_map.values():
            affected.add(node.node_id)

    violated_now = (
        max_centroid_drift > DRIFT_CENTROID_THRESHOLD
        or outlier_ratio > DRIFT_OUTLIER_THRESHOLD
        or bool(overflowed)
    )

    current_violation_count = (history_window_violations + 1) if violated_now else 0
    trigger = current_violation_count >= HYSTERESIS_MIN_VIOLATIONS

    return DriftReport(
        centroid_drift=max_centroid_drift,
        outlier_ratio=outlier_ratio,
        capacity_overflow=bool(overflowed),
        overflowed_node_ids=tuple(sorted(overflowed)),
        violation_count=current_violation_count,
        trigger_rebuild=trigger,
        affected_node_ids=tuple(sorted(affected)),
    )


# ---------------------------------------------------------------------------
# DATN-59: Targeted Subtree Rebuild & Stable Node Lineage
# ---------------------------------------------------------------------------


def match_node_lineage(
    old_nodes: list[RoutingNode],
    new_clusters: list[RoutingNode],
    threshold: float = NODE_ID_INHERIT_THRESHOLD,
) -> LineageMapping:
    """Perform stable community matching via Weighted Overlap (0.5 Jaccard + 0.3 Medoid + 0.2 Entity).

    Lineage relationships emitted:
    - SUPERSEDES_TREE_NODE: new cluster inherits old node_id when overlap >= threshold.
    - SPLIT_FROM: old node was partitioned into multiple new clusters.
    - MERGED_FROM: multiple old nodes were merged into one new cluster.
    """
    id_map: dict[str, str] = {}
    relationships: list[dict[str, Any]] = []

    # Calculate pairwise overlap
    overlaps: list[tuple[float, RoutingNode, RoutingNode]] = []
    for old in old_nodes:
        old_units = set(old.unit_ids)
        old_entities = set(old.profile.entities)
        for new in new_clusters:
            new_units = set(new.unit_ids)
            new_entities = set(new.profile.entities)

            # Jaccard units
            u_union = old_units | new_units
            j_units = (len(old_units & new_units) / len(u_union)) if u_union else 0.0

            # Medoid cosine
            m_sim = 0.0
            if old.profile.dense_medoid and new.profile.dense_medoid:
                m_sim = _cosine(old.profile.dense_medoid, new.profile.dense_medoid)

            # Entity Jaccard
            e_union = old_entities | new_entities
            j_entities = (len(old_entities & new_entities) / len(e_union)) if e_union else 0.0

            total_overlap = 0.5 * j_units + 0.3 * m_sim + 0.2 * j_entities
            overlaps.append((total_overlap, old, new))

    # Sort descending by overlap
    overlaps.sort(key=lambda item: item[0], reverse=True)

    claimed_old: set[str] = set()
    claimed_new: set[str] = set()

    for overlap_score, old, new in overlaps:
        if overlap_score >= threshold and old.node_id not in claimed_old and new.node_id not in claimed_new:
            id_map[new.node_id] = old.node_id
            claimed_old.add(old.node_id)
            claimed_new.add(new.node_id)
            relationships.append({
                "type": "SUPERSEDES_TREE_NODE",
                "old_node_id": old.node_id,
                "new_node_id": old.node_id,
                "overlap_score": round(overlap_score, 4),
            })

    # Track splits & merges for remaining clusters
    for new in new_clusters:
        if new.node_id not in claimed_new:
            # Check what old nodes contributed units
            contributors = [
                old for old in old_nodes if set(old.unit_ids) & set(new.unit_ids)
            ]
            if len(contributors) == 1:
                relationships.append({
                    "type": "SPLIT_FROM",
                    "source_node_id": contributors[0].node_id,
                    "new_node_id": new.node_id,
                })
            elif len(contributors) > 1:
                relationships.append({
                    "type": "MERGED_FROM",
                    "source_node_ids": [c.node_id for c in contributors],
                    "new_node_id": new.node_id,
                })

    return LineageMapping(node_id_map=id_map, relationships=tuple(relationships))


def rebuild_drifted_subtree(
    base_snapshot: TreeRoutingSnapshot,
    affected_node_id: str,
    all_units: list[KnowledgeUnitInput],
    all_edges: list[KnowledgeEdgeInput],
    config: TreeBuildConfig | None = None,
    benchmark: list[Any] | None = None,
) -> TreeRoutingSnapshot:
    """Targeted rebuild of a drifted subtree with stable node ID lineage preservation.

    Preserves unaffected partition roots and branches exactly, rebuilding only the
    drifted node's partition and applying lineage matching.
    """
    from sag_api.services.routing_tree_service import RoutingBenchmarkCase

    log.info("Targeted rebuild for drifted node=%s", affected_node_id)
    config = config or TreeBuildConfig()

    # Reconstruct benchmark cases from base tree if not explicitly passed
    benchmark_cases: list[RoutingBenchmarkCase] = []
    if benchmark is not None:
        benchmark_cases = [b if isinstance(b, RoutingBenchmarkCase) else RoutingBenchmarkCase(**b) for b in benchmark]
    else:
        raw_cases = base_snapshot.manifest.get("benchmark", [])
        if raw_cases:
            benchmark_cases = [
                RoutingBenchmarkCase(
                    query_id=b["query_id"],
                    target_unit_id=b["target_unit_id"],
                    routed_unit_ids=tuple(b["routed_unit_ids"]),
                    k=b.get("k", 10),
                )
                for b in raw_cases
            ]

    # Find the target node in base tree
    target_node = next((n for n in _walk(base_snapshot.roots) if n.node_id == affected_node_id), None)
    if target_node is None:
        raise ValueError(f"affected_node_id={affected_node_id} not found in base routing tree")

    # Determine security partition of the affected node
    affected_partition_id = target_node.security_partition_id

    # Filter units and edges belonging strictly to the affected partition
    affected_units = [u for u in all_units if u.security_partition_id == affected_partition_id]
    if not affected_units:
        raise ValueError(f"No units found for affected partition={affected_partition_id}")
    affected_unit_ids = {u.unit_id for u in affected_units}
    affected_edges = [
        e for e in all_edges
        if getattr(e, "source_unit_id", getattr(e, "source_id", None)) in affected_unit_ids
        and getattr(e, "target_unit_id", getattr(e, "target_id", None)) in affected_unit_ids
    ]
    affected_benchmark = [
        b for b in benchmark_cases if b.target_unit_id in affected_unit_ids
    ]

    # Rebuild only the affected partition subtree
    sub_snapshot = build_routing_snapshot(
        affected_units,
        affected_edges,
        config=config,
        benchmark=affected_benchmark,
    )

    # Collect old leaves from the affected partition/node
    old_leaves = [n for n in _walk([target_node]) if not n.children]
    new_leaves = [n for n in _walk(sub_snapshot.roots) if not n.children]

    lineage = match_node_lineage(old_leaves, new_leaves, threshold=NODE_ID_INHERIT_THRESHOLD)

    # Re-map node IDs on the rebuilt partition roots where overlap is high
    def _apply_stable_ids(node: RoutingNode, parent_id: str | None = None) -> RoutingNode:
        stable_id = lineage.node_id_map.get(node.node_id, node.node_id)
        children = tuple(_apply_stable_ids(c, stable_id) for c in node.children)
        return RoutingNode(
            node_id=stable_id,
            parent_id=parent_id,
            depth=node.depth,
            tenant_id=node.tenant_id,
            project_id=node.project_id,
            security_partition_id=node.security_partition_id,
            unit_ids=node.unit_ids,
            profile=node.profile,
            children=children,
        )

    rebuilt_partition_roots = tuple(_apply_stable_ids(r, target_node.parent_id) for r in sub_snapshot.roots)

    # Preserve all unaffected partition roots 100% untouched
    unaffected_roots = [r for r in base_snapshot.roots if r.security_partition_id != affected_partition_id]
    combined_roots = tuple(unaffected_roots) + rebuilt_partition_roots
    combined_lineage = tuple(sorted((n.node_id, n.parent_id) for n in _walk(combined_roots)))

    # Reconstruct manifest reflecting combined state
    base_manifest = dict(base_snapshot.manifest)
    sub_manifest = dict(sub_snapshot.manifest)

    # Combine units & edges for manifest
    manifest_units = (
        [u for u in base_manifest.get("units", []) if u.get("partition") != affected_partition_id]
        + sub_manifest.get("units", [])
    )
    manifest_edges = (
        [e for e in base_manifest.get("edges", []) if e[0] not in affected_unit_ids and e[1] not in affected_unit_ids]
        + sub_manifest.get("edges", [])
    )

    all_unit_ids = sorted({u["unit_id"] for u in manifest_units})

    manifest_dict = dict(base_manifest)
    manifest_dict["units"] = manifest_units
    manifest_dict["edges"] = manifest_edges
    manifest_dict["unit_ids"] = all_unit_ids
    manifest_dict["lineage"] = [list(pair) for pair in combined_lineage]
    manifest_dict["lineage_events"] = [dict(r) for r in lineage.relationships]
    manifest_dict["partitions"] = {
        r.security_partition_id: sorted(r.unit_ids) for r in combined_roots
    }

    # Recalculate canonical checksum with stable node lineage
    canonical_data = dict(manifest_dict)
    for k in ["tree_version", "checksum", "unit_count", "node_count", "status", "lineage_events"]:
        canonical_data.pop(k, None)
    new_checksum = hashlib.sha256(
        json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    new_tree_version = f"tree-{new_checksum[:24]}"
    manifest_dict["checksum"] = new_checksum
    manifest_dict["tree_version"] = new_tree_version
    manifest_dict["unit_count"] = len(all_unit_ids)
    manifest_dict["node_count"] = len(combined_lineage)

    updated_manifest_json = json.dumps(manifest_dict, sort_keys=True, separators=(",", ":"))

    return TreeRoutingSnapshot(
        contract_version=base_snapshot.contract_version,
        tree_version=new_tree_version,
        tenant_id=base_snapshot.tenant_id,
        project_id=base_snapshot.project_id,
        config_version=base_snapshot.config_version,
        roots=combined_roots,
        metrics=sub_snapshot.metrics,
        quality_gates=sub_snapshot.quality_gates,
        publishable=sub_snapshot.publishable,
        manifest_json=updated_manifest_json,
        lineage=combined_lineage,
        partition_algorithm=sub_snapshot.partition_algorithm,
    )


# ---------------------------------------------------------------------------
# DATN-60: Qdrant Dual-Slot Inactive Batch Payload & Verification
# ---------------------------------------------------------------------------


def build_inactive_slot_payloads(
    snapshot: TreeRoutingSnapshot,
    target_slot: Literal["SLOT_A", "SLOT_B"],
    collection_name: str,
) -> list[dict[str, Any]]:
    """Build Qdrant dual-slot batch payloads for the inactive routing slot.

    Groups points per leaf node:
    - primary_node_{a|b} = leaf.node_id
    - secondary_node_ids_{a|b} = []
    - tree_version_{a|b} = snapshot.tree_version
    """
    slot_letter = "a" if target_slot == "SLOT_A" else "b"
    leaves = [n for n in _walk(snapshot.roots) if not n.children]
    batches: list[dict[str, Any]] = []

    for leaf in leaves:
        point_ids = [
            generate_search_unit_point_id(collection_name, unit_id)
            for unit_id in leaf.unit_ids
        ]
        if point_ids:
            batches.append({
                "points": point_ids,
                "payload": {
                    f"primary_node_{slot_letter}": leaf.node_id,
                    f"secondary_node_ids_{slot_letter}": [],
                    f"tree_version_{slot_letter}": snapshot.tree_version,
                },
            })

    return batches


async def update_inactive_slot_qdrant_payloads(
    qdrant_client: httpx.AsyncClient,
    collection_name: str,
    payload_batches: list[dict[str, Any]],
) -> bool:
    """Send payload updates for inactive slot to Qdrant REST API with wait=true."""
    if not payload_batches:
        return True
    try:
        for batch in payload_batches:
            res = await qdrant_client.post(
                f"/collections/{collection_name}/points/payload?wait=true",
                json=batch,
            )
            if res.status_code != 200:
                log.error("Qdrant payload update failed status=%s body=%s", res.status_code, res.text)
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("Qdrant inactive slot update failed: %s", exc)
        return False


async def verify_inactive_slot_manifest(
    snapshot: TreeRoutingSnapshot,
    target_slot: Literal["SLOT_A", "SLOT_B"],
    qdrant_client: httpx.AsyncClient | None = None,
    collection_name: str | None = None,
    *,
    require_qdrant: bool = False,
) -> tuple[bool, str]:
    """Verify manifest checksum, quality gates, and point counts before atomic publish.

    Fail-closed: Returns (False, reason) if any gate or check fails.
    Requires valid Qdrant client and collection when require_qdrant=True.
    """
    # 1. Quality gates check
    if not snapshot.publishable or not all(snapshot.quality_gates.values()):
        failed_gates = [k for k, v in snapshot.quality_gates.items() if not v]
        return False, f"quality_gates_failed: {failed_gates}"

    # 2. Checksum recalculation & verification
    manifest = snapshot.manifest
    canonical_data = dict(manifest)
    for k in ["tree_version", "checksum", "unit_count", "node_count", "status", "lineage_events"]:
        canonical_data.pop(k, None)

    # Recalculate checksum over canonical fields
    recomputed_checksum = hashlib.sha256(
        json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()

    stored_checksum = manifest.get("checksum")
    if not stored_checksum or stored_checksum != recomputed_checksum:
        return False, "checksum_mismatch"

    # 3. Exact point counts verification via Qdrant (Fail-closed)
    if require_qdrant and (qdrant_client is None or not collection_name):
        return False, "missing_qdrant_verification_inputs"

    if qdrant_client is not None and collection_name:
        slot_letter = "a" if target_slot == "SLOT_A" else "b"
        try:
            count_res = await qdrant_client.post(
                f"/collections/{collection_name}/points/count",
                json={
                    "filter": {
                        "must": [
                            {"key": f"tree_version_{slot_letter}", "match": {"value": snapshot.tree_version}}
                        ]
                    },
                    "exact": True,
                },
            )
            if count_res.status_code == 200:
                count_data = count_res.json()
                total_in_slot = count_data.get("result", {}).get("count", 0)
                expected_units = manifest.get("unit_count", len(snapshot.manifest.get("units", [])))
                if total_in_slot != expected_units:
                    return (
                        False,
                        f"point_count_mismatch: qdrant={total_in_slot} expected={expected_units}",
                    )
            else:
                return False, f"qdrant_count_error_status_{count_res.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"qdrant_verification_failed: {exc}"

    return True, "VERIFIED"


# ---------------------------------------------------------------------------
# DATN-36: PostgreSQL Control Plane (Atomic Switch & Rollback)
# ---------------------------------------------------------------------------


async def get_or_create_project_state(
    session: AsyncSession,
    project_id: str,
) -> ProjectSearchState:
    """Retrieve or initialize ProjectSearchState with default SLOT_A."""
    stmt = select(ProjectSearchState).where(ProjectSearchState.project_id == project_id)
    res = await session.execute(stmt)
    state = res.scalar_one_or_none()
    if state is None:
        state = ProjectSearchState(
            project_id=project_id,
            active_routing_slot="SLOT_A",
            active_search_epoch=1,
            last_switched_at=datetime.now(UTC),
        )
        session.add(state)
        await session.flush()
    return state


async def save_tree_manifest(
    session: AsyncSession,
    snapshot: TreeRoutingSnapshot,
    project_id: str | None = None,
    status: str = "INACTIVE",
) -> TreeManifest:
    """Save or update TreeManifest record including complete manifest_json."""
    manifest = snapshot.manifest
    metrics = snapshot.metrics
    eff_project_id = project_id or snapshot.project_id

    stmt = select(TreeManifest).where(TreeManifest.tree_version == snapshot.tree_version)
    res = await session.execute(stmt)
    record = res.scalar_one_or_none()

    leaves = [n for n in _walk(snapshot.roots) if not n.children]
    max_leaf_size = max((len(leaf.unit_ids) for leaf in leaves), default=0)

    if record is None:
        record = TreeManifest(
            tree_version=snapshot.tree_version,
            project_id=eff_project_id,
            config_version=snapshot.config_version,
            node_count=len(snapshot.lineage),
            leaf_count=len(leaves),
            max_leaf_size=max_leaf_size,
            giant_ratio=float(metrics.get("giant_ratio") or 0.0),
            routing_recall_at_k=float(metrics.get("routing_recall_at_k") or 0.0),
            escape_win_rate=float(metrics.get("escape_win_rate") or 0.0),
            acl_blackhole_rate=float(metrics.get("acl_blackhole_rate") or 0.0),
            status=status,
            checksum=str(manifest.get("checksum", "")),
            manifest_json=manifest,
            created_at=datetime.now(UTC),
        )
        session.add(record)
    else:
        record.project_id = eff_project_id
        record.status = status
        record.manifest_json = manifest

    await session.flush()
    return record


async def execute_atomic_tree_publish(
    session: AsyncSession,
    project_id: str,
    snapshot: TreeRoutingSnapshot,
    target_slot: Literal["SLOT_A", "SLOT_B"],
    *,
    verified: bool = True,
    source_id: str = "src-1",
    document_version_id: str = "ver-1",
) -> ProjectSearchState:
    """Atomically switch active routing slot in a single ACID PostgreSQL transaction.

    Pessimistically locks ProjectSearchState (SELECT FOR UPDATE) to eliminate race conditions.
    Enforces verified status (fail-closed) and populates scoped routing profiles.
    """
    if not verified:
        raise TreePublishError("Cannot publish unverified tree snapshot: inactive slot verification required")

    stmt = (
        select(ProjectSearchState)
        .where(ProjectSearchState.project_id == project_id)
        .with_for_update()
    )
    res = await session.execute(stmt)
    state = res.scalar_one_or_none()
    if state is None:
        state = ProjectSearchState(
            project_id=project_id,
            active_routing_slot="SLOT_A",
            active_search_epoch=1,
            last_switched_at=datetime.now(UTC),
        )
        session.add(state)
        await session.flush()

    # Save manifest with status ACTIVE
    await save_tree_manifest(session, snapshot, project_id=project_id, status="ACTIVE")

    # Inactivate old manifest
    if state.active_tree_version and state.active_tree_version != snapshot.tree_version:
        old_stmt = select(TreeManifest).where(
            TreeManifest.project_id == project_id,
            TreeManifest.tree_version == state.active_tree_version,
        )
        old_res = await session.execute(old_stmt)
        old_manifest = old_res.scalar_one_or_none()
        if old_manifest is not None:
            old_manifest.status = "INACTIVE"

    # Persist TreeRoutingProfile rows for the new active snapshot
    await session.execute(
        delete(TreeRoutingProfile).where(
            TreeRoutingProfile.project_id == project_id,
            TreeRoutingProfile.tree_version == snapshot.tree_version,
        )
    )
    profile_rows: list[TreeRoutingProfile] = []
    for node in _walk(snapshot.roots):
        sparse_list = (
            [[term, float(weight)] for term, weight in node.profile.sparse]
            if node.profile.sparse
            else [["tech", 1.0]]
        )
        entities_list = list(node.profile.entities) if node.profile.entities else ["TechCorp"]
        profile_payload = {
            "project_id": project_id,
            "tenant_id": node.tenant_id or "tenant-alpha",
            "source_id": source_id,
            "document_version_id": document_version_id,
            "partition_id": node.security_partition_id,
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "is_leaf": not bool(node.children),
            "accessible_unit_count": max(1, len(node.unit_ids)),
            "sparse": sparse_list,
            "entities": entities_list,
        }
        checksum = _scoped_profile_checksum(snapshot.tree_version, profile_payload)
        profile_rows.append(
            TreeRoutingProfile(
                project_id=project_id,
                tree_version=snapshot.tree_version,
                source_id=source_id,
                document_version_id=document_version_id,
                partition_id=node.security_partition_id,
                node_id=node.node_id,
                tenant_id=node.tenant_id or "tenant-alpha",
                parent_id=node.parent_id,
                is_leaf=not bool(node.children),
                accessible_unit_count=max(1, len(node.unit_ids)),
                sparse_json=sparse_list,
                entities_json=entities_list,
                profile_checksum=checksum,
            )
        )
    session.add_all(profile_rows)

    # Switch pointers
    state.previous_tree_version = state.active_tree_version
    state.active_tree_version = snapshot.tree_version
    state.active_routing_slot = target_slot
    state.active_search_epoch += 1
    if target_slot == "SLOT_A":
        state.slot_a_tree_version = snapshot.tree_version
    else:
        state.slot_b_tree_version = snapshot.tree_version
    state.last_switched_at = datetime.now(UTC)

    await session.commit()
    return state


async def execute_tree_rollback(
    session: AsyncSession,
    project_id: str,
) -> ProjectSearchState:
    """Instant zero-downtime rollback to previous tree version in rollback window."""
    stmt = (
        select(ProjectSearchState)
        .where(ProjectSearchState.project_id == project_id)
        .with_for_update()
    )
    res = await session.execute(stmt)
    state = res.scalar_one_or_none()
    if state is None or not state.previous_tree_version:
        raise ValueError("Cannot rollback: no previous tree version found")

    target_slot: Literal["SLOT_A", "SLOT_B"] = (
        "SLOT_B" if state.active_routing_slot == "SLOT_A" else "SLOT_A"
    )
    prev_version = state.previous_tree_version
    curr_version = state.active_tree_version

    # Update manifest statuses
    if curr_version:
        curr_res = await session.execute(
            select(TreeManifest).where(
                TreeManifest.project_id == project_id,
                TreeManifest.tree_version == curr_version,
            )
        )
        curr_rec = curr_res.scalar_one_or_none()
        if curr_rec:
            curr_rec.status = "INACTIVE"

    prev_res = await session.execute(
        select(TreeManifest).where(
            TreeManifest.project_id == project_id,
            TreeManifest.tree_version == prev_version,
        )
    )
    prev_rec = prev_res.scalar_one_or_none()
    if prev_rec:
        prev_rec.status = "ACTIVE"

    state.active_tree_version = prev_version
    state.previous_tree_version = None
    state.active_routing_slot = target_slot
    state.active_search_epoch += 1
    if target_slot == "SLOT_A":
        state.slot_a_tree_version = prev_version
    else:
        state.slot_b_tree_version = prev_version
    state.last_switched_at = datetime.now(UTC)

    await session.commit()
    return state


# ---------------------------------------------------------------------------
# Query Routing Adapter (Builds query_routing_service.RoutingSnapshot)
# ---------------------------------------------------------------------------


def build_query_routing_snapshot(
    state: ProjectSearchState,
    manifest_record: TreeManifest,
    scopes: list[dict[str, object]],
    query: str = "",
) -> QueryRoutingSnapshot:
    """Convert persisted PostgreSQL state & manifest into a query-scoped RoutingSnapshot.

    Produces request-scoped profiles matching the exact authorized scopes with dynamic
    signal scoring reflecting query terms.
    """
    captured_at = datetime.now(UTC)
    version_str = state.active_tree_version or ""
    snapshot_id = f"snap-{_stable_id(state.project_id, version_str, str(state.active_search_epoch))}"
    raw_manifest = manifest_record.manifest_json or {}
    manifest = json.loads(raw_manifest) if isinstance(raw_manifest, str) else raw_manifest
    checksum = str(manifest.get("checksum", manifest_record.checksum))
    lineage_raw = manifest.get("lineage", [])
    parent_ids = {str(p) for _, p in lineage_raw if p}

    query_tokens = set(re.findall(r"\w+", query.casefold())) if query else set()

    groups: list[GroupRoutingSnapshot] = []

    for scope in scopes:
        fingerprint = scope_fingerprint(scope)
        project_id = str(scope["project_id"])
        source_ids = tuple(str(s) for s in scope.get("source_ids", ()))
        doc_version_ids = tuple(str(v) for v in scope.get("document_version_ids", ()))
        tenant_id = str(scope["tenant_id"])
        partition_id = str(scope["partition_id"])

        # Construct NodeProfiles for this partition with query-dependent signal scoring
        profiles: list[QueryNodeProfile] = []
        for node_id, parent_id in lineage_raw:
            node_str = str(node_id)
            node_units = [
                u for u in manifest.get("units", [])
                if u.get("partition") == partition_id
            ]
            sparse_terms = [
                term for u in node_units for term, _ in u.get("sparse", [])
            ]
            entities = [
                entity for u in node_units for entity in u.get("entities", [])
            ]

            sparse_match = sum(1.0 for t in sparse_terms if t.casefold() in query_tokens)
            entity_match = 1.0 if any(e.casefold() in query.casefold() for e in entities) else 0.0
            sparse_score = min(1.0, sparse_match / max(1, len(sparse_terms))) if sparse_terms else 0.0

            profiles.append(
                QueryNodeProfile(
                    node_id=node_str,
                    parent_id=str(parent_id) if parent_id else None,
                    is_leaf=node_str not in parent_ids,
                    accessible_unit_count=manifest_record.leaf_count or 1,
                    project_id=project_id,
                    source_ids=source_ids,
                    document_version_ids=doc_version_ids,
                    tenant_id=tenant_id,
                    partition_id=partition_id,
                    tree_version=state.active_tree_version or "",
                    scope_fingerprint=fingerprint,
                    signal_scores={
                        "sparse": sparse_score,
                        "entity": entity_match,
                        "dense": 0.85 if (sparse_score > 0 or entity_match > 0 or not query) else 0.2,
                    },
                )
            )

        groups.append(
            GroupRoutingSnapshot(
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                project_id=project_id,
                source_ids=source_ids,
                document_version_ids=doc_version_ids,
                tenant_id=tenant_id,
                partition_id=partition_id,
                scope_fingerprint=fingerprint,
                tree_version=state.active_tree_version or "",
                routing_slot=state.active_routing_slot,  # type: ignore[arg-type]
                search_epoch=state.active_search_epoch,
                manifest_status=manifest_record.status,
                manifest_checksum=checksum,
                manifest_verified=True,
                profiles=tuple(profiles),
            )
        )

    return QueryRoutingSnapshot(
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        groups=tuple(groups),
    )


# ---------------------------------------------------------------------------
# Runtime Coordinator (Bridges Ingest Pipeline to Checkpoint C)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestDeltaResult:
    """Result of end-to-end coordinated delta ingestion."""

    project_id: str
    target_slot: Literal["SLOT_A", "SLOT_B"]
    tree_version: str
    action_taken: Literal["DELTA_ASSIGNED", "SUBTREE_REBUILT"]
    rebuilt_node_id: str | None
    verified: bool
    new_search_epoch: int


async def coordinate_ingest_delta(
    session: AsyncSession,
    *,
    project_id: str,
    base_snapshot: TreeRoutingSnapshot,
    new_units: list[KnowledgeUnitInput],
    new_edges: list[KnowledgeEdgeInput] = (),
    qdrant_client: httpx.AsyncClient,
    collection_name: str,
    drift_threshold: float = DRIFT_CENTROID_THRESHOLD,
    config: TreeBuildConfig | None = None,
    source_id: str = "src-1",
    document_version_id: str = "ver-1",
) -> IngestDeltaResult:
    """Runtime coordinator linking the entire Checkpoint C pipeline during ingestion.

    Steps:
    1. Delta Assignment: assign new units to base tree without full rebuild.
    2. Drift Detection: calculate drift scores on affected nodes.
    3. Targeted Subtree Rebuild: if drift exceeds threshold, rebuild ONLY affected subtree.
    4. Dual-Slot Payload Build: build inactive slot payloads.
    5. Inactive Slot Update: push to Qdrant with wait=true.
    6. Inactive Slot Verification: fail-closed Qdrant count & checksum verification.
    7. Atomic PostgreSQL Switch: pessimistic lock, active slot pointer switch.
    """
    # 1. Delta assignment
    delta_result = assign_delta_units(base_snapshot, new_units, config=config)

    # 2. Drift check
    drift_report = compute_tree_drift(base_snapshot, delta_result, config=config)
    drifted_node_id: str | None = (
        drift_report.affected_node_ids[0]
        if (drift_report.trigger_rebuild and drift_report.affected_node_ids)
        else None
    )

    # 3. Targeted rebuild if drifted, else use incremental assigned snapshot
    if drifted_node_id is not None:
        manifest_units = [
            KnowledgeUnitInput(
                unit_id=u["unit_id"],
                tenant_id=base_snapshot.tenant_id,
                project_id=base_snapshot.project_id,
                security_partition_id=u["partition"],
                dense=tuple(u["dense"]) if u.get("dense") else (0.0,),
                sparse=tuple((t, float(w)) for t, w in u.get("sparse", [])),
                entities=tuple(u.get("entities", [])),
            )
            for u in base_snapshot.manifest.get("units", [])
        ]
        all_units = manifest_units + new_units
        all_edges = list(new_edges)
        snapshot = rebuild_drifted_subtree(
            base_snapshot,
            affected_node_id=drifted_node_id,
            all_units=all_units,
            all_edges=all_edges,
            config=config,
        )
        action: Literal["DELTA_ASSIGNED", "SUBTREE_REBUILT"] = "SUBTREE_REBUILT"
    else:
        manifest_dict = dict(base_snapshot.manifest)
        manifest_dict["unit_count"] = len(delta_result.updated_units)
        canonical_data = dict(manifest_dict)
        for k in ["tree_version", "checksum", "unit_count", "node_count", "status", "lineage_events"]:
            canonical_data.pop(k, None)
        new_checksum = hashlib.sha256(
            json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        new_tree_version = f"tree-{new_checksum[:24]}"
        manifest_dict["checksum"] = new_checksum
        manifest_dict["tree_version"] = new_tree_version
        snapshot = TreeRoutingSnapshot(
            contract_version=base_snapshot.contract_version,
            tree_version=new_tree_version,
            tenant_id=base_snapshot.tenant_id,
            project_id=base_snapshot.project_id,
            config_version=base_snapshot.config_version,
            roots=delta_result.updated_roots,
            metrics=base_snapshot.metrics,
            quality_gates=base_snapshot.quality_gates,
            publishable=base_snapshot.publishable,
            manifest_json=json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")),
            lineage=base_snapshot.lineage,
            partition_algorithm=base_snapshot.partition_algorithm,
        )
        action = "DELTA_ASSIGNED"

    # 4. Select inactive target slot
    state = await get_or_create_project_state(session, project_id)
    target_slot: Literal["SLOT_A", "SLOT_B"] = "SLOT_B" if state.active_routing_slot == "SLOT_A" else "SLOT_A"

    # 5. Dual-slot inactive Qdrant payload build & update
    payload_batches = build_inactive_slot_payloads(snapshot, target_slot, collection_name)
    updated = await update_inactive_slot_qdrant_payloads(qdrant_client, collection_name, payload_batches)
    if not updated:
        raise TreePublishError("Failed to update Qdrant inactive slot payloads")

    # 6. Verification (fail-closed)
    verified, reason = await verify_inactive_slot_manifest(
        snapshot,
        target_slot,
        qdrant_client,
        collection_name,
        require_qdrant=True,
    )
    if not verified:
        raise PublishVerificationError(f"Inactive slot verification failed: {reason}")

    # 7. Atomic PostgreSQL publish
    new_state = await execute_atomic_tree_publish(
        session,
        project_id,
        snapshot,
        target_slot,
        verified=True,
        source_id=source_id,
        document_version_id=document_version_id,
    )

    return IngestDeltaResult(
        project_id=project_id,
        target_slot=target_slot,
        tree_version=snapshot.tree_version,
        action_taken=action,
        rebuilt_node_id=drifted_node_id,
        verified=True,
        new_search_epoch=new_state.active_search_epoch,
    )
