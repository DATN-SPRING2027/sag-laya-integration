# DATN-67 — Phase 4 Fusion and Regression Evidence

**Date:** 2026-10-05
**Scope:** `SAG/tasks/plan.md` Task P4.2/P4.4 and Checkpoints P4.2/P4 complete.

## Dependencies and boundaries

DATN-35 / Checkpoint B is complete. This change consumes the authorized-scope
contract already used by global retrieval. It does not implement production ACL
issuer/JWKS, Project→Source mapping/backfill/revoke, or staging tenant-isolation
checks; those remain in DATN-61. DATN-62 Phase 5–6 can start after DATN-67 and
DATN-61 are both complete.

No ingestion/index, MMR, evidence-context, citation, or no-answer code changed.

## Implementation reviewed

- Dense/engine and lexical results remain separate ranked lists and fuse with
  Reciprocal Rank Fusion (RRF); raw scores are not added across retrievers.
- Output scores are normalized to `[0, 1]`. Retrieval stats report the fusion
  method, per-retriever candidate counts, fused candidate/relevant counts, and
  filtered count; global API responses preserve those stats.
- Exact candidates dedupe by source-config/source plus chunk key. When the chunk
  key is absent, the fallback is SHA-256 over the full normalized heading/body;
  long candidates sharing a prefix no longer collapse.
- Equal fused scores resolve by summed rank and then source-config/chunk key.
  Exact lexical terms retain their earlier rank within the lexical list.

## Regression coverage

- Semantic paraphrase without lexical overlap and exact glued identifier.
- Vietnamese query with retrieval-domain terms and an exact task identifier.
- Duplicate chunk across dense/lexical lists, source isolation, and missing-key
  fingerprints over long shared prefixes.
- Rank agreement, equal-score deterministic ordering, score-scale invariance,
  normalized output scores, candidate/filter counts.
- Global `/search` and `/search/stream` fusion stats, exact-identifier response,
  and P3 strategy/fallback trace preservation.
- SearchUnit retrieval and existing authorized-scope regressions.

## Checks

Run from `SAG/apps/api`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_retrieval_relevance.py tests\test_search_strategy.py tests\test_search_stream.py tests\test_search_unit_retrieval_service.py -q
.\.venv\Scripts\python.exe -m ruff check sag_api\services\retrieval_service.py tests\test_retrieval_relevance.py tests\test_search_strategy.py tests\test_search_stream.py tests\test_search_unit_retrieval_service.py
```

- Pytest: **93 passed** on the final run. The local virtualenv initially lacked
  `jieba-py`; the test environment was supplied the lockfile version `0.46.12`,
  with no repo dependency or lockfile change. The first passing run emitted one
  upstream `SyntaxWarning`; the final rerun was clean.
- Ruff: **passed**.
- `git diff --check`: passed.

## Gate result

Checkpoint P4.2 and the DATN-67 focused P4 code/review gate pass. This is not
production ACL acceptance: DATN-61 remains open, as do the broader real-corpus
retrieval, answerability, MMR/context, and ingestion/index acceptance items.
