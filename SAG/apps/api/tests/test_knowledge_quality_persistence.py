import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    Document,
    KnowledgeGap,
    KnowledgeGapQuestion,
    KnowledgeQualityAssessment,
    KnowledgeQueryDemandDaily,
    KnowledgeTreeNode,
    Source,
    SourceProjectMapping,
    TreeManifest,
)
from sag_api.services.knowledge_gap_question_service import generate_question_for_gap
from sag_api.services.knowledge_quality_persistence import (
    persist_scoring_results,
    query_demand_totals,
    record_query_demand,
)
from sag_api.services.knowledge_quality_repository import (
    build_topic_inputs,
    confirmed_source_ids_for_quality_scope,
)
from sag_api.services.knowledge_quality_service import (
    EvidenceSignal,
    KnowledgeQualityConfig,
    QualityTopicInput,
    UnitSignal,
    score_topic,
)
from sag_api.services.knowledge_service import checksum


@pytest.fixture
async def quality_store(tmp_path):
    url = os.getenv("SAG_KNOWLEDGE_TEST_DATABASE_URL")
    schema = "quality_test_" + uuid.uuid4().hex
    bootstrap = None
    if url:
        bootstrap = create_async_engine(url)
        async with bootstrap.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "quality.db"))

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()
        if bootstrap:
            async with bootstrap.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await bootstrap.dispose()


def _scored_topic():
    as_of = datetime(2026, 10, 12, tzinfo=UTC)
    topic = QualityTopicInput(
        tenant_id="tenant-a",
        project_id="project-a",
        security_partition_id="partition-a",
        tree_version="tree-v1",
        node_id="node-a",
        as_of=as_of,
        demand_window_days=7,
        query_demand_current=350,
        query_demand_previous=0,
        units=(
            UnitSignal(
                unit_id="unit-a",
                source_id="source-a",
                published_at=as_of - timedelta(days=900),
                evidence=(
                    EvidenceSignal(
                        evidence_id="evidence-a",
                        quote="Anchored supporting quote",
                        locator="page:2",
                        confidence=0.4,
                    ),
                ),
            ),
            UnitSignal(
                unit_id="unit-b",
                source_id="source-a",
                published_at=as_of - timedelta(days=900),
                evidence=(),
            ),
        ),
    )
    return topic


def _scored_result():
    return score_topic(_scored_topic(), KnowledgeQualityConfig(minimum_priority=0.0))


def _scored_snapshot():
    topic = _scored_topic()
    evidence_refs = [
        {
            "evidence_id": evidence.evidence_id,
            "unit_id": unit.unit_id,
            "source_id": unit.source_id,
            "quote": evidence.quote,
            "locator": evidence.locator,
        }
        for unit in topic.units
        for evidence in unit.evidence
        if evidence.anchored
    ]
    return {
        "node-a": {
            "scoring_input": topic.model_dump(mode="json"),
            "tenant_id": topic.tenant_id,
            "project_id": topic.project_id,
            "security_partition_id": topic.security_partition_id,
            "tree_version": topic.tree_version,
            "tree_manifest_checksum": topic.tree_manifest_checksum,
            "node_profile_checksum": topic.node_profile_checksum,
            "unit_ids": sorted(unit.unit_id for unit in topic.units),
            "evidence_ids": sorted(evidence.evidence_id for unit in topic.units for evidence in unit.evidence),
            "evidence_refs": evidence_refs,
            "contradiction_relation_ids": sorted(
                relation.relation_id for relation in topic.contradictions
            ),
            "query_demand_current": topic.query_demand_current,
            "query_demand_previous": topic.query_demand_previous,
            "demand_window_days": topic.demand_window_days,
            "as_of": topic.as_of.astimezone(UTC).isoformat(),
        }
    }


