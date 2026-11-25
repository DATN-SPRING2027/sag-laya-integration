"""Shared test-only StructuredLLMClient double for ECCE / persistence / branch lineage tests."""

from __future__ import annotations

import json
from typing import Any


def is_session_extraction_prompt(messages: list[dict[str, Any]] | None) -> bool:
    if not messages or len(messages) < 2:
        return False
    sys_msg = str(messages[0].get("content") or "")
    usr_msg = str(messages[1].get("content") or "")
    return '"turns"' in usr_msg and "epistemic_status" in sys_msg


def _analyze_test_turn(turn: dict[str, Any]) -> dict[str, Any]:
    turn_id = str(turn.get("turn_id") or "")
    content = str(turn.get("content") or "")
    lower = content.casefold()

    entities: list[dict[str, Any]] = []
    superseded: list[str] = []
    slot: str | None = None
    epistemic = "FACT"
    is_anaphoric = len(content.split()) <= 9
    is_interrogative = content.rstrip().endswith("?")

    def add_ent(surface: str, key: str | None = None, aliases: list[str] | None = None) -> None:
        canonical = (key or surface).casefold().strip()
        if any(e["canonical_key"] == canonical for e in entities):
            return
        entities.append(
            {
                "surface": surface,
                "canonical_key": canonical,
                "soft_aliases": list(aliases or []),
            }
        )

    # Entities for synthetic test corpora
    if "minh fe" in lower:
        add_ent("Minh FE", "minh fe", ["minh"])
    if "minh be" in lower:
        add_ent("Minh BE", "minh be", ["minh"])
    if "chị lan" in lower:
        add_ent("Lan", "lan")
    if "anh tuấn" in lower:
        add_ent("Tuấn", "tuấn")
    if "mongodb" in lower:
        add_ent("MongoDB", "mongodb")
    if "cockroachdb" in lower:
        add_ent("CockroachDB", "cockroachdb")
    if "postgresql 18" in lower:
        add_ent("PostgreSQL 18", "postgresql", ["postgresql 18"])
    elif "postgresql 16" in lower:
        add_ent("PostgreSQL 16", "postgresql", ["postgresql 16"])
    elif "unlogged table" in lower:
        add_ent("PostgreSQL UNLOGGED TABLE", "postgresql unlogged table", ["postgresql"])
    elif "listen/notify" in lower:
        add_ent("PostgreSQL LISTEN/NOTIFY", "postgresql listen/notify", ["postgresql"])
    elif "postgresql" in lower:
        add_ent("PostgreSQL", "postgresql")
    if "mysql" in lower:
        add_ent("MySQL", "mysql")
    if "redis cluster" in lower:
        add_ent("Redis Cluster", "redis cluster", ["redis"])
    elif "redis streams" in lower:
        add_ent("Redis Streams", "redis streams", ["redis"])
    if "react" in lower:
        add_ent("React", "react")
    if "vue" in lower:
        add_ent("Vue", "vue")
    if "svelte" in lower:
        add_ent("Svelte", "svelte")
    if "qdrant" in lower:
        add_ent("Qdrant", "qdrant")
    if "milvus" in lower:
        add_ent("Milvus", "milvus")
    if "vnpay" in lower:
        add_ent("VNPAY", "vnpay")
    if "payment-gateway-v2" in lower:
        add_ent("payment-gateway-v2", "payment-gateway-v2")
    if "vault-hsm-prod" in lower:
        add_ent("Vault-HSM-Prod", "vault-hsm-prod")
    if "mtls" in lower:
        add_ent("mTLS", "mtls")
    if "keycloak" in lower:
        add_ent("Keycloak", "keycloak")
    if "pgbackrest" in lower:
        add_ent("pgBackRest", "pgbackrest")
    if "ubuntu 24.04" in lower:
        add_ent("Ubuntu 24.04 LTS", "ubuntu 24.04 lts")
    if "docker compose" in lower:
        add_ent("Docker Compose", "docker compose")
    if "chronos99" in lower:
        add_ent("chronos99", "chronos99")

    # Topic slots
    if any(k in lower for k in ("mongodb", "cockroachdb", "postgresql 16", "postgresql 18", "mysql", "database")):
        if "listen/notify" not in lower and "unlogged table" not in lower:
            slot = "slot:database"
    if any(k in lower for k in ("hàng đợi", "listen/notify", "redis streams")):
        slot = "slot:queue"
    elif any(k in lower for k in ("session cache", "redis cluster", "unlogged table")):
        slot = "slot:session_cache"
    elif "kho vector" in lower or ("phương án 2" in lower and "anh tuấn" in lower):
        slot = "slot:vector_db"
        if "phương án 2" in lower:
            add_ent("Qdrant", "qdrant")
            is_anaphoric = True
    elif "minh fe" in lower:
        slot = "slot:owner_frontend"
    elif "minh be" in lower:
        slot = "slot:owner_backend"
    elif "chị lan" in lower:
        slot = "slot:owner_owasp"
    elif "ngân sách" in lower and "1450 usd" in lower:
        slot = "slot:budget"
    elif "golive" in lower:
        slot = "slot:golive"
    elif "keycloak" in lower or "sso" in lower:
        slot = "slot:auth"
    elif "pgbackrest" in lower or "rpo" in lower:
        slot = "slot:backup"
    elif "docker compose" in lower:
        slot = "slot:deployment"

    # Epistemic state & superseded entities
    if is_interrogative:
        epistemic = "FACT"
    elif "cảnh báo" in lower or "đang bị lỗi" in lower:
        epistemic = "OPEN_ISSUE"
    elif any(k in lower for k in ("chưa chốt", "đang cân nhắc", "cân nhắc")):
        epistemic = "CONSIDERING"
    elif any(k in lower for k in ("không thống nhất", "đừng quyết định")):
        epistemic = "NEGATED"
    elif any(
        k in lower
        for k in (
            "chốt",
            "thống nhất",
            "quyết định",
            "chuyển sang dùng vue",
            "bắt buộc cấu hình",
            "ấn định",
            "ghi chú triển khai",
        )
    ):
        epistemic = "CONFIRMED"
    elif any(k in lower for k in ("đề xuất", "gửi 2 lựa chọn")):
        epistemic = "PROPOSED"

    if "thay cho redis cluster" in lower:
        superseded.append("redis cluster")
    if "thay cho listen/notify" in lower:
        superseded.append("postgresql listen/notify")
    if "bỏ redis streams" in lower:
        superseded.append("redis streams")
    if "bỏ mongodb" in lower or "thay mongodb bằng" in lower:
        superseded.append("mongodb")
    if "bỏ cockroachdb" in lower:
        superseded.append("cockroachdb")
    if "thay postgresql 16 bằng" in lower:
        superseded.append("postgresql")
    if "thay cho react" in lower:
        superseded.append("react")
    if "bỏ vue" in lower:
        superseded.append("vue")

    return {
        "turn_id": turn_id,
        "epistemic_status": epistemic,
        "topic_slot_key": slot,
        "is_anaphoric_or_short": is_anaphoric,
        "is_interrogative": is_interrogative,
        "entities": entities,
        "superseded_entities": superseded,
    }


def mock_extract_turns_json(messages: list[dict[str, Any]]) -> str:
    payload = json.loads(str(messages[1]["content"]))
    turns = payload.get("turns") or []
    analyzed = [_analyze_test_turn(t) for t in turns if isinstance(t, dict)]
    return json.dumps({"turns": analyzed}, ensure_ascii=False)


class StructuredExtractorTestLLM:
    """Test-only StructuredLLMClient producing deterministic JSON for synthetic test turns."""

    configured = True

    def __init__(self) -> None:
        self.extraction_calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> str:
        del response_format, _kwargs
        if is_session_extraction_prompt(messages):
            self.extraction_calls += 1
            return mock_extract_turns_json(messages)
        return "Summary"
