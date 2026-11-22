# Adapter conformance checks

`briefcase connect` writes other vendors' config files and `briefcase receive` parses their telemetry, and
those formats change without notice. The conformance harness installs the latest release of each tool that can
be installed from a script, runs `briefcase connect` against it, and asks the tool itself whether it accepted
the result. It runs nightly in CI and tells you when a release breaks an adapter.

## The one command

```bash
briefcase doctor --connect
```

Real output from a temp HOME, with Claude Code and Codex CLI on PATH:

```text
briefcase connect integrations:
[warn] connect: no tools connected yet; run: briefcase connect
[ok]   conformance claude-code: Claude Code 2.1.330: telemetry-verified on 2026-11-21; your version 2.1.330 matches
[ok]   conformance codex: Codex CLI 0.171.0: config-accepted on 2026-11-21; your version 0.171.0 matches
```

When the installed tool is newer than the version the report was produced with, the line is a warning:
`Codex CLI 0.171.0: config-accepted on 2026-11-20; your version 0.180.2 is newer, untested`.
The data comes from `briefcase/connect/conformance-report.json`, shipped with the package.

## What the levels mean

| Level | Meaning |
| --- | --- |
| `docs-only` | Built from the vendor's documentation. No file that briefcase wrote was loaded by a real install of the tool. |
| `config-accepted` | A real install loaded the config `briefcase connect` wrote, and the tool's own commands reported it (or did not reject it). No telemetry was captured. |
| `telemetry-verified` | The real tool emitted telemetry and `briefcase receive` turned it into decision records. |

Levels are recorded per tool with the tool version and the date. A failed run never erases a recorded level; it
adds a `last_attempt` entry and opens the tracking issue.

## Current table (run on 2026-11-21)

| Tool | Version | Level | How it was verified |
| --- | --- | --- | --- |
| Claude Code | 2.1.330 | `telemetry-verified` | `claude mcp list`/`get`, `claude plugin validate` on the bundled plugin, `claude doctor` (no invalid settings), then `claude -p` against a local stub model API: the real binary exported OTLP/JSON log events that `briefcase receive` mapped to records (user prompt, model request, response). |
| Codex CLI | 0.171.0 | `config-accepted` | `codex mcp list` and `codex mcp get briefcase` load `config.toml`; `codex doctor --json` reports `config.load` ok and `config.toml parse` ok. Its parser is strict: a deliberately bad `[otel]` value made `codex mcp list` fail, so acceptance is meaningful. No telemetry: it needs a login. |
| Gemini CLI | 0.70.0 | `config-accepted` (weak) | The project `.gemini/settings.json` loads without a configuration error. Gemini CLI has no command that echoes telemetry settings and accepts unknown values, so this only proves the file parses. A telemetry run with a dummy key was tried and stopped at "Invalid auth method selected". |
| GitHub Copilot CLI | 1.0.114 | `docs-only` | `connect` is print-only for this tool. `copilot help environment` documents `COPILOT_OTEL_ENABLED` and `OTEL_EXPORTER_OTLP_ENDPOINT`, and `copilot mcp get briefcase` accepts the MCP entry we print, but nothing briefcase wrote was loaded. |
| VS Code Copilot Chat | not installed | `docs-only` | VS Code is a desktop app; its `code` CLI cannot validate `settings.json` and no schema for the Copilot OTel keys was found. |
| Cursor | not installed | `docs-only` | Desktop app; no documented headless install and no `hooks.json` validator or published schema was found. |
| Cowork, Cline, Windsurf, opencode | not covered | `docs-only` | Print-only adapters with no installable tool in scope. |

