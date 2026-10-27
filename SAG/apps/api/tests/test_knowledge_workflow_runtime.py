import asyncio
import hashlib
import multiprocessing
import random
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_canonical_ingestion_readiness as fixtures
from sqlalchemy import select

from sag_api.core.config import Settings, settings
from sag_api.core.errors import ApiError, is_retryable_error
from sag_api.db.models import CanonicalBlock, DocumentVersion, Job, KnowledgeJob
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.jobs.knowledge import KnowledgeWorker
from sag_api.services import cpu_worker, knowledge_service, universe_tree_service

ingestion_db = fixtures.ingestion_db


async def ready(factory):
    async with factory() as session:
        version = await session.get(DocumentVersion, "version")
        version.search_status, version.search_ready_at = "SEARCH_READY", datetime.now(UTC)
        session.add(
            CanonicalBlock(
                id="block",
                document_version_id="version",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                normalized_text="The upload size limit is 10 MiB.",
                content_hash=hashlib.sha256(b"The upload size limit is 10 MiB.").hexdigest(),
                section_path="Upload",
                source_anchor="upload",
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("enqueue_fails", [False, True])
async def test_foundation_only_builds_its_version_and_durably_requests_project(
    ingestion_db, monkeypatch, enqueue_fails
):
    await ready(ingestion_db)
    monkeypatch.setattr(
        knowledge_service, "persist_graph", AsyncMock(side_effect=AssertionError("whole-project graph"))
    )
    queue = SimpleNamespace(enqueue=AsyncMock())
    worker = KnowledgeWorker(
        ingestion_db,
        Settings(),
        embed=SimpleNamespace(batch_generate=AsyncMock(return_value=[[1.0, 0.0]])),
        job_queue=queue,
    )
    await worker.discover()
    original = universe_tree_service.enqueue_tree_rebuild_for_version
    if enqueue_fails:
        monkeypatch.setattr(universe_tree_service, "enqueue_tree_rebuild_for_version", AsyncMock(side_effect=OSError()))
    assert await worker.run_one()
    async with ingestion_db() as session:
        job = await session.scalar(select(KnowledgeJob))
        assert job.status == "SUCCEEDED"
        assert (await session.get(DocumentVersion, "version")).knowledge_status == "DATA_READY"
        assert (job.tree_requested_at is None) is enqueue_fails
    monkeypatch.setattr(universe_tree_service, "enqueue_tree_rebuild_for_version", original)
    await worker.discover()
    async with ingestion_db() as session:
        assert (await session.scalar(select(KnowledgeJob))).tree_requested_at is not None
        trees = list((await session.scalars(select(Job).where(Job.type == JobType.REBUILD_UNIVERSE_TREE))).all())
        assert len(trees) == 1 and trees[0].status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_rebuild_running_retains_one_queued_followup(ingestion_db):
    queue = SimpleNamespace(enqueue=AsyncMock())
    async with ingestion_db() as session:
        first = await universe_tree_service.enqueue_project_tree_rebuild(
            session, queue, tenant_id="tenant", project_ids=["project"], reason="first"
        )
        first.status = JobStatus.RUNNING
        await session.commit()
        second = await universe_tree_service.enqueue_project_tree_rebuild(
            session, queue, tenant_id="tenant", project_ids=["project"], reason="changed"
        )
        third = await universe_tree_service.enqueue_project_tree_rebuild(
            session, queue, tenant_id="tenant", project_ids=["project"], reason="changed_again"
        )
        assert first.id != second.id == third.id


def test_prepared_cosine_matches_original_math():
    rng = random.Random(42)
    vectors = [[rng.uniform(-1, 1) for _ in range(32)] for _ in range(8)] + [[0.0] * 32, []]
    for left in vectors:
        for right in vectors:
            assert knowledge_service._prepared_cosine(
                knowledge_service._prepare_dense(left),
                knowledge_service._prepare_dense(right),
            ) == knowledge_service._cosine(left, right)
    with pytest.raises(ValueError):
        knowledge_service._prepare_dense([float("nan")])


@pytest.mark.parametrize("status,expected", [(401, False), (429, True), (503, True)])
def test_retry_survives_domain_wrapper(status, expected):
    request = httpx.Request("POST", "https://provider.invalid")
    root = httpx.HTTPStatusError("failure", request=request, response=httpx.Response(status, request=request))
    wrapped = ApiError("index failed")
    wrapped.__cause__ = root
    assert is_retryable_error(wrapped) is expected
    wrapped.retryable = False
    assert is_retryable_error(wrapped) is False


def test_foundation_deadline_covers_provider_retries_and_admission():
    worker = KnowledgeWorker(None, Settings(embedding_timeout=120, embedding_max_retries=3))
    assert worker.foundation_timeout > 120 * 4 + 120


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cpu_deadline_or_cancellation_reaps_child(monkeypatch, cancel):
    monkeypatch.setattr(settings, "knowledge_graph_timeout_seconds", 0.01 if not cancel else 60)
    before = {p.pid for p in multiprocessing.active_children()}
    task = asyncio.create_task(cpu_worker.run_cpu("graph", {"units": [], "config": {}}))
    if cancel:
        await asyncio.sleep(0.02)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert {p.pid for p in multiprocessing.active_children()} <= before


@pytest.mark.asyncio
async def test_graph_cache_avoids_second_cpu_build_and_checks_evidence(ingestion_db, monkeypatch):
    await ready(ingestion_db)
    calls = 0

    async def calculate(operation, payload):
        nonlocal calls
        calls += 1
        return knowledge_service.graph_payload([SimpleNamespace(**row) for row in payload["units"]])

    monkeypatch.setattr(cpu_worker, "run_cpu", calculate)
    async with ingestion_db() as session:
        units = await knowledge_service.rebuild_knowledge_version(session, "version", extra_entities=())
        build = await knowledge_service.persist_graph(session, units)
        await session.commit()
        assert (await knowledge_service.persist_graph(session, units)).id == build.id
        assert calls == 1
        units[0].features_json = {**units[0].features_json, "entities": ["tampered"]}
        with pytest.raises(ValueError, match="checksum"):
            await knowledge_service.persist_graph(session, units)


@pytest.mark.asyncio
async def test_real_cpu_process_keeps_parent_responsive(monkeypatch):
    from sag_api.services.routing_tree_service import KnowledgeUnitInput, TreeBuildConfig

    monkeypatch.setattr(settings, "knowledge_graph_timeout_seconds", 30)
    task = asyncio.create_task(
        cpu_worker.run_cpu(
            "routing",
            {
                "units": [KnowledgeUnitInput("u", "tenant", "project", "partition", dense=(1.0, 0.0))],
                "config": TreeBuildConfig(min_cluster_size=1, min_partition_units=1),
            },
        )
    )
    intervals = []
    while not task.done():
        started = time.perf_counter()
        await asyncio.sleep(0.02)
        intervals.append(time.perf_counter() - started)
    snapshot = await task
    assert snapshot.publishable
    assert max(intervals) < 0.5


@pytest.mark.asyncio
async def test_document_timeout_retries_then_finishes_without_inline_foundation(ingestion_db, monkeypatch):
    from sag_api.db.models import Document, IngestionRun
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services import search_index_service, universe_service

    attempts = 0

    async def index(session, *, document_version, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("")
        document_version.search_status = "SEARCH_READY"
        document_version.search_ready_at = datetime.now(UTC)
        return [SimpleNamespace(id="unit")]

    monkeypatch.setattr(search_index_service, "run_search_indexing_stage", index)
    monkeypatch.setattr(universe_service, "schedule_universe_refresh", AsyncMock())
    monkeypatch.setattr(knowledge_service, "rebuild_knowledge_version", AsyncMock(side_effect=AssertionError("inline")))
    manager = SimpleNamespace(
        get_sag_embedding=AsyncMock(return_value=None), process_document=AsyncMock(side_effect=AssertionError("legacy"))
    )
    queue = InProcessAsyncQueue(ingestion_db, manager)
    queue._schedule_retry = lambda *args: None
    async with ingestion_db() as session:
        job = await session.get(Job, "job")
        job.status = JobStatus.QUEUED
        await session.commit()
    await queue._run_job("job")
    async with ingestion_db() as session:
        assert (await session.get(Job, "job")).status == JobStatus.QUEUED
        assert (await session.get(Document, "document")).status == DocumentStatus.PENDING
        assert (await session.get(IngestionRun, "run")).status == "QUEUED"
    await queue._run_job("job")
    async with ingestion_db() as session:
        job = await session.get(Job, "job")
        assert job.status == JobStatus.SUCCEEDED and job.attempts == 2
        assert (await session.get(Document, "document")).status == DocumentStatus.READY
    knowledge_service.rebuild_knowledge_version.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_snapshot_carries_graph_and_rejects_changed_input(ingestion_db):

    from sag_api.services.knowledge_candidate_service import build_project_routing_snapshot
    from sag_api.services.routing_tree_service import TreeBuildConfig
    from sag_api.services.tree_publish_service import StaleTreePublish, _verify_knowledge_inputs

    await ready(ingestion_db)
    async with ingestion_db() as session:
        units = await knowledge_service.rebuild_knowledge_version(
            session, "version", extra_entities=(), dense_by_block_id={"block": [1.0, 0.0]}
        )
        await session.commit()
        snapshot, _, graph, _ = await build_project_routing_snapshot(
            session,
            tenant_id="tenant",
            project_id="project",
            queries=[],
            config=TreeBuildConfig(min_cluster_size=1, min_partition_units=1),
        )
        assert snapshot.manifest["knowledge_graph_build_id"] == graph.id
        candidate = SimpleNamespace(manifest_json=snapshot.manifest_json, tenant_id="tenant", project_id="project")
        await _verify_knowledge_inputs(session, candidate)
        units[0].checksum = "changed"
        await session.flush()
        with pytest.raises(StaleTreePublish):
            await _verify_knowledge_inputs(session, candidate)


@pytest.mark.asyncio
async def test_additive_schema_upgrade_preserves_existing_data(tmp_path, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from sag_api.core import db

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "publish_lock_engine", engine)
    try:
        await db.init_db()
        async with engine.begin() as connection:
            await connection.execute(text("DROP INDEX ix_knowledge_graph_input"))
            await connection.execute(text("ALTER TABLE knowledge_graph_builds DROP COLUMN input_fingerprint"))
            await connection.execute(text("ALTER TABLE knowledge_jobs DROP COLUMN tree_requested_at"))
            await connection.execute(text("ALTER TABLE canonical_blocks DROP COLUMN dedup_signature_json"))
        await db.init_db()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT input_fingerprint FROM knowledge_graph_builds"))
            await connection.execute(text("SELECT tree_requested_at FROM knowledge_jobs"))
            await connection.execute(text("SELECT dedup_signature_json FROM canonical_blocks"))
    finally:
        await engine.dispose()


def test_minhash_optimization_preserves_all_permutations():
    from sag_api.services.dedup_and_temporal_service import compute_minhash_signature

    rng = random.Random(123)
    for tokens in (
        [],
        ["không"],
        ["a", "a"],
        ["thần", "kinh", "tri", "thức", "não", "bộ"],
        [str(rng.randrange(100)) for _ in range(500)],
    ):
        k = min(5, len(tokens))
        shingles = {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)} if tokens else set()
        expected = [
            min((int(hashlib.md5(f"{i}:{s}".encode()).hexdigest()[:8], 16) for s in shingles), default=0)
            for i in range(128)
        ]
        assert compute_minhash_signature(tokens) == expected


@pytest.mark.asyncio
async def test_minhash_persistent_cache_reuses_and_rejects_corruption(monkeypatch):
    from sag_api.services import dedup_and_temporal_service as dedup

    dedup._MINHASH_CACHE.clear()
    calls = 0

    async def calculate(operation, payload):
        nonlocal calls
        calls += 1
        return cpu_worker._calculate(operation, payload)

    monkeypatch.setattr(cpu_worker, "run_cpu", calculate)
    block = SimpleNamespace(
        id="b", normalized_text="Không được tự động gộp các bằng chứng trái ngược.", dedup_signature_json=None
    )
    expected = await dedup._block_signatures([block], persist_ids={"b"})
    dedup._MINHASH_CACHE.clear()
    assert await dedup._block_signatures([block], persist_ids={"b"}) == expected
    assert calls == 1
    block.dedup_signature_json["signature"] = [0] * 128
    assert await dedup._block_signatures([block], persist_ids={"b"}) == expected
    # A valid process cache can repair a corrupt stored derivative.
    assert calls == 1
    dedup._MINHASH_CACHE.clear()
    block.dedup_signature_json["content_hash"] = "wrong"
    assert await dedup._block_signatures([block], persist_ids={"b"}) == expected
    assert calls == 2


def test_routing_profile_matches_original_medoid_arithmetic():
    from sag_api.services.routing_tree_service import KnowledgeUnitInput, _cosine, _profile

    rng = random.Random(14)
    vectors = [tuple(rng.uniform(-1, 1) for _ in range(32)) for _ in range(70)]
    vectors += [(0.0,) * 32, (), (1e300, -1e300)]
    for count in (1, 15, 64, len(vectors)):
        units = [
            KnowledgeUnitInput(str(i), "tenant", "project", "partition", dense=v) for i, v in enumerate(vectors[:count])
        ]
        candidates = sorted(
            ((u.unit_id, u.dense) for u in units if u.dense), key=lambda item: hashlib.sha256(item[0].encode()).digest()
        )[:64]
        expected = min(
            (vector for _, vector in candidates),
            key=lambda candidate: (-sum_original_cosines(candidate, units, _cosine), candidate),
        )
        assert _profile(units, 64).dense_medoid == expected


def sum_original_cosines(candidate, units, cosine):
    import math

    return math.fsum(cosine(candidate, unit.dense) for unit in units if unit.dense)


@pytest.mark.asyncio
async def test_cpu_admission_wait_has_a_deadline(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_graph_timeout_seconds", 0.03)
    slot = asyncio.Semaphore(0)
    cpu_worker._SLOTS[asyncio.get_running_loop()] = slot
    with pytest.raises(TimeoutError):
        await cpu_worker.run_cpu("graph", {})
    assert slot.locked()


@pytest.mark.asyncio
async def test_tree_dependency_deferral_does_not_spend_attempt(ingestion_db):
    from sag_api.jobs.inproc import InProcessAsyncQueue

    queue = InProcessAsyncQueue(ingestion_db, SimpleNamespace())
    queue._schedule_retry = lambda *args: None
    async with ingestion_db() as session:
        tree = await universe_tree_service.enqueue_project_tree_rebuild(
            session, SimpleNamespace(enqueue=AsyncMock()), tenant_id="tenant", project_ids=["project"], reason="batch"
        )
        tree_id = tree.id
    await queue._run_job(tree_id)
    async with ingestion_db() as session:
        tree = await session.get(Job, tree_id)
        assert tree.status == JobStatus.QUEUED and tree.attempts == 0
        ingest = await session.get(Job, "job")
        ingest.status = JobStatus.SUCCEEDED
        session.add(
            KnowledgeJob(
                id="foundation",
                tenant_id="tenant",
                document_version_id="version",
                kind="FOUNDATION",
                input_checksum="checksum",
                status="QUEUED",
                priority=1,
            )
        )
        await session.commit()
    await queue._run_job(tree_id)
    async with ingestion_db() as session:
        tree = await session.get(Job, tree_id)
        assert tree.status == JobStatus.QUEUED and tree.attempts == 0


@pytest.mark.asyncio
async def test_tree_waits_for_search_ready_version_before_foundation_discovery(ingestion_db):
    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobDeferred

    await ready(ingestion_db)
    async with ingestion_db() as session:
        (await session.get(Job, "job")).status = JobStatus.SUCCEEDED
        await session.commit()
        tree = Job(type=JobType.REBUILD_UNIVERSE_TREE, payload={"tenant_id": "tenant", "project_ids": ["project"]})
        with pytest.raises(JobDeferred):
            await tasks.rebuild_universe_tree(session, tree, engine_manager=None)


@pytest.mark.asyncio
async def test_empty_project_clears_active_pointer(ingestion_db, monkeypatch):
    from sag_api.db.models import ProjectSearchState, TreeManifest
    from sag_api.services import knowledge_candidate_service

    monkeypatch.setattr(universe_tree_service, "build_project_routing_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(knowledge_candidate_service, "current_units", AsyncMock(return_value=[]))
    async with ingestion_db() as session:
        manifest = TreeManifest(
            tree_version="tree",
            project_id="project",
            config_version="v1",
            node_count=1,
            leaf_count=1,
            max_leaf_size=1,
            giant_ratio=0,
            routing_recall_at_k=1,
            escape_win_rate=0,
            acl_blackhole_rate=0,
            status="ACTIVE",
            checksum="sum",
        )
        session.add(manifest)
        session.add(ProjectSearchState(project_id="project", active_tree_version="tree", active_search_epoch=2))
        await session.commit()
        assert (
            await universe_tree_service.rebuild_project_universe_tree(
                session, None, tenant_id="tenant", project_id="project"
            )
            is None
        )
        state = await session.get(ProjectSearchState, "project")
        assert state.active_tree_version is None and state.previous_tree_version == "tree"
        assert state.active_search_epoch == 3 and manifest.status == "INACTIVE"


@pytest.mark.asyncio
async def test_source_delete_durably_requests_remaining_project_rebuild(ingestion_db, monkeypatch, tmp_path):
    from sag_api.services import source_service, universe_service

    monkeypatch.setattr(universe_service, "schedule_universe_refresh", AsyncMock())
    queue = SimpleNamespace(enqueue=AsyncMock())
    async with ingestion_db() as session:
        await source_service.delete_source(
            session,
            "source",
            engine_manager=SimpleNamespace(release=AsyncMock()),
            upload_dir=str(tmp_path),
            job_queue=queue,
        )
    async with ingestion_db() as session:
        tree = await session.scalar(select(Job).where(Job.type == JobType.REBUILD_UNIVERSE_TREE))
        assert tree.status == JobStatus.QUEUED
        assert tree.payload["tenant_id"] == "tenant" and tree.payload["project_ids"] == ["project"]
        queue.enqueue.assert_awaited_once_with(tree.id)


@pytest.mark.asyncio
async def test_dedup_cpu_preserves_exact_and_contradiction_candidates():
    from sag_api.services import dedup_and_temporal_service as dedup

    current = [
        dict(
            id="exact", normalized_text="exact", content_hash="same", block_type="paragraph", document_version_id="new"
        ),
        dict(
            id="changed",
            normalized_text="not permitted",
            content_hash="changed",
            block_type="paragraph",
            document_version_id="new",
        ),
    ]
    prior = [
        dict(
            id="p1", normalized_text="permitted", content_hash="same", block_type="paragraph", document_version_id="old"
        ),
        dict(
            id="p2",
            normalized_text="permitted",
            content_hash="other",
            block_type="paragraph",
            document_version_id="old",
        ),
    ]
    signatures = {row["id"]: dedup.compute_minhash_signature(row["normalized_text"].split()) for row in current + prior}
    result = await cpu_worker.run_cpu(
        "dedup_candidates",
        dict(
            current=current,
            prior=prior,
            signatures=signatures,
            embeddings={"changed": [1.0, 0.0], "p1": [1.0, 0.0], "p2": [1.0, 0.0]},
            version_id="new",
        ),
    )
    exact, candidates = result
    assert exact == 1 and len(candidates) == 1
    assert candidates[0]["target_block_id"] == "p2"
    assert candidates[0]["relation_type"] == "CONTRADICTS" and candidates[0]["auto_merged"] is False


def test_mmr_compares_each_selected_pair_only_once(monkeypatch):
    from sag_api.sag import RetrievedSection
    from sag_api.services import retrieval_service

    candidates = [
        RetrievedSection(
            chunk_id=str(i),
            document_id="doc",
            heading="policy",
            rank=i,
            score=0.95,
            content=f"Unique evidence policy number {i} and unrelated conclusion {i * 103}",
        )
        for i in range(40)
    ]
    calls = 0
    original = retrieval_service._jaccard

    def compare(left, right):
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr(retrieval_service, "_jaccard", compare)
    selected, stats = retrieval_service.select_diverse_sections("policy", candidates, limit=None)
    assert len(selected) == len(candidates) and stats["deduplicated_count"] == 0
    assert calls <= len(candidates) * (len(candidates) - 1)


@pytest.mark.asyncio
async def test_prepared_tree_cpu_payload_and_checksum_equal_direct_builder():
    from dataclasses import replace

    from sag_api.services.routing_tree_service import KnowledgeUnitInput, TreeBuildConfig, build_routing_snapshot
    from sag_api.services.tree_publish_service import SearchUnitAssignment, prepare_tree_candidate

    snapshot = build_routing_snapshot(
        [KnowledgeUnitInput("u", "tenant", "project", "partition", dense=(1.0, 0.0))],
        config=TreeBuildConfig(min_cluster_size=1, min_partition_units=1),
    )
    assignments = (SearchUnitAssignment(("u",), "su", "source", "version", "partition"),)
    expected = prepare_tree_candidate(snapshot, assignments)
    actual = await cpu_worker.run_cpu(
        "prepare_tree",
        {
            "snapshot": replace(snapshot, metrics=dict(snapshot.metrics), quality_gates=dict(snapshot.quality_gates)),
            "assignments": assignments,
        },
    )
    assert actual == expected
    import json

    assert actual.manifest == json.loads(actual.manifest_json)


@pytest.mark.asyncio
async def test_background_demand_write_survives_normal_database_delay(ingestion_db, monkeypatch):
    from sag_api.services import search_unit_retrieval_service as retrieval

    scope = {
        "project_id": "project",
        "source_ids": ("source",),
        "tenant_id": "tenant",
        "partition_id": "partition",
        "document_version_ids": ("version",),
    }
    group = SimpleNamespace(
        scope_key=("project", "source", "tenant", "partition"),
        tenant_id="tenant",
        project_id="project",
        partition_id="partition",
    )
    route = SimpleNamespace(
        routed=True,
        selected_nodes=("node",),
        project_id="project",
        tree_version="tree",
        acl_scope_fingerprint=retrieval.scope_fingerprint(scope),
    )
    saved = False

    async def write(*args, **kwargs):
        nonlocal saved
        await asyncio.sleep(0.15)
        saved = True

    monkeypatch.setattr(retrieval, "routing_scope_for_group", lambda group: scope)
    monkeypatch.setattr(retrieval, "record_query_demand", write)
    monkeypatch.setattr(retrieval, "SessionLocal", ingestion_db)
    await retrieval._record_routed_query_demand([group], {group.scope_key: route})
    assert saved


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_busy_project_builder_defers_without_exhausting_failure_attempts(ingestion_db, monkeypatch, wrapped):
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.jobs import tasks
    from sag_api.jobs.control import JobDeferred
    from sag_api.services.tree_publish_service import TreePublishInProgress

    busy = TreePublishInProgress("another builder")
    error = ServiceUnavailableError("busy") if wrapped else busy
    if wrapped:
        error.__cause__ = busy
    monkeypatch.setattr(universe_tree_service, "rebuild_project_universe_tree", AsyncMock(side_effect=error))
    async with ingestion_db() as session:
        (await session.get(Job, "job")).status = JobStatus.SUCCEEDED
        await session.commit()
        job = Job(type=JobType.REBUILD_UNIVERSE_TREE, payload={"tenant_id": "tenant", "project_ids": ["project"]})
        for _ in range(5):
            with pytest.raises(JobDeferred):
                await tasks.rebuild_universe_tree(session, job, engine_manager=None)


@pytest.mark.asyncio
async def test_async_diversity_preserves_selection_and_keeps_loop_responsive():
    from sag_api.sag import RetrievedSection
    from sag_api.services import retrieval_service
    from sag_api.services import search_unit_retrieval_service as retrieval

    rng = random.Random(42)
    sections = [
        RetrievedSection(
            chunk_id=str(i),
            document_id=f"doc-{i % 3}",
            heading="policy",
            content=" ".join(f"fact{rng.randrange(10000)}" for _ in range(100)),
            score=0.95,
            rank=i,
        )
        for i in range(80)
    ]
    expected = retrieval_service.select_diverse_sections("policy", sections, limit=None)
    task = asyncio.create_task(
        retrieval._select_diverse_sections(
            "policy", sections, limit=None, deadline=asyncio.get_running_loop().time() + 30
        )
    )
    intervals = []
    while not task.done():
        start = time.perf_counter()
        await asyncio.sleep(0.02)
        intervals.append(time.perf_counter() - start)
    actual = await task
    assert ([s.model_dump() for s in actual[0]], actual[1]) == ([s.model_dump() for s in expected[0]], expected[1])
    assert max(intervals) < 0.5


@pytest.mark.asyncio
async def test_cancelled_diversity_stops_thread_before_return(monkeypatch):
    import threading

    from sag_api.sag import RetrievedSection
    from sag_api.services import retrieval_service
    from sag_api.services import search_unit_retrieval_service as retrieval

    entered = threading.Event()
    calls = []
    original = retrieval_service._content_shingles

    def slow(section):
        entered.set()
        calls.append(section.chunk_id)
        time.sleep(0.03)
        return original(section)

    monkeypatch.setattr(retrieval_service, "_content_shingles", slow)
    sections = [RetrievedSection(chunk_id=str(i), content=f"unique content {i}") for i in range(100)]
    task = asyncio.create_task(
        retrieval._select_diverse_sections(
            "test", sections, limit=None, deadline=asyncio.get_running_loop().time() + 10
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    count_after_cancel = len(calls)
    await asyncio.sleep(0.1)
    assert len(calls) == count_after_cancel < len(sections)


def test_selection_scope_revalidation_rejects_revoked_or_changed_mapping():
    from sag_api.core.errors import ServiceUnavailableError
    from sag_api.services.search_unit_retrieval_service import _verify_selection_scope

    original = SimpleNamespace(
        source_id="s",
        project_id="p",
        tenant_id="t",
        partition_id="part",
        acl_mapping_version=1,
        ingestion_run_id="run",
        index_stage_run_id="index",
        search_manifest_checksum="sum",
    )
    changed = SimpleNamespace(**{**vars(original), "acl_mapping_version": 2})
    _verify_selection_scope({"v"}, {"v": original}, {"v": original})
    with pytest.raises(ServiceUnavailableError):
        _verify_selection_scope({"v"}, {"v": original}, {"v": changed})
    with pytest.raises(ServiceUnavailableError):
        _verify_selection_scope({"v"}, {"v": original}, {})
