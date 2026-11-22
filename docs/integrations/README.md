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
| Tool calls | tool name, tool-use id, permission decision and its source, success, duration, result size | plus tool parameters, commands, file paths, URLs and tool input/output, as the tool sent them |
| Prompts and responses | length only | text |
| Errors | error type and `[redacted]`; the message is never kept (it routinely quotes the command or path that failed) | raw message |
| `user.email` | keyed hash (`user_email_hmac_sha256`; the key stays in `~/.briefcase/receiver/`) | as sent |
| Host paths, transcript paths, URLs, raw API bodies | dropped | as sent |
| OTLP metrics | dropped (`briefcase receive --metrics` passes them through) | the same |

Default redaction is an allow-list, not a filter: only the fields in the
"Default" column are copied, and a value is copied only when it looks like
an identifier (no whitespace, not a path, URL or e-mail address). Free text
is never passed through the SDK `Sanitizer` and kept: a PII sanitiser
rewrites e-mail addresses, not `cat /Users/me/project/.env`.

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
  `https://` sink, `BRIEFCASE_EXPORTER_TOKEN` is sent as the bearer token (the
  same variable `briefcase.observe()` reads; `BRIEFCASE_EXPORT_TOKEN` is a
  deprecated alias that still works and warns once).

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

## Limits and failure modes

This section covers the zero-code path: `briefcase exec -- <command>`,
`briefcase.observe()` with no argument, and `briefcase.auto()` with the
provider integrations (`openai`, `anthropic`, `google-genai`, `bedrock`,
`aiobotocore`). `briefcase.auto.status()` (text: `briefcase.auto.format_status()`)
reports the same facts for the running process: what is patched, what was
skipped and why, the SDK version found and the versions the patches were
tested with.

### What the wrappers guarantee

- The wrapped call returns the SDK's own object and raises the SDK's own
  exception object. Wrapper failures are logged at DEBUG and never raised.
- One record per call, emitted once: when the call returns, or for a stream
  when it is exhausted, closed, fails, or is garbage-collected.
- A record never contains a guess. A token counter the provider did not
  report is `null`, never `0`; `token_usage` is `null` when nothing was
  reported; anything not captured is named in `capture_limits`.
- No prompt, completion, tool argument, `user` / `metadata` value, header or
  API key is recorded unless `capture_content` is `full` (or `hash`). With
  `full`, each string is cut at 8,000 characters, one `inputs`/`outputs`
  value at 128,000, bytes become `<N bytes>`, other objects their type name.

### Forked worker processes

This applies to every process that exports records (`briefcase.observe()` /
`@capture` / `auto()`), not only under `briefcase exec`.

- Just before a fork the export queue is given up to 0.5 s to drain, so the
  export thread holds no exporter lock at the moment of the fork.
- In the child every lock of the export pipeline (and of the JSONL and HTTP
  exporters, the stream finaliser and `auto()`) is recreated, so a lock
  another thread held at the fork cannot hang the child. The child starts
  its own export thread on its first record.
- Records the parent had queued, and the HTTP exporter's unsent buffer, are
  exported by the parent only; a provider call in flight at the fork is
  recorded by the parent only.
- `multiprocessing` fork children leave through `os._exit` (no `atexit`);
  their queue is drained through multiprocessing's own exit path. A child
  that calls `os._exit` itself, or a pool that does (Celery's prefork pool
  uses billiard), can lose what is still queued: call `briefcase.flush()`
  before it exits.
- The GCP Logging exporter follows the same rules (fresh locks, the parent's
  unsent buffer is not re-sent, and the child builds its own client, since a
  gRPC channel cannot cross a fork). The OpenTelemetry exporter relies on the
  SDK's own at-fork handling (new lock, empty queue, new worker thread in the
  child); a `multiprocessing` child's last spans are flushed by the exit drain.

### `status` values on provider records

| `status` | Meaning |
| --- | --- |
| `ok` | The call returned; a stream reached its terminal event. |
| `error` | The SDK raised. `error_type`, `http_status` and `request_id` when the SDK error has them; `error` is the class name unless content capture is on. |
| `cancelled` | `asyncio.CancelledError`, `KeyboardInterrupt` or `SystemExit` passed through the call. |
| `incomplete` | A stream ended before its terminal event with no error seen: the program stopped reading, closed it, or dropped it. `outputs.chunk_count` is what was read; usage is what had arrived (usually `null`). |

Stream timing: `execution_time_ms` runs to the end of the stream, so it
includes the program's own time between chunks. `time_to_stream_ms` is
request start to the SDK returning the stream; `time_to_first_chunk_ms` is
request start to the first chunk reaching the program.

