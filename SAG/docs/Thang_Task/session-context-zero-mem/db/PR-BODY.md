> Lịch sử PR DB riêng #39, đã được thay thế theo yêu cầu Thang bởi PR hợp nhất #40. Không tạo hoặc merge một PR DB khác từ mẫu này. Mô tả hiện hành: ../PR-BODY.md; hướng dẫn hiện hành: ../HANDOFF.md.

Thêm hai bảng dẫn xuất cho session context: index theo thread/owner/scope/model và provenance theo Message/hash/partition/Source/dependencies. Raw chat và authority owner không đổi. Migration0007 mới, không sửa migration cũ.

Validation:7 SQLiteFK testsPASS; targetedRuffPASS; actual up/down SQL đã chạy disposablePG18.6WASM/PGlite0.5.12. T9 sau đó xác minh nativePostgreSQL16.15 trong containerQA mới:5testsPASS20.20s, actual0007up/down/reapply giữrawchat và sharedrowlocks chặnconcurrentdelete/invalidate/revoke trongruntimelease; separatepublication khôngdeadlock. Chưa chứng nhận tải triển khai tùyý.

Database impact:apply0007 sau0006 trước enabledAPI/jobs; FKcascade chỉ bảng dẫn xuất. No production DB migrated. Configuration/security:no secrets/rawchats/weights; defaultunknownorigin không cấp authority.

Risk/rollback:runtime phải revalidate rawhash/scope; cachekhông là authority. Stopjobs/drainreaders+flagOFF trước down; downchỉdrop2derivedtables, rawchatgiữ. Metadata provenance mất không được rebuild bằng đoánquyền. Human review/CI/merge required.

Bootstrap impact: existing init_db create_all runs regardless of feature flag/environment. Apply0007 before starting the new binary; OFF is not a DDL guard. No real deployment/startup/migration was performed in this task. See PERSISTENCE.
