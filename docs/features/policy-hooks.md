# Policy for coding tools

Stop a coding agent before it reads your SSH keys, deletes your home directory, force-pushes
`main` or pipes a download into a shell. A hook in Claude Code, Cursor and Codex CLI checks each
shell command, file access, MCP tool call and web request against `~/.briefcase/policy.toml`
before it runs, and answers allow, ask or deny with a reason a person can read.

Status: **experimental**. Claude Code 2.1.330 was driven end to end (see [How this was
verified](#how-this-was-verified)); the Cursor and Codex hooks are built from their documentation
and were never run against those tools.

## The one command

```bash
briefcase-ai policy init          # or: briefcase-ai connect --enforce (telemetry + policy in one go)
```

Real output, from a run in a temp `HOME` (paths shortened to `~`):

```text
$ briefcase-ai policy init claude-code --yes
Policy: create ~/.briefcase/policy.toml (preset guarded)

== Claude Code policy hooks ==
  create ~/.claude/settings.json
         Run the briefcase-ai policy before each tool call (PreToolUse)
         note: a denied or asked call shows the rule's reason in Claude Code; allowed calls go through Claude Code's normal permission prompts
    --- /dev/null
    +++ ~/.claude/settings.json
    @@ -0,0 +1,16 @@
    +{
    +  "hooks": {
    +    "PreToolUse": [
    +      {
    +        "matcher": "^(Bash|PowerShell|Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|WebFetch|WebSearch)$|^mcp__",
    +        "hooks": [
    +          {
    +            "type": "command",
    +            "command": "/usr/local/bin/python -I -S -c 'import runpy; runpy.run_path(\"/home/user/wt/sdk-policy/briefcase/policy/hook_main.py\", run_name=\"__main__\")' claude-code --enforce",
    +            "timeout": 10
    +          }
    +        ]
    +      }
    +    ]
    +  }
    +}

  wrote  ~/.briefcase/policy.toml
  wrote  ~/.claude/settings.json

Undo the hooks any time with: briefcase-ai policy off
Next: briefcase-ai policy test -- rm -rf /
```

Without tool names it installs for every tool it detects (Claude Code, Cursor, Codex CLI). It
shows the diff and asks once; `--yes` skips the question, `--dry-run` writes nothing.

### See what the policy would do, and why

`briefcase-ai policy test` runs nothing. It shows how the command was read, which rule decided,
and what each tool will actually do with that decision:

```text
$ briefcase-ai policy test -- "FOO=1 bash -c 'cd / && rm -rf --no-preserve-root \$TARGET'"
DENY   preset rule shell.rm-root
       Recursive delete of a root, home or system directory.

Tested: FOO=1 bash -c 'cd / && rm -rf --no-preserve-root $TARGET'

How it was read
  command: bash -c 'cd / && rm -rf --no-preserve-root $TARGET'
      allow  (no rule matches)
  command: cd / (via bash -c)
      allow  (no rule matches)
  command: rm -rf --no-preserve-root $TARGET (via bash -c)
      deny   shell.rm-root  [rm --no-preserve-root]

What each tool would do
  Claude Code  deny
  Cursor       deny
  Codex CLI    deny

Policy: ~/.briefcase/policy.toml
```

```text
$ briefcase-ai policy test -- npm install left-pad
ASK    preset rule pkg.install
       Installs packages, which runs code from the internet on this machine.

Tested: npm install left-pad

How it was read
  command: npm install left-pad
      ask    pkg.install  [matches `npm install|i|add|ci|exec`]

What each tool would do
  Claude Code  ask
  Cursor       ask
  Codex CLI    deny   (cannot ask from PreToolUse; ask_fallback = deny)

Policy: ~/.briefcase/policy.toml
```

```text
$ briefcase-ai policy test --file .env.production
DENY   preset rule secrets.files
       This path matches the built-in list of credential files (SSH keys, cloud credentials, .env). To allow one path, add an allow rule for it above the preset in your policy.

Tested: read .env.production

How it was read
  read: .env.production
      deny   secrets.files  [matches the rule's paths]

What each tool would do
  Claude Code  deny
  Cursor       deny
  Codex CLI    not enforced   (no file-read hook; Codex reads files through shell commands, which are checked)

Policy: ~/.briefcase/policy.toml
```

Other forms: `--mcp server.tool`, `--url https://...`, `--file PATH --write`, `--preset strict`
(try a preset without changing your file), `--json`, and `--expect deny` (exit 1 otherwise, for CI).

## What it writes or sends, and where

| What | Where | When |
| --- | --- | --- |
| The policy | `~/.briefcase/policy.toml` (mode 600) | `policy init`; never overwritten without `--force` (a backup is kept) |
| Claude Code hook | `hooks.PreToolUse` in `~/.claude/settings.json` | `policy init` |
| Cursor hooks | `beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`, `preToolUse` in `~/.cursor/hooks.json` | `policy init` |
| Codex hook | `hooks.PreToolUse` in `~/.codex/hooks.json` | `policy init` |
| Decision log | `~/.briefcase/policy/decisions.jsonl` (append-only, mode 600, rotated once at 5 MiB) | every judged tool call |
| Hook health | `~/.briefcase/policy/health.json` | when the hook fails or the policy is invalid |
| Decision record | `POST <receiver>/v1/policy/decisions` -> `decision_type = "policy.tool_call"` | after each decision, only if a receiver was configured or started here |

Every tool-file edit goes through the same machinery as `briefcase-ai connect`: key-level, with a
diff, one confirmation, a timestamped backup and a journal entry. `--scope project` writes
`.briefcase/policy.toml`, `.claude/settings.json`, `.cursor/hooks.json` and `.codex/hooks.json`
in the current project instead, with the portable `briefcase-ai hook <tool> --enforce` command.

The hook decides in its own process: no network hop. After it has printed its answer it appends
one line to the log and, when a receiver is expected, hands the record to a detached child that
posts it; the hook does not wait for it.

**Content.** The command, path or URL is not written to the log or sent anywhere. The log and
the record carry an HMAC-SHA256 of it (key in `~/.briefcase/policy/hash.key`, never sent), the
rule id, its source and the effect. `briefcase-ai policy init --capture-content` adds the text to the
log and the record; the receiver keeps it only if it also runs with `--keep-content`.

```text
$ briefcase-ai policy log
TIME                 TOOL         KIND   EFFECT RULE                       WHAT
2026-11-22T04:57:15  claude-code  shell  deny   secrets.files              #72d457a012d7
2026-11-22T04:57:15  claude-code  shell  allow  -                          #9cd74cde003d
2026-11-22T04:57:15  claude-code  shell  ask    pkg.install                #d18892524527

WHAT is a keyed hash: commands and paths are not logged unless the hooks were installed with --capture-content.
Next: briefcase-ai policy test -- <command>   (to see why a command gets its result)
```

## Defaults and how to change them

The file `policy init` writes is commented; the important settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `preset` | `guarded` | `observe`: no rules, log only. `guarded`: deny secret files, recursive deletes of `/`, `~` or system directories, disk writes, `curl \| sh`, force-push to protected branches; ask for package installs, force-push elsewhere and commands that can send data out. `strict`: guarded, plus deny instead of ask for sending data out, and ask for `sudo`, writes outside the project, every MCP tool and every web request. |
| `mode` | `enforce` (`observe` for that preset) | `observe` logs what would happen and blocks nothing |
| `fail_closed` | `false` | see [Failure modes](#limits-and-failure-modes) |
| `on_unparseable` | `ask` (`allow` observe, `deny` strict) | a shell line that cannot be analysed (eval of a variable, text piped into a shell, a computed command name) |
| `ask_fallback` | `deny` | what an `ask` becomes where the tool cannot ask (Codex; Cursor file reads and writes) |
| `protect_self` | `true` | deny the agent's writes to the policy files, `~/.briefcase`, the tools' hook settings, and commands like `briefcase-ai policy off` or `claude --safe-mode` |
| `protected_branches` | `main`, `master`, `trunk`, `production`, `prod`, `release`, `release/*` | used by `git.force-push-protected` |
| `disable` | `[]` | preset rule ids to switch off (`briefcase-ai policy check` lists them) |

Your own rules go above the preset's and the first match decides:

```toml
[[rule]]
id = "no-prod-db"
tool = "shell"            # shell | read | write | file (read+write) | mcp | web
effect = "deny"           # allow | ask | deny
command = ["psql --host=prod*", "kubectl delete"]
reason = "Production changes go through the runbook."

[[rule]]
id = "test-fixtures"
tool = "file"
effect = "allow"
paths = ["tests/fixtures/.env"]   # ~, ${project}, ${tmp}; no leading / or ~ = any depth

[[rule]]
id = "github-writes"
tool = "mcp"
effect = "ask"
server = "github"
name = ["create_*", "delete_*", "merge_*"]

[[rule]]
id = "no-pastebins"
tool = "web"
effect = "deny"
hosts = ["pastebin.com", "*.ngrok.io"]
```

How matching works:

- **Shell** commands are lexed like a POSIX shell and reduced to argv lists. Quoting, `&&`, `;`,
  `||`, pipes, `$(...)`, backticks, `<(...)`, here-documents, `VAR=x` prefixes, `sudo`, `env`,
  `nohup`, `timeout`, `xargs`, `find -exec`, `sh -c '...'`, `eval '...'` and `cd dir && ...` are
  all followed. A command pattern is `name|alt flag positional`: flags may appear anywhere and
  short flags match inside clusters (`-rf` matches `-r -f`); for ask and deny rules the command
  may also sit behind an unknown wrapper (`strace -f rm -rf /`).
- **Paths** are matched after `~`, `..` and symlinks are resolved. A deny or ask rule matches if
  any spelling of the path matches; an allow rule only if all do, so a symlink cannot borrow an
  allowed directory. File paths named in shell commands are checked too (`cat ~/.ssh/id_rsa`,
  `cp .env /tmp/x`, `curl -d @.env`).
- **MCP** rules match the server and tool name (globs). **Web** rules match the URL's host.

**Where the policy comes from.** Up to three files apply, strongest first: the system policy
(`/etc/briefcase/policy.toml`, `/Library/Application Support/Briefcase/policy.toml` on macOS,
`C:\ProgramData\Briefcase\policy.toml` on Windows; override with `BRIEFCASE_SYSTEM_POLICY`),
your `~/.briefcase/policy.toml`, and the nearest project `.briefcase/policy.toml`. The strongest
file that exists is the base and applies as written. Each weaker file may only make things
stricter:

- its `ask` and `deny` rules are added as a separate group; across groups the strictest result
  wins, so a base `allow` cannot pre-empt a project `deny`;
- its `allow` rules and `disable` list are ignored;
- a setting is taken only if it is stricter (`preset` observe < guarded < strict, `mode` enforce
  over observe, `fail_closed`/`protect_self` true over false, `on_unparseable`/`default`/
  `ask_fallback` deny > ask > allow); `protected_branches` are added;
- whatever is ignored is listed under "Ignored" by `briefcase-ai policy check`.

A file is applied whole or not at all: one unknown key or bad value rejects it. If the base file
is the rejected one, nothing applies (a weaker file is never promoted to base).

## How to turn it off or undo

```text
$ briefcase-ai policy off --yes
Removing:
  ~/.claude/settings.json: removed our entry from hooks.PreToolUse
  ~/.claude/settings.json: deleted (briefcase created it and nothing else is in it)
Enforcement is off. The policy file is kept: ~/.briefcase/policy.toml
Next: briefcase-ai policy init   (to turn it back on)
```

`policy off` removes exactly the hook entries `policy init` added (also after you edited them),
and leaves your own hooks and settings alone. `briefcase-ai connect --undo` with no tool named
removes these and everything `connect` wrote. To keep the hooks but stop blocking, set
`mode = "observe"`.

## Limits and failure modes

### What each tool can enforce

| | Claude Code | Cursor | Codex CLI |
| --- | --- | --- | --- |
| Hook | `PreToolUse` | `beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`, `preToolUse` (Write, Delete) | `PreToolUse` (Bash, apply_patch, MCP) |
| Shell | deny, ask | deny, ask | deny (ask -> `ask_fallback`) |
| File read | deny, ask | deny (ask -> `ask_fallback`) | not hooked; reads through shell commands are checked |
| File write | deny, ask | deny (ask -> `ask_fallback`); `tool_input` field names are not documented, a call with no recognisable path is allowed and logged | deny via `apply_patch` paths |
| MCP | deny, ask | deny, ask | deny |
| Web | deny, ask (`WebFetch`; `WebSearch` has no URL) | not hooked | not hooked |
| Allowed call | prints nothing: Claude Code's own permission prompts still apply | `{"permission": "allow"}` | prints nothing |
| Verified | real binary, 2.1.330 | documentation only | documentation only |

Codex skips a new or changed hook until you trust it in `/hooks`. Cursor reloads `hooks.json` on
save. Claude Desktop / Cowork has no hook the policy can use.

### When something breaks

Default is **fail open**: a broken policy file, a payload the hook does not understand, a crash
or a timeout lets the call through. It is never silent: the problem goes to
`~/.briefcase/policy/health.json` and the log, `briefcase-ai status`, `briefcase-ai policy status` and
`briefcase-ai doctor --connect` report it, and the hook returns it as a `systemMessage` warning
(Claude Code and Codex) at most once every ten minutes:

```text
$ briefcase-ai policy status
policy: ATTENTION  policy file invalid, enforcement is OFF (failing open): not valid TOML: Invalid value (at line 3, column 7)
  INVALID user     ~/.briefcase/policy.toml
  ok      hook     claude-code
  problem: ~/.briefcase/policy.toml: not valid TOML: Invalid value (at line 3, column 7)
Next: briefcase-ai policy check
```

```text
$ (hook stdout for `rm -rf ~` with that broken policy)
{"systemMessage": "briefcase-ai policy: the policy cannot be applied (~/.briefcase/policy.toml: not valid TOML: Invalid value (at line 3, column 7)). Enforcement is OFF until it is fixed. Fix it with `briefcase-ai policy check`, or turn enforcement off with `briefcase-ai policy off`."}
```

`fail_closed = true` (or `policy init --fail-closed`) is the explicit opt-in to block instead.
Then a broken policy blocks every call with a message naming `briefcase-ai policy check`; the line
`fail_closed = true` is honoured even when the rest of the file does not parse. The installed
hook entries get the tools' own failure switches: Claude Code `onFailure: "block"` (2.1.295+;
`policy init --fail-closed` is refused when the installed version is older or cannot be read),
Cursor `failClosed: true`. Codex has no such switch.

**Codex and a missing executable.** Codex starts the hook command itself. If the executable it
names is missing or not on PATH when Codex starts, the command fails with a status Codex treats
as an ordinary hook failure and runs the tool anyway. briefcase-ai cannot block that, because
none of its code has started. Once the hook runs, `--fail-closed` for Codex denies an unreadable
policy, malformed input, a missing required policy and a crash, and exits 2 with a reason on stderr
when it cannot write its verdict. It does not cover a missing executable, a host timeout, or stdout
and stderr both being closed.
`policy init --fail-closed` for Codex, and `connect --managed` with a `fail_closed` policy that
includes Codex, therefore refuse until you pass `--accept-codex-startup-risk`.

A fail-closed hook allows a call only when the event is complete and names a tool the policy
covers, the policy loaded without error (a managed hook: from the exact bytes of the file whose
hash it was deployed with), and the policy itself says allow. For a managed hook (one that carries
`--require-policy-sha256`) "says allow" is literal: a rule with `effect = "allow"` matched every part of the call, or the
policy states `default = "allow"` itself; the built-in default does not count, and `mode = "observe"` does not turn a
deny into an allow. A required policy that does neither blocks every call, so add `default = "allow"` (or allow
rules) to the policy you deploy. An unknown tool, an empty command,
a missing event name, an option left without a value, a `WebFetch` without a URL host, an MCP call that
does not name both a server and a tool, a relative path with no working directory in the event, a shell
command that yields nothing to judge, and a verdict that cannot be written all block. A managed hook that finds its system policy missing or changed blocks whether or not
`--fail-closed` was passed, and does so before any bookkeeping that could fail; a hook that carries
`--require-policy-sha256` also blocks when it crashes or cannot write its verdict.

When the verdict cannot be written the hook puts its reason on stderr (Codex takes exit status 2 with
a nonempty stderr as its block) and exits with 2. If stdout and stderr are both closed nothing can be
shown, and exit status 2 is all the hook can offer; a host that ignores it, or one that times the hook
out, is outside what a command-line hook can guarantee.

What happens when the hook cannot even start (the virtualenv was deleted, briefcase moved):
Python exits with code 2 for a script that does not exist, and exit code 2 *blocks* in all three
tools. So the default command starts the launcher through `-c` and `runpy`, which exits 1 when
the file is gone (a failed hook, allowed); the fail-closed command passes the file directly, so
a missing launcher exits 2 and blocks, in Codex too. `briefcase-ai policy status` flags a launcher
that no longer exists. A project policy that turns `fail_closed` on and is broken blocks only
inside that project.

### What a local policy cannot guarantee

A policy in your home directory is a seatbelt for an agent that makes a mistake, not a wall
against one that is trying to get around it. It decides on the tool call, not on what the call
then does:

- A script the agent writes and then runs (`sh ./cleanup.sh`) is judged by its command line, not
  its contents. So is anything an interpreter does: `python3 -c "import shutil;
  shutil.rmtree('/')"` is allowed (inline code is only searched for protected path names).
- Aliases and functions from your shell profile are not seen.
- Plain downloads and requests are allowed (`curl https://x.example/?d=hello`); only uploads,
  `-d`, `-F`, `nc`, `scp` and similar ask. Data can leave in a URL.
- MCP tool arguments are not inspected (a filesystem MCP server can read `~/.ssh`).
- A path is checked when the call is made; a symlink swapped in afterwards is not.
- `protect_self` stops the agent's own edits and the obvious off switches (`briefcase-ai policy off`,
  `pip uninstall briefcase-ai`, `claude --safe-mode`, `claude --settings`, `claude
  --setting-sources`), but anything that runs as you can edit files you own, for example from a
  script or with `CLAUDE_CODE_SAFE_MODE=1` set in the environment.

For a team that needs this to hold, deploy the policy and hooks as managed settings that users
cannot change (`briefcase-ai connect --managed`, see [managed-rollout.md](managed-rollout.md)), and
put OS-level controls (sandboxing, file permissions, egress rules) next to it.

### Latency

Measured on the machine this was built on (4 vCPU Linux, Python 3.13), 50 runs each, whole
process from start to exit, guarded preset:

| Command form | p50 | p95 |
| --- | --- | --- |
| installed launcher (`python -I -S -c ... hook_main.py`), allowed `git status && npm test` | 40.0 ms | 53.0 ms |
| installed launcher, denied `curl ... \| sh` | 34.6 ms | 43.1 ms |
| `briefcase-ai hook claude-code --enforce` (imports the SDK; `--portable`, managed packs) | 137.3 ms | 163.5 ms |

A bare `python -I -S -c pass` took 19.5 ms on the same machine. The hook timeout written into the
tools' settings is 10 s.

## Troubleshooting

| Symptom | Do this |
| --- | --- |
| Nothing is ever blocked | `briefcase-ai policy status`. `OFF`: run `briefcase-ai policy init`. `ATTENTION ... failing open`: run `briefcase-ai policy check` and fix the line it names. `mode observe` in the status line: set `mode = "enforce"`. |
| A command you expected to be denied is allowed | `briefcase-ai policy test -- <the command>` shows how it was read and which rule matched. Check "What each tool would do": some calls are not hooked in some tools. |
| Everything is blocked with "fail_closed" | The policy is broken and `fail_closed = true`. `briefcase-ai policy check`, fix the line, or `briefcase-ai policy off`. |
| Claude Code shows `PreToolUse:Bash hook error: ...` with `No such file or directory` or `can't open file` | The launcher moved (briefcase was reinstalled elsewhere or its virtualenv was deleted); `briefcase-ai policy status` says so. Run `briefcase-ai policy off` and then `briefcase-ai policy init`; `init` does not rewrite a hook that is installed. |
| An `ask` rule blocks in Codex or for Cursor file reads | Those cannot ask from a hook; `ask_fallback` (default `deny`) applies. Set `ask_fallback = "allow"` or make the rule `deny`/`allow`. |
| Codex never runs the hook | Open `/hooks` in Codex and trust it; Codex skips new or changed hooks until you do. |
| Cursor ignores the hook | Customize > Hooks and the Hooks output channel in Cursor; restart Cursor if `hooks.json` did not reload. |
| `briefcase-ai policy check` says `no TOML parser on this Python` | Python 3.9 and 3.10 have no `tomllib`: `pip install tomli` in the environment briefcase runs from. Until then the hook fails open and says so. |
| A project policy's `allow` has no effect | By design: a project policy can only restrict. `briefcase-ai policy check` lists it under "Ignored". |
| Decisions never reach the receiver | `briefcase-ai receive --status`. The hook only reports when `--endpoint` was given or a receiver was started with `briefcase-ai receive`; `BRIEFCASE_POLICY_REPORT=0` turns reporting off. The local log is written either way. |
| `briefcase-ai policy log` shows `#3f2a...` instead of commands | That is the keyed hash; run `briefcase-ai policy off`, then `briefcase-ai policy init --capture-content` to log the text. |

## How this was verified

- Claude Code **2.1.330 (real binary)**, 2026-11-22: `claude -p` in a temp `HOME` against a local
  stub of the model API that answered with one tool call. With the hook installed by
  `briefcase-ai policy init`: `Bash cat ~/.ssh/id_rsa` and `Read ~/.ssh/id_rsa` came back as
  `PreToolUse:... hook error: Blocked by briefcase-ai policy (rule secrets.files): ...`;
  `echo allowed-marker-42` ran; `pip install ...` (ask) was refused in `-p` with the rule's
  reason; a broken policy let the call run (fail open); with `fail_closed` the broken policy
  blocked; a missing launcher blocked with `onFailure: "block"` and was allowed with the
  default command form; after `briefcase-ai policy off` the call ran.
- Formats, read 2026-11-21 and re-read unchanged 2026-11-22:
  https://code.claude.com/docs/en/hooks, https://cursor.com/docs/hooks (the page
  https://cursor.com/docs/agent/hooks serves), https://developers.openai.com/codex/hooks.
  They are recorded under `enforce` in `briefcase/connect/adapters.json`.
- Cursor and Codex: never run. Their hook process is tested with payloads built from the
  documented schemas (`tests/policy/test_hook.py`).
