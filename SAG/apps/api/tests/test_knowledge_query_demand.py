import asyncio
from types import SimpleNamespace

import pytest

from sag_api.services import search_unit_retrieval_service as retrieval
from sag_api.services.query_routing_service import (
    RouteDecision,
    routing_scope_for_group,
    scope_fingerprint,
)


class _SessionContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None


def _group(source_id: str, version: str):
    return retrieval._SearchGroup(
        project_id="project-a",
        tenant_id="tenant-a",
        partition_id="partition-a",
        source=SimpleNamespace(id=source_id),
        versions=(version,),
    )


def _route(group, nodes, *, fingerprint=None):
    return RouteDecision(
        project_id=group.project_id,
        tree_version="tree-v1",
        selected_nodes=tuple(nodes),
        membership_node_ids=("unit-a",),
        acl_scope_fingerprint=fingerprint or scope_fingerprint(routing_scope_for_group(group)),
        reason_code="ROUTED",
    )


@pytest.mark.asyncio
async def test_search_records_deduplicated_nodes_only_for_acl_validated_routes(monkeypatch):
    first = _group("source-a", "version-a")
    second = _group("source-b", "version-b")
    mismatched = _group("source-c", "version-c")
    decisions = {
        first.scope_key: _route(first, ("node-a", "node-b")),
        second.scope_key: _route(second, ("node-b", "node-c")),
        mismatched.scope_key: _route(mismatched, ("node-secret",), fingerprint="wrong-scope"),
    }
    writes = []

    async def record(_session, **kwargs):
        writes.append(kwargs)
        return len(kwargs["node_ids"])

    monkeypatch.setattr(retrieval, "SessionLocal", _SessionContext)
    monkeypatch.setattr(retrieval, "record_query_demand", record)

    await retrieval._record_routed_query_demand([first, second, mismatched], decisions)

    assert len(writes) == 1
    assert writes[0]["tenant_id"] == "tenant-a"
    assert writes[0]["project_id"] == "project-a"
    assert writes[0]["security_partition_id"] == "partition-a"
    assert writes[0]["tree_version"] == "tree-v1"
    assert writes[0]["node_ids"] == ("node-a", "node-b", "node-c")
    assert "query" not in writes[0]


@pytest.mark.asyncio
async def test_search_skips_demand_write_for_unrouted_or_acl_mismatched_group(monkeypatch):
    group = _group("source-a", "version-a")
    no_route = RouteDecision(project_id=group.project_id, reason_code="NO_TREE")
    writes = []

    async def record(_session, **kwargs):
        writes.append(kwargs)
        return 1

    monkeypatch.setattr(retrieval, "SessionLocal", _SessionContext)
    monkeypatch.setattr(retrieval, "record_query_demand", record)

    await retrieval._record_routed_query_demand([group], {group.scope_key: no_route})

    assert writes == []


@pytest.mark.asyncio
async def test_search_schedules_demand_write_without_waiting_and_tracks_task(monkeypatch):
    group = _group("source-a", "version-a")
    decision = _route(group, ("node-a",))
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_write(_groups, _decisions):
        started.set()
        await release.wait()

    monkeypatch.setattr(retrieval, "_record_routed_query_demand", delayed_write)
    existing_tasks = set(retrieval._routed_query_demand_tasks)

    retrieval._schedule_routed_query_demand([group], {group.scope_key: decision})

    scheduled_tasks = retrieval._routed_query_demand_tasks - existing_tasks
    assert len(scheduled_tasks) == 1
    task = next(iter(scheduled_tasks))
    assert not started.is_set()
    try:
        await started.wait()
        assert not task.done()
    finally:
        release.set()
    await task
    await asyncio.sleep(0)

    assert task not in retrieval._routed_query_demand_tasks
