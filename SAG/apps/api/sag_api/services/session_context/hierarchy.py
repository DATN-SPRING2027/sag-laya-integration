"""Deterministic temporal views over authorized raw traces; no generated summaries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.reader import HistoryRead, order_key, trace_work
from sag_api.services.session_context.scope import _check_time
from sag_api.services.session_context.types import (
    Coverage,
    EvidenceSpan,
    MessageReference,
    SessionScope,
    SessionSnapshot,
    digest_json,
    require,
)


@dataclass(frozen=True, slots=True)
class TraceUnit:
    unit_id: str
    kind: Literal["turn", "window", "episode", "local"]
    spans: tuple[EvidenceSpan, ...]
    children: tuple[str, ...]
    origin: MessageReference | None = None
    anchor_id: str | None = None


@dataclass(frozen=True, slots=True)
class TraceHierarchy:
    snapshot: SessionSnapshot
    units: tuple[TraceUnit, ...]
    coverage: Coverage
    unknown_provenance: bool

    def resolve_spans(self, unit_id: str, *, budget: BudgetState) -> tuple[EvidenceSpan, ...]:
        # Lookup construction is metered too; later persistent indexing can replace it.
        budget.consume_index_units(len(self.units))
        lookup = {unit.unit_id: unit for unit in self.units}
        require(unit_id in lookup, "unit_unavailable")
        stack, visited, spans = [unit_id], set(), {}
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            unit = lookup[current]
            budget.consume_index_units(1 + len(unit.children) + len(unit.spans))
            visited.add(current)
            for span in unit.spans:
                spans.setdefault(span.evidence_id, span)
            stack.extend(reversed(unit.children))
        return tuple(spans.values())


def build_hierarchy(history: HistoryRead, *, scope: SessionScope, budget: BudgetState,
                    window_turns: int, episode_windows: int, local_radius: int,
                    gap_seconds: float, now: float | None = None) -> TraceHierarchy:
    """Window/episode sizes bound unit fanout, never the retained history.

    Episodes use temporal continuity only, not the paper's semantic continuity.
    Every excluded ordinal and large time gap is a boundary for all parent views.
    Trusted HistoryRead is internal; callers must revalidate T2 before model use.
    """
    _check_time(scope, now)
    require(history.snapshot.scope_fingerprint == scope.fingerprint, "hierarchy_scope")
    require(all(type(value) is int and value > 0 for value in (window_turns, episode_windows)), "unit_size")
    require(type(local_radius) is int and local_radius >= 0, "local_radius")
    require(type(gap_seconds) in (int, float) and math.isfinite(gap_seconds) and gap_seconds > 0, "time_gap")
    require(len(history.messages) == len(history.ordinals), "history_ordinals")
    units = []
    scope_fingerprint = scope.fingerprint

    def append(kind, *, spans=(), children=(), origin=None, anchor_id=None):
        budget.consume_index_units(1 + len(spans) + len(children))
        unit_id = digest_json({"version": "temporal-v1", "scope": scope_fingerprint, "kind": kind,
                               "spans": [span.evidence_id for span in spans], "children": children,
                               "origin": (origin.message_id, origin.content_hash) if origin else None,
                               "anchor": anchor_id})
        unit = TraceUnit(unit_id, kind, spans, children, origin, anchor_id)
        units.append(unit)
        return unit_id

    def group(kind, identifiers, start, end, *, anchor_id=None):
        end = min(end, len(identifiers))
        budget.consume_index_units(end - start)  # Reserve before copying child references.
        return append(kind, children=tuple(identifiers[start:end]), anchor_id=anchor_id)

    segments: list[list[str]] = []
    message_ids = set()
    previous_key, previous_ordinal = None, None
    for message, ordinal in zip(history.messages, history.ordinals, strict=True):
        budget.consume_index_units(trace_work(message))
        key = order_key(message)
        require(message.key == scope.key and message.message_id in history.coverage.eligible_ids, "history_scope")
        require(message.message_id not in message_ids, "duplicate_message")
        message_ids.add(message.message_id)
        require(previous_key is None or previous_key < key, "history_order")
        require(type(ordinal) is int and ordinal >= 0
                and (previous_ordinal is None or ordinal > previous_ordinal), "history_ordinals")
        if (previous_key is None or ordinal != previous_ordinal + 1
                or (key[0] - previous_key[0]).total_seconds() > gap_seconds):
            segments.append([])
        reference = message.reference
        spans = (EvidenceSpan(reference, 0, len(message.content)),) if message.content else ()
        segments[-1].append(append("turn", spans=spans, origin=reference))
        previous_key, previous_ordinal = key, ordinal
    require(message_ids == history.coverage.eligible_ids, "hierarchy_coverage")
    for turns in segments:
        windows = []
        for start in range(0, len(turns), window_turns):
            windows.append(group("window", turns, start, start + window_turns))
        for start in range(0, len(windows), episode_windows):
            group("episode", windows, start, start + episode_windows)
        for index in range(len(turns)):
            group("local", turns, max(0, index - local_radius), index + local_radius + 1, anchor_id=turns[index])
    budget.consume_index_units(0)  # Also check deadline/cancel before publishing a complete build.
    _check_time(scope, now)
    return TraceHierarchy(history.snapshot, tuple(units), history.coverage, history.unknown_provenance)
