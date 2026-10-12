# [SAG][Phase 11] ACL- and version-safe cache boundaries

## Task source

User-provided Phase 11 task. The repository's Phase 11 summary is in
[`SAG/tasks/plan.md`](../../../tasks/plan.md) and its checklist is in
[`SAG/tasks/todo.md`](../../../tasks/todo.md).

## Purpose

Add cache behavior only after route, search, and tree version boundaries are
stable. Cached decisions or results must preserve tenant/project authorization,
ACL changes, data versions, and request filters.

## Scope

- Include tenant/project scope, an ACL fingerprint and epoch, search/tree/model/
  planner/config versions, and all filters relevant to the cached artifact in
  cache keys.
- Keep answer caching disabled by default.
- Enable semantic route caching only after measured route-equivalence precision
  and ACL isolation satisfy gates agreed by the project owner.
- Cover authorization revoke, tree/search changes, publish/rollback, and
  cross-scope isolation.

## Acceptance criteria

- A cache hit never crosses ACL, tenant/project, data-version, or filter
  boundaries.
- Revoke and tree/search epoch changes cannot serve stale protected evidence.
- Tests and evidence report route-equivalence precision before any optional
  semantic route cache is enabled.

## Guardrails

- Current principal and Source authorization checks remain authoritative; a
  cache must not replace or bypass them.
- No answer/evidence response cache is enabled by default.
- Preserve unrelated local files and keep database changes separate if a
  persistent ACL epoch or cache store later proves necessary.
- Research is recorded before implementation; implementation starts only after
  the user approves `research.md`.
