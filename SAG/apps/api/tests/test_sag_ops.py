"""Tests for sag_ops CLI operations tool (Phase 11 Hardening)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from scripts.sag_ops import run_cache_status, run_rebuild_qdrant, run_rollback_tree


def test_sag_ops_cache_status(capsys):
    run_cache_status()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "enabled" in data
    assert "current_entries" in data
    assert "max_entries" in data
    assert data["max_entries"] == 512


@pytest.mark.asyncio
async def test_sag_ops_rebuild_qdrant_dispatch(capsys):
    with (
        patch("sag_api.sag.engine_manager.EngineManager.get_search_unit_qdrant_client", new_callable=AsyncMock),
        patch("sag_api.sag.engine_manager.EngineManager.get_sag_embedding", new_callable=AsyncMock),
        patch(
            "sag_api.services.rebuild_service.rebuild_search_index_for_project",
            new_callable=AsyncMock,
        ) as mock_rebuild,
    ):
        mock_rebuild.return_value = {
            "project_id": "proj-1",
            "collection_name": "search_units_proj-1",
            "rebuilt_versions_count": 1,
            "total_search_units_rebuilt": 10,
        }

        ok = await run_rebuild_qdrant("proj-1")
        assert ok is True
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "success"
        assert data["result"]["project_id"] == "proj-1"
        assert data["result"]["total_search_units_rebuilt"] == 10


@pytest.mark.asyncio
async def test_sag_ops_rollback_tree_dispatch(capsys):
    from types import SimpleNamespace

    with (
        patch("sag_api.sag.engine_manager.EngineManager.get_search_unit_qdrant_client", new_callable=AsyncMock),
        patch(
            "sag_api.services.tree_rollback_service.rollback_tree_candidate",
            new_callable=AsyncMock,
        ) as mock_rollback,
    ):
        mock_rollback.return_value = SimpleNamespace(
            project_id="proj-1",
            active_slot="SLOT_A",
            active_tree_version="tree-prev",
            active_search_epoch=3,
            retained_previous_tree_version="tree-rolled",
        )

        ok = await run_rollback_tree("proj-1", "tree-prev")
        assert ok is True
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "success"
        assert data["project_id"] == "proj-1"
        assert data["active_slot"] == "SLOT_A"
        assert data["active_search_epoch"] == 3


@pytest.mark.asyncio
async def test_sag_ops_empty_project_id_validation(capsys):
    assert await run_rebuild_qdrant("") is False
    captured = capsys.readouterr()
    assert "project_id cannot be empty" in captured.err

    assert await run_rollback_tree("   ", "") is False
    captured = capsys.readouterr()
    assert "project_id cannot be empty" in captured.err


@pytest.mark.asyncio
async def test_sag_ops_rebuild_qdrant_handles_error(capsys):
    with (
        patch("sag_api.sag.engine_manager.EngineManager.get_search_unit_qdrant_client", new_callable=AsyncMock),
        patch("sag_api.sag.engine_manager.EngineManager.get_sag_embedding", new_callable=AsyncMock),
        patch(
            "sag_api.services.rebuild_service.rebuild_search_index_for_project",
            side_effect=RuntimeError("database connection timeout"),
        ),
    ):
        ok = await run_rebuild_qdrant("proj-1")
        assert ok is False
        captured = capsys.readouterr()
        data = json.loads(captured.err)
        assert data["status"] == "error"
        assert "database connection timeout" in data["message"]

