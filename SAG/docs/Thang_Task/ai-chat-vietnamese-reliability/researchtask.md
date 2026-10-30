# AI Chat: retrieval reliability and Vietnamese evaluation

## Scope and base

Thang requested a fresh branch, diagnosis and repeated live test -> inspect ->
fix -> retest, including Vietnamese understanding and answer quality. Branch:
`feat/Thang-ai-chat-vietnamese-reliability-be-api`, from updated `origin/main`
`cd3b7a6`. Related BE/FE branches are separate deliverables if code changes are
needed there. Thang subsequently authorized committing, pushing and opening PRs
for the implemented fixes. Merging remains outside the authorized action.

## Verified current flow and boundaries

- BE `AiChatService.resolveSagSession` authenticates the current IAM actor and
  Project before SSO to SAG. Browser auth remains HttpOnly/BFF controlled.
- SAG agent -> `SearchContextTool.invoke` -> `retrieve_search_unit_sections` ->
  authorized Source mappings and manifest-verified current DocumentVersions ->
  Qdrant dense/sparse -> canonical hydration -> evidence pack -> cited answer.
- PostgreSQL is canonical; Qdrant is a derived search accelerator. Exact tenant,
  Project, security partition, version, manifest and locator checks must remain.
- `no_answer_text()` is currently a fixed English response. A retrieval outage
  or index inconsistency must not masquerade as an ordinary lack of evidence.
- Readiness claims in PostgreSQL alone are not live retrieval acceptance.

## Baseline evidence (2026-10-30, local only)

- Organization WDP301-SPRING-2027: 137 uploaded files plus one unfiled draft.
  PostgreSQL has 94 active SEARCH_READY / DATA_READY documents, 1,209 chunks.
- Frontend has three ready documents. A real UI request for the framework in
  `02_FRONTEND_NEXTJS.md`, explicitly in Vietnamese with citations, returned
  the English no-answer response in 12 seconds, zero citations.
- Persisted public tool trace: correct Frontend Project/Source scope, no local
  or global candidates, no transport failure recorded.
- Qdrant Frontend collection exists (134 points), but sampled Source/Version
  identifiers differ from canonical PostgreSQL. Exact scoped coverage and safe
  recovery must be checked before any mutation.
- Two API processes listen on different addresses at port 8000. Do not stop the
  unrelated reload process; only restart the identified native task service
  when required. Never expose credentials or frozen model prompts in evidence.

## Proposed changes and operational work

1. Add regression tests for an empty accelerator under a nonempty verified
   canonical scope. Distinguish genuine no-match from missing indexed versions.
2. If the regression confirms it, update `sag/search_unit_store.py` and
   `services/search_unit_retrieval_service.py` to report an unavailable index
   through existing sanitized error semantics. Keep query deadlines and ACLs.
3. Recover only missing derived points using current canonical units and the
   configured embedder. Upsert exact stable IDs; no collection deletion, no
   orphan cleanup, no authorization/mapping or source document changes. Record
   the operation and scoped coverage verification. Inspect existing indexing
   helpers before choosing implementation.
4. Retest actual answers, then fix demonstrated Vietnamese/grounding defects in
   their owning layer with failing regression tests first. Do not pre-emptively
   change routing or lower evidence thresholds.

## Test matrix and acceptance

- Exact named document; Vietnamese paraphrase; unaccented Vietnamese; natural
  typo; concise answer instruction; follow-up reference to previous question.
- A second Project with ready sources; multi-fact/comparison within a Project.
- Unsupported fact and nonexistent document: safe abstention, no fabrication.
- Cross-Project request: no scope expansion or unauthorized citations.
- Source-backed fact correctness, Vietnamese output, useful answer, citation
  provenance/click, error reporting and latency evaluated separately.
- Each live test records question, scope, outcome, sources, elapsed time and
  pass/fail reasons. Unit fixtures never count as live acceptance.
