# @briefcase-ai/controls

Controls layer for AI invocations: ports (quota, entitlements, trace export),
a gateway that enforces hard caps, quota, and throttle cooldowns around every
call, a suggestion pipeline, throttle classification and retry helpers, and
decision-record exporters. Bedrock-specific helpers (model-id resolution,
capability probes, pricing, credential provider) live on the `./bedrock`
subpath.

## Install

```sh
npm install @briefcase-ai/controls
```

## Usage

```ts
import { MemoryQuotaStore, createGateway } from "@briefcase-ai/controls";

const gateway = createGateway({
  quotaStore: new MemoryQuotaStore(),
  buckets: {
    suggestions: { capacity: 10, refillSecondsPerToken: 60 },
  },
});

const outcome = await gateway.invoke({
  tenantId: "tenant-1",
  bucket: "suggestions",
  fn: () => callYourModel(),
});

if (outcome.ok) {
  console.log(outcome.value, outcome.tokensRemaining);
} else {
  console.log(outcome.reason, outcome.cooldownUntil);
}
```

`MemoryQuotaStore` is in-process; production stores implement the `QuotaStore` port.

## Bedrock subpath

`@briefcase-ai/controls/bedrock` exports model-id resolution, Converse
capability probes, list-price lookup, and a memoized credential provider; the
`@ai-sdk/amazon-bedrock` and `@aws-sdk/credential-providers` peer dependencies
are optional and only needed by the credential provider and adapters.

Source: https://github.com/briefcasebrain/briefcase-ai-sdk
