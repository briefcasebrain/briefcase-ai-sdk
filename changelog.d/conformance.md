### Added
- Adapter conformance harness (`python -m scripts.conformance run|drift|scrub|merge|changes`): installs the latest Claude Code, Codex CLI, Gemini CLI and Copilot CLI into an isolated prefix and temp HOME, runs `briefcase connect --yes`, checks the result with each tool's own commands, and records a level (`docs-only` < `config-accepted` < `telemetry-verified`) with tool version and date.
- Docs-drift check: `briefcase/connect/conformance.json` lists the strings each adapter depends on; `python -m scripts.conformance drift` fetches each `docs_url` and reports expectations that vanished (network failures are "unknown").
- Captured-payload corpus `tests/receiver/fixtures/real/` with a scrubber and tests; seeded with Claude Code 2.1.330 telemetry emitted by the real binary.
- `.github/workflows/conformance.yml`: nightly and manual run, per-tool matrix, report artifact, one tracking issue on a level change or failure. Never runs on pull requests.
- `briefcase doctor --connect` shows the last known conformance level per tool and flags an installed version newer than the tested one (`briefcase/connect/conformance-report.json`).
