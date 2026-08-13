# Security Policy

## Supported versions

Security fixes are applied to the latest commit on the default branch. This repository does not currently publish long-term-support release branches.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private vulnerability reporting feature under **Security > Advisories > Report a vulnerability**.

Include the affected component, reproduction steps, impact, and any known mitigations. Do not include live credentials, personal data, proprietary financial data, or exploit activity against systems you do not own.

Maintainers should acknowledge a complete report within five business days, assess severity, coordinate remediation and disclosure, and credit the reporter when requested and appropriate.

## Deployment responsibility

The application provides hashed service-principal credentials, tenant isolation, route RBAC, distributed rate/concurrency limits, a hash-chained governance log, and mandatory report approval by default. It does not provide interactive end-user identity, TLS termination, a WAF, or network isolation. Operators must supply those controls and follow [docs/operations.md](docs/operations.md).

Provider payloads and model output are untrusted. The runtime bounds and screens external content, allowlists tools, enforces autonomous-call budgets, validates citations against source provenance, and withholds consequential output for human approval. These controls reduce risk but do not guarantee factual accuracy or detect every adversarial instruction.
