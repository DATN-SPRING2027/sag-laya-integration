from __future__ import annotations

import asyncio
import json
from hashlib import sha256

import httpx

from sag_api.core.db import SessionLocal
from sag_api.db.models.document import Document
from sag_api.db.models.routing_rag import CanonicalBlock, DocumentVersion, SearchUnit
from sag_api.db.models.source import Source
from sag_api.services.routing_tree_service import (
    KnowledgeEdgeInput,
    KnowledgeUnitInput,
    RoutingBenchmarkCase,
    TreeBuildConfig,
    build_routing_snapshot,
)
from sag_api.services.tree_publish_service import SearchUnitAssignment


def _snapshot(project_id: str, variant: int = 0):
    units = [
        KnowledgeUnitInput(
            unit_id=f"ku-{index:02d}",
            tenant_id="tenant-c",
            project_id=project_id,
            security_partition_id="partition-public" if index < 4 else "partition-private",
            dense=(1.0, float(index < 4), float(variant if index == 0 else 0)),
            sparse=(
                (
                    ("alpha", "alpha", "gamma", "gamma", "beta", "beta", "delta", "delta")[index],
                    1.0 + variant if index == 0 else 1.0,
                ),
            ),
            entities=(("Alpha thông tin", "Alpha", "Gamma", "Gamma", "Beta", "Beta", "Delta", "Delta")[index],),
        )
        for index in range(8)
    ]
    edges = [KnowledgeEdgeInput(f"ku-{i:02d}", f"ku-{i + 1:02d}", 0.8) for i in range(7)]
    benchmark = [RoutingBenchmarkCase("query-1", "ku-00", ("ku-00", "ku-01"))]
    return build_routing_snapshot(
        units,
        edges,
        config=TreeBuildConfig(target_cluster_size=4, max_cluster_size=4, max_children=4),
        benchmark=benchmark,
    )


def _stable_id(*parts: str) -> str:
    return sha256(":".join(parts).encode()).hexdigest()[:32]


def _assignments(snapshot) -> tuple[SearchUnitAssignment, ...]:
    result = []
    for unit in snapshot.manifest["units"]:
        index = int(unit["unit_id"][-2:])
        source_number = index % 4 // 2
        source_id = _stable_id(snapshot.project_id, "source", str(source_number))
        result.append(
            SearchUnitAssignment(
                knowledge_unit_id=unit["unit_id"],
                search_unit_id=_stable_id(snapshot.project_id, "search-unit", str(index)),
                source_id=source_id,
                document_version_id=_stable_id(snapshot.project_id, "version", str(source_number)),
                partition_id=unit["partition"],
            )
        )
    return tuple(result)


def _public_scope(snapshot):
    assignment = next(item for item in _assignments(snapshot) if item.partition_id == "partition-public")
    return {
        "project_id": snapshot.project_id,
        "source_ids": [assignment.source_id],
        "document_version_ids": [assignment.document_version_id],
        "tenant_id": snapshot.tenant_id,
        "partition_id": assignment.partition_id,
    }


async def _seed_search_units(snapshot) -> None:
    project_id = snapshot.project_id
    assignments = _assignments(snapshot)
    async with SessionLocal() as session:
        for source_number in range(2):
            source_id = _stable_id(project_id, "source", str(source_number))
            document_id = _stable_id(project_id, "document", str(source_number))
            version_id = _stable_id(project_id, "version", str(source_number))
            block_id = _stable_id(project_id, "block", str(source_number))
            session.add(Source(id=source_id, name="Checkpoint C fixture", sag_source_config_id=source_id))
            await session.flush()
            session.add(
                Document(
                    id=document_id,
                    source_id=source_id,
                    tenant_id="tenant-c",
                    project_id=project_id,
                    filename="fixture.txt",
                    storage_path="fixture.txt",
                )
            )
            await session.flush()
            session.add(
                DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    version_no=1,
                    file_hash="0" * 64,
                )
            )
            await session.flush()
            session.add(
                CanonicalBlock(
                    id=block_id,
                    document_version_id=version_id,
                    ordinal=0,
                    block_type="paragraph",
                    page_from=1,
                    page_to=1,
                    section_path="fixture",
                    normalized_text="fixture",
                    content_hash="0" * 64,
                )
            )
            await session.flush()
        for item in assignments:
            source_number = 0 if item.source_id == _stable_id(project_id, "source", "0") else 1
            block_id = _stable_id(project_id, "block", str(source_number))
            session.add(
                SearchUnit(
                    id=item.search_unit_id,
                    document_version_id=item.document_version_id,
                    block_from_id=block_id,
                    block_to_id=block_id,
                    security_partition_id=item.partition_id,
                    content_hash="0" * 64,
                    token_count=1,
                    page_from=1,
                    page_to=1,
                    section_path="fixture",
                )
            )
        await session.commit()


