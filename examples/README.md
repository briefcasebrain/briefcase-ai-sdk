# Briefcase AI Examples

Examples showing how to use the Briefcase AI SDK.

## Examples

### [Python Basic](python-basic/)
Core Python usage covering all fundamental features:
- Decision tracking with `@capture` decorator
- Drift detection
- Cost analysis
- Data sanitization
- Storage backends

### [Rust](rust/)
Native Rust usage via `briefcase-core`:
- Direct API usage
- Feature flags
- Storage backends
- PII sanitization

### [Validation](validation/)
Prompt validation engine:
- Validation modes and configuration
- Custom validation rules

### [lakeFS Versioning](lakefs_versioning/)
Versioned storage with lakeFS:
- VersionedClient usage
- Context managers and decorators

### [Multi-Agent Correlation](multi_agent_correlation/)
Workflow correlation across multiple agents:
- BriefcaseWorkflowContext
- Trace propagation

## Running Examples

```bash
# Clone repository
git clone https://github.com/briefcasebrain/briefcase-ai-sdk.git
cd briefcase-ai-sdk

# Choose an example
cd examples/python-basic

# Follow the README instructions
cat README.md
```

## Contributing Examples

We welcome new examples! Please:

1. Include a comprehensive README with setup instructions
2. Add tests demonstrating key functionality
3. Update this README with your example
