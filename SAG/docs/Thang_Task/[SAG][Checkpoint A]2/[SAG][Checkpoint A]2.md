# [SAG][Checkpoint A] Production E2E search, ACL and citation acceptance

## Purpose

Close the remaining production acceptance gaps for Checkpoint A without reimplementing the already completed Phase 1–2C pipeline or Search reader.

## Scope

- Confirm trusted tenant/security-partition, Project-to-Source, readiness/retry, and embedding-model identity contracts with BE/security/ingestion owners.
- Run real upload → worker/index manifest → Qdrant → `/search`, `/search/stream`, and `search_context` on a corpus with real SearchUnit locators.
- Verify staging ACL leakage/revocation, enrichment disabled/lagging/failure behavior, and split-SearchUnit FE navigation.
- Validate citation/provenance across assistant history, answerability/exact-anchor behavior, and canonical stream acceptance.

## Acceptance

- Evidence records the actual provider/corpus/runtime used and confirms no forbidden evidence is returned after revoke.
- Search/citations resolve exact source/version/block/page/anchor, or fail closed when locator is absent.
- Upload-to-answer works with real Qdrant and trusted principal scope; no fixture-only acceptance.
- Update Checkpoint A items/evidence in `SAG/tasks/todo.md`.