@pytest.mark.asyncio
async def test_query_demand_is_scoped_daily_aggregate_and_keeps_no_query_text(quality_store):
    async with quality_store() as session:
        occurred_at = datetime(2026, 10, 12, 8, tzinfo=UTC)
        await record_query_demand(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_ids=("node-a", "node-b", "node-a"),
            occurred_at=occurred_at,
        )
        await record_query_demand(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_ids=("node-a",),
            occurred_at=occurred_at + timedelta(minutes=1),
        )
        await record_query_demand(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_ids=("node-a",),
            occurred_at=occurred_at - timedelta(days=7),
        )
        await session.commit()

        rows = (await session.scalars(select(KnowledgeQueryDemandDaily))).all()
        assert {(row.node_id, row.request_count) for row in rows} == {
            ("node-a", 2),
            ("node-b", 1),
            ("node-a", 1),
        }
        assert not any(hasattr(row, "query_text") for row in rows)

        current, previous = await query_demand_totals(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_id="node-a",
            as_of=datetime(2026, 10, 12, 12, tzinfo=UTC),
            window_days=7,
        )
        assert current == 2
        assert previous == 1

        current, previous = await query_demand_totals(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_id="node-b",
            as_of=datetime(2026, 10, 12, 12, tzinfo=UTC),
            window_days=7,
        )
        assert current == 1
        assert previous is None

        current, previous = await query_demand_totals(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            node_id="node-c",
            as_of=datetime(2026, 10, 12, 12, tzinfo=UTC),
            window_days=7,
        )
        assert current is None
        assert previous is None


@pytest.mark.asyncio
async def test_quality_scope_uses_only_confirmed_active_source_mappings(quality_store):
    async with quality_store() as session:
        session.add_all(
            [
                Source(id="source-confirmed", name="Confirmed", sag_source_config_id="sag-confirmed"),
                Source(id="source-revoked", name="Revoked", sag_source_config_id="sag-revoked"),
                Source(id="source-other-tenant", name="Other tenant", sag_source_config_id="sag-other"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Document(
                    id="doc-confirmed",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    source_id="source-confirmed",
                    filename="confirmed.txt",
                    storage_path="/confirmed.txt",
                    is_active=True,
                ),
                Document(
                    id="doc-revoked",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    source_id="source-revoked",
                    filename="revoked.txt",
                    storage_path="/revoked.txt",
                    is_active=True,
                ),
                Document(
                    id="doc-other-tenant",
                    tenant_id="tenant-b",
                    project_id="project-a",
                    source_id="source-other-tenant",
                    filename="other.txt",
                    storage_path="/other.txt",
                    is_active=True,
                ),
                SourceProjectMapping(
                    source_id="source-confirmed",
                    organization_id="org-a",
                    project_id="project-a",
                    state="CONFIRMED",
                    mapping_version=1,
                    confirmed_at=datetime(2026, 10, 1, tzinfo=UTC),
                    confirmed_by="owner",
                    approval_ref="approval-confirmed",
                ),
                SourceProjectMapping(
                    source_id="source-revoked",
                    organization_id="org-a",
                    project_id="project-a",
                    state="REVOKED",
                    mapping_version=2,
                    revoked_at=datetime(2026, 10, 1, tzinfo=UTC),
                    revoked_by="owner",
                    revocation_ref="revocation-record",
                ),
                SourceProjectMapping(
                    source_id="source-other-tenant",
                    organization_id="org-b",
                    project_id="project-a",
                    state="CONFIRMED",
                    mapping_version=1,
                    confirmed_at=datetime(2026, 10, 1, tzinfo=UTC),
                    confirmed_by="owner",
                    approval_ref="approval-other",
                ),
            ]
        )
        await session.flush()

        source_ids = await confirmed_source_ids_for_quality_scope(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
        )

        assert source_ids == {"source-confirmed"}


@pytest.mark.asyncio
async def test_scoring_results_persist_idempotently_with_gap_evidence_and_reason(quality_store):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    input_snapshot = _scored_snapshot()
    async with quality_store() as session:
        first = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=input_snapshot,
        )
        await session.commit()
        first_id = first.id

        second = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=input_snapshot,
        )
        await session.commit()

        assert second.id == first_id
        assessments = (await session.scalars(select(KnowledgeQualityAssessment))).all()
        gaps = (await session.scalars(select(KnowledgeGap))).all()
        assert len(assessments) == 1
        assert len(gaps) == 1
        assert gaps[0].assessment_id == assessments[0].id
        assert gaps[0].evidence_refs_json[0]["evidence_id"] == "evidence-a"
        assert "ANCHORED_EVIDENCE_COVERAGE_GAP" in gaps[0].reason_codes_json
        persisted_input = assessments[0].input_snapshot_json["node-a"]["scoring_input"]
        assert persisted_input["units"][0]["published_at"] == "2024-04-25T00:00:00Z"
        assert persisted_input["units"][0]["evidence"][0]["confidence"] == 0.4


