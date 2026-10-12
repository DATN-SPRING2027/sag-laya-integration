"""Source -> knowledge -> graph -> persisted inactive candidate; queue failure isolation."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    KnowledgeEvidence,
    KnowledgeGraphBuild,
    KnowledgeJob,
    KnowledgeQueueControl,
    KnowledgeTreeNode,
    KnowledgeUnit,
    KnowledgeUnitEdge,
    ProjectSearchState,
    SearchUnit,
    Source,
    SourceProjectMapping,
    SourceSnapshot,
    TreeManifest,
)
from sag_api.enums import DocumentStatus
from sag_api.jobs.knowledge import KnowledgeWorker, QueuePolicy, apply_e2, claim_job, enqueue
from sag_api.services.canonical_service import parse_and_persist_document_content
from sag_api.services.knowledge_candidate_service import (
    CorpusQuery,
    build_knowledge_candidate,
    current_units,
    verify_knowledge_candidate,
)
from sag_api.services.knowledge_service import (
    KnowledgeConfig,
    fuse_signals,
    graph_payload,
    persist_graph,
    rebuild_knowledge_version,
    stable_id,
    version_input,
)
from sag_api.services.routing_tree_service import TreeBuildConfig

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = TreeBuildConfig(target_cluster_size=2, max_cluster_size=4, max_children=4, min_routing_recall_at_k=0.8)


@pytest.fixture
async def store(tmp_path):
    url = os.getenv("SAG_KNOWLEDGE_TEST_DATABASE_URL")
    schema = "knowledge_test_" + uuid.uuid4().hex
    bootstrap = None
    if url:
        bootstrap = create_async_engine(url)
        async with bootstrap.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "knowledge.db"))

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()
        if bootstrap:
            assert schema.startswith("knowledge_test_") and schema.removeprefix("knowledge_test_").isalnum()
            async with bootstrap.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await bootstrap.dispose()


async def seed(session, *, tenant="t", project="p", partition="public", name="config", content=None):
    content = content or (
        "# Deployment\n\nRedis CACHE_TTL is 60 seconds for DATN-42.\n\n"
        "`Redis` supersedes version v1.2 for cache persistence.\n\n"
        "PostgreSQL stores documents with stable evidence.\n\n"
        "# Security\n\nThe API requires AUTH_TOKEN and validates /api/v1/search.\n\n"
        "Tenant access never crosses the partition boundary.\n"
    )
    source_id = stable_id(tenant, project, partition, name).replace("-", "")
    session.add(Source(id=source_id, name=name, sag_source_config_id=source_id))
    await session.flush()
    session.add(
        SourceProjectMapping(
            source_id=source_id,
            organization_id=tenant,
            project_id=project,
            state="CONFIRMED",
            confirmed_at=NOW,
            confirmed_by="test",
            approval_ref="fixture",
        )
    )
    document_id, version_id = stable_id(source_id, "doc").replace("-", ""), stable_id(source_id, "version")
    session.add(
        Document(
            id=document_id,
            source_id=source_id,
            tenant_id=tenant,
            project_id=project,
            filename=name + ".md",
            storage_path="fixture.md",
            status=DocumentStatus.READY,
            is_active=True,
        )
    )
    await session.flush()
    file_hash = hashlib.sha256(content.encode()).hexdigest()
    session.add(
        DocumentVersion(
            id=version_id,
            document_id=document_id,
            version_no=1,
            file_hash=file_hash,
            search_status="SEARCH_READY",
            search_ready_at=NOW,
            observed_at=NOW,
            valid_from=NOW,
            valid_to=NOW + timedelta(days=365),
            metadata_json={"security_partition_id": partition},
        )
    )
    await session.flush()
    session.add(
        SourceSnapshot(
            id=stable_id(version_id, "snapshot"),
            document_version_id=version_id,
            storage_uri="fixture.md",
            original_filename=name + ".md",
            mime_type="text/markdown",
            byte_size=len(content.encode()),
            checksum_sha256=file_hash,
        )
    )
    blocks = await parse_and_persist_document_content(session, version_id, content)
    session.add(
        SearchUnit(
            id=stable_id(version_id, "search"),
            document_version_id=version_id,
            block_from_id=blocks[0].id,
            block_to_id=blocks[-1].id,
            security_partition_id=partition,
            content_hash=file_hash,
            token_count=100,
            page_from=1,
            page_to=1,
            section_path="Root",
        )
    )
    await session.commit()
    return version_id


def gold(units, *, query="Redis CACHE_TTL DATN-42"):
    target = next(u for u in units if "CACHE_TTL" in u.text and u.security_partition_id == "public")
    return [
        CorpusQuery(
            query_id="cache",
            query=query,
            target_unit_id=target.id,
            partition_id=target.security_partition_id,
            authorized_source_ids=(target.source_id,),
            k=10,
        )
    ]


async def test_rebuild_stable_distinct_units_anchored_evidence_and_source_reparse(store):
    async with store() as session:
        version = await seed(session)
        block = await session.scalar(select(CanonicalBlock).where(CanonicalBlock.block_type != "heading"))
        block.source_anchor = "   "
        units = await rebuild_knowledge_version(session, version)
        identity = [(u.id, u.checksum) for u in units]
        search_ids = set(await session.scalars(select(SearchUnit.id)))
        assert not search_ids.intersection(u.id for u in units)
        facts = (await session.scalars(select(KnowledgeEvidence))).all()
        assert {f.kind for f in facts} >= {"entity", "claim", "relation"}
        for fact in facts:
            unit = next(u for u in units if u.id == fact.unit_id)
            p = fact.payload_json
            assert unit.text[p["start"] : p["end"]] == p["quote"]
            assert p["provenance"]["source_snapshot_id"] and p["source_anchor"].strip()
            assert p["valid_from"] == unit.valid_from.isoformat() and p["candidate"] is True
        await session.commit()
        assert [(u.id, u.checksum) for u in await rebuild_knowledge_version(session, version)] == identity
        # Canonical reparse invalidates current knowledge, but retains historical evidence.
        blocks = (
            await session.scalars(
                select(CanonicalBlock)
                .where(CanonicalBlock.document_version_id == version)
                .order_by(CanonicalBlock.ordinal)
            )
        ).all()
        from sag_api.parsing.canonical import ExtractedBlock
        from sag_api.services.canonical_service import persist_canonical_blocks

        await persist_canonical_blocks(
            session,
            version,
            [
                ExtractedBlock(
                    b.ordinal,
                    b.block_type,
                    b.normalized_text,
                    b.content_hash,
                    b.page_from,
                    b.page_to,
                    b.section_path,
                    b.source_anchor,
                )
                for b in blocks
            ],
        )
        assert (await session.get(DocumentVersion, version)).knowledge_status != "DATA_READY"
        assert [(u.id, u.checksum) for u in await rebuild_knowledge_version(session, version)] == identity
        assert (await session.get(DocumentVersion, version)).search_status == "SEARCH_READY"


async def test_sparse_graph_rebuild_calibration_missing_signals_degree_and_acl(store):
    async with store() as session:
        a, b = await seed(session), await seed(session, partition="private")
        units = await rebuild_knowledge_version(session, a) + await rebuild_knowledge_version(session, b)
        config = KnowledgeConfig(max_degree=2, edge_min_score=0.1)
        first = await persist_graph(session, units, config)
        second = await persist_graph(session, list(reversed(units)), config)
        assert first.id == second.id
        assert first.manifest_json == graph_payload(list(reversed(units)), config)
        degree = Counter()
        index = {u.id: u for u in units}
        for edge in first.manifest_json["edges"]:
            assert index[edge["source"]].security_partition_id == index[edge["target"]].security_partition_id
            assert 0 <= edge["weight"] <= 1 and edge["signals"]["semantic"] is None
            degree.update((edge["source"], edge["target"]))
        assert max(degree.values()) <= 2
        assert set(first.manifest_json["calibrations"]) == {"public", "private"}
        assert fuse_signals({"semantic": None, "lexical": 0.8}) == pytest.approx(0.8)
        await session.commit()
        stored = await session.scalar(select(KnowledgeUnitEdge).where(KnowledgeUnitEdge.build_id == first.id))
        stored.weight = 0
        await session.flush()
        with pytest.raises(ValueError, match="edges mismatch"):
            await persist_graph(session, units, config)


async def test_temporal_version_change_requeues_knowledge_and_preserves_search_ready(store):
    from sag_api.services.dedup_and_temporal_service import resolve_temporal_supersedes

    async with store() as session:
        version_id = await seed(session)
        units = await rebuild_knowledge_version(session, version_id)
        original_ids = [u.id for u in units]
        previous = await session.get(DocumentVersion, version_id)
        instant = NOW + timedelta(days=1)
        content = "# Revision\n\nPostgreSQL stores the revised routing manifest with evidence."
        digest = hashlib.sha256(content.encode()).hexdigest()
        revised = DocumentVersion(
            id=stable_id(version_id, "revised"),
            document_id=previous.document_id,
            version_no=2,
            file_hash=digest,
            observed_at=instant,
            valid_from=instant,
            valid_to=previous.valid_to,
            search_status="SEARCH_READY",
            search_ready_at=instant,
            metadata_json={"security_partition_id": "public"},
        )
        session.add(revised)
        await session.flush()
        await parse_and_persist_document_content(session, revised.id, content)
        await resolve_temporal_supersedes(session, current_version=revised, document_id=previous.document_id)
        assert previous.knowledge_status == "NOT_STARTED" and previous.search_status == "SEARCH_READY"
        await session.commit()
    worker = KnowledgeWorker(store, SimpleNamespace(knowledge_e2_url="", knowledge_e2_api_key=""), policy=QueuePolicy())
    await worker.discover()
    assert await worker.run_one() and await worker.run_one()
    async with store() as session:
        current = await current_units(session, "t", "p")
        historical = [u for u in current if u.document_version_id == version_id]
        assert sorted(u.id for u in historical) == sorted(original_ids)
        assert all(u.valid_to == instant for u in historical)
        facts = (
            await session.scalars(select(KnowledgeEvidence).where(KnowledgeEvidence.unit_id.in_(original_ids)))
        ).all()
        assert all(f.payload_json["valid_to"] == instant.isoformat() for f in facts)
        assert (await session.get(DocumentVersion, revised.id)).search_status == "SEARCH_READY"


async def test_persisted_candidate_checksum_profiles_lineage_idempotency_and_no_active_switch(store):
    async with store() as session:
        version = await seed(session)
        units = await rebuild_knowledge_version(session, version)
        state = ProjectSearchState(project_id="p", active_tree_version="already-active", active_search_epoch=7)
        session.add(state)
        candidate = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        assert candidate.status == "INACTIVE"
        await session.commit()
        second = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        assert candidate.tree_version == second.tree_version
        assert (await session.get(ProjectSearchState, "p")).active_tree_version == "already-active"
        assert (await session.get(ProjectSearchState, "p")).active_search_epoch == 7
        await verify_knowledge_candidate(session, candidate.tree_version)
        original_giant_ratio = candidate.giant_ratio
        candidate.giant_ratio = -1
        await session.flush()
        with pytest.raises(ValueError, match="metrics/config"):
            await verify_knowledge_candidate(session, candidate.tree_version)
        candidate.giant_ratio = original_giant_ratio
        await session.flush()
        node = await session.scalar(
            select(KnowledgeTreeNode).where(KnowledgeTreeNode.tree_version == candidate.tree_version)
        )
        node.parent_id = "tampered"
        await session.flush()
        with pytest.raises(ValueError, match="nodes/profiles/lineage"):
            await verify_knowledge_candidate(session, candidate.tree_version)


async def test_failed_quality_candidate_is_persisted_rejected_and_cannot_be_promoted(store):
    async with store() as session:
        units = await rebuild_knowledge_version(session, await seed(session))
        rejected = await build_knowledge_candidate(session, tenant_id="t", project_id="p", queries=[], config=CONFIG)
        assert rejected.status == "REJECTED"
        assert not rejected.manifest_json["snapshot"]["quality_gates"]["routing_recall"]
        rejected.status = "INACTIVE"
        await session.flush()
        with pytest.raises(ValueError, match="Failed quality/ACL"):
            await verify_knowledge_candidate(session, rejected.tree_version)
        assert units


async def test_failed_acl_gate_is_persisted_rejected_without_an_active_tree(store, monkeypatch):
    from sag_api.services import knowledge_candidate_service
    from sag_api.services.routing_tree_service import RoutingBenchmarkCase

    async with store() as session:
        units = await rebuild_knowledge_version(session, await seed(session))
        private = await rebuild_knowledge_version(session, await seed(session, partition="private"))
        target = gold(units)[0].target_unit_id

        def leaking_route(*_args):
            return [RoutingBenchmarkCase("leak", target, (private[0].id,))], [{"query_id": "leak"}]

        monkeypatch.setattr(knowledge_candidate_service, "benchmark_routes", leaking_route)
        candidate = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        assert candidate.status == "REJECTED"
        assert not candidate.manifest_json["snapshot"]["quality_gates"]["acl_partition_isolation"]
        await verify_knowledge_candidate(session, candidate.tree_version)
        assert not await session.get(ProjectSearchState, "p")


async def test_tenant_project_partition_source_isolation_and_corrupt_provenance_fail_closed(store):
    async with store() as session:
        versions = [
            await seed(session),
            await seed(session, tenant="other"),
            await seed(session, project="other"),
            await seed(session, partition="private"),
        ]
        for version in versions:
            await rebuild_knowledge_version(session, version)
        units = await current_units(session, "t", "p")
        assert len(units) == 10 and {u.tenant_id for u in units} == {"t"} and {u.project_id for u in units} == {"p"}
        candidate = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        snapshot = candidate.manifest_json["snapshot"]
        assert snapshot["quality_metrics"]["acl_routing_leakage_rate"] == 0
        assert all(
            next(u for u in units if u.id == uid).security_partition_id == "public"
            for uid in candidate.manifest_json["producer"]["benchmark_evidence"][0]["routed_unit_ids"]
        )
        mapping = await session.scalar(
            select(SourceProjectMapping).where(SourceProjectMapping.source_id == units[0].source_id)
        )
        mapping.state, mapping.revoked_at = "REVOKED", NOW
        mapping.revoked_by, mapping.revocation_ref = "test", "revoke-fixture"
        with pytest.raises(ValueError, match="confirmed Project-to-Source"):
            await current_units(session, "t", "p")
        mapping.state = "CONFIRMED"
        units[0].security_partition_id = "private" if units[0].security_partition_id == "public" else "public"
        with pytest.raises(ValueError, match="ACL checksum"):
            await current_units(session, "t", "p")


async def test_queue_admission_concurrency_budget_lease_retry_and_max_age(store):
    policy = QueuePolicy(capacity=2, concurrency=1, daily_tokens=30000, retry_seconds=1)
    async with store() as session:
        version = await seed(session)
        units = await rebuild_knowledge_version(session, version)
        a = await enqueue(session, version, "t", "E2", units[0].input_checksum, policy, unit_id=units[0].id)
        assert (
            await enqueue(session, version, "t", "E2", units[0].input_checksum, policy, unit_id=units[0].id)
        ).id == a.id
        b = await enqueue(session, version, "t", "E2", units[1].input_checksum, policy, unit_id=units[1].id)
        assert await enqueue(session, version, "t", "FOUNDATION", "x", policy) is None
        first = await claim_job(session, policy, e2_enabled=True)
        assert first and await claim_job(session, policy, e2_enabled=True) is None
        first.status = "SUCCEEDED"
        await session.flush()
        assert await claim_job(session, policy, e2_enabled=True) is None
        assert (await session.get(KnowledgeJob, b.id if first.id == a.id else a.id)).error_code == "DAILY_BUDGET"
        first.status, first.lease_until = "RUNNING", datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()
        await claim_job(session, policy, e2_enabled=False)
        assert first.status == "RETRY" and first.error_code == "LEASE_EXPIRED"
        first.created_at = datetime.now(UTC) - timedelta(days=2)
        await session.flush()
        await claim_job(session, policy, e2_enabled=False)
        assert first.status == "EXPIRED"
        assert (await session.get(DocumentVersion, version)).search_status == "SEARCH_READY"


async def test_async_failure_retry_does_not_change_search_and_does_not_leak_error(store):
    async def fail(_unit):
        raise RuntimeError("Bearer sk-secret-do-not-persist")

    settings = SimpleNamespace(knowledge_e2_url="", knowledge_e2_api_key="")
    policy = QueuePolicy(max_attempts=2, retry_seconds=1, daily_tokens=100000)
    async with store() as session:
        version = await seed(session)
        units = await rebuild_knowledge_version(session, version)
        unit = units[0]
        job = await enqueue(session, version, "t", "E2", unit.input_checksum, policy, unit_id=unit.id)
        await session.commit()
    worker = KnowledgeWorker(store, settings, policy=policy, enrich=fail)
    assert await worker.run_one()
    async with store() as session:
        job = await session.get(KnowledgeJob, job.id)
        assert job.status == "RETRY" and job.error_code == "RuntimeError"
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await worker.run_one()
    async with store() as session:
        assert (await session.get(KnowledgeJob, job.id)).status == "FAILED"
        version = await session.get(DocumentVersion, version)
        assert version.search_status == "SEARCH_READY" and version.search_ready_at == NOW


async def test_e2_exact_evidence_success_retry_and_unanchored_or_stale_output_rejected(store):
    async with store() as session:
        version = await seed(session)
        unit = (await rebuild_knowledge_version(session, version))[0]
        job = await enqueue(session, version, "t", "E2", unit.input_checksum, QueuePolicy(), unit_id=unit.id)
        result = {
            "entities": [
                {
                    "quote": unit.text[:5],
                    "start": 0,
                    "end": 5,
                    "block_id": unit.provenance_json["evidence_block_ids"][0],
                    "confidence": 0.8,
                    "label": "Redis",
                }
            ],
            "aliases": [],
            "claims": [],
            "relations": [],
        }
        await apply_e2(session, job, result, extractor_version="test-e2")
        digest = unit.checksum
        await apply_e2(session, job, result, extractor_version="test-e2")
        assert unit.checksum == digest
        assert (await rebuild_knowledge_version(session, version))[0].checksum == digest
        result["entities"][0]["quote"] = "fabricated"
        with pytest.raises(ValueError, match="exact in-unit"):
            await apply_e2(session, job, result, extractor_version="test-e2")
        original_text = unit.text
        unit.text += " tampered"
        with pytest.raises(ValueError, match="Stale"):
            await apply_e2(session, job, result, extractor_version="test-e2")
        unit.text = original_text
        job.input_checksum = "stale"
        with pytest.raises(ValueError, match="Stale"):
            await apply_e2(session, job, result, extractor_version="test-e2")


async def test_discovery_is_bounded_selective_and_recoverable(store):
    async def enrich(_unit):
        raise AssertionError("This test only runs foundation work")

    settings = SimpleNamespace(knowledge_e2_url="", knowledge_e2_api_key="")
    worker = KnowledgeWorker(store, settings, policy=QueuePolicy(), enrich=enrich)
    async with store() as session:
        version = await seed(session)
    await worker.discover()
    await worker.discover()
    assert await worker.run_one()
    await worker.discover()
    async with store() as session:
        jobs = (await session.scalars(select(KnowledgeJob))).all()
        assert sum(j.kind == "FOUNDATION" for j in jobs) == 1
        assert sum(j.kind == "E2" for j in jobs) == 1
        assert (await session.get(DocumentVersion, version)).knowledge_status == "DATA_READY"
        assert (await session.get(DocumentVersion, version)).search_status == "SEARCH_READY"


@pytest.mark.parametrize("existing_backlog", [False, True])
async def test_disabled_e2_does_not_admit_or_block_foundation(store, existing_backlog):
    policy = QueuePolicy(capacity=1)
    settings = SimpleNamespace(knowledge_e2_url="", knowledge_e2_api_key="", knowledge_e2_model="")
    worker = KnowledgeWorker(store, settings, policy=policy)
    async with store() as session:
        version = await seed(session)
    await worker.discover()
    assert await worker.run_one()
    await worker.discover()
    async with store() as session:
        assert not await session.scalar(select(KnowledgeJob.id).where(KnowledgeJob.kind == "E2"))
        if existing_backlog:
            unit = await session.scalar(select(KnowledgeUnit).where(KnowledgeUnit.document_version_id == version))
            job = await enqueue(session, version, "t", "E2", unit.input_checksum, policy, unit_id=unit.id)
            job.created_at = datetime.now(UTC) - timedelta(days=2)
            await session.commit()
        newer = await seed(session, name="next-document")
    await worker.discover()
    assert await worker.run_one()
    async with store() as session:
        version = await session.get(DocumentVersion, newer)
        assert version.knowledge_status == "DATA_READY" and version.search_status == "SEARCH_READY"
    if not existing_backlog:
        calls = []

        async def enrich(unit):
            calls.append(unit.id)
            return {"entities": [], "aliases": [], "claims": [], "relations": []}, "test-enabled-e2"

        enabled = KnowledgeWorker(store, settings, policy=policy, enrich=enrich)
        await enabled.discover()
        assert await enabled.run_one() and len(calls) == 1


@pytest.mark.parametrize(
    ("field", "corrupted"),
    [
        ("text", "Corrupted derived text"),
        ("tenant_id", "wrong-tenant"),
        ("project_id", "wrong-project"),
        ("source_id", "wrong-source"),
        ("security_partition_id", "wrong-partition"),
        ("ordinal", 999),
    ],
)
async def test_rebuild_restores_canonical_text_and_scope_after_derived_corruption(store, field, corrupted):
    async with store() as session:
        version = await seed(session)
        unit = (await rebuild_knowledge_version(session, version))[0]
        expected, original_checksum = getattr(unit, field), unit.checksum
        setattr(unit, field, corrupted)
        await session.commit()
        rebuilt = await rebuild_knowledge_version(session, version)
        repaired = next(u for u in rebuilt if u.id == unit.id)
        assert getattr(repaired, field) == expected and repaired.checksum == original_checksum
        assert await current_units(session, "t", "p")


async def test_postgres_lease_recovery_skips_completion_lock_without_duplicate_budget(store):
    async with store() as session:
        if session.bind.dialect.name != "postgresql":
            pytest.skip("Requires PostgreSQL independent row locks")
        version = await seed(session)
        unit = (await rebuild_knowledge_version(session, version))[0]
        policy = QueuePolicy(concurrency=1)
        job = await enqueue(session, version, "t", "E2", unit.input_checksum, policy, unit_id=unit.id)
        await session.commit()
        job = await claim_job(session, policy, e2_enabled=True)
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        job_id, charge = job.id, job.reserved_tokens
        await session.commit()
    async with store() as completion:
        current = await completion.scalar(select(KnowledgeJob).where(KnowledgeJob.id == job_id).with_for_update())
        result = {"entities": [], "aliases": [], "claims": [], "relations": []}
        await apply_e2(completion, current, result, extractor_version="test-completion")
        current.status, current.lease_token, current.lease_until = "SUCCEEDED", None, None
        current.result_json, current.extractor_version = result, "test-completion"

        async def recover():
            async with store() as recovery:
                claimed = await claim_job(recovery, policy, e2_enabled=True)
                await recovery.commit()
                return claimed

        assert await asyncio.wait_for(recover(), timeout=3) is None
        await completion.commit()
    async with store() as session:
        assert await claim_job(session, policy, e2_enabled=True) is None
        completed = await session.get(KnowledgeJob, job_id)
        assert completed.status == "SUCCEEDED" and completed.attempts == 1
        budget = await session.get(KnowledgeQueueControl, "t:" + datetime.now(UTC).date().isoformat())
        assert budget.tokens_reserved == charge


@pytest.mark.parametrize("fail", [False, True])
async def test_late_success_or_failure_cannot_overwrite_recovered_lease(store, fail):
    entered, resume = asyncio.Event(), asyncio.Event()

    async def enrich(_unit):
        entered.set()
        await resume.wait()
        if fail:
            raise RuntimeError("stale-owner-secret")
        return {"entities": [], "aliases": [], "claims": [], "relations": []}, "test-late-owner"

    policy = QueuePolicy()
    async with store() as session:
        version = await seed(session)
        unit = (await rebuild_knowledge_version(session, version))[0]
        job = await enqueue(session, version, "t", "E2", unit.input_checksum, policy, unit_id=unit.id)
        job_id = job.id
        await session.commit()
    worker = KnowledgeWorker(store, SimpleNamespace(), policy=policy, enrich=enrich)
    task = asyncio.create_task(worker.run_one())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with store() as session:
            previous = await session.get(KnowledgeJob, job_id)
            previous.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            recovered = await claim_job(session, policy, e2_enabled=True)
            token = recovered.lease_token
            await session.commit()
        resume.set()
        assert await asyncio.wait_for(task, timeout=3)
        async with store() as session:
            current = await session.get(KnowledgeJob, job_id)
            assert current.status == "RUNNING" and current.lease_token == token and current.attempts == 2
            assert current.result_json is None and current.error_code is None
            assert (await session.get(DocumentVersion, version)).search_status == "SEARCH_READY"
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_rebuild_after_erasing_derived_stores_matches_candidate(store):
    async with store() as session:
        version = await seed(session)
        units = await rebuild_knowledge_version(session, version)
        original = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        identity = original.tree_version
        for model in (
            KnowledgeTreeNode,
            TreeManifest,
            KnowledgeUnitEdge,
            KnowledgeGraphBuild,
            KnowledgeJob,
            KnowledgeEvidence,
            KnowledgeUnit,
        ):
            await session.execute(delete(model))
        await session.commit()
        units = await rebuild_knowledge_version(session, version)
        rebuilt = await build_knowledge_candidate(
            session, tenant_id="t", project_id="p", queries=gold(units), config=CONFIG
        )
        assert rebuilt.tree_version == identity


async def test_simultaneous_workers_respect_persisted_global_concurrency(store):
    policy = QueuePolicy(concurrency=1)
    async with store() as session:
        version = await seed(session)
        _, _, _, digest = await version_input(session, version, KnowledgeConfig())
        await enqueue(session, version, "t", "FOUNDATION", digest, policy)
        await enqueue(session, version, "t", "FOUNDATION", "other", policy)
        await session.commit()

    async def claim():
        async with store() as session:
            job = await claim_job(session, policy, e2_enabled=False)
            await session.commit()
            return job.id if job else None

    results = await asyncio.gather(claim(), claim())
    assert sum(r is not None for r in results) == 1


async def test_postgres_search_lane_can_finish_and_reparse_during_knowledge_lag(store, monkeypatch):
    from sag_api.services import knowledge_service

    async with store() as session:
        if session.bind.dialect.name != "postgresql":
            pytest.skip("Requires PostgreSQL independent row locks")
        version_id = await seed(session)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = knowledge_service.unit_checksum

    async def delayed_checksum(session, unit):
        if not entered.is_set():
            entered.set()
            await resume.wait()
        return await original(session, unit)

    monkeypatch.setattr(knowledge_service, "unit_checksum", delayed_checksum)

    async def slow_rebuild():
        async with store() as session:
            await rebuild_knowledge_version(session, version_id)
            await session.commit()

    task = asyncio.create_task(slow_rebuild())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with store() as session:
            version = await session.get(DocumentVersion, version_id)
            version.search_status = "SEARCH_READY"
            version.search_ready_at = NOW + timedelta(seconds=1)
            await asyncio.wait_for(session.commit(), timeout=3)
            await asyncio.wait_for(
                parse_and_persist_document_content(
                    session, version_id, "# Changed\n\nRedis CACHE_TTL is 90 seconds for DATN-42."
                ),
                timeout=3,
            )
            await asyncio.wait_for(session.commit(), timeout=3)
        resume.set()
        with pytest.raises(ValueError, match="changed during knowledge rebuild"):
            await task
        async with store() as session:
            version = await session.get(DocumentVersion, version_id)
            assert version.search_status == "SEARCH_READY" and version.knowledge_status == "NOT_STARTED"
            assert not await session.scalar(select(KnowledgeUnit.id))
    finally:
        resume.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


async def test_validated_e2_artifacts_replay_after_derived_store_loss_without_model_cost(store):
    calls = 0

    async def enrich(unit):
        nonlocal calls
        calls += 1
        return {
            "entities": [
                {
                    "quote": unit.text[:5],
                    "start": 0,
                    "end": 5,
                    "block_id": unit.provenance_json["evidence_block_ids"][0],
                    "confidence": 0.8,
                    "label": "Redis",
                }
            ],
            "aliases": [],
            "claims": [],
            "relations": [],
        }, "test-e2-artifact-v1"

    async with store() as session:
        version = await seed(session)
        units = await rebuild_knowledge_version(session, version)
        unit = units[0]
        job = await enqueue(session, version, "t", "E2", unit.input_checksum, QueuePolicy(), unit_id=unit.id)
        await session.commit()
    worker = KnowledgeWorker(
        store, SimpleNamespace(knowledge_e2_url="", knowledge_e2_api_key=""), policy=QueuePolicy(), enrich=enrich
    )
    assert await worker.run_one()
    async with store() as session:
        job = await session.get(KnowledgeJob, job.id)
        assert job.status == "SUCCEEDED" and job.result_json
        unit = await session.get(KnowledgeUnit, unit.id)
        original_checksum = unit.checksum
        await session.execute(delete(KnowledgeEvidence))
        await session.execute(delete(KnowledgeUnit))
        await session.commit()
        rebuilt = await rebuild_knowledge_version(session, version)
        assert next(u for u in rebuilt if u.id == unit.id).checksum == original_checksum
        assert calls == 1
        stale_job = await enqueue(session, version, "t", "E2", "stale", QueuePolicy(), unit_id=unit.id)
        await session.commit()
    assert await worker.run_one()
    async with store() as session:
        assert calls == 1  # Stale input is denied before sending source text to the model.
        assert (await session.get(KnowledgeJob, stale_job.id)).error_code == "ValueError"
        assert (await session.get(DocumentVersion, version)).search_status == "SEARCH_READY"
