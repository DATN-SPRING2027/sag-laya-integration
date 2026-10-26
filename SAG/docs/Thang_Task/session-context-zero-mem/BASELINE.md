# T1: Corpus chuẩn và baseline session context

Ngày thực hiện: 2026-10-26. Đây là baseline của đường chat cũ, **chưa có selector Zero-Mem**.

## Phạm vi và tính tái lập

- Corpus: `SAG/apps/api/tests/fixtures/session_context.jsonl`, 24 ca synthetic; 12 calibration và 12 holdout, chưa chạy holdout. Mỗi split có 10 câu hỏi thông thường và 2 câu hỏi toàn-session.
- Mỗi ca chứa facts với vị trí/role/nguyên văn, câu hỏi, gold items, aliases và gold evidence quotes. Mở rộng filler xác định bằng case ID và thứ tự message, không dùng random hay LLM sinh dữ liệu.
- Trường hợp bao phủ: recent, within-window, quá 200 message, đổi quyết định, đại từ/câu tiếp nối, Việt–Anh, vượt compression threshold, câu không có câu trả lời, liệt kê toàn session và đề xuất chưa được user duyệt.
- Filler synthetic lặp có kiểm soát để tạo session dài. Holdout dùng cùng template với identifiers khác; đây là regression corpus, không phải benchmark độc lập có tính đại diện như dữ liệu triển khai thật.
- Gold chỉ ở evaluator; không truyền đáp án, aliases hoặc evidence labels vào prompt.
- Replay dùng SQLite in-memory riêng từng ca và các hàm `prepare_ask`, `generate_stream`, persist answer thật. Không mở database production, không đọc chat thật, không fake retrieval tài liệu rồi nhận là nghiệm thu tích hợp.
- Không có attached Sources/MCP trong corpus. Hai profile dùng registry công cụ hiện có; câu hỏi yêu cầu viết lại thông tin từ lịch sử và không tra Internet, đi qua quy tắc direct-response hiện tại. Kết quả không đại diện chi phí cloud routing/retrieval hoặc tool-heavy workloads.
- Agent fixture là named agent, `is_default=false`, persona rỗng; owner/principal là synthetic. Đây không phải login/authentication test hoặc đo toàn bộ default-agent deployment đang chạy.
- Chạy cả `web_enabled=false` (default AskRequest hiện có) và `true` trên cùng ca, báo cáo riêng. Không gộp chính sách grounding với khả năng phục hồi lịch sử thành một điểm duy nhất.
- Không hạ context window, output cap hay số message giữ lại để làm baseline xấu đi. Báo cáo lưu cấu hình hiện tại, source hashes, corpus/expansion hashes và versions.
- Cấu hình của lần đo lấy từ `Settings`/dotenv như ghi trong report. Harness không nạp overrides từ database production bằng `apply_startup_overrides`; không khẳng định cấu hình này trùng Settings UI của một server đang chạy. Lần đo triển khai thật cần ghim effective settings riêng và không xuất credentials.

## Lệnh chạy

Từ `SAG/apps/api`, dùng environment hiện có:

```powershell
.venv/Scripts/python.exe scripts/eval_session_context.py --output .data/session-context/corpus-manifest.json
.venv/Scripts/python.exe -m pytest tests/test_session_context_baseline.py -q
.venv/Scripts/python.exe scripts/eval_session_context.py --live --split calibration --profile both --max-calls 64 --max-seconds 1200 --output .data/session-context/baseline-calibration.json
```

Không có `--live` chỉ validate/ghi manifest và không gọi provider. Report/answers synthetic nằm trong `.data/` đã ignore. Chạy live được Thang cho phép cho T1; không tự coi đây là cho phép gọi chat thật hoặc làm backfill dữ liệu thật.

Chọn một ca để kiểm tra:

```powershell
.venv/Scripts/python.exe scripts/eval_session_context.py --live --case calibration-recent --profile web-enabled --max-calls 8 --max-seconds 120 --output .data/session-context/preflight-usage.json
```

CLI có thêm `--max-estimated-tokens`. Đây là giới hạn reservation từ heuristic của thí nghiệm, có reserve output/retries cấu hình; không phải hóa đơn thật hoặc ngân sách sản phẩm T4. Deadline/call cap hữu hạn, usage thật được lưu riêng. Giá trị CLI phải dương.

## Accounting và chỉ số

