# Review độc lập PR #40 — 27/10/2026

**Merge: REQUEST CHANGES. Rollout: NO-GO. Giữ `SAG_SESSION_CONTEXT_MODE=off`.**

## A. Review snapshot

- PR: https://github.com/DATN-SPRING2027/sag-laya-integration/pull/40
- Repository: `F:/LEARN KÌ 8/ĐATN/sag-laya-integration`.
- Branch: `feat/Thang-session-context-runtime-be-api`.
- Head: `05b5b19c00822d124f351dc5019e64e0effe3605`.
- Base `main` và merge-base: `77722700fa8ed83112bff6640213b12d0235c11c`.
- Đã kiểm lại remote cuối review: cả hai SHA không đổi. GitHub hiện OPEN, `isDraft=false`, dù description/tài liệu còn gọi là Draft.
- Toàn diff base→head: 90 files, 30.429 insertions, 46 deletions; phần lớn số dòng là evidence JSON. Đã lập bản đồ toàn bộ changed files, đọc implementation session-context, integration changes, schema/up/down, harness/accounting và các tests liên quan đến invariants/failure paths. Không chỉ review commit cuối.
- Trace chính: HTTP/JWT/owner → prepare_ask → scope/provenance/storage → index/reader/selection → packing/provider/native tools → aggregate → capture/persist → authorized history reload.
- Đã đối chiếu SPEC, CONTRACT, numeric policy, PERSISTENCE, RUNTIME, MODEL-LICENSES, VERIFICATION, HANDOFF, research/task docs; đọc PR body. Không có GitHub comments/reviews tại snapshot.
- Không đánh giá độ tái lập paper; không khẳng định implementation tương đương phương pháp nghiên cứu.
- Không chạy provider/holdout mới, backfill hoặc DB ứng dụng; không đọc real `.env`/credentials/chat. Không sửa production code/test gốc; không push, merge, approve hoặc đăng GitHub review.
- Khi review, reproduction/audit nằm trong scratch được Git ignore, không có commit mới và `.serena/` được giữ nguyên. Sau khi Thang yêu cầu push lên PR40, báo cáo và scripts được đưa vào thư mục review này; snapshot runtime được review vẫn là SHA ở trên.

### Commands và kết quả thực chạy

| Kiểm tra | Kết quả |
|---|---|
| `git status --short`, `git branch --show-current`, `git remote -v`, branches/log/diff, `git merge-base`, `git ls-remote` | Head/base khớp GitHub; tracked files không đổi |
| `gh pr view 40 --repo DATN-SPRING2027/sag-laya-integration --json ...` | Đọc body, commits, comments/reviews, head/base và trạng thái |
| Pytest session tests, `-q -m "not ai_smoke"` | **273 passed, 5 skipped, 1 deselected**, 36,93s |
| Full pytest, `-q -m "not ai_smoke"` | **304 passed, 5 skipped, 2 deselected**, 46,59s |
| Native `tests/test_session_context_postgres.py` | **5 passed**, 6,16s, PostgreSQL **16.15** |
| `python run_repros.py --native-pg` | **4 failed**, 20,82s; assertions diễn đạt invariant cần đạt, thất bại xác nhận bốn lỗi bên dưới |
| `python audit_artifacts.py` | Source/corpus/review hashes và các số tổng bên dưới khớp |
| `python -m ruff check sag_api/services/session_context scripts/backfill_session_context.py scripts/verify_session_runtime.py` | PASS |
| `python -m ruff check sag_api sag_agent tests --output-format json` | FAIL, **123 diagnostics**; không báo full lint xanh |
| `git diff --check` | PASS |

Pytest được gọi qua Python wrapper chặn DotEnvSettingsSource đọc `.env` thật, ép global DB sang SQLite memory và tắt model/network opt-in. Full-suite wrapper vẫn cho phép test_config đọc `.env` synthetic trong tmp_path. Native PG chỉ chạy sau khi xác minh port55439 trống và tự tạo container `datn-pr40-review-20261027`, image `postgres:16`, user/DB `session_qa`, data tmpfs. Container có ID `6c723242fb6501aed0f466e9d1f2ce58c79b30e85527945ddce96d5564e98470`; đã stop/remove sau QA. Không dùng endpoint DB ứng dụng.

