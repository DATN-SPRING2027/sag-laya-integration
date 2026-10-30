# AI Chat Mind Map V1 — SAG API & Domain Synthesis Service

## Scope and provenance

Thang approved `_docs-and-media/specs/ai-chat-mind-map.md` and
`_docs-and-media/specs/ai-chat-mind-map.openapi.json` on 2026-10-30
(`"oke checkout nhánh mới từ main và code đi"`).
Branch `feat/Thang-ai-chat-mind-map-be-api` in `sag-laya-integration` is created
fresh from `origin/main` (`aa85865`). V1 introduces **no database tables or
migrations**: maps are synthesized on demand over authorized search-ready
Project documents and cached only in scoped client session memory.

## Current contracts and security boundaries

- Project-scoped service endpoints under `/api/v1/projects/{project_id}/...`
  enforce Continuum BFF authentication (`require_bff_service_token` +
  `require_project_bff_principal`). Direct user Bearer JWTs without
  `X-SAG-BFF-Service-Token` must be rejected (`403`) so archived-project and
  live Project read checks in `DATN_BE` cannot be bypassed.
- Evidence visibility in `SAG` requires:
  - `SourceProjectMapping.state == "CONFIRMED"`,
    `SourceProjectMapping.organization_id == principal.organization_id`, and
    `SourceProjectMapping.project_id == project_id`;
  - `Document.tenant_id == principal.tenant_id`,
    `Document.project_id == project_id`, `Document.is_active == True`,
    non-deleting/non-failed `Document.status`, and exclusion of documents with
    active `DELETE_DOCUMENT` / `REPROCESS_DOCUMENT` control jobs;
  - `DocumentVersion` usable in scope (`status != "REVOKED"`,
    `search_status in ("READY", "SEARCH_READY")`,
    `knowledge_status != "REVOKED"`, unrevoked `metadata_json`, temporal
    validity `valid_from <= now < valid_to`, and
    `metadata_json["security_partition_id"]` inside
    `principal.allowed_partition_ids` with no out-of-partition sibling
    versions);
  - `SearchUnit.security_partition_id` inside `principal.allowed_partition_ids`
    joined to bounded `CanonicalBlock` spans.
- `LLMClient.complete()` (`sag_api/generation/llm.py:277`) currently accepts
  only `messages: list[Message]`. Optional `response_format` passthrough must
  preserve full backward compatibility for all existing callers
  (`system.py`, `agent_domain.py`, `llm_judge.py`, `handover_assist_service.py`,
  `retrieval_service.py`) and gracefully downgrade `json_schema` ->
  `json_object` -> prompt-only JSON when a provider rejects structured output
  parameters.

## Quality & honesty invariants

1. **Citation membership != semantic faithfulness**: Filtering `citationNumbers`
   to `[1..N]` proves only that each cited chunk belongs to the authorized
   evidence catalog loaded for this request; verbatim `snippet`, `heading`,
   `versionNo`, and `pageFrom`/`pageTo` are returned so users can inspect
   primary text directly.
2. **Explicit coverage accounting**: Combine `section_path` outlines with
   round-robin (overview) or lexical/heading-ranked (`topic`) `SearchUnit`
   excerpts, returning `accessibleReadyDocumentCount`, `usedDocumentCount`,
   `omittedDocumentCount`, `pendingDocumentCount`, `truncatedDocumentCount`, and
   `evidenceUnitCount`.
3. **Strict error vs. fallback separation**:
   - Auth/scope failures -> `403`, invalid input/unknown body keys -> `400`,
     database/query failures -> `503` (never fallback).
   - Zero search-ready documents -> `200` with `status="empty"`,
     `generationMode="none"`, `tree=null` without calling the LLM.
   - `generationMode="outline_fallback"` (`LLM_UNAVAILABLE` or
     `LLM_INVALID_OUTPUT`) triggers **only** after authorized ready sources
     (`> 0`) were loaded cleanly from the database.

## Planned files

- `SAG/apps/api/sag_api/schemas/mind_map.py`: strict Pydantic v2 request and
  response schemas (`extra="forbid"`, `topic` `1..200` trimmed chars, max 4
  levels, max 8 children/node, max 60 nodes).
- `SAG/apps/api/sag_api/generation/llm.py`: optional `response_format` keyword
  parameter on `_create_completion` and `complete` with safe provider fallback.
- `SAG/apps/api/sag_api/services/mind_map_service.py`: ACL-scoped document and
  `SearchUnit` loader, SHA-256 `scopeFingerprint`, coverage accounting, LLM JSON
  synthesis + BFS tree clamping + citation reference pruning, and deterministic
  `outline_fallback` builder from authorized document names and `section_path`
  headings.
