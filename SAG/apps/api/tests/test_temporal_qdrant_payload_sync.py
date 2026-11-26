"""TDD tests for Phase 1 Wave 1.1A: temporal supersede precedence, Qdrant set_payload sync,
reconcile recovery, StageRun clock-skew clamp, and chronological DR rebuild."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.base import Base
from sag_api.db.models import (
    CanonicalBlock,
    Document,
    DocumentVersion,
    IngestionRun,
    Job,
    SearchUnit,
    Source,
    StageRun,
)
from sag_api.enums import ConnectorKind, DocumentStatus, JobStatus, JobType, SourceStatus, SourceType
from sag_api.jobs import tasks
from sag_api.services import dedup_and_temporal_service as temporal_service
from sag_api.services import rebuild_service, search_index_service

MAX_VALID_TO = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest_asyncio.fixture
async def sync_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class Fixed4DEmbedder:
    model = "bge-large-en-v1.5"
    base_url = "https://api.siliconflow.com/v1"
    dimensions = 4

    async def batch_generate(self, texts: list[str]) -> list[list[float]]:
        return [[0.25, 0.25, 0.25, 0.25] for _ in texts]


def _make_qdrant_harness(
    *,
    project_id: str,
    fail_payload_versions: set[str] | None = None,
    missing_payload_index_once: bool = False,
):
    """In-memory Qdrant HTTP mock tracking points, deletes, set_payloads, and index creations."""
    store: dict[str, dict] = {}
    delete_calls: list[dict] = []
    payload_calls: list[dict] = []
    created_indexes: list[str] = []
    state = {
        "fail_payload_versions": set(fail_payload_versions or ()),
        "missing_payload_index_once": missing_payload_index_once,
        "missing_index_triggered": False,
    }
    col_path = f"/collections/search_units_{project_id}"

    def _matches_filter(pt: dict, flt: dict) -> bool:
        must = (flt or {}).get("must") or []
        payload = pt.get("payload") or {}
        for cond in must:
            key = cond.get("key")
            match = cond.get("match") or {}
            if "value" in match and payload.get(key) != match["value"]:
                return False
            if "any" in match and payload.get(key) not in match["any"]:
                return False
        return True

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        path = request.url.path

        if request.method == "GET" and path == col_path:
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"content_vector": {"size": 4}},
                                "sparse_vectors": {"bm25_sparse": {"modifier": "idf"}},
                            }
                        },
                        "payload_schema": {},
                    }
                },
            )
        if request.method == "PUT" and path.endswith("/index"):
            created_indexes.append(body["field_name"])
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if request.method == "GET" and "/points/" in path:
            return httpx.Response(404, json={"status": {"error": "not found"}})
        if request.method == "PUT" and path.endswith("/points"):
            for pt in body.get("points", []):
                store[str(pt["id"])] = {
                    "id": str(pt["id"]),
                    "payload": dict(pt.get("payload") or {}),
                    "vector": pt.get("vector"),
                }
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path.endswith("/points/count"):
            flt = body.get("filter")
            cnt = sum(1 for pt in store.values() if not flt or _matches_filter(pt, flt))
            return httpx.Response(200, json={"result": {"count": cnt}})
        if path.endswith("/points/scroll"):
            flt = body.get("filter")
            pts = [
                {"id": pt["id"], "payload": dict(pt["payload"])}
                for pt in store.values()
                if not flt or _matches_filter(pt, flt)
            ]
            return httpx.Response(200, json={"result": {"points": pts, "next_page_offset": None}})
        if path.endswith("/points/delete"):
            delete_calls.append(body)
            if "filter" in body:
                for pid in list(store.keys()):
                    if _matches_filter(store[pid], body["filter"]):
                        del store[pid]
            for pid in body.get("points", []):
                store.pop(str(pid), None)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        if path.endswith("/points/payload"):
            payload_calls.append(
                {
                    "url": str(request.url),
                    "body": body,
                }
            )
            if state["missing_payload_index_once"] and not state["missing_index_triggered"]:
                state["missing_index_triggered"] = True
                return httpx.Response(
                    400,
                    text='Index required but not found for "document_version_id"',
                )
            must = (body.get("filter") or {}).get("must") or []
            target_vid = next(
                (
                    cond.get("match", {}).get("value")
                    for cond in must
                    if cond.get("key") == "document_version_id"
                ),
                None,
            )
            if target_vid in state["fail_payload_versions"]:
                return httpx.Response(503, text=f"qdrant set_payload unavailable for {target_vid}")
            patch = body.get("payload") or {}
            for pt in store.values():
                if _matches_filter(pt, body.get("filter") or {}):
                    pt["payload"].update(patch)
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {}})

    return store, delete_calls, payload_calls, created_indexes, state, handler


@pytest.mark.asyncio
async def test_resolve_temporal_supersedes_effective_from_precedence_all_4_sites(sync_db):
    """effective_from > source_published_at > observed_at across INITIAL, SUPERSEDED,
    successor rewiring, and OUT_OF_ORDER_ARCHIVED, returning accurate mutated_versions."""
    t0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
    t_mid = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    t3 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=UTC)

    async with sync_db() as session:
        session.add(Source(id="src-eff", name="Eff", sag_source_config_id="cfg-eff"))
        session.add(
            Document(
                id="doc-eff",
                source_id="src-eff",
                tenant_id="t1",
                project_id="p1",
                filename="policy.md",
                storage_path="policy.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

        # Site 1 (INITIAL): effective_from=t1 overrides later source_published_at & observed_at
        v1 = DocumentVersion(
            id="v1",
            document_id="doc-eff",
            version_no=1,
            file_hash="h1",
            status="READY",
            search_status="SEARCH_READY",
            effective_from=t1,
            source_published_at=t3,
            observed_at=t3 + timedelta(days=10),
            authority_tier=2,
            scope_tags_json=["VN", "HR Policy"],
        )
        session.add(v1)
        await session.flush()

        res1 = await temporal_service.resolve_temporal_supersedes(
            session, current_version=v1, document_id="doc-eff"
        )
        assert res1["action"] == "INITIAL"
        assert res1["active_version_id"] == "v1"
        assert v1.valid_from == t1
        assert v1.valid_to == MAX_VALID_TO
        assert "mutated_versions" in res1
        assert [e["version_id"] for e in res1["mutated_versions"]] == ["v1"]
        assert res1["mutated_versions"][0]["valid_from"] == t1
        assert res1["mutated_versions"][0]["is_superseded"] is False
        assert res1["mutated_versions"][0]["authority_tier"] == 2
        assert res1["mutated_versions"][0]["effective_from"] == t1
        assert res1["mutated_versions"][0]["scope_tags"] == ["hr-policy", "vn"]

        # Site 2 (SUPERSEDED): v3 has effective_from=t3 (even though source_published_at=t0!)
        v3 = DocumentVersion(
            id="v3",
            document_id="doc-eff",
            version_no=2,
            file_hash="h3",
            status="READY",
            search_status="SEARCH_READY",
            effective_from=t3,
            source_published_at=t0,
            observed_at=t0,
            authority_tier=1,
            scope_tags_json=["VN"],
        )
        session.add(v3)
        await session.flush()

        res3 = await temporal_service.resolve_temporal_supersedes(
            session, current_version=v3, document_id="doc-eff"
        )
        assert res3["action"] == "SUPERSEDED"
        assert res3["active_version_id"] == "v3"
        assert v3.supersedes_id == "v1"
        assert v1.valid_from == t1
        assert v1.valid_to == t3
        assert v3.valid_from == t3
        assert v3.valid_to == MAX_VALID_TO
        mutated_map3 = {e["version_id"]: e for e in res3["mutated_versions"]}
        assert set(mutated_map3.keys()) == {"v3", "v1"}
        assert mutated_map3["v1"]["is_superseded"] is True
        assert mutated_map3["v1"]["valid_to"] == t3
        assert mutated_map3["v3"]["is_superseded"] is False

        # Site 3 (Successor rewiring): insert v_mid between t1 and t3.
        # v3.source_published_at is t0 (< t_mid), so rewiring ONLY succeeds if site 3 uses effective_from!
        v_mid = DocumentVersion(
            id="v_mid",
            document_id="doc-eff",
            version_no=3,
            file_hash="hmid",
            status="READY",
            search_status="SEARCH_READY",
            effective_from=t_mid,
            source_published_at=t0,
            observed_at=t3 + timedelta(days=5),
            authority_tier=3,
            scope_tags_json=["ops"],
        )
        session.add(v_mid)
        await session.flush()

        res_mid = await temporal_service.resolve_temporal_supersedes(
            session, current_version=v_mid, document_id="doc-eff"
        )
        assert res_mid["action"] == "SUPERSEDED"
        assert res_mid["active_version_id"] == "v3"
        assert v_mid.supersedes_id == "v1"
        assert v_mid.valid_from == t_mid
        assert v_mid.valid_to == t3
        assert v1.valid_to == t_mid
        assert v3.supersedes_id == "v_mid", "Site 3 must use effective_from to rewire successor v3"
        mutated_mid_ids = {e["version_id"] for e in res_mid["mutated_versions"]}
        assert {"v_mid", "v1"} <= mutated_mid_ids

        # Site 4 (OUT_OF_ORDER_ARCHIVED): v0 has effective_from=t0 (before v1.effective_from=t1)
        # while observed_at/source_published_at are far in the future.
        v0 = DocumentVersion(
            id="v0",
            document_id="doc-eff",
            version_no=4,
            file_hash="h0",
            status="READY",
            search_status="SEARCH_READY",
            effective_from=t0,
            source_published_at=t3 + timedelta(days=30),
            observed_at=t3 + timedelta(days=31),
            authority_tier=4,
        )
        session.add(v0)
        await session.flush()

        res0 = await temporal_service.resolve_temporal_supersedes(
            session, current_version=v0, document_id="doc-eff"
        )
        assert res0["action"] == "OUT_OF_ORDER_ARCHIVED"
        assert res0["active_version_id"] == "v3"
        assert v0.valid_from == t0
        assert v0.valid_to == t1
        assert v1.supersedes_id == "v0"
        mutated_map0 = {e["version_id"]: e for e in res0["mutated_versions"]}
        assert "v0" in mutated_map0
        assert mutated_map0["v0"]["is_superseded"] is True
        assert mutated_map0["v0"]["valid_to"] == t1


@pytest.mark.asyncio
async def test_run_search_indexing_stage_retains_superseded_and_syncs_set_payload(sync_db):
    """v1 -> v2 retains v1 points in Qdrant, syncs v2 & v1 via POST /points/payload?wait=true,
    preserves content/manifest_checksum, writes new metadata fields, auto-heals missing payload index,
    and clamps StageRun.created_at >= ing_run.started_at."""
    t1 = datetime(2025, 1, 10, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 5, 1, 0, 0, 0, tzinfo=UTC)
    store, delete_calls, payload_calls, created_indexes, _state, handler = _make_qdrant_harness(
        project_id="proj-ret",
        missing_payload_index_once=True,
    )

    assert ("authority_tier", "integer") in search_index_service.PAYLOAD_INDEX_FIELDS
    assert ("effective_from_ts", "integer") in search_index_service.PAYLOAD_INDEX_FIELDS
    assert ("scope_tags", "keyword") in search_index_service.PAYLOAD_INDEX_FIELDS

    async with sync_db() as session:
        session.add(Source(id="src-ret", name="Ret", sag_source_config_id="cfg-ret"))
        session.add(
            Document(
                id="doc-ret",
                source_id="src-ret",
                tenant_id="tenant-ret",
                project_id="proj-ret",
                filename="handbook.md",
                storage_path="handbook.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

        v1 = DocumentVersion(
            id="v1-ret",
            document_id="doc-ret",
            version_no=1,
            file_hash="fh-ret-1",
            effective_from=t1,
            source_published_at=t1,
            authority_tier=2,
            scope_tags_json=["VN", "HR Policy"],
            status="RECEIVED",
            search_status="PENDING",
            metadata_json={"security_partition_id": "part-ret"},
        )
        session.add(v1)
        await session.flush()
        v1_text = "Old vacation policy allows 12 annual leave days."
        session.add(
            CanonicalBlock(
                id="blk-ret-1",
                document_version_id="v1-ret",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Leave",
                source_anchor="leave-1",
                normalized_text=v1_text,
                content_hash=hashlib.sha256(v1_text.encode("utf-8")).hexdigest(),
            )
        )
        await session.flush()

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            units_v1 = await search_index_service.run_search_indexing_stage(
                session,
                project_id="proj-ret",
                document_version=v1,
                security_partition_id="part-ret",
                tenant_id="tenant-ret",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
            await session.commit()

            assert len(units_v1) == 1
            assert "document_version_id" in created_indexes
            v1_point_id = search_index_service.generate_search_unit_point_id(
                "search_units_proj-ret", units_v1[0].id
            )
            assert v1_point_id in store
            original_v1_content = store[v1_point_id]["payload"]["content"]
            original_v1_hash = store[v1_point_id]["payload"]["content_hash"]
            assert original_v1_content == v1_text
            assert store[v1_point_id]["payload"]["authority_tier"] == 2
            assert store[v1_point_id]["payload"]["effective_from"] == t1.isoformat()
            assert store[v1_point_id]["payload"]["effective_from_ts"] == int(t1.timestamp())
            assert store[v1_point_id]["payload"]["scope_tags"] == ["hr-policy", "vn"]

            # Now index v2 superseding v1, with IngestionRun.started_at artificially 500ms in the future
            future_started_at = datetime.now(UTC) + timedelta(milliseconds=500)
            v2 = DocumentVersion(
                id="v2-ret",
                document_id="doc-ret",
                version_no=2,
                file_hash="fh-ret-2",
                effective_from=t2,
                source_published_at=t2,
                authority_tier=1,
                scope_tags_json=["VN", "Legal"],
                status="RECEIVED",
                search_status="PENDING",
                metadata_json={"security_partition_id": "part-ret"},
            )
            session.add(v2)
            await session.flush()
            ing_run_v2 = IngestionRun(
                id="run-ret-2",
                tenant_id="tenant-ret",
                project_id="proj-ret",
                document_version_id="v2-ret",
                idempotency_key="idem-ret-2",
                payload_hash="fh-ret-2",
                status="RUNNING",
                attempt_count=2,
                started_at=future_started_at,
            )
            session.add(ing_run_v2)
            v2_text = "Updated binding vacation policy allows 15 annual leave days."
            session.add(
                CanonicalBlock(
                    id="blk-ret-2",
                    document_version_id="v2-ret",
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Leave",
                    source_anchor="leave-2",
                    normalized_text=v2_text,
                    content_hash=hashlib.sha256(v2_text.encode("utf-8")).hexdigest(),
                )
            )
            await session.flush()

            delete_calls.clear()
            payload_calls.clear()

            units_v2 = await search_index_service.run_search_indexing_stage(
                session,
                project_id="proj-ret",
                document_version=v2,
                security_partition_id="part-ret",
                tenant_id="tenant-ret",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
                run_id="run-ret-2",
            )
            await session.commit()

            assert len(units_v2) == 1
            v2_point_id = search_index_service.generate_search_unit_point_id(
                "search_units_proj-ret", units_v2[0].id
            )

            # 1) NEVER calls POST /points/delete for superseded v1
            deleted_for_v1 = [
                call
                for call in delete_calls
                if "v1-ret" in json.dumps(call)
            ]
            assert deleted_for_v1 == [], f"Superseded v1 must NEVER be deleted: {deleted_for_v1}"
            assert v1_point_id in store, "Superseded v1 point must remain in Qdrant"
            assert v2_point_id in store, "Active v2 point must be in Qdrant"

            # 2) Calls POST /points/payload?wait=true syncing both v2 and v1
            assert all("wait=true" in c["url"] for c in payload_calls)
            synced_vids = {
                cond["match"]["value"]
                for c in payload_calls
                for cond in (c["body"].get("filter") or {}).get("must") or []
                if cond.get("key") == "document_version_id"
            }
            assert {"v1-ret", "v2-ret"} <= synced_vids

            # 3) v1.valid_to_ts == v2.valid_from_ts and content/hash byte-identical
            v1_payload = store[v1_point_id]["payload"]
            v2_payload = store[v2_point_id]["payload"]
            assert v1_payload["valid_to_ts"] == int(t2.timestamp())
            assert v2_payload["valid_from_ts"] == int(t2.timestamp())
            assert v1_payload["valid_to_ts"] == v2_payload["valid_from_ts"]
            assert v1_payload["content"] == original_v1_content
            assert v1_payload["content_hash"] == original_v1_hash
            assert v2_payload["authority_tier"] == 1
            assert v2_payload["effective_from"] == t2.isoformat()
            assert v2_payload["effective_from_ts"] == int(t2.timestamp())
            assert v2_payload["scope_tags"] == ["legal", "vn"]

            # 4) StageRun.created_at clamped >= ing_run.started_at (500ms clock skew!) + manifest checksum valid
            stage_run = await session.scalar(
                select(StageRun).where(
                    StageRun.run_id == "run-ret-2",
                    StageRun.stage == "INDEX_SEARCH",
                )
            )
            assert stage_run is not None
            assert stage_run.created_at >= future_started_at
            assert stage_run.metrics_json["manifest_verified"] is True
            assert (
                stage_run.metrics_json["manifest_checksum"]
                == stage_run.metrics_json["qdrant_checksum"]
            )
            assert stage_run.metrics_json.get("attempt_count") == 2


@pytest.mark.asyncio
async def test_partial_set_payload_failure_degraded_metadata_and_reconcile_recovery(sync_db):
    """When set_payload returns 503 for v1 while v2 succeeds: v2 stays SEARCH_READY,
    qdrant_temporal_sync_degraded=True is merged without losing security_partition_id,
    RECONCILE_SEARCH_INDEX job is persisted, and tasks.reconcile_search_index repairs v1 & clears flags."""
    t1 = datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC)
    t2 = datetime(2025, 4, 1, 0, 0, 0, tzinfo=UTC)
    store, delete_calls, _payload_calls, _indexes, state, handler = _make_qdrant_harness(
        project_id="proj-part",
        fail_payload_versions=set(),
    )

    async with sync_db() as session:
        session.add(Source(id="src-part", name="Part", sag_source_config_id="cfg-part"))
        session.add(
            Document(
                id="doc-part",
                source_id="src-part",
                tenant_id="t-part",
                project_id="proj-part",
                filename="spec.md",
                storage_path="spec.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

        v1 = DocumentVersion(
            id="v1",
            document_id="doc-part",
            version_no=1,
            file_hash="fh-p1",
            effective_from=t1,
            authority_tier=2,
            scope_tags_json=["vn"],
            status="READY",
            search_status="PENDING",
            metadata_json={"security_partition_id": "part-sec-1", "extra": "keep-v1"},
        )
        session.add(v1)
        await session.flush()
        session.add(
            CanonicalBlock(
                id="blk-p1",
                document_version_id="v1",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Spec",
                source_anchor="s1",
                normalized_text="Version 1 specification clause.",
                content_hash=hashlib.sha256(b"Version 1 specification clause.").hexdigest(),
            )
        )
        await session.flush()

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            u1 = await search_index_service.run_search_indexing_stage(
                session,
                project_id="proj-part",
                document_version=v1,
                security_partition_id="part-sec-1",
                tenant_id="t-part",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
            await session.commit()
            pt_v1 = search_index_service.generate_search_unit_point_id("search_units_proj-part", u1[0].id)

            # Fail set_payload ONLY for v1 during v2 indexing
            state["fail_payload_versions"].add("v1")
            v2 = DocumentVersion(
                id="v2",
                document_id="doc-part",
                version_no=2,
                file_hash="fh-p2",
                effective_from=t2,
                authority_tier=1,
                scope_tags_json=["vn", "prod"],
                status="READY",
                search_status="PENDING",
                metadata_json={"security_partition_id": "part-sec-1", "upload_note": "keep-v2"},
            )
            session.add(v2)
            await session.flush()
            session.add(
                CanonicalBlock(
                    id="blk-p2",
                    document_version_id="v2",
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="Spec",
                    source_anchor="s2",
                    normalized_text="Version 2 specification clause.",
                    content_hash=hashlib.sha256(b"Version 2 specification clause.").hexdigest(),
                )
            )
            await session.flush()

            await search_index_service.run_search_indexing_stage(
                session,
                project_id="proj-part",
                document_version=v2,
                security_partition_id="part-sec-1",
                tenant_id="t-part",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
            await session.commit()

            assert v2.search_status == "SEARCH_READY"
            assert v2.metadata_json["qdrant_temporal_sync_degraded"] is True
            assert v2.metadata_json["qdrant_temporal_sync_failed_version_ids"] == ["v1"]
            assert v2.metadata_json["security_partition_id"] == "part-sec-1"
            assert v2.metadata_json["upload_note"] == "keep-v2"
            assert pt_v1 in store
            # v1's Qdrant payload is still stale (open valid_to) until reconcile runs
            assert store[pt_v1]["payload"]["valid_to_ts"] == int(MAX_VALID_TO.timestamp())

            reconcile_job = await session.get(Job, v2._last_reconcile_job_id)
            assert reconcile_job is not None
            assert reconcile_job.type == JobType.RECONCILE_SEARCH_INDEX
            assert reconcile_job.status == JobStatus.QUEUED
            assert reconcile_job.payload == {
                "project_id": "proj-part",
                "reason": "temporal_payload_sync_failed",
                "sync_version_ids": ["v1"],
            }

            # Now Qdrant recovers -> run tasks.reconcile_search_index
            state["fail_payload_versions"].clear()
            delete_calls.clear()
            mgr = SimpleNamespace(get_search_unit_qdrant_client=AsyncMock(return_value=q_client))
            await tasks.reconcile_search_index(session, reconcile_job, engine_manager=mgr)

            assert delete_calls == [], "Targeted payload-sync reconcile must NOT delete superseded points"
            assert pt_v1 in store
            assert store[pt_v1]["payload"]["valid_to_ts"] == int(t2.timestamp())
            await session.refresh(v2)
            assert v2.metadata_json["qdrant_temporal_sync_degraded"] is False
            assert "qdrant_temporal_sync_error" not in v2.metadata_json
            assert "qdrant_temporal_sync_degraded_reason" not in v2.metadata_json
            assert "qdrant_temporal_sync_failed_version_ids" not in v2.metadata_json
            assert v2.metadata_json["security_partition_id"] == "part-sec-1"
            assert v2.metadata_json["upload_note"] == "keep-v2"
            assert reconcile_job.payload.get("temporal_payloads_synced") == 1


@pytest.mark.asyncio
async def test_failed_stage_run_clamps_clock_skew_in_tasks(sync_db, tmp_path, monkeypatch):
    """tasks.py failure path clamps failed_stage_run.created_at >= ing_run.started_at."""
    doc_file = tmp_path / "fail_skew.md"
    doc_file.write_text("# Fail\n\nTrigger failure.", encoding="utf-8")
    future_started_at = datetime.now(UTC) + timedelta(milliseconds=600)

    monkeypatch.setattr(tasks, "SessionLocal", sync_db)
    monkeypatch.setattr(tasks.settings, "document_event_extraction_enabled", False)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("simulated index stage failure")

    monkeypatch.setattr(search_index_service, "run_search_indexing_stage", boom)

    async with sync_db() as session:
        session.add(
            Source(
                id="src-sk",
                name="Skew",
                source_type=SourceType.DOCUMENT,
                connector_kind=ConnectorKind.FILE_UPLOAD,
                sag_source_config_id="cfg-sk",
                status=SourceStatus.ACTIVE,
            )
        )
        session.add(
            Document(
                id="doc-sk",
                source_id="src-sk",
                tenant_id="t-sk",
                project_id="p-sk",
                filename="fail_skew.md",
                storage_path=str(doc_file),
                status=DocumentStatus.PENDING,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id="ver-sk",
                document_id="doc-sk",
                version_no=1,
                file_hash="fh-sk",
                status="RECEIVED",
                metadata_json={"security_partition_id": "p-sk"},
            )
        )
        session.add(
            CanonicalBlock(
                id="blk-sk",
                document_version_id="ver-sk",
                ordinal=0,
                block_type="paragraph",
                page_from=1,
                page_to=1,
                section_path="Fail",
                normalized_text="Trigger failure.",
                content_hash=hashlib.sha256(b"Trigger failure.").hexdigest(),
            )
        )
        session.add(
            IngestionRun(
                id="run-sk",
                tenant_id="t-sk",
                project_id="p-sk",
                document_version_id="ver-sk",
                idempotency_key="idem-sk",
                payload_hash="fh-sk",
                status="RUNNING",
                started_at=future_started_at,
            )
        )
        job = Job(
            id="job-sk",
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id="src-sk",
            document_id="doc-sk",
            payload={"run_id": "run-sk", "canonical_checkpoint_stage": "DEDUP_TEMPORAL"},
        )
        session.add(job)
        await session.commit()

        q_client = httpx.AsyncClient(
            base_url="http://qdrant.local",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"result": {}})),
        )
        mgr = SimpleNamespace(
            get_sag_embedding=AsyncMock(return_value=Fixed4DEmbedder()),
            get_search_unit_qdrant_client=AsyncMock(return_value=q_client),
        )
        try:
            with pytest.raises(Exception, match="simulated index stage failure"):
                await tasks._process_document_unlocked(session, job, engine_manager=mgr)
        finally:
            await q_client.aclose()

        failed_stage = await session.scalar(
            select(StageRun).where(StageRun.run_id == "run-sk", StageRun.status == "FAILED")
        )
        assert failed_stage is not None
        assert failed_stage.created_at >= future_started_at


@pytest.mark.asyncio
async def test_rebuild_search_index_for_project_chronological_ordering(sync_db, monkeypatch):
    """Out-of-insert-order versions rebuild in deterministic chronological order via PG16-safe
    EXISTS semi-join, retaining both v1 and v2 in Qdrant with closed/active intervals."""
    t_old = datetime(2024, 3, 1, 0, 0, 0, tzinfo=UTC)
    t_new = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
    store, _delete_calls, _payload_calls, _indexes, _state, handler = _make_qdrant_harness(
        project_id="proj-rb",
    )

    processed_order: list[str] = []
    real_run_stage = rebuild_service.run_search_indexing_stage

    async def spy_run_stage(session, **kwargs):
        processed_order.append(kwargs["document_version"].id)
        return await real_run_stage(session, **kwargs)

    monkeypatch.setattr(rebuild_service, "run_search_indexing_stage", spy_run_stage)

    async with sync_db() as session:
        session.add(Source(id="src-rb", name="RB", sag_source_config_id="cfg-rb"))
        session.add_all(
            [
                Document(
                    id="doc-a",
                    source_id="src-rb",
                    tenant_id="t-rb",
                    project_id="proj-rb",
                    filename="a.md",
                    storage_path="a.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc-b",
                    source_id="src-rb",
                    tenant_id="t-rb",
                    project_id="proj-rb",
                    filename="b.md",
                    storage_path="b.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
            ]
        )
        await session.flush()

        # Deliberately insert v2 (newer) BEFORE v1 (older) into DB, with multiple SearchUnits each!
        versions_seed = [
            ("doc-b-v2", "doc-b", 2, t_new, "Doc B v2 clause"),
            ("doc-a-v2", "doc-a", 2, t_new, "Doc A v2 clause"),
            ("doc-b-v1", "doc-b", 1, t_old, "Doc B v1 clause"),
            ("doc-a-v1", "doc-a", 1, t_old, "Doc A v1 clause"),
        ]
        for vid, did, vno, eff, txt in versions_seed:
            session.add(
                DocumentVersion(
                    id=vid,
                    document_id=did,
                    version_no=vno,
                    file_hash=f"fh-{vid}",
                    effective_from=eff,
                    observed_at=eff,
                    valid_from=eff,
                    valid_to=MAX_VALID_TO,
                    status="READY",
                    search_status="PENDING",
                    metadata_json={"security_partition_id": "part-rb"},
                )
            )
            await session.flush()
            for idx in range(2):
                btxt = f"{txt} paragraph {idx}"
                bh = hashlib.sha256(btxt.encode("utf-8")).hexdigest()
                bid = f"blk-{vid}-{idx}"
                session.add(
                    CanonicalBlock(
                        id=bid,
                        document_version_id=vid,
                        ordinal=idx,
                        block_type="paragraph",
                        page_from=1,
                        page_to=1,
                        section_path=f"Section {idx}",
                        source_anchor=bid,
                        normalized_text=btxt,
                        content_hash=bh,
                    )
                )
                await session.flush()
                session.add(
                    SearchUnit(
                        id=f"su-{vid}-{idx}",
                        document_version_id=vid,
                        block_from_id=bid,
                        block_to_id=bid,
                        security_partition_id="part-rb",
                        content_hash=bh,
                        token_count=6,
                        page_from=1,
                        page_to=1,
                        section_path=f"Section {idx}",
                    )
                )
        await session.commit()

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            result = await rebuild_service.rebuild_search_index_for_project(
                session,
                project_id="proj-rb",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
            await session.commit()

        assert result["rebuilt_versions_count"] == 4
        assert processed_order == ["doc-a-v1", "doc-a-v2", "doc-b-v1", "doc-b-v2"]

        # Verify both v1 (closed) and v2 (active) retained in Qdrant for both docs
        by_ver: dict[str, list[dict]] = {}
        for pt in store.values():
            vid = pt["payload"].get("document_version_id")
            if vid:
                by_ver.setdefault(vid, []).append(pt["payload"])
        assert set(by_ver.keys()) == {"doc-a-v1", "doc-a-v2", "doc-b-v1", "doc-b-v2"}
        for doc_prefix in ("doc-a", "doc-b"):
            assert all(p["valid_to_ts"] == int(t_new.timestamp()) for p in by_ver[f"{doc_prefix}-v1"])
            assert all(p["valid_from_ts"] == int(t_new.timestamp()) for p in by_ver[f"{doc_prefix}-v2"])
            assert all(p["valid_to_ts"] == int(MAX_VALID_TO.timestamp()) for p in by_ver[f"{doc_prefix}-v2"])


@pytest.mark.asyncio
async def test_rebuild_query_uses_pg16_safe_exists_without_select_distinct(sync_db, monkeypatch):
    """Ensure rebuild_search_index_for_project issues an EXISTS semi-join (zero SELECT DISTINCT)
    so PostgreSQL 16 accepts COALESCE(...) in ORDER BY."""
    from sqlalchemy.dialects import postgresql

    captured_pg_sql: list[str] = []

    async with sync_db() as session:
        real_execute = session.execute

        async def spy_execute(stmt, *args, **kwargs):
            compiled = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": False},
                )
            )
            captured_pg_sql.append(compiled)
            return await real_execute(stmt, *args, **kwargs)

        monkeypatch.setattr(session, "execute", spy_execute)
        async with httpx.AsyncClient(
            base_url="http://qdrant.local",
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, json={"result": {}})),
        ) as q_client:
            out = await rebuild_service.rebuild_search_index_for_project(
                session,
                project_id="proj-sql-check",
                qdrant_client=q_client,
                embedder=Fixed4DEmbedder(),
            )
        assert out["rebuilt_versions_count"] == 0
        assert captured_pg_sql
        rebuild_sql = captured_pg_sql[0].upper()
        assert "EXISTS (" in rebuild_sql
        assert "COALESCE(" in rebuild_sql
        assert "ORDER BY" in rebuild_sql
        assert "SELECT DISTINCT" not in rebuild_sql


@pytest.mark.asyncio
async def test_reconcile_clears_stale_degraded_flag_when_failed_predecessor_revoked_or_deleted(
    sync_db,
):
    """MINOR-1: If a failed predecessor version was revoked or its document deleted before
    reconcile runs, reconcile_version_temporal_payloads must merge-clear stale degraded markers
    on the surviving active version while preserving security_partition_id and unrelated keys,
    without prematurely clearing still-active unsynced failed IDs."""
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2025, 1, 1, tzinfo=UTC)
    _store, _del, payload_calls, _idx, _state, handler = _make_qdrant_harness(
        project_id="proj-stale",
    )

    async with sync_db() as session:
        session.add(Source(id="src-stale", name="Stale", sag_source_config_id="cfg-stale"))
        session.add_all(
            [
                Document(
                    id="doc-active",
                    source_id="src-stale",
                    tenant_id="t-s",
                    project_id="proj-stale",
                    filename="active.md",
                    storage_path="active.md",
                    status=DocumentStatus.READY,
                    is_active=True,
                ),
                Document(
                    id="doc-deleted",
                    source_id="src-stale",
                    tenant_id="t-s",
                    project_id="proj-stale",
                    filename="deleted.md",
                    storage_path="deleted.md",
                    status=DocumentStatus.READY,
                    is_active=False,
                ),
            ]
        )
        await session.flush()

        # Predecessor 1: revoked on same doc; Predecessor 2: on soft-deleted doc
        v1_revoked = DocumentVersion(
            id="v1-revoked",
            document_id="doc-active",
            version_no=1,
            file_hash="fh-s1",
            valid_from=t1,
            valid_to=t2,
            status="REVOKED",
            search_status="SEARCH_READY",
            metadata_json={"security_partition_id": "part-s"},
        )
        v1_deleted = DocumentVersion(
            id="v1-deleted",
            document_id="doc-deleted",
            version_no=1,
            file_hash="fh-s-del",
            valid_from=t1,
            valid_to=MAX_VALID_TO,
            status="READY",
            search_status="SEARCH_READY",
            metadata_json={"security_partition_id": "part-s"},
        )
        # Surviving v2 pointing only to now-dead predecessors
        v2_stale = DocumentVersion(
            id="v2-stale",
            document_id="doc-active",
            version_no=2,
            file_hash="fh-s2",
            valid_from=t2,
            valid_to=MAX_VALID_TO,
            status="READY",
            search_status="SEARCH_READY",
            metadata_json={
                "security_partition_id": "part-s",
                "custom_keep": "yes",
                "qdrant_temporal_sync_degraded": True,
                "qdrant_temporal_sync_degraded_reason": "temporal_payload_sync_failed",
                "qdrant_temporal_sync_error": "HTTP 503",
                "qdrant_temporal_sync_failed_version_ids": ["v1-revoked", "v1-deleted"],
            },
        )
        # Another active version still legitimately unsynced
        v3_live_unsynced = DocumentVersion(
            id="v3-live",
            document_id="doc-active",
            version_no=3,
            file_hash="fh-s3",
            valid_from=t2 + timedelta(days=1),
            valid_to=MAX_VALID_TO,
            status="READY",
            search_status="SEARCH_READY",
            metadata_json={"security_partition_id": "part-s"},
        )
        # Partial version referencing one dead predecessor + one still-active version
        v4_partial = DocumentVersion(
            id="v4-partial",
            document_id="doc-active",
            version_no=4,
            file_hash="fh-s4",
            valid_from=t2 + timedelta(days=2),
            valid_to=MAX_VALID_TO,
            status="READY",
            search_status="SEARCH_READY",
            metadata_json={
                "security_partition_id": "part-s",
                "qdrant_temporal_sync_degraded": True,
                "qdrant_temporal_sync_degraded_reason": "temporal_payload_sync_failed",
                "qdrant_temporal_sync_error": "HTTP 503",
                "qdrant_temporal_sync_failed_version_ids": ["v1-revoked", "v3-live"],
            },
        )
        session.add_all([v1_revoked, v1_deleted, v2_stale, v3_live_unsynced, v4_partial])
        await session.commit()

        async with httpx.AsyncClient(
            base_url="http://qdrant.local", transport=httpx.MockTransport(handler)
        ) as q_client:
            # Explicit reconcile targeting ONLY the now-dead versions
            res = await search_index_service.reconcile_version_temporal_payloads(
                session,
                q_client,
                project_id="proj-stale",
                version_ids=["v1-revoked", "v1-deleted"],
            )
            await session.commit()

            assert res["synced_versions"] == 0
            assert payload_calls == []

            await session.refresh(v2_stale)
            assert v2_stale.metadata_json["qdrant_temporal_sync_degraded"] is False
            assert "qdrant_temporal_sync_degraded_reason" not in v2_stale.metadata_json
            assert "qdrant_temporal_sync_error" not in v2_stale.metadata_json
            assert "qdrant_temporal_sync_failed_version_ids" not in v2_stale.metadata_json
            assert v2_stale.metadata_json["security_partition_id"] == "part-s"
            assert v2_stale.metadata_json["custom_keep"] == "yes"

            # v4_partial pruned dead v1-revoked, but keeps degraded=True for still-active v3-live
            await session.refresh(v4_partial)
            assert v4_partial.metadata_json["qdrant_temporal_sync_degraded"] is True
            assert v4_partial.metadata_json["qdrant_temporal_sync_failed_version_ids"] == ["v3-live"]
            assert v4_partial.metadata_json["security_partition_id"] == "part-s"

            # Now full discovery sweep (version_ids=None) syncs v3-live and clears v4_partial
            res2 = await search_index_service.reconcile_version_temporal_payloads(
                session,
                q_client,
                project_id="proj-stale",
                version_ids=None,
            )
            await session.commit()
            assert res2["synced_versions"] == 1
            await session.refresh(v4_partial)
            assert v4_partial.metadata_json["qdrant_temporal_sync_degraded"] is False
            assert "qdrant_temporal_sync_failed_version_ids" not in v4_partial.metadata_json
            assert v4_partial.metadata_json["security_partition_id"] == "part-s"


@pytest.mark.asyncio
async def test_resolve_temporal_supersedes_matches_legacy_ready_and_excludes_revoked_failed(sync_db):
    """MINOR-2: resolve_temporal_supersedes accepts legacy search_status='READY' predecessors
    and excludes status in ('REVOKED', 'FAILED') matching document_service."""
    t1 = datetime(2024, 1, 1, tzinfo=UTC)
    t2 = datetime(2025, 1, 1, tzinfo=UTC)

    async with sync_db() as session:
        session.add(Source(id="src-leg", name="Leg", sag_source_config_id="cfg-leg"))
        session.add(
            Document(
                id="doc-leg",
                source_id="src-leg",
                tenant_id="t-leg",
                project_id="p-leg",
                filename="legacy.md",
                storage_path="legacy.md",
                status=DocumentStatus.READY,
                is_active=True,
            )
        )
        await session.flush()

        v_legacy = DocumentVersion(
            id="v-legacy",
            document_id="doc-leg",
            version_no=1,
            file_hash="fh-l1",
            effective_from=t1,
            valid_from=t1,
            valid_to=MAX_VALID_TO,
            status="READY",
            search_status="READY",
        )
        v_revoked = DocumentVersion(
            id="v-revoked",
            document_id="doc-leg",
            version_no=2,
            file_hash="fh-l2",
            effective_from=t1 + timedelta(days=30),
            valid_from=t1 + timedelta(days=30),
            valid_to=MAX_VALID_TO,
            status="REVOKED",
            search_status="SEARCH_READY",
        )
        v_failed = DocumentVersion(
            id="v-failed",
            document_id="doc-leg",
            version_no=3,
            file_hash="fh-l3",
            effective_from=t1 + timedelta(days=60),
            valid_from=t1 + timedelta(days=60),
            valid_to=MAX_VALID_TO,
            status="FAILED",
            search_status="SEARCH_READY",
        )
        v_new = DocumentVersion(
            id="v-new",
            document_id="doc-leg",
            version_no=4,
            file_hash="fh-l4",
            effective_from=t2,
            status="RECEIVED",
            search_status="PENDING",
        )
        session.add_all([v_legacy, v_revoked, v_failed, v_new])
        await session.flush()

        res = await temporal_service.resolve_temporal_supersedes(
            session, current_version=v_new, document_id="doc-leg"
        )
        assert res["action"] == "SUPERSEDED"
        assert v_new.supersedes_id == "v-legacy"
        assert v_legacy.valid_to == t2
        assert v_revoked.valid_to == MAX_VALID_TO
        assert v_failed.valid_to == MAX_VALID_TO