## B. Findings theo severity

### F1 — [P1] Full-session bypass gate của explicit @Source

- **Vị trí head:** `SAG/apps/api/sag_api/services/agent_service.py:1038–1039`; caller liên quan `:724`, `:752–753`, `:820`.
- **Trigger:** ON + `context_mode=full_session` + `source_ids` chứa Source được phép + lịch sử user source-free có evidence trả lời.
- **Actual:** `_generate_stream` bỏ resolve Sources và mọi tools cho full-session; final gate lại chấp nhận memory supports vì `memory.selection.mode == "full_session"`, kể cả đã có explicit Source filter. Reproduction trả hai quyết định từ chat với `answer_status=answered`, `citations=[]`, `tools=[]`.
- **Expected:** Source filter phải giữ hợp đồng yêu cầu evidence/citation từ Source đã chọn; không dùng session evidence để thay gate đó. Nếu tổ hợp này chưa hỗ trợ thì reject tường minh trước provider.
- **Hậu quả:** Người dùng nhận câu trả lời được đánh dấu answered ngoài phạm vi evidence họ đã chọn. Đây là bypass ranh giới grounding/filter; reproduction không chứng minh đọc chéo owner/project.
- **Root cause:** Nhánh full-session được miễn document gate, đồng thời tắt công cụ có thể đáp ứng gate.
- **Sửa nhỏ nhất:** Reject explicit Source + full-session trước dispatch cho tới khi hỗ trợ document retrieval; hoặc nối retrieval có scope và giữ cùng final citation gate. Không chỉ xóa điều kiện rồi vẫn gọi model vô ích.
- **Regression:** Test HTTP/native runtime với Source hợp lệ, memory hỗ trợ nhưng không có document support; không được answered. Kiểm thêm Source revoke/khác project.
- **Phân loại:** Lỗi mới do PR, phát hiện mới. **Chặn merge và rollout.**

### F2 — [P1] Full-session làm mất quyết định ở map và phát false absence qua reduce/fallback

- **Vị trí head:** `SAG/apps/api/sag_api/services/session_context/aggregate.py:168–169`, `:202`, `:233–237`; raw evidence `SAG/apps/api/tests/fixtures/session_context.jsonl:22–23` và `evidence/runtime-holdout.semantic-review.jsonl:19–22`.
- **Trigger:** Full-session câu hỏi liệt kê tất cả quyết định trên corpus đã lưu.
- **Trace window case:** User chốt UI `NEBULA-862` tại ordinal100, full evidence ID `S101`. Traversal240/240; map chỉ xuất API/S1 và DB/S239, mất UI ở tầng **map→intermediate**. Reducer không nhận item UI rồi tự khẳng định không có quyết định UI. Decoder chấp nhận support S1/S239 hợp lệ; visible answer giữ nguyên false absence. Đây không phải UI/fitter bỏ một UI item đã được map trích xuất.
- **Trace compression/web-on:** Map thực sự trích UI/S91 nhưng cũng sinh các global absence items từ filler. Reduce trả schema `items` thay `answer`, bị từ chối; fallback dòng202 nối nguyên những items mâu thuẫn, gồm cả UI đã chốt lẫn “không có API/UI/DB”. Prefix partial không sửa nội dung sai.
- **Expected/hậu quả:** Danh sách quyết định không được mất gold hoặc khẳng định không tồn tại trái raw evidence. Ready traversal không đồng nghĩa semantic completeness; reproduction này không dùng nhãn ready làm bằng chứng completeness.
- **Root cause:** Map extraction không bảo toàn semantic items; reduce chỉ kiểm membership support, không entailment/completeness; fallback không có cơ chế xử lý contradictions/global absence. Không quy toàn bộ lỗi cho retrieval hoặc model mà bỏ qua publication/fallback policy.
- **Hướng sửa:** Thiết kế extraction/absence/contradiction policy rõ, giữ typed intermediate và trạng thái chưa xác minh; fallback phải không biến absence của một batch thành kết luận toàn phiên. Kiểm bằng corpus validation mới, không viết regex theo NEBULA hoặc tune holdout đã mở.
- **Regression:** Provider doubles tái hiện empty/omitted map item, reducer bỏ support, schema sai và mâu thuẫn xuyên batch; sau đó semantic validation mới có gold độc lập.
- **Phân loại:** Lỗi chất lượng mới của feature, **known issue còn nguyên ở head**. **Chặn rollout**; không phải lý do duy nhất để chặn merge của code OFF.

