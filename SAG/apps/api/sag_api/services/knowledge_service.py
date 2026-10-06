"""Rebuildable E0/E1 passages, anchored candidates and bounded sparse graph."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    KnowledgeEvidence,
    KnowledgeGraphBuild,
    KnowledgeJob,
    KnowledgeQueueControl,
    KnowledgeUnit,
    KnowledgeUnitEdge,
    SourceProjectMapping,
    SourceSnapshot,
)
from sag_api.services.search_index_service import COMMON_STOPWORDS


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "knowledge-v1"
    max_evidence_per_unit: int = Field(default=64, ge=1, le=256)
    max_terms: int = Field(default=64, ge=1, le=256)
    candidate_limit: int = Field(default=96, ge=1, le=256)
    posting_limit: int = Field(default=24, ge=1, le=64)
    max_degree: int = Field(default=32, ge=1, le=64)
    edge_min_score: float = Field(default=0.55, ge=0, le=1)
    e2_low_confidence: float = Field(default=0.65, ge=0, le=1)
    e2_importance_min: float = Field(default=0.5, ge=0, le=1)
    # Project-local dictionary: alias -> canonical label. No global entity linking.
    aliases: tuple[tuple[str, str], ...] = ()


SIGNAL_WEIGHTS = {"semantic": 0.4, "lexical": 0.2, "entity": 0.15, "structure": 0.1, "temporal": 0.1, "citation": 0.05}
_ENTITY_RE = re.compile(
    r"https?://[^\s<>\]\)]+|[\w.+-]+@[\w.-]+\.\w+|\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b(?:v?\d+\.\d+(?:\.\d+)?|[A-Z][A-Z0-9_]+(?:-\d+)?)\b|/[\w./{}-]+"
)


def checksum(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "sag:knowledge:" + checksum(parts)))


async def insert_once(session, model, **values):
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(insert(model).values(**values).on_conflict_do_nothing())


async def knowledge_lock(session, key: str):
    await insert_once(session, KnowledgeQueueControl, id=key, tokens_reserved=0)
    await session.execute(
        update(KnowledgeQueueControl)
        .where(KnowledgeQueueControl.id == key)
        .values(tokens_reserved=KnowledgeQueueControl.tokens_reserved)
    )


def terms(text: str) -> Counter:
    return Counter(word for word in re.findall(r"\w+", text.casefold()) if word not in COMMON_STOPWORDS)


def _fact(
    kind: str, text: str, start: int, end: int, block_id: str, anchor: str, confidence: float, **extra: object
) -> dict:
    return {
        "kind": kind,
        "confidence": confidence,
        "quote": text[start:end],
        "start": start,
        "end": end,
        "evidence_block_ids": [block_id],
        "source_anchor": anchor,
        "candidate": True,
        **extra,
    }


def extract_e0_e1(block: CanonicalBlock, config: KnowledgeConfig) -> tuple[list[dict], dict]:
    text, anchor = block.normalized_text, (block.source_anchor or "").strip() or f"block-{block.ordinal}"
    facts: list[dict] = []
    for match in _ENTITY_RE.finditer(text):
        facts.append(
            {
                "tier": "E0",
                "extractor_version": "e0-lexer-v1",
                **_fact("entity", text, *match.span(), block.id, anchor, 0.95, label=match[0]),
            }
        )
    for alias, label in config.aliases:
        if not alias.strip() or not label.strip():
            raise ValueError("Alias dictionary requires non-empty labels")
        for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.IGNORECASE):
            facts.append(
                {
                    "tier": "E0",
                    "extractor_version": "e0-dictionary-v1",
                    **_fact("alias", text, *match.span(), block.id, anchor, 0.9, label=label, alias=match[0]),
                }
            )
    # Keep code/table boundaries; narrative statements retain exact character spans.
    pattern = r"[^\n]+" if block.block_type in {"code", "table", "list"} else r"[^.!?\n]+(?:[.!?]|$)"
    for match in re.finditer(pattern, text):
        if match[0].strip() and len(match[0].strip()) >= 12:
            facts.append(
                {
                    "tier": "E0",
                    "extractor_version": "e0-statements-v1",
                    **_fact("claim", text, *match.span(), block.id, anchor, 0.55),
                }
            )
    # Bounded lightweight E1 candidates, as allowed by the dependency-pattern tier.
    for match in re.finditer(r"`([^`\n]{2,80})`", text):
        facts.append(
            {
                "tier": "E1",
                "extractor_version": "e1-domain-patterns-v1",
                **_fact("entity", text, *match.span(1), block.id, anchor, 0.7, label=match[1]),
            }
        )
    facts = facts[: config.max_evidence_per_unit]
    labels = sorted({str(f["label"]) for f in facts if f["kind"] in {"entity", "alias"}})
    if len(labels) >= 2 and len(facts) < config.max_evidence_per_unit:
        facts.append(
            {
                "tier": "E1",
                "extractor_version": "e1-domain-patterns-v1",
                **_fact(
                    "relation",
                    text,
                    0,
                    len(text),
                    block.id,
                    anchor,
                    0.4,
                    subject=labels[0],
                    predicate="CO_OCCURS",
                    object=labels[1],
                ),
            }
        )
    tokens = terms(block.section_path + " " + text)
    sparse = sorted(tokens.items(), key=lambda item: (-item[1], item[0]))[: config.max_terms]
    confidence = max((f["confidence"] for f in facts if f["kind"] in {"entity", "alias"}), default=0.55)
    importance = min(1.0, 0.25 + 0.1 * len(labels) + (0.25 if block.block_type in {"code", "table"} else 0))
    hard = bool(re.search(r"contradict|supersed|thay thế|trái ngược", text, re.IGNORECASE))
    return facts, {
        "dense": [],
        "sparse": sparse,
        "entities": labels,
        "citations": sorted(set(re.findall(r"https?://[^\s<>\]\)]+", text))),
        "local_confidence": confidence,
        "importance": importance,
        "e2_reason": "semantic_relation"
        if hard
        else (
            "low_confidence_important"
            if confidence < config.e2_low_confidence and importance >= config.e2_importance_min
            else None
        ),
    }


def evidence_payload(fact: dict, provenance: dict, valid_from: datetime, valid_to: datetime) -> dict:
    return {**fact, "provenance": provenance, "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat()}


async def unit_checksum(session: AsyncSession, unit: KnowledgeUnit) -> str:
    facts = (
        await session.scalars(
            select(KnowledgeEvidence).where(KnowledgeEvidence.unit_id == unit.id).order_by(KnowledgeEvidence.id)
        )
    ).all()
    return checksum(
        {
            "id": unit.id,
            "input": unit.input_checksum,
            "content_hash": unit.content_hash,
            "provenance": unit.provenance_json,
            "features": unit.features_json,
            "valid_from": unit.valid_from.isoformat(),
            "valid_to": unit.valid_to.isoformat(),
            "evidence": [[f.id, f.tier, f.kind, f.extractor_version, f.confidence, f.payload_json] for f in facts],
        }
    )


async def version_input(session: AsyncSession, version_id: str, config: KnowledgeConfig) -> tuple:
    version = await session.get(DocumentVersion, version_id, populate_existing=True)
    document = await session.get(Document, version.document_id, populate_existing=True) if version else None
    if not version or not document or not document.is_active or version.search_status not in {"READY", "SEARCH_READY"}:
        raise ValueError("Knowledge producer requires an active SEARCH_READY version")
    partition = (version.metadata_json or {}).get("security_partition_id")
    if not all(
        isinstance(v, str) and v.strip()
        for v in (document.tenant_id, document.project_id, document.source_id, partition)
    ):
        raise ValueError("Knowledge producer requires explicit tenant/project/source/partition")
    mapping = await session.scalar(
        select(SourceProjectMapping).where(
            SourceProjectMapping.source_id == document.source_id,
            SourceProjectMapping.project_id == document.project_id,
            SourceProjectMapping.state == "CONFIRMED",
        )
    )
    if not mapping:
        raise ValueError("Knowledge producer requires a confirmed Project-to-Source mapping")
    conflicting = await session.scalar(
        select(IngestionRun.id).where(
            IngestionRun.document_version_id == version.id,
            (IngestionRun.tenant_id != document.tenant_id) | (IngestionRun.project_id != document.project_id),
        )
    )
    if conflicting:
        raise ValueError("Knowledge producer scope conflicts with ingestion provenance")
    blocks = (
        await session.scalars(
            select(CanonicalBlock)
            .where(CanonicalBlock.document_version_id == version.id)
            .order_by(CanonicalBlock.ordinal)
        )
    ).all()
    if not blocks:
        raise ValueError("Knowledge producer requires canonical source artifacts")
    if any(hashlib.sha256(b.normalized_text.encode()).hexdigest() != b.content_hash for b in blocks):
        raise ValueError("Canonical artifact checksum mismatch")
    source = await session.scalar(select(SourceSnapshot).where(SourceSnapshot.document_version_id == version.id))
    if source and source.checksum_sha256 != version.file_hash:
        raise ValueError("Source snapshot checksum differs from DocumentVersion")
    provenance = {
        "tenant_id": document.tenant_id,
        "project_id": document.project_id,
        "source_id": document.source_id,
        "document_id": document.id,
        "document_version_id": version.id,
        "version_no": version.version_no,
        "security_partition_id": partition,
        "source_snapshot_id": source.id if source else None,
        "source_checksum": source.checksum_sha256 if source else version.file_hash,
        "published_at": version.source_published_at.isoformat() if version.source_published_at else None,
        "observed_at": version.observed_at.isoformat(),
        "ingested_at": version.ingested_at.isoformat() if version.ingested_at else None,
        "supersedes_id": version.supersedes_id,
    }
    digest = checksum(
        {
            "config": config.model_dump(mode="json"),
            "provenance": provenance,
            "validity": [version.valid_from.isoformat(), version.valid_to.isoformat()],
            "blocks": [
                [b.id, b.ordinal, b.block_type, b.content_hash, b.page_from, b.page_to, b.section_path, b.source_anchor]
                for b in blocks
            ],
        }
    )
    return version, blocks, provenance, digest


async def rebuild_knowledge_version(
    session: AsyncSession, version_id: str, config: KnowledgeConfig = KnowledgeConfig()
) -> list[KnowledgeUnit]:
    # Serialize knowledge work on its own row, rather than a long-held ingestion/readiness row.
    await knowledge_lock(session, "version:" + version_id)
    version, blocks, base, digest = await version_input(session, version_id, config)
    existing = {
        u.id: u
        for u in (
            await session.scalars(select(KnowledgeUnit).where(KnowledgeUnit.document_version_id == version_id))
        ).all()
    }
    units = []
    for block in blocks:
        if block.block_type == "heading" or not block.normalized_text.strip():
            continue
        unit_id = stable_id(
            base["tenant_id"], base["project_id"], base["security_partition_id"], version_id, block.id, "passage-v1"
        )
        provenance = {
            **base,
            "evidence_block_ids": [block.id],
            "source_anchor": (block.source_anchor or "").strip() or f"block-{block.ordinal}",
            "page_from": block.page_from,
            "page_to": block.page_to,
            "section_path": block.section_path,
            "block_type": block.block_type,
        }
        input_hash = checksum({"version_input": digest, "block_id": block.id})
        unit = existing.get(unit_id)
        if (
            unit
            and unit.input_checksum == input_hash
            and all(
                getattr(unit, key) == base[key]
                for key in ("document_version_id", "tenant_id", "project_id", "source_id", "security_partition_id")
            )
            and unit.ordinal == block.ordinal
            and unit.text == block.normalized_text
            and unit.content_hash == block.content_hash
            and unit.provenance_json == provenance
            and unit.checksum == await unit_checksum(session, unit)
        ):
            unit.is_current = True
            units.append(unit)
            continue
        facts, features = extract_e0_e1(block, config)
        unit = unit or KnowledgeUnit(id=unit_id)
        unit.document_version_id, unit.tenant_id, unit.project_id = version_id, base["tenant_id"], base["project_id"]
        unit.source_id, unit.security_partition_id = base["source_id"], base["security_partition_id"]
        unit.ordinal, unit.text, unit.content_hash = block.ordinal, block.normalized_text, block.content_hash
        unit.input_checksum, unit.checksum, unit.is_current = input_hash, "", True
        unit.provenance_json, unit.features_json = provenance, features
        unit.valid_from, unit.valid_to = version.valid_from, version.valid_to
        session.add(unit)
        await session.flush()
        await session.execute(delete(KnowledgeEvidence).where(KnowledgeEvidence.unit_id == unit_id))
        for fact in facts:
            payload = evidence_payload(fact, provenance, unit.valid_from, unit.valid_to)
            session.add(
                KnowledgeEvidence(
                    id=stable_id(unit_id, checksum(fact)),
                    unit_id=unit_id,
                    tier=fact["tier"],
                    kind=fact["kind"],
                    extractor_version=fact["extractor_version"],
                    confidence=fact["confidence"],
                    payload_json=payload,
                )
            )
        await session.flush()
        unit.checksum = await unit_checksum(session, unit)
        units.append(unit)
    live = {u.id for u in units}
    for old in existing.values():
        if old.id not in live:
            old.is_current = False
    if not units:
        raise ValueError("Knowledge input has no non-heading passages")
    await session.scalar(select(DocumentVersion).where(DocumentVersion.id == version_id).with_for_update())
    _, _, _, final_digest = await version_input(session, version_id, config)
    if final_digest != digest:
        raise ValueError("Source artifacts changed during knowledge rebuild")
    version.knowledge_status = "DATA_READY"
    version.metadata_json = {
        **(version.metadata_json or {}),
        "knowledge_input_checksum": digest,
        "knowledge_config": config.model_dump(mode="json"),
    }
    await session.flush()
    # Validated model responses are source artifacts too: replay without another model call or token charge.
    from sag_api.jobs.knowledge import apply_e2

    artifacts = (
        await session.scalars(
            select(KnowledgeJob).where(
                KnowledgeJob.document_version_id == version_id,
                KnowledgeJob.kind == "E2",
                KnowledgeJob.status == "SUCCEEDED",
                KnowledgeJob.result_json.is_not(None),
            )
        )
    ).all()
    by_id = {u.id: u for u in units}
    for artifact in artifacts:
        unit = by_id.get(artifact.unit_id)
        if unit and artifact.input_checksum == unit.input_checksum:
            await apply_e2(
                session, artifact, artifact.result_json, extractor_version=artifact.extractor_version, config=config
            )
    return units


def _cosine(left: list, right: list) -> float | None:
    if not left or not right:
        return None
    if len(left) != len(right) or any(not math.isfinite(v) for v in left + right):
        raise ValueError("Knowledge dense features must be finite and share a dimension")
    scale_a, scale_b = max(map(abs, left)), max(map(abs, right))
    if not scale_a or not scale_b:
        return None
    a, b = [v / scale_a for v in left], [v / scale_b for v in right]
    value = math.fsum(x * y for x, y in zip(a, b, strict=True)) / math.sqrt(
        math.fsum(x * x for x in a) * math.fsum(y * y for y in b)
    )
    return (max(-1.0, min(1.0, value)) + 1) / 2


def fuse_signals(signals: dict[str, float | None]) -> float:
    observed = [(SIGNAL_WEIGHTS[name], value) for name, value in signals.items() if value is not None]
    if any(not math.isfinite(value) or not 0 <= value <= 1 for _, value in observed):
        raise ValueError("Graph signals must be calibrated into [0,1]")
    return math.fsum(w * v for w, v in observed) / math.fsum(w for w, _ in observed) if observed else 0.0


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    low, high = math.floor(offset), math.ceil(offset)
    return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)


def graph_payload(units: list[KnowledgeUnit], config: KnowledgeConfig) -> dict:
    scopes = {(u.tenant_id, u.project_id) for u in units}
    if len(scopes) != 1 or len({u.id for u in units}) != len(units):
        raise ValueError("Graph input requires unique Knowledge Units in one tenant/project")
    partitions: dict[str, list[KnowledgeUnit]] = defaultdict(list)
    for unit in sorted(units, key=lambda u: u.id):
        if not unit.security_partition_id:
            raise ValueError("Graph input requires an ACL partition")
        partitions[unit.security_partition_id].append(unit)
    output, calibrations = [], {}
    for partition, rows in sorted(partitions.items()):
        unit_map = {u.id: u for u in rows}
        token_counts = {u.id: terms(u.text + " " + u.provenance_json["section_path"]) for u in rows}
        df = Counter(t for counts in token_counts.values() for t in counts)
        entity_df = Counter(e for u in rows for e in u.features_json["entities"])
        avg_len = sum(sum(c.values()) for c in token_counts.values()) / len(rows) or 1
        postings: dict[tuple, list[str]] = defaultdict(list)
        for unit in rows:
            features, provenance = unit.features_json, unit.provenance_json
            keys = [("term", t) for t in sorted(token_counts[unit.id], key=lambda t: (df[t], t))[: config.max_terms]]
            keys += [("entity", e) for e in features["entities"]] + [("citation", c) for c in features["citations"]]
            keys += [
                ("section", unit.document_version_id, provenance["section_path"]),
                ("document", provenance["document_id"]),
                ("temporal", unit.valid_from.date().isoformat()),
            ]
            dense = features.get("dense", [])
            if dense:
                # ponytail: fixed LSH buckets; replace with partition-filtered ANN when dense recall needs it.
                bits = tuple(
                    sum(
                        v * (1 if int(hashlib.sha256(f"{p}:{i}".encode()).hexdigest()[:8], 16) % 2 else -1)
                        for i, v in enumerate(dense)
                    )
                    >= 0
                    for p in range(8)
                )
                keys += [("dense", band, bits[band : band + 4]) for band in (0, 4)]
            for key in set(keys):
                postings[key].append(unit.id)
        by_unit: dict[str, list[tuple]] = defaultdict(list)
        for key, ids in postings.items():
            for unit_id in ids:
                by_unit[unit_id].append(key)
        pairs = set()
        for unit in rows:
            votes: Counter = Counter()
            for key in sorted(by_unit[unit.id], key=lambda k: (len(postings[k]), str(k))):
                ids = postings[key]
                index = bisect_left(ids, unit.id)
                half = config.posting_limit // 2
                start = max(0, min(index - half, len(ids) - config.posting_limit - 1))
                for other in ids[start : start + config.posting_limit + 1]:
                    if other != unit.id:
                        votes[other] += 1
            for other, _ in sorted(votes.items(), key=lambda item: (-item[1], item[0]))[: config.candidate_limit]:
                pairs.add(tuple(sorted((unit.id, other))))

        def bm25(query: Counter, target: Counter, df=df, size=len(rows), avg_len=avg_len) -> float:
            length = sum(target.values())
            return math.fsum(
                math.log(1 + (size - df[t] + 0.5) / (df[t] + 0.5))
                * target[t]
                * 2.5
                / (target[t] + 1.5 * (0.25 + 0.75 * length / avg_len))
                for t in sorted(query.keys() & target.keys())
            )

        raw = {
            pair: 0.5
            * (bm25(token_counts[pair[0]], token_counts[pair[1]]) + bm25(token_counts[pair[1]], token_counts[pair[0]]))
            for pair in sorted(pairs)
        }
        q05, q95 = _quantile(list(raw.values()), 0.05), _quantile(list(raw.values()), 0.95)
        calibrations[partition] = {
            "method": "symmetric-bm25-q05-q95-v1",
            "q05": q05,
            "q95": q95,
            "pair_count": len(raw),
            "avg_doc_len": avg_len,
            "unit_count": len(rows),
        }
        candidates = []
        for (left, right), value in raw.items():
            a, b = unit_map[left], unit_map[right]
            ap, bp = a.provenance_json, b.provenance_json
            af, bf = a.features_json, b.features_json
            ea, eb = set(af["entities"]), set(bf["entities"])
            weights = {e: math.log(1 + len(rows) / entity_df[e]) for e in sorted(ea | eb)}
            entity = math.fsum(weights[e] for e in sorted(ea & eb)) / math.fsum(weights.values()) if weights else None
            same_doc = ap["document_id"] == bp["document_id"]
            structure = (
                0.85
                if same_doc and ap["section_path"] == bp["section_path"]
                else 0.65
                if same_doc and abs(a.ordinal - b.ordinal) <= 2
                else 0.45
                if same_doc
                else 0.25
                if a.source_id == b.source_id
                else 0.0
            )
            citations_a, citations_b = set(af["citations"]), set(bf["citations"])
            signals = {
                "semantic": _cosine(af.get("dense", []), bf.get("dense", [])),
                "lexical": max(0.0, min(1.0, (value - q05) / (q95 - q05))) if q95 > q05 else float(value > 0),
                "entity": entity,
                "structure": structure,
                "temporal": float(a.valid_from < b.valid_to and b.valid_from < a.valid_to),
                "citation": (0.7 if citations_a & citations_b else 0.0) if citations_a or citations_b else None,
            }
            score = fuse_signals(signals)
            if score > 0 and score >= config.edge_min_score:
                candidates.append(
                    {
                        "source": left,
                        "target": right,
                        "partition": partition,
                        "weight": score,
                        "signals": signals,
                        "raw_lexical": value,
                    }
                )
        degree: Counter = Counter()
        for edge in sorted(candidates, key=lambda e: (-e["weight"], e["source"], e["target"])):
            if degree[edge["source"]] < config.max_degree and degree[edge["target"]] < config.max_degree:
                output.append(edge)
                degree.update((edge["source"], edge["target"]))
    tenant, project = next(iter(scopes))
    return {
        "contract": "knowledge-graph.v1",
        "tenant_id": tenant,
        "project_id": project,
        "config": config.model_dump(mode="json"),
        "weights": SIGNAL_WEIGHTS,
        "calibrations": calibrations,
        "units": [[u.id, u.checksum] for u in sorted(units, key=lambda u: u.id)],
        "edges": sorted(output, key=lambda e: (e["source"], e["target"])),
    }


async def persist_graph(
    session: AsyncSession, units: list[KnowledgeUnit], config: KnowledgeConfig = KnowledgeConfig()
) -> KnowledgeGraphBuild:
    for unit in units:
        if unit.checksum != await unit_checksum(session, unit):
            raise ValueError("Knowledge Unit/evidence checksum mismatch")
    payload = graph_payload(units, config)
    digest = checksum(payload)
    build_id = "graph-" + digest[:24]
    build = await session.get(KnowledgeGraphBuild, build_id)
    if build:
        await verify_graph(session, build_id)
        return build
    build = KnowledgeGraphBuild(
        id=build_id,
        tenant_id=payload["tenant_id"],
        project_id=payload["project_id"],
        checksum=digest,
        manifest_json=payload,
    )
    session.add(build)
    await session.flush()
    for edge in payload["edges"]:
        session.add(
            KnowledgeUnitEdge(
                build_id=build_id,
                source_unit_id=edge["source"],
                target_unit_id=edge["target"],
                security_partition_id=edge["partition"],
                weight=edge["weight"],
                signals_json={"signals": edge["signals"], "raw_lexical": edge["raw_lexical"]},
            )
        )
    await session.flush()
    return build


async def verify_graph(session: AsyncSession, build_id: str) -> KnowledgeGraphBuild:
    build = await session.get(KnowledgeGraphBuild, build_id, populate_existing=True)
    if not build or checksum(build.manifest_json) != build.checksum or build.id != "graph-" + build.checksum[:24]:
        raise ValueError("Persisted graph manifest checksum mismatch")
    stored = (
        await session.scalars(
            select(KnowledgeUnitEdge)
            .where(KnowledgeUnitEdge.build_id == build_id)
            .order_by(KnowledgeUnitEdge.source_unit_id, KnowledgeUnitEdge.target_unit_id)
        )
    ).all()
    actual = [
        {
            "source": e.source_unit_id,
            "target": e.target_unit_id,
            "partition": e.security_partition_id,
            "weight": e.weight,
            **e.signals_json,
        }
        for e in stored
    ]
    if actual != build.manifest_json["edges"]:
        raise ValueError("Persisted graph edges mismatch")
    return build
