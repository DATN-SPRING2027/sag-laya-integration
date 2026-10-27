# Bằng chứng trình duyệt thật — partial session answer

2026-10-27, isolated Chrome headless profile; SAGweb `npm run dev` tại3002, actualFastAPI8019 với signed scoped session, DBSQLitefixture riêng và configured Qwen3-8B provider. Không mock/intercept HTTP hoặc sửa frontend. Fixture chỉ gồm thông tin giả BLUE-472/CSV và một assistant legacy unknown để buộc partial. Private token file đã xóa, browser/API/web processes do task tạo đã dừng.

Before: backend map/reduce2requests200OK và DBanswerpartial đúng, nhưng sanitized message.delta thiếuversion nên FEparseFrame báo missingrequiredfields; before-report visiblePartial=false, reloadtrue. before-1440.png ghi lỗi UI.

After: copy full native event envelope, giữ sanitized text/sequence; exactHTTP regression kiểmversion/type/runID/sequence/payload/timestamp/turn/wiretype. Browser rerun sau finalID-only+JSON protocol: UI live cảnhbáopartial và BLUE-472/CSV, reloadvẫnđúng;390/768/1280/1440screenshots. consoleErrors/exceptions/failedRequests/httpErrors đều0. Desktop/tablet/mobile ảnh đã xem; không thaylayout.

QA gồm3browserruns ×2providerrequests =6requests, cộng2JSONmodepreflightrequests ngoài pairedevaluationledger. Không có đủ aggregatebrowserusage đểđịnhgiátiền; khôngcoilà0. Bằngchứng này kiểmchứng SAGUIfixture, khôngchứngnhận Continuum embed riêng/nativePostgreSQLconcurrency/documentDataEngine quality.