### Per provider

| Provider (tested SDK version) | Captured | Not captured, and what you see |
| --- | --- | --- |
| `openai` (3.57.0; reported working with 3.3.0) | `chat.completions.create/parse/stream`, `responses.create/parse/stream`, `completions.create`, `embeddings.create`, sync and async, `with_raw_response`, `with_streaming_response` when the program calls `.parse()`. Usage including cached, reasoning, audio and prediction tokens; `finish_reason`; for `n > 1` `choice_count` and `finish_reasons`; `tool_call_count`; `refusal`. | **Chat/completions streams have `token_usage: null`** and a `capture_limits` note unless the request sets `stream_options={"include_usage": true}` or `stream_usage=True` is passed (below). `with_streaming_response` read as raw bytes: `token_usage: null` plus a note. Assistants, Realtime, Batch, images, audio: no record. Objects created before `auto()` that hold a bound method (`client.chat.completions.with_raw_response`): no record. |
| `anthropic` (2.12.0) | `messages.create/parse/stream` and `beta.messages.*`, sync and async, raw-response modes as for openai. Cache read/creation tokens. | A stream abandoned before its first `message_delta`: `completion_tokens: null` (the API's `message_start` only carries a placeholder). `count_tokens`, Batches: no record. |
| `google-genai` (3.6.1) | `models.generate_content` and `generate_content_stream`, sync and async, chat sessions. Thought and cached-content token counts. | Automatic function calling makes several requests inside one call: one record with the usage of the **final** response only. No request id (the SDK exposes none). `embed_content`, `count_tokens`, Live API: no record. |
| `bedrock` (botocore 1.44.9) | `Converse`, `ConverseStream` (usage from the `metadata` event), `InvokeModel`, `InvokeModelWithResponseStream`. | `InvokeModel`: token counts from the `x-amzn-bedrock-*-token-count` response headers when present (`usage_source: http_headers`; the headers are not in the AWS API reference). Otherwise the body is observed **as the program reads it** (never read for it) and parsed for Anthropic, Titan Text and Llama shapes (`usage_source: body`); the record is then emitted when the body has been read or closed. Any other model family, or a body not read to its end: `token_usage: null` plus a note. `InvokeModelWithResponseStream`: usage from `amazon-bedrock-invocationMetrics` (undocumented, read when present), else Anthropic or Titan chunk fields, else `null` plus a note. Bidirectional streaming, guardrail and async-invoke operations: no record. |
| `aiobotocore` (none: see below) | The same four operations through async clients. | Written against the aiobotocore 3.10.0 source and tested against a stand-in client only; **not run against a real aiobotocore install**. `status()` shows `tested_versions: []`. |

`stream_usage=True` (`briefcase.auto("openai", stream_usage=True)` or
`BRIEFCASE_OPENAI_STREAM_USAGE=1`) adds `stream_options={"include_usage": true}`
to plain chat/completions streams that set no `stream_options`, and only for
clients whose `base_url` host is `api.openai.com` (extend with
`stream_usage_hosts=[...]`). The extra usage-only chunk the API then sends is
consumed by the wrapper and the `usage: null` marker removed from the other
chunks, so the program iterates and serialises the same chunks as before.
It is off by default because that clean-up depends on the SDK's chunk model;
Azure and OpenAI-compatible endpoints are never sent the option.

### When the SDK does not look as expected

Every patch target is looked up, and its signature checked, when the
integration is installed. Targets are grouped into surfaces (for example
`chat` = sync and async `create` and `parse`); a surface with one missing
or changed target is left completely untouched. You see one WARNING naming
every skipped surface (also as a Python `RuntimeWarning`, because briefcase's
logger is silent by default), `state: "partial"` and the reasons in
`briefcase.auto.status()`, and no records for those calls. If nothing
matches, `auto("name")` raises and `auto()` skips the integration and
reports `state: "failed"`.

A stream object without the attribute the wrapper instruments is handed
back untouched; the record says the stream was not observed. For
`anthropic` `messages.stream()` the record is then built from the public
`current_message_snapshot` when the `with` block exits, without chunk count
or timing.

### Framework and provider together

LangChain (or LlamaIndex, CrewAI, the OpenAI Agents SDK) on an instrumented
provider SDK produces two records for one model call. By default both are
kept and linked: the provider record carries `parent_run_id` (the
framework's LLM run id) and `parent_framework`. **Sum `token_usage` over one
side only**, for example over records without `parent_run_id`.
`briefcase.auto(dedupe="provider")` (`briefcase exec --dedupe provider`)
drops the provider record of a call made inside a framework LLM run;
`dedupe="framework"` drops the framework's LLM-level record when a provider
record was emitted for it.

The link is a context variable, so it follows the thread or asyncio task
that makes the call. It is absent, and both records are emitted unlinked
whatever `dedupe` says, when the framework reports the start of the run in
a context the model call does not inherit. Verified with langchain-core
1.8.4 over the openai SDK (invoke, stream, ainvoke, concurrent tasks and
threads), and with llama-index-core 0.14.26's own `CallbackManager` over
the openai SDK (sync, concurrent tasks, threads; not through a llama-index
LLM class). The CrewAI and Agents SDK hooks are tested against stand-ins
only, and CrewAI's event bus may run listeners on another thread,
in which case there is no link. A framework run whose end is never reported
stops being a parent when the next run starts in that context, or after 15
minutes.

`briefcase.auto("langchain")` and the other framework handlers record full
prompt and completion content by default; the provider integrations and
`briefcase exec` record none. The first framework installed on that default
says so once (WARNING and `UserWarning`). Pass `capture_content="none"` or
set `BRIEFCASE_CAPTURE_CONTENT=none`.

### Patching and other libraries

- `auto()` twice is a no-op. `auto("name", option=...)` with options that
  differ from the active ones re-instruments with the new options (WARNING);
  it is never silently ignored.
- A library that patched the same method **before** briefcase is called
  through. One that patched **after** briefcase is left in place by `undo()`
  (WARNING); the briefcase wrapper beneath it is switched off.
- `auto(lazy=True)` (what `exec` uses) imports no SDK: an import hook patches
  each package when the program imports it. The hook answers only for those
  packages, hands the module its real loader back, and is removed when
  nothing is waiting or on `undo()`.

### `briefcase exec`

| Situation | What happens |
| --- | --- |
| Unknown `--to` / `BRIEFCASE_EXPORTER` value, malformed OTLP endpoint, unsupported OTLP protocol, unknown `--skip` name | `exec` prints the reason and exits 2 before starting the program. Nothing falls back to the console. |
| `python -I`, `-E` or `-S` on the command line or the script's `#!` line | Refused with the reason (exit 2): such an interpreter ignores `PYTHONPATH` or never imports `sitecustomize`. `--allow-uninstrumented` runs it with a warning and no records. |
| The program starts another Python with `-I`/`-E`/`-S` | That child is not instrumented and nothing can say so. |
| A child Python that cannot import briefcase (other version or virtualenv) | One line on its stderr, the child runs untouched; nothing is added to its `sys.path`. |
| The destination becomes invalid inside a child (environment changed) | One line on stderr, that process runs uninstrumented. |
| `subprocess`, `multiprocessing` spawn/forkserver children | Run the bootstrap themselves: instrumented once. |
| `os.fork`, gunicorn workers, `multiprocessing` fork children | Inherit the parent's patches (once). Export works in the child as described under "Forked worker processes" below. |
| An existing `sitecustomize` (conda, Debian, your own) | Runs after briefcase's, and is what `import sitecustomize` returns. An error in it is reported, as Python does. |
| Signals and exit code | On POSIX the program replaces the `briefcase` process, so both are exactly the program's. On Windows it runs as a child: the exit code is passed on, Ctrl-C reaches both. |
| Non-Python commands | Run unchanged; any Python they start is instrumented. |

`briefcase.observe()` from the environment: `BRIEFCASE_EXPORTER` wins, then
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, then `OTEL_EXPORTER_OTLP_ENDPOINT`.
The OTLP protocol is `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`, else
`OTEL_EXPORTER_OTLP_PROTOCOL`, else the OpenTelemetry default
`http/protobuf`; it is not guessed from the port (an endpoint on 4317 with
no protocol set gets a WARNING). `OTEL_SDK_DISABLED=true` with an OTLP
destination exports nothing and says so. Header values and URL credentials
are never logged.

## What has and has not been verified

- Formats were taken from each tool's documentation on the `verified_on` date
  in `adapters.json`. Claude Code's MCP commands and the plugin manifests were
  also run against Claude Code 2.1.330. No other tool was run.
- The receiver's tests use synthetic payloads built from the documented event
  names, plus one real capture from Claude Code 2.1.330
  (`tests/receiver/fixtures/real/`). That capture made no tool calls, so the
  default-redaction tests for tool events reuse its real envelope with tool
  attributes filled in by hand (commands, host paths, URLs, credentials).
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