### F3 — [P1] Normal abstain dù gold vẫn ở fitted prompt

- **Vị trí head:** `SAG/apps/api/sag_api/services/session_context/packing.py:9–20`; `evidence/runtime-holdout.semantic-review.jsonl:14,16`; `evidence/t9-holdout-runtime-v4.json:1891,2181`.
- **Trigger:** `holdout-compression-early`/`middle`, web-on.
- **Actual:** Final context evidence giữ đúng message ID, full span và raw hash của gold. Audit xác nhận early span[0,32), middle[0,33) khớp nguyên văn fixture. Raw provider đã trả `{"answer":"UNKNOWN","supports":[]}`; visible UNKNOWN là bản đúng của output đó. Early là per-case regression từ baseline trả đúng.
- **Expected/hậu quả:** Câu hỏi có evidence trực tiếp cần trả đúng, không abstain. Normal16/20 không đạt zero-regression gate; lỗi này không do decoder biến đáp án đúng thành UNKNOWN hoặc fitter cắt mất gold.
- **Root cause đã xác minh:** Sai ở generation với prompt/evidence hiện có; nguyên nhân sâu hơn giữa framing/model/distractors chưa được cô lập bằng thử nghiệm mới. Không khẳng định đổi một prompt chắc chắn sửa được.
- **Sửa tối thiểu có căn cứ:** Cô lập framing/extraction trên calibration/validation mới; giữ one-attempt/budget và đo semantic correctness trước khi chọn thay đổi. Không thêm paid retry để che regression.
- **Regression:** Doubles kiểm đường không làm mất gold/answer; validation model thật mới kiểm abstention. Unit test xanh không chứng minh model đã hết lỗi.
- **Phân loại:** **Known quality issue còn nguyên**, chặn rollout; không tự coi đây là merge blocker độc lập khi OFF.

### F4 — [P2] Thứ tự khóa ask/delete tạo deadlock PostgreSQL

- **Vị trí head:** `SAG/apps/api/sag_api/services/session_context/runtime.py:213–228`; phía writer `sag_api/services/agent_domain.py:761–764`.
- **Trigger:** Normal context lease đã lấy shared Message/provenance locks nhưng chưa lấy Index lock; concurrent delete đã chạy invalidate Index nhưng chưa xóa Message.
- **Actual:** Ask chờ Index đang bị writer giữ; writer chờ Message đang bị ask giữ. Native reproduction dùng đúng `context.lease` và `agent_domain.delete_message`, chỉ đồng bộ điểm interleave, nhận **SQLSTATE40P01**; một transaction bị rollback. Lần quan sát: reader hoàn tất, writer DBAPIError.
- **Expected/hậu quả:** Một thứ tự khóa thống nhất để delete/ask tiến triển; hiện thao tác xóa có thể thất bại khi chat đồng thời. Không có chứng cứ dữ liệu tái xuất sau một delete đã commit thành công.
- **Root cause:** Reader Message→Index, writer Index→Message; không có retry cho deadlock.
- **Sửa nhỏ nhất:** Lấy Index trước Message/provenance trong lease, đồng bộ thứ tự với invalidate/delete và các writer liên quan. Không giữ vòng khóa rồi chỉ tăng timeout.
- **Regression:** Giữ test nativePG interleave tại lúc acquire; cả writer/reader phải kết thúc đúng hợp đồng, không40P01; đọc lại raw/index sau delete. Năm test PG hiện có chỉ bắt mutation sau khi lease đã acquire đủ locks, nên không bắt race này.
- **Phân loại:** Lỗi mới do PR, phát hiện mới. **Chặn merge và rollout.**

