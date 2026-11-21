# Captured payloads from real tools

Everything under `real/<tool>/<version>/` was emitted by the vendor's own binary, at the version
in the directory name, and saved by `python -m scripts.conformance run <tool> --save-payloads DIR`
followed by `python -m scripts.conformance scrub DIR real/<tool>`. Nothing here is hand-written. The
synthetic payloads used by the other receiver tests live in `tests/receiver/payloads.py` and say so.

## Provenance

| Tool | Version | Captured | How |
| --- | --- | --- | --- |
| Claude Code | 2.1.330 | 2026-11-21 | Real `claude -p` (npm `@anthropic-ai/claude-code@latest`) in a temp HOME configured by `briefcase connect claude-code`. No account or credentials: the model API was a local stub (`ANTHROPIC_BASE_URL`, dummy key) that returns a canned "ok". OTLP/JSON requests from the tool were recorded verbatim by a tee in front of `briefcase receive`. |

What this does and does not show:

- The telemetry (`user_prompt`, `api_request`, `assistant_response`, plugin and MCP events, metrics) is the
  tool's own. The model reply behind it is canned, so token counts and latency are not realistic.
- The capture ran inside a managed sandbox, so `plugin_loaded` events name that sandbox's managed plugins.
- Codex CLI, Gemini CLI, Copilot CLI, Cursor and VS Code have no entries: none of them emits telemetry
  without a logged-in account (or, for the desktop apps, could not be run here). No payload is faked for them.

## Scrubbing

`scripts/conformance/scrub.py` runs before anything is saved. It replaces e-mail addresses, absolute host
paths (Unix and Windows), API keys and tokens (`sk-`, `ghp_`, JWTs, bearer values, long hex and opaque strings),
UUIDs (account, organisation, session; mapped to stable placeholders so references still line up), non-loopback
IP addresses, and the values of identity-looking attributes (`user.*`, `organization.*`, `*email*`, `*account*`,
`*token*`, `workspace.host_paths`, ...). Trace and span ids are replaced by sequential placeholders.
`scrub_json` refuses to return output in which its own `findings()` check still sees anything, and
`tests/conformance/test_scrub.py` runs that check over every file in this directory.

Add a capture only through the scrubber; never copy a raw payload in by hand.