def _qdrant_client(
    candidate,
    *,
    wrong_count: bool = False,
    acknowledged_only: bool = False,
    wrong_source_payload: bool = False,
    seen_writes: list[dict] | None = None,
    shared_points: dict[str, dict] | None = None,
    fail_write_number: int | None = None,
    query_pause: asyncio.Event | None = None,
    query_started: asyncio.Event | None = None,
):
    from sag_api.services.search_index_service import generate_search_unit_point_id

    collection = f"search_units_{candidate.project_id}"
    initial_points = {
        generate_search_unit_point_id(collection, item.search_unit_id): {
            "search_unit_id": item.search_unit_id,
            "source_id": item.source_id,
            "document_version_id": item.document_version_id,
            "project_id": candidate.project_id,
            "tenant_id": candidate.tenant_id,
            "security_partition_id": item.partition_id,
        }
        for item in candidate.search_unit_assignments
    }
    points = shared_points if shared_points is not None else {}
    write_count = 0
    for point_id, payload in initial_points.items():
        points.setdefault(point_id, payload)
    if wrong_source_payload:
        next(iter(points.values()))["source_id"] = "wrong-source"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal write_count
        if request.url.path.endswith("/points/query"):
            if query_started is not None:
                query_started.set()
            if query_pause is not None:
                await asyncio.wait_for(query_pause.wait(), timeout=10)
            body = json.loads(request.content)

            def matches(payload, condition):
                value = payload.get(condition["key"])
                match = condition["match"]
                if "value" in match:
                    return value == match["value"]
                values = value if isinstance(value, list) else [value]
                return any(item in match["any"] for item in values)

            query_filter = body["filter"]
            result = [
                {"id": point_id, "payload": payload, "score": 1.0}
                for point_id, payload in points.items()
                if all(matches(payload, condition) for condition in query_filter["must"])
                and (
                    not query_filter.get("should")
                    or any(matches(payload, condition) for condition in query_filter["should"])
                )
            ]
            return httpx.Response(200, json={"result": {"points": result[: body["limit"]]}})
        if request.url.path.endswith("/points/payload/delete"):
            if request.url.params.get("wait") != "true":
                return httpx.Response(400, json={"status": "error"})
            body = json.loads(request.content)
            filters = body["filter"]["must"]
            for payload in points.values():
                if all(payload.get(item["key"]) == item["match"]["value"] for item in filters):
                    for key in body["keys"]:
                        payload.pop(key, None)
            return httpx.Response(
                200,
                json={"status": "ok", "result": {"status": "completed", "operation_id": 1}},
            )
        if request.url.path.endswith("/points/payload"):
            if request.url.params.get("wait") != "true":
                return httpx.Response(400, json={"status": "error"})
            write_count += 1
            if write_count == fail_write_number:
                return httpx.Response(500, text="injected partial write failure")
            body = json.loads(request.content)
            if seen_writes is not None:
                seen_writes.append(body)
            for point_id in body["points"]:
                points[point_id].update(body["payload"])
            status = "acknowledged" if acknowledged_only else "completed"
            return httpx.Response(200, json={"status": "ok", "result": {"status": status, "operation_id": 1}})
        if request.url.path.endswith("/points/count"):
            body = json.loads(request.content)
            filters = body["filter"]["must"]
            count = sum(
                all(payload.get(item["key"]) == item["match"]["value"] for item in filters)
                for payload in points.values()
            )
            if wrong_count:
                count -= 1
            return httpx.Response(200, json={"status": "ok", "result": {"count": count}})
        if request.url.path.endswith("/points"):
            body = json.loads(request.content)
            result = [{"id": point_id, "payload": points[point_id]} for point_id in body["ids"] if point_id in points]
            return httpx.Response(200, json={"status": "ok", "result": result})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://qdrant.test")
