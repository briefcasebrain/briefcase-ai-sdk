# Guided setup, status and the feature catalog

`briefcase-ai` has many commands. These are the three places to start from, so you never have
to read source to find one: a guided setup, a status screen, and a catalog of every feature.

## The one command

```bash
briefcase-ai
```

The first time you run it on a terminal it asks one question and walks you through two or
three steps for your answer. Here is the "AI coding tools" path, start to finish:

```text
briefcase-ai setup
One question, then two or three steps. Nothing is written without asking first.

What are you setting up?
  1) I use AI coding tools (Claude Code, Cursor, Codex…)
  2) I'm building an app that calls LLMs
  3) I run a team or platform
  4) Skip for now
Choice [1/2/3/4]: 1

Step 1 of 2 · Connect your coding tools
This is `briefcase-ai connect`: it lists every file it would change, shows the diff and
asks once. Prompt and tool content stays off. Undo any time: briefcase-ai connect --undo
Detected: Claude Code

== Claude Code ==
  create ~/.claude/settings.json
         Turn on Claude Code's OpenTelemetry export and point it at the briefcase receiver
    --- /dev/null
    +++ ~/.claude/settings.json
    @@ -0,0 +1,9 @@
    +{
    +  "env": {
    +    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    +    "OTEL_METRICS_EXPORTER": "otlp",
    +    "OTEL_LOGS_EXPORTER": "otlp",
    +    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    +    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"
    +  }
    +}
  run    claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-ai-mcp
         Register the briefcase MCP server with Claude Code

Write 1 file(s) and run 1 command(s)? [Y/n] y
  wrote  ~/.claude/settings.json
  ran    claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-ai-mcp

Undo any time with: briefcase-ai connect --undo

Step 2 of 2 · Start the receiver
The receiver listens on 127.0.0.1:4318 and turns what the tools send into records in
~/.briefcase/receiver/records.jsonl. Prompts, responses and paths are dropped.
Start it in the background now? [Y/n] y

receiver started (pid 10202) on http://127.0.0.1:4318
  records: ~/.briefcase/receiver/records.jsonl
  log:     ~/.briefcase/receiver/receiver.log
  stop:    briefcase-ai receive --stop
Restart the connected tools so they pick up the new settings.

briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  on        Coding tools  Claude Code connected
  on        Receiver      running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected
  on        MCP server    registered with Claude Code

Capture
  on        Records       coding tools -> ~/.briefcase/receiver/records.jsonl; Python apps -> the console (stderr), not kept

4 on · 0 off · 0 need attention
Next: briefcase-ai features  # see what else briefcase can do
```

After that first run, a bare `briefcase-ai` prints the command list and the status screen
instead of the guide. `briefcase-ai setup` runs the guide again whenever you want it.

The other two answers:

- **"I'm building an app that calls LLMs"** shows `briefcase-ai exec -- python your_app.py`
  and offers a dry run on a five-line program, which prints one record and writes nothing:

```text
Step 1 of 2 · Capture a Python app without changing it
  briefcase-ai exec -- python your_app.py
Each OpenAI, Anthropic, Gemini and Bedrock call, and each LangChain, LlamaIndex, CrewAI
or AutoGen run, becomes one record: model, tokens, latency, status. Prompt and response
text is not recorded unless you add --capture-content.

A dry run starts this 5-line program through `briefcase-ai exec` and prints its one record
to the terminal. Nothing is written and nothing leaves this machine:
    import briefcase

    @briefcase.capture
    def greet(name):
        return 'hello ' + name

    greet('world')
Run it now? [Y/n] y

{"schema_version": "1.1", "decision_id": "d87556ce-16c5-4bb0-8b5b-91cb60626f1e", "decision_type": "greet", "function_name": "greet", "inputs": {"args": "('world',)"}, "outputs": {"result": "'hello world'"}, "started_at": "2026-11-21T23:19:59.131808+00:00", "ended_at": "2026-11-21T23:19:59.131830+00:00", "execution_time_ms": 0.002}

The JSON line above is the decision record for greet('world').

Step 2 of 2 · Keep the records
Records print to stderr until you choose a destination:
  briefcase-ai exec --to runs.jsonl -- python your_app.py     # a file
  export BRIEFCASE_EXPORTER=https://collector.example.com/v1/decisions   # your endpoint
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  # an OTLP collector
To record your own functions as well, see: briefcase-ai features capture-decorator
```

