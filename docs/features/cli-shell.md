# Guided setup, status and the feature catalog

`briefcase` has many commands. These are the three places to start from, so you never have
to read source to find one: a guided setup, a status screen, and a catalog of every feature.

## The one command

```bash
briefcase
```

The first time you run it on a terminal it asks one question and walks you through two or
three steps for your answer. Here is the "AI coding tools" path, start to finish:

```text
briefcase setup
One question, then two or three steps. Nothing is written without asking first.

What are you setting up?
  1) I use AI coding tools (Claude Code, Cursor, Codex…)
  2) I'm building an app that calls LLMs
  3) I run a team or platform
  4) Skip for now
Choice [1/2/3/4]: 1

Step 1 of 2 · Connect your coding tools
This is `briefcase connect`: it lists every file it would change, shows the diff and
asks once. Prompt and tool content stays off. Undo any time: briefcase connect --undo
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
  run    claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-mcp
         Register the briefcase MCP server with Claude Code

Write 1 file(s) and run 1 command(s)? [Y/n] y
  wrote  ~/.claude/settings.json
  ran    claude mcp add --scope user briefcase -- uvx --from 'briefcase-ai[mcp]' briefcase-mcp

Undo any time with: briefcase connect --undo

Step 2 of 2 · Start the receiver
The receiver listens on 127.0.0.1:4318 and turns what the tools send into records in
~/.briefcase/receiver/records.jsonl. Prompts, responses and paths are dropped.
Start it in the background now? [Y/n] y

receiver started (pid 10202) on http://127.0.0.1:4318
  records: ~/.briefcase/receiver/records.jsonl
  log:     ~/.briefcase/receiver/receiver.log
  stop:    briefcase receive --stop
Restart the connected tools so they pick up the new settings.

briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  on        Coding tools  Claude Code connected
  on        Receiver      running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected
  on        MCP server    registered with Claude Code

Capture
  on        Records       coding tools -> ~/.briefcase/receiver/records.jsonl; Python apps -> the console (stderr), not kept

4 on · 0 off · 0 need attention
Next: briefcase features  # see what else briefcase can do
```

After that first run, a bare `briefcase` prints the command list and the status screen
instead of the guide. `briefcase setup` runs the guide again whenever you want it.

The other two answers:

- **"I'm building an app that calls LLMs"** shows `briefcase exec -- python your_app.py`
  and offers a dry run on a five-line program, which prints one record and writes nothing:

```text
Step 1 of 2 · Capture a Python app without changing it
  briefcase exec -- python your_app.py
Each OpenAI, Anthropic, Gemini and Bedrock call, and each LangChain, LlamaIndex, CrewAI
or AutoGen run, becomes one record: model, tokens, latency, status. Prompt and response
text is not recorded unless you add --capture-content.

A dry run starts this 5-line program through `briefcase exec` and prints its one record
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
  briefcase exec --to runs.jsonl -- python your_app.py     # a file
  export BRIEFCASE_EXPORTER=https://collector.example.com/v1/decisions   # your endpoint
  export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318  # an OTLP collector
To record your own functions as well, see: briefcase features capture-decorator
```

- **"I run a team or platform"** prints the receiver commands, then offers the rollout pack
  (`briefcase connect --managed`) and policy (`briefcase policy init`) as real steps. Each asks
  first (default no) and `policy init` shows its plan and asks again before writing. A step whose
  command is not in the version you installed says "Coming" instead:

