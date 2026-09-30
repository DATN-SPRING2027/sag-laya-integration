# SAG ACL Evidence-Path Inventory

> Status: implementation inventory, reviewed against workspace `origin/main` at
> `acf0bc3` on 2026-09-30. This records the pre-ACL state; it is not proof that
> an entry point is currently authorized by Continuum.
>
> Related drafts: [Principal Assertion Contract](principal-assertion-contract.md),
> [Project-to-Source Mapping Contract](project-to-source-mapping-contract.md).

## Baseline trust boundary at `acf0bc3`

At this baseline, the SAG API authenticates with a local SAG JWT or a connector token.
The Dify adapter has a static API key, and MCP is mounted with a preselected
Source list. None of these current mechanisms carries a Continuum-resolved
`allowed_project_ids` scope. `Source` has no organization/project mapping, and
`search_source_candidates()` treats absent `source_ids` as bounded global
candidate selection. Authentication and logical-delete/reprocess filtering do
not establish Project authorization.

The open P4 pull request #9 is at `c951267`; it changes fusion and removes
global event/graph projection. The stacked ACL branch starts at that commit and
adds the runtime verifier, Project→Source mapping and evidence path guards.
The table below records the earlier `acf0bc3` exposure and required remediation;
the branch disposition table records the combined implementation state.

## Evidence-producing paths

