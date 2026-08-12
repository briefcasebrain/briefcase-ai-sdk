# Contributing to Briefcase SDK

Thank you for your interest in contributing to Briefcase SDK.

## Getting Started

1. Fork and clone the repository:

```bash
git clone https://github.com/briefcasebrain/briefcase-ai-sdk.git
cd briefcase-ai-sdk
```

2. Install Python development dependencies:

```bash
pip install briefcase-ai[dev]
```

3. For Rust development, ensure you have Rust 1.70+ installed:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

## Project Structure

The repository is a monorepo:

- **Python SDK** (`briefcase/`): Python package published as `briefcase-ai` on PyPI, backed by the Rust core through the `briefcase._native` extension. Requires Python >=3.9.
- **Rust core** (`crates/briefcase-core/`): Core library published as `briefcase-core` on crates.io.
- **Python bindings** (`bindings/python/`): PyO3/maturin bridge between Rust and Python.

## Development Workflow

### Python

Build the native extension:

```bash
pip install maturin
maturin develop
```

Run the full test suite. The facade tests under `tests/` mock the native module,
so the native binding tests run separately against the built extension:

```bash
maturin develop                      # build the native extension first
pytest tests/ -v --tb=short          # Python facade (mocks briefcase._native)
pytest bindings/python/tests/ -v     # native binding tests (real extension)
python scripts/check_imports.py      # smoke-test that every submodule imports
python scripts/version_sync.py check # version strings agree across targets
```

The two pytest runs must stay in separate processes: `tests/conftest.py`
installs a mock `briefcase._native` for the whole interpreter, so sharing one
with the binding tests makes them assert against mocks. A bare `pytest` runs
only `tests/` (`testpaths` in `pyproject.toml`).

Lint:

```bash
flake8 briefcase/ scripts/ tests/ bindings/python/tests/ examples/
```

CI enforces this and the tree is clean, so any new finding is yours. Settings
live in `.flake8`; the `per-file-ignores` there cover the semantic-convention
modules, which exist to be star-imported.

`mypy briefcase/` is not clean repo-wide and is not enforced. It is clean for
`briefcase/integrations/evals` and `briefcase/integrations/gym`; keep those two
that way (`mypy briefcase/integrations/evals briefcase/integrations/gym`).

The codebase is **not** black-formatted, and running `black` would rewrite most
of it, including merging wrapped strings onto worse lines. Do not run it.

### Rust

```bash
cargo fmt --all --check
cargo test -p briefcase-core --locked --all-features
cargo clippy -p briefcase-core --locked --all-features -- -D warnings
cargo test -p briefcase-python --locked          # bindings crate unit tests
cargo clippy -p briefcase-python --locked -- -D warnings
cargo run --manifest-path examples/rust/Cargo.toml   # standalone example crate
```

`--all-features` is not optional: `sanitize` is not a default feature, so a
narrower run silently compiles out every PII-redaction test.

## Pull Request Guidelines

1. Create a feature branch from `main`.
2. Write tests for new functionality.
3. Ensure all existing tests pass before submitting.
4. Keep PRs focused on a single change.
5. Update documentation if your change affects public APIs.
6. Follow existing code style and conventions.

## Commit Messages

Use clear, descriptive commit messages. Prefix with the area of change when helpful:

```
Fix versioned_context branch resolution for non-main branches
Add vector-store regression tests for replay engine
Update routing heuristics for async exporters
```

## Reporting Issues

Open issues on [GitHub](https://github.com/briefcasebrain/briefcase-ai-sdk/issues) with:

- A clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Python/Rust version and OS

## License

By contributing, you agree that your contributions will be licensed under the [Apache-2.0 License](LICENSE).
