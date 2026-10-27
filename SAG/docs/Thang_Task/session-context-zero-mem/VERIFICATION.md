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
