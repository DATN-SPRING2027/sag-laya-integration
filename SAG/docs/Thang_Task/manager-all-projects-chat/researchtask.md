# Research Task: Manager All-Projects AI Chat Scope (`manager-all-projects-chat`)

## 1. Objective
Enable Organization Managers (`ADMIN` technical role in Continuum IAM) to query across **all active projects** in their organization within a single AI Chat workspace (`project_id = "all"`), while keeping Project Leaders (`TEAM_LEADER`) and Members (`MEMBER`) strictly restricted to single-project AI Chat scopes.

## 2. Current Architecture & Findings in `SAG`
1. **Multi-Project Retrieval Already Supported**:
   - `VerifiedPrincipal` (`sag_api/core/principal_assertion.py`) already models `allowed_project_ids: frozenset[str]` and `allowed_partition_ids: frozenset[str]`.
   - `source_service.py` (`search_source_candidates`, `_authorized_source_statement`), `search_unit_retrieval_service.py`, `agent_domain.py` (`resolve_sources`, `filter_messages_for_scope`), and `session_context/persistence.py` (`resolve_persistence_scope`) already support `len(principal.allowed_project_ids) > 1`.
2. **Gaps to Address in `SAG`**:
   - `sag_api/api/v1/auth.py` (`continuum_login`):
     - Currently accepts a single `X-Continuum-Project-Id` header and encodes only `"project_id"` and `"security_partition"` in the Continuum session JWT.
     - Needs to accept optional `allowed_project_ids: list[str] | None` in `ContinuumLoginRequest` (or `X-Continuum-Allowed-Project-Ids` header) so when `project_id == "all"`, the session JWT carries `allowed_project_ids` and `allowed_partition_ids` for all active projects in the Manager's organization, while preserving `"project_id": "all"` for conversation scope ownership.
   - `sag_api/core/principal_assertion.py` (`VerifiedPrincipal`, `require_project_read_principal`, `require_project_bff_principal`):
     - Add `conversation_scope: str | None = None` to `VerifiedPrincipal`.
     - In `require_project_read_principal`, decode `allowed_project_ids` and `allowed_partition_ids` lists when present in the Continuum session JWT, and set `conversation_scope="all"` when `project_id == "all"`.
     - In `require_project_bff_principal`, allow `X-Continuum-Project-Id: all` when `principal.allowed_project_ids` is non-empty, and set `conversation_scope="all"`.
   - `sag_api/core/conversation_ownership.py` (`conversation_owner_key`):
     - When `principal.conversation_scope == "all"`, use `["all"]` as the project scope segment in the owner key hash so the Manager's "All Projects" threads are isolated from single-project threads and remain stable even as projects are created or archived in the organization.
   - `sag_api/services/answer_actions_service.py`:
     - Update `_resolve_project_scope(principal)` so feedback (`get_own_feedback`, `upsert_own_feedback`, `delete_own_feedback`), thread branching (`branch_thread_from_message`), and answer sharing (`create_or_get_share`, `read_project_share`, `revoke_project_share`) work in both single-project and multi-project (`conversation_scope == "all"`) scopes while still verifying that all cited sources belong to `principal.allowed_project_ids`.
   - `sag_api/api/v1/mind_map.py` & `sag_api/services/mind_map_service.py`:
     - Allow `project_id == "all"` when `principal.allowed_project_ids` is non-empty, querying ready documents across `principal.allowed_project_ids`.

## 3. Cross-Repository Implementation Summary
1. **`sag-laya-integration` (`feat/Thang-manager-all-projects-chat-be-api`)**:
   - `sag_api/api/v1/auth.py`: Added `_extract_continuum_login_options` to parse and validate `allowed_project_ids` (1–256 non-`"all"` safe IDs) when `X-Continuum-Project-Id: all`, embedding `allowed_project_ids` and `allowed_partition_ids` into the Continuum SAG session JWT.
   - `sag_api/core/principal_assertion.py`: Added `conversation_scope: str | None = None` to `VerifiedPrincipal` and populated `allowed_project_ids`, `allowed_partition_ids`, and `conversation_scope="all"` in both `require_project_read_principal` and `require_project_bff_principal`.
   - `sag_api/core/conversation_ownership.py`: Updated `conversation_owner_key` so `principal.conversation_scope == "all"` uses `["all"]`, keeping Manager All-Projects threads isolated and stable across project additions/archivals.
   - `sag_api/services/answer_actions_service.py`, `sag_api/api/v1/mind_map.py`, and `sag_api/services/mind_map_service.py`: Enabled feedback, thread branching, answer sharing, and mind-map synthesis for `project_id == "all"`.
   - `tests/test_all_projects_chat_scope.py`: Added unit and integration tests covering `project_id="all"` session issuance, principal assertion, owner key stability, answer actions, and mind map scope.
2. **`DATN_BE` (`feat/Thang-manager-all-projects-chat-be-api`)**:
   - `src/integrations/ai-engine/ai-chat.service.ts`: Added `AI_CHAT_ALL_PROJECTS_ID = 'all'` and `assertOrganizationManagerForAllProjects` enforcing that ONLY Organization Managers (`roles.includes('ADMIN')`) can access `projectId = "all"`, throwing `403 ForbiddenException` (`AI_CHAT_ALL_PROJECTS_FORBIDDEN`) for Leader (`TEAM_LEADER`) and Member (`MEMBER`). Resolved all active projects in the organization (`status: 'ACTIVE'`, up to 256) and passed `allowed_project_ids` / `X-Continuum-Allowed-Project-Ids` to SAG.
   - `src/integrations/ai-engine/ai-chat.controller.ts` & `src/services/capture/application/work-notes.service.ts`: Added `listAiChatAllProjectsSources` to aggregate document sources across all active projects for Managers and enrich each source with `projectId`, `projectCode`, and `projectName`.
3. **`DATN_FE` (`feat/Thang-manager-all-projects-chat-fe-ui`)**:
   - `src/components/ai-chat/AiChatWorkspace.tsx` & `AiChatProjectMenu.tsx`: Exposed the `"All projects"` / `"Tất cả dự án"` (`id: "all"`, code `ALL`) option in the project selector dropdown exclusively when `currentUserQuery.data?.roles?.includes("ADMIN")`. Non-manager roles (`TEAM_LEADER`, `MEMBER`) do not see the option and fall back to their first active project even if `?projectId=all` is passed in the URL.
   - `src/components/ai-chat/AiChatSourcesPanel.tsx`: Rendered a project badge (`projectCode`) on each document source item when viewing All-Projects sources.
