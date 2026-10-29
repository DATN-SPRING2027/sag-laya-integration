## Problem and behavior

Provides project-scoped grounded Handover assistance: official source proposals, cited draft generation and cited Q&A through the existing BFF principal boundary. Inputs bound tenant/project/partition/source version; Task Notes stay out of automatic proposals and official Q&A.

Review round 2 prevents the enrichment query from materializing every CanonicalBlock of a selected document version. It joins only the selected SearchUnit spans, caps each unit at 8 blocks, and caps each returned text cell at 1,600 characters in SQL. Existing exact source/version filters, per-source SearchUnit allocation and fair round-robin prompt budget remain intact.

Oversized spans, oversized text and per-source unit limits now feed coverage warnings. Coverage also aggregates duplicate source inputs and strips logical-source whitespace consistently. BE #110 projects a bounded partial-evidence warning that FE #102 renders.

## Validation

- Existing API virtualenv: `python -B -m pytest -p no:cacheprovider tests/test_handover_assist.py -q` — **10 passed**.
- Scoped `ruff check` and `ruff format --check` — PASS.
- Regression fixture seeds 1,000 unrelated blocks in the requested version and verifies rows fetched by SQL, including a huge requested span. Further regressions cover duplicate source inputs and normalized logical IDs.
- CodeRabbit raised 1 minor coverage-accounting issue. Reproduced with two failing tests, fixed, then reran all 10 Handover tests successfully.

## Database and configuration

No schema, migration, seed, index, Qdrant or environment change. Reuses the existing service token and project BFF principal enforcement. SQL is tested with actual SQLAlchemy/SQLite execution; live PostgreSQL integration is not claimed.

## Risks, dependencies and rollback

Coordinate deployment with BE #110 / FE #102 to display coverage warnings. Very large units deliberately contribute bounded evidence and report truncation; original source access remains available under its own permissions. Roll back the code commit independently; no data rollback is needed.
