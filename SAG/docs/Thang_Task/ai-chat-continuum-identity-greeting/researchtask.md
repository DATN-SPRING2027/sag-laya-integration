# AI Chat: Continuum AI identity and context-aware user greeting

## Scope and base

Thang requested replacing the AI Chat assistant identity `Zleap` with
`Continuum AI`, enabling the assistant to know the asking user's display name
(e.g., `KeyT` or Vietnamese full names) and naturally alternate between calling
the user by name in greetings/openings and using contextual pronouns (`bạn` /
`you`) in regular and technical turns.

Branches created from updated `origin/main`:

- `sag-laya-integration`: `feat/Thang-continuum-ai-identity-greeting-be-api`
  (from `073a916`)
- `DATN_BE`: `feat/Thang-continuum-ai-user-name-be-api` (from `e454b0a`)

Each repository deliverable remains independently backward- and
forward-compatible and ships via its own PR.

## Verified current flow and root causes

1. **Hardcoded default branding (`Zleap`)**:
   - `SAG/apps/api/sag_api/branding.py` defines `DEFAULT_AGENT_NAME = "Zleap"`.
   - `SAG/apps/web/lib/branding.ts` defines `DEFAULT_AGENT_NAME = "Zleap"`.
2. **Persisted default `Agent` row in PostgreSQL**:
   - `get_default_agent(session)` in `sag_api/services/agent_domain.py` creates
     an `Agent(is_default=True, name=DEFAULT_AGENT_NAME, avatar="AI", persona=_DEFAULT_PERSONA)`
     row on first access.
   - `_is_legacy_default_agent(agent)` previously only matched `agent.name == "sag"`
     and `agent.avatar in {"s", "S"}`, leaving existing `is_default=True` rows
     persisted as `"Zleap"` even if `DEFAULT_AGENT_NAME` changed.
   - Furthermore, `DATN_BE` caches `{ accessToken, agentId }` for up to 10
     minutes and calls `/ask` and `/regenerate` via `get_agent(...)` directly
     without re-calling `GET /api/v1/agents/default`.
3. **Prior-turn history echo**:
   - `_IDENTITY["en"]` in `sag_api/generation/prompt.py` only stated:
     `"Your name is {name}. Use this name when the user asks who you are."`
   - In an existing thread whose earlier assistant turn already introduced
     itself as `"Zleap"`, an LLM can echo `"Zleap"` from conversation history
     unless instructed to always identify as `Continuum AI` regardless of legacy
     assistant turns.
4. **Missing user display name in prompt assembly & ASCII header stripping**:
   - `DATN_BE` (`AiChatService.resolveSagSession` and `AiEngineAuthController`)
     authenticates `actor = this.auth.getCurrentIdentity(...)` and passes
     `X-Continuum-Name: sanitizeContinuumActorHeaderName(actor.name)` to
     `POST /api/v1/auth/continuum-login`.
   - `SAG` (`continuum_login` -> `get_or_create_continuum_user`) already persists
     and updates `User.name` in PostgreSQL.
   - However, `prepare_ask` (`agent_domain.py`), `prepare_regenerate`
     (`answer_actions_service.py`), and `chat_completions` (`openai.py`) did not
     pass `user.name` into `build_ask_context` / `build_agent_messages` /
     `_identity_prompt`.
   - Additionally, `X-Continuum-Name` is normalized to printable ASCII for HTTP
     header transport (`"KeyT"` stays `"KeyT"`, whereas `"Nguyễn Văn Thắng"`
     becomes `"Nguyen Van Thang"`). Sending an optional UTF-8 `displayName` in
     the JSON body of `POST /api/v1/auth/continuum-login` (falling back to
     `X-Continuum-Name`) preserves full Vietnamese diacritics without breaking
     header safety or cross-repo deploy ordering.

## Security, prompt-injection, and grounding boundaries

- User display names originate from profile metadata (`User.name`) and must
  never be treated as system instructions or factual document evidence.