- **"I run a team or platform"** prints the receiver commands, then offers the rollout pack
  (`briefcase-ai connect --managed`) and policy (`briefcase-ai policy init`) as real steps. Each asks
  first (default no) and `policy init` shows its plan and asks again before writing. A step whose
  command is not in the version you installed says "Coming" instead:

```text
Step 1 only prints commands. The steps after it offer to run the real command;
each one shows what it would write and asks first.

Step 1 of 4 · Collect records in one place
  On a shared host:   briefcase-ai receive --host 0.0.0.0 --token <secret> --to <destination>
  On each machine:    briefcase-ai connect --endpoint https://<your-receiver-host> \
                          --headers "Authorization=Bearer <secret>"
  Docs: docs/integrations/README.md (The receiver)

Step 2 of 4 · Roll out to every developer machine
  briefcase-ai connect --managed --out ./briefcase-rollout --endpoint https://<your-receiver-host>
  Docs: docs/features/managed-rollout.md
Writes the files your device management deploys (managed settings, hooks) into one
directory. Nothing outside it is touched, and nothing is installed on this machine.
Write a rollout pack to ./briefcase-rollout now? [y/N]

Step 3 of 4 · Decide what coding tools may run
  briefcase-ai policy init
  Docs: docs/features/policy-hooks.md
Writes ~/.briefcase/policy.toml and a hook in each coding tool. It lists every file and
asks before writing; undo with `briefcase-ai policy off`. This affects this machine only.
Set up policy on this machine now? [y/N]

Step 4 of 4 · Cap spend
  Coming: there is no command for this in the version installed here.
  Today:  briefcase-ai features controls-gateway   # the in-app spend and quota gate that ships today

Everything that limits or stops things: briefcase-ai features --verb enforce
```

### `briefcase-ai status`

One screen: what is on, what is off, and one suggested next command.

```text
$ briefcase-ai status
briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  on        Coding tools  Claude Code connected
  on        Receiver      running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected
  on        MCP server    registered with Claude Code

Capture
  on        Records       coding tools -> ~/.briefcase/receiver/records.jsonl; Python apps -> the console (stderr), not kept

4 on · 0 off · 0 need attention
Next: briefcase-ai features  # see what else briefcase can do
```

On a machine where nothing is set up yet:

```text
$ briefcase-ai status
briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; detected on this machine: Claude Code
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase-ai connect  # a coding tool is installed but not connected
```

`--json` prints the same data for scripts (first row shown, the rest cut):

```text
$ briefcase-ai status --json
{
  "schema_version": 1,
  "version": "4.4.0",
  "home": "~/.briefcase",
  "items": [
    {
      "id": "connect",
      "title": "Coding tools",
      "group": "connect",
      "state": "on",
      "detail": "Claude Code connected",
      "next": null,
      "source": "core",
      "data": {
        "connected": [
          "claude-code"
        ],
        "drifted": [],
        "detected_not_connected": [],
        "capture_content": [],
        "endpoints": [
          "http://127.0.0.1:4318"
        ]
      }
    },
    ...
  ],
  "summary": {
    "on": 4,
    "off": 0,
    "attention": 0
  },
  "next": {
    "command": "briefcase-ai features",
    "reason": "see what else briefcase can do"
  },
  "problems": []
}
```

### `briefcase-ai features`

Every feature, grouped by Connect, Capture, Enforce, Verify and Review, with the first
command to run. `--verb` shows one group, `--json` is for scripts:

```text
$ briefcase-ai features --verb enforce
briefcase-ai features · 2 features · 1 group

Enforce (2)
  Budget and quota gateway  [experimental]  id: controls-gateway
      Cap spend and request rates per tenant before a model call goes out.
      $ briefcase-ai features controls-gateway
  Guardrail checks  [experimental]  id: guardrails
      Decide allow or deny for an agent action, and fail closed on errors.
      $ briefcase-ai features guardrails

Next: briefcase-ai features <id>  # details, docs link and whether it is on here
```

Name a feature to see its documentation link and whether it is on for you right now:

```text
$ briefcase-ai features receive
Local receiver  [experimental]  Connect
Turn what your coding tools send into decision records on your own machine.

  Start with:    briefcase-ai receive --detach
  Needs:         pip install 'briefcase-ai[receiver]'
  Active here:   yes (running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected)
  Docs:          docs/integrations/README.md#the-receiver
                 https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/docs/integrations/README.md#the-receiver
  Verified with: Real HTTP server exercised over loopback in tests, with synthetic OTLP payloads built from the tools' documented event names. No capture from a real tool session was replayed.

Next: briefcase-ai status  # see what is on across this machine
```

The same catalog as a page: [catalog.md](catalog.md).

### `briefcase-ai --help`

Commands are listed under the same five verbs:

```text
$ briefcase-ai --help
usage: briefcase [-h] [--server SERVER] [--repo REPO] [--dry-run] [--debug] [--no-input]
                 <command> ...

Briefcase: record what AI coding tools and LLM apps do, limit it, and prove the records are
intact. Run `briefcase-ai` with no arguments for a guided setup.

Start here: find your way around
  setup       Guided setup: say what you are doing, get the two or three steps for it.
  status      What is on, what is off, and what to do next.
  features    Browse everything briefcase can do, grouped by Connect, Capture, Enforce, Verify,
              Review.
  doctor      Diagnose the install and print the compatibility matrix.
  completion  Print a shell completion script (bash, zsh or fish).

Connect: wire tools, apps and destinations
  connect     Configure coding tools (Claude Code, Codex, Cursor, Copilot, ...) to report to
              briefcase.
  exec        Run a command with decision capture enabled (no code changes).
  hook        Tool hook entry points (installed by `briefcase-ai connect`).
  mcp         Install the briefcase MCP server into coding tools.
  receive     Run a local OTLP receiver that turns coding-tool telemetry into decision records.
  (6 features in this group: briefcase-ai features --verb connect)

Capture: what gets recorded
  (7 features in this group: briefcase-ai features --verb capture)

Enforce: stop or limit things
  (2 features in this group: briefcase-ai features --verb enforce)

Verify: prove records are intact and reproducible
  (5 features in this group: briefcase-ai features --verb verify)

Review: look at what happened
  (4 features in this group: briefcase-ai features --verb review)

Evaluate: datasets, secrets and evaluation runs
  dataset     Register and list datasets.
  secret      Store and list secrets.
  run         Submit and manage evaluation runs.
  engine      Managed engine access and legacy migration.

Global options:
  -h, --help       show this help message and exit
  --server SERVER  oci-jj-server gRPC endpoint (default $OCI_JJ_SERVER).
  --repo REPO      oci-jj repository / image family (default $OCI_JJ_REPO).
  --dry-run        Print the resolved oci-jj command for a run submit instead of executing it.
  --debug          Show tracebacks and name any command module that failed to load (or set
                   BRIEFCASE_DEBUG=1).
  --no-input       Never prompt; a bare `briefcase-ai` prints status and this list (or set
                   BRIEFCASE_NO_INPUT=1).

Run `briefcase-ai <command> --help` for one command, `briefcase-ai status` to see where you are,
and `briefcase-ai features` for everything briefcase can do.
```

### `briefcase-ai completion`