@pytest.mark.asyncio
async def test_assessment_snapshot_rejects_raw_query_text(quality_store):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = {
        "node-a": {
            "unit_ids": ["unit-a", "unit-b"],
            "query-original": "private search string",
        }
    }
    async with quality_store() as session:
        with pytest.raises(ValueError, match="raw query/message content"):
            await persist_scoring_results(
                session,
                (result,),
                config=config,
                input_snapshot=snapshot,
            )


@pytest.mark.asyncio
async def test_assessment_snapshot_rejects_query_text_in_demand_field(quality_store):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()
    snapshot["node-a"]["query_demand"] = "private search string"
    async with quality_store() as session:
        with pytest.raises(ValueError, match="numeric aggregates only"):
            await persist_scoring_results(
                session,
                (result,),
                config=config,
                input_snapshot=snapshot,
            )


@pytest.mark.asyncio
async def test_assessment_snapshot_rejects_data_that_differs_from_scored_input(quality_store):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()
    snapshot["node-a"]["scoring_input"]["query_demand_current"] = 0
    async with quality_store() as session:
        with pytest.raises(ValueError, match="does not match the scored input checksum"):
            await persist_scoring_results(
                session,
                (result,),
                config=config,
                input_snapshot=snapshot,
            )


@pytest.mark.asyncio
async def test_question_generation_is_phrase_only_and_keeps_gap_evidence_and_reasons(
    quality_store, monkeypatch
):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()
    evidence_refs = snapshot["node-a"]["evidence_refs"]
    calls = []

    async def verified_refs(_session, _assessment, _gap):
        return evidence_refs

    async def phrase(prompt_json):
        calls.append(prompt_json)
        return "What can verify the Anchored supporting quote?"

    monkeypatch.setattr(
        "sag_api.services.knowledge_gap_question_service._load_current_evidence_refs",
        verified_refs,
    )
    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        await session.flush()
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))
        question = await generate_question_for_gap(
            session,
            gap.id,
            phrase=phrase,
            model_version="test-model-v1",
        )
        repeated = await generate_question_for_gap(
            session,
            gap.id,
            phrase=phrase,
            model_version="test-model-v1",
        )
        await session.commit()

        assert len(calls) == 2
        assert "Anchored supporting quote" in calls[0]
        assert repeated.id == question.id
        assert await session.scalar(select(KnowledgeGapQuestion.id)) == question.id
        assert question.gap_id == gap.id
        assert question.evidence_ids_json == ["evidence-a"]
        assert question.reason_codes_json == gap.reason_codes_json
        assert question.question_text.endswith("?")


@pytest.mark.asyncio
async def test_unrelated_question_output_is_rejected_without_persistence(quality_store, monkeypatch):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()

    async def verified_refs(_session, _assessment, _gap):
        return snapshot["node-a"]["evidence_refs"]

    async def phrase(_prompt_json):
        return "What deployment conditions affect European launches?"

    monkeypatch.setattr(
        "sag_api.services.knowledge_gap_question_service._load_current_evidence_refs",
        verified_refs,
    )
    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))

        with pytest.raises(ValueError, match="preserve an evidence phrase"):
            await generate_question_for_gap(session, gap.id, phrase=phrase)
        assert await session.scalar(select(KnowledgeGapQuestion.id)) is None


@pytest.mark.asyncio
async def test_ineligible_gap_never_calls_question_model(quality_store):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()
    calls = []

    async def phrase(_prompt_json):
        calls.append(True)
        return "Should never run?"

    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))
        gap.eligible = False
        await session.flush()

        question = await generate_question_for_gap(session, gap.id, phrase=phrase)

        assert question is None
        assert calls == []
        assert await session.scalar(select(KnowledgeGapQuestion.id)) is None


@pytest.mark.asyncio
async def test_question_model_is_not_called_when_supporting_source_is_no_longer_current(
    quality_store, monkeypatch
):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()
    calls = []

    async def no_current_refs(_session, _assessment, _gap):
        return []

    async def phrase(_prompt_json):
        calls.append(True)
        return "Should never run?"

    monkeypatch.setattr(
        "sag_api.services.knowledge_gap_question_service._load_current_evidence_refs",
        no_current_refs,
    )
    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))

        question = await generate_question_for_gap(session, gap.id, phrase=phrase)

        assert question is None
        assert calls == []
        assert await session.scalar(select(KnowledgeGapQuestion.id)) is None


