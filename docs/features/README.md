# Features

Three ways in. You should never need to read source to find out what briefcase can do or
whether it is working.

| You want to | Run | What you get |
| --- | --- | --- |
| Get started | `briefcase-ai` | A guided setup: one question about what you are doing, then the two or three steps for it. Nothing is written without asking. |
| Know where you stand | `briefcase-ai status` | What is on, what is off, and one suggested next command. |
| See everything | `briefcase-ai features` | The catalog, grouped by the five steps below. Also as a page: [catalog.md](catalog.md). |

Details, real output and troubleshooting for all three: [cli-shell.md](cli-shell.md).

## Choose your setup

| If you... | Start with | Then |
| --- | --- | --- |
| use AI coding tools (Claude Code, Cursor, Codex, Copilot) | `briefcase-ai connect` | `briefcase-ai receive --detach` ([integrations](../integrations/README.md)) |
| are building an app that calls LLMs | `briefcase-ai exec -- python your_app.py` | `briefcase-ai features --verb capture` |
| run a team or platform | `briefcase-ai setup`, third answer | `briefcase-ai features --verb enforce` |

`briefcase-ai` with no arguments asks this question and runs the steps with you.

## One vocabulary everywhere

Commands in `briefcase-ai --help`, groups in `briefcase-ai features` and the sections of these
docs all use the same five steps:

| Step | Meaning | Examples |
| --- | --- | --- |
| Connect | wire tools, apps and destinations | `connect`, `receive`, `exec`, `mcp` |
| Capture | what gets recorded | `@capture`, auto-instrumentation, exporters |
| Enforce | stop or limit things | guardrails, the budget and quota gateway |
| Verify | prove records are intact and reproducible | hash chain, examiner bundle, replay |
| Review | look at what happened | drift, cost, eval runs, the MCP server |

`briefcase-ai --help` adds two groups around them: "Start here" (`setup`, `status`,
`features`, `doctor`, `engine`, `completion`) and "Evaluate" (`dataset`, `secret`, `run`, `stack`).
The engine (installed with `briefcase-ai engine install`, licensed separately) and how the
Evaluate commands use it: [engine.md](engine.md); its commands: [graph.md](graph.md) (Review)
and [env-artifacts.md](env-artifacts.md) (Capture).

## For contributors: how a feature shows up

A feature is visible in all three entry points once it has these four things. None of them
needs an edit to a shared file.

### 1. A command module

`briefcase/cli/<name>_commands.py` is discovered automatically. Do not edit
`briefcase/cli/__main__.py`.

```python
"""``briefcase-ai policy``: allow, ask or deny what coding tools may run."""
from briefcase.cli import _ux

GROUP = "enforce"   # start | connect | capture | enforce | verify | review | evaluate | other
ORDER = 50          # optional; lower is listed first inside the group (default 100)


def register(subparsers) -> None:
    p = subparsers.add_parser("policy", help="Allow, ask or deny what coding tools may run.")
    _ux.add_json_flag(p)
    p.set_defaults(func=cmd_policy)


def cmd_policy(args, store, engine, stack) -> int:
    ...
    _ux.print_next("briefcase-ai status")
    return 0
```

- `register(subparsers)` is required. Keep it to argparse wiring and import anything heavy
  inside the handler: every `briefcase-ai` invocation imports every command module.
- `GROUP` decides where the command is listed in `briefcase-ai --help`. Without it, or with a
  value not in the list above, the command appears under "Other".
- Handlers are called as `handler(args, store, engine, stack)` and return the exit code:
  0 ok, 1 failed, 2 usage. `store` and `engine` are created only when a handler touches
  them, so a command that ignores them never creates `~/.briefcase`.
- A module that raises on import or in `register` is skipped and the rest of the CLI keeps
  working. `briefcase-ai --debug <anything>` prints one line naming the module and the error.

### 2. A status hook (optional)

Add a `status()` function to the same module and the feature gets a row on
`briefcase-ai status`, under its `GROUP`.

