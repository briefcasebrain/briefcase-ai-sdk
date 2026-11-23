# Pricing data

## What it is for

Briefcase turns token counts into dollars using a price table. This feature ships that table as a
versioned data file with provenance, lets you add verified prices for your own models, and tells you
which of the models in YOUR records have no price at all.

**Honest status.** The 39 shipped prices were copied mechanically from the Rust core's built-in table
(`crates/briefcase-core/src/cost.rs`). Nobody has checked them against provider price pages, so every
model has `source_url`, `effective_from` and `verified_on` set to `null`, and the file says so in its
top-level `notice`. Many current models are not priced (see `briefcase/data/pricing/unpriced.json`).

## The one command

```bash
briefcase-ai pricing check --records runs.jsonl
```

Validates every pricing layer, flags unverified or old data, and lists the models in your records that
have no price, each with the exact command that adds one.

## Output (real run in a temp HOME)

Captured with the command module driven directly from a throwaway `HOME`; `briefcase-ai pricing ...`
becomes available once the CLI shell auto-discovers `briefcase/cli/*_commands.py`. `o3` is a known
model with no shipped price; `acme-large` is a made-up model and its prices are placeholders, not real.

```text
$ briefcase-ai pricing check --records runs.jsonl
ok  packaged   39 models  packaged
warn 39 of 39 models have no provenance (source_url/verified_on are null): prices are unverified
warn 2 model(s) in your records have no price

Records: 5 read from runs.jsonl (4 distinct models)
Models in your records with NO price:
  o3  (2 records)  [known model, no price shipped]
      briefcase-ai pricing set o3 --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL
  acme-large  (1 records)
      briefcase-ai pricing set acme-large --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL
Next: briefcase-ai pricing set o3 --input ... --output ... --source-url URL

$ briefcase-ai pricing set acme-large --input 1 --output 2 --source-url https://acme.example/pricing
'acme-large' is not in the table, so --context-window N and --provider NAME is required. Run:
  briefcase-ai pricing set acme-large --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL

$ briefcase-ai pricing set acme-large --input 1 --output 2 --context-window 128000 --provider acme --source-url https://acme.example/pricing --yes
Set acme-large: input $1/MTok, output $2/MTok

--- ~/.briefcase/pricing.json (current)
+++ ~/.briefcase/pricing.json (proposed)
@@ -0,0 +1,18 @@
+{
+  "schema_version": 1,
+  "models": [
+    {
+      "model_name": "acme-large",
+      "provider": "acme",
+      "input_cost_per_1k_tokens": 0.001,
+      "output_cost_per_1k_tokens": 0.002,
+      "context_window": 128000,
+      "max_output_tokens": null,
+      "aliases": [],
+      "source_url": "https://acme.example/pricing",
+      "effective_from": null,
+      "verified_on": "2026-11-21",
+      "origin": "user:briefcase-ai pricing set"
+    }
+  ]
+}

Wrote ~/.briefcase/pricing.json
Next: briefcase-ai pricing show acme-large

$ briefcase-ai pricing check --records runs.jsonl
ok  packaged   39 models  packaged
ok  user        1 models  ~/.briefcase/pricing.json
warn 39 of 40 models have no provenance (source_url/verified_on are null): prices are unverified
warn 1 model(s) in your records have no price

Records: 5 read from runs.jsonl (4 distinct models)
Models in your records with NO price:
  o3  (2 records)  [known model, no price shipped]
      briefcase-ai pricing set o3 --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL
Next: briefcase-ai pricing set o3 --input ... --output ... --source-url URL

$ briefcase-ai pricing show claude-sonnet-4-5-20250929
claude-sonnet-4-5  (anthropic)   [claude-sonnet-4-5-20250929 matched by normalized]
  input   $3 per million tokens
  output  $15 per million tokens
  limits  context 200,000, max output 64,000
  layer   packaged (packaged with briefcase-ai)
  source  none (unverified)
  verified_on never   effective_from unknown   origin builtin-table@feb8c62104c0
  aliases none
  extras  cache/long-context/fast-mode rates exist for this built-in; the pure-Python estimate refuses them (native extension needed)
  note    this price has not been checked against a provider page

Next: briefcase-ai pricing check --records FILE

$ briefcase-ai pricing show gpt-5
No price for 'gpt-5'.
It is a known model with no price shipped (provider openai); add one with `briefcase-ai pricing set`.
Closest priced ids: gpt-5.5, gpt-5.4, gpt-4
Add it:  briefcase-ai pricing set gpt-5 --input USD_PER_MTOK --output USD_PER_MTOK --context-window N --provider NAME --source-url URL
```

## What it writes or sends, and where

- Reads `briefcase/data/pricing/v1.json` (packaged), `~/.briefcase/pricing.json` (user, if present),
  `$BRIEFCASE_PRICING_FILE` (if set). `$BRIEFCASE_HOME` moves `~/.briefcase`.
