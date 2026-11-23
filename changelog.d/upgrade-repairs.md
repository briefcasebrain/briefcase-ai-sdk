### Fixed
- `briefcase-ai connect` replaces a Claude Code MCP entry that an earlier version registered (`claude mcp add` refused with "already exists"): it removes the old entry and adds the new one.
- On macOS, paths under `/etc` are refused as system paths after symlink resolution (`/etc` is `/private/etc`), so `connect --managed --out /etc/...` and edits of files there are refused as intended.
- Budget and content-guard config files are read on Python 3.9 and 3.10 (the `tomli` dependency was never used there).
- `briefcase-ai secret list` prints "no secrets stored" on a terminal when nothing is stored; piped and `--json` output is unchanged.
