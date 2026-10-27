# Knowledge usage and optional project quota

`tenant_id` comes from the verified Continuum organization scope. For Continuum
login, SAG sets it to `organization_id`; it is an authorization/data-isolation
boundary, not a provider quota or provider account identifier.

`SAG_KNOWLEDGE_DAILY_TOKENS=0` is the default: no daily E2 token cap. A positive
value, for example `100000`, enables a separate E2 reservation cap for each
`(tenant_id, project_id, UTC day)`. This deployment setting currently applies the
same policy independently to all projects; per-project subscription plans are
not implemented. Foundation extraction is not charged against this E2 cap.

`knowledge_queue_control.tokens_reserved` tracks conservative reservations, not
provider-reported usage. Project keys use
`project:{stable_id(tenant_id, project_id)}:{YYYY-MM-DD}`. Organization observation
keys use `tenant:{tenant_id}:{YYYY-MM-DD}` and never block work. Legacy keys from
the old organization-wide cap are retained for audit, not reused as project caps.
Retries reserve again because they can issue additional provider requests. A
budget-deferred job remains queued until the next UTC day; disabling the cap
releases those jobs on the next claim. Queue capacity and concurrency protection
remain in force independently of daily token policy.

## Provider usage is separate from reservations

SiliconFlow documents actual response usage:

- [Chat completions](https://docs.siliconflow.cn/docs/api/chat-completions-post):
  `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens`, and
  optional cached/reasoning token details.
- [Embeddings](https://docs.siliconflow.cn/docs/api/embeddings-post):
  `usage.prompt_tokens` and `usage.total_tokens`.

The generation client reads response usage into runtime usage events. The E2
HTTP path currently discards response usage after reading the generated content.
There is not yet a complete persistent per-request ledger covering generation,
legacy extraction, E2 and embeddings. Do not use reservation totals as actual
consumption or as a customer invoice.

A billing ledger must attribute each attempt to the authenticated organization
and project, provider/model, request/trace ID, operation, timestamp, status and
provider-reported usage. Missing usage after a timeout must stay unknown rather
than becoming zero. Daily organization/project reports can aggregate that ledger
without enforcing a cap. Enforcing subscription quotas is a separate opt-in
policy, not a side effect of collecting usage.

Changes to worker settings take effect at application startup. Existing running
workers keep their original policy; do not restart an active benchmark merely to
change its recorded configuration.
