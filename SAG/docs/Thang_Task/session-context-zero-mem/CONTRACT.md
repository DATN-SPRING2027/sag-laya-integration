# T2 — Hợp đồng context của một session

Ngày 26/10/2026. Phạm vi: hợp đồng nội bộ có executable validators và tests; chưa nối vào ask, chưa thay retrieval, schema hay cấu hình production. Theo [SPEC](SPEC.md), [baseline](BASELINE.md) và [kế hoạch](tasks/plan.md).

## 1. Evidence là đoạn nguyên văn có nguồn gốc

| Kiểu | Hợp đồng |
|---|---|
| `SessionKey` | `owner_key + agent_id + thread_id`; không tìm sang thread khác |
| `TraceMessage` | Nội dung gốc, role, thời gian có timezone, status, dấu partition lúc ghi, trạng thái provenance, Source dependencies và message dependencies |
| `MessageReference` | ID message và SHA-256 của UTF-8 nguyên văn; thay nội dung làm reference cũ hết hiệu lực |
| `EvidenceSpan` | Reference và đoạn `[start,end)` theo Unicode codepoint Python; không phải byte hoặc UTF-16 offset của JavaScript; không normalize nội dung trước khi hash |
| `ResolvedEvidence` | Đoạn đã kiểm tra, loại phát biểu, tập Source và mọi message dependency bắc cầu, gồm cả message gốc |
| `SessionSnapshot` | Identity, scope fingerprint, generation và digest lịch sử |

`user_statement` chứng minh người dùng đã nói điều gì trong session. `assistant_statement` chứng minh assistant đã nói điều gì. Đề xuất của assistant không tự trở thành quyết định của người dùng; phát biểu trong chat không tự trở thành sự thật có chứng cứ tài liệu. Muốn kết luận user đã duyệt phải dẫn phát biểu xác nhận tương ứng. Đây là ranh giới loại evidence; T7 phải thực thi trong prompt/grounding/output, typed contract không tự giải quyết chất lượng suy luận.

Citation tài liệu của SAG giữ loại và cơ chế xác minh hiện tại. Không lấy session evidence để vượt document grounding gate. Baseline web-off trả no-answer khi chỉ có history vẫn là gap runtime chưa sửa ở T2.

## 2. Scope do server xác lập

Luồng adapter: lấy identity từ server đã xác thực → `get_thread()` kiểm owner/agent/thread → lấy toàn bộ ID Source phụ thuộc cần kiểm → `get_authorized_source_ids()` → giao với bộ lọc Source người dùng chọn → tạo scope. Không dùng số lượng kết quả search làm whitelist quyền đọc. Resolver lỗi phải trả lỗi, không biến thành thành công với tập rỗng.

Scope gồm issuer, subject, org, tenant, projects, partitions và identity session. `partition_fingerprint` gắn các trường này; `fingerprint` thêm whitelist Source hiệu lực và bộ lọc yêu cầu. Filter `None` nghĩa không thu hẹp thêm; tập rỗng nghĩa không cho dùng Source nào. Whitelist không được vượt filter. Token refresh cùng quyền không làm đổi fingerprint chỉ vì token ID/expiry đổi; request vẫn phải kiểm hạn principal mới.

Scope/DTO không phải credential. Caller phải dùng VerifiedPrincipal và owner key do dependency server tạo, không nhận bản scope/owner do client tự khai. Validator T2 không xác minh JWT và không thay auth middleware.

**Dấu partition phải có từ lúc ghi hoặc backfill có chứng cứ đáng tin.** Owner key hiện có không gắn project giống nhau cho mọi issuer, nên không đủ để suy ra scope gốc. Không gán project hiện tại vào message legacy khi đọc. Exact partition fingerprint là chính sách bảo thủ: tập quyền project/partition thay đổi làm dấu không khớp; việc hỗ trợ biến đổi quyền tinh hơn phải có thiết kế/migration riêng trước khi nới.

