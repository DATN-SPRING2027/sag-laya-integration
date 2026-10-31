# Research Task — AI Chat Branch Lineage Domain, Tombstone GC & Bi-Temporal `SAG-ECCE` (`feat/Thang-ai-chat-branch-lineage-be-api`)

## 1. Context & Goal

Deliverable 2 (`feat/Thang-ai-chat-branch-lineage-be-api`, stacked on `feat/Thang-ai-chat-branch-lineage-db`) implements the complete backend domain, API, pagination, security/ACL enforcement, Tombstone Reference-Counted GC, and Bi-Temporal `SAG-ECCE` Session Memory integration for **"Branch in new chat" (Option 3)**.

## 2. Inspected Files

- `SAG/apps/api/sag_api/schemas/agent.py`
- `SAG/apps/api/sag_api/api/v1/agents.py`
- `SAG/apps/api/sag_api/services/agent_domain.py`
- `SAG/apps/api/sag_api/services/answer_actions_service.py`
- `SAG/apps/api/sag_api/services/session_context/substrate.py`
- `SAG/apps/api/sag_api/services/session_context/persistence.py`
- `SAG/apps/api/sag_api/services/session_context/runtime.py`
- `SAG/apps/api/tests/test_answer_actions_api.py`
- `SAG/apps/api/tests/test_session_context_persistence.py`

## 3. Core Algorithms & Invariants

### 3.1 Lineage Resolution & Direct Origin Pointer
- `resolve_thread_lineage(session, thread)` walks `parent_thread_id` up to `MAX_LINEAGE_DEPTH = 32` with cycle detection (`visited`) and verifies `(agent_id, owner_key)` match at every hop (even if an ancestor has `deleted_by_owner_at IS NOT NULL`).
- Each ancestor hop yields `LineageSegment(thread_id, cutoff_created_at, cutoff_message_id, cutoff_reply_to_message_id, is_inherited=True)`.
- Within an ancestor segment:
  - Messages must satisfy `(created_at, id) <= (cutoff_created_at, cutoff_message_id)`.
  - If `cutoff_reply_to_message_id` is present (assistant turn with regeneration versions), sibling assistant drafts sharing `reply_to_message_id == cutoff_reply_to_message_id` and `id != cutoff_message_id` are excluded so the branch locks onto the exact `< k / N >` version selected by the user.
- When branching from message `M` inside thread `T` (where `M` may be owned by `T` or inherited from an ancestor `A`), the new child thread sets `parent_thread_id = M.thread_id` and `branch_point_message_id = M.id` (**Direct Origin Pointer**).

### 3.2 Tombstone Reference-Counted Garbage Collection
- `list_threads` and `get_thread` enforce `Thread.deleted_by_owner_at.is_(None)`.
- `delete_thread`:
  - If any child `Thread` has `parent_thread_id == thread.id`, marks `thread.deleted_by_owner_at = datetime.now(UTC)` and evicts `SESSION_INDEX_CACHE` for `thread.id`, preserving underlying messages/units for descendants.
  - Otherwise, physically deletes `thread` (plus its `session_*` rows and Qdrant points), then walks upward along `parent_thread_id` physically deleting any ancestor that has `deleted_by_owner_at IS NOT NULL` and zero remaining children.
- `delete_message`: blocks deleting a message (`409 ConflictError`) if any child thread of `thread.id` has `branch_point_message` at or after `(message.created_at, message.id)`.
- `delete_agent`: deletes child threads in leaf-to-root order before deleting the `Agent` so `ON DELETE RESTRICT` on `threads.parent_thread_id` / `branch_point_message_id` never blocks agent deletion.

### 3.3 Multi-Segment HMAC Cursor (`v: 2`)
- `list_messages_page` walks segments in reverse chronological order (`[leaf, parent, ..., root]`), encoding `v: 2` HMAC cursors bound to `scope = sha256(leaf_thread_id)[:24]` with `(seg, created_at, id)` while continuing to accept `v: 1` cursors (`seg = 0`).

