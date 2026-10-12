# Research Task — PR #26 review follow-up

- Task: address the four review notes on Phase 9 knowledge quality scoring.
- Date: 2026-10-12.
- Repository/branch: SAG, `feat/Thang-sag-phase9-knowledge-quality-be-api-db`.
- PR: https://github.com/DATN-SPRING2027/sag-laya-integration/pull/26.
- Main research/specification: [research.md](research.md); this record scopes only the review follow-up.
- State before implementation: branch contains commit `67fcddb`; only unrelated untracked `.serena/` exists at worktree root.

## Verified current flow and contracts

- `knowledge_service.checksum(value)` in `apps/api/sag_api/services/knowledge_service.py` computes canonical JSON SHA-256 (`sort_keys`, compact separators, UTF-8, no NaN). `knowledge_quality_service.py`, `knowledge_quality_persistence.py`, and `knowledge_gap_question_service.py` each duplicate this helper. `knowledge_quality_repository.py` already imports the shared helper.
- `retrieve_search_unit_sections` finishes retrieval/routing, then awaits `_record_routed_query_demand`. The writer validates route scope fingerprints, writes only scoped node/day counts through a separate `SessionLocal`, has a 100 ms timeout and logs ordinary failures without failing search. Awaiting it can add up to the timeout to search response latency.
- Demand totals use equal adjacent windows. A node has known demand only when at least one bucket exists in both the current and prior window; absent buckets are `None`, which keeps priority/question eligibility fail-closed. Demand has no raw query/message field.
- The search route calls the retrieval service before assembling its response/stream. `asyncio` background-task sets are an established in-process pattern in `sag_api/jobs/inproc.py`; a short best-effort metric write can use a retained task reference without changing search API contracts.
- ACL/provenance boundary remains tenant, project, security partition, tree version, and validated routing fingerprint. The follow-up must not pass query text into the writer or detach the scope checks.

## Proposed minimal changes

1. Import and reuse `knowledge_service.checksum`; remove only the three duplicate checksum definitions and imports made unused. Keep JSON serialization in the question service for its prompt payload.
2. Schedule the existing fail-soft demand writer with `asyncio.create_task` after a valid search result; keep a strong reference until completion and test that request flow does not wait on the DB write. Preserve the 100 ms writer timeout, logging, and ACL filtering. Demand remains approximate/best-effort during process shutdown.
3. Correct the Phase 9 task folder name from `[SAG][Phase 9}` to `[SAG][Phase 9]`; repair links and document cold-start explicitly: at least one recorded bucket in each of the current and preceding windows is needed before demand/growth can affect eligibility.
4. Update this task record and `tasks/todo.md` with actual implementation/check outcomes.

## Test matrix and acceptance

- Unit test shared-checksum parity for quality assessment, persistence, and question output.
- Async query-demand test proves scheduling returns without awaiting a stalled write and retains the task until completion; existing writer tests cover the validated scope/privacy boundary.
- Run focused Phase 9, routing, and retrieval regressions plus Ruff on changed Python files and `git diff --check`.
- Every review note is resolved or explicitly documented; the PR remains on `main` and no unrelated worktree files are committed.

## Out of scope and known gaps

- No durable queue, retry store, demand backfill, schema/API changes, or altered scoring semantics.
- Background demand writes may be lost on forced process shutdown; missing demand windows remain unknown and prevent priority eligibility.
- Production PostgreSQL/Qdrant and production traffic acceptance remain outside this PR's local verification.

## Implementation and verification results

- Reused the shared canonical checksum helper in all three Phase 9 services; added a regression for non-blocking, strongly retained demand recording.
- Corrected the evidence folder and recorded the two-window cold-start rule in the plan and todo evidence.
- Ruff on the five changed Python files passed; Python 3.11 `py_compile` passed on the same files.
- Pytest could not run after these edits: the default runner lacks `zleap`; the locked `uv` environment failed to build `litellm` because MSVC `link.exe` is unavailable, before test collection. The 26/26 Phase 9 and 27/27 routing/retrieval outcomes were reported by the reviewer before this follow-up, not rerun against these edits.
- Implementation commit: `c5ca0946fd83f13f3cbd153b2d49dbb854f5d63b` (`fix(sag): address Phase 9 review feedback`). PR #26 remains open.