### F5 — [P2] Provenance bỏ sót batch đã dispatch khi map thất bại

- **Vị trí head:** `SAG/apps/api/sag_api/services/session_context/runtime.py:99–100`, `:241–246`; boundary `aggregate.py:168–190`; callback `agent_service.py:1171–1174`.
- **Trigger:** Một map batch thành công; batch tiếp theo đã gửi model nhưng output malformed; runtime phát fallback từ các items đã validate.
- **Actual:** `note_dispatch` chụp `context.resolved` là visited của các batch trước, không phải evidence trong request hiện hành. Map lỗi không cập nhật visited. Synthetic reproduction gửi m0–m4 nhưng provenance answer chỉ giữ current,m0,m1,m2; thiếu m3,m4.
- **Expected/hậu quả:** Dispatch ledger phải là hợp của evidence thực sự đã gửi, tách khỏi coverage visited và final supports; nếu không thì downstream mutation/revoke validation thiếu dependencies theo hợp đồng producer. Reproduction chứng minh ledger thiếu, không chứng minh answer fallback đã tiết lộ dữ kiện từ discarded map.
- **Root cause:** Đồng nhất dispatched evidence với resolved/visited; normal fitter union không giải quyết boundary full-map này.
- **Sửa nhỏ nhất:** Ghi rõ tập evidence của từng map/reduce request ngay trước dispatch sau validation; capture hợp ledger đó dù output malformed/unknown. Không đánh visited sớm để sửa provenance.
- **Regression:** Malformed/failed second map, missing usage, cap sau dispatch; assert mọi original ref/source đã gửi đều nằm trong captured dependency ledger; visited vẫn chỉ gồm batch hợp lệ.
- **Phân loại:** Lỗi mới do PR, phát hiện mới. **Chặn merge và rollout.**

### F6 — [P2] Resume evaluation nhận config/caps mới nhưng giữ manifest cũ

- **Vị trí head:** `SAG/apps/api/scripts/verify_session_runtime.py:171–174`, `:216–222`.
- **Trigger:** Resume output đang thiếu replay; model/config/caps/corpus/dependencies đã thay đổi trong khi source manifest và split giữ nguyên.
- **Actual:** Chỉ source+split được kiểm tra; ledger dùng args mới, report vẫn giữ metadata cũ. Reproduction trên bản copy của report: candidate double nhận `SYNTHETIC-NEW-MODEL`, report vẫn ghi `Qwen/Qwen3-8B`; cap yêu cầu500 vẫn report180. Không phát sinh paid request, không sửa artifact gốc.
- **Expected/hậu quả:** Reject trước bất kỳ provider attempt nếu frozen inputs/caps không khớp; nếu không, kết quả có thể trộn điều kiện mà vẫn mang một manifest, không còn so sánh/evaluate đáng tin.
- **Root cause:** Resume validation không dùng toàn bộ manifest đã lưu; nguồn chi phối baseline prompt/expansion cũng chưa được hash đầy đủ trong manifest runtime này.
- **Sửa nhỏ nhất:** Đối chiếu corpus/config/provider-options/models/dependencies/case-set/variants/caps và toàn bộ source ảnh hưởng prompt; preserve cumulative reservations/elapsed. Đổi config phải tạo experiment mới.
- **Regression:** Thay từng trường phải reject, zero provider calls; unchanged resume giữ nguyên số calls/tokens/time đã tiêu.
- **Phân loại:** Lỗi mới ở harness, phát hiện mới. **Chặn handoff evaluation đáng tin và merge harness như hiện tại**; không tự suy rằng các report đã lưu bị trộn.

## C. Ma trận gate

