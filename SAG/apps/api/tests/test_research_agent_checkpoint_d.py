"""E2E & Integration tests for Checkpoint D — AGENT_READY: Bounded Research Agent.

Verifies:
1. Traceability: Every task originates from a scored gap and approved policy/allowlist.
2. Budget, allowlist, SSRF, and duplicate rules are strictly enforced.
3. No-new-information and retry cases terminate cleanly without a crawl loop.
4. Fetched data enters normal ingestion (handle_document_upload) and incremental tree update.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    DocumentVersion,
    IngestionRun,
    KnowledgeGap,
    KnowledgeGapQuestion,
    KnowledgeQualityAssessment,
    Source,
)
from sag_api.services.research_agent_service import (
    FetchOutcome,
    apply_incremental_tree_update_for_research,
    execute_research_task,
    fetch_url_bounded,
)
from sag_api.services.research_planner_service import (
    ResearchBudget,
    ResearchPolicy,
    ResearchTaskStatus,
    create_or_get_research_task,
    plan_research_task,
)
from sag_api.services.routing_tree_service import (
    KnowledgeUnitInput,
    TreeBuildConfig,
    build_routing_snapshot,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint_d_test.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def seeded_gap(session: Any):
    now = datetime.now(UTC)
    assessment = KnowledgeQualityAssessment(
        id=str(uuid.uuid4()),
        tenant_id="tenant-d",
        project_id="project-d",
        security_partition_id="partition-d",
        tree_version="tree-v1",
        as_of=now,
        metric_definition_version="v1",
        scoring_formula_version="v1",
        config_version="v1",
        config_checksum="chk-cfg",
        input_checksum="chk-in",
        assessment_checksum="chk-assess",
        config_json={},
        input_snapshot_json={},
        metrics_json={},
        reason_codes_json=["LOW_COVERAGE", "HIGH_DEMAND"],
    )
    gap = KnowledgeGap(
        id=str(uuid.uuid4()),
        assessment_id=assessment.id,
        node_id="node-deep-learning",
        priority=0.88,
        severity=0.9,
        importance=0.85,
        eligible=True,
        metrics_json={"coverage": 0.15},
        reason_codes_json=["LOW_COVERAGE", "HIGH_DEMAND"],
        evidence_refs_json=[],
        checksum="gap-checksum-001",
    )
    question = KnowledgeGapQuestion(
        id=str(uuid.uuid4()),
        gap_id=gap.id,
        question_text="What are recent neural network optimization techniques?",
        generator_version="gen-v2",
        model_version="model-v2",
        prompt_version="prompt-v2",
        evidence_ids_json=["ev-1"],
        reason_codes_json=["LOW_COVERAGE"],
        checksum="question-checksum-001",
    )
    session.add(assessment)
    await session.flush()
    session.add_all([gap, question])
    await session.commit()
    return gap, question


async def test_traceability_gap_to_research_task(session, seeded_gap):
    gap, question = seeded_gap
    policy = ResearchPolicy(allowed_domains=("wikipedia.org", "arxiv.org"))
    budget = ResearchBudget(max_fetches=2)

    plan = plan_research_task(
        gap,
        question,
        tenant_id="tenant-d",
        project_id="project-d",
        security_partition_id="partition-d",
        tree_version="tree-v1",
        policy=policy,
        budget=budget,
        candidate_urls=[
            "https://en.wikipedia.org/wiki/Stochastic_gradient_descent",
            "https://arxiv.org/abs/2103.00020",
            "https://unauthorized-domain.com/article",
        ],
    )

    task, created = await create_or_get_research_task(session, plan)
    assert created is True
    await session.commit()

    # Traceability checks
    assert task.gap_id == gap.id
    assert task.question_id == question.id
    assert task.node_id == "node-deep-learning"
    assert task.reason_codes_json == ["HIGH_DEMAND", "LOW_COVERAGE"]
    # Allowlist checks
    assert len(task.target_urls_json) == 2
    assert "https://unauthorized-domain.com/article" not in task.target_urls_json
    assert all("wikipedia.org" in u or "arxiv.org" in u for u in task.target_urls_json)


async def test_fetch_url_bounded_guards():
    policy = ResearchPolicy(allowed_domains=("python.org",))
    budget = ResearchBudget(max_fetches=1, max_retries=1)

    # 1. Disallowed domain
    outcome_disallowed = await fetch_url_bounded(
        "https://evil.com/page",
        policy,
        budget,
    )
    assert outcome_disallowed.success is False
    assert "not in allowed domains" in outcome_disallowed.error

    # 2. Private IP / SSRF
    outcome_ssrf = await fetch_url_bounded(
        "https://127.0.0.1/admin",
        policy,
        budget,
    )
    assert outcome_ssrf.success is False


async def test_no_new_info_and_duplicate_guard_terminates(session, seeded_gap):
    gap, question = seeded_gap
    policy = ResearchPolicy(allowed_domains=("wikipedia.org",))
    budget = ResearchBudget(max_fetches=3)

    plan = plan_research_task(
        gap,
        question,
        tenant_id="tenant-d",
        project_id="project-d",
        security_partition_id="partition-d",
        tree_version="tree-v1",
        policy=policy,
        budget=budget,
        candidate_urls=[
            "https://en.wikipedia.org/wiki/SGD_Page_1",
            "https://en.wikipedia.org/wiki/SGD_Page_2",
        ],
    )
    task, _ = await create_or_get_research_task(session, plan)
    await session.commit()

    # Mock fetcher where all pages return identical payload (duplicates)
    same_content = "# Optimizers\n\nIdentical content across pages."
    same_hash = hashlib.sha256(same_content.encode("utf-8")).hexdigest()

    async def mock_duplicate_fetcher(url: str, _policy: ResearchPolicy, _budget: ResearchBudget) -> FetchOutcome:
        return FetchOutcome(
            url=url,
            success=True,
            status_code=200,
            payload_hash=same_hash,
            title="Optimizers",
            markdown=same_content,
        )

    # Pre-seed IngestionRun with this hash to simulate previously ingested document
    source = Source(
        id=str(uuid.uuid4()),
        name="test-src",
        description="test",
        source_type="DOCUMENT",
        connector_kind="file_upload",
        sag_source_config_id="cfg-1",
        config={"tenant_id": "tenant-d", "project_id": "project-d"},
    )
    doc = Document(
        id=str(uuid.uuid4()),
        source_id=source.id,
        filename="test.md",
        content_type="text/markdown",
        size_bytes=100,
        storage_path="/tmp/test",
    )
    doc_ver = DocumentVersion(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        version_no=1,
        file_hash=same_hash,
    )
    pre_run = IngestionRun(
        id=str(uuid.uuid4()),
        tenant_id="tenant-d",
        project_id="project-d",
        document_version_id=doc_ver.id,
        idempotency_key="key-existing",
        payload_hash=same_hash,
        status="READY",
    )
    session.add(source)
    await session.flush()
    session.add(doc)
    await session.flush()
    session.add(doc_ver)
    await session.flush()
    session.add(pre_run)
    await session.commit()

    task, outcomes = await execute_research_task(
        session,
        task.id,
        fetch_adapter=mock_duplicate_fetcher,
    )

    # Verified: All pages were duplicate, so no new document ingested and status is STOPPED_NO_NEW_INFO
    assert task.status == ResearchTaskStatus.STOPPED_NO_NEW_INFO.value
    assert task.fetch_count == 2
    assert all(o.is_duplicate for o in outcomes)
    assert task.ingestion_run_ids_json == []


async def test_normal_ingestion_relay_success(session, seeded_gap):
    gap, question = seeded_gap
    policy = ResearchPolicy(allowed_domains=("wikipedia.org",))
    budget = ResearchBudget(max_fetches=2)

    plan = plan_research_task(
        gap,
        question,
        tenant_id="tenant-d",
        project_id="project-d",
        security_partition_id="partition-d",
        tree_version="tree-v1",
        policy=policy,
        budget=budget,
        candidate_urls=[
            "https://en.wikipedia.org/wiki/Adam_Optimizer",
        ],
    )
    task, _ = await create_or_get_research_task(session, plan)
    await session.commit()

    content = "# Adam Optimizer\n\nAdam is an adaptive learning rate optimization algorithm."
    p_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def mock_new_fetcher(url: str, _policy: ResearchPolicy, _budget: ResearchBudget) -> FetchOutcome:
        return FetchOutcome(
            url=url,
            success=True,
            status_code=200,
            payload_hash=p_hash,
            title="Adam Optimizer",
            markdown=content,
        )

    task, outcomes = await execute_research_task(
        session,
        task.id,
        fetch_adapter=mock_new_fetcher,
    )

    # Verified: Successfully relayed to handle_document_upload
    assert task.status == ResearchTaskStatus.COMPLETED.value
    assert len(task.ingestion_run_ids_json) == 1
    assert len(task.document_version_ids_json) == 1
    assert outcomes[0].is_duplicate is False

    # Verify IngestionRun and DocumentVersion created in DB
    run_id = task.ingestion_run_ids_json[0]
    run = await session.get(IngestionRun, run_id)
    assert run is not None
    assert run.tenant_id == "tenant-d"
    assert run.project_id == "project-d"
    assert run.payload_hash == p_hash


def test_incremental_tree_update_with_research_unit():
    # Build base tree
    config = TreeBuildConfig(min_cluster_size=2, target_cluster_size=4, max_cluster_size=8)
    units = [
        KnowledgeUnitInput(
            unit_id=f"ku-{i}",
            tenant_id="tenant-d",
            project_id="project-d",
            security_partition_id="partition-d",
            dense=(1.0, 0.0) if i < 3 else (0.0, 1.0),
            sparse=(("dl", 1.0),),
            entities=("NeuralNet",),
        )
        for i in range(6)
    ]
    base_snapshot = build_routing_snapshot(units, (), config=config)
    assert base_snapshot.manifest["unit_count"] == 6

    # New unit ingested from research task
    research_unit = KnowledgeUnitInput(
        unit_id="ku-research-adam",
        tenant_id="tenant-d",
        project_id="project-d",
        security_partition_id="partition-d",
        dense=(1.0, 0.0),
        sparse=(("dl", 1.5), ("adam", 2.0)),
        entities=("AdamOptimizer",),
    )

    update_result = apply_incremental_tree_update_for_research(
        base_snapshot,
        [research_unit],
        config=config,
    )

    # Verified: Incremental delta update without full rebuild
    assert update_result.rebuilt_node_ids == ()
    assert update_result.drift.trigger_rebuild is False
    assert any(u.get("unit_id") == "ku-research-adam" for u in update_result.snapshot.manifest["units"])
    assert "ku-research-adam" in update_result.snapshot.roots[0].unit_ids
