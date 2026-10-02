from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from sag_api.sag.search_unit_store import (
    SearchIndexUnavailable,
    SearchUnitQdrantStore,
    build_search_filter,
    build_sparse_query_vector,
)


def test_sparse_query_uses_producer_tokenizer_hash_and_sorted_unique_indices():
    query = "SAG sag Checkpoint-A 42"
    actual = build_sparse_query_vector(query)
    tokens = {"sag": 2, "checkpoint": 1, "a": 1, "42": 1}
    expected = {
        int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % 1_000_000: float(count)
        for token, count in tokens.items()
    }

    assert actual == {
        "indices": sorted(expected),
        "values": [expected[index] for index in sorted(expected)],
    }


def test_search_filter_requires_all_acl_dimensions_and_versions():
    actual = build_search_filter(
        project_id="project-a",
        tenant_id="tenant-a",
        partition_id="partition-a",
        document_version_ids=["v2", "v1", "v2"],
    )

    assert actual["must"] == [
        {"key": "project_id", "match": {"value": "project-a"}},
        {"key": "tenant_id", "match": {"value": "tenant-a"}},
        {"key": "security_partition_id", "match": {"value": "partition-a"}},
        {"key": "document_version_id", "match": {"any": ["v1", "v2"]}},
    ]


@pytest.mark.parametrize(
    "scope",
    [
        {"project_id": "", "tenant_id": "tenant", "partition_id": "p", "document_version_ids": ["v"]},
        {"project_id": "p", "tenant_id": "", "partition_id": "p", "document_version_ids": ["v"]},
        {"project_id": "p", "tenant_id": "t", "partition_id": "", "document_version_ids": ["v"]},
        {"project_id": "p", "tenant_id": "t", "partition_id": "p", "document_version_ids": []},
    ],
)
def test_search_filter_fails_closed_without_complete_scope(scope):
    with pytest.raises(ValueError):
        build_search_filter(**scope)


@pytest.mark.asyncio
async def test_query_sends_scope_filter_before_top_k_and_parses_hits():
    seen = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"result": {"points": [{"id": "point-1", "score": 0.42, "payload": {"search_unit_id": "unit-1"}}]}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://qdrant")
    store = SearchUnitQdrantStore(client, max_retries=0)
    result = await store.search(
        "search_units_project-a",
        vector_name="bm25_sparse",
        query={"indices": [12], "values": [1.0]},
        search_filter=build_search_filter(
            project_id="project-a",
            tenant_id="tenant-a",
            partition_id="partition-a",
            document_version_ids=["version-a"],
        ),
        limit=8,
    )
    await client.aclose()

    assert result[0].point_id == "point-1"
    assert result[0].payload["search_unit_id"] == "unit-1"
    assert seen[0]["filter"]["must"][-1]["key"] == "document_version_id"
    assert seen[0]["limit"] == 8
    assert seen[0]["using"] == "bm25_sparse"


@pytest.mark.asyncio
async def test_empty_sparse_query_does_not_send_invalid_qdrant_request():
    async def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an empty sparse query must be skipped")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called), base_url="http://qdrant")
    store = SearchUnitQdrantStore(client)
    result = await store.search(
        "search_units_project-a",
        vector_name="bm25_sparse",
        query={"indices": [], "values": []},
        search_filter=build_search_filter(
            project_id="project-a",
            tenant_id="tenant-a",
            partition_id="partition-a",
            document_version_ids=["version-a"],
        ),
        limit=8,
    )
    await client.aclose()

    assert result == []


@pytest.mark.asyncio
async def test_transient_query_retries_once_and_never_exposes_upstream_body():
    attempts = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="api-key=secret-value")
        return httpx.Response(200, json={"result": {"points": []}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://qdrant")
    store = SearchUnitQdrantStore(client)
    result = await store.search(
        "search_units_project-a",
        vector_name="content_vector",
        query=[0.1, 0.2],
        search_filter=build_search_filter(
            project_id="project-a",
            tenant_id="tenant-a",
            partition_id="partition-a",
            document_version_ids=["version-a"],
        ),
        limit=4,
    )
    await client.aclose()

    assert result == []
    assert attempts == 2


@pytest.mark.asyncio
async def test_permanent_query_error_is_sanitized_and_not_retried():
    attempts = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, text="api-key=secret-value")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="http://qdrant")
    store = SearchUnitQdrantStore(client)
    with pytest.raises(SearchIndexUnavailable, match="Search index query failed") as error:
        await store.search(
            "search_units_project-a",
            vector_name="content_vector",
            query=[0.1],
            search_filter=build_search_filter(
                project_id="project-a",
                tenant_id="tenant-a",
                partition_id="partition-a",
                document_version_ids=["version-a"],
            ),
            limit=4,
        )
    await client.aclose()

    assert "secret-value" not in str(error.value)
    assert attempts == 1


@pytest.mark.asyncio
async def test_invalid_index_request_is_sanitized():
    class InvalidUrlClient:
        async def request(self, *_args, **_kwargs):
            raise httpx.InvalidURL("https://user:secret-value@qdrant.internal/private")

    store = SearchUnitQdrantStore(InvalidUrlClient())
    with pytest.raises(SearchIndexUnavailable) as error:
        await store.search(
            "search_units_project-a",
            vector_name="content_vector",
            query=[0.1],
            search_filter=build_search_filter(
                project_id="project-a",
                tenant_id="tenant-a",
                partition_id="partition-a",
                document_version_ids=["version-a"],
            ),
            limit=1,
        )

    assert "secret-value" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_request_error_does_not_keep_secret_exception_cause():
    class RequestFailureClient:
        async def request(self, *_args, **_kwargs):
            raise httpx.RequestError("https://user:secret-value@qdrant.internal/private")

    store = SearchUnitQdrantStore(RequestFailureClient())
    with pytest.raises(SearchIndexUnavailable, match="Search index request failed") as error:
        await store.search(
            "search_units_project-a",
            vector_name="content_vector",
            query=[0.1],
            search_filter=build_search_filter(
                project_id="project-a",
                tenant_id="tenant-a",
                partition_id="partition-a",
                document_version_ids=["version-a"],
            ),
            limit=1,
        )

    assert "secret-value" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_search_unit_client_is_pooled_and_closed_by_engine_manager(monkeypatch):
    from sag_api.core.config import settings
    from sag_api.sag import engine_manager as engine_manager_module

    created = []

    class ManagedClient:
        def __init__(self, **options):
            self.options = options
            self.closed = False

        async def aclose(self):
            self.closed = True

    def make_client(**options):
        client = ManagedClient(**options)
        created.append(client)
        return client

    monkeypatch.setattr(engine_manager_module.httpx, "AsyncClient", make_client)
    manager_settings = settings.model_copy(
        update={
            "sag_qdrant_url": "http://qdrant-test",
            "sag_qdrant_api_key": "test-key",
            "search_source_timeout": 7.0,
        }
    )
    manager = engine_manager_module.EngineManager(manager_settings)

    first = await manager.get_search_unit_qdrant_client()
    second = await manager.get_search_unit_qdrant_client()

    assert first is second
    assert len(created) == 1
    assert first.options["headers"] == {"api-key": "test-key"}

    manager_settings.sag_qdrant_url = "http://qdrant-reconfigured"
    third = await manager.get_search_unit_qdrant_client()
    assert third is not first
    assert first.closed is True

    await manager.aclose_all()
    assert third.closed is True