| Gate | Verdict | Bằng chứng/giới hạn |
|---|---|---|
| Correctness | FAIL | F1 và F4; original suite chưa bao phủ hai đường lỗi |
| Authorization/provenance/privacy | FAIL | F1 filter/grounding, F5 dispatch ledger; tests cross-owner/scope/hash/revoke hiện có pass, chưa thấy cross-owner leak mới |
| Database/migration/rollback | PASS | Native migration up/down/reapply + FK/lifecycle tests giữ raw chat; chỉ chứng nhận disposable QA đã chạy |
| Concurrency/lifecycle | FAIL | Actual PostgreSQL40P01 tại acquisition race F4 |
| Budget/accounting | FAIL | Runtime reservation/no-retry/redirect/cumulative/unknown tests pass; experiment frozen caps/config không được giữ khi resume F6 |
| Compatibility/OFF | PASS | OFF/shadow/marker-history tests pass trong full suite; OFF **không** chặn bootstrap DDL |
| Normal quality | FAIL | Calibration16/20, holdout16/20; F3 |
| Full-session quality | FAIL | Calibration1/4, holdout0/4; F2 |
| Token/cost evidence | NOT VERIFIED | Token reduction đã tính lại đúng; chưa có money/rate/invoice/CPU-storage cost đầy đủ |
| Documentation/evaluation integrity | FAIL | Current stored manifests nhất quán nhưng resume integrity F6 sai; description Draft không khớp GitHub metadata |

### Đối chiếu artifact hiện tại

- Calibration và holdout cùng16source hashes; cả16 khớp working code. Corpus, config, caps, model pins, dependency_versions và provider-extra-body hash khớp nhau.
- Semantic reviews khớp raw-report hash và SHA256 của mọi completed answer. Failed calibration rows không có answer nên không so như completed answer.
- Calibration:24candidate rows,36calls,445159input+1137output; normal16/20,full1/4; hai TimeoutError ở compression-middle. Complete usage của36calls không có nghĩa24replays đều thành công.
- Holdout:48rows,67calls,694252input+4449output; mọi usage đầy đủ; normal16/20,full0/4;4falseabsence. Không có evidence trong audit này để kết luận cherry-pick/tune/rerun bad answers; cũng không chứng minh được tuyệt đối rằng không có requests ngoài report.
- Long-normal giữ đủ4cases/profile gồm bad answers: web-off58594→15343tokens (**73,81%**), web-on60163→13244 (**77,99%**).
- Full holdout báo riêng: legacy6calls/57849tokens; candidate17calls/389164tokens. Không dùng giảm tokens normal làm claim giảm tổng tiền.
- Alias scoring trong harness tự ghi requires_review; semantic labels là assistant review, không blinded human acceptance. Đã trace raw output cho F2/F3 thay vì chỉ tin alias/reviewer labels.

### Chưa kiểm chứng

Không chạy lại live provider/model quality; không chạy browser hoặc Continuum embed mới; không xác minh production DataEngine/Qdrant, multiworker load, billing retries bên provider hay training-data rights. Browser screenshots/reports là evidence lịch sử. Các shared locks có thể giữ mutation chờ đến deadline60s/360s. `asyncio.to_thread` không tự dừng native inference; chưa chứng nhận hard CPU-stop deadline của inference đang chạy. Những điểm này là giới hạn/rủi ro, không được thêm thành confirmed bugs chỉ vì chưa test.

`init_db` luôn create_all và aggregate-import models mới: startup binary mới có thể tạo hai bảng kể cả OFF. Apply0007 sau0006 **trước startup**; nếu đã create_all thì inspect/reconcile schema thay vì chạy up mù quáng. Down chỉ sau drain/stop readers. Muốn bảng tiếp tục absent sau restart phải rollback binary trước; không reconstruct assistant authority bằng suy đoán. Đây là tác động thật đã được docs công bố, không dùng OFF để miễn review DB.

## D. Kế hoạch fix ưu tiên