- `TrackedLLM` dùng riêng trong harness, kế thừa provider boundary cũ; quan sát cả compression non-stream trước runtime và generation stream.
- Tùy chọn mới `LLMClient(..., include_stream_usage=True)` chỉ yêu cầu stream trả usage. Mặc định False giữ request production như trước. Không đổi prompt, model, thuật toán nén hoặc selection.
- Provider trả usage cumulative: lấy snapshot cuối, không cộng lặp từng chunk. Missing input/output usage giữ null.
- Ledger ghi logical calls, stage, usage input/output/cache/reasoning khi có, observed deployment attempts/failures, provider TTFT, duration, error class; không ghi prompts/keys/full exceptions.
- Failure/retry có thể thiếu billed usage: `known_*` là phần biết được; total thành null khi accounting không đủ. Deployment hooks không chứng minh số HTTP retries bên trong SDK; báo giới hạn này rõ ràng. Không suy retries thiếu usage là miễn phí.
- Không suy tiền bằng giá mặc định LiteLLM cho tên model custom. `monetary_cost=null`, `price_status=unverified` tới khi xác minh rate đúng provider/model/account.
- `literal_item_recall` chỉ đo literal alias xuất hiện. `semantic_correctness=requires_review`: nhắc một mã trong câu phủ định hay đề xuất không tự chứng minh trả lời đúng. Cần đọc output và review theo gold.
- `gold_verbatim_in_initial_context` chỉ là bằng chứng nguyên văn còn trong context ban đầu, không phải semantic evidence recall. Phần summary có thể diễn đạt lại. Gold không có evidence không được tính là đã phục hồi bằng chứng.
- p50/p95 là số mô tả sample nhỏ. Chỉ một lượt mỗi ca/profile chưa phải cold/warm repeated benchmark hay SLA production.
- Số liệu runtime usage là phụ; ledger toàn request mới bao gồm compression. Chưa đo index/encoder cost vì selector mới chưa tồn tại.

## Kết quả

Đã hoàn tất 24/24 replay (12 calibration × 2 profile), đều có terminal `run.completed`, với model Qwen/Qwen3-8B qua SiliconFlow. Cấu hình: context 128000, max output 4096, retries 2, timeout mỗi call 120000 ms, history limit 200 và keep recent 8. Không đổi các tham số này để đo. Tổng thời gian replay khoảng 566,953 giây, chạy tuần tự một lần mỗi ca/profile.

Bằng chứng được track: [summary JSON](evidence/baseline-calibration.summary.json). Raw report và answers synthetic ở `SAG/apps/api/.data/session-context/baseline-calibration.json`; bản sửa metadata ở `baseline-calibration-reviewed.json` cùng thư mục. Các file raw được ignore, không đưa vào Git. Summary chứa hashes, config, từng replay, stage totals và lineage của sửa nhãn.

### Usage provider trả về

| Stage | Logical calls | Input tokens | Output tokens |
|---|---:|---:|---:|
| Compression trước runtime | 6 | 19.800 | 2.606 |
| Generation trong agent | 24 | 227.098 | 585 |
| **Tổng** | **30** | **246.898** | **3.191** |

Tổng input + output là **250.089 tokens**. Mọi logical call trong run này có usage và một observed deployment attempt, không có observed deployment failure. Đây là accounting usage do provider trả; giới hạn visibility HTTP retries bên trong SDK vẫn áp dụng. Không tuyên bố đã đối soát hóa đơn.

Compression tiêu thụ 22.406 tokens, khoảng 9% tổng tokens trong corpus này. Đây là chi phí baseline quan sát được, không phải cam kết selector mới sẽ giảm được tương ứng: selector còn chi phí encoder/index và có thể chọn lượng evidence khác.

**Chi phí tiền: chưa xác định.** Rate đúng model/provider/account chưa được xác minh; không ghi tiền bằng 0 hoặc dùng giá mặc định của tên model khác. Preflight/smoke riêng không gộp vào 30 calls của primary run; các report preflight được giữ riêng.

### Literal-item presence và latency

| Nhóm | Web enabled | Số ca | Mean literal-item recall | p50 end-to-end | p95 end-to-end |
|---|---|---:|---:|---:|---:|
| Thông thường | false | 10 | 0% | 12,937 s | 65,172 s |
| Thông thường | true | 10 | 50% | 14,438 s | 127,453 s |
| Toàn session | false | 2 | 0% | 33,454 s | 46,469 s |
| Toàn session | true | 2 | 66,67% | 23,375 s | 28,235 s |

Các tỷ lệ này **không phải semantic accuracy hoặc kết quả production**. Đặc biệt ca unknown ở profile false trả canonical no-answer thay vì literal UNKNOWN; đó là từ chối thiếu căn cứ nhưng grader literal vẫn ghi 0. Cần giữ cả output và trạng thái khi review, không đánh đồng 0 literal với mọi khía cạnh đều sai. p95 sample 2/10 ca chỉ mô tả run này, không phải SLA.

### Review output theo gold — profile web-enabled