@pytest.mark.asyncio
async def test_invalid_question_model_output_is_rejected_without_persistence(
    quality_store, monkeypatch
):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()

    async def verified_refs(_session, _assessment, _gap):
        return snapshot["node-a"]["evidence_refs"]

    async def phrase(_prompt_json):
        return "Question one?\nQuestion two?"

    monkeypatch.setattr(
        "sag_api.services.knowledge_gap_question_service._load_current_evidence_refs",
        verified_refs,
    )
    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))

        with pytest.raises(ValueError, match="one bounded question"):
            await generate_question_for_gap(session, gap.id, phrase=phrase)
        assert await session.scalar(select(KnowledgeGapQuestion.id)) is None


@pytest.mark.asyncio
async def test_question_model_error_is_not_persisted(quality_store, monkeypatch):
    result = _scored_result()
    config = KnowledgeQualityConfig(minimum_priority=0.0)
    snapshot = _scored_snapshot()

    async def verified_refs(_session, _assessment, _gap):
        return snapshot["node-a"]["evidence_refs"]

    async def failed_phrase(_prompt_json):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "sag_api.services.knowledge_gap_question_service._load_current_evidence_refs",
        verified_refs,
    )
    async with quality_store() as session:
        assessment = await persist_scoring_results(
            session,
            (result,),
            config=config,
            input_snapshot=snapshot,
        )
        gap = await session.scalar(select(KnowledgeGap).where(KnowledgeGap.assessment_id == assessment.id))

        with pytest.raises(RuntimeError, match="provider unavailable"):
            await generate_question_for_gap(session, gap.id, phrase=failed_phrase)
        assert await session.scalar(select(KnowledgeGapQuestion.id)) is None


def _tree_manifest(tree_version="tree-v1", project_id="project-a"):
    digest = hashlib.sha256(b"{}").hexdigest()
    manifest_json = {
        "tree_version": tree_version,
        "checksum": digest,
        "unit_count": 1,
        "node_count": 1,
        "status": "ACTIVE",
    }
    return TreeManifest(
        tree_version=tree_version,
        project_id=project_id,
        config_version="tree-config-v1",
        node_count=1,
        leaf_count=1,
        max_leaf_size=1,
        giant_ratio=0.0,
        routing_recall_at_k=1.0,
        escape_win_rate=0.0,
        acl_blackhole_rate=0.0,
        status="ACTIVE",
        checksum=digest,
        manifest_json=manifest_json,
    )


def _tree_profile(*, tenant_id="tenant-a", project_id="project-a", partition_id="partition-a"):
    return {
        "node_id": "node-a",
        "parent_id": None,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "partition_id": partition_id,
        "unit_ids": ["unit-a"],
        "is_leaf": True,
        "accessible_unit_count": 1,
    }


def _current_unit(*, tenant_id="tenant-a", project_id="project-a", partition_id="partition-a"):
    return SimpleNamespace(
        id="unit-a",
        tenant_id=tenant_id,
        project_id=project_id,
        security_partition_id=partition_id,
        document_version_id="version-a",
        source_id="source-a",
        provenance_json={
            "published_at": "2026-10-01T00:00:00+00:00",
            "observed_at": "2026-10-02T00:00:00+00:00",
            "ingested_at": "2026-10-03T00:00:00+00:00",
            "evidence_block_ids": [],
        },
    )


@pytest.mark.asyncio
async def test_collector_keeps_metrics_inside_requested_partition(quality_store, monkeypatch):
    as_of = datetime(2026, 10, 12, tzinfo=UTC)
    async with quality_store() as session:
        session.add(_tree_manifest())
        await session.flush()
        profile = _tree_profile()
        session.add(
            KnowledgeTreeNode(
                tree_version="tree-v1",
                node_id="node-a",
                parent_id=None,
                security_partition_id="partition-a",
                payload_json=profile,
                checksum=checksum(profile),
            )
        )
        await session.commit()

        async def current_units_for_test(_session, _tenant_id, _project_id):
            return [_current_unit(partition_id="partition-b")]

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.current_units",
            current_units_for_test,
        )
        async def confirmed_sources_for_test(*_args, **_kwargs):
            return {"source-a"}

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.confirmed_source_ids_for_quality_scope",
            confirmed_sources_for_test,
        )
        with pytest.raises(ValueError, match="violates current unit or ACL scope"):
            await build_topic_inputs(
                session,
                tenant_id="tenant-a",
                project_id="project-a",
                security_partition_id="partition-a",
                tree_version="tree-v1",
                as_of=as_of,
                demand_window_days=7,
            )


