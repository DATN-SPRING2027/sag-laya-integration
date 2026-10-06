# Implementation Plan — Checkpoint C failure and rollback acceptance

## Order

1. Add current-publisher rollback based on the persisted, checksummed prior manifest and its retained inactive Qdrant slot.
2. Invalidate the previous-slot pointer/version when staging begins to reuse that inactive slot, then reject failures after the atomic switch attempt without affecting the active snapshot.
3. Add fault-injection regressions: build/quality, manifest/checksum/count/ACL verification, partial Qdrant write, and PostgreSQL switch transaction failure.
4. Add publish/rollback parity and concurrent request snapshot tests, including slot-lease lifetime.
5. Update Checkpoint C rows and evidence in `SAG/tasks/todo.md` and this folder's `researchtask.md`.
6. Run focused tests, relevant API regression tests, changed-file lint/compile, and the code-review skill; fix findings and record exact results.

## Completion gate

- Each requested failure case preserves the last active tree and never serves a mixed request snapshot.
- Successful rollback proves PostgreSQL manifest/pointer/slot and Qdrant payload version agree.
- Documentation distinguishes SQLite/mock tests from live PostgreSQL/Qdrant acceptance.
- Existing unrelated untracked files remain untouched.

## Kết quả

- [x] Rollback verified từ retained manifest/Qdrant slot, switch PG pointer/status/epoch nguyên tử.
- [x] Thu hồi slot metadata trước khi tái sử dụng slot inactive; reject lỗi verify và switch.
- [x] Failure injection cho subtree build, quality, manifest/checksum/count, partial Qdrant batch và commit switch.
- [x] Publish parity, rollback parity, dense/sparse reads theo scope đã pin và giữ slot lease qua publish/rollback.
- [x] Cập nhật mọi hàng Phase 8 / Checkpoint C trong `SAG/tasks/todo.md` và thêm `evidence.md`.
- [x] Focused suite đạt 63 passed; lint/compile/build và diff hygiene đạt.
- [ ] Live PostgreSQL/Qdrant, multi-process locks/load, process crash và tích hợp producer DATN-58/59 chưa được nghiệm thu; không đánh dấu runtime `INCREMENTAL_READY`.
- [ ] Full API suite chưa hoàn tất sạch: full run bị dừng vì treo lâu; chẩn đoán `-x` gặp lỗi độc lập ở mock ACL cũ (`record_search_unit_scope` không nhận `query_strategy_plan`).
