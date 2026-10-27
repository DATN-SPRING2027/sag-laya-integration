"""Bounded, killable CPU work; children receive data, never database sessions or clients."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from weakref import WeakKeyDictionary


def _calculate(operation, payload):
    if operation == "prepare_tree":
        from sag_api.services.tree_publish_service import prepare_tree_candidate

        return prepare_tree_candidate(payload["snapshot"], payload["assignments"])
    if operation == "dedup_candidates":
        from sag_api.services.dedup_and_temporal_service import _dedup_candidates

        return _dedup_candidates(
            [SimpleNamespace(**row) for row in payload["current"]],
            [SimpleNamespace(**row) for row in payload["prior"]],
            payload["signatures"],
            payload["embeddings"],
            payload["version_id"],
        )
    if operation == "minhash":
        import re

        from sag_api.services.dedup_and_temporal_service import compute_minhash_signature

        return {
            key: compute_minhash_signature(re.findall(r"\w+", text.lower())) for key, text in payload["texts"].items()
        }
    if operation == "graph":
        from sag_api.services.knowledge_service import KnowledgeConfig, graph_payload

        return graph_payload(
            [SimpleNamespace(**row) for row in payload["units"]], KnowledgeConfig.model_validate(payload["config"])
        )
    if operation == "routing":
        import json

        from sag_api.services.routing_tree_service import build_routing_snapshot
        from sag_api.services.tree_publish_service import _checksum_manifest

        options = dict(payload)
        metadata = options.pop("knowledge_metadata", None)
        snapshot = build_routing_snapshot(**options)
        if metadata is not None:
            manifest = snapshot.manifest
            manifest.update(metadata)
            manifest["checksum"] = _checksum_manifest(manifest)
            manifest["tree_version"] = "tree-" + manifest["checksum"][:24]
            snapshot = replace(
                snapshot,
                tree_version=manifest["tree_version"],
                manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False),
            )
        return replace(snapshot, metrics=dict(snapshot.metrics), quality_gates=dict(snapshot.quality_gates))
    raise ValueError("Unknown CPU operation")


def _entry(connection, operation, payload):
    try:
        connection.send((True, _calculate(operation, payload)))
    except Exception as error:
        # Error bodies can contain source data. Only the class crosses this boundary.
        connection.send((False, type(error).__name__))
    finally:
        connection.close()


def _execute(operation, payload, timeout, cancelled):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_entry, args=(child, operation, payload))
    started = time.monotonic()
    try:
        process.start()
        child.close()
        while not parent.poll(0.05):
            if cancelled.is_set():
                raise InterruptedError("CPU operation cancelled")
            if time.monotonic() - started >= timeout:
                raise TimeoutError(f"CPU {operation} exceeded its deadline")
            if not process.is_alive():
                raise RuntimeError(f"CPU {operation} exited without a result")
        ok, result = parent.recv()
        if not ok:
            if result == "ValueError":
                raise ValueError(f"CPU {operation} rejected invalid input")
            if result == "PublishVerificationError":
                from sag_api.services.tree_publish_service import PublishVerificationError

                raise PublishVerificationError(f"CPU {operation} rejected an invalid tree candidate")
            raise RuntimeError(f"CPU {operation} failed ({result})")
        return result
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join()


_SLOTS = WeakKeyDictionary()


async def run_cpu(operation, payload):
    from sag_api.core.config import settings

    loop = asyncio.get_running_loop()
    slot = _SLOTS.setdefault(loop, asyncio.Semaphore(1))
    deadline = loop.time() + settings.knowledge_graph_timeout_seconds
    await asyncio.wait_for(slot.acquire(), timeout=max(0, deadline - loop.time()))
    try:
        cancelled = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                _execute,
                operation,
                payload,
                max(0, deadline - loop.time()),
                cancelled,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            # Do not release admission until the child has actually been reaped.
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
    finally:
        slot.release()