```text
Step 1 only prints commands. The steps after it offer to run the real command;
each one shows what it would write and asks first.

Step 1 of 4 · Collect records in one place
  On a shared host:   briefcase receive --host 0.0.0.0 --token <secret> --to <destination>
  On each machine:    briefcase connect --endpoint https://<your-receiver-host> \
                          --headers "Authorization=Bearer <secret>"
  Docs: docs/integrations/README.md (The receiver)

Step 2 of 4 · Roll out to every developer machine
  briefcase connect --managed --out ./briefcase-rollout --endpoint https://<your-receiver-host>
  Docs: docs/features/managed-rollout.md
Writes the files your device management deploys (managed settings, hooks) into one
directory. Nothing outside it is touched, and nothing is installed on this machine.
Write a rollout pack to ./briefcase-rollout now? [y/N]

Step 3 of 4 · Decide what coding tools may run
  briefcase policy init
  Docs: docs/features/policy-hooks.md
Writes ~/.briefcase/policy.toml and a hook in each coding tool. It lists every file and
asks before writing; undo with `briefcase policy off`. This affects this machine only.
Set up policy on this machine now? [y/N]

Step 4 of 4 · Cap spend
  Coming: there is no command for this in the version installed here.
  Today:  briefcase features controls-gateway   # the in-app spend and quota gate that ships today

Everything that limits or stops things: briefcase features --verb enforce
```

### `briefcase status`

One screen: what is on, what is off, and one suggested next command.

```text
$ briefcase status
briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  on        Coding tools  Claude Code connected
  on        Receiver      running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected
  on        MCP server    registered with Claude Code

Capture
  on        Records       coding tools -> ~/.briefcase/receiver/records.jsonl; Python apps -> the console (stderr), not kept

4 on · 0 off · 0 need attention
Next: briefcase features  # see what else briefcase can do
```

On a machine where nothing is set up yet:

```text
$ briefcase status
briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; detected on this machine: Claude Code
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase connect  # a coding tool is installed but not connected
```

`--json` prints the same data for scripts (first row shown, the rest cut):

```text
$ briefcase status --json
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
    "command": "briefcase features",
    "reason": "see what else briefcase can do"
  },
  "problems": []
}
```

### `briefcase features`

Every feature, grouped by Connect, Capture, Enforce, Verify and Review, with the first
command to run. `--verb` shows one group, `--json` is for scripts:

```text
$ briefcase features --verb enforce
briefcase features · 2 features · 1 group

Enforce (2)
  Budget and quota gateway  [experimental]  id: controls-gateway
      Cap spend and request rates per tenant before a model call goes out.
      $ briefcase features controls-gateway
  Guardrail checks  [experimental]  id: guardrails
      Decide allow or deny for an agent action, and fail closed on errors.
      $ briefcase features guardrails

Next: briefcase features <id>  # details, docs link and whether it is on here
```

Name a feature to see its documentation link and whether it is on for you right now:

```text
$ briefcase features receive
Local receiver  [experimental]  Connect
Turn what your coding tools send into decision records on your own machine.

  Start with:    briefcase receive --detach
  Needs:         pip install 'briefcase-ai[receiver]'
  Active here:   yes (running on http://127.0.0.1:4318 (pid 10202); 0 requests, 0 records, 0 rejected)
  Docs:          docs/integrations/README.md#the-receiver
                 https://github.com/briefcasebrain/briefcase-ai-sdk/blob/main/docs/integrations/README.md#the-receiver
  Verified with: Real HTTP server exercised over loopback in tests, with synthetic OTLP payloads built from the tools' documented event names. No capture from a real tool session was replayed.

Next: briefcase status  # see what is on across this machine
```

The same catalog as a page: [catalog.md](catalog.md).

### `briefcase --help`

Commands are listed under the same five verbs:

```text
$ briefcase --help
usage: briefcase [-h] [--server SERVER] [--repo REPO] [--dry-run] [--debug] [--no-input]
                 <command> ...

Briefcase: record what AI coding tools and LLM apps do, limit it, and prove the records are
intact. Run `briefcase` with no arguments for a guided setup.

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
  hook        Tool hook entry points (installed by `briefcase connect`).
  mcp         Install the briefcase MCP server into coding tools.
  receive     Run a local OTLP receiver that turns coding-tool telemetry into decision records.
  (6 features in this group: briefcase features --verb connect)

Capture: what gets recorded
  (7 features in this group: briefcase features --verb capture)

Enforce: stop or limit things
  (2 features in this group: briefcase features --verb enforce)

Verify: prove records are intact and reproducible
  (5 features in this group: briefcase features --verb verify)

Review: look at what happened
  (4 features in this group: briefcase features --verb review)

Evaluate: datasets, secrets and evaluation runs
  dataset     Register and list datasets.
  secret      Store and list secrets.
  run         Submit and manage evaluation runs.
  stack       Manage the bundled engine stack (docker compose).

Global options:
  -h, --help       show this help message and exit
  --server SERVER  oci-jj-server gRPC endpoint (default $OCI_JJ_SERVER).
  --repo REPO      oci-jj repository / image family (default $OCI_JJ_REPO).
  --dry-run        Print the resolved oci-jj command for a run submit instead of executing it.
  --debug          Show tracebacks and name any command module that failed to load (or set
                   BRIEFCASE_DEBUG=1).
  --no-input       Never prompt; a bare `briefcase` prints status and this list (or set
                   BRIEFCASE_NO_INPUT=1).

Run `briefcase <command> --help` for one command, `briefcase status` to see where you are,
and `briefcase features` for everything briefcase can do.
```

### `briefcase completion`

```bash
eval "$(briefcase completion bash)"                               # this shell only
briefcase completion zsh > ~/.zfunc/_briefcase                    # then: fpath+=(~/.zfunc); compinit
briefcase completion fish > ~/.config/fish/completions/briefcase.fish
```

```text
$ briefcase completion bash | head -n 12
# bash completion for briefcase. Generated by `briefcase completion bash`.
# Load it in this shell with:  eval "$(briefcase completion bash)"
_briefcase() {
    local cur prev ctx word i words
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev=""
    if (( COMP_CWORD > 0 )); then prev="${COMP_WORDS[COMP_CWORD-1]}"; fi
    ctx="briefcase"
    for (( i = 1; i < COMP_CWORD; i++ )); do
        word="${COMP_WORDS[i]}"
        # after a literal -- the rest belongs to another program (briefcase exec -- ...)
```

## What it writes or sends, and where

| Command | Writes | Sends |
| --- | --- | --- |
| `briefcase status`, `briefcase features`, `briefcase --help`, `briefcase completion` | nothing | nothing; `status` asks `http://127.0.0.1:4318/healthz` (or the endpoint your tools were connected to) whether a receiver is up |
| `briefcase` / `briefcase setup`, once you finished or skipped the guide | `~/.briefcase/setup.json` | nothing |
| the guide's connect step | exactly what `briefcase connect` writes, after its own diff and confirmation | nothing |
| the guide's receiver step | exactly what `briefcase receive --detach` writes under `~/.briefcase/receiver/` | nothing |
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
| The guide runs on a bare `briefcase` the first time, on a terminal | `briefcase --no-input`, or `BRIEFCASE_NO_INPUT=1`, never prompts |
| State lives in `~/.briefcase` | `BRIEFCASE_HOME=/some/dir` |
| Colour on a terminal | `NO_COLOR=1` turns it off; output that is piped is never coloured |
| Errors are one line plus a `Fix:` line | `briefcase --debug <command>` (or `BRIEFCASE_DEBUG=1`) shows the traceback |
| `status` exits 0 whatever it finds | use `briefcase doctor --connect --strict` to fail a script on problems |

Without a terminal (CI, a pipe, a script) a bare `briefcase` never asks anything. It prints
the command list and the status screen and exits 0:

```text
$ briefcase < /dev/null
Start here: find your way around
  setup       Guided setup: say what you are doing, get the two or three steps for it.
...
briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; no supported coding tool found on this machine
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase exec -- python your_app.py  # no coding tool found here; capture a Python app instead
[exit 0]
```

## How to turn it off or undo

- Skip the guide: answer `4` (or `s`), or pass `--no-input`.
- See the guide again on a bare `briefcase`: `rm ~/.briefcase/setup.json`.
- Undo what the guide's steps did with the commands they came from:

