from datetime import UTC, datetime, timedelta

import pytest

from sag_api.services.knowledge_quality_service import (
    ContradictionSignal,
    EvidenceSignal,
    KnowledgeQualityConfig,
    QualityTopicInput,
    UnitSignal,
    score_topic,
)

AS_OF = datetime(2026, 10, 12, 12, tzinfo=UTC)


def _topic(
    *,
    units: tuple[UnitSignal, ...] | None = None,
    contradictions: tuple[ContradictionSignal, ...] = (),
    demand_current: int | None = 8,
    demand_previous: int | None = 4,
) -> QualityTopicInput:
    if units is None:
        units = (
            UnitSignal(
                unit_id="unit-a",
                source_id="source-a",
                published_at=AS_OF - timedelta(days=10),
                evidence=(
                    EvidenceSignal(
                        evidence_id="evidence-a",
                        quote="Anchored fact A",
                        locator="page:1",
                        confidence=0.8,
                    ),
                ),
            ),
            UnitSignal(
                unit_id="unit-b",
                source_id="source-a",
                published_at=AS_OF - timedelta(days=20),
                evidence=(),
            ),
        )
    return QualityTopicInput(
        tenant_id="tenant-a",
        project_id="project-a",
        security_partition_id="partition-a",
        tree_version="tree-v1",
        node_id="node-a",
        as_of=AS_OF,
        demand_window_days=7,
        query_demand_current=demand_current,
        query_demand_previous=demand_previous,
        units=units,
        contradictions=contradictions,
    )


def test_scoring_is_reproducible_for_identical_versioned_inputs():
    topic = _topic()
    config = KnowledgeQualityConfig()

    first = score_topic(topic, config)
    second = score_topic(topic, config)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.input_checksum
    assert first.config_version == config.version


def test_more_anchored_evidence_improves_coverage_and_lowers_priority():
    baseline = score_topic(_topic(), KnowledgeQualityConfig())
    completed_units = _topic().units
    improved = score_topic(
        _topic(
            units=(
                completed_units[0],
                completed_units[1].model_copy(
                    update={
                        "evidence": (
                            EvidenceSignal(
                                evidence_id="evidence-b",
                                quote="Anchored fact B",
                                locator="page:2",
                                confidence=0.9,
                            ),
                        )
                    }
                ),
            )
        ),
        KnowledgeQualityConfig(),
    )

    assert improved.metrics.coverage == 1.0
    assert improved.metrics.coverage > baseline.metrics.coverage
    assert improved.priority < baseline.priority


def test_older_evidence_reduces_freshness_and_increases_priority():
    baseline = score_topic(_topic(), KnowledgeQualityConfig())
    old_units = tuple(
        unit.model_copy(update={"published_at": AS_OF - timedelta(days=730)})
        for unit in _topic().units
    )
    older = score_topic(_topic(units=old_units), KnowledgeQualityConfig())

    assert older.metrics.freshness < baseline.metrics.freshness
    assert older.priority > baseline.priority


def test_contradiction_candidate_increases_metric_priority_and_carries_evidence():
    baseline = score_topic(_topic(), KnowledgeQualityConfig())
    conflict = ContradictionSignal(
        relation_id="relation-1",
        source_unit_id="unit-a",
        target_unit_id="unit-b",
        evidence_ids=("evidence-a",),
    )
    contradicted = score_topic(
        _topic(contradictions=(conflict,)),
        KnowledgeQualityConfig(),
    )

    assert contradicted.metrics.contradiction_candidate_rate > 0
    assert contradicted.priority > baseline.priority
    assert "CONTRADICTION_CANDIDATE" in contradicted.reason_codes
    assert contradicted.supporting_evidence_ids == ("evidence-a",)


def test_higher_query_demand_and_growth_increase_priority():
    baseline = score_topic(_topic(), KnowledgeQualityConfig())
    higher_demand = score_topic(
        _topic(demand_current=80, demand_previous=4),
        KnowledgeQualityConfig(),
    )

    assert higher_demand.metrics.query_demand > baseline.metrics.query_demand
    assert higher_demand.metrics.demand_growth > baseline.metrics.demand_growth
    assert higher_demand.priority > baseline.priority


def test_missing_query_demand_is_unknown_and_not_question_eligible():
    result = score_topic(
        _topic(demand_current=None, demand_previous=None),
        KnowledgeQualityConfig(),
    )

    assert result.metrics.query_demand is None
    assert result.priority is None
    assert not result.eligible
    assert "QUERY_DEMAND_UNAVAILABLE" in result.reason_codes


def test_missing_freshness_timestamp_keeps_priority_unknown():
    units = tuple(
        unit.model_copy(
            update={
                "published_at": None,
                "observed_at": None,
                "ingested_at": None,
            }
        )
        for unit in _topic().units
    )

    result = score_topic(_topic(units=units), KnowledgeQualityConfig())

    assert result.metrics.freshness is None
    assert result.severity is None
    assert result.priority is None
    assert not result.eligible
    assert "FRESHNESS_TIMESTAMP_UNAVAILABLE" in result.reason_codes


def test_invalid_contradiction_reference_is_rejected():
    conflict = ContradictionSignal(
        relation_id="relation-1",
        source_unit_id="unit-a",
        target_unit_id="unit-b",
        evidence_ids=("unknown-evidence",),
    )

    with pytest.raises(ValueError, match="contradiction evidence is outside topic scope"):
        score_topic(_topic(contradictions=(conflict,)), KnowledgeQualityConfig())
