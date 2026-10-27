# Bàn giao implementation T5–T9

Feature giữ `SAG_SESSION_CONTEXT_MODE=off`. Implementation và kết quả nghiệm thu là hai trạng thái riêng; quyết định gate cuối ở VERIFICATION.md/evidence/runtime-verification.summary.json. Không chạy migration/backfill dữ liệu thật, không push/merge/approve.

## Repository và nhánh

Chỉ `sag-laya-integration/SAG` thay đổi. Không sửa DATN_BE/DATN_FE hoặc ingestion/retrieval tài liệu.

- DB riêng: `feat/Thang-session-context-schema-db`, commit `74971ba`, từ main `7772270`. Hai bảng dẫn xuất, migration0007/up/down, schema tests và DBaudit.
- API: `feat/Thang-session-context-runtime-be-api`, có toàn bộ prerequisites T1–T4 + T6–T9 và merge prerequisite DB. Diff DB của API so với nhánh schema bằng0; sau DB được merge vào main, PR API tự loại các file schema chung.
- PR38 hiện vẫn là nhánh reader T1–T3b tại5db4464, chưa nhận các commit local. API branch mới đã chứa các commits đó. Con người cần chọn merge PR38 trước hoặc đánh dấu nó bị thay thế bởi PR API mới để tránh duy trì hai diff trùng; agent không đổi/merge/force-push PR38.

DB PR cần review/CI/merge trước API. Branch API mới tuân theo yêu cầu không reuse taskbranch cũ. Các nhánh persistence-db cũ chỉ là checkpoint local, nhánh schema-db mới là deliverable DB riêng.

## Lệnh cho Thang

Từ repo root sau khi kiểm tra báo cáo cuối:

```powershell
git push -u origin feat/Thang-session-context-schema-db
gh pr create --base main --head feat/Thang-session-context-schema-db --title "feat(db): add derived session context metadata" --body-file SAG/docs/Thang_Task/session-context-zero-mem/db/PR-BODY.md
# Sau khi DB đã được review/CI/merge, push nhánh API và tạo PR:
git push -u origin feat/Thang-session-context-runtime-be-api
gh pr create --base main --head feat/Thang-session-context-runtime-be-api --title "feat(sag): add bounded thread session context runtime" --body-file SAG/docs/Thang_Task/session-context-zero-mem/PR-BODY.md
```

Nếu Thang muốn gộp vào PR38, cần chủ động chọn cách reconcile nhánh/DB prerequisite; không gửi schema chung vào PR cũ khi chưa tách review DB.

## Tác động và rollback

Apply migration0007 sau0006 trước khi bật readers/jobs. Hai bảng cache/provenance có FKcascade nhưng không thay rawmessages/owner; xem PERSISTENCE. Rollback: flagOFF, dừngjobs/drainrequests, giữ raw và bảng ban đầu; chỉ chạy down sau khi tất cả readers dừng. Không lấy assistant legacy unknown làm authority khi rebuild.

Optional session-context extra cần weights BGE/NER/tokenizer pinned trong local cache; không tải lúc request, không commit weights. Publisher modellicense/attribution và giới hạn datasetrights tại MODEL-LICENSES. Missing models/tokenizer/migration failclosed; ON chỉ text. SHADOW không phát sinh thêm LLM trả phí ngoài legacy. Scope luôn từ signedserverprincipal/owner, không clientprojectoverride.

## Giới hạn nghiệm thu

Ngoài SQLite/PGWASM,5nativePostgreSQLtwo-connection tests đã kiểm sharedlease/publish/delete/revoke/migration; chưa chứng nhận tải triển khai đa tiến trình tùy ý. Native source/tool protocol doubles không chứng minh DataEngine/Qdrant quality. Browserfixture kiểm chứng SAGUI/API/provider thật, chưa chứng nhận luồng Continuum embed riêng. CPUindex tiền chưa định giá; unknownusage/requestfailed không được coi0chi phí hoặc costgatePASS. Fullcoveragevisited không là semanticrecall. Không bật rollout nếu các gate quality/fullsession/usage hoặc hạ tầng vẫn thiếu.


Lưu ý bootstrap hiện có của SAG: `core/db.py:init_db` gọi `Base.metadata.create_all` không có guard theo môi trường. Vì models mới được aggregate-import, khởi động binary mới có thể tạo hai bảng dẫn xuất ngay cả khi feature flag OFF. Task không khởi động binary mới trên DB thật. Với PostgreSQL deployment, phải apply0007 bằng quy trình DB riêng **trước khi khởi động code mới** để giữ schema/defaults/constraints theo migration; OFF chỉ tắt đường generation mới, không là guard DDL. Không sửa bootstrap legacy trong phạm vi này. Nếu startup đã tự tạo bảng thì không chạy SQLup mù quáng; kiểm schema thực tế và reconcile qua migration mới có review.

Nếu rollback cần giữ hai bảng ở trạng thái đã drop, phải rollback binary trước khi restart: binary mới vẫn có thể create_all tạo lại các bảng dẫn xuất rỗng. Raw chat không mất, nhưng provenance rỗng không được coi là authority.
