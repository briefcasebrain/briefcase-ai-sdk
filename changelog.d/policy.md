### Added

- `briefcase policy` (init, check, test, log, off, status): allow, ask or deny what Claude Code,
  Cursor and Codex CLI may run. A `PreToolUse` / `before*` hook decides in-process against
  `~/.briefcase/policy.toml` (presets `observe`, `guarded`, `strict`; project and system policy
  layers that can only make it stricter), with shell analysis that follows quoting, chaining,
  substitutions, `sh -c`, `eval`, `sudo`/`env` wrappers and here-documents, and path rules that
  resolve `..` and symlinks. Fails open by default and says so in `briefcase status`;
  `fail_closed = true` blocks instead. Claude Code 2.1.330 verified end to end; Cursor and Codex
  from their documentation.
- `briefcase hook claude-code|codex|cursor --enforce`: the hook entry point, always exit 0, with
  the verdict as each tool's documented JSON.
- `briefcase connect --enforce` (connect plus `policy init`) and `briefcase connect --managed
  --out DIR`: a rollout pack for administrators (Claude Code `managed-settings.d` drop-in and
  macOS profile, Cursor enterprise `hooks.json`, Codex system `hooks.json`, labelled templates
  for the rest, and a README per OS). Nothing is written to system paths.
- Receiver: `POST /v1/policy/decisions` turns hook decisions into `policy.tool_call` records
  (rule id and effect; command and path only as a keyed hash unless content capture is on).
- `briefcase.policy.guardrail.ToolPolicyEnv`: the same policy as a guardrails `GuardrailEnv`.

### Changed

- `briefcase connect` data: Copilot CLI sends OTLP as http/json by default (`copilot help
  environment`, 1.0.114), so it no longer needs the protobuf extra, and `--capture-content`
  prints `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT=SPAN_ONLY`. Gemini CLI's docs link points at
  the raw GitHub file. Tested versions recorded: Claude Code 2.1.330, Codex CLI 0.171.0.

### Fixed

- `briefcase connect --undo` reported a hook entry that had been edited after it was written as
  "already removed" and left it in place; it now recognises its own entry by its marker. Undoing two
  owners of one file (connect telemetry and policy hooks in Cursor's `hooks.json`) no longer
  leaves an empty `"hooks": {}` behind.
