### Changed
- **Breaking:** `@briefcase-ai/controls`, `@briefcase-ai/runtime` and `@briefcase-ai/native` now declare `engines.node` as `>=26` (was `>=20.19`, and `>= 12.22` for native). Node 26 is the Active LTS; Node 20 reached end of life on 2026-04-30. The code uses nothing newer than `AbortSignal.any` (Node 20.3), so no later 26.x minor is required.
- CI tests the TypeScript packages on Node 26 only (previously 20.19 and 22); the next even line is added when it ships. Single-version jobs, `publish.yml` and `conformance.yml` use Node 26.