### 3.4 Bi-Temporal As-Of `SAG-ECCE` Session Memory
- Root threads (`parent_thread_id IS NULL`) keep the existing fast path unchanged.
- Branched threads:
  1. Load ancestor segments in chronological order, run `filter_messages_for_scope` (enforcing live Continuum Project & Document ACL), deduplicate prior regenerations, and hydrate persisted `SessionUnit (TURN)` + vectors + `SessionDecisionLedger` up to each ancestor's cutoff (`0` HTTP re-embeds; falls back to read-only in-memory indexing if an ancestor has not been persisted yet).
  2. **As-Of Restoration:** In `_reconstruct_prefix_into_index(..., as_of_cutoff=True)`, if an ancestor unit/decision has `epistemic_status == 'SUPERSEDED'` in DB because the parent thread superseded it *after* the branch point (`superseded_by_id` not in the cutoff set), restore its pre-supersession intrinsic epistemic state (`classify_epistemic_state`) and `valid_to_ts = None`.
  3. **Sealed Episode/Window Boundary (`seal_inherited_boundary`):** Closes the last ancestor episode/window at `inherited_turn_count` so all own `TURN`, `WINDOW`, `EPISODE`, `SessionDecisionLedger`, `SessionGraphEdge`, and Qdrant points persisted under `scope.thread_id = child.id` reference ONLY `Message` rows belonging to `child.id`, 100% satisfying all PostgreSQL/SQLite triggers without mutating ancestor rows. Cross-lineage decision supersession is recorded via `"inherited_turn:<id>"` entries in `supersedes_ids_json` and re-applied deterministically on cold-start hydration.

### 3.5 Answer Actions Across Lineage
- `feedback` (`GET`/`PUT`/`DELETE`) and `shares` (`POST`) accept any authorized assistant message in `thread`'s valid lineage.
- `_verify_evidence_document_provenance` and `resolve_parent_question` traverse valid lineage segments (respecting segment cutoffs) so answers citing inherited `session_citations` pass provenance verification.
- `prepare_regenerate` rejects inherited messages (`400 BadRequestError`) to preserve ancestor immutability, while regenerating an own message on a branched thread includes the inherited lineage prefix in its context!


## 4. Implementation & Verification Results

### Changed Files (`feat/Thang-ai-chat-branch-lineage-be-api`)
- `SAG/apps/api/sag_api/schemas/agent.py` (`ThreadBranchIn` with `extra="forbid"`, `ThreadOut` lineage metadata, `MessageOut.is_inherited`)
- `SAG/apps/api/sag_api/services/agent_domain.py` (`_next_monotonic_utc_now()`, `LineageSegment`, `resolve_thread_lineage_segments`, `load_segment_messages_chronological`, `_encode_message_cursor_v2` / `_decode_message_cursor_v2` with `v: 1` compatibility, `serialize_threads_out` / `serialize_thread_out`, Tombstone + upward Reference-Counted GC in `delete_thread`, leaf-to-root `delete_agent`, `409 ConflictError` guard in `delete_message`, multi-segment `list_messages_page` & `_history`)
- `SAG/apps/api/sag_api/services/answer_actions_service.py` (`branch_thread_from_message` with Direct Origin Pointer + `MAX_LINEAGE_DEPTH` check, lineage-aware `_verify_evidence_document_provenance` & `_ensure_authorized_assistant_message`, `create_or_get_share` using `message.thread_id`, and `prepare_regenerate` blocking inherited messages with `400` while hydrating ancestor context on own-turn regeneration)
- `SAG/apps/api/sag_api/services/session_context/substrate.py` (`seal_inherited_boundary()`, `inherited_*_count` watermarks, `force_new_episode_next`, `inherited_lineage_signature`)
- `SAG/apps/api/sag_api/services/session_context/persistence.py` (`_hydrate_ancestor_segments_as_of` with `0` HTTP re-embeds, Bi-Temporal As-Of `epistemic_status` restoration in `_reconstruct_prefix_into_index`, `"inherited_turn:<tid>"` cross-branch supersession tracking, own-turn slicing `>= inherited_*_count` across DB & Qdrant sync/repair/backfill)
- `SAG/apps/api/sag_api/services/session_context/runtime.py` (`load_ancestor_segments_visible_records`, live ACL `lineage_sig` invalidation, own-turn persisted watermark comparison)
- `SAG/apps/api/sag_api/api/v1/agents.py` (`POST /{agent_id}/threads/{thread_id}/messages/{message_id}/branch` returning `201 Created`, `is_inherited` message flag, `serialize_threads_out` / `serialize_thread_out`)
- `SAG/apps/api/tests/test_branch_lineage_api.py` (6 end-to-end API, Bi-Temporal `SAG-ECCE`, nested lineage, pagination, Tombstone GC, and live ACL revocation tests)

### Checks Run
- `ruff check` on all modified files -> `All checks passed!` (`0` errors)
- `pytest tests/test_branch_lineage_schema.py tests/test_branch_lineage_api.py tests/test_answer_actions_api.py tests/test_answer_actions_schema.py tests/test_ecce_session_context.py tests/test_session_context_persistence.py tests/test_session_context_schema.py -q` -> **88 passed**