```text
$ briefcase connect --undo
Undoing:
  ~/.claude/settings.json: removed env.OTEL_EXPORTER_OTLP_ENDPOINT
  ~/.claude/settings.json: removed env.OTEL_EXPORTER_OTLP_PROTOCOL
  ~/.claude/settings.json: removed env.OTEL_LOGS_EXPORTER
  ~/.claude/settings.json: removed env.OTEL_METRICS_EXPORTER
  ~/.claude/settings.json: removed env.CLAUDE_CODE_ENABLE_TELEMETRY
  ~/.claude/settings.json: deleted (briefcase created it and nothing else is in it)
  to remove the MCP registration, run: claude mcp remove --scope user briefcase
Done. Backups of every file connect touched are next to it (*.briefcase-bak-*).
$ briefcase receive --stop
receiver stopped (pid 10202)
$ rm ~/.briefcase/setup.json
$ briefcase status
briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)

Connect
  off       Coding tools  none connected; detected on this machine: Claude Code
  off       Receiver      not running
  off       MCP server    not registered with any tool

Capture
  off       Records       Python apps -> the console (stderr), not kept

0 on · 4 off · 0 need attention
Next: briefcase connect  # a coding tool is installed but not connected
```

- Remove completion: delete the file you wrote, or the `eval` line from your shell profile.

## Limits and failure modes

- `status` reports what briefcase itself set up and recorded in `~/.briefcase`. A tool you
  configured by hand shows as "detected", not "connected", and an MCP server removed inside
  the tool still shows as registered until `briefcase connect --undo`.
- The "Records" row describes the destination a Python program would get from the
  environment `status` runs in (`BRIEFCASE_EXPORTER`, `OTEL_EXPORTER_OTLP_*`). A program
  started with a different environment, or one that calls `briefcase.observe("...")` itself,
  can send elsewhere. Record counts come from the receiver; a Python program's own counters
  (`briefcase.export_stats()`) live inside that program and are not shown.
- With `BRIEFCASE_EXPORTER=<dir>/runs.jsonl`, `status` creates `<dir>` if it is missing,
  because it asks the SDK's own resolver, which does so.
- A feature shows "not tracked by `briefcase status`" when nothing reports its state. That
  covers the features you use from Python.
- A command module that fails to load is skipped so the rest keeps working; `--debug` names it:

```text
$ briefcase --debug status   # with a deliberately broken briefcase/cli/zz_broken_commands.py
warning: skipped briefcase/cli/zz_broken_commands.py (import failed: RuntimeError: boom)
briefcase status  (briefcase-ai 4.4.0, state in ~/.briefcase)
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
| `briefcase` prints status instead of the guide | The guide already ran. Run `briefcase setup`. |
| `briefcase` asks nothing in CI and you expected it to | That is intended without a terminal. Run the steps directly: `briefcase connect --yes`, `briefcase receive --detach`. |
| "Coding tools: none connected; no supported coding tool found" but the tool is installed | Run `briefcase connect list` to see what was looked for, then `briefcase connect <tool>`. |
| "Coding tools" shows `attention` | A setting connect wrote is gone. Run the command on the `Next:` line (`briefcase connect <tool>`). |
| "Receiver: not running; connected tools have nowhere to send" | `briefcase receive --detach` |
| Records never arrive | `briefcase status`, then `briefcase doctor --connect` for the per-tool checks. |
| "Records" shows `attention` | `BRIEFCASE_EXPORTER` has a value briefcase cannot use; the row says which values work. |
| A command is missing from `briefcase --help` | `briefcase --debug --help` names any command module that failed to load, and why. |
| `error: no feature with id ...` | The `Fix:` line has the closest id; `briefcase features` lists them all. |
| Tab completion offers old commands | Re-generate: `eval "$(briefcase completion bash)"`. |

An error always has this shape, with the command that fixes it:

```text
$ briefcase features recieve
error: no feature with id 'recieve'
Fix: briefcase features receive
[exit 2]
```

All output on this page was captured from real runs in a temporary HOME (shown as `~`), on
Linux with Python 3.13, briefcase-ai 4.4.0 from this repository.
