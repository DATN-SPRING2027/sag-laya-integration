# AI Chat Mind Map V1.1 — Project Snapshot Persistence API & Service

## Scope and provenance

Thang confirmed server-side Project Mind Map persistence on 2026-10-31 (`"Lưu trên Server theo Project (mọi thành viên trong Project mở lên đều thấy ngay 0s loading; chỉ gen lại khi bấm Tạo lại)"`).
Branch `feat/Thang-ai-chat-mind-map-persistence-be-api` builds on `feat/Thang-ai-chat-mind-map-persistence-db` (`9ac9cf4`). BFF (`DATN_BE`) and Frontend (`DATN_FE`) are delivered on separate branches/PRs per `AGENTS.md`.

## Current contracts and security boundaries

- `GET /api/v1/projects/{project_id}/mind-map` (optional `?topic=...`) and `POST /api/v1/projects/{project_id}/mind-map` (`{"topic"?: string, "refresh"?: boolean}`) enforce `require_bff_service_token` + `require_project_bff_principal` unconditionally (rejecting direct user JWTs without BFF token with `403`).
- `GET` never invokes the LLM:
  1. Loads live authorized non-revoked `READY` Project documents via `_load_project_scope_snapshot`.
  2. Returns `status="empty"` when `accessibleReadyDocumentCount == 0`.
  3. Looks up `ProjectMindMapSnapshot` scoped by `(tenant_id, project_id, partition_scope_hash, topic_key)`.
  4. Verifies `saved_used_document_version_ids.issubset(live_ready_document_version_ids)`. If any contributing document version was revoked, deleted, superseded, or removed from caller's partition, the stale snapshot is evicted and `status="uninitialized"` is returned so revoked text never leaks.
  5. When valid, returns `status="ready"`, `cached=True`, and `hasSourceUpdates=(snapshot.scope_fingerprint != live_scope_fingerprint)` so subsequent opens by any authorized Project member resolve in ~10ms without re-running synthesis even if additional documents were uploaded later.
  6. When no snapshot exists yet for non-empty `ready_docs`, returns `status="uninitialized"` (`tree=None`) so the first visitor triggers `POST` once and persists the snapshot for the Project.
- `POST` synthesizes (or returns cached snapshot when `refresh=False` is passed explicitly) and atomically upserts `project_mind_maps` via dialect-aware `ON CONFLICT DO UPDATE`. Database errors during scope or snapshot lookup raise `503 ServiceUnavailableError` (never silently degrading to outline fallback).

## Planned files and test matrix

- `SAG/apps/api/sag_api/schemas/mind_map.py`: add strict `refresh: bool = True` on `MindMapGenerateIn`; enforce `len(trimmed) <= 200 and len(trimmed.casefold()) <= 200` in `normalize_mind_map_topic` so Unicode casefold expansions (`"ß" * 101` -> `202` chars) reject with `400` before LLM synthesis or `VARCHAR(200)` `topic_key` persistence; extend `MindMapOut` with `status: Literal["ready", "empty", "uninitialized"]`, `cached: bool = False`, and `hasSourceUpdates: bool = False`.
- `SAG/apps/api/sag_api/services/mind_map_service.py`: implement `get_saved_project_mind_map`, atomic snapshot upsert/eviction in `generate_project_mind_map`, and live `used_document_version_ids` subset verification.
- `SAG/apps/api/sag_api/api/v1/mind_map.py`: add `GET /api/v1/projects/{project_id}/mind-map` with strict query parameter allowlist (`topic` only) and BFF principal checks.
- `SAG/apps/api/tests/test_mind_map_api.py`: RED-first ASGI tests for `GET` uninitialized -> `POST` persist -> cross-user `GET` instant hit (`cached=True`, 0 extra LLM calls), `hasSourceUpdates=True` on new document upload without reload, revocation/deletion invalidation safety, `POST` `refresh=False` vs `refresh=True`, and strict `GET` query / BFF auth guards.

## Impact and rollback

- **Database/Config/Security**: Uses additive `project_mind_maps` table from `0011_project_mind_map_snapshots.sql`; no new environment variables or secrets; enforces live document revocation & partition subset checks on every snapshot read.
- **Rollback**: Revert API branch before running any manual schema rollback.

## Results

- Implemented `GET /api/v1/projects/{project_id}/mind-map` (`?topic=...`) and updated `POST /api/v1/projects/{project_id}/mind-map` (`refresh` support + atomic `ON CONFLICT DO UPDATE` persistence + stale snapshot eviction) across `sag_api/schemas/mind_map.py`, `sag_api/services/mind_map_service.py`, `sag_api/api/v1/mind_map.py`, and `tests/test_mind_map_api.py`.
- Verification:
  - `pytest tests/test_mind_map_api.py tests/test_mind_map_schema.py -q --tb=short` -> `26 passed`.
  - `ruff check sag_api/schemas/mind_map.py sag_api/services/mind_map_service.py sag_api/api/v1/mind_map.py tests/test_mind_map_api.py` -> `All checks passed!`.
