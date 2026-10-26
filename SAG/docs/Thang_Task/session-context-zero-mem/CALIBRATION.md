# T4 — Calibration, budget và quyết định gate

Đo ngày 26–27/10/2026 (Asia/Bangkok). **Đã hoàn tất phép đo và khóa contract kiểm chứng; chưa cho rollout.** Profile được chọn là **balanced**. Lỗi chất lượng còn lại và license NER không được che bằng điểm retrieval hoặc trung bình toàn bộ.

## Kết quả chính

12 calibration cases: 10 normal và 2 full-session. Không chạy model/retrieval/answer trên holdout để chọn ngưỡng. 24 paired legacy replays và 48 candidate replays (hai lượt cùng câu hỏi), cùng Qwen/Qwen3-8B, temperature0.3, output4096, context128000, tools/agent/runtime/grounding như T1. Không mock provider để nhận là nghiệm thu tích hợp.

| Chỉ số | Paired legacy | Candidate balanced |
|---|---:|---:|
| Normal web-enabled, câu trả lời đúng sau review | 5/10 | 9/10, cả hai lượt |
| Long normal web-enabled (>=120 messages), câu đúng | 2/6 | 5/6, cả hai lượt |
| Long normal, input+output tokens mỗi lượt trên 6 ca | 82.550 | 19.173 / 19.239 |
| Giảm LLM tokens nhóm long normal | — | **76,77% / 76,69%** |
| Normal toàn bộ 10 ca, input+output tokens | 98.047 | 27.006 / 27.072 |
| Giảm LLM tokens normal toàn bộ | — | 72,46% / 72,39% |
| Normal candidate generation calls/query | — | 1; không có compression call |

Đây là kết quả **web-enabled** để tách khả năng dùng history khỏi document-only grounding. Với web-disabled, cả baseline và candidate đều có 12/12 no-answer trong mỗi lượt: pipeline hiện tại vẫn không chấp nhận session-only evidence làm grounding. Review cho normal chỉ tính đúng ca UNKNOWN (1/10); không coi no-answer ở câu có đáp án là thành công. Không bật web hoặc bypass policy trong production để che gap này.

Review thủ công từng output, có answer hash và rubric trong [semantic review](evidence/calibration.semantic-review.jsonl). Đây là một reviewer assistant, không blinded hoặc kiểm định độc lập. Literal matching và semantic correctness được báo riêng; corpus synthetic lặp, sample nhỏ, hai lượt cùng câu hỏi không phải 20 câu hỏi độc lập.

### Regression cần xử lý

`calibration-compression-middle`: raw gold evidence có trong context đã chọn nhưng cả hai output candidate chép filler không liên quan; baseline trả UNKNOWN. Binary correctness của cả hai là sai, nhưng hành vi candidate kém hơn abstention. Rubric bảo thủ `correct > abstention/incomplete > unrelated echo` ghi nhận **một case regression, lặp ở hai lượt**. Vì vậy gate **0 per-case quality regressions FAIL**, dù trung bình 50% →90%.

Không chạy lại câu trả lời này để chọn kết quả đẹp. T7 cần kiểm đóng gói/grounding/output với evidence session và giảm nhiễu; chưa xác minh nguyên nhân là riêng prompt hay model. Gate không được hạ để hợp thức hóa output hiện tại.

## Khóa profile trước answer replay

Sweep chỉ dùng gold để chấm **sau selection**, không đưa gold vào encoder/query/prompt. Chọn profile nhỏ nhất giữ đủ evidence normal, trước khi xem answer provider:

| Profile | Main/bridge/local | Messages/codepoints cap | Evidence normal | Evidence full diagnostic |
|---|---|---|---:|---:|
| compact | 2/1/1 | 8 /8.000 | 8/9 | 4/6 |
| **balanced** | **4/2/1** | **16 /16.000** | **9/9** | **6/6** |
| wide | 6/3/2 | 24 /24.000 | 9/9 | 6/6 |

UNKNOWN không có gold evidence nên không đưa vào mẫu số evidence. Compact thiếu chuỗi support của follow-up, không được chọn. Balanced: beam4/PPR20/rho0.6/damping0.6, hierarchy window4/episode4/local-radius1/gap1800s. Đây là cap evidence được chọn từ toàn lịch sử, không phải giữ 16 message cuối. Compression-middle vẫn `partial / selection_cap`; không sửa thành ready vì đã tìm thấy target gold.

