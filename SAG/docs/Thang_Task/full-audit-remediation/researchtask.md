# SAG Full Audit Remediation (`full-audit-remediation`)

- **Branch:** `feat/Thang-full-audit-remediation-be-api` (base `2e4bc3c`)
- **Authority:** `CONTINUUM_FULL_AUDIT/11_REMEDIATION_EXECUTION_PLAN.md` (Track 1: 27 primary findings + SAG co-owner slices), `01_ALL_FINDINGS.md`, `05_SAG_AI_RAG_AUDIT.md`.

## Verified Current Flow & Gaps
1. **Runtime & Ingestion (`SAG-ING-001..005`, `DEP-001`, `INFRA-004`, `TEST-002`):**
   - `pyproject.toml` declares `requires-python = ">=3.11"` while `knowledge_service.py`, `routing_tree_service.py`, and `tree_query_snapshot_service.py` invoke Python 3.12+ `math.sumprod`, crashing `.venv` (3.11.15) and `.local/sag-runtime/start-sag.ps1`.
   - `parsing/text.py::_PLAIN_TEXT_SUFFIXES` omits `.json` (fix from unmerged `bb4d60d`).
   - `handle_document_upload()` mutates live `Document` fields before `v2` indexes and monotonically increments `Source.chunk_count`.
   - PyMuPDF (`local_worker.py:236-253`) duplicates PDF tables; uploads lack magic-byte validation, UUID snapshot filenames, zip-bomb bounds, and Excel formula fallback.
   - `InProcJobQueue` and `init_db()` lack multi-worker lease scaffolding and Alembic bootstrap.
2. **Security, ACL & Logging (`SAG-SEC-001..006`, `SAG-DB-001`, `OBS-002/003/004`):**
   - `MCP_SERVER` bindings allow arbitrary `stdio_client` RCE and unguarded `streamablehttp_client` SSRF by read-only project JWTs (`agents.py`, `agent_domain.py`, `tools/mcp.py`).
   - `/conflicts/{id}/resolve` and `/reopen` (`conflicts.py`) lack BFF service token & authoritative reviewer role enforcement.
   - `/system/dsh/*`, `/system/dsh-connection`, `/system/preferences`, `/system/metrics`, `/system/capabilities` lack operator auth or expose loopback token discovery.
   - `filter_messages_for_scope` (`agent_domain.py`) and ECCE (`session_context/runtime.py`) filter only at Source granularity, leaking revoked single documents in chat history and session memory.
   - Project document revocation only deletes Qdrant points when `reason == "DOCUMENT_DELETED"` and skips `REBUILD_UNIVERSE_TREE`.
   - `sources.py` `/sources/{id}/sync` calls nonexistent `VerifiedPrincipal.has_project_access` / `allowed_partitions`; `handover_assist_service.py` omits `SourceProjectMapping.state == "CONFIRMED"` and explicit `!= REVOKED` checks; `SAG/apps/web/lib/auth.ts` stores JWT in non-`HttpOnly` cookie.
   - Source ID existence oracles (`agent_domain.py`, `dsh_integration_service.py`), DNS-rebinding TOCTOU in `OpenWebPageTool`, dead wildcard `core/deps.py:get_verified_principal`.
   - `core/logging.py` accepts unsanitized `X-Request-Id` and lacks UTC ISO-8601 / JSON prod formatting.
3. **Storage, Performance, Retrieval & Chat (`SAG-DB-002..003`, `SAG-PERF-001..003`, `SAG-RET-001..004`, `SAG-CHAT-001..003`, `E2E-INT-011`):**
   - Unbatched Qdrant upserts (`qdrant_store.py`, `search_index_service.py`, `session_context/persistence.py`); unbounded RAM orphan scroll; unsigned legacy `embedding_model_signature` bypass; default SQLAlchemy pool limits.
   - 19 synchronous CPU/disk operations in `async def` functions.
   - Default `decision_provider="siliconflow"` (`Kev-4b`) adds +53.1% retrieval p50 / +31.4% tokens for 0% Recall@5 gain; buffered pseudo-streaming causes TTFT == E2E (6–15s vs 2s SLA); unpooled `httpx.AsyncClient` and 4–7 PG cache-key queries per search.
   - `RetrievalPlan.constraints` & `.order` are trace-only dead code; `universe_tree_service.py` uses vacuous quality gates and never calls `incremental_tree_service.py`; sub-queries run BM25-only and `_expand_multi_hop_sections` lacks PPR.
   - `_verify_grounded_entailment()` lacks prompt-injection delimiters and drops valid cited answers on 10s timeout; `web_enabled=True` web-cited answers rejected when local sources exist; `generation/llm.py` leaks raw exceptions and retries up to 9x.
   - `_CONTINUUM_PUBLIC_DOC_ID_RE` only matches `project_document` (missing `work_report` and `work_note`).
