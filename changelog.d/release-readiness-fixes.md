### Added
- `briefcase-ai --version` prints `Briefcase AI <version>`. On a terminal, `briefcase-ai`, `briefcase-ai setup` and `briefcase-ai --help` open with a `Briefcase AI <version>` banner and a one-line description; nothing is printed to pipes or in `--json` output.

### Fixed
- `briefcase-ai exec` accepts `BRIEFCASE_EXPORTER=chain:PATH.db` and `--to chain:PATH.db`, the tamper-evident store that `briefcase.observe()` and `briefcase-ai status` already name.
- `briefcase-ai engine status --json` exits 3 when no engine is installed, as the text form and the documentation say.
- `briefcase-ai engine --help` points licence tokens at the portal's Engine page instead of an API Keys page.
- `scripts/version_sync.py set` and `scripts/bump-version.sh` also rewrite the workspace crate versions in `Cargo.lock`, so `cargo --locked` keeps passing after a bump.
