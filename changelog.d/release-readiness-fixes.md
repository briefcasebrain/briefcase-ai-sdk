### Added
- `briefcase --version` prints `Briefcase AI <version>`. On a terminal, `briefcase`, `briefcase setup` and `briefcase --help` open with a `Briefcase AI <version>` banner and a one-line description; nothing is printed to pipes or in `--json` output.

### Fixed
- `briefcase exec` accepts `BRIEFCASE_EXPORTER=chain:PATH.db` and `--to chain:PATH.db`, the tamper-evident store that `briefcase.observe()` and `briefcase status` already name.
- `briefcase engine status --json` exits 3 when no engine is installed, as the text form and the documentation say.
- `briefcase engine --help` points licence tokens at the portal's Engine page instead of an API Keys page.
- `scripts/version_sync.py set` and `scripts/bump-version.sh` also rewrite the workspace crate versions in `Cargo.lock`, so `cargo --locked` keeps passing after a bump.
