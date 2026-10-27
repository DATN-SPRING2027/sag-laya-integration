# T9 — Kiểm chứng runtime session context

Ngày 2026-10-27. **NO-GO rollout: giữ SAG_SESSION_CONTEXT_MODE=off.** T5–T8 đã triển khai; T9 thực hiện đo chất lượng, chi phí và lifecycle. Việc code/test chạy được không đồng nghĩa đạt gate chất lượng. Báo cáo machine-readable ở [runtime-verification.summary.json](evidence/runtime-verification.summary.json). Ngưỡng T4 tại CALIBRATION-POLICY.json được giữ nguyên; đây là kết quả runtime mới, không ghi đè số đo T4/NER cũ.

## Bản đem đo

BGE-M3/NER OpenMed/tokenizer pinned theo MODEL-LICENSES/RUNTIME; source hashes của toàn bộ session-context modules, agent_domain/service và harness giữ trong từng report. Final candidate reference protocol hỗ trợ objectID/stringID, backend materialize quote nguyên văn đã kiểm quyền; JSON mode đã xác minh SDK/provider. Không thêm repair/retry LLM. Holdout chạy một lần sau khi freeze; không tune từ holdout.

Fixture là chat giả lập có producer provenance tin cậy, mỗi replay dùng DBSQLite riêng; không promote assistant legacy thật. Named agent, không Sources tài liệu; mỗi câu đo cả web-off và web-on. Đường candidate chạy actual prepare_ask/generate_stream + provider SDK; baseline chạy legacyOFF. Cold index đo riêng; profile thứ hai copy đúng cache của cùng rawfixture vào DB mới, không copy answer từ lượt trước. Baseline calibration v2 được giữ lại làm tham chiếu, tránh chọn lại kết quả baseline; log v2 thiếu một phần config snapshot nên không dùng riêng các cặp này để chứng nhận gate quality/cost. Holdout chạy mới cả legacy/candidate trong cùng process, cùng config và source hashes với final calibration. Failed/unknown pairs không được cost PASS.

## Calibration cuối

24 candidate replays:20normal,4full-session. Review độc lập theo gold/roles/facts: **17/24 đúng**,2 lỗi thực thi và5 sai semantic. Normal8/10 đúng ở mỗi web profile (16/20); full-session chỉ1/4 strictPASS. Có3 khẳng định sai rằng quyết định không tồn tại; không thấy đề xuất chưa duyệt bị coi là xác nhận. Exact semantic review và answer hashes ở [review](evidence/runtime-calibration.semantic-review.jsonl).

Các lỗi chính:

- old-within-window/web-on trảUNKNOWN dù evidence đủ.
- proposed-not-approved/web-off lặp lại câu hỏi thay vì đáp án; không phải promotion đề xuất assistant.
- compression-middle cả hai profile dựng index lạnh quá180s, trả TimeoutError; không cắt về lastN và không coi0modelcalls là tiết kiệm thành công.
- full240messages/web-on duyệt đủ nhưng map bỏ UI862, rồi khẳng định không có quyết định UI.
- fullcompression/web-off phủ180/180 vẫn khẳng định không có API/UI/DB; web-on partial93/180 vẫn có globalabsence claim sai. Coveragevisited không chứng minh semanticrecall.

Gate quality không đạt; full-session missing/wrongdecisions không đạt. Gate savings trên toàn bộ tập dài **không thểPASS** do có failedpairs/unknown baselineusage; không cherry-pick các cặp thành công để thay kết luận. CPU encoder/NER và storage chưa quy tiền, provider usage không phải hóa đơn. CLI checkpoint sau mỗi replay hoàn tất; các diagnostic runs bị dừng/crash có thể thiếu record của request đang bay trước checkpoint, nên không chứng nhận tổng spend hoặc crash-proof billing ledger từ chúng. Không diễn các khoảng thiếu đó thành0chi phí; final run được báo riêng.

## Kiểm chứng kỹ thuật

