### Added
- `briefcase` with no arguments is now a front door. The first time on a terminal it runs a
  guided setup: one question (AI coding tools, an app that calls LLMs, or a team rollout),
  then two or three steps that call the existing commands (`connect`, `receive --detach`,
  `exec`), each with its own confirmation. Completion is remembered in
  `~/.briefcase/setup.json` only after the guide was finished or skipped; `briefcase setup`
  runs it again. Without a terminal, or with `--no-input` / `BRIEFCASE_NO_INPUT=1`, it
  prints the command list and the status screen and exits 0.
- `briefcase status [--json]`: one screen showing connected coding tools, the receiver and
  its request counters, MCP registration and where records go, ending with exactly one
  `Next:` suggestion. Command modules can add a row with an optional `status()` hook.
- `briefcase features [ID] [--verb V] [--json] [--markdown]`: the feature catalog, grouped
  by Connect, Capture, Enforce, Verify and Review, generated from one JSON manifest per
  feature in `briefcase/_features/`. `briefcase features <id>` shows the docs link and
  whether the feature is active on this machine. `briefcase.features.load_catalog()` reads
  the manifests through `importlib.resources`, skips malformed files with a warning and
  ignores unknown keys. Manifests are seeded for the 23 features that already existed.
- `briefcase completion bash|zsh|fish`: a completion script generated from the installed
  commands, with no new dependency.
- Command auto-discovery: every `briefcase/cli/*_commands.py` module with a
  `register(subparsers)` function is registered without editing `__main__.py`. A module
  declares its help group with `GROUP` (and optionally its position with `ORDER`). A module
  that fails to import is skipped, and `--debug` names it.
- `briefcase/cli/_ux.py`: shared helpers for the `Next:` line, errors with a `Fix:`
  command, `--json` output, prompts, and terminal and `NO_COLOR` detection.
- Global flags `--debug` (tracebacks, skipped modules; also `BRIEFCASE_DEBUG=1`) and
  `--no-input`.
- `docs/features/`: the three entry points, the contributor protocol (command modules,
  status hooks, manifests) and a generated catalog page.

### Changed
- `briefcase --help` lists commands under "Start here", Connect, Capture, Enforce, Verify,
  Review and Evaluate instead of one flat list. `briefcase <command> --help` is unchanged.
- `briefcase` with no arguments exits 0 (it used to print help and exit 2). A command group
  without a subcommand, such as `briefcase run`, still exits 2 and now prints that group's
  help instead of the top-level help.
- An unexpected error in a command prints one `error:` line and a `Fix:` line and exits 1;
  the traceback needs `--debug`. `briefcase features | head` no longer ends in a broken-pipe
  traceback.
- The `dataset`, `secret`, `run`, `stack` and `doctor` parsers moved out of `__main__.py`
  into discovered modules (`job_commands.py`, `stack_commands.py`, `doctor_commands.py`)
  with the same names, flags, defaults and handlers.
