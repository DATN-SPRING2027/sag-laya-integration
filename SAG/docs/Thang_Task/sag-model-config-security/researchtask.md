# SAG shared model configuration authorization

## Research record

- Repository: `sag-laya-integration/SAG`
- Branch: `feat/Thang-sag-model-config-security-be-api`
- Base: `origin/main` at `12aaa5542cc691729a00539028bae1348fd0cc51`
- Status: implementation complete; branch is ready for independent review after
  the full-repository checks are recorded. No database or API contract change.

## Scope and evidence

Review the report that ordinary Continuum-authenticated SAG users can test a
caller-selected LLM endpoint with the application-wide key or change shared
model settings.

Verified source paths:

- `apps/api/sag_api/api/v1/system.py`: `PUT /system/model-config` writes the
  shared settings and applies them to the runtime; `POST /system/model-config/test`
  builds a draft client and invokes it; `POST /system/model-setup/302` writes
  shared model and embedding keys. Each route currently authenticates only a
  SAG `User`.
- `apps/api/sag_api/generation/llm.py`: completion sends `api_key` and
  `api_base` to LiteLLM.
- `apps/api/sag_api/db/models/user.py` and `core/deps.py`: SAG's user model and
  authentication dependency have no human role/permission field.
- `docs/security/principal-assertion-contract.md` and
  `docs/security/be-iam-integration-decision.md`: BE owns membership/roles; the
  current BE-BFF login flow does not pass a per-user operator assertion to SAG.
- `apps/api/sag_api/services/settings_service.py`: the MinerU shortcut currently
  rejects use rather than persisting, but it is a sibling configuration route.

Finding is confirmed as an authorization gap. Successful external impact still
depends on deployment configuration and outbound reachability. A trusted SAG
operator identity is not available in the current Continuum login contract.

## Proposed narrow mitigation

- Add an environment-backed comma-separated allowlist of SAG user IDs for
  shared model configuration operators.
- In `auth_mode=password` and `auth_mode=continuum`, deny model test and every
  shared model-setting mutation when the authenticated SAG user ID is not
  allowlisted; an empty list denies by default. Do not infer operator authority
  from organization, project, or service identity claims.
- Preserve local single-user setup behavior. Document how an operator obtains
  their SAG ID from the authenticated `/api/v1/auth/me` response and configures
  the allowlist.
- Forward the new environment setting through both Compose profiles and expose
  it in their example environment files.
- Cover the currently disabled MinerU shortcut with the same authorization
  dependency so enabling its service later cannot introduce a sibling bypass.

No model, API, database, or dependency changes are planned. Configuration adds
one non-secret environment variable and requires a deployment rollout in
Continuum mode.

## Verification plan

- Added API regression tests for allowlisted and ordinary users across the
  model-test, model-update, quick-setup, and MinerU shortcut routes.
- Focused API tests: **8 passed**. Ruff on changed Python files passed when
  excluding `E501` in `system.py`; that file already has four unrelated long
  lines. Full SAG API suite: **310 passed, 1 skipped, 7 failed** in graph and
  knowledge-runtime tests. Full repo Ruff check reports **133 findings** across
  unrelated existing files. These failures are recorded without changing
  out-of-scope files.
- Compose validation: both profiles render successfully when the required
  secret environment values are provided as validation-only placeholders.
- Final diff review confirms `.serena/` remains untracked and untouched.

## Open deployment evidence

The repository contains no evidence of the production SAG environment or its
egress controls. The route-level authorization gap is fixed independently of
those deployment conditions; do not claim a demonstrated production exploit.
Continuum and password deployments must set `SAG_MODEL_CONFIG_OPERATOR_IDS`
before their designated operator can use the shared model settings UI/API;
until then, those writes/tests return 403 by design.
