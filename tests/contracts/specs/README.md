# Pinned provider API specs

The contract tests (`tests/contracts`, see `docs/features/provider-contracts.md`) validate
against these documents. `manifest.json` records, for each one, the source URL, retrieval
date, version, SHA-256 (of the uncompressed bytes) and licence; a vendored file whose hash
does not match is refused. Refresh with `python scripts/refresh_provider_specs.py --write`.

| Spec | File | Licence | Vendored |
|---|---|---|---|
| OpenAI API OpenAPI 3.1 (github.com/openai/openai-openapi, commit `2a24deae6f89`) | `openai-openapi.json.gz` | MIT, text in `LICENSE.openai-openapi` | yes |
| Anthropic Messages request/response types, JSON Schema generated from `anthropic` 2.12.0 (Anthropic publishes no machine-readable API spec) | `anthropic-types.json.gz` | derived from the SDK, Apache-2.0 / MIT | yes |
| Gemini API discovery document, generativelanguage v1beta | (cache only) | Google APIs Terms of Service; no redistribution licence | **no**: downloaded on first use into `~/.cache/briefcase-contracts/` (`$BRIEFCASE_CONTRACTS_CACHE`); the tests that need it skip when it is absent and the network is not available (`BRIEFCASE_CONTRACTS_OFFLINE=1` forbids the download) |
| Amazon Bedrock Runtime service model, botocore 1.44.9 (`botocore/data/bedrock-runtime/2023-09-30/service-2.json`) | `botocore-bedrock-runtime-2023-09-30.json.gz` | Apache-2.0, botocore's notice in `NOTICE.botocore` | yes |

The files are gzip-compressed copies of the upstream bytes (`gzip -9 -n`), unmodified
except for the Anthropic snapshot, which is generated (see `_specs.generate_anthropic_schemas`).