- `sanitize_user_display_name(user_name)` in `sag_api/generation/prompt.py`:
  - Normalizes Unicode (`NFC`), strips control characters/newlines/tabs and
    structural/quote delimiters (`"`, `'`, `` ` ``, `<`, `>`, `[`, `]`, `{`,
    `}`, `\`), collapses whitespace, and bounds length (`<= 60` chars).
  - Excludes generic fallback placeholders (`"Continuum user"`, `"local user"`,
    `"anonymous"`, `"guest"`, `"user"`, `"unknown"`) and email-like strings
    containing `@` so the assistant never greets `"Xin chào Continuum user!"`.
- Grounded RAG answers must retain strict citation discipline (`[n]`): the
  prompt explicitly instructs natural, context-dependent addressing (greet by
  name on greetings/openings; use natural pronouns like `bạn` / `you` in normal
  and technical explanations rather than repeating the user's name in every
  sentence).
- No PostgreSQL or MongoDB schema/migration changes are required.

## File-by-file change plan

### `sag-laya-integration` (`feat/Thang-continuum-ai-identity-greeting-be-api`)

1. `SAG/apps/api/sag_api/branding.py` & `SAG/apps/web/lib/branding.ts`:
   - Update `DEFAULT_AGENT_NAME = "Continuum AI"`.
2. `SAG/apps/api/sag_api/generation/prompt.py` & `generation/__init__.py`:
   - Add `sanitize_user_display_name(user_name: str | None) -> str | None`.
   - Enhance `_identity_prompt(name, language, *, user_name=None)` so the
     assistant consistently identifies as `Continuum AI` (never `Zleap`, even if
     older turns in history used `Zleap`) and naturally addresses the user by
     their sanitized display name in greetings/openings while flexibly using
     `bạn` / `you` according to context.
   - Thread optional `user_name: str | None = None` through `build_messages` and
     `build_agent_messages`.
3. `SAG/apps/api/sag_api/services/agent_domain.py`:
   - Expand `_is_legacy_default_agent(agent)` to detect untouched default rows
     named `"sag"` or `"Zleap"` (case-insensitive) with default persona/avatar.
   - Auto-upgrade untouched legacy default agents in `get_default_agent` and
     `get_agent` (when session is clean), and normalize effective agent name in
     `build_ask_context`.
   - Accept optional `user_name: str | None = None` in `build_ask_context` and
     `prepare_ask` (resolving from `session.get(User, principal.subject)` when
     not passed explicitly).
4. `SAG/apps/api/sag_api/services/answer_actions_service.py`,
   `SAG/apps/api/sag_api/api/v1/agents.py`,
   `SAG/apps/api/sag_api/api/v1/openai.py`:
   - Pass authenticated `user.name` through `ask`, `regenerate_message`,
     `prepare_regenerate`, and `chat_completions`.
5. `SAG/apps/api/sag_api/api/v1/auth.py` & `SAG/apps/api/sag_api/schemas/auth.py`:
   - Accept optional JSON body `{ display_name / displayName }` on
     `POST /api/v1/auth/continuum-login` (NFC-normalized UTF-8, control chars
     stripped, `<= 120` chars), falling back cleanly to `X-Continuum-Name` when
     absent or empty.
6. `SAG/apps/api/tests/test_continuum_identity_and_greeting.py`:
   - Regression tests for default branding, DB legacy `"Zleap"` auto-upgrade on
     `get_default_agent` and `get_agent`, custom persona preservation, prompt
     identity & `user_name` sanitization (including `"KeyT"`, Vietnamese UTF-8,
     `"Continuum user"` exclusion, prompt-injection stripping), `/ask` +
     `/regenerate` user name propagation, and `continuum-login` UTF-8
     `displayName` + ASCII header fallback.

### `DATN_BE` (`feat/Thang-continuum-ai-user-name-be-api`)

1. `src/integrations/ai-engine/ai-chat.types.ts`:
   - Add `sanitizeContinuumActorDisplayName(rawName: string): string` preserving
     UTF-8 NFC letters/diacritics while stripping control characters and
     bounding length to 120 chars.
2. `src/integrations/ai-engine/ai-chat.service.ts` &
   `src/integrations/ai-engine/ai-engine-auth.controller.ts`:
   - Include UTF-8 `displayName` in `POST /api/v1/auth/continuum-login` JSON body
     (while keeping ASCII `X-Continuum-Name` header intact) and include
     `displayName` in `sessionCache` key.
3. `src/integrations/ai-engine/ai-chat.service.spec.ts` &
   `test/ai-chat-sag.e2e-spec.ts`:
   - Update and add unit/e2e assertions verifying both ASCII header and UTF-8
     `displayName` body propagation.

## Results

### Implemented fixes

- `SAG/apps/api/sag_api/branding.py` & `SAG/apps/web/lib/branding.ts`:
  updated `DEFAULT_AGENT_NAME` from `"Zleap"` to `"Continuum AI"`.
- `SAG/apps/api/sag_api/services/agent_domain.py`: expanded
  `_is_legacy_default_agent` and added `_upgrade_legacy_default_agent_if_needed`
  so untouched default `Agent` rows persisted as `"sag"` or `"Zleap"` upgrade
  idempotently to `"Continuum AI"` on both `get_default_agent` and `get_agent`
  (preserving any user-customized persona or name), and normalized effective
  agent name + resolved `user_name` in `build_ask_context` and `prepare_ask`.
- `SAG/apps/api/sag_api/generation/prompt.py` & `generation/__init__.py`: added
  `sanitize_user_display_name` (NFC normalization, control/delimiter stripping,
  60-char bound, placeholder/email exclusion) and updated `_identity_prompt`,
  `build_messages`, and `build_agent_messages` to enforce `Continuum AI`
  identity (ignoring prior `"Zleap"` turns in history) and natural,
  context-aware user addressing (`Xin chào <Name>! Tôi là Continuum AI...` on
  greetings/openings, flexible `bạn` / `you` in regular and technical turns).
- `SAG/apps/api/sag_api/services/agent_service.py`: expanded `_DIRECT_INTENT`
  (guarded by negative lookahead for document/project/system terms) to cover
  standalone and compound greetings, assistant identity questions (`bạn tên gì`),
  and current user display-name questions (`tên tôi/mình là gì`,
  `bạn có biết tên tôi không`) without falsely triggering `no_answer_text()`.
- `SAG/apps/api/sag_api/api/v1/agents.py`,
  `SAG/apps/api/sag_api/services/answer_actions_service.py`,
  `SAG/apps/api/sag_api/api/v1/openai.py`, and
  `SAG/apps/api/sag_api/api/v1/auth.py`: propagated `current_user.name` across
  `/ask`, `/regenerate`, and OpenAI completions, and added optional UTF-8
  `displayName` / `display_name` JSON body parsing in `/auth/continuum-login`
  with seamless fallback to `X-Continuum-Name`.
- `DATN_BE` (`feat/Thang-continuum-ai-user-name-be-api`): added
  `sanitizeContinuumActorDisplayName` and passed `{ displayName }` alongside
  `X-Continuum-Name` in `AiChatService` and `AiEngineAuthController`.

### Validation

- `SAG/apps/api`:
  - `pytest tests/test_continuum_identity_and_greeting.py`: **32 passed**.
  - `pytest tests/test_answer_actions_api.py tests/test_vietnamese_ai_chat_reliability.py`:
    **39 passed** (total **71 passed** across targeted suites; 280 passed in
    broader non-smoke suite under Python 3.11 venv).
  - `ruff check` on touched files: **All checks passed**.
- `DATN_BE`:
  - `npm run lint`: **0 errors**.
  - `npm test`: **205 suites passed (1,953 tests passed)**.
  - `npx jest --config ./test/jest-e2e.json test/ai-chat-sag.e2e-spec.ts`:
    **2 passed**.
  - `npm run build`: **passed**.
- Live BFF -> SAG smoke test (`127.0.0.1:3001` -> `127.0.0.1:8000`): verified
  live Vietnamese greeting (`Xin chào <Actor Name>! Tôi là Continuum AI...`)
  and compound identity + user-name question (`Tôi tên là Continuum AI. Về tên
  của bạn, tôi biết rằng bạn là <Actor Name>...`), then deleted the temporary
  thread (`HTTP 204`).