## 3. Provenance đóng bắc cầu

Message đủ điều kiện khi đúng session và partition stamp, status `ok`, provenance `recorded`, mọi Source còn được phép và mọi dependency message có reference hiện hành, đủ điều kiện tương tự. Thiếu/xóa dependency, hash sai, cycle hoặc một Source bị thu hồi làm toàn bộ các message phụ thuộc vào nó mất hiệu lực. Duyệt leaf-first không đệ quy; không có ngưỡng tuổi hay last-8/last-200.

`recorded` phải do producer đáng tin ghi khi biết đầy đủ inputs. Với assistant, dependency ledger phải bao gồm toàn bộ context/message/Source ảnh hưởng đến lượt sinh, kể cả tool và intermediate synthesis. Citations rỗng không chứng minh Source-free; unknown legacy không được nâng thành recorded dựa vào nội dung. User statement gốc có thể không có Source; user message có attachment/derived content vẫn cần ghi đúng dependencies nếu có.

T2 kiểm tính nhất quán của ledger được cung cấp, không chứng minh producer đã ghi đủ. T5/T6 phải lưu ledger và partition stamp, định nghĩa backfill chứng minh được; phần không chứng minh được phải unsupported/partial. Không xoá raw history để xử lý thiếu provenance.

## 4. Snapshot và chống dùng evidence cũ

Digest bao gồm mọi raw row của thread do trusted reader cung cấp, sắp theo UTC timestamp và ID; hash nội dung, role, status, timestamp, provenance, partition, Source và dependency references đều được ràng buộc. Scope/filter hoặc generation đổi cũng vô hiệu selection/cache cũ. Hash không phải chữ ký và không chứng minh reader đã đọc hết database.

T3 adapter phải đọc đầy đủ theo trang, không cắt ở 200; truyền generation từ store. Quy trình runtime sau này:

1. Xác thực principal/ownership; đọc metadata và resolve Source dependencies trước indexing/ranking/model use.
2. Chọn spans từ snapshot đó; giữ reference và dependency closure.
3. Ngay trước model input và trước phát output, lấy lại quyền/rows/generation hiện hành rồi `validate_selection()`; không tái sử dụng whitelist cũ để gọi là đã revalidate.
4. Nếu dữ liệu/quyền đổi, bỏ selection cũ; xử lý lại trong cùng budget hoặc trả partial/unsupported/error phù hợp.

T2 có hàm kiểm các đầu vào này; chưa có transaction, durable generation, cache eviction hoặc concurrency fence. T5/T6 phải xử lý race giữa kiểm tra và phát output. Không coi snapshot digest là bằng chứng đã giải quyết TOCTOU hay deletion production.

## 5. Coverage và trạng thái

`eligible_ids` do bộ lọc quyền/provenance xác định trên snapshot đầy đủ; `visited_ids` là các message đã thực sự xử lý trong tác vụ selection/synthesis, phải là tập con. Không để top-k tự đặt mẫu số. Mọi span trả về phải thuộc visited. Việc duyệt metadata để xác minh dependency không tự tính là đã đọc nội dung để tổng hợp.

| State | Ý nghĩa bắt buộc |
|---|---|
| `ready`, normal | Có thể dùng selection hợp lệ; không hứa đã tổng hợp mọi message |
| `ready`, full_session | Visited bằng eligible, snapshot hiện hành, không còn row thiếu provenance/partition stamp; vẫn chỉ bao phủ dữ liệu hợp lệ được phép đọc |
| `partial` | Chưa thể hoàn tất do budget, coverage hoặc provenance; có thể trả spans đã xác minh và lý do rõ ràng |
| `indexing` | Chưa có index đủ để phục vụ; không trả spans dùng như evidence |
| `unsupported` | Không đủ điều kiện xử lý; không trả spans dùng như evidence |