## Thời gian, encoder và tiền

- Cold feature+build+query/selection: p50 **46,89s**, p95 **80,41s**, chưa cộng model load **14,70s**. Warm query có encode query mới: p50 **0,554s**, p95 **0,687s** trên 12 ca. Không coi là SLA production.
- Normal web-enabled replay p50: legacy5,98s, candidate6,32s; p95 legacy88,27s, candidate29,27s. Thứ tự baseline rồi candidate không random và số lượt khác nhau; không kết luận causal latency gain từ các percentile này.
- Cold context inference: 109 batch invocations cho encoder và 109 cho NER, mỗi loại131.064 unpadded input tokens. Run đầu gồm 48 query encodes bổ sung; resume có48 query encodes nữa, được ghi riêng trong [summary](evidence/calibration.summary.json).
- CPU4 threads, batch16, raw chunks384 codepoints. Optional exact chunk reuse chỉ trong một call, không xuyên scope. Ba ca dài có1419–1433 chunks nhưng chỉ201 chuỗi khác nhau; duplicate chunks vẫn được materialize đủ raw offsets/vector mean cho mọi message. Filler lặp làm cold cost thuận lợi hơn chat thật. Không bỏ filler/message hoặc tính encoder miễn phí.
- Không có đơn giá điện/phần cứng/depreciation để quy CPU thành tiền; **chưa chứng minh total-system monetary savings**. Không có durable production cache; warm là cùng snapshot trong RAM.

