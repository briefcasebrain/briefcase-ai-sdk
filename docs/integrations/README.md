# Plug-and-play integrations

Two commands connect your coding tools to briefcase:

```bash
pip install briefcase-ai
briefcase connect            # detects your tools, shows the plan and a diff, asks once, writes
briefcase receive            # local OTLP receiver: tool telemetry -> briefcase decision records
```

`briefcase connect` writes each tool's telemetry and MCP configuration for you.
`briefcase receive` listens on `http://127.0.0.1:4318` and turns what the tools
send into decision records shaped like `@briefcase.capture` output.

## What `connect` does to your files

- It changes only the keys it names. JSON files keep their other keys and key
  order; TOML files get one block between `# >>> briefcase >>>` and
  `# <<< briefcase <<<`.
- Before every write it saves a timestamped backup next to the file
  (`settings.json.briefcase-bak-20261121T181435`).
- It refuses to touch a file it cannot edit safely (malformed JSON, JSON with
  comments or trailing commas, a TOML file that already has the same table) and
  prints the snippet to paste instead.
- It never writes managed or system settings. When a managed file overrides
  your user settings it says so and prints the snippet for your administrator.
- Commands such as `claude mcp add` are shown in the plan and run only after
  you confirm.
- Running it again changes nothing.

```bash
briefcase connect --dry-run          # plan and diff only
briefcase connect claude-code cursor # only these tools
briefcase connect --scope project    # project-level files where the tool supports them
briefcase connect --no-mcp           # telemetry only
briefcase connect --endpoint https://otel.example.com --headers "Authorization=Bearer T"
briefcase connect list               # supported tools and whether each was detected
briefcase connect status             # what is connected, and whether the keys are still there
briefcase doctor --connect           # keys present, tool version, managed overrides, receiver up
```

### Undo

```bash
briefcase connect --undo             # everything connect wrote
briefcase connect --undo cursor      # one tool
```

Undo reads the journal at `~/.briefcase/connect/journal.json` and removes only
the keys connect set, restoring the value each had before. A key you changed
after connecting is left as you set it. Files connect created are deleted if
nothing else is in them. A JSON file that was edited is rewritten in indented
form, so a file that was on one line does not return to one line; the backup
has the original bytes. MCP servers registered through a tool's own command
are not removed automatically; undo prints the command that removes them.

## Tools

| Tool | Command | Mode | What is written |
| --- | --- | --- | --- |
| Claude Code | `briefcase connect claude-code` | automatic | `env` keys in `~/.claude/settings.json`; MCP via `claude mcp add --scope user` |
| Codex CLI | `briefcase connect codex` | automatic | `[otel]` block in `~/.codex/config.toml`; MCP via `codex mcp add` |
| Cursor | `briefcase connect cursor` | automatic | hooks in `~/.cursor/hooks.json`; MCP in `~/.cursor/mcp.json` |
| VS Code Copilot Chat | `briefcase connect vscode-copilot` | automatic | `github.copilot.chat.otel.*` in VS Code user `settings.json`; MCP via `code --add-mcp` |
| Gemini CLI | `briefcase connect gemini-cli` | experimental | `telemetry` block in `.gemini/settings.json` |
| Claude Desktop / Cowork | `briefcase connect cowork` | print-only | nothing; prints the admin steps |
| GitHub Copilot CLI | `briefcase connect copilot-cli` | print-only | nothing; prints env vars and the MCP entry |
| Cline | `briefcase connect cline` | print-only | nothing; prints env vars |
| Windsurf, opencode | `briefcase connect windsurf opencode` | print-only | nothing; prints the MCP server command |

"Print-only" means the tool has no local setting to write, or its format is
not confirmed against its documentation, so `connect` shows you what to add and
writes nothing. Per-tool paths, the documentation page each format came from,
and the date it was checked are in
[`briefcase/connect/adapters.json`](../../briefcase/connect/adapters.json).

### Claude Code

Written to the `env` key of `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"
  }
}
```

Claude Code ignores telemetry variables in a project's `.claude/settings.json`,
so telemetry is always user-level, also with `--scope project`. The protocol
is `http/json`, which the receiver decodes with no extra dependency.
`--capture-content` adds `OTEL_LOG_USER_PROMPTS=1` and `OTEL_LOG_TOOL_DETAILS=1`.

