### Changed
- The minimum supported Rust version is now 1.89 (was 1.85); CI builds on exactly that toolchain.
- The Python binding moves from pyo3 0.24 to 0.29, which also removes the two pyo3 advisory ignores
  from `deny.toml`.

### Security
- RUSTSEC-2026-0459 (SQLite page-cache disclosure through a crafted `-wal` file) is cleared by
  upgrading rusqlite 0.38 to 0.41 (libsqlite3-sys 0.39, SQLite 3.54).
- RUSTSEC-2026-0592 (stack overflow in `Url::origin()`) is cleared by `url` 2.5.9.
- RUSTSEC-2026-0176 and RUSTSEC-2026-0177 (pyo3 `nth`/`nth_back` out-of-bounds reads) are cleared by pyo3 0.29.
