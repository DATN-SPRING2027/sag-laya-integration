"""测试夹具：在导入 sag_api 之前把配置指向临时目录（settings 为进程级单例）。"""

import os
import sys
import tempfile
from math import sqrt

import pytest
from zleap.sag.core.adapters import registry
from zleap.sag.core.adapters.capabilities import Capability
from zleap.sag.core.adapters.models import BulkResult, Filter, VectorHit, VectorQuery, VectorRecord

_TMP = tempfile.mkdtemp(prefix="sag-test-")
os.environ.setdefault("SAG_DATABASE_URL", f"sqlite+aiosqlite:///{_TMP}/sag.db?timeout=30")
os.environ.setdefault("SAG_DATA_DIR", f"{_TMP}/sag")
os.environ.setdefault("SAG_UPLOAD_DIR", f"{_TMP}/uploads")
os.environ["SAG_DSH_CONNECTION_FILE"] = f"{_TMP}/dsh-connection.json"
os.environ.setdefault("SAG_DEBUG", "false")
os.environ.setdefault("SAG_SAG_LANGUAGE", "zh")
os.environ.setdefault("SAG_SAG_VECTOR_PROVIDER", "qdrant")
os.environ.setdefault("SAG_SAG_RELATIONAL_PROVIDER", "sqlite")
os.environ.setdefault("SAG_AUTH_MODE", "password")
# The suite intentionally shares one temporary SQLite database. Startup warmup
# would otherwise provision sources persisted by earlier cases in the background
# while the current case is writing, introducing cross-test lock contention.
os.environ["SAG_ENGINE_WARMUP_COUNT"] = "0"
# 强制离线：即使存在带真实 key 的 .env，也保证测试确定性（不发起 LLM 调用）
os.environ["SAG_LLM_API_KEY"] = ""
os.environ["SAG_LLM_BASE_URL"] = ""
os.environ["SAG_EMBEDDING_API_KEY"] = ""
os.environ["SAG_MINERU_API_KEY"] = ""
os.environ["SAG_MINERU_BASE_URL"] = ""


class _InMemoryVectorStore:
    """Keep unit tests on the provider contract without requiring Qdrant."""

    provider = "qdrant"
    capabilities = frozenset({Capability.VECTOR_KNN, Capability.FILTERED_KNN})
    _collections: dict[str, dict[str, VectorRecord]] = {}

    def __init__(self, **_kwargs):
        pass

    @staticmethod
    def _matches(expression: Filter | None, record: VectorRecord) -> bool:
        if expression is None:
            return True
        value = record.id if expression.field == "_id" else record.payload.get(expression.field)
        if expression.operator == "eq":
            return value == expression.value
        if expression.operator == "in":
            return value in expression.value
        if expression.operator == "range":
            return all(
                (bound != "gt" or value is not None and value > target)
                and (bound != "gte" or value is not None and value >= target)
                and (bound != "lt" or value is not None and value < target)
                and (bound != "lte" or value is not None and value <= target)
                for bound, target in expression.value.items()
            )
        if expression.operator == "exists":
            return value is not None
        if expression.operator == "and":
            return all(_InMemoryVectorStore._matches(child, record) for child in expression.children)
        if expression.operator == "or":
            return any(_InMemoryVectorStore._matches(child, record) for child in expression.children)
        if expression.operator == "not":
            return not _InMemoryVectorStore._matches(expression.children[0], record)
        return False

    async def upsert(self, collection: str, records: list[VectorRecord]) -> BulkResult:
        bucket = self._collections.setdefault(collection, {})
        for record in records:
            bucket[record.id] = record
        return BulkResult(succeeded_ids=tuple(record.id for record in records))

    async def get_many(self, collection: str, ids: list[str]) -> list[VectorRecord]:
        bucket = self._collections.get(collection, {})
        return [bucket[record_id] for record_id in ids if record_id in bucket]

    async def query(self, collection: str, request: VectorQuery) -> list[VectorHit]:
        hits: list[VectorHit] = []
        for record in self._collections.get(collection, {}).values():
            if not self._matches(request.filters, record):
                continue
            vector = record.vectors.get(request.vector_field)
            if request.vector is None:
                score = 1.0
            elif not vector:
                continue
            else:
                denominator = sqrt(
                    sum(value * value for value in request.vector) * sum(value * value for value in vector)
                )
                score = (
                    sum(left * right for left, right in zip(request.vector, vector, strict=True)) / denominator
                    if denominator
                    else 0.0
                )
            hits.append(
                VectorHit(
                    record.id,
                    score,
                    dict(record.payload),
                    dict(record.vectors) if request.include_vectors else {},
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: request.limit]

    async def delete(self, collection: str, ids: list[str]) -> BulkResult:
        bucket = self._collections.get(collection, {})
        for record_id in ids:
            bucket.pop(record_id, None)
        return BulkResult(succeeded_ids=tuple(ids))

    async def publish(self, _collections: list[str]) -> None:
        return None

    async def schema_object_names(self) -> frozenset[str]:
        return frozenset(self._collections)

    async def validate_schema_object(self, _name: str, _dimensions: int | None) -> None:
        return None

    async def create_schema_object(self, name: str, _dimensions: int) -> None:
        self._collections.setdefault(name, {})

    async def optimize(self) -> bool:
        return True

    async def ping(self, _timeout: float = 5.0) -> None:
        return None

    async def close(self) -> None:
        return None


# Keep the application configured as Qdrant while unit tests use a deterministic
# in-memory implementation. Real Qdrant coverage belongs in an integration job.
import sag_api.sag.qdrant_store  # noqa: E402,F401

registry.register("vector", "qdrant", _InMemoryVectorStore)


@pytest.fixture(autouse=True)
async def _isolate_persisted_jobs():
    """A test must not recover queued jobs created by an earlier app lifespan."""
    yield
    if "sag_api.core.db" not in sys.modules:
        return

    from sqlalchemy import delete, inspect

    from sag_api.core.db import SessionLocal, engine
    from sag_api.db.models import Document, Job
    from sag_api.enums import DocumentStatus

    async with engine.connect() as connection:
        exists = await connection.run_sync(lambda sync: inspect(sync).has_table(Job.__tablename__))
    if not exists:
        return

    async with SessionLocal() as session:
        await session.execute(delete(Job))
        await session.execute(
            delete(Document).where(
                Document.status.in_(
                    [
                        DocumentStatus.PAUSING,
                        DocumentStatus.DELETING,
                        DocumentStatus.DELETE_FAILED,
                    ]
                )
            )
        )
        await session.commit()
