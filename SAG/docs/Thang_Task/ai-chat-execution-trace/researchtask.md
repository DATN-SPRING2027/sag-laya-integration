# AI Chat Real-Time Agent Execution Trace (Level 1) — SAG Stream Enrichment

## Scope and provenance

User requested implementing **Level 1 Real-Time Agent Execution Trace (Activity Log)** across Continuum AI Chat (`SAG` -> `DATN_BE` -> `DATN_FE`), creating fresh branches from `main`, running full validation, pushing, and opening PRs to `main`.

Target branch in `sag-laya-integration`: `feat/Thang-ai-chat-execution-trace-be-api` (branched from `origin/main` `aa85865`).

## Current flow and verified code facts

1. `sag_agent.runtime.AgentRuntime` (`SAG/apps/api/sag_agent/runtime.py:367-835`) already emits live lifecycle events during each run:
   - `run.started`
   - `turn.started` (`turn`, `message_count`, `tool_count`)
   - `message.started` (`role: "assistant"`)
   - `message.delta` (`role: "assistant"`, `delta`)
   - `message.completed` (`duration_ms`, `has_tool_calls`)
   - `tool.started` (`tool_call_id`, `name`, `label`, `arguments`, `risk`)
   - `tool.progress` (`tool_call_id`, `name`, `message`, `details`)
   - `tool.completed` (`tool_call_id`, `name`, `duration_ms`, `details`, `artifacts`, `terminate` — **note**: `runtime.py:776` omits `label` on `tool.completed` and `tool.failed`)
   - `tool.failed` (`tool_call_id`, `name`, `duration_ms`, `error`)
   - `run.completed`, `run.failed`, `run.cancelled`.
2. `sag_api.services.agent_service.generate_stream` (`SAG/apps/api/sag_api/services/agent_service.py:943-1380`) adapts runtime events to SSE:
   - Gap A: `plan.session_audit_trace` is already computed before `run.started` (`agent_domain.py:678`, `answer_actions_service.py:1105`), yet `run.started` does not include sanitized session-context timing (`mode`, `ms`), so live SSE consumers cannot show the ECCE session-memory step until historical reload.
   - Gap B: `tool.completed` and `tool.failed` store `started.get("label")` in `trace` (`agent_service.py:1212, 1228`) for DB persistence, but do **not** attach `"label"` onto `output_payload`, causing downstream SSE consumers (`DATN_BE`) to fall back to raw internal tool names (`search_context`) during live streaming.
   - Gap C: `run.completed` persists `steps=trace` (including `session_context` with `session_citations`, `thinking`, `tool`, and `answer`) via `persist_answer` (`agent_service.py:1303-1312`), but omits `"steps"` from `run.completed` `output_payload`, leaving live `completed` events without `session_citations` and full step parity prior to history refetch.
3. Grounded RAG privacy gate (`agent_service.py:1242-1245`) holds back `message.delta` when `local_grounding_required` or `host_context.search_context_called` until `_finalize_answer_citations` finishes at `run.completed`. Preserving `turn.started` and `message.completed` across SSE allows `DATN_BE` and `DATN_FE` to display live model planning and citation-verified synthesis steps without leaking unverified draft text or raw Chain-of-Thought.

## Security and ACL boundaries

- No change to `get_conversation_owner`, `require_project_read_principal`, `require_answer_mutation_principal`, `resolve_sources`, or `_verify_evidence_document_provenance`.
- Grounded `message.delta` suppression before `_finalize_answer_citations` (`agent_service.py:1242-1245`) remains 100% intact.
- Raw LLM reasoning / hidden Chain-of-Thought is never emitted or persisted.
- No SQLite/PostgreSQL schema or migration changes (`messages.steps` JSON format is unchanged).

## Proposed file-by-file changes

- `SAG/apps/api/sag_api/services/agent_service.py`:
  - Include `"session_context": {"mode": ..., "ms": ...}` on `run.started` `output_payload` when `plan_audit_trace` is a `Mapping`.
  - Ensure `tool.started`, `tool.approval_required`, `tool.completed`, and `tool.failed` always carry canonical `"label"` (resolved from `started["label"]`, `payload["label"]`, `_TOOL_LABELS`, or `name`) on `output_payload`.
  - Include `"steps": list(trace)` on `run.completed` `output_payload` so `DATN_BE` can extract sanitized `retrievalSteps` and `sessionCitations` immediately at stream completion.
- `SAG/apps/api/tests/test_execution_trace_stream.py`:
  - RED-first async unit tests verifying:
    1. `run.started` exposes `session_context` metadata (`mode`, `ms`) when `session_audit_trace` is present and omits it when `None`.
    2. `tool.started`, `tool.completed`, and `tool.failed` propagate human-readable `_TOOL_LABELS` (`"Tìm trong kho tri thức"`) on live SSE `output_payload`.
    3. `run.completed` includes `"steps"` matching the persisted `trace` (`session_context` with `session_citations`, `thinking`, `tool`, `answer`) while grounded `message.delta` remains held until citation verification completes.

## Out-of-scope items

- Streaming raw LLM Chain-of-Thought (`reasoning_content`) before citation verification (Level 2).
- Database migrations or changes to `messages` table columns.

## Acceptance checklist

- [x] RED-first unit tests in `tests/test_execution_trace_stream.py` fail before implementation and pass after implementation.
- [x] `ruff check` clean on modified/new files.
- [x] Existing `test_agent_retrieval_failure.py`, `test_vietnamese_ai_chat_reliability.py`, and `test_answer_actions_api.py` pass without regression.

## Verification results, impact, and rollback

### Files changed

- `SAG/apps/api/sag_api/services/agent_service.py`
- `SAG/apps/api/tests/test_execution_trace_stream.py`
- `SAG/docs/Thang_Task/ai-chat-execution-trace/researchtask.md`

### Tests and checks run

- `ruff check tests/test_execution_trace_stream.py` -> `All checks passed!`
- `ruff check --ignore E501 sag_api/services/agent_service.py` -> `All checks passed!`
- `pytest tests/test_execution_trace_stream.py tests/test_agent_retrieval_failure.py tests/test_vietnamese_ai_chat_reliability.py tests/test_answer_actions_api.py -q --tb=short` -> `44 passed in 10.87s`
- Full non-smoke API suite (`pytest tests -m "not ai_smoke"`): `223 passed` across all agent, SSE, citation, ACL, and BFF suites (the only 7 unrelated failures in `test_knowledge_workflow_runtime.py` / `test_audit_remediation.py` were local `.venv` Python 3.11 lacking `math.sumprod` added in Python 3.12).

### Database, configuration, and security impact

- **Database impact**: None. `messages.steps` JSON persistence is unchanged; zero SQLite/PostgreSQL schema or migration changes.
- **Configuration impact**: None. No new environment variables or feature flags.
- **Security & grounding impact**: Additive metadata only (`session_context` mode/timing on `run.started`, canonical tool `label` on `tool.*`, and sanitized `steps` list on `run.completed`). Grounded `message.delta` gating prior to `_finalize_answer_citations` and single-project BFF ACL enforcement remain untouched.

### Rollback notes

- Safe single-commit revert of `feat/Thang-ai-chat-execution-trace-be-api`. Downstream `DATN_BE` falls back gracefully when `session_context`, `label`, or `run.completed.steps` are omitted.