Non-ready bắt buộc có reason. Unknown legacy ngăn full-session ready, kể cả eligible đã duyệt hết. Dữ liệu bị thu hồi quyền không nằm trong câu trả lời; không tiết lộ nội dung/ID của phần không được phép. Traversal complete không đồng nghĩa semantic recall hoặc “model nhớ tuyệt đối”. Driver T8 phải ghi coverage qua từng lô và không đánh visited trước khi xử lý thành công; T2 không có model để tự xác nhận điều đó.

## 6. Budget hữu hạn dùng chung cả request

`BudgetLimits` bắt buộc truyền tường minh: context window, output reserve mỗi call, tổng calls, tổng input+output tokens, tổng index-work units và deadline theo monotonic clock. T2 không đặt default sản phẩm hoặc phần trăm tiết kiệm; T4 chốt số từ calibration.

- Reserve trước mỗi physical attempt, bao gồm retry, compression và synthesis. Mỗi reservation thuộc đúng một budget/request; không settle nhầm request hoặc hai lần.
- Input count phải bao gồm toàn prompt, context, tool schema/protocol và overhead phù hợp model. Input + output reserve phải vừa context window và tổng token còn lại. Provider max output phải khớp reserve. Cached input vẫn là token usage, không tự miễn phí.
- Settle bằng provider usage; ghi phần biết ngay cả khi vượt reserve, lỗi, hết giờ hoặc cancel. Vượt reserve/cap chặn công việc mới và giữ số thực đã dùng.
- Thiếu toàn bộ hoặc một phần usage: giữ reservation như khoản chưa xác định, remaining unknown, chặn gọi/duyệt thêm. Không coi failure/cancel là 0 tokens. `held_unknown_tokens` là khoản giữ bảo thủ, không cộng nó thành usage thực đã xác nhận.
- Index work và deadline cùng request; caller phải chia lô và kiểm budget trước từng đơn vị công việc. T3 phải định nghĩa work-unit cho đọc/build/graph, kể cả cold index; không chỉ đo LLM calls.
- Cancel ngăn công việc mới. Driver phải hủy I/O/model đang chạy và vẫn thu nhận usage nếu có. T2 chưa triển khai async cancellation, timeout vận chuyển hoặc quan sát retry nội bộ SDK.

`BudgetState` dùng một dispatcher trong một process, không phải distributed ledger. Token estimate là giới hạn lập kế hoạch; không thể cam kết hard billing cap khi tokenizer/transport retries chưa quan sát đầy đủ. Giá tiền/cached-rate/encoder cost phải ghi riêng theo cấu hình đã xác minh, không suy giá từ số tokens. Không reset budget khi retry hoặc đổi strategy để vượt cap.

## 7. Bàn giao cho T3 và tác động

Code tại `SAG/apps/api/sag_api/services/session_context/{types,scope,budget}.py`; tests tại `SAG/apps/api/tests/test_session_context_scope.py`. Không có caller production; không thêm dependencies, env, API/SSE fields hay DB schema/migration. Không gọi provider và không chạy backfill chat thật trong T2.

T3a dùng fixture adapter trước, đọc toàn lịch sử theo trang, tạo hierarchy có spans/hash ổn định và meter công việc. T3b chọn graph/closure nhưng giữ nguyên hợp đồng ACL/snapshot/coverage. Chưa được coi là production memory khi chưa có T4–T9 và persistence/lifecycle đã kiểm chứng.

Nhánh T2: `feat/Thang-session-context-contract-be-api`; phụ thuộc local commit T1 `f82eb78`, chưa có trên origin/main tại lúc bắt đầu. Review T2 delta so với T1; PR target main sẽ có cả T1 cho tới khi T1 được merge. Không tự push/merge. Rollback T2: revert riêng commit T2; không cần data rollback vì chưa có schema/runtime integration.

Kết quả kiểm tra và review cuối được ghi tại [researchtask](researchtask.md). Các hợp đồng này là căn cứ triển khai tiếp, chưa phải nghiệm thu chất lượng hoặc tiết kiệm của giải pháp hoàn chỉnh.