```bash
eval "$(briefcase-ai completion bash)"                               # this shell only
briefcase-ai completion zsh > ~/.zfunc/_briefcase_ai                 # then: fpath+=(~/.zfunc); compinit
briefcase-ai completion fish > ~/.config/fish/completions/briefcase-ai.fish
```

```text
$ briefcase-ai completion bash | head -n 12
# bash completion for briefcase-ai. Generated by `briefcase-ai completion bash`.
# Load it in this shell with:  eval "$(briefcase-ai completion bash)"
_briefcase_ai() {
    local cur prev ctx word i words
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev=""
    if (( COMP_CWORD > 0 )); then prev="${COMP_WORDS[COMP_CWORD-1]}"; fi
    ctx="briefcase-ai"
    for (( i = 1; i < COMP_CWORD; i++ )); do
        word="${COMP_WORDS[i]}"
        # after a literal -- the rest belongs to another program (briefcase-ai exec -- ...)
```

## What it writes or sends, and where

| Command | Writes | Sends |
| --- | --- | --- |
| `briefcase-ai status`, `briefcase-ai features`, `briefcase-ai --help`, `briefcase-ai completion` | nothing | nothing; `status` asks `http://127.0.0.1:4318/healthz` (or the endpoint your tools were connected to) whether a receiver is up |
| `briefcase-ai` / `briefcase-ai setup`, once you finished or skipped the guide | `~/.briefcase/setup.json` | nothing |
| the guide's connect step | exactly what `briefcase-ai connect` writes, after its own diff and confirmation | nothing |
| the guide's receiver step | exactly what `briefcase-ai receive --detach` writes under `~/.briefcase/receiver/` | nothing |
| the guide's dry run | nothing; one record is printed to the terminal | nothing |

`setup.json` holds only the answer and a timestamp:

```text
$ cat ~/.briefcase/setup.json
{
  "version": 1,
  "outcome": "finished",
  "persona": "app",
  "at": "2026-11-21T23:19:59+0000"
}
```

The guide is remembered only when you finished or skipped it. Closing the terminal or
pressing Ctrl-C part-way leaves no file, so the guide is offered again next time.

## Defaults and how to change them

| Default | Change it with |
| --- | --- |
| The guide runs on a bare `briefcase-ai` the first time, on a terminal | `briefcase-ai --no-input`, or `BRIEFCASE_NO_INPUT=1`, never prompts |
| State lives in `~/.briefcase` | `BRIEFCASE_HOME=/some/dir` |
| Colour on a terminal | `NO_COLOR=1` turns it off; output that is piped is never coloured |
| Errors are one line plus a `Fix:` line | `briefcase-ai --debug <command>` (or `BRIEFCASE_DEBUG=1`) shows the traceback |
| `status` exits 0 whatever it finds | use `briefcase-ai doctor --connect --strict` to fail a script on problems |

Without a terminal (CI, a pipe, a script) a bare `briefcase-ai` never asks anything. It prints
the command list and the status screen and exits 0:

```text
$ briefcase-ai < /dev/null
Start here: find your way around
  setup       Guided setup: say what you are doing, get the two or three steps for it.
...
briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; no supported coding tool found on this machine
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase-ai exec -- python your_app.py  # no coding tool found here; capture a Python app instead
[exit 0]
```

## How to turn it off or undo

- Skip the guide: answer `4` (or `s`), or pass `--no-input`.
- See the guide again on a bare `briefcase-ai`: `rm ~/.briefcase/setup.json`.
- Undo what the guide's steps did with the commands they came from:

```text
$ briefcase-ai connect --undo
Undoing:
  ~/.claude/settings.json: removed env.OTEL_EXPORTER_OTLP_ENDPOINT
  ~/.claude/settings.json: removed env.OTEL_EXPORTER_OTLP_PROTOCOL
  ~/.claude/settings.json: removed env.OTEL_LOGS_EXPORTER
  ~/.claude/settings.json: removed env.OTEL_METRICS_EXPORTER
  ~/.claude/settings.json: removed env.CLAUDE_CODE_ENABLE_TELEMETRY
  ~/.claude/settings.json: deleted (briefcase created it and nothing else is in it)
  to remove the MCP registration, run: claude mcp remove --scope user briefcase
Done. Backups of every file connect touched are next to it (*.briefcase-bak-*).
$ briefcase-ai receive --stop
receiver stopped (pid 10202)
$ rm ~/.briefcase/setup.json
$ briefcase-ai status
briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; detected on this machine: Claude Code
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase-ai connect  # a coding tool is installed but not connected
```

- Remove completion: delete the file you wrote, or the `eval` line from your shell profile.

## Limits and failure modes

- `status` reports what briefcase itself set up and recorded in `~/.briefcase`. A tool you
  configured by hand shows as "detected", not "connected", and an MCP server removed inside
  the tool still shows as registered until `briefcase-ai connect --undo`.
- The "Records" row describes the destination a Python program would get from the
  environment `status` runs in (`BRIEFCASE_EXPORTER`, `OTEL_EXPORTER_OTLP_*`). A program
  started with a different environment, or one that calls `briefcase.observe("...")` itself,
  can send elsewhere. Record counts come from the receiver; a Python program's own counters
  (`briefcase.export_stats()`) live inside that program and are not shown.
- With `BRIEFCASE_EXPORTER=<dir>/runs.jsonl`, `status` creates `<dir>` if it is missing,
  because it asks the SDK's own resolver, which does so.
- A feature shows "not tracked by `briefcase-ai status`" when nothing reports its state. That
  covers the features you use from Python.
- A command module that fails to load is skipped so the rest keeps working; `--debug` names it:

```text
$ briefcase-ai --debug status   # with a deliberately broken briefcase/cli/zz_broken_commands.py
warning: skipped briefcase/cli/zz_broken_commands.py (import failed: RuntimeError: boom)
briefcase-ai status  (briefcase-ai 4.4.0, state in ~/.briefcase)
```

- Completion scripts are generated when you run the command. Re-run it after upgrading.
  The bash script is tested by loading it in bash. The zsh and fish scripts have not been
  loaded in those shells yet.
- The connect step shown above was run against a stand-in `claude` executable in a temp
  HOME, not a real Claude Code install. What `connect` itself was verified against is in
  [`docs/integrations/README.md`](../integrations/README.md).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `briefcase-ai` prints status instead of the guide | The guide already ran. Run `briefcase-ai setup`. |
| `briefcase-ai` asks nothing in CI and you expected it to | That is intended without a terminal. Run the steps directly: `briefcase-ai connect --yes`, `briefcase-ai receive --detach`. |
| "Coding tools: none connected; no supported coding tool found" but the tool is installed | Run `briefcase-ai connect list` to see what was looked for, then `briefcase-ai connect <tool>`. |
| "Coding tools" shows `attention` | A setting connect wrote is gone. Run the command on the `Next:` line (`briefcase-ai connect <tool>`). |
| "Receiver: not running; connected tools have nowhere to send" | `briefcase-ai receive --detach` |
| Records never arrive | `briefcase-ai status`, then `briefcase-ai doctor --connect` for the per-tool checks. |
| "Records" shows `attention` | `BRIEFCASE_EXPORTER` has a value briefcase cannot use; the row says which values work. |
| A command is missing from `briefcase-ai --help` | `briefcase-ai --debug --help` names any command module that failed to load, and why. |
| `error: no feature with id ...` | The `Fix:` line has the closest id; `briefcase-ai features` lists them all. |
| Tab completion offers old commands | Re-generate: `eval "$(briefcase-ai completion bash)"`. |

An error always has this shape, with the command that fixes it:

```text
$ briefcase-ai features recieve
error: no feature with id 'recieve'
Fix: briefcase-ai features receive
[exit 2]
```

All output on this page was captured from real runs in a temporary HOME (shown as `~`), on
Linux with Python 3.13, briefcase-ai 4.4.0 from this repository.