@pytest.mark.asyncio
async def test_collector_versions_tree_profile_and_marks_unobserved_demand_unknown(
    quality_store, monkeypatch
):
    as_of = datetime(2026, 10, 12, tzinfo=UTC)
    async with quality_store() as session:
        session.add(_tree_manifest())
        await session.flush()
        profile = _tree_profile()
        session.add(
            KnowledgeTreeNode(
                tree_version="tree-v1",
                node_id="node-a",
                parent_id=None,
                security_partition_id="partition-a",
                payload_json=profile,
                checksum=checksum(profile),
            )
        )
        await session.commit()

        async def current_units_for_test(_session, _tenant_id, _project_id):
            return [_current_unit()]

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.current_units",
            current_units_for_test,
        )
        async def confirmed_sources_for_test(*_args, **_kwargs):
            return {"source-a"}

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.confirmed_source_ids_for_quality_scope",
            confirmed_sources_for_test,
        )
        topics, snapshots = await build_topic_inputs(
            session,
            tenant_id="tenant-a",
            project_id="project-a",
            security_partition_id="partition-a",
            tree_version="tree-v1",
            as_of=as_of,
            demand_window_days=7,
        )

        assert len(topics) == 1
        assert topics[0].node_id == "node-a"
        assert topics[0].tree_manifest_checksum == hashlib.sha256(b"{}").hexdigest()
        assert topics[0].node_profile_checksum == checksum(profile)
        assert topics[0].query_demand_current is None
        assert topics[0].query_demand_previous is None
        assert snapshots["node-a"]["unit_ids"] == ["unit-a"]
        assert "query_text" not in snapshots["node-a"]


@pytest.mark.asyncio
async def test_collector_fails_closed_when_source_mapping_is_not_confirmed(quality_store, monkeypatch):
    as_of = datetime(2026, 10, 12, tzinfo=UTC)
    async with quality_store() as session:
        session.add(_tree_manifest())
        await session.flush()
        profile = _tree_profile()
        session.add(
            KnowledgeTreeNode(
                tree_version="tree-v1",
                node_id="node-a",
                parent_id=None,
                security_partition_id="partition-a",
                payload_json=profile,
                checksum=checksum(profile),
            )
        )
        await session.commit()

        async def current_units_for_test(_session, _tenant_id, _project_id):
            return [_current_unit()]

        async def no_confirmed_sources(*_args, **_kwargs):
            return set()

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.current_units",
            current_units_for_test,
        )
        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.confirmed_source_ids_for_quality_scope",
            no_confirmed_sources,
        )
        with pytest.raises(ValueError, match="violates current unit or ACL scope"):
            await build_topic_inputs(
                session,
                tenant_id="tenant-a",
                project_id="project-a",
                security_partition_id="partition-a",
                tree_version="tree-v1",
                as_of=as_of,
                demand_window_days=7,
            )


@pytest.mark.asyncio
async def test_collector_rejects_tree_manifest_checksum_mismatch(quality_store, monkeypatch):
    as_of = datetime(2026, 10, 12, tzinfo=UTC)
    async with quality_store() as session:
        manifest = _tree_manifest()
        manifest.manifest_json = {
            "tree_version": "tree-v1",
            "checksum": "different-checksum",
            "node_count": 1,
            "status": "ACTIVE",
        }
        session.add(manifest)
        await session.flush()
        profile = _tree_profile()
        session.add(
            KnowledgeTreeNode(
                tree_version="tree-v1",
                node_id="node-a",
                parent_id=None,
                security_partition_id="partition-a",
                payload_json=profile,
                checksum=checksum(profile),
            )
        )
        await session.commit()

        async def current_units_for_test(_session, _tenant_id, _project_id):
            return [_current_unit()]

        monkeypatch.setattr(
            "sag_api.services.knowledge_quality_repository.current_units",
            current_units_for_test,
        )
        with pytest.raises(ValueError, match="tree manifest checksum mismatch"):
            await build_topic_inputs(
                session,
                tenant_id="tenant-a",
                project_id="project-a",
                security_partition_id="partition-a",
                tree_version="tree-v1",
                as_of=as_of,
                demand_window_days=7,
            )
