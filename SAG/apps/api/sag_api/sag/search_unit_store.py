"""Bounded read-only queries for Phase 2C SearchUnit points."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from sag_api.core.bm25_tokenizer import (
    BM25_STEM_WEIGHT,
    COMMON_STOPWORDS,
    english_stem,
    expand_continuous_script_tokens,
    token_hash_bucket,
    tokenize_bm25_tokens,
)


class SearchIndexUnavailable(RuntimeError):
    """A sanitized failure while reading the canonical search index."""


@dataclass(frozen=True, slots=True)
class SearchUnitHit:
    point_id: str
    score: float
    payload: dict[str, Any]


_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SPARSE_QUERY_STOPWORDS = COMMON_STOPWORDS
_QUERY_FRAMING_STOPWORDS = frozenset(
    {
        "question", "answer", "response", "give", "discuss", "reference",
        "based", "according", "provide", "provided", "information", "please", "state",
        "tell", "explain", "describe", "whether",
    }
)


def _expand_continuous_script_tokens(tokens: list[str]) -> list[str]:
    """Split unspaced CJK/Thai/Lao/Khmer/Myanmar runs into overlapping character bigrams."""
    return expand_continuous_script_tokens(tokens)


def build_sparse_query_vector(query: str) -> dict[str, list[int] | list[float]]:
    """Encode query terms with the unified Unicode BM25 tokenizer and stable MD5 buckets.

    Uses sub-linear query TF and alphanumeric/entity boosting so repeated framing words
    never drown out discriminative entity, numeric, or domain anchors.
    """
    tokenized = tokenize_bm25_tokens(query, filter_stopwords=True)
    if not tokenized.expanded_tokens:
        return {"indices": [], "values": []}

    counts = Counter(tokenized.expanded_tokens)
    is_long_query = len(tokenized.tokens) >= 6
    sparse: dict[int, float] = {}
    for token, frequency in counts.items():
        base_tf = 1.0 + 0.35 * math.log(frequency) if frequency > 1 else 1.0
        if is_long_query and token in _QUERY_FRAMING_STOPWORDS:
            weight = base_tf * 0.2
        elif is_long_query and (any(ch.isdigit() for ch in token) or token in tokenized.proper_tokens):
            weight = base_tf * 2.2
        else:
            weight = base_tf

        token_id = token_hash_bucket(token)
        sparse[token_id] = max(sparse.get(token_id, 0.0), round(float(weight), 4))
        stem = english_stem(token)
        if stem:
            stem_id = token_hash_bucket(stem)
            sparse[stem_id] = max(
                sparse.get(stem_id, 0.0),
                round(float(weight) * BM25_STEM_WEIGHT, 4),
            )
    indices = sorted(sparse)
    return {"indices": indices, "values": [sparse[index] for index in indices]}


def build_search_filter(
    *,
    project_id: str,
    tenant_id: str,
    partition_id: str,
    document_version_ids: list[str],
    tree_version: str | None = None,
    routing_slot: str | None = None,
    membership_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build exact ACL filters plus an optional version-pinned tree membership."""
    versions = sorted(set(document_version_ids))
    if not project_id.strip() or not tenant_id.strip() or not partition_id.strip() or not versions:
        raise ValueError("A complete authorized SearchUnit scope is required")
    filter_body: dict[str, Any] = {
        "must": [
            {"key": "project_id", "match": {"value": project_id}},
            {"key": "tenant_id", "match": {"value": tenant_id}},
            {"key": "security_partition_id", "match": {"value": partition_id}},
            {"key": "document_version_id", "match": {"any": versions}},
        ]
    }
    has_tree_scope = tree_version is not None or routing_slot is not None or membership_node_ids is not None
    if has_tree_scope:
        nodes = sorted({node.strip() for node in membership_node_ids or [] if node.strip()})
        if not tree_version or routing_slot not in {"SLOT_A", "SLOT_B"} or not nodes or len(nodes) > 256:
            raise ValueError("A complete bounded tree membership filter is required")
        slot = "a" if routing_slot == "SLOT_A" else "b"
        filter_body["must"].append({"key": f"tree_version_{slot}", "match": {"value": tree_version}})
        filter_body["should"] = [
            {"key": f"primary_node_{slot}", "match": {"any": nodes}},
            {"key": f"secondary_node_ids_{slot}", "match": {"any": nodes}},
        ]
    return filter_body


class SearchUnitQdrantStore:
    """Thin adapter over Qdrant's points/query and exact-point read endpoints."""

    def __init__(self, client: httpx.AsyncClient, *, max_retries: int = 1) -> None:
        self._client = client
        self._max_retries = max(0, min(max_retries, 3))

    @staticmethod
    def _parse_points(points: list[Any]) -> list[SearchUnitHit]:
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
        return self._parse_points(points)

    async def search_batch(
        self,
        collection: str,
        *,
        searches: list[dict[str, Any]],
    ) -> list[list[SearchUnitHit]]:
        """Execute multiple vector/sparse sub-queries in a single Qdrant HTTP round-trip."""
        if not searches:
            return []
        active_indices: list[int] = []
        active_bodies: list[dict[str, Any]] = []
        for idx, spec in enumerate(searches):
            limit = int(spec.get("limit") or 0)
            vector_name = str(spec.get("vector_name") or "")
            query = spec.get("query")
            search_filter = spec.get("search_filter")
            if limit <= 0:
                continue
            if vector_name == "bm25_sparse" and (
                not isinstance(query, dict) or not query.get("indices") or not query.get("values")
            ):
                continue
            active_indices.append(idx)
            active_bodies.append(
                {
                    "query": query,
                    "using": vector_name,
                    "filter": search_filter,
                    "limit": limit,
                    "with_payload": True,
                    "with_vector": False,
                }
            )
        results: list[list[SearchUnitHit]] = [[] for _ in searches]
        if not active_bodies:
            return results
        path = f"/collections/{quote(collection, safe='')}/points/query/batch"
        data = await self._request("POST", path, json={"searches": active_bodies})
        batch_result = data.get("result")
        if not isinstance(batch_result, list) or len(batch_result) != len(active_bodies):
            raise SearchIndexUnavailable("Search index returned an invalid batch query response")
        for idx, item in zip(active_indices, batch_result, strict=True):
            points = (item or {}).get("points") if isinstance(item, dict) else None
            if not isinstance(points, list):
                raise SearchIndexUnavailable("Search index returned an invalid query response")
            results[idx] = self._parse_points(points)
        return results

    async def count(self, collection: str, *, search_filter: dict[str, Any]) -> int:
        """Check derived-index availability within the exact authorized scope."""
        path = f"/collections/{quote(collection, safe='')}/points/count"
        data = await self._request("POST", path, json={"filter": search_filter, "exact": True})
        result = data.get("result")
        count = result.get("count") if isinstance(result, dict) else None
        if type(count) is not int or count < 0:
            raise SearchIndexUnavailable("Search index returned an invalid count")
        return count

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
            except httpx.InvalidURL:
                raise SearchIndexUnavailable("Search index request configuration is invalid") from None
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt < self._max_retries:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
                raise SearchIndexUnavailable("Search index is temporarily unavailable") from None
            except httpx.RequestError:
                raise SearchIndexUnavailable("Search index request failed") from None
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
