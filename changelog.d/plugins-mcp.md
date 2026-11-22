### Added

- Plugin entry points (`briefcase.plugins`, plugin API 1): installed packages can add exporters
  (`briefcase.exporters`, used by `observe("NAME:TARGET")` and `BRIEFCASE_EXPORTER`), bitemporal
  backends (`briefcase.bitemporal_backends`, `get_backend(NAME)`), VCS providers
  (`briefcase.vcs_providers`, `get_provider(NAME)`), receiver sources
  (`briefcase.receiver_sources`, `POST /v1/sources/NAME`), `briefcase connect` adapters
  (`briefcase.connect_adapters`), guardrails (`briefcase.guardrails`, `make(NAME)`), CLI commands
  (`briefcase.cli_commands`) and feature cards (`briefcase.features`). Plugins are imported only
  when their name is used (`briefcase --help` imports none), a broken one is skipped with one
  warning naming its package, built-in names always win, and `BRIEFCASE_PLUGINS=off` /
  `BRIEFCASE_PLUGINS_DISABLE=...` turn them off. A plugin may declare `BRIEFCASE_PLUGIN_API`;
  an unsupported version is refused with the upgrade command.
- `briefcase plugins [list|show NAME] [--json]`, and `briefcase doctor --plugins` (the default
  `briefcase doctor` adds a plugins section only when a plugin is installed).
- `briefcase-mcp` tools over recorded decisions, all read-only: `search_decisions`,
  `get_decision` (content only with `include_content=true`, capped), `get_trace` (tree with
  durations, tokens and cost; provider records under framework runs counted once),
  `summarize_usage` (recorded vs `briefcase.pricing`-estimated vs unpriced cost),
  `explain_policy_decision`, `verify_records` (hash chain and payload hashes; bitemporal SQLite
  content hashes), `replay_plan` (plans only, never runs) and `drift_check`. Reads the receiver's
  records, a `.jsonl` `BRIEFCASE_EXPORTER`, `BRIEFCASE_MCP_RECORDS` or a `path` confined to the
  home directory or `BRIEFCASE_MCP_ROOTS`. Works with mcp 1.x and 2.x.
- `briefcase.bitemporal.backends.get_backend` / `open_backend` / `available_backends` and
  `briefcase.integrations.vcs.get_provider` / `available_providers`: name-based lookup of the
  built-in backends and providers.

### Changed

- `briefcase --help` has a "Plugins" group; `briefcase mcp install` and `briefcase mcp link` list
  the server's tools.
- `briefcase.features.load_catalog(plugins=False)`: plugin feature cards are added only when
  asked for (`briefcase features` does; `--markdown` and `briefcase --help` counts do not).
