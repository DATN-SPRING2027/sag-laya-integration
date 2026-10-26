# Thread session context runtime contract

## T7 primitives

model.py supports the configured OpenAI-compatible Qwen/Qwen3-8B route; other model/tokenizer families fail explicitly until validated. Qwen tokenizer pinned atb968826d9c46dd6066d109eabc6255188de91218, local cache only; complete chat template+tool schemas+256 framing headroom. This is a conservative input bound checked against provider usage, not invoicing. BoundedLLM uses OpenAI SDK2.54 with SDK retries0 and owned httpx client follow_redirects=False; avoids LiteLLM's implicit alternating-message resend loop. OFF/stateless/default LLMClient transport unchanged. Extra dependency is optional and matches installed version, no new package installation required.

Actual SDK MockTransport tests cover one physical request on400/429/500 and307/308, no redirect/role rewrite/retry, deadline, unknown usage, stream closure/cancellation. On cancellation even a prior usage snapshot is incomplete: reserve remains unknown and further paid calls are blocked. Usage settles from last cumulative snapshot only. This validates SDK boundary, not provider billing internals.

packing.py wraps raw roles/spans as inert JSON, current query last once, system instructions separate; no historical assistant text becomes active assistant completion. Groups fitted atomically with model counter and actual tool schemas. Excess system/query raises; never truncates the question or silently keeps lastN. Provider returns JSON answer/supports with exact quotes from authorized IDs. Invented ID/quote or malformed JSON becomes UNKNOWN; quote membership is provenance validation, not proof of semantic entailment. Document numeric citations keep their existing independent gate.

## Additive thread API integration (T7/T8)

SAG_SESSION_CONTEXT_MODE=off|shadow|on, defaultoff. OFF keeps legacy, SHADOW can build/select local features but never adds answer/compression calls beyond the legacy request. ON uses validated session context; migrations0007 and pinned local models/tokenizer must exist. Input text only; image request under ON reports explicit unsupported mode. Named and default agent thread ask supported; stateless OpenAI endpoint remains unchanged.

Existing POST /api/v1/agents/{agent}/threads/{thread}/ask gains optional context_mode=normal|full_session. Omitted selects full only for explicit recognized whole-session phrases, otherwise normal; explicit enum overrides phrase routing. No extra endpoint/event type. This field adds no auth, Source scope, model or budget authority to the caller. Legacy clients keep their payload.

Existing terminal event payload gains optional session_context (mode,state,reason,eligible,visited,evidence refs/hash/offsets) and session_accounting (calls/usage/accounting completeness, no raw prompts/keys). Original output/citations/answer_status remain. Session quotes are not forged document citations; source/span provenance is separate metadata. Existing clients can ignore additive metadata; full partial output must include a visible natural-language prefix and missing-coverage counts, so clients do not need to interpret a new SSE event to see incompleteness. No claim of browser/FE-BE acceptance until exercised.

Runtime integration/coverage implementation and final results are recorded in researchtask and updated here when complete. Feature remains OFF; numeric thresholds unchanged from CALIBRATION-POLICY; new NER needs calibration and holdout before rollout. PostgreSQL shared row locks/fresh scope checks protect input/publication; SQLite/WASM tests alone cannot certify server multi-process behavior.
