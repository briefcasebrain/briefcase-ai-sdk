# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 3.0.x   | Yes                |
| < 3.0   | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **security@briefcaseai.org** with:

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
- Enable PII sanitization (via Rust `Sanitizer`) before logging decision snapshots
- Pin dependency versions in production deployments
