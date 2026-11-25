"""TDD regression tests for P2 Step 4: structured session turn extractor and substrate hierarchy bias."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sag_api.core.config import settings
from sag_api.services.session_context import IncrementalSessionIndex, build_ecce_context_pack
from sag_api.services.session_context import extractor as ext


def test_session_extractor_removes_all_semantic_regexes() -> None:
    for removed in (
        "_HONORIFIC_ENTITY_RE",
        "_TECH_ANCHOR_RE",
        "_CLAUSE_SPLIT_RE",
        "_PRE_CONFIRM_GUARD_RE",
        "_POST_CONFIRM_NEG_RE",
        "_CONFIRMED_CUE_RE",
        "_CONSIDERING_CUE_RE",
        "_PROPOSED_CUE_RE",
        "_NEGATED_CUE_RE",
        "_ISSUE_CUE_RE",
        "_ANAPHORIC_SHORT_RE",
        "_EXPLICIT_REPLACED_SPAN_PATTERNS",
        "_SLOT_PATTERNS",
        "_OWNER_SLOT_RE",
        "classify_epistemic_state",
        "extract_superseded_entity_keys",
        "infer_topic_slot",
    ):
        assert not hasattr(ext, removed), f"Legacy semantic regex/helper {removed} must be removed"


@pytest.mark.asyncio
async def test_extract_turns_structured_batch_merges_e0_e1_caches_and_supersedes(monkeypatch) -> None:
    ext.clear_turn_extraction_cache()
    monkeypatch.setattr(settings, "llm_structured_output_mode", "json_object")

    payload = {
        "turns": [
            {
                "turn_id": "t-1",
                "epistemic_status": "PROPOSED",
                "topic_slot_key": "slot:database",
                "is_anaphoric_or_short": False,
                "is_interrogative": False,
                "entities": [{"surface": "MongoDB", "canonical_key": "mongodb", "soft_aliases": []}],
                "superseded_entities": [],
            },
            {
                "turn_id": "t-2",
                "epistemic_status": "CONFIRMED",
                "topic_slot_key": "slot:database",
                "is_anaphoric_or_short": False,
                "is_interrogative": False,
                "entities": [{"surface": "PostgreSQL 16", "canonical_key": "postgresql", "soft_aliases": []}],
                "superseded_entities": ["mongodb"],
            },
        ]
    }
    complete = AsyncMock(return_value=json.dumps(payload))
    llm = SimpleNamespace(configured=True, complete=complete)

    turns = [
        {"id": "t-1", "role": "user", "content": "Đề xuất dùng MongoDB cho ADR-004."},
        {"id": "t-2", "role": "user", "content": "Chốt chuyển sang PostgreSQL 16 thay cho MongoDB."},
    ]
    results, degraded, reason = await ext.extract_turns_structured_batch(
        turns, llm=llm, tenant_id="tenant-p2", thread_id="thread-p2"
    )
    assert degraded is False
    assert reason is None
    assert complete.await_count == 1
    assert complete.await_args.kwargs.get("response_format") == {"type": "json_object"}
    assert results[0].epistemic_status == "PROPOSED"
    assert results[1].epistemic_status == "CONFIRMED"
    assert results[1].topic_slot_key == "slot:database"
    assert "entity:mongodb" in results[1].superseded_entity_keys
    # Structural E0/E1 (ADR-004) preserved alongside LLM entities:
    assert any(m.key == "entity:adr-004" for m in results[0].entities)

    # Second call for identical normalized content + role + tenant + thread hits cache -> 0 new LLM calls:
    results_cached, degraded2, _ = await ext.extract_turns_structured_batch(
        turns, llm=llm, tenant_id="tenant-p2", thread_id="thread-p2"
    )
    assert degraded2 is False
    assert complete.await_count == 1
    assert [r.epistemic_status for r in results_cached] == ["PROPOSED", "CONFIRMED"]


@pytest.mark.asyncio
async def test_extract_turns_structured_batch_fails_closed_on_missing_or_broken_llm() -> None:
    ext.clear_turn_extraction_cache()
    turns = [{"id": "t-9", "role": "user", "content": "Chốt dùng PostgreSQL thay cho MongoDB!"}]

    res_none, deg_none, reason_none = await ext.extract_turns_structured_batch(turns, llm=None)
    assert deg_none is True
    assert reason_none == "session_extractor:llm_unconfigured"
    assert res_none[0].epistemic_status == "FACT"
    assert res_none[0].topic_slot_key is None
    assert res_none[0].superseded_entity_keys == ()
    assert res_none[0].degraded is True

    bad_llm = SimpleNamespace(configured=True, complete=AsyncMock(return_value="not-json"))
    res_bad, deg_bad, reason_bad = await ext.extract_turns_structured_batch(turns, llm=bad_llm)
    assert deg_bad is True
    assert reason_bad == "session_extractor:invalid_json"
    assert res_bad[0].epistemic_status == "FACT"
    assert res_bad[0].superseded_entity_keys == ()


@pytest.mark.asyncio
async def test_substrate_propagates_extraction_degraded_and_uses_hierarchy_bias() -> None:
    ext.clear_turn_extraction_cache()
    idx = IncrementalSessionIndex("thread-degraded")
    await idx.append_messages(
        [
            {"id": "m-1", "role": "user", "content": "Chốt dùng PostgreSQL cho hệ thống."},
            {"id": "m-2", "role": "assistant", "content": "Đã ghi nhận."},
        ],
        embedder=None,
        llm=None,
    )
    assert idx.extraction_degraded is True
    assert idx.turns[0].epistemic_status == "FACT"

    pack = await build_ecce_context_pack(
        idx,
        query="Quyết định database là gì?",
        budget_tokens=1000,
        route_decision={
            "memory_scope": "session_history",
            "temporal_mode": "current_effective",
            "hierarchy_bias": "early",
            "topic_slot_key": "slot:database",
            "entities": ["PostgreSQL"],
            "model": "test",
            "degraded": False,
        },
        keep_recent_default=0,
    )
    assert pack.audit_trace.extraction_degraded is True
    assert pack.audit_trace.extraction_degraded_reason == "session_extractor:llm_unconfigured"
    assert pack.audit_trace.to_dict()["extraction_degraded"] is True
