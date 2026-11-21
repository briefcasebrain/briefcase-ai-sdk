### Added
- Versioned pricing data: `briefcase/data/pricing/v1.json` (39 models with `aliases` and null provenance fields,
  extracted mechanically from `cost.rs`) and `unpriced.json` (ids of common models with no shipped price).
- `briefcase.pricing`: layered loading (packaged < `~/.briefcase/pricing.json` < `$BRIEFCASE_PRICING_FILE` < explicit),
  line-anchored validation, `apply()` into a native `CostCalculator`, staleness helpers, and a pure-Python
  `estimate()` for flat per-token pricing that raises "needs native extension" for anything else.
- `briefcase pricing list|show|set|unset|import|check`; `check --records FILE` lists the models in your records
  that have no price.
- `scripts/pricing/extract.py`, `generate.py --check` and a cargo oracle, plus drift tests tying the JSON to
  `cost.rs` and `js/controls/src/bedrock/pricing.ts`.