- Writes only `~/.briefcase/pricing.json`, only from `pricing set`, `pricing unset` and `pricing import`,
  each after showing a diff and asking once (`--yes` skips, `--dry-run` writes nothing).
- `pricing check --records` reads only model-id fields (`model`, `model_name`,
  `gen_ai.request.model`, `gen_ai.response.model`, top level and one level into
  `inputs`/`outputs`/`metadata`). Prompt and response content is never read or printed.
- Nothing is sent anywhere.

## Defaults and how to change them

| What | Default | Change |
|---|---|---|
| Merge order | packaged < user file < `$BRIEFCASE_PRICING_FILE` < explicit path | set the env var / file |
| A higher layer's model | replaces the lower entry whole | `pricing unset MODEL` |
| "Old data" threshold | 90 days | `pricing check --max-age-days N` |
| Warnings fail the check | no (exit 0) | `pricing check --strict` |

Schema (`schema_version` 1): `{"models": [...]}` is what the native
`CostCalculator.load_pricing_json` accepts, prices in **USD per 1,000 tokens**
(`input_cost_per_1k_tokens`, `output_cost_per_1k_tokens`, `context_window`, `max_output_tokens`, `provider`).
This feature adds `aliases`, `source_url`, `effective_from`, `verified_on`, `origin` and
`rate_card_extras` (cache, long-context, fast-mode, priority data copied from `cost.rs`, in USD per
million tokens). Unknown fields are tolerated. `pricing set` takes USD per million tokens and divides by
1000, as the core does.

Python use:

```python
from briefcase import pricing
table = pricing.load()                    # merged layers
table.apply()                             # push into a native CostCalculator (None if no native extension)
table.estimate("gpt-4", 1000, 500)        # pure Python, simple per-token case only
table.age_days(); table.unverified()
```

`apply()` only pushes models your layers added or changed, plus built-ins the native calculator lacks:
the native loader replaces a model outright and drops its cache and long-context data, so re-pushing the
identical built-ins would make native pricing worse.

## Turn it off or undo

`briefcase-ai pricing unset MODEL` removes one entry; deleting `~/.briefcase/pricing.json` removes the user layer.
The packaged file is read-only data.

## Limits and failure modes

- The pure-Python `estimate` mirrors the native arithmetic for flat per-token pricing only. It raises
  "needs native extension" for prompt-cache tokens, non-standard rate cards (batch, priority, regional...)
  and inputs that hit a long-context tier, rather than returning an approximation.
- `pricing set --cached-input` is recorded but the native engine applies flat rates only to user entries.
- A model with no price is never guessed from a family name.
- The "known model, no price shipped" list is a hint of ids, not prices, and is not exhaustive.

## Troubleshooting

| Symptom | Do this |
|---|---|
| `Pricing files are invalid:` with `path:line:` lines | Edit that line, or move the file aside; `briefcase-ai pricing check` shows every layer |
| A model shows `cost 0` or "unknown model" | `briefcase-ai pricing check --records FILE`, then run the `pricing set` it prints |
| `pricing set` says `--context-window N and --provider NAME is required` | The model is not in the table; pass both |
| `Not writing without confirmation` | No terminal to ask on; add `--yes` (or `--dry-run` to preview) |
| `needs native extension` from `estimate` | Install the native wheel (`pip install briefcase-ai`); the case is not flat per-token |
| Check warns "39 of 39 models have no provenance" | Expected for the shipped data; verify and `pricing set` the models you rely on |

## For maintainers: updating a price

`briefcase/data/pricing/v1.json` is the reviewed source of truth. The flow is:

1. Edit the model in `v1.json`, setting `source_url`, `effective_from` and `verified_on` (review the diff).
2. Run `python scripts/pricing/generate.py --check`. Today it only VERIFIES that `cost.rs` and
   `js/controls/src/bedrock/pricing.ts` agree with the JSON and prints what a regeneration would change;
   it does not rewrite those files. Update them by hand to match until regeneration is implemented
   (rewriting `crates/` and `js/` is out of scope for this package).
3. `pytest tests/pricing` fails whenever the JSON and `cost.rs` disagree, or any rate in `pricing.ts`
   is missing from the JSON.

`scripts/pricing/extract.py --generated-at YYYY-MM-DD` re-creates the file from `cost.rs` (it resets
provenance, so use it only when the Rust table itself changed). Extraction exactness is proven by
comparing the JSON with the core's own serialisation (`scripts/pricing/native_oracle`,
`BRIEFCASE_PRICING_ORACLE=1 pytest tests/pricing`, needs cargo with an offline dependency cache).

Real output of the consistency check:

```text
$ python scripts/pricing/generate.py --check
ok: cost.rs and pricing.ts agree with briefcase/data/pricing/v1.json
```
