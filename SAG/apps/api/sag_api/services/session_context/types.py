"""Immutable internal contracts, not wire DTOs or authorization credentials."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sag_api.core.errors import ValidationError


class ContextContractError(ValidationError):
    code = "session_context_invalid"

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Session context rejected: {reason}.", code=self.code)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContextContractError(reason)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _identifiers(value: object) -> bool:
    return isinstance(value, frozenset) and all(_identifier(item) for item in value)


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def digest_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionKey:
    owner_key: str
    agent_id: str
    thread_id: str

    def __post_init__(self):
        require(all(_identifier(value) for value in (self.owner_key, self.agent_id, self.thread_id)), "identity")


@dataclass(frozen=True, slots=True)
class SessionScope:
    key: SessionKey
    issuer: str
    subject: str
    organization_id: str
    tenant_id: str
    project_ids: frozenset[str]
    partition_ids: frozenset[str]
    source_ids: frozenset[str]
    requested_source_ids: frozenset[str] | None
    expires_at: float

    def __post_init__(self):
        require(isinstance(self.key, SessionKey), "identity")
        require(all(_identifier(value) for value in (self.issuer, self.subject, self.organization_id)), "principal")
        require(isinstance(self.tenant_id, str), "tenant")
        require(all(_identifiers(value) for value in (self.project_ids, self.partition_ids, self.source_ids)), "ids")
        require(self.requested_source_ids is None or _identifiers(self.requested_source_ids), "source_filter")
        require(self.requested_source_ids is None or self.source_ids <= self.requested_source_ids,
                "source_filter_widened")
        require(type(self.expires_at) in (int, float) and math.isfinite(self.expires_at), "principal_expiry")

    @property
    def partition_fingerprint(self) -> str:
        return digest_json({
            "owner": self.key.owner_key, "agent": self.key.agent_id, "thread": self.key.thread_id,
            "issuer": self.issuer, "subject": self.subject, "org": self.organization_id,
            "tenant": self.tenant_id, "projects": sorted(self.project_ids), "partitions": sorted(self.partition_ids),
        })

    @property
    def fingerprint(self) -> str:
        return digest_json({"partition": self.partition_fingerprint, "sources": sorted(self.source_ids),
                            "requested_sources": (None if self.requested_source_ids is None
                                                  else sorted(self.requested_source_ids))})


@dataclass(frozen=True, slots=True)
class MessageReference:
    message_id: str
    content_hash: str

    def __post_init__(self):
        require(_identifier(self.message_id) and _digest(self.content_hash), "message_reference")


@dataclass(frozen=True, slots=True)
class TraceMessage:
    key: SessionKey
    message_id: str
    role: Literal["user", "assistant"]
    content: str = field(repr=False)
    created_at: datetime
    status: Literal["ok", "failed", "cancelled"] = "ok"
    provenance: Literal["recorded", "unknown"] = "unknown"
    source_ids: frozenset[str] = frozenset()
    dependencies: tuple[MessageReference, ...] = ()
    partition_fingerprint: str | None = None

    def __post_init__(self):
        require(isinstance(self.key, SessionKey) and _identifier(self.message_id), "message_identity")
        require(self.role in {"user", "assistant"} and self.status in {"ok", "failed", "cancelled"}, "message_role")
        require(isinstance(self.content, str), "message_content")
        require(isinstance(self.created_at, datetime) and self.created_at.utcoffset() is not None, "message_time")
        require(self.provenance in {"recorded", "unknown"} and _identifiers(self.source_ids), "provenance")
        require(isinstance(self.dependencies, tuple)
                and all(isinstance(ref, MessageReference) for ref in self.dependencies), "dependencies")
        require(len({ref.message_id for ref in self.dependencies}) == len(self.dependencies), "duplicate_dependency")
        require(self.partition_fingerprint is None or _digest(self.partition_fingerprint), "partition_stamp")

    @property
    def reference(self) -> MessageReference:
        return MessageReference(self.message_id, hashlib.sha256(self.content.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    ref: MessageReference
    start: int
    end: int

    def __post_init__(self):
        require(isinstance(self.ref, MessageReference), "span_reference")
        require(type(self.start) is int and type(self.end) is int and 0 <= self.start < self.end, "span_offsets")

    @property
    def evidence_id(self) -> str:
        return f"session:{self.ref.message_id}:{self.ref.content_hash}:{self.start}:{self.end}"


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    key: SessionKey
    scope_fingerprint: str
    generation: str
    history_digest: str

    def __post_init__(self):
        require(isinstance(self.key, SessionKey) and _identifier(self.generation), "snapshot_identity")
        require(_digest(self.scope_fingerprint) and _digest(self.history_digest), "snapshot_digest")


@dataclass(frozen=True, slots=True)
class Coverage:
    eligible_ids: frozenset[str]
    visited_ids: frozenset[str]

    def __post_init__(self):
        require(_identifiers(self.eligible_ids) and _identifiers(self.visited_ids), "coverage_ids")
        require(self.visited_ids <= self.eligible_ids, "coverage_outside_scope")

    @property
    def traversal_complete(self) -> bool:
        return self.visited_ids == self.eligible_ids


@dataclass(frozen=True, slots=True)
class ContextSelection:
    snapshot: SessionSnapshot
    mode: Literal["normal", "full_session"]
    state: Literal["ready", "partial", "indexing", "unsupported"]
    spans: tuple[EvidenceSpan, ...]
    coverage: Coverage
    reason: str | None = None

    def __post_init__(self):
        require(isinstance(self.snapshot, SessionSnapshot) and isinstance(self.coverage, Coverage), "selection_types")
        require(self.mode in {"normal", "full_session"}, "selection_mode")
        require(self.state in {"ready", "partial", "indexing", "unsupported"}, "selection_state")
        require(isinstance(self.spans, tuple) and all(isinstance(span, EvidenceSpan) for span in self.spans), "spans")
        require(len({span.evidence_id for span in self.spans}) == len(self.spans), "duplicate_evidence")
        require((self.reason is None) if self.state == "ready" else _identifier(self.reason), "selection_reason")
        require(self.state not in {"indexing", "unsupported"} or not self.spans, "unusable_evidence")


@dataclass(frozen=True, slots=True)
class ResolvedEvidence:
    span: EvidenceSpan
    kind: Literal["user_statement", "assistant_statement"]
    text: str = field(repr=False)
    dependency_refs: frozenset[MessageReference]
    source_ids: frozenset[str]
