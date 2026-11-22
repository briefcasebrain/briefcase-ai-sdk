# Spending budgets for LLM calls

## What it is for

Put a hard and a soft spending limit on the LLM calls your app makes, per app, per model, or per
tenant / user / session, checked in your process before each request is sent. A call that would
cross a hard limit is blocked (or let through with a warning, or switched to a cheaper model), and
every call's record says what it cost and which limit it counted against.

**Status: experimental.** Enforcement was exercised through the real openai 3.57.0, anthropic
2.12.0, google-genai 3.6.1 and botocore 1.44.9 clients over fake transports: no request reached a
provider and no real bill was compared. The shared stores were run against real Redis 7.0.15,
PostgreSQL 16.15 and MySQL 8.0.46 servers on localhost. Prices come from the packaged table, which
is **unverified** (see [pricing-data.md](pricing-data.md)); a budget is only as right as its prices.

## The one command

```bash
briefcase budget set daily --usd 20
```

Then, in the app (budgets act on the SDK calls that `briefcase.auto()` instruments):

```python
import briefcase
from briefcase import budgets

briefcase.auto()          # openai, anthropic, google-genai, bedrock
budgets.enable()          # reads ~/.briefcase/budgets.toml

with budgets.budget_scope(tenant="acme"):      # optional: attribute calls to a tenant
    client.chat.completions.create(model="gpt-4o", messages=[...], max_tokens=300)
```

Or define budgets in code only, with no file:

```python
budgets.budget("daily", usd=20, soft_usd=15)
budgets.budget("per-tenant", usd=2, per="tenant", window="month",
               on_exceed="degrade", degrade_to="gpt-4o-mini")
```

## Output (real run in a temp HOME)

```text
$ briefcase budget set daily --usd 20 --soft-usd 15
--- /dev/null
+++ ~/.briefcase/budgets.toml
@@ -0,0 +1,7 @@
+# briefcase budgets (written by `briefcase budget set`; edit freely).
+# Enforced in apps that call briefcase.budgets.enable(). Docs: docs/features/budgets.md
+
+[budgets.daily]
+usd = 20.0
+soft_usd = 15.0
+window = "day"
Wrote ~/.briefcase/budgets.toml
Apps enforce it after calling briefcase.budgets.enable() (and briefcase.auto()).
Next: briefcase budget test --model gpt-4o --input-tokens 1000 --max-tokens 500  # see what a call would do

$ briefcase budget set per-tenant --usd 2 --per tenant --window month --on-exceed degrade --degrade-to gpt-4o-mini --model gpt-4o*
...
Wrote ~/.briefcase/budgets.toml

$ briefcase budget test --model gpt-4o --input-tokens 2000 --max-tokens 500 --scope tenant=acme
call: gpt-4o, 2000 input tokens, up to 500 output tokens, scope tenant=acme
price: $5 in / $15 out per million tokens; reserve $0.017500 before sending
counters: memory store, so every budget starts at 0 here
  daily [*, 2026-11-22]: $0.017500 of $20 after this call -> allow
  per-tenant [tenant=acme, 2026-11]: $0.017500 of $2 after this call -> allow
decision: ALLOW
Next: briefcase budget status
```

Then an app (`app.py`: `briefcase.auto("openai")`, `budgets.enable()`, gpt-4o calls of 1,200 input +
300 output tokens for `tenant="acme"` through a fake transport) with the counters in SQLite
(`--store sqlite:///$HOME/.briefcase/budgets.db` added to the second `set`):

```text
$ python app.py
call 190: degraded to gpt-4o-mini
$ briefcase budget status
store: sql (sqlite, table briefcase_budget_usage)
BUDGET           SCOPE                  WINDOW                        SPENT      %  RESETS
daily            *                      2026-11-22              $1.9954/$20   10.0  18h 15m left
per-tenant       tenant=acme            2026-11                  $1.9954/$2   99.8  8d 18h left
Next: briefcase budget show  # 1 budget(s) at 80% or more: raise a limit with budget set
$ briefcase status
...
Enforce
  attention Budgets                per-tenant (tenant=acme) at 99.8% of its limit; 1 over 80%
  off       Content guardrails     no guard.toml (apps may still call content.guard() in code)
```

A blocked call raises before anything is sent:

```text
BudgetExceeded: budget 'daily' (*, window 2026-11-22) would be exceeded: $0.000240 spent + $0.015025 reserved for this call > $0.01 limit; resets 2026-11-23 00:00 UTC. The request was not sent. Check: briefcase budget status
```

## How a call is counted

1. **Reserve** (before the request): the most the call can cost. Input tokens are estimated high
   (UTF-8 bytes / 3 per text, plus 4 per text for framing and 1,600 per image / audio / file part),
   or counted exactly by your `token_counter=`. Output tokens are the request's own cap
   (`max_tokens`, `max_completion_tokens`, `max_output_tokens`, `inferenceConfig.maxTokens`, times
   `n`); with no cap, the model's maximum from the price table (`default_max_tokens`, 4096, if the
   table has none). Prices are the highest rate the model has (long-context tiers included). The
   reservation is added to every budget that applies, atomically and all-or-nothing.
