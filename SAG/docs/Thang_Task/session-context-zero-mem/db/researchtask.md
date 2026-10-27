# DB-only change audit

Fresh branch from origin/main 77722700fa8ed83112bff6640213b12d0235c11c after fetch and ff-only pull. This deliverable contains only new derived tables/models, migration0007/up/down, schema tests and this audit/persistence contract. No existing migration edited; no production DB touched.

Same schema as locally verified e30793e, separated from API prerequisites for independent PR/review. Raw messages and thread ownership are unchanged. FK cascades remove provenance with Message and both tables with Thread. Index cache is derived; runtime must revalidate raw rows and permissions before use. JSON does not authorize a caller. No credentials/chat/model weights committed.

Validation: 7 FK-enabled SQLite lifecycle/constraint/scope/model/rollback tests, plus actual PostgreSQL SQL up/down exercised earlier in disposable PGlite0.5.12 (PG18.6 WASM). Neither certifies real PostgreSQL server row-lock concurrency. T5 historical environment had no Docker daemon/native server. T9 later added actual PostgreSQL16.15 two-connection runtime lock/publication/migration tests in a separate disposable container; see ../VERIFICATION.md. Arbitrary deployment load remains unverified.

Migration impact: apply0007 after0006 before enabled API readers/jobs. Rollback requires flagOFF, jobs stopped and active requests drained, then0007_session_context.down.sql drops only the two derived tables. Raw chat survives; provenance cannot be reconstructed by granting unknown assistant content authority. Human review/CI/merge required. No push or merge performed by the agent.