| Entry point | Evidence-producing call chain | Protection at `acf0bc3` | Required remediation |
|---|---|---|---|
| Source-scoped search `POST /sources/{source_id}/search` | `api/v1/search.py::search` → `get_source` → `retrieve_relevant_sections` + `recall_event_scores` → `_event_graph_fields` → answer synthesis | Local SAG user JWT; source identifier is treated as scope, with no BE assertion or Project mapping check | Verify principal; resolve that Source through a confirmed same-organization Project mapping before dense, lexical, event, or graph work. P3 graph/event calls receive only the effective source set. |
| Global search `POST /search` | `global_router` → `_prepare_global_search` → `search_source_candidates` → `retrieve_relevant_sections` and, on current `main`, event recall/graph projection → answer synthesis | Local JWT or connector token; missing client `source_ids` selects bounded global Sources. PR #9 removes global graph projection but is not merged. | Require a verified BE principal; derive authorized Sources from confirmed mappings; intersect request IDs; no retrieval for empty scope. Preserve the PR #9 no-KG/Tree global P4 behavior when integrating. |
| Streaming search `POST /search/stream` | `global_search_stream` → `_prepare_global_search` → retrieval and SSE result/summary events | Local SAG user JWT; shares global candidate selection semantics | Reuse the same verified principal and scope resolver as `/search`; resolve before retrieval and before sending any evidence event. |
| Strategy comparison `POST /search/eval-compare` | `eval_compare` → `search_source_candidates` → `_run_one_strategy` → `retrieve_relevant_sections` → optional pairwise judge | Local SAG user JWT; client IDs or bounded global selection; production reachability is not separated from evaluation use | Treat as production-reachable unless an explicit deployment boundary proves otherwise. Apply the same scope before each strategy; otherwise disable/guard the route. |
| Dify `POST /dify/retrieval` | Dify API-key dependency → `get_source(knowledge_id)` → `retrieve_relevant_sections` | Static Dify key authenticates the integration, but is not a user or Project grant | Require a Continuum-issued scoped service principal/grant; verify its Project scope and Source mapping before retrieval. If no scoped grant is configured, reject without evidence. |
| Source chunk read `GET /sources/{source_id}/chunks/{chunk_id}` | `api/v1/sources.py::get_chunk` → `get_source` → `EngineManager.get_chunk` | Local JWT or connector; source ID lookup is not ACL | Authorize Source before calling `get_chunk`; non-visible and nonexistent Source must not be distinguishable by evidence response. |
| Document list/read routes | `api/v1/documents.py` → `get_public_document` / local file / `get_document_markdown` | Local JWT or connector; caller-provided Source ID selects the parent but no Project authorization is checked | Authorize parent Source before document metadata, file, preview, parsed content, or read output. Documents and versions inherit Source scope for P1. |
| Knowledge routes: outline, grep, document read, entity context | `api/v1/knowledge.py` → `get_source`/`get_public_document` → `list_chunk_headings`, `grep_chunks`, local file, or `entity_context` | Local JWT and Source ID; no Project check | Resolve Source authorization first, then run the scoped storage call. Graph/event contexts may not traverse outside the authorized Source set. |
| Insights: entities and graph | `api/v1/insights.py` → `get_source` → entity/graph engine calls | Local JWT; Source identifier is the only scope | Apply Source mapping authorization before engine access; if graph implementation cannot guarantee same-Source traversal, deny/disable that operation until it can. |
| Universe manifest, expand, timeline, node detail, explorations | `api/v1/universe.py` → `universe_service`/`engine_manager` using request or saved `source_ids` | Local JWT; request/persisted source IDs do not prove authorization | Resolve effective Source IDs before reads, graph expansion, timeline or evidence detail. Saved explorations must be re-authorized on every read; do not trust a historical saved scope. |
| Agent ask and OpenAI-compatible chat | `api/v1/agents.py` / `api/v1/openai.py` → `agent_service` → `agent_domain.resolve_sources` → agent runtime/tools | Local JWT; explicit body IDs, bindings, or default-agent global selection determine Sources | Verify and propagate one principal/effective scope for the full run. Intersect body IDs and bindings; do not allow tools or MCP integrations to widen it. Re-check saved thread evidence before returning it. |
| Built-in `SearchContextTool` | `tools/builtin.py::SearchContextTool.invoke` → retrieval + `recall_event_scores` + graph projections | Receives the Agent's Source list; that list is currently derived from untrusted/request or local binding selection | Require an authorization-scoped Source collection in the tool context. Authorize before retrieval and before event/graph recall. Global P4 must not invoke graph/tree. |
| MCP search/read/grep/chunk tools (HTTP and stdio) | `mcp/server.py::build_source_mcp` → `MCPScope.sources` → retrieval, document queries, `grep_chunks`, `get_chunk` | Tool calls are limited to the constructed Source tuple, but the session has no verified Continuum principal; stdio without `SAG_MCP_SOURCE_ID` loads all Sources | Bind MCP session to a verified user assertion or an explicitly scoped service grant. Remove the all-Source stdio default. Re-authorize each read/search against current mappings. |
| Source creation and OCTX import/transfer | `source_service.create_source`; `octx_transfer_service` has separate Source creation paths | Local SAG JWT/connector or transfer authorization; created Source has no Project mapping | Every new/imported Source starts non-searchable until a trusted Project mapping is confirmed. OCTX package scope is untrusted; require explicit destination Project mapping. Do not change ingestion/index internals to imply authorization. |
| Internal retrieval callers | `retrieval_service.retrieve_relevant_sections`, `recall_event_scores`, `EngineManager.search_many`/`grep_chunks`; callers include search, Dify, MCP, agents | Service methods accept Source lists but cannot tell whether they were authorized | Tighten the internal boundary so production evidence calls require an `EffectiveSourceScope`/equivalent, and ensure dense, lexical, and P3 event candidate generation receives that scope before its own top-k. Tests must instrument candidate inputs, not only final responses. |

## Current branch disposition

