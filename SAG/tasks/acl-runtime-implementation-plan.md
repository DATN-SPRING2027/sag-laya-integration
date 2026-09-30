# SAG ACL Runtime Implementation Plan

**Branch baseline:** stacked on PR #9 at `c951267` (`feat/Thang-P4-acl-runtime-be-api`)

**Status:** implementation in progress; **P1 is not complete**.
**Authority:** BE/Continuum resolves principal and Project access. SAG verifies
the signed assertion and owns the confirmed Project→Source mapping.

## Plan and current result

| Step | Work | State |
|---|---|---|
| 1 | Inventory HTTP, stream, MCP, agent, direct-read, graph, Dify, eval, OCTX and internal evidence paths. | Done in [evidence-path inventory](../docs/security/acl-evidence-path-inventory.md); route regressions remain part of acceptance. |
| 2 | Specify assertion claims, RS256/JWKS trust, lifetime, cache, replay and failure behavior. | Draft in [Principal Assertion Contract](../docs/security/principal-assertion-contract.md); BE/security freeze required. |
| 3 | Specify one active Project per Source, confirmation audit, unassigned-source behavior, import rules and backfill. | Draft in [Project-to-Source Mapping Contract](../docs/security/project-to-source-mapping-contract.md); DB/data-owner approval required. |
| 4 | Add verifier, mapping schema/resolver, request intersection and failure behavior. | Implemented with mocked issuer/JWKS and local DB tests. No Project-scope cache is used. |
| 5 | Enforce ACL before retrieval/direct reads and close unsupported global graph paths. | Implemented on inventoried routes; MCP stdio and global Universe manifest/rebuild are disabled. Continue route-by-route regression review. |
| 6 | Integrate global dense/lexical RRF without raw-score addition; make semantic-only gate invariant to positive score scale. | Preserved from PR #9 and verified with the ACL runtime changes on this branch. |
| 7 | Runtime acceptance with production signer, JWKS rotation/revocation, approved mappings/backfill and real cross-tenant tests. | Awaiting external authority/data-owner deliverables; mock tests do not satisfy this step. |

## Security invariants

1. No verified principal means no evidence retrieval or read.
2. No confirmed Project→Source mapping means the Source is not searchable/readable.
3. No dense, lexical or source-scoped graph/event candidate is generated outside the effective Source scope.
4. Client Source IDs only narrow: `requested_source_ids ∩ authorized_source_ids`.
5. An omitted Source list resolves only to the bounded set of confirmed Sources mapped to the principal's allowed Projects. Empty scope does not invoke retrieval.
6. There is no authorization-scope cache. Mapping lookup errors fail closed with no global fallback.
7. Global P4 is dense + lexical RRF only, without KG/Tree or graph fields. P3 graph/event recall is allowed only after Source authorization.

## Path disposition on this branch

- **Protected:** `/search`, `/search/stream`, `/search/eval-compare`, source search,
  Source listing/reads/chunks, document/knowledge/insight routes, activity, Dify,
  agent ask/OpenAI chat/message history, source-scoped Universe reads, MCP HTTP,
  OCTX transfer/export reads and source/document job reads. Mapping resolution
  precedes candidate generation or file/engine access.
- **Disabled:** MCP stdio; global Universe manifest and rebuild; global P4 graph,
  event, KG and Tree projection.
- **Constrained:** created/imported Sources stay unmapped and inaccessible until
  an owner-approved mapping exists. OCTX import into an existing Source checks
  the requested destination before applying it.
- **Awaiting integration/review:** Continuum assertion issuance and JWKS,
  trusted BFF header overwrite/stripping, scoped service grants for Dify/MCP,
  production DB migration approval, mapping writer/revoke authority and
  owner-approved legacy Source inventory/backfill.

## Local verification on the combined branch

- Focused ACL, retrieval, search strategy and stream suite: 80 passed.
- Settings test in the checkout without a local `.env`: passed. The earlier
  `llm_timeout_ms` failure was caused by a local `.env` override.
- Full API suite on Windows: 651 passed, 6 failed, 1 skipped, and 1 teardown
  error. The six failing tests reproduce on the PR #9 checkout or are flaky on
  both branches. The teardown error was a shared SQLite database lock; the
  affected hardening test passed three isolated runs on each branch.
- These local tests use test-issued assertions and mappings. They do not satisfy
  Continuum staging, approved legacy backfill, or revocation acceptance.

## Security test plan

- Verifier: valid RS256 signature; wrong audience; missing/bad Organization or
  Project; malformed/duplicate Projects; expired/future assertion. Add key
  rotation, unknown key, JWKS outage and response-limit tests.
- Mapping: confirmed, pending, unmapped, revoked, wrong Organization, wrong
  Project, ambiguous mapping, requested-scope intersection, absent/empty scope
  and mapping dependency error.
- Retrieval boundary: seed an unauthorized higher-score Source and assert it is
  absent from retriever input before top-k. Verify missing assertion blocks
  `/search` and `/search/stream`; verify unauthorized P3 Sources do not reach
  graph/event recall.
- Direct reads and integrations: test not-found before file/chunk/document/graph
  I/O; then cover Dify, MCP HTTP, OCTX, saved agent history including local
  knowledge-tool trace provenance, eval and jobs. Use real BE assertions for
  final cross-Project/cross-Organization acceptance.

## Runtime acceptance gate

`real verified assertion + approved current mapping + fail-closed unmapped Source
+ pre-candidate ACL on every reachable evidence path + approved revocation bounds
+ real cross-Project/cross-Organization leakage tests = P1 complete`.

Until every term is evidenced, PR #9 remains Draft and **P1 remains NOT DONE**.
