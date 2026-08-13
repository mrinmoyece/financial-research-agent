# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
make install
make gate
```

## Ground rules

- Every behavior change includes tests; every bug fix includes a regression test.
- Changes to prompts, tool routing, provider fallback, or report validation include deterministic eval coverage.
- Keep production fail-closed behavior: no silent provider, Redis, authentication, or validation fallback.
- Update architecture, operations, security, and failure-mode documentation with related behavior.
- Update `CHANGELOG.md` under Unreleased.
- Never commit credentials, customer data, proprietary financial data, or output presented as current market data.

Pull requests require the protected checks, one approving code-owner review, resolved conversations, signed commits, and squash merge.
