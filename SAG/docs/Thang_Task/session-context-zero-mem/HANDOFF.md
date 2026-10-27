# Bàn giao session context T1–T9 — PR hợp nhất

**PR duy nhất: [#40](https://github.com/DATN-SPRING2027/sag-laya-integration/pull/40), target `main`, draft.** Theo yêu cầu trực tiếp của Thang, PR này thay thế #38 và #39, chứa cả DB/schema và API/runtime. Quyết định này thay cho hướng dẫn tách DB/API trước đó.

Giữ `SAG_SESSION_CONTEXT_MODE=off`: T9 đã đo xong nhưng quality gate FAIL. Xem VERIFICATION.md; không coi một PR là chấp thuận merge/deploy/bật feature. Chưa migrate/backfill dữ liệu thật.

## Nhánh duy nhất để tiếp tục sửa

```powershell
git fetch origin
git switch feat/Thang-session-context-runtime-be-api
git pull --ff-only origin feat/Thang-session-context-runtime-be-api
# Sửa, kiểm chứng, commit rồi push vào cùng PR #40 theo ủy quyền của Thang.
```

Head #38 `5db4464` và #39 `74971ba` đều là ancestors của #40 trước hợp nhất `df0c904`; schema/migration39→40 có diff bằng0. Toàn bộ commit history được giữ, không cần cherry-pick/merge thêm hoặc force-push. Các nhánh reader/schema cũ chỉ giữ để tra cứu; không cần merge hai PR cũ và không tiếp tục phát triển ở đó.

Chỉ `sag-laya-integration/SAG` thay đổi, không sửa DATN_BE/DATN_FE hay ingestion/retrieval tài liệu. Chỉ cập nhật docs khi hợp nhất, production source và hash của bản holdout không đổi. Mô tả đầy đủ T1–T9, tests, DB/config/security/risks/rollback ở PR-BODY.md.

## Review và migration

Một PR bao gồm hai nhóm cần reviewer kiểm rõ: schema/model/migration0007 và API/runtime/provenance/budget. Không còn dependency merge #39 trước, nhưng khi triển khai vẫn phải apply migration trước startup code mới. Human review/CI/merge vẫn bắt buộc.

## Tác động và rollback

Apply migration0007 sau0006 trước khi bật readers/jobs. Hai bảng cache/provenance có FKcascade nhưng không thay rawmessages/owner; xem PERSISTENCE. Rollback: flagOFF, dừngjobs/drainrequests, giữ raw và bảng ban đầu; chỉ chạy down sau khi tất cả readers dừng. Không lấy assistant legacy unknown làm authority khi rebuild.

Optional session-context extra cần weights BGE/NER/tokenizer pinned trong local cache; không tải lúc request, không commit weights. Publisher modellicense/attribution và giới hạn datasetrights tại MODEL-LICENSES. Missing models/tokenizer/migration failclosed; ON chỉ text. SHADOW không phát sinh thêm LLM trả phí ngoài legacy. Scope luôn từ signedserverprincipal/owner, không clientprojectoverride.

## Giới hạn nghiệm thu

Ngoài SQLite/PGWASM,5nativePostgreSQLtwo-connection tests đã kiểm sharedlease/publish/delete/revoke/migration; chưa chứng nhận tải triển khai đa tiến trình tùy ý. Native source/tool protocol doubles không chứng minh DataEngine/Qdrant quality. Browserfixture kiểm chứng SAGUI/API/provider thật, chưa chứng nhận luồng Continuum embed riêng. CPUindex tiền chưa định giá; unknownusage/requestfailed không được coi0chi phí hoặc costgatePASS. Fullcoveragevisited không là semanticrecall. Không bật rollout nếu các gate quality/fullsession/usage hoặc hạ tầng vẫn thiếu.


Lưu ý bootstrap hiện có của SAG: `core/db.py:init_db` gọi `Base.metadata.create_all` không có guard theo môi trường. Vì models mới được aggregate-import, khởi động binary mới có thể tạo hai bảng dẫn xuất ngay cả khi feature flag OFF. Task không khởi động binary mới trên DB thật. Với PostgreSQL deployment, phải apply0007 bằng quy trình DB riêng **trước khi khởi động code mới** để giữ schema/defaults/constraints theo migration; OFF chỉ tắt đường generation mới, không là guard DDL. Không sửa bootstrap legacy trong phạm vi này. Nếu startup đã tự tạo bảng thì không chạy SQLup mù quáng; kiểm schema thực tế và reconcile qua migration mới có review.

Nếu rollback cần giữ hai bảng ở trạng thái đã drop, phải rollback binary trước khi restart: binary mới vẫn có thể create_all tạo lại các bảng dẫn xuất rỗng. Raw chat không mất, nhưng provenance rỗng không được coi là authority.