Giá tham chiếu chính chủ [SiliconFlow Qwen3-8B](https://www.siliconflow.com/models/qwen3-8b): USD0,06/M input và USD0,06/M output; host thực api.siliconflow.com. Theo **uncached public list**, 10 normal queries: legacy khoảng **$0,005883**, candidate khoảng **$0,001620–0,001624**. Đây không phải invoice; chưa xác minh account discounts/cached rates/SDK-internal retries. Giữ `monetary_cost=null` trong raw ledger.

Toàn experiment provider: **94 logical calls**,394.817 input +8.847 output tokens, gồm paid compression của attempt bị chặn sau đó. Preflight riêng1 call/15 input/22 output. Tổng reference khoảng **$0,02422**, không gồm CPU. Mọi call ghi nhận có usage và một observed deployment attempt; không suy số HTTP retry nội bộ SDK từ hook này.

## Ceilings đã khóa trước holdout

Machine-readable tại [CALIBRATION-POLICY.json](CALIBRATION-POLICY.json). Đây là contract cho bước triển khai/kiểm chứng tiếp theo, **chưa là production config đang áp dụng**.

| Luồng | Ceiling |
|---|---|
| Normal warm | Input8.192 + output reserve4.096 =12.288 model tokens;1 model attempt; SDK internal retries phải0; work16M; retrieval5s; request60s |
| Cold index job | Work32M;180s; hết budget thì checkpoint/indexing/partial, không nhận last-N là hoàn tất |
| Full-session để T8 kiểm chứng | Tối đa12 model attempts,262.144 total tokens,360s; provisional ceiling, hết thì partial |

Input max thực đo normal candidate5.681 tokens;1 generation/query. Warm lớn nhất0,687s; cold lớn nhất80,407s +load14,703s. Work32M lấy headroom trên max15.518.675 units của whole-case sweep (bao gồm nhiều profile/warm passes), không suy ra CPU SLA từ units. T6/T7/T8 phải enforce đúng counters/timeout/prompt fitting và kiểm lại trên workloads thật. Settings baseline retries2/timeout120s **không bị đổi** trong phép so công bằng này; policy mới chưa được coi đã enforce transport attempt cap.

Gates: raw evidence normal100%; không regression từng ca; cải thiện thông tin cũ; long-normal tokens giảm>=50%; unknown usage/failed replay không được pass cost;0 scope/provenance violations; full-session0 thiếu/sai quyết định; license rõ; holdout trước rollout. Gate không được relax sau khi xem holdout.

## Full-session và license

Web-enabled full diagnostic: legacy tìm đúng4/6 items, candidate6/6 trong cả hai lượt; review strict toàn list legacy0/2, candidate2/2. Đây vẫn là **normal selector trả lời hai câu full**, không phải T8 exhaustive traversal/synthesis; không chứng minh mọi message đã được tổng hợp. Có core-agent/tool rounds lên tới7 calls, tách khỏi single-call normal. T8/T9 và coverage gate còn lại.

License NER: [audit](NER-LICENSE-AUDIT.md) kiểm pinned card/tree/API/community, không tìm thấy grant/license fine-tune. Base XLM-R MIT không đủ để tự gán MIT cho Davlan fine-tune. **UNVERIFIED/NO-GO rollout** tới khi có grant đúng artifact hoặc thay model có license rõ rồi calibration lại. Kết quả quality tốt không thay thế gate này.

## Tính toàn vẹn và tái lập

Pilot serial/batch không reuse bị dừng ở profiling trước answer replay; giữ artifacts đã hoàn tất, không tính CPU phần bị dừng là0. Run v2 chạm reservation2M của suite ở11 replay cuối, trong khi mọi observed call đã trả usage. Sau khi phát hiện failed replay có thể bị tính thành “0 token savings”, regression test RED được thêm và metric sửa: failed/unpaired/unknown không có reduction hợp lệ.

Giữ report v2 nguyên trạng, không sử dụng comparisons của nó làm nghiệm thu. Resume đúng11 attempts chưa xong với cumulative cap128 calls/4M estimated tokens; seed lại94-call final ledger từ80 calls trước và reservation đã dùng, không reset tiền/call history. Cumulative replay-time budget vẫn1200s, trừ866,747s đã dùng; không tính khoảng dừng review là inference. **61 completed rows giữ nguyên toàn bộ nội dung/usage**, kể cả output xấu. Paid failed-attempt overhead3.737 tokens có trong tổng experiment, tách khỏi successful paired comparison.

Evidence track: summary, semantic review, license proof và policy; raw outputs/feature vectors nằm `.data/session-context/` ignore. Raw hashes/source versions/config/corpus và resume chain có trong summary. Post-check xác nhận hai report thật khớp profiles/hierarchy/extra-body/dependency versions/case IDs và tám source hashes đã có từ run đầu. Header v2 cũ chưa ghi hash của agent_domain/agent_service/llm/prompt; header cuối ghi đủ và các file này khớp Git `5db4464`, không có working diff. Đây là giới hạn audit lịch sử, không bổ sung ngược metadata vào raw report cũ.

Sau đo, thêm regression tests và siết resume cho lần chạy sau: bắt buộc cả hash generation/replay và dependency/case identities; ghi ledger/reservation sau mỗi replay trước replay tiếp theo; giữ thời gian/attempt bị loại qua nhiều lần resume; không cho ghi đè input report. Header v2 cũ thiếu bốn hash sẽ bị guard mới từ chối; không dùng nó làm input resume mới. Checkpoint giữa hai replay không chứng minh usage của request còn đang chạy khi process bị kill; phải đối soát phần đó trước khi tiếp tục, không coi là miễn phí. Những sửa guard này không chạy lại provider hoặc thay answers đã đo.

Lệnh từ `SAG/apps/api` (pinned local model cache/optional extra và Settings đã chuẩn bị):

```powershell
.venv/Scripts/python.exe scripts/calibrate_session_context.py --live --max-calls 128 --max-estimated-tokens 4000000 --output .data/session-context/t4-new-run.json
# Resume một run bị gián đoạn, ghi file mới; chỉ rerun các replay chưa completed:
.venv/Scripts/python.exe scripts/calibrate_session_context.py --live --resume --resume-provider-from .data/session-context/interrupted-run.json --output .data/session-context/resumed-new.json
.venv/Scripts/python.exe -m pytest -m "not ai_smoke" -q
```

Không `--live/--sweep` chỉ validate manifest; `--sweep` chỉ local model/corpus. Không đọc database/chat thật. Branch `feat/Thang-session-context-calibration-be-api`; prerequisites T1–T3b chưa có trên main lúc bắt đầu. Không schema/data migration; rollback code calibration/batching và documents riêng. Xem kết quả kiểm cuối ở [researchtask](researchtask.md).
