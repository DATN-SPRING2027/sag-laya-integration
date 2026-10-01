"""Dedup & Temporal Service for SAG Routing RAG (Phase 2B).

Follows Ponytail minimalism:
- Pure Python/stdlib shingle Jaccard for near-duplicate candidate clustering.
- Strict non-auto-merge for CONTRADICTS / SUPERSEDES (candidates only).
- Multi-timestamp tracking (published, observed, ingested, valid_from/to).
- Out-of-order ingestion protection against overwriting newer facts with older data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Sequence
import uuid

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models.routing_rag import (
    CanonicalBlock,
    DocumentVersion,
    KnowledgeGraphEdge,
    SearchUnit,
    StageRun,
)

# Standard edge relation taxonomy for Phase 2B
RELATION_EQUIVALENT = "EQUIVALENT"
RELATION_SUPPORTS = "SUPPORTS"
RELATION_CONTRADICTS = "CONTRADICTS"
RELATION_SUPERSEDES = "SUPERSEDES"
RELATION_RELATED = "RELATED"

VALID_RELATIONS = {
    RELATION_EQUIVALENT,
    RELATION_SUPPORTS,
    RELATION_CONTRADICTS,
    RELATION_SUPERSEDES,
    RELATION_RELATED,
}


def compute_text_similarity(text1: str, text2: str) -> float:
    """Compute lexical Jaccard similarity using word tokens (stdlib)."""
    t1 = set(text1.strip().lower().split())
    t2 = set(text2.strip().lower().split())
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


def classify_relation_candidate(
    sim_score: float,
    is_contradiction: bool = False,
    is_supersede: bool = False,
    is_support: bool = False,
) -> str:
    """Map similarity and semantic signals to one of the 5 canonical relation types.

    Never auto-merge CONTRADICTS or SUPERSEDES.
    """
    if is_supersede:
        return RELATION_SUPERSEDES
    if is_contradiction:
        return RELATION_CONTRADICTS
    if is_support:
        return RELATION_SUPPORTS
    if sim_score >= 0.85:
        return RELATION_EQUIVALENT
    if sim_score >= 0.50:
        return RELATION_RELATED
    return RELATION_RELATED


async def register_relation_candidate(
    session: AsyncSession,
    *,
    project_id: str,
    source_unit_id: str,
    target_unit_id: str,
    edge_type: str,
    weight: float,
    evidence_mapping: dict[str, Any] | None = None,
) -> KnowledgeGraphEdge:
    """Record a semantic edge candidate.

    Enforces that CONTRADICTS and SUPERSEDES relations remain isolated candidates
    with provenance evidence, without destructive auto-merge.
    """
    if edge_type not in VALID_RELATIONS:
        raise ValueError(f"Invalid relation type: {edge_type}. Must be one of {VALID_RELATIONS}")

    metadata = evidence_mapping or {}
    metadata.setdefault("is_candidate", True)
    metadata.setdefault("auto_merged", False)

    # In case of CONTRADICTS / SUPERSEDES, record strict audit trail
    if edge_type in {RELATION_CONTRADICTS, RELATION_SUPERSEDES}:
        metadata["requires_human_or_eval_resolution"] = True

    edge = KnowledgeGraphEdge(
        id=str(uuid.uuid4()),
        project_id=project_id,
        source_unit_id=source_unit_id,
        target_unit_id=target_unit_id,
        edge_type=edge_type,
        weight=weight,
        calibrated_weight=weight,
        metadata_json=metadata,
    )
    session.add(edge)
    return edge


async def resolve_temporal_supersedes(
    session: AsyncSession,
    *,
    current_version: DocumentVersion,
    document_id: str,
) -> dict[str, Any]:
    """Link versions along temporal axes (published, observed, ingested).

    Guarantees out-of-order protection:
    If current_version has an older published timestamp than the active version,
    it is marked as historical and does not overwrite active validity.
    """
    # Find existing versions of the document
    stmt = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.id != current_version.id,
        )
        .order_by(desc(DocumentVersion.version_no))
    )
    res = await session.execute(stmt)
    previous_versions = res.scalars().all()

    if not previous_versions:
        # First version: valid from observed_at to eternity
        current_version.valid_from = current_version.observed_at
        current_version.valid_to = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
        return {"action": "INITIAL", "active_version_id": current_version.id}

    latest_prev = previous_versions[0]

    # Out-of-order detection: check source_published_at
    if (
        current_version.source_published_at
        and latest_prev.source_published_at
        and current_version.source_published_at < latest_prev.source_published_at
    ):
        # Out-of-order ingestion: this is older knowledge arriving late!
        # Do not overwrite latest_prev!
        current_version.valid_from = current_version.source_published_at
        current_version.valid_to = latest_prev.source_published_at
        current_version.supersedes_id = None
        return {
            "action": "OUT_OF_ORDER_ARCHIVED",
            "active_version_id": latest_prev.id,
            "archived_version_id": current_version.id,
        }

    # Normal case: current is newer, so it supersedes latest_prev
    current_version.supersedes_id = latest_prev.id
    current_version.valid_from = current_version.observed_at
    current_version.valid_to = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    # Invalidate previous version
    latest_prev.valid_to = current_version.observed_at
    session.add(latest_prev)

    return {
        "action": "SUPERSEDED",
        "active_version_id": current_version.id,
        "superseded_version_id": latest_prev.id,
    }


async def run_dedup_and_temporal_stage(
    session: AsyncSession,
    *,
    document_version: DocumentVersion,
    document_id: str,
    project_id: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Execute Phase 2B stage with StageRun audit trail."""
    start_time = datetime.now(UTC)

    temporal_result = await resolve_temporal_supersedes(
        session,
        current_version=document_version,
        document_id=document_id,
    )

    duration_ms = (datetime.now(UTC) - start_time).total_seconds() * 1000.0

    if run_id:
        stage_run = StageRun(
            id=str(uuid.uuid4()),
            run_id=run_id,
            stage="DEDUP_TEMPORAL",
            status="SUCCESS",
            duration_ms=duration_ms,
            metrics_json={
                "temporal_action": temporal_result["action"],
                "active_version_id": temporal_result["active_version_id"],
            },
        )
        session.add(stage_run)

    return temporal_result