2. **Settle** (when the response, or the end of the stream, arrives): the reservation is replaced by
   the cost of the usage the provider reported, and the difference is released.

| What happened | `cost_source` | Counted |
| --- | --- | --- |
| Usage reported, flat per-token price | `usage` | the exact cost (`briefcase.pricing` arithmetic) |
| Usage reported with prompt-cache tokens or a long-context tier | `usage_upper_bound` | cache reads at the full input rate, cache writes at the highest write rate (or 2x input), every token at the highest tier rate: never less than the real charge for the table's prices |
| No usage (a chat stream without `stream_options.include_usage`, an abandoned stream, a timeout, a 5xx) | `reserved` | the whole reservation: never undercounted silently |
| Not sent (blocked by a budget or a guardrail), or a 4xx answer | `not_billed` | nothing; the reservation is released |
| Model has no price | `unpriced` | tokens only; USD limits cannot count it |

A stream is settled when it is read to its end, closed, or dropped (the garbage collector
finalizes it). A settled cost can exceed its reservation (the estimate was low); the overshoot is
counted and the next call sees it.

**Windows** are calendar windows in UTC: `hour`, `day`, `month`, or `total` (never resets). They were
chosen over rolling windows because a shared store then needs one counter per window, the reset
time is predictable (`briefcase budget status` shows it), and monthly windows line up with provider
invoices. A reservation is always settled into the window it was taken in.

**Effects** at the hard limit (`on_exceed`):

| Effect | What happens |
| --- | --- |
| `block` (default) | `briefcase.budgets.BudgetExceeded` is raised from the SDK call; nothing is sent. |
| `warn` | The call goes through, is counted, and a WARNING is logged (at most once a minute per counter). |
| `degrade` | The call is switched to `degrade_to` (or to what `degrade(model, budget, info)` returns) and re-checked; it is blocked if the cheaper model does not fit either. |

A soft limit (`soft_usd`, `soft_tokens`) never stops a call: it logs a WARNING once a minute and the
record says `budget_effect: "soft_limit"`.

## What it writes or sends, and where

- `briefcase budget set|unset` writes `~/.briefcase/budgets.toml` (or `$BRIEFCASE_BUDGETS_FILE`; a path
  outside `~/.briefcase` asks first, `--yes` skips). Every write shows a diff; `--dry-run` writes nothing.
- Counters go to the configured store: in-process memory (default), Redis, or a SQL table. Keys are
  `<budget>|<scope>|<window>`; no prompt or completion content is ever stored.
- Each provider call's record gets these fields (no content):

  | Field | Value |
  | --- | --- |
  | `budget_effect` | `allow`, `soft_limit`, `unpriced`, `warn`, `degrade`, `block`, or `gateway` (counted by a `controls.Gateway` reservation) |
  | `budget_scope` | the `budget_scope(...)` values, e.g. `{"tenant": "acme"}` |
  | `budget_checks` | per budget: `budget`, `scope`, `window`, `effect`, `used_usd` / `used_tokens` after the reservation, `limit_usd` / `limit_tokens` |
  | `cost_usd` | settled cost (null when unpriced) |
  | `cost_source` | see the table above |
  | `cost_reserved_usd` | what was reserved before the call |
  | `budget_degraded_from` | the requested model, when `degrade` switched it |

Real record fields from a call with a $0.01 daily budget (fake transport):

```json
{
  "budget_effect": "allow",
  "budget_scope": {},
  "cost_reserved_usd": 0.003065,
  "budget_checks": [{"budget": "daily", "scope": "*", "window": "2026-11-22", "effect": "allow",
                     "used_usd": 0.003065, "limit_usd": 0.01, "used_tokens": 213, "limit_tokens": null}],
  "cost_usd": 0.00024,
  "cost_source": "usage"
}
```

## Defaults and how to change them

| Setting | Default | Change it |
| --- | --- | --- |
| Store | in-process memory (limits hold within one process) | `store = "redis://host:6379/0"`, `"sqlite:///path.db"`, `"postgresql://..."`, `"mysql://user:pw@host/db"`; or `budgets.configure(store=RedisBudgetStore(client))` |
| Unpriced models | `warn`: one log line per model naming `briefcase pricing set`; token limits still apply | `unpriced = "block"` or `"allow"` |
| Output tokens when the request sets no cap | the model's maximum, else 4096 | set `max_tokens` on requests; `default_max_tokens = N` |
| Input tokens | high estimate | `budgets.configure(..., token_counter=lambda provider, model, kwargs: n)` |
| Window | `day` | `--window hour|day|month|total` |
| Effect | `block` | `--on-exceed warn|degrade` |
| Config file | `~/.briefcase/budgets.toml` | `$BRIEFCASE_BUDGETS_FILE`, or `budgets.enable(path)`; `$BRIEFCASE_HOME` moves `~/.briefcase` |

**Shared stores.** Redis: one hash per counter, reserve and settle are Lua scripts (atomic on the
server); `pip install redis`. SQL: one table, created if missing:

