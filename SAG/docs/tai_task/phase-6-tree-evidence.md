# Phase 6 tree builder evidence

Date: 2026-10-03
Scope: Phase 6 tree/profile/quality candidate builder and early B2 fixture contract.

## Contract

`routing-snapshot.v1` accepts Phase 5 Knowledge Units and graph edges. Each input
unit carries tenant, project, security partition, dense/sparse features, entities,
and validity range. The builder requires one tenant/project per invocation and
builds a separate root for each security partition. Cross-partition edges are
discarded before clustering or metrics.

The resulting immutable snapshot contains stable node IDs, parent/depth lineage,
profiles (bounded-candidate cosine medoid, sparse terms, entities, temporal range,
accessible-unit count), a content/config/edge/algorithm/benchmark checksum, quality
metrics, quality-gate decisions, and a `publishable` flag. Dense medoid selection
scores at most 64 deterministic candidates against all vectors in the node to keep
profile construction bounded. A failed gate has manifest status `REJECTED`; the
pure builder does not write or switch an active tree.

The B2 fixture is exposed by
`sag_api.services.routing_tree_service.fixture_snapshot()` and deliberately has
two security partitions with separate profiles and hierarchy roots.

## Synthetic fixture measurement

Reproducible fixture tree version:
`tree-260c6d76cdd0c7369ad9069e`

| Metric | Fixture result |
|---|---:|
| Cohesion | 0.7143 |
| Giant ratio | 0.375 |
| Child-size entropy | 1.7753 |
| Edge cut | 0.2857 |
| Depth | 1 |
| Routing recall@k | 1.0 |
| ACL routing leakage | 0.0 |
| ACL blackhole rate | 0.0 |
| Quality gates | 10/10 passed |

The recall value is computed from the fixture's supplied routed-unit outcome. It
is a contract/regression value, not a production retrieval benchmark.

## Verification

- `tests/test_phase_6_routing_tree.py`: **15 passed**.
- Ruff on the builder and focused test: **passed**.
- `uv lock --check`: **passed**.
- `git diff --check`: **passed** (Git reports only a CRLF normalization notice for
  `uv.lock`).
- `igraph==0.11.9` and `leidenalg==0.10.2` are pinned through project ranges and
  `uv.lock`; the local venv install used these locked versions.

## Remaining integration gates

- Phase 5 Knowledge Unit/Graph producer and persistence are not implemented in
  this repository; `todo.md` still records those Phase 5 items as open.
- This slice returns an in-memory candidate snapshot. Persisting nodes/profiles,
  recording the manifest/lineage in PostgreSQL, and transactionally switching the
  active tree need the producer/storage contract and remain open.
- Corpus routing-recall thresholds and ACL runtime leakage tests remain open, so
  this evidence does not mark Checkpoint B `ROUTING_READY`.
