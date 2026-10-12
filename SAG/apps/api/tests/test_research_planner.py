"""Unit tests for Research Planner, URL allowlist, budget caps, and gap detection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    KnowledgeGap,
    KnowledgeGapQuestion,
    KnowledgeQualityAssessment,
)
from sag_api.services.research_planner_service import (
    ResearchBudget,
    ResearchPolicy,
    ResearchTaskStatus,
    create_or_get_research_task,
    detect_eligible_gaps,
    is_url_allowed,
    normalize_url,
    plan_research_task,
)


@pytest.fixture
async def test_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'planner_test.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_normalize_url():
    assert normalize_url("HTTPS://Docs.Python.org/3/library/#math") == "https://docs.python.org/3/library"
    assert normalize_url("https://example.com:443/page/") == "https://example.com/page"
    assert normalize_url("http://example.com:80/") == "http://example.com/"
    assert normalize_url("   https://example.com?q=1   ") == "https://example.com?q=1"
    assert normalize_url("") == ""


def test_is_url_allowed():
    policy = ResearchPolicy(
        allowed_domains=("python.org", "wikipedia.org"),
        allow_subdomains=True,
        require_https=True,
    )
    # Allowed
    assert is_url_allowed("https://python.org/doc", policy) is True
    assert is_url_allowed("https://docs.python.org/3/", policy) is True
    assert is_url_allowed("https://en.wikipedia.org/wiki/AI", policy) is True

    # Disallowed: HTTP when HTTPS required
    assert is_url_allowed("http://python.org/doc", policy) is False

    # Disallowed: Domain not in allowlist
    assert is_url_allowed("https://malicious.org/hack", policy) is False
    assert is_url_allowed("https://google.com/search", policy) is False

    # Disallowed: SSRF / Private IPs / Localhost
    assert is_url_allowed("https://localhost/admin", policy) is False
    assert is_url_allowed("https://127.0.0.1:8000/", policy) is False
    assert is_url_allowed("https://192.168.1.1/secret", policy) is False
    assert is_url_allowed("https://10.0.0.1/", policy) is False

    # Disallowed: Subdomains disabled
    strict_policy = ResearchPolicy(
        allowed_domains=("python.org",),
        allow_subdomains=False,
        require_https=True,
    )
    assert is_url_allowed("https://python.org/about", strict_policy) is True
    assert is_url_allowed("https://docs.python.org/about", strict_policy) is False


def test_plan_research_task_determinism_and_budget_caps():
    gap = KnowledgeGap(
        id="gap-123",
        assessment_id="assess-1",
        node_id="node-ai-research",
        priority=0.85,
        severity=0.9,
        importance=0.8,
        eligible=True,
        metrics_json={"coverage": 0.2},
        reason_codes_json=["LOW_COVERAGE", "HIGH_DEMAND"],
        evidence_refs_json=[],
        checksum="checksum-gap-123",
    )
    question = KnowledgeGapQuestion(
        id="q-456",
        gap_id="gap-123",
        question_text="What are recent breakthroughs in transformer models?",
        generator_version="gen-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
        evidence_ids_json=["ev-1"],
        reason_codes_json=["LOW_COVERAGE"],
        checksum="checksum-q-456",
    )

    policy = ResearchPolicy(allowed_domains=("wikipedia.org", "python.org"))
    budget = ResearchBudget(max_fetches=2)

    candidates = [
        "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
        "https://docs.python.org/3/whatsnew/index.html",
        "https://disallowed.com/spam",
        "https://en.wikipedia.org/wiki/Attention_mechanism",
    ]

    plan1 = plan_research_task(
        gap,
        question,
        tenant_id="t1",
        project_id="p1",
        security_partition_id="part1",
        tree_version="tv1",
        policy=policy,
        budget=budget,
        candidate_urls=candidates,
    )

    # Budget strictly limits target_urls to max_fetches (2) and filters out disallowed domain
    assert len(plan1.target_urls) == 2
    assert "https://disallowed.com/spam" not in plan1.target_urls
    assert plan1.target_urls[0] == "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)"
    assert plan1.target_urls[1] == "https://docs.python.org/3/whatsnew/index.html"

    # Determinism: same input gives exact same checksum and plan
    plan2 = plan_research_task(
        gap,
        question,
        tenant_id="t1",
        project_id="p1",
        security_partition_id="part1",
        tree_version="tv1",
        policy=policy,
        budget=budget,
        candidate_urls=candidates,
    )
    assert plan1.checksum == plan2.checksum

    # Ineligible gap cannot be planned
    ineligible_gap = KnowledgeGap(
        id="gap-bad",
        assessment_id="assess-1",
        node_id="node-x",
        priority=None,
        severity=None,
        importance=None,
        eligible=False,
        metrics_json={},
        reason_codes_json=[],
        evidence_refs_json=[],
        checksum="c-bad",
    )
    with pytest.raises(ValueError, match="Cannot plan research task"):
        plan_research_task(
            ineligible_gap,
            None,
            tenant_id="t1",
            project_id="p1",
            security_partition_id="part1",
            tree_version="tv1",
        )


async def test_detect_eligible_gaps_and_task_persistence(test_session):
    now = datetime.now(UTC)
    assessment = KnowledgeQualityAssessment(
        id="assess-test",
        tenant_id="tenant-1",
        project_id="project-1",
        security_partition_id="partition-1",
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
        reason_codes_json=[],
    )
    gap_eligible = KnowledgeGap(
        id="gap-eligible",
        assessment_id="assess-test",
        node_id="node-1",
        priority=0.8,
        severity=0.8,
        importance=0.8,
        eligible=True,
        metrics_json={},
        reason_codes_json=["LOW_COVERAGE"],
        evidence_refs_json=[],
        checksum="chk-gap-1",
    )
    gap_ineligible = KnowledgeGap(
        id="gap-ineligible",
        assessment_id="assess-test",
        node_id="node-2",
        priority=0.1,
        severity=0.1,
        importance=0.1,
        eligible=False,
        metrics_json={},
        reason_codes_json=[],
        evidence_refs_json=[],
        checksum="chk-gap-2",
    )
    test_session.add(assessment)
    await test_session.flush()
    test_session.add_all([gap_eligible, gap_ineligible])
    await test_session.commit()

    # 1. Detect gaps: only eligible gap with priority >= 0.20 is detected
    detected = await detect_eligible_gaps(
        test_session,
        tenant_id="tenant-1",
        project_id="project-1",
        security_partition_id="partition-1",
        tree_version="tree-v1",
        min_priority=0.20,
    )
    assert len(detected) == 1
    assert detected[0][0].id == "gap-eligible"

    # 2. Plan research task
    plan = plan_research_task(
        gap_eligible,
        None,
        tenant_id="tenant-1",
        project_id="project-1",
        security_partition_id="partition-1",
        tree_version="tree-v1",
        candidate_urls=["https://docs.python.org/3/"],
    )

    # 3. Create research task
    task, created = await create_or_get_research_task(test_session, plan)
    assert created is True
    assert task.status == ResearchTaskStatus.PENDING.value
    assert task.gap_id == "gap-eligible"
    assert task.target_urls_json == ["https://docs.python.org/3"]
    await test_session.commit()

    # 4. Idempotency check: creating again returns existing task
    task_existing, created_again = await create_or_get_research_task(test_session, plan)
    assert created_again is False
    assert task_existing.id == task.id

    # 5. Gap detection now excludes gap-eligible because it already has an ACTIVE task
    detected_after = await detect_eligible_gaps(
        test_session,
        tenant_id="tenant-1",
        project_id="project-1",
        security_partition_id="partition-1",
        tree_version="tree-v1",
        min_priority=0.20,
    )
    assert len(detected_after) == 0
