### Changed
- **Breaking:** `@briefcase-ai/controls`, `@briefcase-ai/runtime` and `@briefcase-ai/native` now declare `engines.node` as `>=22.0.0` (was `>=20.19`, and `>= 12.22` for native). Node 20 reached end of life on 2026-04-30; 22 is the oldest line still supported. The code itself uses nothing newer than `AbortSignal.any` (Node 20.3 / 22.0), so 22.0.0 is the exact floor on the 22 line.
- CI runs the TypeScript packages on Node 22, 24 and 26 (previously 20.19 and 22); single-version jobs, `publish.yml` and `conformance.yml` use Node 26 (Active LTS).