| Path | Current disposition | Remaining acceptance evidence |
|---|---|---|
| Global `/search`, `/search/stream`, `/search/eval-compare` | Signed principal dependency; confirmed same-Organization Project mapping intersection runs before dense/lexical retrieval. | Real BE signer/JWKS and cross-tenant tests. |
| Global P4 graph/KG/Tree | Event recall and graph projection are not called; graph response fields remain empty. | Keep this restriction until a scoped graph contract exists. |
| Source-scoped P3 search | Source mapping is resolved before retrieval and graph/event recall. | Real-principal integration tests. |
| Direct Source/document/knowledge/insight reads | Path Source dependency denies unmapped/out-of-scope IDs before handler I/O; job and saved agent history reads also check mappings. | Expand negative coverage across every route and prove on real storage. |
| Dify | Static API key plus signed principal; only the mapped `knowledge_id` Source is passed to retrieval. | Continuum service principal and key lifecycle are not available yet. |
| MCP HTTP | Local SAG token plus signed principal; Source set is resolved per request. Non-HTTP WebSocket is rejected. | BE/BFF propagation test and scoped service grant. |
| MCP stdio | Disabled. | Keep disabled until every invocation can carry and verify a fresh principal. |
| OCTX | Signed principal required; Source transfer/export paths check current mapping; import decisions targeting an existing Source authorize that destination. New imported Sources stay unmapped. | Full transfer leakage regression and approved mappings for imported Sources. |
| Universe | Source-specific reads and saved explorations are re-authorized. Global manifest/rebuild return dependency-unavailable. | Implement per-Project graph builds before enabling global operations. |
| Agent asks, OpenAI-compatible chat and history | Principal scope is propagated to Source selection. Assistant history is returned only when all cited SAG Sources and Sources recorded in local knowledge-tool trace metadata remain authorized; ambiguous knowledge-tool provenance is hidden. | Review every agent tool and remote MCP service grant; run cross-Project integration tests. |
| Jobs | Principal required; source/document jobs resolve their Source mapping before returning job metadata. | Validate owner behavior for source-less internal jobs. |

The branch contains runtime ACL implementation and local contract/runtime
fixtures. Production Continuum issuer/JWKS, signed assertion propagation,
approved Project mapping data, mapping-writer authority, production migration
approval and real revocation/leakage evidence are not present. Therefore
production ACL rollout acceptance remains open.

## Operational rollout gates

Track these production acceptance gates in this security inventory, not as
implementation subtasks in `tasks/todo.md`. They govern rollout of the runtime
ACL and do not block independent Workflow Phase 2 or Phase 3 engineering.

- Obtain the approved Continuum issuer/audience/JWKS configuration and verify
  trusted assertion propagation, key rotation and failure behavior.
- Obtain DB/data-owner approval for the DDL and migration window; confirm
  Project-to-Source mappings through the owner-approved mapping/backfill and
  revoke workflow. No production migration or backfill is implied by this PR.
- Run staging leakage and revocation tests with real principals and data across
  Project/Organization boundaries and every reachable evidence path.
- Keep MCP stdio and global Universe manifest/rebuild disabled until their
  paths enforce Project-scoped graph authorization.

In earlier PR notes, **P1** means the Priority 1 runtime ACL rollout package;
it does not mean **Workflow Phase 1 — Upload & Versioned Source**. Phase 1
completion remains tracked against its own checklist and evidence.

## Required request flow

```text
entry point
  -> verify Continuum-signed principal (or deny)
  -> resolve confirmed Project-to-Source mappings for principal.org_id
  -> intersect requested IDs; absent request IDs means all authorized Sources
  -> if empty, return no evidence without calling retrievers
  -> dense + lexical retrieval (same effective Source IDs before candidate/top-k)
  -> P3 graph/event retrieval only with authorized Source IDs
  -> fusion / top-k / response
```

Source/document read endpoints use the same principal and mapping resolver before
opening a file or calling an engine. An authorization error, mapping error, or
empty scope must never cause a retry without the scope filter.

## Acceptance coverage matrix

The implementation is not complete until tests cover each row above that is
reachable in the deployed configuration. The tests must include an unauthorized
high-score vector/lexical item that would win an unfiltered top-k, a P3 event
outside the scope, an unmapped legacy Source, and a cross-organization Project
ID. MCP stdio, Dify, agent tools, saved explorations, and `eval-compare` need an
explicit `protected`, `scoped service grant`, or `disabled/internal-only`
deployment decision; “authenticated locally” is not an ACL disposition.
