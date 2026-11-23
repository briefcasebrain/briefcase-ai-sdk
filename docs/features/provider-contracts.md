# Provider API contract checks

The code that talks to OpenAI, Anthropic, Gemini and Bedrock (provider capture, replay of
recorded calls, budget reservations) is checked against each provider's published API
specification, not against a live model. No model is called, nothing leaves the machine
except the one-time download of the Gemini discovery document.

Flow: **Verify**. Feature id: `provider-contracts`. Status: experimental.

## The one command

```bash
pytest tests/contracts -q
```

It runs offline from the pinned copies in `tests/contracts/specs/` (82 tests). The Gemini
tests also need the discovery document in the cache (below); without it, and without
network access, those 9 tests skip and say why. CI runs the suite on every push
(`.github/workflows/ci.yml`, step "Provider contract tests") with the SDK versions listed
below.

## What is validated

Every check drives the installed SDK's own client: only the HTTP layer is replaced (an
`httpx` mock transport, or botocore's HTTP session), so serialisation, response parsing
and streaming are the SDK's own code.

| Area | What is checked | Where |
|---|---|---|
| Replay requests | The request each replay executor builds from a recorded call is read off the wire and validated against the endpoint's request schema, with undeclared top-level fields rejected: OpenAI chat completions, Responses, legacy completions; Anthropic Messages; Gemini `generateContent`; Bedrock `Converse` (botocore's own `ParamValidator` against the pinned model). Each runs as recorded and with `--model` / `--param` / `--drop-param` overrides. | `test_*_contract.py::test_replay_requests_are_valid_*` |
| Recorded parameters | Every parameter and input the instrumentation records (`temperature`, `max_completion_tokens`, `inferenceConfig.maxTokens`, ...) is a request property of that endpoint, including embeddings and `InvokeModel`; a request carrying all of them with spec-generated values is valid. | `test_recorded_parameter*` |
| Responses | Example responses are generated from the response schemas (every field filled, each integer distinct), served to the SDK, and the record must carry exactly the example's model, ids, finish / stop reason and every usage counter. Every integer under `usage` the spec defines must be recorded or listed with the reason it is not (modality and TTL splits, request counts). The replay executors read the same response the same way. | `test_spec_generated_*` |
| Types | The fields the code reads exist in the spec with the type the code assumes (string ids and reasons, integer counters). | `test_fields_the_records_read_*` |
| Streams | OpenAI chat and completions chunks (content, finish, usage-only), Responses events (`response.created`, `response.output_text.delta`, `response.completed`), Anthropic message stream events, Gemini `streamGenerateContent` chunks and Bedrock `ConverseStream` event-stream frames, each generated from the spec and validated against it. | `test_stream*` |
| Budget caps | The output-token parameters budgets reserve for are the spec's token-limit parameters per endpoint: `max_completion_tokens` (preferred; `max_tokens` is deprecated in the spec) and `max_tokens` for chat, `max_output_tokens` for Responses, `max_tokens` (default 16, as the spec says) for legacy completions, none for embeddings, `max_tokens` (required) for Anthropic, `generationConfig.maxOutputTokens` x `candidateCount` for Gemini, `inferenceConfig.maxTokens` for Converse. InvokeModel bodies are opaque blobs in the AWS spec: only their location is checked. | `test_budget_params_contract.py` |
| Replay coverage | Every request setting in each spec is recorded, named in the replay's "not recorded" list (so `replay explain` warns), or listed with the reason it cannot change the compared answer. A setting a new spec adds fails until classified. | `test_replay_coverage_contract.py` |
| Datasets and evals | Records produced from spec-generated OpenAI, Anthropic and Bedrock responses go through `briefcase dataset from-records` and `briefcase eval run` with a deterministic target and the `exact` and `json` scorers. | `test_datasets_eval_contract.py` |

## Spec versions