- Run focused regression tests, relevant broader suites and required quality
  gates. Review full diff, secrets, generated files and unrelated changes.

## Impact, rollback and exclusions

No planned schema/migration, IAM permission, credential or provider changes.
Any required canonical data change needs its own database branch and explicit
impact/rollback record. Derived point recovery must retain unrelated points.
Out of scope: broad ingestion rewrites, model training, globally changing
embedding identity, fake answers, disabling ACLs or unrequested UI redesign.

## Results

### Implemented fixes

- `sag/search_unit_store.py`: strict exact point counts with the existing
  sanitized transport and complete caller-supplied scope filter.
- `services/search_unit_retrieval_service.py`: when both retrieval paths return
  no candidates, check accelerator availability under the same tenant, Project,
  partition and current-version filters, within the existing query deadline.
  Missing indexed scope is a service outage rather than an evidence abstention.
- `tools/base.py` and `services/agent_service.py`: retain an unresolved search
  outage across the runtime's recoverable tool-error boundary, emit/persist a
  failed host run, sanitize provider details, and clear the flag after a
  successful search retry.
- `services/search_index_service.py`: import the missing `settings` dependency
  used by embedding usage recording. The previous NameError could silently
  trigger the session embedding fallback; the existing incremental-session
  regression now passes without weakening its assertions.
- `generation/prompt.py`: preserve numeric baselines, relative reductions and
  final levels when paraphrasing source evidence. This is guidance, not a
  deterministic semantic-correctness guarantee.
- New regression suites: `tests/test_search_index_consistency.py` (11 cases)
  and `tests/test_agent_retrieval_failure.py` (3 cases), verified RED before fixes.

### Operational recovery (already applied, not replayed by this PR)

Read-only comparison confirmed that all expected points for the three Frontend
versions were absent, despite unrelated points in the same Project collection.
Canonical reconstruction matched the stored IDs, hashes and block ranges.
Missing derived points were upserted for 94 ready documents across eight
Projects: 1,209 points verified by stable ID and content hash. Existing unrelated
points were retained. No canonical PostgreSQL data, schema, permissions or
Source mappings were modified by the recovery script.

The environment-specific recovery script and per-Project added-point manifests
remain outside Git in the local task artifacts. They are not application assets
and must not be automatically replayed in another environment. Other deployments
need their own read-only consistency check and reviewed recovery plan.

### Validation and limits

- Python 3.12: `python -m pytest tests -m "not ai_smoke" -q --tb=short`:
  **168 passed, 1 deselected**. The opt-in AI smoke test was not part of this run.
- The six-file Python lint comparison against main introduced no diagnostics.
  Existing baseline diagnostics remain in `agent_service.py` (24 E501) and
  `search_index_service.py` (8 diagnostics); repository-wide lint is not clean.
- Real authorized browser session in WDP301-SPRING-2027 / Frontend: the original
  framework question now answers Next.js (App Router) with the correct document;
  source preview opens. Unaccented Vietnamese, follow-up reference, two-fact
  state-management comparison, nonexistent document and unsupported-fact cases
  were exercised. The final two correctly abstained.
- An initial summary changed "reduced by 45%" into "reduced to 45%". After the
  prompt change, a new summary retained the correct reduction. Repetition and
  holdout evaluation remain incomplete; no general intelligence score is claimed.
- The typo case answered correctly but cited a less direct BFF passage. Citation
  entailment calibration, wider Project coverage and the full benchmark remain
  follow-up work. Unit fixtures are not substituted for live acceptance.
- FE localization, historical no-answer and Copy fixes are a separate DATN_FE
  branch/PR. DATN_BE has no code changes for this scoped patch.

### Impact and rollback

No migration, public request schema, provider configuration, credential or IAM
policy change. Revert the application commit to roll back the code. That does
not remove recovered Qdrant points. Any operational rollback must use only the
recorded added IDs after owner review; never delete a whole shared collection.
Normal live test conversations persist through the existing application flow.
