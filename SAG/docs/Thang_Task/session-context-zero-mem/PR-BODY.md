Bổ sung quản lý context cho từng thread chat mà vẫn giữ SAG retrieval tài liệu. ON đọc toàn lịch sử được phép, dùng graph/fusion và nguyên văn có provenance cho câu hỏi thường; chế độ full_session duyệt từng lô và tổng hợp với coverage ledger. Không còn giới hạn last-8/last-200 làm phạm vi có thể nhớ.

Phạm vi gồm: hash/scope/model-bound cache có checkpoint/CAS, kiểm quyền trước input/output, provenance bắc cầu, budget/tokenizer/provider retries0, native tool/cancel/SSE, authenticated backfill CLI mặc định dry-run và feature flag off|shadow|on (defaultoff). Không sửa frontend hoặc ingestion/retrieval tài liệu. Tài liệu implementation tại RUNTIME.md; kết quả T9 và gate tại VERIFICATION.md.

Validation:304 offline testsPASS,5nativePGopt-in testsSKIPPED,2live-smoketestsdeselected; targetedRuffPASS; dependencycheck159packagescompatible. FullRuff còn123 findings có từ code legacy (baseline124), không coi full lint làPASS. Actual SDKtransporttests, JWT/HTTP/SSE/SQLite lifecycle;5nativePostgreSQL16.15tests thực thi migrationup/down/reapply và rowlocks/publish trênhaiDBconnections, pg_blocking_pids xác nhậnwriterwait; browser SAGUI/API/provider thật đã kiểm partial/live/reload ở390/768/1280/1440, before/after evidence đính kèm. Provider calibration/holdout đo riêng trong báo cáo T9, không dùng mocks chứng minh chất lượng/chi phí.

DB impact:prerequisite nhánh feat/Thang-session-context-schema-db/74971ba phải review/CI/merge trước. API branch đã chứa cùng DB prerequisite; diff schema so với nhánh DB bằng0. Migration0007 chỉ hai bảng dẫn xuất, không thay raw chat/owner. Không migrate/backfill dữ liệu thật.

Configuration/security:defaultOFF; ON cần optional session-contextextra và pinned local encoder/NER/tokenizer, chỉ text. Scope từ principal ký thật/owner, không clientprojectgrant; unknownlegacyassistant excluded. NER publisher Apache2 grant ghi MODEL-LICENSES, trainingdata rights chưa xác minh độc lập; không redistributemodelweights. Không secrets/rawchats/weights committed.

Risk/rollback:không bật rollout nếu T9 gate chưaPASS. Tải triển khai đa tiến trình, productionDataEngine retrieval và Continuumembed riêng chưa chứng nhận; CPU chưa định giá, unknownusage khác0. RollbackflagOFF,stopjobs/drainrequests, giữrawchat; down0007chỉsaureadersdừng. Human review/CI/merge required; agent không push/merge/approve.

Bootstrap impact: existing init_db create_all runs regardless of feature flag/environment. Apply0007 before starting the new binary; OFF is not a DDL guard. No real deployment/startup/migration was performed in this task. See PERSISTENCE.

FinalT9:holdout48pairedreplays/67callsaccountingcomplete,candidate16/20normalcorrect0/4fullstrictPASS;longnormaltokens-73.81%/-77.99%nhưngqualityregressions/fullomissionsFAIL. KeepOFF, NO-GOrollout. Calibration24candidates17/24correctincl2executionfailure. VERIFICATION/rawsyntheticreports/semanticreviewsincludeallbadanswers.
