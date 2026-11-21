# Claude Code plugin

`briefcase/` is a Claude Code plugin that registers the SDK's MCP server
(`briefcase-mcp`, started through `uvx` so nothing has to be installed first).
The marketplace file that lists it is `.claude-plugin/marketplace.json` at the
repository root.

```bash
claude plugin marketplace add briefcasebrain/briefcase-ai-sdk
claude plugin install briefcase@briefcase-ai
```

The plugin only adds the MCP server. Telemetry is a user-level setting that a
plugin cannot write; `briefcase connect claude-code` sets that up. See
[`docs/integrations/README.md`](../../docs/integrations/README.md).
