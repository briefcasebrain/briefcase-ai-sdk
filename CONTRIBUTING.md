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
```

Lint and format:

```bash
black briefcase/ tests/
flake8 briefcase/ tests/
mypy briefcase/
```

### Rust

```bash
cargo test -p briefcase-core --locked
cargo clippy -p briefcase-core --locked -- -D warnings
cargo fmt --all -- --check
```

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
