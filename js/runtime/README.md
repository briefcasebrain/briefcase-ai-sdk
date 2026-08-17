# @briefcase-ai/runtime

Node runtime primitives for governed AI applications. The package has no root
export; import only the capability you use:

```ts
import { HashChainAppender } from "@briefcase-ai/runtime/integrity";
import { createLakefsClient } from "@briefcase-ai/runtime/lakefs";
import { runConnectorSync } from "@briefcase-ai/runtime/connectors";
import { createTraceRecorder } from "@briefcase-ai/runtime/trace";
```

The runtime owns mechanisms and contracts. Applications retain provider
implementations, credentials, authentication and authorization policy,
databases, and domain-specific resource mapping.

## Subpaths

- `integrity`: canonical JSON, SHA-256 hash chains, CAS stores, and verification.
- `lakefs`: typed lakeFS REST client with bounded read retries and request deadlines.
- `connectors`: refresh-safe sync orchestration, PKCE state, and AES-GCM envelopes.
- `trace`: ordered invocation and step recording behind a `TraceStore` port.

Cross-language canonical JSON matches the Python profile for strings,
integers, booleans, null, and containers of those values. Python and
JavaScript format floating-point numbers differently, as documented by the
Python integrity profile, so portable hashed payloads should encode decimals
as strings.

Source: https://github.com/briefcasebrain/briefcase-ai-sdk
