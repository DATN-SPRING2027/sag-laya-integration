# Session context persistence (T5)

Hai bảng dẫn xuất mới nằm ở models/session_context.py; migrations/0007_session_context.sql và .down.sql. Raw messages/threads và owner authority không đổi. session_context_indexes tách theo thread+owner+scope+model; generation/snapshot digest/state/checkpoint/features JSON chỉ là cache dựng lại được. session_message_provenance ràng buộc Message FK cascade, raw hash, immutable partition, Source IDs và dependency hashes. Default origin unknown; không suy provenance từ citation rỗng.

Apply migration0007 sau0006 trước khi bật/session jobs. Không gọi create_all để upgrade database đang có. OFF giữ legacy, không cần đọc bảng mới. Rollback: tắt flag, dừng jobs/readers, chạy down; không mất raw chat. Mất bảng provenance làm assistant messages không có authority xuyên nguồn cho recall, phải dựng lại an toàn hoặc giữ unknown. Không tự chạy migration trên dữ liệu thật.

FK message delete cascade provenance, thread delete cascade cả hai bảng; message delete không tự sửa JSON features của index. T6 bắt buộc re-read raw snapshot, prune IDs/hash và fence publish; không được tin cache như nguồn dữ liệu. Unique constraint không thay concurrency CAS. JSON updates dùng reassignment để ORM ghi thay đổi. Không thêm PII category mới, namespace Qdrant hoặc cross-database FK.

Kiểm chứng: 7 disposable SQLite tests với FK enforcement, restart session/checkpoint, uniqueness/check constraints, message/thread cascade, model/scope isolation và rollback giữ raw messages. Full offline trước đó180 passed2deselected (gồm initial schema test và2 grounding characterization); focused mở rộng7passed1.40s. Ruff targeted PASS. SQL PostgreSQL up/down được chạy riêng bằng disposable PGlite; đây không chứng minh production PostgreSQL đa tiến trình/concurrency, môi trường không có daemon Docker/server PG. Xem researchtask cho bằng chứng cuối.

T6a đã có adapter keyset/checkpoint/CAS tại services/session_context/storage.py và index.py. Cached vectors chỉ dùng sau current raw hash/model/scope/mention-offset validation, row lookup luôn refresh ORM identity map. Request trước dispatch/output phải revalidate authority và generation; hooks/locks được hoàn tất ở T6b/T7, không suy rằng row-ready đã đủ production safety.

T9 bổ sung native PostgreSQL16 trong containerQA riêng khi Docker trở nên khả dụng: actual0007up/down/reapply giữrawchat; runtime sharedlease chốngconcurrentdelete/invalidate/revoke và separatepublication khôngdeadlock. Opt-in tests/test_session_context_postgres.py,5cases. DùnghaiDBconnections và DBtạmmới; khôngchứngnhận tảiđa tiến trình tùyý, khôngmigrateproduction. BằngchứngcuốiởVERIFICATION.md.


Lưu ý bootstrap hiện có của SAG: `core/db.py:init_db` gọi `Base.metadata.create_all` không có guard theo môi trường. Vì models mới được aggregate-import, khởi động binary mới có thể tạo hai bảng dẫn xuất ngay cả khi feature flag OFF. Task không khởi động binary mới trên DB thật. Với PostgreSQL deployment, phải apply0007 bằng quy trình DB riêng **trước khi khởi động code mới** để giữ schema/defaults/constraints theo migration; OFF chỉ tắt đường generation mới, không là guard DDL. Không sửa bootstrap legacy trong phạm vi này. Nếu startup đã tự tạo bảng thì không chạy SQLup mù quáng; kiểm schema thực tế và reconcile qua migration mới có review.

Nếu rollback cần giữ hai bảng ở trạng thái đã drop, phải rollback binary trước khi restart: binary mới vẫn có thể create_all tạo lại các bảng dẫn xuất rỗng. Raw chat không mất, nhưng provenance rỗng không được coi là authority.
