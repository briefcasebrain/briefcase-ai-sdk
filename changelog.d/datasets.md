### Added

- `briefcase dataset from-records FILE.jsonl --name NAME`: turn captured decision records into an immutable, content-addressed evaluation dataset version, with `--where` filters (globs; `status`, `model`, `prompt_name`, `prompt_version` keys), seeded `--sample`, `--dedupe`, `--reference recorded|none` and `--redact-fields`. Records captured without content are counted and explained, never turned into empty cases. Content is run through `briefcase.sanitize` when the native extension is present; otherwise `--allow-unsanitized` is required and the output says so.
- `briefcase dataset show|diff|export|import`: inspect and compare versions; export as hand-editable JSONL, inspect-ai `Sample` JSONL, or an lm-eval docs file plus task YAML (experimental); re-import edited JSONL with errors reported by line. `briefcase dataset list` also lists local versions and takes `--json`; `dataset register` / `list` behave as before.
- `briefcase eval run --dataset NAME[@v] --target module:function`: score a function per case offline with `exact`, `contains`, `json` or a custom scorer, `--concurrency`, `--fail-under` as a CI gate (exit 1), a results table split by prompt version with the worst failures first. Results are `eval.case` / `eval.run` records logged through `EvalRun` (content as SHA-256 digests unless `--capture-content`).
- `briefcase.prompts`: `prompt(name, template)` gives a template a content-hash version; records made while it is used carry `prompt_name` and `prompt_version`. `briefcase prompt list|show|diff`.
- `status()` hooks for `briefcase status`: dataset count and the last eval result.

### Changed

- Decision records gain the optional fields `prompt_name` and `prompt_version` (added to the golden schema fixture's optional fields).
