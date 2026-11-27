"""Shared lifespan-managed httpx.AsyncClient pool to avoid per-request TCP/TLS teardown."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

_CLIENTS: dict[tuple[int, str], httpx.AsyncClient] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_pooled_http_client(
    name: str,
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: float | httpx.Timeout = 15.0,
) -> httpx.AsyncClient:
    """Return a reusable AsyncClient keyed by active event loop and logical pool name."""
    key = (_loop_key(), f"{name}|{base_url}")
    client = _CLIENTS.get(key)
    if client is not None and not client.is_closed:
        return client
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "limits": httpx.Limits(max_connections=64, max_keepalive_connections=24, keepalive_expiry=30.0),
    }
    if base_url:
        kwargs["base_url"] = base_url
    if headers:
        kwargs["headers"] = headers
    client = httpx.AsyncClient(**kwargs)
    _CLIENTS[key] = client
    return client


async def close_pooled_http_clients() -> None:
    """Close all pooled HTTP clients cleanly on app shutdown."""
    loop_id = _loop_key()
    keys = [k for k in list(_CLIENTS) if k[0] in (loop_id, 0)]
    for key in keys:
        client = _CLIENTS.pop(key, None)
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