- Full offline:304PASS,5opt-inPGSKIPPED,2ai_smokeDESELECTED; finalrun97.66s.
- NativePostgreSQL16.15:5PASS18.08s. ContainerQA tmpfs/--rm riêng, localhost55439, không đụng DB SAG; actual0007up/down/reapply giữ rawchat. Runtime lease trên hai kết nối chặn delete/invalidate/revoke; pg_blocking_pids xác nhận writer đang chờ lock. Separateanswerpublication không selfdeadlock; sau lease mutation xong và revalidation từ chối evidence cũ. Container đã xóa sau test. Không suy thành chứng nhận mọi distributed load. Shared lease có thể làm delete/revoke chờ tới deadline của request (normal60s/full360s); cần cân nhắc độ trễ mutation khi triển khai.
- HTTP/SSE actual router + JWT/Project owner kiểm401/404/422, visiblepartial/nativecancel; modelboundary doubles được phân biệt với liveprovider.
- ActualSDK one-attempt/HTTP400/429/500/no307/308redirect, cumulative/unknown/cancelusage, tool-pressure atomicgroups/citation pruning/source revoke đều có regression tests.
- Browser SAG thật với API/DBfixture/provider thật: livepartial/reload giữ đúng, screenshots390/768/1280/1440,0console/exception/failedrequest/HTTPerror. Before/after tại [browser](evidence/browser/README.md). Không sửa FE files, không dùng mockHTTP để claim acceptance.
- TargetedRuff cho toàn bộ session modules/scripts/tests mới PASS; fullRuff còn123legacyfindings (baseline124), không coi full lint làPASS. uv pip check159packages compatible; compileallPASS.

## Giới hạn và vận hành

Không migrate/backfill dữ liệu thật, không bật feature, không push/merge/approve. Existing init_db auto-create_all có thể tạo tables khi chạy binary mới kể cả OFF; xem PERSISTENCE, apply migration trước startup theo luồng DB riêng. Optionalmodels/cache và migration0007 cần có trước ON. NERpublisher grant Apache2 đã xác minh; datasetrights chưa review độc lập, attribution ghi MODEL-LICENSES.

Các bước ngoài nghiệm thu fixture: workload/deployment nhiều worker, DataEngine/Qdrant thực và Continuumembed cần xác minh riêng. Ngay cả khi các bước đó qua, semanticfullgate và coldindexfailedcases trên đã đủ chặn rollout. Hướng xử lý tiếp phải dùng calibration/validation mới để cải thiện extractor/model hoặc đường coldindex; không chỉnh theo holdout đã mở.

Rollback:OFF,stopjobs/drainrequests, giữraw; down0007chỉsaureadersdừng. Schema/API branches và lệnh bàn giao ở [HANDOFF](HANDOFF.md).

## Holdout

Đã hoàn tất **48/48 paired replays**,24legacy và24candidate,67logicalproviderrequests; tất cả usage đã quan sát đầy đủ:694252input +4449outputtokens (cả hai lanes). Calibrationfinal36requests/445159input+1137output; không cộng chẩn đoán bị gián đoạn thành hóa đơn. Source/config/caps/models/dependencies của calibrationfinal/holdout và workingcode trùng nhau; không tune sau khi mở holdout. Rawsynthetic reports được lưu [calibration](evidence/t9-calibration-runtime-v4.json), [holdout](evidence/t9-holdout-runtime-v4.json), không có credentials/rawproductionchats.

| Đánh giá candidate | Calibration | Holdout |
|---|---:|---:|
| Normal đúng |16/20|16/20|
| Full-session strict đúng |1/4|0/4|
| Lỗi thực thi |2|0|
| Khẳng định sai rằng quyết định vắng mặt |3|4|

Holdout: fullwindow cảhaiwebprofile bỏUI862 dù240/240; fullcompressionpartial93/180 và180/180 trảthiếu/saiquyếtđịnh. Normalcompressionearly/web-on baselineđúng→candidateUNKNOWN là regression; cảearly/middleweb-on vẫngiữgold trongfittedprompt, providerrawexplicitUNKNOWN. Proposed-not-approved cảhai trảUNKNOWN dùuserconfirmed472có, khôngpromotionassistant862. [Semanticreview](evidence/runtime-holdout.semantic-review.jsonl) do assistantđánhgiá,22candidateschildreview và2proposalcuốiparentreview, khôngblindedhumanacceptance.

Bốn longnormalcases/profile (địnhnghĩatrướclàmessage_count>200 hoặcfiller_repeat>1), khôngchọnbỏbadanswers:

| Web | Legacy input+output | Candidate input+output | Giảm token |
|---|---:|---:|---:|
| Tắt |58594|15343|73,81%|
| Bật |60163|13244|77,99%|

Token gate riêngholdoutlongnormal vượt50%; **qualitygateFAIL nên khôngđủđểrollout hoặcclaim tiếtkiệm tổngtiền**. Fullholdout riêng:legacy6calls/57849tokens, candidate17calls/389164tokens, candidatekhôngđạtyêucầuchấtlượng. Khôngtrộnfullvànormalđểclaimcostsavings. Oldinformationrecall cải thiệnởoutsidewindow/changed/followup nhưngzero-regressiongatevẫnFAIL.