- `SAG/apps/api/sag_api/api/v1/mind_map.py` &
  `SAG/apps/api/sag_api/api/v1/__init__.py`: `POST /api/v1/projects/{project_id}/mind-map`
  with unconditional BFF service auth and `Cache-Control: no-store`.
- `SAG/apps/api/tests/test_mind_map_api.py`: RED-first security and behavioral
  ASGI tests.

## Results

- **Actual changed/created files (`sag-laya-integration` branch `feat/Thang-ai-chat-mind-map-be-api`)**:
  - `SAG/apps/api/sag_api/generation/llm.py`: added optional keyword-only `response_format: dict[str, Any] | None = None` on `LLMClient._create_completion` and `LLMClient.complete` (100% backward compatible for all existing callers) with automatic `json_schema` -> `json_object` -> plain prompt-JSON fallback on provider `400`/`422` errors.
  - `SAG/apps/api/sag_api/schemas/mind_map.py`: strict Pydantic v2 models (`extra="forbid"`) for `MindMapGenerateIn`, `MindMapCoverageOut`, `MindMapCitationOut`, `MindMapNodeOut`, and `MindMapOut`.
  - `SAG/apps/api/sag_api/services/mind_map_service.py`: implemented ACL-scoped active Project document + `DocumentVersion` + `SourceProjectMapping.CONFIRMED` + `SearchUnit`/`CanonicalBlock` loader, SHA-256 `scopeFingerprint`, Continuum 24-hex `logical_source_id` extraction, round-robin overview vs `topic`-ranked evidence selection with exact `coverage` accounting, BFS tree normalizer (`<= 4` levels, `<= 8` children/node, `<= 60` total nodes), citation membership pruning + compact `1..M` citation renumbering, and deterministic `_build_outline_fallback_tree` (`LLM_UNAVAILABLE` / `LLM_INVALID_OUTPUT`).
  - `SAG/apps/api/sag_api/api/v1/mind_map.py` & `SAG/apps/api/sag_api/api/v1/__init__.py`: wired `POST /api/v1/projects/{project_id}/mind-map` with unconditional `require_bff_service_token` + `require_project_bff_principal`, strict body/query validation (`400`), and `Cache-Control: no-store`.
  - `SAG/apps/api/tests/test_mind_map_api.py`: 8 RED-first async ASGI security and behavioral tests covering BFF boundary enforcement, cross-tenant/partition/revoked-document exclusion, empty state without LLM invocation, coverage + pruning + clamping, `outline_fallback` on LLM timeout/malformed output, `503` on database failure (never falling back to outline), and `LLMClient.complete(response_format=...)` downgrade safety.
- **Verification**:
  - `ruff check sag_api/api/v1/__init__.py sag_api/api/v1/mind_map.py sag_api/generation/llm.py sag_api/schemas/mind_map.py sag_api/services/mind_map_service.py tests/test_mind_map_api.py` -> `All checks passed!`.
  - `python -X utf8 -m pytest tests/test_mind_map_api.py -q --tb=short` (Python 3.12.13) -> `8 passed in 10.94s` (verified clean RED failure `8 failed` prior to implementation, then GREEN).
  - `python -X utf8 -m pytest tests -m "not ai_smoke" -q --tb=short` (Python 3.12.13) -> `236 passed, 1 deselected in 53.64s`.
- **Database / Configuration / Security / Rollback Impact**:
  - **Database**: Zero schema, table, column, index, or migration changes in V1 (on-demand synthesis over existing `documents`, `document_versions`, `source_project_mappings`, `search_units`, and `canonical_blocks`).
  - **Configuration & Security**: Reuses existing `SAG_BFF_SERVICE_TOKEN` and `LLMClient` provider settings; no new `.env` secrets required. Enforces unconditional BFF service authentication and live partition/revocation checks.
  - **Rollback**: Stateless API addition — reverting the `sag-laya-integration` PR cleanly removes `POST /api/v1/projects/{project_id}/mind-map` with no data migration rollback needed.
- **Remaining Gaps**:
  - Paired BFF gateway (`DATN_BE` branch `feat/Thang-ai-chat-mind-map-be-api`) and interactive React + SVG Studio UI (`DATN_FE` branch `feat/Thang-ai-chat-mind-map-fe-ui`) are delivered in separate repositories/PRs per workspace governance. Semantic faithfulness of LLM node summaries beyond authorized citation membership requires ongoing human evaluation on real project corpora.