```sql
CREATE TABLE IF NOT EXISTS briefcase_budget_usage (
    budget_key VARCHAR(255) NOT NULL PRIMARY KEY,
    used_nano BIGINT NOT NULL DEFAULT 0,        -- nano-USD (1e-9) spent or reserved
    used_tokens BIGINT NOT NULL DEFAULT 0,
    inflight_nano BIGINT NOT NULL DEFAULT 0,    -- the part still reserved by calls in flight
    inflight_tokens BIGINT NOT NULL DEFAULT 0,
    expires_at BIGINT NULL                      -- unix time; a day after the window ends
)
```

A reservation is one `UPDATE ... SET used_nano = used_nano + ? WHERE budget_key = ? AND
used_nano + ? <= ?`, which takes the row lock and re-checks the limit against the latest row, so
concurrent reservations cannot overshoot; deadlocks and serialisation failures are retried. Drivers:
`sqlite3` (stdlib), psycopg 3 / psycopg2, PyMySQL; a SQLAlchemy `Engine` is accepted by
`SQLBudgetStore(engine)`.

**The controls gateway.** `Gateway(GatewayConfig(..., budgets=Budgets([...])))` and
`gateway.invoke(..., budget=budgets.BudgetRequest("gpt-4o", input_tokens, max_tokens))` reserve after
the quota check (scope `tenant=<tenant_id>`), return `reason="budget_exceeded"` when refused, and
settle from the instrumented calls made inside `fn` (not reserved twice), else from
`BudgetRequest(usage_of=...)`, else at the reservation. `budgets.gateway_model()` inside `fn` names
the model to use after a `degrade`.

## How to turn it off or undo

- `briefcase budget unset NAME` removes one budget (shows the diff); delete `budgets.toml` to remove all.
- In code: `briefcase.budgets.disable()`. Counters in a shared store are kept; delete the Redis keys
  `briefcase:budget:*` or the `briefcase_budget_usage` rows to reset them.

## Limits and failure modes

- Only calls made through instrumented SDKs are counted: openai (chat, responses, completions,
  embeddings), anthropic (`messages.create/parse/stream`), google-genai (`generate_content`,
  streaming), Bedrock (Converse, InvokeModel and their streams). Raw HTTP calls, other SDKs and
  bound methods created before `briefcase.auto()` are not. Use the gateway's `usage_of` for those.
- Prices are the packaged, unverified table plus your `briefcase pricing set` entries.
  Cached-prompt and long-context calls settle at an upper bound, not the exact price.
- The input-token estimate is deliberately high; a tight limit can block a call that would have fit.
  Pass a `token_counter` for exact counts.
- A hard limit can be exceeded by at most the difference between a call's settled cost and its
  reservation (only when the estimate was low); the next call is then blocked.
- A process that dies between reserve and settle leaves its reservation counted (shown as "in flight").
- Shared stores add a blocking round trip on the calling thread, including inside `async` SDK calls.
  Measured on this machine (median of 200 reserve + settle pairs): memory 27 us, Redis (localhost)
  0.15-0.22 ms, PostgreSQL 0.75-4.8 ms, MySQL 1.7-6.8 ms, SQLite 1.5-20 ms (all fsync-bound,
  varied between runs). The whole budget hook added about 0.18-0.19 ms per call with the memory store.
- `per="tenant"` reads the scope from a `contextvars.ContextVar`: it follows `asyncio` tasks and
  `asyncio.to_thread`, not threads you start by hand.
- In-process (`memory`) counters are invisible to `briefcase budget status`; use a shared store or
  `briefcase.budgets.status()` in the app.
- Redis counters are Lua numbers: exact up to 2**53 nano-USD (about 9 million USD) per counter.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| Calls are never blocked / records have no `budget_effect` | The SDK is not instrumented or budgets are not enabled in that process: call `briefcase.auto()` and `briefcase.budgets.enable()`; check `briefcase.auto.status()`. |
| `BudgetExceeded` on the first call of the day | The reservation (input estimate + output cap) is larger than the limit: `briefcase budget test --model M --input-tokens N --max-tokens M` shows it; set `max_tokens` on the request or pass a `token_counter`. |
| `briefcase budget status` shows "(in app)" | The store is in-process memory. Set a shared store: `briefcase budget set NAME --store sqlite:///$HOME/.briefcase/budgets.db`. |
| WARNING "model ... has no price" | Add one: `briefcase pricing set MODEL --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL`. |
| `cost_source: "reserved"` on every streamed openai call | Chat streams carry usage only with `stream_options={"include_usage": True}` (or `BRIEFCASE_OPENAI_STREAM_USAGE=1`); otherwise the reservation is kept. |
| "in flight" stays above zero | A process died mid-call, or a stream object is still alive. It clears when the window ends. |
| Spend in `briefcase status` looks too high | `cost_source: "usage_upper_bound"` calls (prompt caching) are counted at an upper bound. |
| `briefcase budget status` fails with a store error | Check the URL in `briefcase budget show`; Redis needs `pip install redis`, Postgres `psycopg`, MySQL `pymysql`. |
