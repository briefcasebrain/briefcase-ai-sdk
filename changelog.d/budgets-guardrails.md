### Added
- Budgets (`briefcase.budgets`): hard and soft spending limits on LLM calls, in USD and/or tokens, per
  app, per model, or per tenant/user/session/key (`budgets.budget_scope(tenant="acme")`), over calendar
  UTC windows (hour, day, month) or a total. Each call reserves its maximum cost before it is sent
  (input estimate + output cap, at the packaged prices) and settles to the provider's reported usage
  afterwards; unknown usage keeps the reservation (`cost_source: "reserved"`). `on_exceed` = `block`
  (`BudgetExceeded` before sending), `warn`, or `degrade` (switch to a cheaper model). Unpriced models:
  `unpriced = "warn"` (default) / `"allow"` / `"block"`.
- Budget stores: in-process (default), Redis (atomic Lua reserve/settle) and SQL (SQLite, PostgreSQL,
  MySQL; conditional `UPDATE`, retried on deadlocks), tested against real Redis 7, PostgreSQL 16 and
  MySQL 8 servers including two processes.
- `briefcase budget set|unset|show|status|test` and `~/.briefcase/budgets.toml`
  (`$BRIEFCASE_BUDGETS_FILE`); a `briefcase status` row that turns to attention above 80%.
- Content guardrails (`briefcase.guardrails.content`): PII/secret checks (native Sanitizer when
  importable plus pure-Python detectors for `password=`/`secret=` assignments, `Authorization`/`Bearer`
  credentials, PEM/OpenSSH/PGP private keys, international and national phone numbers),
  prompt-injection heuristics on tool and retrieved content (`content.retrieved(text)`, pluggable
  `classifier=`), and output policies (regex, JSON schema, callable). Effects `block` / `redact` /
  `warn`; redaction keeps the SDK object's type; `block` checks fail closed when the checker raises.
  Streams: final-text checks, optional per-chunk early stop.
- `briefcase guard test|show|init` and `~/.briefcase/guard.toml` (`$BRIEFCASE_GUARD_FILE`); a
  `briefcase status` row.
- Call hooks for provider auto-instrumentation (`briefcase.integrations.providers._base.add_call_hook`):
  `before(ctx)` / `after(ctx, outcome)` around every instrumented openai, anthropic, google-genai and
  Bedrock call, including streams; nothing runs when no hook is registered.
- Provider records gain optional fields `budget_effect`, `budget_scope`, `budget_checks`, `cost_usd`,
  `cost_source`, `cost_reserved_usd`, `budget_degraded_from`, `guard_effect`, `guard_findings`,
  `guard_limits`.
- `controls.Gateway`: `GatewayConfig(budgets=...)` and `invoke(..., budget=BudgetRequest(...))`, new
  outcome reason `budget_exceeded` and `Outcome.budget`.
