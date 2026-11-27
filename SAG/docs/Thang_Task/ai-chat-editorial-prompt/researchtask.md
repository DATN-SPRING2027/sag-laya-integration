# Research Task — Enterprise Editorial Formatting for AI Chat Prompts (`sag_api`)

- **Branch:** `feat/Thang-ai-chat-editorial-prompt-be-api` (from `origin/main` `dd9cc42`)
- **Owner:** Thang
- **Date:** 2026-11-27

## 1. Current Flow & Problem Statement
- Verified callers of answer generation prompts in `SAG/apps/api/sag_api/generation/prompt.py`:
  - `build_messages(...)` (direct search/QA synthesis) and `build_agent_messages(...)` (agent tool-calling runtime invoked by `sag_api/services/agent_service.py`) both assemble system instructions from `_GUIDANCE["en"]` via `_guidance_for_language(lang)`.
- Current `[Answer rules]` (`prompt.py:164-185`) enforces grounding, `[n]` citation fidelity, abstention, and LaTeX delimiters, but defines **no editorial formatting or anti-emoji policy**.
- As a result, LLMs frequently emit consumer-style decorative emojis (`✅`, `🚀`, `📌`, `💡`, `🔹`) in section headings (e.g., `### ✅ 1. Framework nền tảng: ...`), add a space before terminal punctuation after `[n]` (`[4] .`), over-bold whole clauses, and nest 3-level bullet lists instead of compact tables for structured architecture/stack answers.

## 2. Contracts, Security & ACL Boundaries
- **Unchanged:** Project/organization ACL enforcement, `knowledge_only` strict grounding, entailment verifier (`_verify_grounded_entailment`), citation rewrite/stripping scanner (`_scan_markdown_prose_segments`), SSE event schema, and database schema.
- **No DB / migration / env changes.**
- **Prompt budget impact:** Adding ~130 tokens of concise `[Formatting and tone]` guidance to `_GUIDANCE["en"]` has negligible budget impact and keeps all existing grounding/citation/language instructions intact.

## 3. Proposed File-by-File Changes
1. `SAG/apps/api/sag_api/generation/prompt.py`:
   - Add a concise `[Formatting and tone]` block inside `_GUIDANCE["en"]` forbidding decorative emojis in headings/bullets/tables (unless verbatim quoted or explicitly requested), encouraging an executive-first 1–2 sentence takeaway, recommending compact GFM Markdown tables for 3+ multi-attribute comparisons/tech stacks, restricting bolding to concise labels/entities, capping bullet nesting at 2 levels, using blockquotes (`>`) sparingly for caveats/gaps, and placing `[n]` directly before terminal punctuation with no trailing space (`[4].`).
2. `SAG/apps/api/tests/test_p1_runtime_llm_prompt.py`:
   - Add unit tests asserting both `build_messages` and `build_agent_messages` include the editorial formatting rules across `en` and `vi` while preserving abstention and grounding directives.

## 4. Test Matrix & Acceptance Checklist
- [x] `ruff check sag_api/generation/prompt.py tests/test_p1_runtime_llm_prompt.py` — **PASS** (`All checks passed!`).
- [x] `pytest tests/test_p1_runtime_llm_prompt.py tests/test_answer_actions_api.py tests/test_branch_lineage_api.py -q` — **PASS** (`40 passed`).
- [x] Live SAG API smoke test via Continuum FE AI Chat (`http://localhost:3000/ai-chat?projectId=all`) — **PASS**: real grounded Vietnamese answer streamed in 16s with zero decorative emojis (`✅`/`🚀`/`📌`/`💡`), clean structure, and 3 verified clickable document citations (`[1]`, `[2]`, `[3]`).

## 5. Actual Changed Files, Impact & Rollback
- **Changed files:**
  - `SAG/apps/api/sag_api/generation/prompt.py`
  - `SAG/apps/api/tests/test_p1_runtime_llm_prompt.py`
  - `SAG/docs/Thang_Task/ai-chat-editorial-prompt/researchtask.md`
- **Database / Configuration / Security impact:** None (prompt instruction update only; zero schema, env, ACL, or API contract changes).
- **Rollback:** Revert the single commit on `feat/Thang-ai-chat-editorial-prompt-be-api`.

## 6. Out-of-Scope
- Frontend Markdown rendering, emoji-stripping fallback for historical messages, citation hover tooltips, and source strip UI (tracked separately in `DATN_FE` branch `feat/Thang-ai-chat-editorial-ui-fe-ui`).
- Changing retrieval ranking, entailment thresholds, or API payloads.