Kết luận:T9measurement/reviewđãthựchiện, **acceptanceFAIL/rolloutNO-GO**. Khôngđánhdấuproductcomplete; giữOFF, khôngrerunholdoutđểchọnđápántốt. Côngviệccảithiệnsemanticextractor/coldindexcầnvalidationmớiđộclập; holdoutnàyđãđượcđọckhôngcònlàtậpđểtuning.


## Nghiệm thu độc lập F2/F3 trên tập Validation mới (27/10/2026)

Sau khi đóng 4 finding `[P2]` mới (fallback `SHADOW` khi thiếu tokenizer cache, tự rebuild index khi sai chiều vector, durable reservation/in-flight checkpoint trước provider call và khóa `provider_route_sha256` khi resume calibration), đã chạy đợt nghiệm thu độc lập F2/F3 trên tập synthetic mới `tests/fixtures/session_context_validation_f2_f3.jsonl` (`split="validation"`, 12 cases: 10 `normal` gồm 5 `long-normal` + 2 `full_session`). Báo cáo chi tiết tại [f2-f3-independent-validation.summary.json](evidence/f2-f3-independent-validation.summary.json) và [f2-f3-independent-validation.semantic-review.jsonl](evidence/f2-f3-independent-validation.semantic-review.jsonl); raw reports tại [v1](evidence/f2-f3-independent-validation-runtime-v1.json) và [v2](evidence/f2-f3-independent-validation-runtime-v2.json).

- **F3 (`normal` mode — 20/20 replays): PASS trong phạm vi validation**:
  - `normal_raw_gold_evidence_recall = 1.0` (`20/20` giữ đủ gold trong cả `raw_selected_evidence` và `fitted_evidence_messages`).
  - Độ chính xác ngữ nghĩa: `runtime` đạt **20/20 (100%)** (`10/10` web-off, `10/10` web-on) so với `legacy` **6/20 (30%)** (`0/10` web-off, `6/10` web-on). Không còn hiện tượng trả `UNKNOWN` sai khi gold nằm trong fitted prompt (`0/20`), không promote đề xuất chưa duyệt của assistant (`0/20`), `0` per-case regression trên `normal`.
  - Cold-index: **10/10** normal cases đạt `ready` dưới trần `180s` (`v2`: `p50 = 45.86s`, `p95 = 59.65s`, `max = 61.95s`; `v1`: `p50 = 41.88s`, `p95 = 74.64s`, `max = 90.09s`, `0` timeout).
  - Giảm token trên 5 case `long-normal`: web-off giảm **87,37%** (`116.242` → `14.684`), web-on giảm **90,98%** (`154.212` → `13.912`), tổng hợp giảm **89,43%**.
- **F2 (`full_session` mode — 0/4 replays): FAIL**:
  - `val-full-session-decisions-vi` (cả 2 web profiles): map trích xuất đúng cả 4 quyết định có `supports` hợp lệ nhưng kèm 1 dòng ghi chú không có mã hợp đồng pháp lý bên thứ ba với `supports: []`; `_items()` tại `aggregate.py:77–78` ném `ValueError("invalid_map_support")` hủy toàn bộ batch (`visited=0/220`, `reason="map_not_ready"`, trả `UNKNOWN`), tạo ra 1 per-case regression so với `legacy` web-on (`2/4`).
  - `val-full-session-timeline-en` (cả 2 web profiles): `_batches()` tại `aggregate.py:107` gom `240` messages (`~29.874` input tokens) vào 1 call non-streaming trong khi `BudgetedLLM` (`model.py:92`) cắt `llm_timeout_ms <= 60000ms`, dẫn tới `APITimeoutError` ở `~61,3s–61,8s` (`status="run.failed"`, `accounting_complete=False`).
- **Kết luận**: Giữ `SAG_SESSION_CONTEXT_MODE=off`, **rollout NO-GO** cho tới khi sửa 2 bug `full_session` (`aggregate.py:77–78` và `model.py:92` / `aggregate.py:107`) và xác nhận lại trên tập validation mới.


## Đính chính số liệu Validation F2/F3 và Sửa lỗi F2 + Harness (27/10/2026)