Install commands come from each vendor's documentation (checked 2026-11-21) and are recorded in
`scripts/conformance/drivers.py`: `npm install -g @anthropic-ai/claude-code@latest`
([setup](https://code.claude.com/docs/en/setup)), `npm install -g @openai/codex`
([README](https://github.com/openai/codex)), `npm install -g @google/gemini-cli@latest`
([README](https://github.com/google-gemini/gemini-cli)) and `npm install -g @github/copilot`
([README](https://github.com/github/copilot-cli)). The harness runs them with `--prefix` into a temp directory.

## What it writes or sends, and where

Everything happens in temp directories removed at the end: a temp HOME, an isolated npm prefix, a temp project.
It never touches your real tool configuration. The harness removes `CLAUDE*`, `ANTHROPIC*`, `OPENAI*`, `GEMINI*`,
`COPILOT*` and `OTEL_*` variables from the environment it hands the tool, so no identity or credential leaks in.
The telemetry step talks only to `127.0.0.1`: a local stub answers the tool's model API call with a canned
"ok", and a tee records the tool's OTLP requests and forwards them to a real `briefcase receive`. The tool's
telemetry is its own; only the model's answer is canned.

## Run it locally

```bash
python -m scripts.conformance run claude-code            # one tool, installs it in a temp prefix
python -m scripts.conformance run --use-path codex       # use the binary already on PATH
python -m scripts.conformance run --out report.json      # all tools, write a report
python -m scripts.conformance drift                      # fetch each docs_url, check expected strings
python -m scripts.conformance scrub RAW_DIR DEST_DIR     # scrub captured payloads for the corpus
```

`run` needs `node` and `npm` for the four installable tools and network access. Behind a proxy, set the usual
`HTTPS_PROXY` and CA variables. `drift` reads `briefcase/connect/conformance.json` (strings per adapter id);
a page that cannot be fetched is reported `unknown`, never as a failure. Real output when a fixture page lacks one string (`--pages` reads
`<adapter>.html` from a directory instead of the network):

```text
VANISHED claude-code      https://code.claude.com/docs/en/monitoring-usage
           no longer on the page: 'OTEL_LOG_USER_PROMPTS'
```

## CI

`.github/workflows/conformance.yml` runs nightly and on manual dispatch only (never on pull requests), one job
per tool plus a docs-drift job. It uploads the report as an artifact and, when a level changes, a run fails or a
documentation string vanishes, opens or updates one issue labelled `adapter-conformance`. It has `contents: read`
everywhere and `issues: write` on the last job only, publishes nothing, and uses only `actions/*` and the
preinstalled `gh`. The shipped report is updated by a reviewed PR from the artifact, not by the workflow.

## Captured payloads

`tests/receiver/fixtures/real/<tool>/<version>/` holds telemetry emitted by real tools, scrubbed of e-mail
addresses, host paths, tokens and identifiers before saving. Only Claude Code 2.1.330 is there. See that
directory's `README.md` for provenance.

## Turn it off or undo

There is nothing to undo: the harness writes only to temp directories. Disable the schedule by disabling the
workflow in GitHub. `briefcase doctor --connect` simply omits the section's rows for tools it has no data on.

## Limits and failure modes

- A level is a statement about one version on one date. Newer versions are "untested" until the next run.
- `config-accepted` for Gemini CLI is weak (see the table). Codex CLI and Claude Code reject bad config, so theirs is stronger.
- Telemetry is verified only where the tool emits without credentials; most tools do not.
- Claude Code's stub run shows the receiver handles the events present in one short session, not every event type.
- The docs-drift check proves a string is on the page, not that it is still correct there. Pages that render
  client-side or block automated clients show as `unknown`.
- The workflow file parses as YAML and runs the same commands as the local run, but it has not run on GitHub yet.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `doctor --connect` says "your version is newer, untested" | Nothing is broken yet. Run `python -m scripts.conformance run --use-path <tool>` to test your version. |
| `doctor --connect` shows "no conformance data shipped for this tool" | The tool is not covered; see `briefcase connect list`. |
| A tool run fails at install | Check `node --version` (CI runs 26) and proxy settings; the check name and detail are printed per step. |
| `claude mcp list` check fails offline | The MCP health check starts `uvx --from briefcase-ai[mcp]`; the harness only requires that `briefcase` is listed. |
| Drift reports `unknown` for every page | No network from this machine; nothing was checked. |
| Drift reports a vanished string | Read the vendor page, update the adapter and `briefcase/connect/conformance.json`, then re-run `run <tool>`. |