MCP: `claude mcp add --scope user briefcase -- briefcase-mcp`, or with
`--scope project` an entry in `.mcp.json`.

A plugin that adds the MCP server (no telemetry; a plugin cannot set it):

```bash
claude plugin marketplace add briefcasebrain/briefcase-ai-sdk
claude plugin install briefcase@briefcase-ai
```

### Codex CLI

Appended to `~/.codex/config.toml` (Codex ignores `[otel]` in project config):

```toml
# >>> briefcase >>>
[otel]
environment = "dev"
exporter = { otlp-http = { endpoint = "http://127.0.0.1:4318/v1/logs", protocol = "binary", headers = {} } }
log_user_prompt = false
# <<< briefcase <<<
```

Codex sends protobuf, so the receiver needs one more package:

```bash
pip install 'briefcase-ai[receiver]'
```

`connect` checks for it and prints that command when it is missing.
`--capture-content` sets `log_user_prompt = true`. MCP is registered with
`codex mcp add briefcase -- briefcase-mcp`, or written as
`[mcp_servers.briefcase]` in the same block when `codex` is not on PATH.

### Cursor

Cursor has no OpenTelemetry export. `connect` adds one hook entry to each of
`sessionStart`, `sessionEnd`, `beforeSubmitPrompt`, `postToolUse`,
`afterAgentResponse` and `stop` in `~/.cursor/hooks.json` (or
`.cursor/hooks.json` with `--scope project`):

```json
{ "command": "briefcase hook cursor", "timeout": 5 }
```

`briefcase hook cursor` reads the hook event from stdin and posts it to the
receiver. It always exits 0, prints nothing, and gives up after one second, so
it cannot block or slow the agent when the receiver is down. Prompt text,
agent text, tool input and output, `user_email`, `workspace_roots` and
`transcript_path` are dropped inside the hook process, before anything is
sent, unless the hook was installed with `--capture-content`.

MCP goes in `~/.cursor/mcp.json` (or `.cursor/mcp.json`).

### VS Code Copilot Chat

Written to the VS Code user `settings.json`:

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://127.0.0.1:4318"
}
```

VS Code settings files often contain comments. When yours does, `connect`
prints this snippet instead of rewriting the file. Copilot is assumed to send
protobuf, so install `briefcase-ai[receiver]`. `--capture-content` adds
`github.copilot.chat.otel.captureContent: true`.

### Claude Desktop / Cowork

Telemetry is configured by an organization admin under Organization settings >
Data and privacy > Monitoring. `briefcase connect cowork` prints those steps
and the receiver endpoint. Cowork always sends `user.email` and
`workspace.host_paths`; the receiver hashes the first and drops the second.

## What is captured

| | Default | With `--capture-content` (connect) and `--keep-content` (receive) |
| --- | --- | --- |
| Model calls | model, token counts, cost, duration, status, session and prompt ids | the same |
| Tool calls | tool name, tool-use id, permission decision and its source, success, duration, result size | plus tool parameters and input |
| Prompts and responses | length only | text |
| Errors | error type; the message only after the SDK `Sanitizer` has rewritten it, otherwise `[redacted]` | raw message |
| `user.email` | keyed hash (`user_email_hmac_sha256`; the key stays in `~/.briefcase/receiver/`) | as sent |
| Host paths, transcript paths, raw API bodies | dropped | as sent |
| OTLP metrics | dropped (`briefcase receive --metrics` passes them through) | the same |

Content needs both switches: `connect --capture-content` makes the tool send
it, and `receive --keep-content` makes the receiver keep it. Either one alone
leaves records without content. Both print a warning.

Records use the `@capture` field set (`schema_version`, `decision_id`,
`decision_type`, `function_name`, `inputs`, `outputs`, `started_at`,
`ended_at`, `execution_time_ms`, and `error`, `status`, `workflow_id`,
`trace_id`, `parent_span_id` when they apply):

| `decision_type` | From |
| --- | --- |
| `model_call` | `api_request`, `api_error`, `api_refusal`; Copilot `chat` spans |
| `tool_call` | `tool_decision` joined with `tool_result` on the tool-use id; Copilot `execute_tool` spans; Cursor `postToolUse` |
| `user_prompt` | `user_prompt`; Cursor `beforeSubmitPrompt`. Opens a prompt group: its id is the `workflow_id` of the records that follow |
| `assistant_response` | `assistant_response`; Codex `agent_response`; Cursor `afterAgentResponse` |
| `event` | anything else (session start and end, events from tools without a dedicated mapping) |
| `metric` | OTLP metric data points, only with `--metrics` |

## The receiver

```bash
briefcase receive                          # foreground, records to the console
briefcase receive --to runs.jsonl          # or https://collector.example.com/v1/decisions, or otlp
briefcase receive --detach                 # background; records to ~/.briefcase/receiver/records.jsonl
briefcase receive --status
briefcase receive --stop
```

- Endpoints: `POST /v1/logs`, `/v1/metrics`, `/v1/traces`, `/v1/hooks/cursor`; `GET /healthz`.
- Encodings: `application/json` always; `application/x-protobuf` with
  `pip install 'briefcase-ai[receiver]'` (otherwise `415` with that hint).
  gzip and chunked bodies are accepted. Bodies over 8 MiB get `413`
  (`--max-body-mb`).
- It binds `127.0.0.1`. Any other `--host` requires `--token` (or
  `BRIEFCASE_RECEIVER_TOKEN`), sent by the tool as `Authorization: Bearer <token>`;
  pass it to the tools with `briefcase connect --headers "Authorization=Bearer <token>"`.
- `--to otlp` forwards records as spans through the `otel` extra
  (`BRIEFCASE_OTLP_ENDPOINT`, default `http://localhost:4317`). For an
  `https://` sink, `BRIEFCASE_EXPORT_TOKEN` is sent as the bearer token.

