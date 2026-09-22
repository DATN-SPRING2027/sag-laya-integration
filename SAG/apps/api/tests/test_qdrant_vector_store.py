from __future__ import annotations

import json

import httpx
from zleap.sag.core.adapters.models import Filter, VectorQuery

from sag_api.core.config import Settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.sag.qdrant_store import QdrantVectorConfig, QdrantVectorStore


def test_qdrant_engine_config_keeps_postgres_relational_store() -> None:
    settings = Settings(
        _env_file=None,
        sag_vector_provider="qdrant",
        sag_relational_provider="postgres",
        sag_qdrant_url="http://qdrant:6333",
    )

    config = build_engine_config(settings)

    assert config.vector.provider == "qdrant"
    assert config.vector.url == "http://qdrant:6333"
    assert config.relational.provider == "postgres"


async def test_qdrant_query_translates_filter_and_score() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": "point-id",
                            "score": 0.8,
                            "payload": {"_sag_id": "chunk-1", "source_id": "source-1"},
                            "vector": {"content_vector": [1.0, 2.0]},
                        }
                    ]
                }
            },
        )

    store = QdrantVectorStore(QdrantVectorConfig())
    store._client = httpx.AsyncClient(
        base_url="http://qdrant:6333",
        transport=httpx.MockTransport(handler),
    )
    try:
        hits = await store.query(
            "source_chunks",
            VectorQuery(
                vector=[1.0, 2.0],
                vector_field="content_vector",
                filters=Filter.eq("source_id", "source-1"),
            ),
        )
    finally:
        await store.close()

    assert hits[0].id == "chunk-1"
    assert hits[0].score == 0.9
    assert requests[0].url.path == "/collections/source_chunks/points/query"
    assert json.loads(requests[0].content)["filter"] == {
        "key": "source_id",
        "match": {"value": "source-1"},
    }
