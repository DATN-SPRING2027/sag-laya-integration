"""SAG Evidence-Centric Context Engine (SAG-ECCE) — Zero-Mem + Main-Native Session Memory."""

from sag_api.services.session_context.models import (
    AblationMode,
    ContextAuditTrace,
    DecisionLedgerEntry,
    EntityMention,
    EpisodeSegment,
    EpistemicStatus,
    IndexedSentence,
    IndexedTurn,
    RejectedCandidateTrace,
    SelectedEvidenceGroup,
    SessionContextPack,
)
from sag_api.services.session_context.optimizer import build_ecce_context_pack
from sag_api.services.session_context.runtime import (
    HIDDEN_SCOPE_PLACEHOLDER,
    invalidate_session_thread_cache,
    load_full_thread_messages_keyset,
    log_session_audit_trace,
    prepare_session_context_for_ask,
)
from sag_api.services.session_context.substrate import (
    SESSION_INDEX_CACHE,
    IncrementalSessionIndex,
)

__all__ = [
    "AblationMode",
    "ContextAuditTrace",
    "DecisionLedgerEntry",
    "EntityMention",
    "EpisodeSegment",
    "EpistemicStatus",
    "HIDDEN_SCOPE_PLACEHOLDER",
    "IncrementalSessionIndex",
    "IndexedSentence",
    "IndexedTurn",
    "RejectedCandidateTrace",
    "SESSION_INDEX_CACHE",
    "SelectedEvidenceGroup",
    "SessionContextPack",
    "build_ecce_context_pack",
    "invalidate_session_thread_cache",
    "load_full_thread_messages_keyset",
    "log_session_audit_trace",
    "prepare_session_context_for_ask",
]
