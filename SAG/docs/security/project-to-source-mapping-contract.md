# Project-to-Source Mapping Contract

> **Status:** Draft for data-owner, BE/Continuum, and security review. No runtime
> schema/migration or confirmed legacy mapping is implied by this document.
>
> **Date:** 2026-09-30
> **Boundary:** BE/Continuum owns identity and Project authorization. SAG owns
> Source metadata and the authoritative mapping from a Source to its Project.

## 1. Purpose and invariants

This contract defines how a verified principal's `allowed_project_ids` become
the exact Source scope SAG may search or read. It does not add independent
Document or Document-version ACL in P1/P4. Documents, versions, chunks, events,
entities, and derived evidence inherit authorization from their owning Source.

The Project-to-Source mapping and every consumer must preserve:

1. No verified principal means no evidence operation.
2. No confirmed mapping means a Source is not searchable/readable.
3. No candidate or direct evidence read may occur outside `effective_source_ids`.
4. Client Source IDs only narrow authority; absent IDs mean the full authorized
   Source set, never global/unrestricted access.

## 2. Ownership and identifier semantics

| Data | Authority/source of truth | SAG responsibility |
|---|---|---|
| User, Organization, Project, membership and Project authorization | Continuum/BE | Verify the signed decision; do not join BE MongoDB or infer roles. |
| SAG Source, Document, job, storage and index lineage | SAG PostgreSQL and SAG-owned stores | Maintain the Source's confirmed Project mapping and inherit its scope for children. |
| Project IDs in the SAG mapping | Opaque external IDs issued by BE | Compare exact ID and `org_id`; do not create a fake FK to a Project table that SAG does not own. |
| Qdrant/vector payload | Retrieval accelerator | Never treat a payload as authority or as the only mapping source. |

The accepted workspace ownership note says BE owns User/Organization/Project/
Membership/Permission and SAG owns Source/document/job metadata. This mapping
contract follows that boundary and refines it for Project-level search ACL.

## 3. Cardinality and mapping state (P1 proposal)

P1 uses **one active confirmed Project per Source**. A Project may own many
Sources. A Source is not shared across Projects in this version. If actual data
proves cross-Project sharing is required, stop before implementation and obtain
a reviewed many-to-many policy; do not silently widen cardinality.

```text
Organization
  └── Project (external opaque ID)
       └── Source (SAG-owned; at most one active Project mapping)
            └── Document / Version / Chunk / Event / Entity (inherit Source scope)
```

Logical mapping states:

| State | Meaning | Search/read allowed? |
|---|---|---|
| `UNMAPPED` | No mapping exists | No |
| `PENDING` | A mapping was proposed/imported but not confirmed by the trusted mapping workflow | No |
| `CONFIRMED` | Exactly one validated `(org_id, project_id)` assignment is active | Yes, only for assertions whose `org_id` matches and whose `allowed_project_ids` contains `project_id` |
| `REVOKED` | A previously confirmed mapping was revoked or replaced; retain audit history | No |

`Source.status`/ingestion readiness is independent from mapping state. A
`READY` Source may still be non-searchable because its mapping is unmapped or
pending. Authorization code must join only a confirmed active mapping.

## 4. SAG-owned storage proposal

SAG PostgreSQL is the mapping source of truth. Project and Organization IDs are
opaque external identifiers; there is no cross-database FK to BE/MongoDB.

Physical shape implemented for review:

```text
source_project_mappings
  source_id                 FK -> sources.id; one current assignment per Source
  organization_id           opaque Continuum Organization ID
  project_id                opaque Continuum Project ID
  state                     PENDING | CONFIRMED | REVOKED
  mapping_version           monotonic version for this Source assignment
  confirmed_at/by           present only after trusted confirmation
  approval_ref              owner ticket/change reference for confirmation
  batch_id/input_sha256     audit reference for explicit backfill input
  revoked_at/by             present after revocation; preserve history/audit
  revocation_ref            owner ticket/change reference for revocation
  created_at/updated_at
```

The SQLAlchemy model is
[SourceProjectMapping](../../apps/api/sag_api/db/models/source_project_mapping.py);
the additive PostgreSQL DDL and rollback scripts are under
`SAG/apps/api/scripts/acl_001_source_project_mappings*.sql`. Existing Sources
have no mapping row and remain non-searchable. No existing migration was
edited. The checked-in API tree has no Alembic environment or revision history,
so production deployment still needs the DB owner to approve the SQL runner and
window. The API's `create_all` path creates this table in dev; it is not claimed
as a production migration runner. Documents/versions continue to inherit Source
scope in P1. Future independent document ACL needs a separate approved contract.

Required data invariants, regardless of physical shape:

- `source_id` references a real SAG Source.
- At most one current pending or confirmed mapping exists per Source.
- Confirmation requires non-empty `organization_id` and `project_id`.
- A mapping's `organization_id` must equal the verified assertion's `org_id`
  before the Project ID is considered.
- Mapping absence, invalid state, duplicates, conflict, or lookup error is not
  interpreted as a default Project.
- Every mapping confirmation/revocation records a version, actor, time and
  approval reference. Qdrant is not the mapping authority.

## 5. Assignment authority and Source lifecycle

### Assignment authority

BE/Continuum remains the authority for whether an actor may assign or move a
Source into a Project. Read access to a Project does not implicitly grant
mapping-write permission. The ordinary principal assertion's
`allowed_project_ids` proves readable Project scope; by itself it must not
authorize mapping administration.

Before enabling online mapping writes, BE and SAG must approve one explicit
trusted mechanism, such as a BE-issued mapping command/service grant bound to
`source_id`, `org_id`, and `project_id`. SAG must verify that mechanism and must
not interpret user role names. Until then, only the owner-approved
`scripts/source_project_acl.py` operator/backfill process may confirm mappings;
user-facing Source creation cannot make a Source searchable by supplying a
client Project ID. `POST /sources` may persist a requested mapping as
`PENDING`, after checking that the Project is in the verified principal's read
scope. Its response reports `mapping_state: "PENDING"`; only the existing
owner-approved workflow can confirm it. If the Project is omitted, creation
may infer the only readable Project, but it must reject an ambiguous multi-
Project scope rather than pick one.

### Normal Source creation and connector ingestion

- Source creation may accept a requested target Project as input, but the input
  is only a request. A Source becomes searchable only after the trusted mapping
  writer confirms the exact assignment.
- If the workflow cannot supply/confirm Project at creation, either reject
  creation where product policy requires an assigned Project or persist it as
  `UNMAPPED`/`PENDING`; both states are non-searchable. Do not default to an
  Organization-wide or global scope.
- Connectors inherit the Source mapping. A connector credential is not a user
  scope; integration retrieval requires a separate authority-issued scoped
  service grant.
- Creation, ingestion, reprocess and index readiness must not toggle mapping
  confirmation implicitly.

### OCTX transfer/import

- OCTX package Source/Project/Organization IDs are foreign-instance metadata,
  not authorization evidence.
- Import must bind the new local SAG Source to an explicitly selected
  destination Project through the trusted mapping workflow. Do not carry over a
  foreign Project ID or mark imported scope confirmed solely because the package
  declares it.
- If target mapping cannot be confirmed, import may remain operationally
  pending if product permits, but the Source is not searchable/readable. If
  pending imports cannot be safely isolated, reject the import before it
  publishes searchable evidence.
- Source replacement/rollback during OCTX transfer must preserve the confirmed
  target mapping or fail closed; restoring an old Source must not revive an
  unverified mapping.

## 6. Scope derivation and pre-retrieval enforcement

Given a verified principal `p` and requested Source IDs `R`:

```text
authorized_source_ids =
  { m.source_id |
      m.state = CONFIRMED
      AND m.organization_id = p.org_id
      AND m.project_id ∈ p.allowed_project_ids }

effective_source_ids =
  authorized_source_ids                  if request omitted source_ids
  requested_source_ids ∩ authorized_source_ids otherwise
```

Normalize duplicate request IDs deterministically. The request set can only
reduce the authorized set. If the effective set is empty, return no evidence
without calling dense, lexical, event/graph, or direct-read storage operations.

Enforcement requirements:

- Resolve mappings before global Source candidate selection. Do not call a
  helper whose `source_ids=None` means “select global candidates” on an
  evidence-producing path.
- Dense/vector retrieval receives an exact Source filter before ANN candidate
  generation and before its top-k.
- Lexical retrieval receives the same exact Source filter before ranking/cap.
- P3 graph/event recall is given only effective Sources before recall; no graph
  traversal may expand to a Source outside that set.
- Source-scoped search/read first authorizes its route `source_id`, then passes
  the singleton authorized scope inward.
- A post-top-k filter is defense in depth only and cannot satisfy this contract.
- Deduplication across shared content may not expose unauthorized Source names,
  IDs, counts, or provenance. A returned evidence item must have an authorized
  Source provenance edge.
- Global P4 remains dense + lexical + fusion only: no Knowledge Graph/Tree,
  no graph projection, and empty graph response fields. Keep Source-scoped P3
  graph behavior only behind the same pre-recall authorization.

## 7. Legacy backfill and migration safety

No legacy Source may be assigned by guessed ownership, source name, uploader,
creation order, current login organization, or a global default. Backfill is an
explicit data-ownership operation:

1. Inventory every Source and emit a report with its ID, current status,
   document/chunk counts, and proposed mapping input columns; do not expose
   document content in the report.
2. Obtain an owner-approved `source_id → org_id, project_id` mapping from an
   authoritative business record. Unresolved rows remain unmapped.
3. Dry-run: reject duplicate Source assignments, unknown Source IDs,
   conflicting mappings, missing Project/Organization IDs, and invalid IDs;
   report mapped, pending, and unresolved counts.
4. Apply idempotently in a transaction/batched migration, writing mappings as
   pending until confirmation evidence is attached, then confirm only reviewed
   rows. Preserve actor, timestamp, batch ID, and input digest for audit.
5. Reconcile row counts and sample ownership with the designated data owner.
   Searchability activates only after confirmation.
6. Roll back by revoking/disabling erroneous mappings and incrementing the
   mapping epoch; never roll back to unrestricted/global retrieval.

`source_project_acl.py inventory` reports metadata only. `apply` validates an
owner-approved CSV and is dry-run by default; `--apply` confirms rows only when
the operator explicitly requests a write. Conflicting active mappings are
rejected and must be revoked through a separately approved command. Legacy
Sources remain unmapped until their owner supplies an approved mapping file.
Production DDL uses the additive SQL script above pending approval of the
repository's missing Alembic runner/baseline; rollback drops the mapping table
and disables all mapped search/read scopes without restoring global access.

## 8. Mapping and authorization cache proposal

P1 default: do not cache `effective_source_ids` across requests. If measured
performance later requires a cache, all requirements below are mandatory:

- Mapping changes increment a monotonic organization mapping epoch in the same
  transaction; confirmation, revoke, reassignment, and backfill all advance it.
- Cache key includes a stable hash of `(issuer, subject, org_id,
  allowed_project_ids)`, mapping epoch, and contract version. Never key only by
  query or by a client Source list.
- Maximum mapping-scope cache TTL is 30 seconds and can never extend beyond the
  assertion's remaining lifetime. Publish invalidation on mapping/authorization
  changes where available; TTL is the fallback bound, not the sole claim of
  immediate revocation.
- If current mapping epoch cannot be read/validated, fail closed and do not use
  stale cached scope. If invalidation is unavailable, document the worst case:
  at most 30 seconds for mapping changes and at most assertion lifetime plus
  configured clock skew for BE permission revocation.
- Any evidence/result cache includes the same scope fingerprint, org, mapping
  epoch and retrieval/config version, or is disabled. Never serve an evidence
  cache entry across scopes.

## 9. Required contract/security tests

Mock/contract tests must cover confirmed vs unmapped/pending/revoked mapping,
Organization mismatch, cross-Project and cross-Organization Source IDs,
requested-scope intersection, omitted requested IDs, empty scope, mapping error,
mapping reassignment/cache epoch, and OCTX import with untrusted foreign IDs.

Pre-top-k tests must seed an unauthorized vector and lexical result with higher
score than an authorized result and inspect retriever candidate inputs to prove
the unauthorized row never entered candidate generation. P3 tests must prove
the unauthorized Source never reaches `recall_event_scores`/graph expansion.
Direct read tests must prove authorization occurs before opening the file or
calling `get_chunk`/document/entity APIs.

These tests prove SAG contract behavior with fixtures; P1 runtime acceptance
additionally requires real BE assertions, confirmed mappings and real
cross-Project/cross-Organization leakage tests over all production-reachable
paths.

## 10. Open decisions required before contract freeze

| Decision | Draft recommendation | Freeze owner/status |
|---|---|---|
| Physical schema | Dedicated explicit Source mapping relation with confirmation state/history; FK only to SAG Source | SAG DB owner reviews against actual migration runner and source lifecycle. |
| Cardinality | One active Project per Source for P1 | Confirm against real product data; stop if true sharing is required. |
| Mapping writer | Trusted BE-authorized command/service grant; ordinary read assertion is insufficient | BE + SAG security owners define endpoint/credential/claim. |
| Source creation while unassigned | May exist pending/non-searchable only if the product lifecycle can safely isolate it; otherwise reject | Product/ingestion owner decides. |
| OCTX destination | Explicit local target Project; package metadata never confirms it | OCTX/BE owner confirms UX/API flow. |
| Legacy assignments | Owner-approved mapping input; unresolved remains non-searchable | Data owner supplies inventory decisions; none is present in the repo. |
| Migration runner | New reviewed production Alembic migration/backfill path; no existing checked-in environment found | DB owner must establish safe production mechanism. |
| Cache | No effective-scope cache in P1; if later added, epoch + <=30s TTL + invalidation | Security/performance review approves before enabling. |