| Spec | Version | Source | SHA-256 | Licence | In the repository |
|---|---|---|---|---|---|
| OpenAI OpenAPI 3.1 | `info.version` 2.3.0 | [openai/openai-openapi](https://github.com/openai/openai-openapi) `openapi.json` at commit `2a24deae6f89`, retrieved 2026-11-23 | `a778b243…a2` (uncompressed) | MIT | yes, `openai-openapi.json.gz` + `LICENSE.openai-openapi` |
| Anthropic Messages types | anthropic 2.12.0 | Anthropic publishes no machine-readable API spec (searched its docs and GitHub organisation, 2026-11-23), so the contract is the request/response types inside the `anthropic` package, rendered as JSON Schema by pydantic 2.13.5 | `7bf73467…1b` | derived from the SDK (Apache-2.0 / MIT) | yes, `anthropic-types.json.gz` |
| Gemini API discovery | generativelanguage v1beta, revision 20261122 | `https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta` | `1da18b6a…9d` (canonical JSON: the service varies key order) | Google APIs Terms of Service, no redistribution licence | **no**: downloaded on first use into `~/.cache/briefcase-contracts/` (`$BRIEFCASE_CONTRACTS_CACHE`) |
| Bedrock Runtime service model | apiVersion 2023-09-30, botocore 1.44.9 | `botocore/data/bedrock-runtime/2023-09-30/service-2.json` at tag 1.44.9 (identical to the installed wheel's copy) | `e1ba1936…73` | Apache-2.0 | yes, `botocore-bedrock-runtime-2023-09-30.json.gz` + `NOTICE.botocore` |

Full hashes, URLs and retrieval dates are in `tests/contracts/specs/manifest.json`; a
vendored file is used only when its hash matches. SDKs the wire checks ran with: openai
3.57.0, anthropic 2.12.0, google-genai 3.6.1, boto3 / botocore 1.44.9.

## What the checks found and changed

Each was a failing test first, then a code change:

| Finding | Source trusted | Change |
|---|---|---|
| OpenAI chat `usage.prompt_tokens_details.cache_write_tokens` and Responses `usage.input_tokens_details.cache_write_tokens` were not recorded | OpenAPI spec (the openai 3.57.0 types agree) | recorded as `token_usage.cache_write_tokens`; budgets price them as a part of `prompt_tokens` at the cache-write rate |
| Anthropic `usage.output_tokens_details.thinking_tokens` (also on `message_delta`) was not recorded | anthropic 2.12.0 types | recorded as `token_usage.reasoning_tokens`, a part of `completion_tokens` as for OpenAI |
| Request settings that change the answer were neither recorded nor named by replay's warning (for example OpenAI `verbosity`, `web_search_options`, Responses `prompt`, completions `echo`, Anthropic `container`, Gemini `frequency_penalty`, `cached_content`, Bedrock `outputConfig`, `promptVariables`) | each spec | scalars recorded (`verbosity`; Gemini `frequency_penalty`, `presence_penalty`), the rest added to the replay "not recorded" list and the replay docs |

Also observed, not ours to change: the Gemini discovery document lists a
`BLOCK_REASON_UNSPECIFIED` block reason that google-genai 3.6.1 does not know (it warns);
anthropic 2.12.0 has no `temperature` / `top_p` / `top_k`, so a record from an older SDK
that carries them is refused by `replay` until they are dropped (tested).

## Refreshing the specs

```bash
python scripts/refresh_provider_specs.py            # what changed upstream; exit 1 on drift
python scripts/refresh_provider_specs.py --write    # update specs/ and the manifest
pytest tests/contracts -q                           # does the code match the new specs?
```

The report lists, per spec, the version and hash change and the request, response and
usage fields that appeared or disappeared in the schemas the code uses. The Anthropic
snapshot is regenerated from the installed `anthropic` package: install the release to
check first. The workflow `.github/workflows/provider-contracts-refresh.yml` runs this weekly
(and on demand) with the latest SDKs, runs the suite against the refreshed specs, and
uploads the report; it writes nothing back. Update the pins with a reviewed PR.

## Limits

- Validation is JSON Schema 2020-12 (`jsonschema`, `$ref` resolved inside each document).
  OpenAPI `discriminator` and `format` keywords are not enforced; undeclared fields are
  rejected at the top level of OpenAI requests and everywhere in Anthropic and Gemini
  requests.
- Example responses fill every field, which no real response does; they prove the reading
  code, not the provider's behaviour.
- Not covered: Azure OpenAI and Vertex AI request variants, the Anthropic beta resource,
  model-specific InvokeModel bodies, OpenAI's Assistants / Realtime / Batch APIs.
- `jsonschema>=4.18` is a dev dependency; without it the suite skips with a reason.