```python
def status() -> dict:
    policy = _load()                       # read-only and quick
    if policy is None:
        return {"id": "policy", "state": "off",
                "detail": "no policy file", "next": "briefcase-ai policy init"}
    return {"id": "policy", "state": "on",
            "detail": f"{len(policy.rules)} rules, deny by default", "next": None}
```

| Key | Required | Value |
| --- | --- | --- |
| `id` | yes | Stable, unique, kebab-case. Use the feature's manifest id so `briefcase-ai features <id>` can say whether it is active. `connect`, `receive`, `mcp` and `records` are taken. |
| `state` | yes | `"on"`, `"off"`, or `"attention"` (on, but something needs doing). |
| `detail` | yes | One short line: what is on, or why it is off. |
| `next` | yes | The command to suggest, or `None`. |
| `title` | no | Label shown instead of the id. |
| `data` | no | Anything JSON-serialisable; passed through to `briefcase-ai status --json`. |

Rules for the hook:

- Return one dict, a list of dicts, or `None` to show nothing.
- It runs on every `briefcase-ai status`. Read files and the environment only; nothing over
  the network beyond localhost, no subprocess that can hang, no writes.
- On a fresh machine return `state: "off"`; do not raise. A hook that raises, or returns a
  row without a valid `id` and `state`, is left out and reported under `--debug`.
- `next` feeds the single `Next:` line. Built-in steps come first (connect a tool, start
  the receiver), then any row in `attention`, then MCP registration, then the first row
  that is `off` and names a command. The full order is in `choose_next` in
  `briefcase/cli/status_commands.py`.

### 3. A manifest

`briefcase/_features/<id>.json`, one file per feature:

```json
{
  "id": "policy-hooks",
  "title": "Policy for coding tools",
  "verb": "enforce",
  "one_line": "Allow, ask or deny what Claude Code and Cursor may run.",
  "command": "briefcase-ai policy init",
  "docs_slug": "enforce/policy-hooks",
  "status": "experimental",
  "requires_extra": null,
  "verified_against": "Claude Code 2.1.330 (real install); Cursor (docs only)"
}
```

Optional keys: `docs_path` (the page, when it is not `docs/features/<id>.md`), `snippet`
(a few lines of Python shown by `briefcase-ai features <id>`), `status_id` (the status row
that says whether the feature is on, when its id differs from the feature id), `runtime`
(`"node"` for a feature started from Node, whose `command` then need not begin with
`briefcase-ai`; default `"python"`). Unknown
keys are ignored. A file that is not valid JSON, lacks a required key or names an unknown
verb is skipped with a warning, never a crash; `tests/test_features.py` fails on any such
file, on a `command` that is not a real `briefcase-ai` command, and on a docs page that does
not exist.

After adding or changing a manifest, regenerate the catalog page:

```bash
briefcase-ai features --markdown > docs/features/catalog.md
```

### 4. A page

`docs/features/<id>.md`, in this order: what it is for, the one command, what it writes or
sends and where, defaults and how to change them, how to turn it off or undo, limits and
failure modes, and a troubleshooting table keyed on the symptom. Paste real output from a
run in a temporary HOME. [cli-shell.md](cli-shell.md) is an example.

### Output helpers

`briefcase/cli/_ux.py` keeps every command reading the same way:

| Helper | Use |
| --- | --- |
| `_ux.print_next(command, reason=None)` | The one `Next:` line after success. |
| `_ux.fail(message, fix=None, code=1)` | `error: ...` plus `Fix: <command>` on stderr; returns the exit code. |
| `_ux.add_json_flag(parser)`, `_ux.print_json(obj)` | The `--json` convention: JSON alone on stdout. |
| `_ux.is_interactive()` | True only on a terminal without `--no-input`. Check it before any prompt. |
| `_ux.confirm(question)`, `_ux.choose(question, options)` | Prompts. `confirm` returns `None` when there is nobody to ask. |
| `_ux.style(text, name)`, `_ux.use_color()` | Colour on a terminal only, never with `NO_COLOR`. |
| `_ux.debug_enabled()` | Whether `--debug` is on; only then show a traceback. |