1. F1: Chốt và enforce tổ hợp Source/full-session trước model; regression API/native filter. Không cần provider trả phí để chứng minh gate.
2. F4/F5: Sửa lock order và ledger dispatch ở boundary chung; nativePG interleave và full-map failed-output tests. Không đánh visited trước thành công để vá ledger.
3. F6: Shared manifest/freeze validation trước paid work; test changed fields + cumulative resume. Không overwrite hai report đã lưu.
4. F2/F3: Giữ NO-GO; thử extraction/contradiction/absence policy và framing trên validation mới độc lập; nghiệm thu semantic zero-regression và full-decision gates riêng. Không tune hoặc chọn lại câu trả lời trên holdout đã mở.
5. Đóng cold-index/runtime/load và operational acceptance trước rollout, tách CPU/index, warm retrieval, answer và full synthesis costs. Không sửa unrelated legacy lint để mở rộng PR.

## E. Kết luận độc lập

- **Merge readiness: REQUEST CHANGES.** Bốn lỗi mới F1/F4/F5/F6 có caller path và reproduction; default OFF không thay thế việc sửa các hợp đồng này.
- **Rollout readiness: NO-GO.** Ngoài bốn lỗi mới, F2/F3 còn nguyên và quality gates thất bại. Giữ OFF.
- Điều kiện review lại merge: fix bốn lỗi mới trên cùng PR40, tests riêng chuyển xanh theo invariant, full suite và targeted lint pass; nativePG phải bao phủ acquisition race; report lại SHA mới và delta.
- Điều kiện review rollout: các quality gates định trước đạt trên validation mới độc lập, không có false absence/per-case regression, full-session item recall đạt, kiểm môi trường/migration/model-cache/license-attribution và hạ tầng triển khai cần thiết.
- Lượt review không sửa production/config/schema hoặc test gốc. Thang sau đó yêu cầu push kết quả lên cùng PR40; commit bàn giao chỉ chứa báo cáo, reproduction/audit và cập nhật research record. Đây không phải commit sửa bốn lỗi.

Lệnh tiếp theo an toàn để Thang tái hiện ba lỗi offline (nativePG mặc định SKIP, assertions hiện dự kiến FAIL):

```powershell
& 'F:/LEARN KÌ 8/ĐATN/sag-laya-integration/SAG/apps/api/.venv/Scripts/python.exe' 'F:/LEARN KÌ 8/ĐATN/sag-laya-integration/SAG/docs/Thang_Task/session-context-zero-mem/reviews/2026-10-27-independent/run_repros.py'
```

Không thêm `--native-pg` nếu chưa tự tạo và xác minh container/DB disposable riêng tại đúng endpoint của test. Test native có drop_all; container của lượt review này đã bị xóa.

## Bàn giao lên PR40

Thang yêu cầu push báo cáo ngày27/10/2026. Scripts được chuyển từ scratch sang thư mục này, sửa discovery repository để chạy ở checkout khác và format/lint trước commit. Reproductions nằm ngoài test suite mặc định; đây là các assertions dự kiến FAIL khi lỗi chưa sửa, không phải quality gate đã PASS. NativePG mặc định SKIP, chỉ opt-in sau khi kiểm chứng disposable DB. Không chạy lại paid provider/holdout để chuẩn bị commit bàn giao.

Kiểm tra gói bàn giao: Ruff cho ba scripts PASS; audit xác nhận16source hashes không đổi và calibration/holdout manifests khớp; runner offline tái hiện3FAIL/1SKIP. File `reproduction_cases.py` không dùng tên theo pattern discovery test mặc định; runner gọi tường minh file này. Không đưa các assertions cố ý đỏ vào test suite mặc định hoặc sửa assertions để làm kết quả xanh.

## Cập nhật Argon tại `227115c` (lịch sử, 27/10/2026)