1. **Đính chính & Bảo toàn nguyên trạng bằng chứng chẩn đoán (`v1`/`v2`)**:
   - Giữ nguyên toàn bộ file lịch sử (`f2-f3-independent-validation-runtime-v1.json`, `f2-f3-independent-validation-runtime-v2.json`, `f2-f3-independent-validation.semantic-review.jsonl`, `f2-f3-independent-validation.summary.json`, `session_context_validation_f2_f3.jsonl`).
   - Phát hành báo cáo đính chính độc lập tại [f2-f3-independent-validation.corrigendum.json](evidence/f2-f3-independent-validation.corrigendum.json) được sinh tự động bởi `SAG/apps/api/scripts/recompute_f2_f3_validation_metrics.py`:
     - **Nguyên nhân lệch `raw_gold_evidence_recall` (`0.0` vs `1.0`)**: `verify_session_runtime.py` lưu `raw_selected_evidence` với khóa `"text"`, nhưng script hậu xử lý một lần đọc `x.get("quote", "")` cho từng dòng trong `paired_results` và `semantic-review.jsonl` (ra `0.0`), trong khi gán cứng `"observed": 1.0` ở `gate_matrix`.
     - **Tái tính bằng hàm thuần nghiêm ngặt** (`eval_session_context.py::score_selected_evidence`, `score_fitted_evidence_messages`): Trên cả `20/20` lượt `normal` của `v2`, `recomputed_raw_gold_evidence_recall = 1.0` (`38/38` gold items khớp chính xác `(message_id, kind, quote)`), và `recomputed_pre_fit_evidence_messages_recall = 1.0` (`38/38`).
     - **Trạng thái `fitted_gold_evidence_recall` sau `memory.fit` đối với `v1`/`v2`**: Đánh dấu tường minh là **`NOT PROVEN`** cho đợt chạy lịch sử vì `v2` ghi nhận `plan.messages` trước khi `agent_service.generate_stream` gọi `memory.fit` và `SessionContext.note_dispatch`. Code hiện tại đã bổ sung ghi nhận `actual_fitted_evidence_messages` và `dispatched_prompt_calls` (kèm `messages_sha256`) ngay tại `BudgetedLLM._create_completion` → `SessionContext.note_dispatch`.
     - **Giới hạn nghiệm thu của đợt `v1`/`v2`**: Đợt chạy `v1` + `v2` chỉ có giá trị chẩn đoán (không phải nghiệm thu cuối) vì `v1` gặp `AttributeError`, `verify_session_runtime.py` thay đổi SHA-256 giữa `v1` và `v2`, tổng thời gian `v1 + v2` là `2.843,86s > 2.400s`, và tập `session_context_validation_f2_f3.jsonl` đã được mở để phân tích lỗi.
2. **Sửa lỗi production F2 (`F2-A`, `F2-B`) và củng cố Harness (Offline — 0 live provider calls)**:
   - **`F2-A` (`aggregate.py`)**: Lọc từng item trong `_items()`, giữ lại các item có `supports` hợp lệ trong batch và chỉ loại bỏ item không có support hợp lệ; khi có item bị loại (`not batch_clean`), đánh dấu `reason = "map_not_ready"`, chỉ ghi nhận `visited` cho các message thực sự được chứng thực (và không để overlap 1-message của batch kế tiếp đánh dấu nhầm các message chưa chứng thực), đảm bảo `complete = False` (`state = "partial"`).
   - **`F2-B` (`aggregate.py`)**: Giới hạn kích thước map batch nhiều tin nhắn (`len(ids) > 8`) ở mức `min(input_limit, 10240)` tokens để tránh gom ~29.874 tokens vào một call vượt timeout 60s; dành riêng 1 call cho bước `reduce` (`reserve_for_reduce = 1`) khi đã có `extracted` items và `max_calls > 1`; bắt `(APIError, httpx.HTTPError, TimeoutError, UpstreamError, ValueError)` ở map/reduce.
   - **Durability & Wall-time Ceiling (`session_context_accounting.py`, `session_context_calibration.py`, `calibrate_session_context.py`, `verify_session_runtime.py`, `eval_session_context.py`)**: Đồng bộ cơ chế checkpoint `in_flight_replay` + `in_flight` call (`flush` + `os.fsync` + atomic `replace`) sang cả đường calibration; giữ `input_tokens = None, output_tokens = None` khi stream bị hủy giữa chừng; áp dụng trần `ledger.deadline` xuyên suốt cold-index, `prepare_ask`, `replay_case` và `candidate`.
   - **Kết luận**: F2/F3 **chưa được tuyên bố PASS nghiệm thu semantic cuối** vì chưa chạy lại trên bộ validation kín mới; giữ nguyên `SAG_SESSION_CONTEXT_MODE=off` và **rollout NO-GO**.
