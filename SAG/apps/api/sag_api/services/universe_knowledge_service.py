"""Read-only, ACL-scoped presentation of published Knowledge Routing snapshots."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.errors import ConflictError, NotFoundError, ServiceUnavailableError, ValidationError
from sag_api.core.principal_assertion import VerifiedPrincipal
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    KnowledgeEvidence,
    KnowledgeUnit,
    ProjectSearchState,
    Source,
    SourceProjectMapping,
    TreeManifest,
)
from sag_api.sag.engine_manager import _decode_universe_cursor, _encode_universe_cursor
from sag_api.schemas.universe import (
    UniverseKnowledgeDetailOut,
    UniverseKnowledgeEdgeOut,
    UniverseKnowledgeEvidenceOut,
    UniverseKnowledgeMatchOut,
    UniverseKnowledgePageOut,
    UniverseKnowledgeReferenceIn,
    UniverseKnowledgeUnitOut,
    UniverseTreeOut,
)
from sag_api.services.knowledge_service import checksum, unit_checksum
from sag_api.services.search_unit_retrieval_service import _load_current_ready_versions
from sag_api.services.tree_publish_service import PublishVerificationError, _checksum_manifest


@dataclass
class KnowledgeUniverseSnapshot:
    tree: UniverseTreeOut
    tenant_id: str
    scope_key: str
    scopes: set[tuple[str, str, str]]
    members: dict[str, set[str]]
    assignments: list[dict[str, Any]]
    edges: list[tuple[str, str, float]]

    @property
    def unit_ids(self) -> set[str]:
        return set().union(*self.members.values())


def _snapshot_memberships(
    raw: dict[str, Any],
    tree: UniverseTreeOut,
    tenant_id: str,
    scopes: set[tuple[str, str, str]],
) -> tuple[dict[str, set[str]], list[dict[str, Any]], list[tuple[str, str, float]]]:
    profiles = raw.get("routing_profiles")
    assignments = raw.get("search_unit_assignments")
    if not isinstance(profiles, list) or not isinstance(assignments, list):
        raise ServiceUnavailableError("The active tree has no published knowledge membership")
    allowed_nodes = {node.id for node in tree.nodes}
    allowed_assignments = []
    allowed_units: set[str] = set()
    for item in assignments:
        if not isinstance(item, dict):
            raise ServiceUnavailableError("Invalid published knowledge assignment")
        scope = (item.get("source_id"), item.get("document_version_id"), item.get("partition_id"))
        if scope not in scopes:
            continue
        ids = item.get("knowledge_unit_ids")
        if (
            not isinstance(ids, list)
            or not all(isinstance(value, str) and value for value in ids)
            or not isinstance(item.get("search_unit_id"), str)
        ):
            raise ServiceUnavailableError("Invalid published knowledge assignment")
        allowed_assignments.append(item)
        allowed_units.update(ids)
    members = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ServiceUnavailableError("Invalid published knowledge profile")
        node_id = profile.get("node_id")
        if node_id not in allowed_nodes:
            continue
        ids = profile.get("unit_ids")
        if (
            profile.get("tenant_id") != tenant_id
            or profile.get("project_id") != tree.project_id
            or profile.get("partition_id") not in {scope[2] for scope in scopes}
            or not isinstance(ids, list)
            or not all(isinstance(value, str) and value for value in ids)
            or node_id in members
        ):
            raise ServiceUnavailableError("Published knowledge membership failed scope validation")
        members[node_id] = set(ids) & allowed_units
    if set(members) != allowed_nodes:
        raise ServiceUnavailableError("Published knowledge membership is incomplete")
    visible = set().union(*members.values())
    edges = []
    for edge in raw.get("edges", []):
        if (
            not isinstance(edge, (list, tuple))
            or len(edge) != 3
            or not isinstance(edge[0], str)
            or not isinstance(edge[1], str)
            or isinstance(edge[2], bool)
            or not isinstance(edge[2], (float, int))
            or not math.isfinite(edge[2])
            or not 0 <= edge[2] <= 1
        ):
            raise ServiceUnavailableError("Invalid published knowledge edge")
        if edge[0] in visible and edge[1] in visible:
            edges.append((edge[0], edge[1], float(edge[2])))
    return members, allowed_assignments, edges


async def load_knowledge_universe_snapshot(
    session: AsyncSession,
    principal: VerifiedPrincipal,
    tree: UniverseTreeOut,
) -> KnowledgeUniverseSnapshot:
    if tree.project_id not in principal.allowed_project_ids or not principal.tenant_id:
        raise NotFoundError("Knowledge project is unavailable")
    row = (
        await session.execute(
            select(TreeManifest.manifest_json, TreeManifest.checksum)
            .join(ProjectSearchState, ProjectSearchState.active_tree_version == TreeManifest.tree_version)
            .where(
                ProjectSearchState.project_id == tree.project_id,
                ProjectSearchState.active_search_epoch == tree.search_epoch,
                TreeManifest.project_id == tree.project_id,
                TreeManifest.tree_version == tree.tree_version,
                TreeManifest.status == "ACTIVE",
            )
            .with_for_update(read=True, of=ProjectSearchState)
        )
    ).one_or_none()
    if row is None:
        raise ConflictError("The knowledge snapshot changed; refresh the universe")
    raw, digest = row
    try:
        verified_digest = _checksum_manifest(raw) if isinstance(raw, dict) else None
    except PublishVerificationError as error:
        raise ServiceUnavailableError("Published knowledge snapshot is not canonical JSON") from error
    if (
        not isinstance(raw, dict)
        or not isinstance(digest, str)
        or raw.get("tenant_id") != principal.tenant_id
        or raw.get("project_id") != tree.project_id
        or verified_digest != digest
        or tree.tree_version != f"tree-{digest[:24]}"
    ):
        raise ServiceUnavailableError("Published knowledge snapshot failed integrity validation")
    sources = list(
        (
            await session.scalars(
                select(Source)
                .where(
                    Source.id.in_(select(SourceProjectMapping.source_id).where(
                        SourceProjectMapping.organization_id == principal.organization_id,
                        SourceProjectMapping.project_id == tree.project_id,
                        SourceProjectMapping.state == "CONFIRMED",
                    )),
                )
                .order_by(Source.id)
                .limit(settings.universe_manifest_source_limit + 1)
            )
        ).all()
    )
    if len(sources) > settings.universe_manifest_source_limit:
        raise ServiceUnavailableError("Universe exceeds the configured authorized source limit")
    versions = await _load_current_ready_versions(session, sources, principal)
    versions = {key: value for key, value in versions.items() if value.project_id == tree.project_id}
    scopes = {(value.source_id, key, value.partition_id) for key, value in versions.items()}
    scope_key = checksum(
        {
            "subject": principal.subject,
            "organization": principal.organization_id,
            "tenant": principal.tenant_id,
            "tree": tree.tree_version,
            "epoch": tree.search_epoch,
            "versions": sorted(
                (key, value.source_id, value.partition_id, value.acl_mapping_version,
                 value.ingestion_run_id, value.index_stage_run_id, value.search_manifest_checksum)
                for key, value in versions.items()
            ),
        }
    )
    members, assignments, edges = _snapshot_memberships(raw, tree, principal.tenant_id, scopes)
    return KnowledgeUniverseSnapshot(tree, principal.tenant_id, scope_key, scopes, members, assignments, edges)


def _unit_query(snapshot: KnowledgeUniverseSnapshot, ids: set[str] | None):
    now = datetime.now(UTC)
    return (
        select(KnowledgeUnit, Document, DocumentVersion, Source)
        .join(DocumentVersion, DocumentVersion.id == KnowledgeUnit.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            KnowledgeUnit.id.in_(ids) if ids is not None else True,
            KnowledgeUnit.tenant_id == snapshot.tenant_id,
            KnowledgeUnit.project_id == snapshot.tree.project_id,
            KnowledgeUnit.is_current.is_(True),
            KnowledgeUnit.valid_from <= now,
            KnowledgeUnit.valid_to > now,
            tuple_(KnowledgeUnit.source_id, KnowledgeUnit.document_version_id,
                   KnowledgeUnit.security_partition_id).in_(sorted(snapshot.scopes)),
            Document.tenant_id == KnowledgeUnit.tenant_id,
            Document.project_id == KnowledgeUnit.project_id,
            Document.source_id == KnowledgeUnit.source_id,
            Document.is_active.is_(True),
            DocumentVersion.knowledge_status == "DATA_READY",
            DocumentVersion.search_status.in_(("READY", "SEARCH_READY")),
            DocumentVersion.valid_from <= now,
            DocumentVersion.valid_to > now,
        )
    )


async def load_source_universe_snapshots(
    session: AsyncSession, principal: VerifiedPrincipal, project_id: str | None = None,
) -> list[KnowledgeUniverseSnapshot]:
    """Current source groups for display only; never a published routing tree."""
    if project_id and project_id not in principal.allowed_project_ids:
        raise NotFoundError("Knowledge project is unavailable")
    if not principal.tenant_id:
        return []
    projects = [project_id] if project_id else sorted(principal.allowed_project_ids)
    sources = list((await session.scalars(
        select(Source).where(Source.id.in_(select(SourceProjectMapping.source_id).where(
            SourceProjectMapping.organization_id == principal.organization_id,
            SourceProjectMapping.project_id.in_(projects), SourceProjectMapping.state == "CONFIRMED",
        ))).order_by(Source.id).limit(settings.universe_manifest_source_limit + 1)
    )).all())
    if len(sources) > settings.universe_manifest_source_limit:
        raise ServiceUnavailableError("Universe exceeds the configured authorized source limit")
    versions = await _load_current_ready_versions(session, sources, principal)
    snapshots = []
    for project in sorted({value.project_id for value in versions.values()} & set(projects)):
        current = {key: value for key, value in versions.items() if value.project_id == project}
        scope_key = checksum({
            "subject": principal.subject, "organization": principal.organization_id,
            "tenant": principal.tenant_id, "project": project, "projection": "source",
            "versions": sorted((key, value.source_id, value.partition_id, value.acl_mapping_version,
                                value.ingestion_run_id, value.index_stage_run_id, value.search_manifest_checksum)
                               for key, value in current.items()),
        })
        tree = UniverseTreeOut(projection="source", project_id=project, tree_version=f"source-{scope_key[:24]}",
                                   search_epoch=None, node_count=0, unit_count=0, source_count=0)
        snapshot = KnowledgeUniverseSnapshot(
            tree, principal.tenant_id, scope_key,
            {(value.source_id, key, value.partition_id) for key, value in current.items()}, {}, [], [],
        )
        inventory = _unit_query(snapshot, None).subquery()
        counts = (await session.execute(select(inventory.c.source_id, func.count())
                                       .group_by(inventory.c.source_id).order_by(inventory.c.source_id))).all()
        names = {source.id: source.name for source in sources}
        nodes = [dict(id=f"source:{source}", label=names[source], depth=0, is_leaf=True,
                      unit_count=count, source_count=1) for source, count in counts]
        # Revalidate the presentation model; these groups carry no routing membership or edges.
        tree = UniverseTreeOut.model_validate({**tree.model_dump(), "nodes": nodes,
                                              "node_count": len(counts), "source_count": len(counts),
                                              "unit_count": sum(count for _, count in counts)})
        snapshot.tree = tree
        snapshot.members = {node.id: set() for node in tree.nodes}
        if tree.nodes:
            snapshots.append(snapshot)
    return snapshots


async def _present_units(session: AsyncSession, rows) -> list[UniverseKnowledgeUnitOut]:
    block_ids = set()
    for unit, _document, _version, _source in rows:
        ids = unit.provenance_json.get("evidence_block_ids")
        if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
            raise ServiceUnavailableError("Knowledge unit has no canonical evidence locator")
        block_ids.add(ids[0])
    blocks = {
        block.id: block
        for block in (await session.scalars(select(CanonicalBlock).where(CanonicalBlock.id.in_(block_ids)))).all()
    }
    output = []
    for unit, document, version, source in rows:
        provenance = unit.provenance_json
        block_id = provenance["evidence_block_ids"][0]
        block = blocks.get(block_id)
        if (
            block is None
            or block.document_version_id != version.id
            or block.content_hash != unit.content_hash
            or block.normalized_text != unit.text
            or any(provenance.get(key) != value for key, value in {
                "tenant_id": unit.tenant_id,
                "project_id": unit.project_id,
                "source_id": source.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "security_partition_id": unit.security_partition_id,
            }.items())
        ):
            raise ServiceUnavailableError("Knowledge evidence failed canonical scope validation")
        heading = block.section_path or document.filename
        text = " ".join(unit.text.split())
        output.append(
            UniverseKnowledgeUnitOut(
                id=unit.id,
                label=text[:110] or heading,
                preview=text[:360],
                source_id=source.id,
                source_name=source.name,
                document_id=document.id,
                document_version_id=version.id,
                document_name=document.filename,
                version_no=version.version_no,
                block_id=block.id,
                heading=heading,
                page_from=block.page_from,
                page_to=block.page_to,
                anchor=(block.source_anchor or provenance.get("source_anchor") or f"block-{block.ordinal}"),
            )
        )
    return output


def _cursor_after(cursor: str | None, context: dict[str, Any]) -> str:
    if cursor is None:
        return ""
    try:
        value = _decode_universe_cursor(cursor, settings.secret_key)
    except ValueError as error:
        raise ValidationError("Invalid knowledge universe cursor") from error
    if any(value.get(key) != expected for key, expected in context.items()):
        raise ConflictError("Knowledge cursor scope changed; refresh the universe")
    after = value.get("after")
    if not isinstance(after, str) or len(after) > 128:
        raise ValidationError("Invalid knowledge universe cursor position")
    return after


async def knowledge_universe_page(
    session: AsyncSession,
    snapshot: KnowledgeUniverseSnapshot,
    *,
    node_id: str,
    limit: int,
    cursor: str | None = None,
    related_to: str | None = None,
) -> UniverseKnowledgePageOut:
    if node_id not in snapshot.members:
        raise NotFoundError("Knowledge cluster is unavailable")
    source_mode = snapshot.tree.projection == "source"
    ids = None if source_mode else snapshot.members[node_id]
    if related_to:
        if not source_mode and related_to not in snapshot.unit_ids:
            raise NotFoundError("Knowledge unit is unavailable")
        anchor = (await session.execute(_unit_query(snapshot, {related_to}))).one_or_none()
        if anchor is None:
            raise NotFoundError("Knowledge unit is unavailable")
        ids = {
            target if source == related_to else source
            for source, target, _weight in snapshot.edges
            if related_to in {source, target}
        }
    context = {
        "v": 2, "kind": "knowledge", "scope": snapshot.scope_key,
        "node": node_id, "related": related_to,
    }
    after = _cursor_after(cursor, context)
    query = _unit_query(snapshot, ids)
    if source_mode:
        query = query.where(KnowledgeUnit.source_id == node_id.removeprefix("source:"))
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (
        await session.execute(query.where(KnowledgeUnit.id > after).order_by(KnowledgeUnit.id).limit(limit + 1))
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    units = await _present_units(session, rows)
    visible = {unit.id for unit in units}
    if related_to:
        visible.add(related_to)
    edges = [
        UniverseKnowledgeEdgeOut(source=source, target=target, weight=weight)
        for source, target, weight in snapshot.edges
        if source in visible and target in visible
    ][:settings.universe_edge_budget_desktop]
    return UniverseKnowledgePageOut(
        project_id=snapshot.tree.project_id,
        tree_version=snapshot.tree.tree_version,
        node_id=node_id,
        scope_key=snapshot.scope_key,
        units=units,
        edges=edges,
        total=total,
        next_cursor=_encode_universe_cursor({**context, "after": units[-1].id}, settings.secret_key)
        if has_more and units else None,
    )


async def knowledge_universe_detail(
    session: AsyncSession,
    snapshot: KnowledgeUniverseSnapshot,
    unit_id: str,
) -> UniverseKnowledgeDetailOut:
    ids = {unit_id} if snapshot.tree.projection == "source" else {unit_id} & snapshot.unit_ids
    row = (await session.execute(_unit_query(snapshot, ids))).one_or_none()
    if row is None:
        raise NotFoundError("Knowledge unit is unavailable")
    unit = row[0]
    if unit.checksum != await unit_checksum(session, unit):
        raise ServiceUnavailableError("Knowledge unit evidence checksum mismatch")
    presented = (await _present_units(session, [row]))[0]
    facts = (await session.scalars(
        select(KnowledgeEvidence).where(KnowledgeEvidence.unit_id == unit.id).order_by(KnowledgeEvidence.id)
    )).all()
    evidence = []
    for fact in facts:
        payload = fact.payload_json
        start, end, quote = payload.get("start"), payload.get("end"), payload.get("quote")
        if (
            type(start) is not int or type(end) is not int
            or not 0 <= start <= end <= len(unit.text)
            or not isinstance(quote, str) or unit.text[start:end] != quote
            or payload.get("evidence_block_ids") != [presented.block_id]
            or not math.isfinite(fact.confidence) or not 0 <= fact.confidence <= 1
        ):
            raise ServiceUnavailableError("Knowledge evidence quote is not anchored to its canonical block")
        evidence.append(UniverseKnowledgeEvidenceOut(
            id=fact.id, tier=fact.tier, kind=fact.kind, quote=quote, confidence=fact.confidence,
            candidate=payload.get("candidate") is not False, start=start, end=end,
        ))
    return UniverseKnowledgeDetailOut(
        **presented.model_dump(), project_id=snapshot.tree.project_id,
        tree_version=snapshot.tree.tree_version, scope_key=snapshot.scope_key,
        content=unit.text, evidence=evidence,
    )


async def resolve_knowledge_universe_references(
    session: AsyncSession,
    snapshot: KnowledgeUniverseSnapshot,
    references: list[UniverseKnowledgeReferenceIn],
) -> list[UniverseKnowledgeMatchOut]:
    if snapshot.tree.projection == "source":
        predicates = []
        for ref in references:
            conditions = [KnowledgeUnit.source_id == ref.source_id]
            if ref.document_version_id:
                conditions.append(KnowledgeUnit.document_version_id == ref.document_version_id)
            if ref.block_id:
                conditions.append(KnowledgeUnit.provenance_json["evidence_block_ids"][0].as_string() == ref.block_id)
            elif ref.search_unit_id:
                continue  # SearchUnit-only references require a verified published assignment.
            predicates.append(and_(*conditions))
        if not predicates:
            return []
        rows = (await session.execute(_unit_query(snapshot, None).where(or_(*predicates))
                                      .order_by(KnowledgeUnit.id).limit(500))).all()
        by_source: dict[str, list[str]] = {}
        for row in rows:
            by_source.setdefault(row[0].source_id, []).append(row[0].id)
        return [UniverseKnowledgeMatchOut(
            project_id=snapshot.tree.project_id, tree_version=snapshot.tree.tree_version,
            node_id=f"source:{source}", unit_ids=ids,
        ) for source, ids in sorted(by_source.items())]
    candidate_ids: set[str] = set()
    for assignment in snapshot.assignments:
        if any(
            ref.source_id == assignment["source_id"]
            and (ref.search_unit_id == assignment["search_unit_id"] or (not ref.search_unit_id and not ref.block_id))
            and (not ref.document_version_id or ref.document_version_id == assignment["document_version_id"])
            for ref in references
        ):
            candidate_ids.update(assignment["knowledge_unit_ids"])
    block_refs = [ref for ref in references if ref.block_id]
    block_ids = {ref.block_id for ref in block_refs}
    if block_ids:
        query = _unit_query(snapshot, snapshot.unit_ids).where(
            KnowledgeUnit.provenance_json["evidence_block_ids"][0].as_string().in_(block_ids)
        )
        for unit, _doc, _version, _source in (await session.execute(query)).all():
            if any(
                ref.source_id == unit.source_id
                and ref.block_id == unit.provenance_json["evidence_block_ids"][0]
                and (not ref.document_version_id or ref.document_version_id == unit.document_version_id)
                for ref in block_refs
            ):
                candidate_ids.add(unit.id)
    rows = (
        await session.execute(
            _unit_query(snapshot, candidate_ids & snapshot.unit_ids).order_by(KnowledgeUnit.id).limit(500)
        )
    ).all()
    matched_ids = {row[0].id for row in rows}
    leaves = {node.id for node in snapshot.tree.nodes if node.is_leaf}
    return [
        UniverseKnowledgeMatchOut(
            project_id=snapshot.tree.project_id, tree_version=snapshot.tree.tree_version,
            node_id=node_id, unit_ids=sorted(ids & matched_ids),
        )
        for node_id, ids in snapshot.members.items()
        if node_id in leaves and ids & matched_ids
    ]