- Đã sửa dứt điểm **F1**, **F4**, **F5**, **F6** (bao gồm vòng follow-up khóa `provider_route_sha256` ở `schema_version: 2`, reject thiếu/hỏng accounting khi resume và chặn chạy tiếp khi đã chạm ngưỡng budget), bỏ `try ... except TypeError` gây retry ngầm trong `index._encode` kèm cô lập `chunk_cache` theo `(model_id, text)`, nâng cấp bài kiểm tra PostgreSQL lock race tại boundary `Message` lock (`FROM messages ... FOR SHARE` + `pg_blocking_pids`), và xử lý phần lỗi deterministic orchestration/fallback + cold-index reuse của **F2/F3** trên nhánh `feat/Thang-session-context-runtime-be-api`.
- File `reproduction_cases.py` đã được cập nhật để kiểm chứng đầy đủ các invariant sau sửa:
  - Offline (`run_repros.py`): **4 passed, 1 skipped**.
  - Native PostgreSQL 16.15 disposable container (`run_repros.py --native-pg`): **5 passed** (bao gồm bài kiểm tra đua khóa `_lease` vs `delete_message` tại boundary `Message` lock với `pg_blocking_pids`, chứng minh 0 deadlock `40P01` trên code mới và bắt được `SQLSTATE 40P01` khi nạp `_lease` cũ `9c243af` trong bộ nhớ).
  - Full offline pytest suite (`pytest -q -m "not ai_smoke"`): **311 passed, 6 skipped, 2 deselected**.
  - Native PostgreSQL suite (`pytest tests/test_session_context_postgres.py -q`): **6 passed**.
- Các artifacts lịch sử `t9-calibration-runtime-v4.json`, `t9-holdout-runtime-v4.json` và semantic reviews được giữ nguyên không sửa đổi. `SAG_SESSION_CONTEXT_MODE` giữ nguyên `off`.
- **Merge readiness (code/contract/safety)**: **READY FOR REVIEW / MERGE** (với `SAG_SESSION_CONTEXT_MODE=off` và quy trình chạy migration `0007` trước khi khởi động binary mới trên PostgreSQL).
- **Rollout readiness (`SAG_SESSION_CONTEXT_MODE=on`)**: **NO-GO** cho tới khi có đợt đánh giá live provider mới trên tập validation độc lập chứng minh đạt các ngưỡng chất lượng của `CALIBRATION-POLICY.json`.

## Đính chính và sửa hai finding F6 còn lại (Codex, 27/10/2026)

Review lại `227115c` không chấp nhận tuyên bố F6 đã đóng trong mục lịch sử trên:
endpoint SDK từ `OPENAI_BASE_URL` chưa được khóa khi Settings để trống; reservation
4196 bị giảm thành 100 vẫn được chấp nhận và cho phép tổng thực tế 8392 vượt cap
6000. Hai finding này được sửa trong commit tiếp theo trên cùng PR40 theo yêu cầu Thang.

- Harness bắt buộc `SAG_LLM_BASE_URL` tường minh, không rỗng, trước work/report write.
  Endpoint được cấu hình trực tiếp ở SDK nên thay `OPENAI_BASE_URL` không đổi route;
  ứng dụng ngoài harness vẫn giữ nguyên Settings và behavior.
- `CallLedger.begin` giữ thêm `max_output_tokens`; schema manifest nâng lên 3.
  Resume bắt buộc input estimate, output reserve, retries và exact tổng reservation,
  kể cả unknown/failed calls; không refund output chưa dùng. Zero-call/zero-reserve
  hợp lệ vẫn chạy được. Report cũ/thiếu trường bị từ chối trước work/overwrite.
- Regression nằm trong suite mặc định ở
  `apps/api/tests/test_session_context_runtime_resume.py` (14 cases). RED trước sửa:
  **11 failed, 2 passed**; GREEN full suite cuối: **325 passed, 6 skipped,
  2 deselected**. Offline reproduction: **4 passed, 1 skipped**; targeted Ruff,
  compileall và dependency check (159 packages): PASS.
- Native PostgreSQL không chạy lại trong vòng này: Docker daemon không có pipe
  `dockerDesktopLinuxEngine`. Kết quả PG/mutation nêu trong mục lịch sử không được
  trình bày là kết quả mới. Không sửa lock code, DB models/migrations hoặc raw evidence.
- Self-review cho hai finding: **PASS, bàn giao review**; không approve/merge PR.
  Giữ feature OFF và rollout **NO-GO** do F2/F3 chưa có live acceptance độc lập.