Review trong task này do agent đọc answers synthetic và đối chiếu gold; chưa được Thang/human reviewer xác nhận. Không gọi thêm LLM judge.

| Ca | Quan sát |
|---|---|
| Recent | Trả đúng ORBIT-472 |
| Old within 120 messages | Evidence còn nguyên trong context ban đầu, nhưng model trả UNKNOWN |
| Outside 200-message window | Mã đầu session không còn trong context; output dùng nhầm ghi chú filler làm quyết định |
| Changed decision outside window | Cả quyết định cũ và thay đổi đều ngoài cửa sổ; trả UNKNOWN |
| Follow-up/local reference | Trả đúng phương án thứ hai ORBIT-862 |
| Mixed Việt–Anh | Trả đúng retention identifier ORBIT-472 |
| Compression early | Thông tin đầu vẫn được giữ, trả đúng ORBIT-472 |
| Compression middle | Mã ở giữa không còn trong context sau compression; trả UNKNOWN |
| Unknown | Trả UNKNOWN đúng yêu cầu |
| Full-session spread | Tìm được UI/DB, thiếu API đầu session: 2/3 gold items |
| Full-session compression | Tìm được API/DB, thiếu UI ở giữa: 2/3 gold items |
| Proposed, not approved | Lặp lại câu hỏi, không trả mã đã xác nhận |

Profile web-disabled có **12/12 `answer_status=no_answer`**, kể cả ca recent có gold trong context. Đối chiếu `agent_service.generate_stream` cho thấy đường knowledge-only bật local grounding và terminal gate yêu cầu bằng chứng tài liệu. Corpus không gắn Sources, nên kết quả bộc lộ ranh giới giữa session history và knowledge evidence hiện tại. Không sửa gate trong T1; T2 phải định nghĩa evidence session/provenance hợp lệ trước khi tích hợp selector, không chỉ bypass kiểm quyền.

### Hiệu chỉnh metadata sau review

Ca `proposed-not-approved` ban đầu được gắn nhầm `full_session`; câu hỏi thực tế chỉ hỏi một mã cụ thể. Đã sửa group về `normal` ở cả hai split và thêm regression test. Không đổi facts, messages, queries, aliases hoặc output provider; không gọi model lại. Hash expanded messages khớp trước/sau. Raw primary report giữ nguyên, report reviewed/summary ghi hash raw và các row được đổi group rồi tính lại summaries. Không dùng bảng n=9/3 trước hiệu chỉnh làm kết quả cuối.

### Kết luận cho bước tiếp theo

- Baseline xác nhận được mất thông tin vì load window và compression prefix; cũng có ca evidence còn trong context nhưng output không phục hồi đúng.
- T2 cần đồng thời hợp đồng evidence session và provenance/grounding. Chỉ thêm semantic search vào history chưa đủ giải quyết profile mặc định.
- Chưa có candidate Zero-Mem để so sánh, nên chưa chứng minh giảm chi phí hoặc đạt chất lượng v1. Chưa chốt ngưỡng T4.

## Kiểm tra và review

- TDD: tests thiếu implementation thất bại trước; tests opt-in stream usage thất bại trước khi thêm option; test sửa nhãn nhóm thất bại trước khi sửa corpus.
- Bộ tests chuyên biệt hiện 12 tests, gồm missing/malformed usage, failure/retry accounting, double-count stream, budget, early close/callback cleanup, no gold in prompt, history limit và corpus/gold validation.
- Ruff các file thay đổi: PASS. Ruff toàn repo: 124 lỗi ở 26 file, không file nào nằm trong phần sửa; bằng chứng local `lint-existing.json` cùng thư mục report. Không sửa lỗi ngoài scope.
- Review correctness/architecture/security/performance: production diff chỉ là opt-in stream usage; selection/auth/schema/dependencies không đổi; raw keys/prompts không đi vào ledger; resource caps hữu hạn; `.data` outputs không được stage.
- Kết quả full offline suite và final Git checks được ghi trong researchtask khi handoff.
- Full offline suite cuối: **43 passed, 1 deselected**, 14,87 giây; 12 tests chuyên biệt nằm trong số đó. Sáu source hashes và corpus hash đã được đối chiếu; hiệu chỉnh chỉ group metadata được xác minh bằng cách dựng lại byte corpus ban đầu (giữ line endings); 23 liên kết tương đối hợp lệ, `git diff --check` PASS.

## Ranh giới nghiệm thu

T1 dựng baseline, chưa chứng minh tiết kiệm của giải pháp mới. Chưa đặt ngưỡng T4, chưa đánh giá holdout và chưa xác minh production ACL/HTTP endpoint/deletion/concurrency. Những mục này thuộc các bước tiếp theo của [plan](tasks/plan.md).
