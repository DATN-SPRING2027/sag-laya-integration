"""Deterministic Stable ID and Idempotency Key Engine for Knowledge Routing RAG.

Defined according to Section 4.3 of phase-0-contracts-and-foundations.md.
"""

from __future__ import annotations

import hashlib
import uuid

NAMESPACE_SAG = uuid.NAMESPACE_URL


def generate_doc_id(tenant_id: str, project_id: str, logical_source_id: str) -> str:
    """Generate deterministic UUIDv5 for a logical document."""
    name = f"sag:doc:{tenant_id}:{project_id}:{logical_source_id}"
    return str(uuid.uuid5(NAMESPACE_SAG, name))


def generate_version_id(doc_id: str, version_no: int) -> str:
    """Generate deterministic UUIDv5 for a document version."""
    name = f"sag:ver:{doc_id}:{version_no}"
    return str(uuid.uuid5(NAMESPACE_SAG, name))


def generate_block_id(version_id: str, ordinal: int, content_hash: str) -> str:
    """Generate deterministic UUIDv5 for a canonical block."""
    name = f"sag:block:{version_id}:{ordinal}:{content_hash}"
    return str(uuid.uuid5(NAMESPACE_SAG, name))


def generate_unit_id(version_id: str, ordinal: int) -> str:
    """Generate deterministic UUIDv5 for a search unit."""
    name = f"sag:unit:{version_id}:{ordinal}"
    return str(uuid.uuid5(NAMESPACE_SAG, name))


def generate_qdrant_point_id(unit_id: str) -> str:
    """Generate deterministic Qdrant point UUID from search unit ID."""
    name = f"sag:qdrant:search_units:{unit_id}"
    return str(uuid.uuid5(NAMESPACE_SAG, name))


def compute_payload_hash(payload_bytes: bytes) -> str:
    """Calculate SHA-256 checksum of file/binary payload."""
    return hashlib.sha256(payload_bytes).hexdigest()


def normalize_idempotency_key(client_token: str) -> str:
    """Validate and normalize client-supplied idempotency key."""
    cleaned = client_token.strip()
    if not cleaned:
        raise ValueError("Idempotency key cannot be empty")
    if len(cleaned) > 64:
        # Truncate or hash if longer than 64 characters
        return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    return cleaned