## Sửa 4 Finding `[P2]` bổ sung và Nghiệm thu độc lập F2/F3 (Antigravity Argon, 27/10/2026)

1. **Đóng 4 finding `[P2]` mới trên `feat/Thang-session-context-runtime-be-api`**:
   - **SHADOW lỗi khi thiếu tokenizer cache** (`model.py:61`, `runtime.py:330`, `agent_domain.py:735`): Chuẩn hóa `(OSError, ImportError)` khi nạp tokenizer thành `ServiceUnavailableError("Mô hình context session chưa sẵn sàng trên máy chủ.")`; `SHADOW` fallback về legacy an toàn, `ON` trả `ServiceUnavailableError(code="session_context_not_ready")`, lỗi ACL/provenance (`ContextContractError`) vẫn fail-closed.
   - **Cache vector sai kích thước không tự rebuild** (`index.py:115`, `graph.py`): Kiểm tra chiều vector (`_encoder_dimension(encoder)`) khi tái sử dụng `row.features` trong `sync_index`; tự động loại bỏ vector lệch chiều và encode lại từ raw message.
   - **Crash giữa replay làm mất accounting và mở lại ngân sách khi resume** (`session_context_accounting.py`, `verify_session_runtime.py`): Ghi bền vững reservation và trạng thái `in_flight` (`status="started"`, `in_flight=True`, `in_flight_replay`) xuống đĩa trước mỗi provider dispatch (cả candidate `observed` và baseline `TrackedLLM`) và checkpoint ngay khi settle. `validate_runtime_resume` từ chối mọi report còn `in_flight_replay`, call `started`/`in_flight`, hoặc tổng call của `results` lệch `calls`.
   - **Calibration resume chưa khóa provider endpoint** (`session_context_calibration.py:66`, `calibrate_session_context.py`): Bổ sung và bắt buộc khớp `provider_route_sha256` trong `validate_resume`.
   - Kiểm chứng offline: RED `7 failed, 61 passed` → GREEN `68 passed`; full offline suite `333 passed, 6 skipped, 2 deselected`; `run_repros.py` `5 passed, 1 skipped`.
2. **Nghiệm thu độc lập F2/F3 trên corpus synthetic mới (`session_context_validation_f2_f3.jsonl`)**:
   - **F3 (`normal` mode — 20/20 replays): PASS trong phạm vi validation** (`20/20` đúng ngữ nghĩa so với legacy `6/20`, `normal_raw_gold_evidence_recall = 1.0`, `0` false `UNKNOWN`, `0` proposal promotion, `0` normal regression; cold-index `10/10 ready`, `max = 61,95s` ở `v2` và `90,09s` ở `v1` < `180s`; giảm `89,43%` tổng token trên 5 case `long-normal`).
   - **F2 (`full_session` mode — 0/4 replays): FAIL** do 2 bug production mới phát hiện và được giữ nguyên bằng chứng không tự sửa:
     - `aggregate.py:77–78, 191–193`: `_items()` ném `ValueError("invalid_map_support")` khi map trả kèm 1 item ghi chú vắng mặt có `supports: []`, hủy toàn bộ 4 quyết định hợp lệ đã trích xuất trong cùng batch (`val-full-session-decisions-vi`, `visited=0/220`, trả `UNKNOWN`).
     - `model.py:92` & `aggregate.py:107`: `_batches()` đóng gói `~29.874` input tokens (`input_limit=30000`) vào một lệnh `llm.complete` trong khi `BudgetedLLM` giới hạn `llm_timeout_ms <= 60000ms`, gây `APITimeoutError` ở `~61,3s–61,8s` (`val-full-session-timeline-en`, `status="run.failed"`).
   - **Kết luận**: Merge cho phần code/hợp đồng khi `SAG_SESSION_CONTEXT_MODE=off` sẵn sàng review; **Rollout `SAG_SESSION_CONTEXT_MODE=on` giữ nguyên `NO-GO`**.
