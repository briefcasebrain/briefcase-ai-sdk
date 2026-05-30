# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 3.2.x   | Yes                |
| 3.1.x   | Yes                |
| 3.0.x   | Yes                |
| < 3.0   | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **security@briefcasebrain.com** with:

1. A description of the vulnerability
2. Steps to reproduce
3. Potential impact assessment
4. Any suggested fixes (optional)

We will acknowledge receipt within 48 hours and aim to provide an initial assessment within 5 business days.

## Disclosure Policy

We follow coordinated disclosure. Once a fix is available, we will:

1. Release a patched version
2. Publish a security advisory on GitHub
3. Credit the reporter (unless they prefer anonymity)

## Security Best Practices

When using Briefcase SDK:

- Never commit API keys or secrets to source control
- Use environment variables for all secrets
- Enable PII sanitization (via the Rust `Sanitizer`) before logging decision snapshots
- When persisting external-data snapshots, pass a sanitizer so raw payloads are
  redacted before they are written to durable storage:
  `ExternalDataTracker(lakefs_client=..., sanitizer=briefcase.sanitize.Sanitizer())`.
  Without it, raw responses (which may contain PII) are stored verbatim.
- Pin dependency versions in production deployments
