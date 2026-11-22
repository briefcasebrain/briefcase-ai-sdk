# Plugins from other packages

Add an exporter, a storage backend, a data-versioning provider, a telemetry source for the
receiver, a `briefcase connect` adapter, a guardrail, a CLI command or a feature card from your
own package, without editing briefcase. Install the package and briefcase finds it.

Flow: **Connect**. Feature id: `plugins`. Status: experimental.

## The one command

```bash
briefcase plugins list
```

It lists every plugin installed packages declare, by group, with the package and version it
comes from and whether it loaded. `briefcase plugins show NAME` explains one;
`briefcase doctor --plugins` gives the same verdicts as `[ok]` / `[warn]` lines for CI.

### Real output

Captured on 2026-11-22 in a temporary `HOME`, with the two fixture packages from
`tests/plugins/fixtures/` installed by `pip install --target` (`bc-plugin-alpha` provides one
working plugin per group; `bc-plugin-beta` provides the broken ones used under
[Troubleshooting](#troubleshooting)). Only alpha installed:

```text
$ briefcase plugins list
briefcase plugins · plugin API 1 · 8 entry points

briefcase.exporters  (exporter class for briefcase.observe('NAME:...') and BRIEFCASE_EXPORTER)
  alpha                loaded    bc-plugin-alpha 0.1.0

briefcase.bitemporal_backends  (bitemporal store backend class)
  alphadb              loaded    bc-plugin-alpha 0.1.0

briefcase.vcs_providers  (data-versioning client class, a VcsClientBase)
  alphavcs             loaded    bc-plugin-alpha 0.1.0

briefcase.receiver_sources  (normaliser for POST /v1/sources/NAME on the receiver)
  alpha                loaded    bc-plugin-alpha 0.1.0

briefcase.connect_adapters  (adapter class for `briefcase connect NAME`)
  alphatool            loaded    bc-plugin-alpha 0.1.0

briefcase.guardrails  (guardrail environment class for guardrails.make('NAME'))
  alpha-check-v1       loaded    bc-plugin-alpha 0.1.0

briefcase.cli_commands  (module whose register(subparsers) adds `briefcase NAME`)
  alpha                loaded    bc-plugin-alpha 0.1.0

briefcase.features  (feature manifest listed by `briefcase features`)
  alpha-export         loaded    bc-plugin-alpha 0.1.0

Next: briefcase features  # plugin features are listed with the built-in ones

$ briefcase alpha hello --name you
hello, you (from bc-plugin-alpha)
Next: briefcase plugins show alpha

$ briefcase --help          (excerpt)
Plugins: commands from installed packages (briefcase plugins list)
  alpha       Plugin command from bc-plugin-alpha 0.1.0.

$ briefcase doctor --plugins
briefcase plugins (plugin API 1):
[ok]   plugin bitemporal_backends:alphadb: bc-plugin-alpha 0.1.0
[ok]   plugin cli_commands:alpha: bc-plugin-alpha 0.1.0
[ok]   plugin connect_adapters:alphatool: bc-plugin-alpha 0.1.0
[ok]   plugin exporters:alpha: bc-plugin-alpha 0.1.0
[ok]   plugin features:alpha-export: bc-plugin-alpha 0.1.0
[ok]   plugin guardrails:alpha-check-v1: bc-plugin-alpha 0.1.0
[ok]   plugin receiver_sources:alpha: bc-plugin-alpha 0.1.0
[ok]   plugin vcs_providers:alphavcs: bc-plugin-alpha 0.1.0
```

## What it writes or sends, and where

Nothing. briefcase only reads package metadata (`entry_points.txt` of installed
distributions) and imports a plugin when its name is used. What a plugin itself does once
imported is up to that plugin: it runs with your permissions, like any package you install.

## Defaults and how to change them

| Setting | Default | Change it with |
| --- | --- | --- |
| Plugins on | every installed plugin is available | `BRIEFCASE_PLUGINS=off` loads none |
| Skip some | none | `BRIEFCASE_PLUGINS_DISABLE=acme,exporters:beta,some-dist` (a name, `GROUP:NAME`, or a distribution name; comma-separated) |
| Name clash with a built-in | the built-in is used, the plugin is `shadowed` | rename the plugin |
| Two packages, one name | the alphabetically first distribution is used, the other is `skipped` | uninstall or disable one |

`briefcase plugins` and `briefcase doctor` always say which applied.

## How to turn it off or undo

`BRIEFCASE_PLUGINS=off` for one run or in your shell profile; `pip uninstall <package>` to
remove a plugin for good. Nothing is left behind.

## Limits and failure modes

- **Lazy.** Listing reads metadata only. `briefcase --help` lists plugin commands without
  importing them. Measured on 2026-11-22 (40 interleaved runs, ~170 installed distributions):
  `briefcase --help` took a median 142 ms before this feature, 148 ms after with no plugin
  installed (a raw scan of `entry_points.txt` files, about 1 ms, finds none and
  `importlib.metadata` is never imported), and 156 ms with the two fixture packages installed. A plugin is imported the first time its group and name are
  needed: `observe("acme:...")`, `get_backend("acme")`, a request to `/v1/sources/acme`,
  `briefcase acme ...`. `briefcase connect` and `briefcase features` import the plugins of their
  own group; `briefcase status` imports plugin commands that have a `status()` hook;
  `briefcase plugins` and `briefcase doctor` import everything (that is how they can tell you
  what works).
- **Isolated.** A plugin that raises on import (including `SystemExit`), names something
  missing, or has the wrong type for its group is skipped. One WARNING goes to the
  `briefcase.plugins` logger naming the distribution and version (visible once logging is on:
  `briefcase.enable_logging("WARNING")` or `BRIEFCASE_LOG_LEVEL=WARNING`). The CLI and your app
  keep working; only that name is unavailable, and using it says why.
- **Plugin API version.** This release implements plugin API **1**. A plugin may declare
  `BRIEFCASE_PLUGIN_API = 1` on the object its entry point names or in that object's module.
  A higher number is refused ("needs briefcase plugin API 2 ... upgrade with: pip install -U
  briefcase-ai"); no declaration is read as 1.
- `check_environment()` (used by `briefcase exec` to validate `BRIEFCASE_EXPORTER` before
  starting your program) imports nothing, so it cannot tell that a not-yet-loaded plugin is
  broken; the exporter raises with the reason when it is built.
- A plugin command's own `GROUP` is not read for `briefcase --help` (that would need an import);
  plugin commands are listed under "Plugins". Its `status()` row does use its `GROUP`.
- Plugin feature cards appear in `briefcase features`, not in the generated
  `docs/features/catalog.md`, which lists what ships with briefcase.

## Writing a plugin

Declare entry points in your package's `pyproject.toml`. Each group's entry point names one
object; the examples below are the files of `tests/plugins/fixtures/bc-plugin-alpha`, which the
test suite builds with pip and exercises end to end.

```toml
[project]
name = "bc-plugin-alpha"
version = "0.1.0"

[project.entry-points."briefcase.exporters"]
alpha = "bc_alpha.exporter:AlphaExporter"

[project.entry-points."briefcase.bitemporal_backends"]
alphadb = "bc_alpha.backend:AlphaBackend"

[project.entry-points."briefcase.vcs_providers"]
alphavcs = "bc_alpha.vcs:AlphaVcsClient"

[project.entry-points."briefcase.receiver_sources"]
alpha = "bc_alpha.source:normalize"

[project.entry-points."briefcase.connect_adapters"]
alphatool = "bc_alpha.adapter:AlphaToolAdapter"

[project.entry-points."briefcase.guardrails"]
alpha-check-v1 = "bc_alpha.guardrail:AlphaCheck"

[project.entry-points."briefcase.cli_commands"]
alpha = "bc_alpha.commands"

[project.entry-points."briefcase.features"]
alpha-export = "bc_alpha.feature:MANIFEST"
```

Names are 1 to 100 characters of letters, digits, `.`, `_` and `-`, starting with a letter or
digit.

| Group | Entry point names | Used by |
| --- | --- | --- |
| `briefcase.exporters` | a `BaseExporter` subclass | `briefcase.observe("NAME")`, `observe("NAME:TARGET")`, `BRIEFCASE_EXPORTER=NAME:TARGET` |
| `briefcase.bitemporal_backends` | a class implementing `BitemporalStore` | `briefcase.bitemporal.backends.get_backend("NAME")`, `open_backend(...)` |
| `briefcase.vcs_providers` | a `VcsClientBase` subclass | `briefcase.integrations.vcs.get_provider("NAME")` |
| `briefcase.receiver_sources` | a callable `normalize(payload) -> event, [events] or None` | `POST /v1/sources/NAME` on `briefcase receive` |
| `briefcase.connect_adapters` | an `Adapter` subclass with a `DATA` dict | `briefcase connect NAME`, `briefcase mcp install NAME` |
| `briefcase.guardrails` | a guardrail environment class | `briefcase.guardrails.framework.make("NAME")`, `list_registered()` |
| `briefcase.cli_commands` | a module with `register(subparsers)` adding command `NAME` | `briefcase NAME ...` |
| `briefcase.features` | a manifest: dict, JSON text, or a callable returning one | `briefcase features` |

### Exporter (`briefcase.exporters`)

`observe("alpha:/tmp/out.jsonl")` calls `AlphaExporter.from_target("/tmp/out.jsonl")`. Without
a `from_target` classmethod the class is called as `cls(TARGET)`, or `cls()` for a bare
`observe("alpha")`. A plugin cannot take a built-in name (`console`, `memory`, `otlp`, `otel`,
`http`, `https`, `file`, `jsonl`).

```python
"""briefcase.exporters: briefcase.observe("alpha:/tmp/out.jsonl") or BRIEFCASE_EXPORTER=alpha:/tmp/out.jsonl."""
import json

from briefcase.exporters.base import BaseExporter, get_field

BRIEFCASE_PLUGIN_API = 1


class AlphaExporter(BaseExporter):
    """Appends one line per decision record: its id and function name."""

    def __init__(self, path: str = "alpha.jsonl") -> None:
        self.path = path

    @classmethod
    def from_target(cls, target: str) -> "AlphaExporter":
        # Everything after "alpha:" in the shorthand.
        return cls(target or "alpha.jsonl")

    async def export(self, decision) -> bool:
        line = {"id": get_field(decision, "decision_id"), "fn": get_field(decision, "function_name")}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        return True

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None
```

### Bitemporal backend (`briefcase.bitemporal_backends`)

```python
"""briefcase.bitemporal_backends: get_backend("alphadb")."""
from briefcase.bitemporal import InMemoryBitemporalStore

BRIEFCASE_PLUGIN_API = 1


class AlphaBackend(InMemoryBitemporalStore):
    """The reference in-memory store under another name. A real backend implements
    append, append_many, history, latest, as_of, keys and __len__."""

    label = "alphadb"
```

Built-in names: `memory`, `sqlite`, `iceberg`, `glue`, `kdb`.
`briefcase.bitemporal.backends.available_backends()` lists built-ins and installed plugins.

### VCS provider (`briefcase.vcs_providers`)

```python
"""briefcase.vcs_providers: get_provider("alphavcs")."""
from typing import Dict

from briefcase.integrations.vcs import VcsClientBase

BRIEFCASE_PLUGIN_API = 1


class AlphaVcsClient(VcsClientBase):
    """Keeps objects in a dict; versions are counters."""

    def __init__(self, repository: str, branch: str = "main", **kwargs) -> None:
        super().__init__("alphavcs", repository, branch, **kwargs)
        self._objects: Dict[str, bytes] = {}
        self._has_provider = True

    def _read_object_impl(self, path: str) -> bytes:
        return self._objects[path]

    def _write_object_impl(self, path: str, data: bytes, content_type=None) -> None:
        self._objects[path] = data

    def _create_version_impl(self, message: str, metadata=None) -> str:
        return f"v{len(self._objects)}"
```

Built-in names: `dvc`, `nessie`, `pachyderm`, `artivc`, `ducklake`, `iceberg`, `gitlfs`.

### Receiver source (`briefcase.receiver_sources`)

The receiver accepts `POST /v1/sources/NAME` with a JSON body and passes it to your
`normalize`. Each event is `{"type", "attrs"}` (optionally `source`, `time`, `trace_id`,
`span_id`), using the event and attribute names of `briefcase.semantic_conventions.cowork`;
it then becomes a record exactly as a built-in source's would, including redaction. `source`
defaults to the plugin name. Built-in names (`cursor`, `otlp`, `policy`, `claude_code`, ...)
never route to a plugin.

```python
"""briefcase.receiver_sources: POST /v1/sources/alpha on `briefcase receive`."""

BRIEFCASE_PLUGIN_API = 1


def normalize(payload):
    """One tool-call event per item in ``payload["calls"]``. Return an event, a list of
    events, or None. ``type`` and ``attrs`` use briefcase.semantic_conventions.cowork names:
    ``tool_result`` with ``tool_name``, ``success`` and ``duration_ms`` becomes a tool_call
    record."""
    if not isinstance(payload, dict):
        return None
    events = []
    for call in payload.get("calls", []):
        events.append({
            "type": "tool_result",
            "attrs": {
                "session.id": payload.get("session"),
                "tool_name": call.get("tool"),
                "success": call.get("ok", True),
                "duration_ms": call.get("ms"),
            },
        })
    return events
```

Run against a real `briefcase receive --port 4399 --to recv.jsonl`:

```text
$ curl -s -X POST http://127.0.0.1:4399/v1/sources/alpha -H 'Content-Type: application/json' \
    -d '{"session": "s1", "calls": [{"tool": "grep", "ok": true, "ms": 12}]}'
{}
$ curl -s -X POST http://127.0.0.1:4399/v1/sources/nope -H 'Content-Type: application/json' -d '{}'
{"error": "no receiver source 'nope' (no installed package provides 'nope' in briefcase.receiver_sources)"}
$ head -1 recv.jsonl
{"schema_version": "1.2", "decision_id": "77e2cb0c-931a-433d-adc6-4141c721ddb6", "decision_type": "tool_call", "function_name": "alpha.tool.grep", "inputs": {"source": "alpha", "session_id": "s1", "tool_name": "grep"}, "outputs": {"success": true}, "started_at": "2026-11-22T05:47:14.108363+00:00", "ended_at": "2026-11-22T05:47:14.120363+00:00", "execution_time_ms": 12.0}
```

### `briefcase connect` adapter (`briefcase.connect_adapters`)

`DATA` holds the keys of an entry of `briefcase/connect/adapters.json`. The rule for built-in
adapters applies to yours too: only a format confirmed against the tool's documentation may be
written (`FileEdit`, `Command`); anything else is a `Manual` item, printed and never written.

```python
"""briefcase.connect_adapters: `briefcase connect alphatool`."""
from typing import Any, List

from briefcase.connect.adapters import Adapter, Context
from briefcase.connect.edits import Manual

BRIEFCASE_PLUGIN_API = 1


class AlphaToolAdapter(Adapter):
    id = "alphatool"
    # The keys of an adapters.json entry. Only a format checked against the tool's own
    # documentation may be written; anything else is a Manual item (printed, never written).
    DATA = {
        "name": "Alpha Tool",
        "mode": "print-only",
        "docs_url": "https://example.invalid/alpha-tool/telemetry",
        "verified_on": "2026-11-22",
        "detect": {"dirs": ["~/.alphatool"], "bins": ["alphatool"], "globs": []},
        "files": {},
    }

    def _plan(self, ctx: Context) -> List[Any]:
        if not ctx.telemetry:
            return []
        env = {"ALPHATOOL_OTLP_ENDPOINT": ctx.endpoint}
        return [Manual(self.id, "Set in the environment Alpha Tool starts from",
                       self.env_exports(env), "example adapter: printed, never written")]
```

```text
$ briefcase connect alphatool --dry-run --no-mcp

== Alpha Tool [print-only] ==
  manual Set in the environment Alpha Tool starts from
         why: example adapter: printed, never written
      export ALPHATOOL_OTLP_ENDPOINT=http://127.0.0.1:4318

Nothing can be written automatically for these tools; follow the manual steps above.
```

### Guardrail (`briefcase.guardrails`)

Plugin guardrails sit behind the ids registered in code: a built-in or an id your app registers
with `register()` always wins. `list_registered()` shows plugin guardrails (tagged `plugin`)
without importing them; `make()` imports.

```python
"""briefcase.guardrails: make("alpha-check-v1", blocked=["delete"])."""
from briefcase.guardrails.framework import BaseGuardrailEnv, Effect, EvalResult

BRIEFCASE_PLUGIN_API = 1


class AlphaCheck(BaseGuardrailEnv):
    _name = "alpha-check"

    def __init__(self, blocked=("delete",)) -> None:
        self.blocked = set(blocked)

    def evaluate(self, request):
        if request.action in self.blocked:
            return EvalResult(Effect.DENY, self._name, reason=f"{request.action} is blocked")
        return EvalResult(Effect.ALLOW, self._name)
```

### CLI command (`briefcase.cli_commands`)

The module follows the protocol of the built-in `briefcase/cli/*_commands.py` modules (see
[README.md](README.md#1-a-command-module)): `register(subparsers)` adds the command named by
the entry point, handlers are `func(args, store, engine, stack)` and return the exit code, and
an optional `status()` adds a row to `briefcase status`. It is imported only when
`briefcase NAME ...` runs (or by `briefcase status` for its `status()` hook). A built-in command
name always wins.

```python
"""briefcase.cli_commands: `briefcase alpha hello --name you`."""

GROUP = "review"   # read by `briefcase plugins`; plugin commands are listed under "Plugins"
BRIEFCASE_PLUGIN_API = 1


def register(subparsers) -> None:
    p = subparsers.add_parser("alpha", help="Say hello from the alpha plugin.")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="alpha_command")
    hello = sub.add_parser("hello", help="Print a greeting.")
    hello.add_argument("--name", default="world")
    hello.set_defaults(func=cmd_hello)


def cmd_hello(args, store, engine, stack) -> int:
    print(f"hello, {args.name} (from bc-plugin-alpha)")
    print("Next: briefcase plugins show alpha")
    return 0


def status():
    return {"id": "alpha", "state": "on", "detail": "alpha plugin is installed", "next": None}
```

### Feature card (`briefcase.features`)

The same keys as `briefcase/_features/*.json` (see [README.md](README.md#3-a-manifest)).
`docs_path` may be a URL. A built-in feature id always wins.

```python
"""briefcase.features: a manifest `briefcase features` lists (same keys as briefcase/_features/*.json)."""

BRIEFCASE_PLUGIN_API = 1

MANIFEST = {
    "id": "alpha-export",
    "title": "Alpha exporter",
    "verb": "capture",
    "one_line": "Send decision records to Alpha.",
    "command": "briefcase alpha hello",
    "docs_slug": "capture/alpha-export",
    "status": "experimental",
    "requires_extra": None,
    "verified_against": "tests/plugins only",
    "docs_path": "https://example.invalid/bc-plugin-alpha#readme",
}
```

### Testing your plugin

Install it into a scratch directory and point `PYTHONPATH` at it, then run
`briefcase plugins show NAME` and `briefcase doctor --plugins --strict` (exit 1 on any skipped
or refused plugin). `tests/plugins/test_plugins.py` does this with `pip install --target`.

## Troubleshooting

Real output, from the same temporary `HOME` with `bc-plugin-beta` installed as well:

```text
$ briefcase plugins show future
briefcase.vcs_providers:future
  Package:     bc-plugin-beta 0.2.0
  Entry point: bc_beta.future:FutureClient
  Provides:    data-versioning client class, a VcsClientBase
  State:       refused (needs briefcase plugin API 99, but this briefcase-ai supports 1..1; upgrade with: pip install -U briefcase-ai)
  Plugin API:  99; this briefcase-ai supports 1..1

Next: briefcase plugins list

$ briefcase beta-broken
error: plugin command 'beta-broken' cannot run: briefcase.cli_commands:beta-broken from bc-plugin-beta 0.2.0 is skipped: import failed: ImportError: bc_beta needs the 'betasdk' package, which is not installed
Fix: briefcase plugins show beta-broken
```

| Symptom | Cause | Fix |
| --- | --- | --- |
| My plugin is not in `briefcase plugins list` | the package is not installed in the interpreter `briefcase` runs on, or the group name is misspelt | `python -m pip show <package>`; check the `[project.entry-points."briefcase.<group>"]` table |
| `skipped ... import failed` | the plugin raised while importing (a missing dependency, usually) | `briefcase plugins show NAME` names the error; install what it needs |
| `refused ... needs briefcase plugin API N` | the plugin is newer than this briefcase-ai | `pip install -U briefcase-ai` |
| `shadowed ... a built-in ... is used instead` | the name is a built-in's | rename the entry point |
| `skipped ... also provided by <other package>` | two packages register the same name | uninstall one, or `BRIEFCASE_PLUGINS_DISABLE=<package>` |
| `observe("acme:...")` raises "cannot be used" | the plugin is skipped, refused or disabled | the message says which; `briefcase plugins show acme` |
| `briefcase acme` is "invalid choice" | plugins are off, or the command is disabled | unset `BRIEFCASE_PLUGINS` / `BRIEFCASE_PLUGINS_DISABLE` |
| A plugin misbehaves and you need briefcase without it now | | `BRIEFCASE_PLUGINS_DISABLE=NAME` (or `BRIEFCASE_PLUGINS=off`) |