## MCP server: copy-paste and one-click installs

`briefcase connect` registers the `briefcase-mcp` server by default. To do only
that, or to share an install link:

```bash
briefcase mcp install                 # every detected tool
briefcase mcp install cursor --scope project
briefcase mcp link cursor             # prints the payloads below
```

When `briefcase-mcp` is not on PATH the payloads use `uvx`, which needs
nothing installed beforehand. Those are the forms shown here.

**Cursor** (open the link, or paste the JSON into `~/.cursor/mcp.json`):

```text
cursor://anysphere.cursor-deeplink/mcp/install?name=briefcase&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyItLWZyb20iLCJicmllZmNhc2UtYWlbbWNwXSIsImJyaWVmY2FzZS1tY3AiXX0%3D
```

```json
{
  "mcpServers": {
    "briefcase": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "briefcase-ai[mcp]", "briefcase-mcp"]
    }
  }
}
```

**Claude Code**

```bash
claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-mcp
claude mcp add-json briefcase '{"type":"stdio","command":"uvx","args":["--from","briefcase-ai[mcp]","briefcase-mcp"]}'
```

Project scope, in `.mcp.json`:

```json
{
  "mcpServers": {
    "briefcase": { "command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-mcp"] }
  }
}
```

**Codex CLI**

```bash
codex mcp add briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-mcp
```

**VS Code**

```bash
code --add-mcp '{"name":"briefcase","type":"stdio","command":"uvx","args":["--from","briefcase-ai[mcp]","briefcase-mcp"]}'
```

Workspace scope, in `.vscode/mcp.json` (note the `servers` key):

```json
{
  "servers": {
    "briefcase": { "type": "stdio", "command": "uvx", "args": ["--from", "briefcase-ai[mcp]", "briefcase-mcp"] }
  }
}
```

## What has and has not been verified

- Formats were taken from each tool's documentation on the `verified_on` date
  in `adapters.json`. Claude Code's MCP commands and the plugin manifests were
  also run against Claude Code 2.1.330. No other tool was run.
- The receiver's tests use synthetic payloads built from the documented event
  names, not captures from the tools.
- Codex's event attribute names and `service.name` are not documented in
  detail; the receiver accepts several spellings, and anything it does not
  recognise is dropped by redaction instead of passed through.
- Whether Copilot Chat's `otlp-http` exporter can send JSON is not documented;
  install the `receiver` extra to be safe.
- Gemini CLI's user-level settings path, Cline's environment variables, the
  Copilot CLI MCP entry shape, and the Windsurf and opencode MCP formats are
  not confirmed, which is why those are experimental or print-only.
- A detected tool version outside an adapter's supported range makes that
  adapter print-only; `--force` writes anyway.