4. **Infra, CI & Contracts (`DEP-002/003/005`, `INFRA-005/009/010`, `TEST-001/003/004`):**
   - Single-stage root `SAG/apps/api/Dockerfile` bypasses `uv.lock`; `SAG/compose.yaml` sets `retries: 480` and `SAG_DSH_LOCAL_DISCOVERY=true`; missing `.github/workflows/ci.yml` and `.github/dependabot.yml`; missing OpenAPI snapshot and pytest-asyncio/SQLAlchemy warning cleanup.

## Planned File-by-File Changes
- Runtime & Config: `pyproject.toml`, `core/config.py`, `core/db.py`, `core/logging.py`, `core/deps.py`, `core/principal_assertion.py`, `core/ssrf.py` (shared DNS-pinned SSRF guard), `parsing/text.py`, `parsing/local_worker.py`, `.local/sag-runtime/start-sag.ps1`.
- API & Services: `api/v1/{agents,conflicts,system,sources,documents,attachments,knowledge}.py`, `services/{agent_domain,agent_service,document_service,dsh_integration_service,handover_assist_service,knowledge_service,routing_tree_service,tree_query_snapshot_service,universe_tree_service,search_index_service,search_unit_retrieval_service,retrieval_service,decision_router,canonical_service,source_service,octx_gc_service,mind_map_service}.py`, `services/session_context/{runtime,persistence}.py`, `tools/{mcp,builtin}.py`, `generation/{llm,prompt}.py`, `jobs/{tasks,inproc}.py`, `mcp/server.py`, `sag/qdrant_store.py`.
- Web & Infra: `SAG/apps/web/{lib/auth.ts,app/api/auth/session/route.ts,app/sso/page.tsx,Dockerfile,package.json}`, `SAG/apps/api/Dockerfile`, `SAG/compose.yaml`, `SAG/compose.postgres.yaml`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `SAG/docs/SAG_Knowledge_Routing_RAG_Workflow_v1.1.md`, OpenAPI snapshot, Alembic scaffold.

## Verification Plan
- Full Python 3.12 `pytest` (0 failures, 0 `math.sumprod` crashes) + targeted regression tests for every finding ID.
- Python AST syntax check across all `.py` files + `ruff` check on modified files.
- `SAG/apps/web`: `tsc --noEmit`, `eslint`, `vitest run`.

## Implementation Results (Completed 2026-11-27)
- **Branch:** `feat/Thang-full-audit-remediation-be-api` (no push performed; uncommitted working tree ready for review/commit).
- **Modified / Added Files:** 65 modified files + 13 new files strictly under `sag-laya-integration/**` plus `F:\LEARN KÌ 8\ĐATN\.local\sag-runtime\start-sag.ps1`. Zero forbidden directories (`DATN_BE`, `DATN_FE`, `Document`, `Dataset`, `.env`, backups, `.serena/`) touched.
- **Verification Summary:**
  - **Python 3.12.13 `.venv` & runtime (`SAG-ING-001`):** `.venv` rebuilt on Python 3.12.13 (140 packages synced); `.local/sag-runtime/start-sag.ps1` launches Python 3.12.13 directly with explicit `sys.version_info >= (3, 12)` guard.
  - **AST syntax check:** `307/307` `.py` files across `sag_api`, `sag_agent`, and `tests` passed (`0` syntax errors).
  - **OpenAPI export (`TEST-001`):** `SAG/docs/openapi/sag-api.openapi.json` exported cleanly (`114` paths).
  - **Full isolated `pytest` (Python 3.12.13):** **917 passed, 0 failed, 5 skipped** (`0` `math.sumprod` crashes; includes 21 new regression tests in `tests/test_full_audit_remediation.py` + `tests/test_parsing_json_text.py`; final handover fix verified with `56 passed in 25.83s` after the 916/917 full run).
  - **`SAG/apps/web` checks:** `tsc --noEmit` (`0` errors), `eslint . --max-warnings=0` (`0` errors, `0` warnings), `vitest run` (`10/10` test files, `63/63` tests passed including new `HttpOnly` SSO code-exchange test `app/api/auth/session/route.test.ts`).
- **Evidence Report:** `C:\Users\KAYT\.gemini\antigravity\brain\239821f2-3439-4e7c-8f68-23ae7b8ec3e0\scratch\fix-sag.md`.

## PR #76 review follow-up

The HttpOnly flow uses the cookie-backed identity API. The callback shares its
one-time exchange across effect re-runs and session mutations require a
same-origin request. The web proxy drops privileged headers supplied by callers
and serves original downloads as attachments. Compose configures the internal API
origin. Grounded streaming defaults to buffered until verification. No database
migration is included. The Conflict reviewer write path still needs a separate
Continuum BFF integration. Verify the web checks, Compose, and Python suite.

A second follow-up makes revocation cleanup retryable. PostgreSQL keeps the
revoked state when Qdrant is unavailable. The retry removes only the original
version IDs, so a later ingestion is kept. Source aggregate recomputation takes
a row lock for concurrent promotions. The canonical ingestion suite passed 21
tests. No database migration or live cleanup was applied.
