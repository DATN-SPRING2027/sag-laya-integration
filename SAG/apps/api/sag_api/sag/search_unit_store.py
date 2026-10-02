"""Bounded read-only queries for Phase 2C SearchUnit points."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class SearchIndexUnavailable(RuntimeError):
    """A sanitized failure while reading the canonical search index."""


@dataclass(frozen=True, slots=True)
class SearchUnitHit:
    point_id: str
    score: float
    payload: dict[str, Any]


_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def build_sparse_query_vector(query: str) -> dict[str, list[int] | list[float]]:
    """Encode query terms with Phase 2C's tokenizer and stable MD5 buckets.

    Qdrant applies the collection IDF modifier. Query term weights therefore
    encode frequency only; they do not invent a query-local corpus statistic.
    """
    counts = Counter(_TOKEN_PATTERN.findall(query.lower()))
    sparse: dict[int, float] = {}
    for token, frequency in counts.items():
        token_id = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
        sparse[token_id] = max(sparse.get(token_id, 0.0), float(frequency))
    indices = sorted(sparse)
    return {"indices": indices, "values": [sparse[index] for index in indices]}


def build_search_filter(
    *,
    project_id: str,
    tenant_id: str,
    partition_id: str,
    document_version_ids: list[str],
) -> dict[str, Any]:
    """Build mandatory pre-top-k scope filters; callers cannot omit a scope."""
    versions = sorted(set(document_version_ids))
    if not project_id.strip() or not tenant_id.strip() or not partition_id.strip() or not versions:
        raise ValueError("A complete authorized SearchUnit scope is required")
    return {
        "must": [
            {"key": "project_id", "match": {"value": project_id}},
            {"key": "tenant_id", "match": {"value": tenant_id}},
            {"key": "security_partition_id", "match": {"value": partition_id}},
            {"key": "document_version_id", "match": {"any": versions}},
        ]
    }


class SearchUnitQdrantStore:
    """Thin adapter over Qdrant's points/query and exact-point read endpoints."""

    def __init__(self, client: httpx.AsyncClient, *, max_retries: int = 1) -> None:
        self._client = client
        self._max_retries = max(0, min(max_retries, 3))

    async def search(
        self,
        collection: str,
        *,
        vector_name: str,
        query: list[float] | dict[str, list[int] | list[float]],
        search_filter: dict[str, Any],
        limit: int,
    ) -> list[SearchUnitHit]:
        if not limit:
            return []
        if vector_name == "bm25_sparse" and (
            not isinstance(query, dict) or not query.get("indices") or not query.get("values")
        ):
            return []
        path = f"/collections/{quote(collection, safe='')}/points/query"
        body = {
            "query": query,
            "using": vector_name,
            "filter": search_filter,
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        data = await self._request("POST", path, json=body)
        points = (data.get("result") or {}).get("points")
        if not isinstance(points, list):
            raise SearchIndexUnavailable("Search index returned an invalid query response")
        hits: list[SearchUnitHit] = []
        for point in points:
            if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
                raise SearchIndexUnavailable("Search index returned an invalid point")
            try:
                score = float(point["score"])
            except (KeyError, TypeError, ValueError) as error:
                raise SearchIndexUnavailable("Search index returned an invalid score") from error
            point_id = point.get("id")
            if not isinstance(point_id, (str, int)) or not math.isfinite(score):
                raise SearchIndexUnavailable("Search index returned an invalid point")
            hits.append(SearchUnitHit(str(point_id), score, point["payload"]))
        return hits

    async def get_point(self, collection: str, point_id: str) -> SearchUnitHit | None:
        path = f"/collections/{quote(collection, safe='')}/points/{quote(point_id, safe='')}"
        data = await self._request("GET", path, params={"with_payload": "true", "with_vector": "false"})
        point = data.get("result")
        if point is None:
            return None
        if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
            raise SearchIndexUnavailable("Search index returned an invalid point")
        try:
            score = float(point.get("score") or 0.0)
        except (TypeError, ValueError) as error:
            raise SearchIndexUnavailable("Search index returned an invalid point") from error
        if not math.isfinite(score):
            raise SearchIndexUnavailable("Search index returned an invalid point")
        return SearchUnitHit(str(point.get("id") or point_id), score, point["payload"])

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.InvalidURL as error:
                raise SearchIndexUnavailable("Search index request configuration is invalid") from error
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt < self._max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                raise SearchIndexUnavailable("Search index is temporarily unavailable") from error
            except httpx.RequestError as error:
                raise SearchIndexUnavailable("Search index request failed") from error
            if response.status_code in _TRANSIENT_STATUS_CODES and attempt < self._max_retries:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            if response.is_error:
                raise SearchIndexUnavailable("Search index query failed")
            try:
                data = response.json()
            except ValueError as error:
                raise SearchIndexUnavailable("Search index returned an invalid response") from error
            if not isinstance(data, dict):
                raise SearchIndexUnavailable("Search index returned an invalid response")
            return data
        raise SearchIndexUnavailable("Search index is temporarily unavailable")
