# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and semantic versioning.

## [Unreleased]

### Security

- Added tenant-scoped service principals, role authorization, distributed rate/concurrency controls, hash-chained audit events, approval-gated report release, prompt-injection screening, citation allowlisting, execution budgets, and provider circuit breakers.
- Added constant-time service API-key authentication, bounded request bodies, explicit CORS controls, security headers, private vulnerability reporting guidance, and fail-fast production configuration.
- Pinned workflow actions and container bases to immutable SHAs/digests; added history secret scanning, dependency review, CodeQL security-and-quality queries, image scanning, provenance, and SBOM generation.
- Upgraded vulnerable framework and agent dependencies to a clean runtime audit.
- Removed an unused alternate HTTP client from the development dependency surface.
- Upgraded pytest to the patched 9.0.3 release after a development-tool advisory.

### Changed

- Replaced API-process background tasks with Redis-backed at-least-once delivery, leased workers, capped retries, stale recovery, idempotent submission, and dead letters.
- Added ordered LLM fallback providers and per-job model/tool/token usage accounting.
- Production data providers fail closed when demo data is disabled.
- Redis status/results use isolated namespaces and persistent-store failures no longer silently fall back in process.
- Kubernetes now requires managed Redis and an immutable image digest; it runs non-root with a read-only filesystem, bounded resources, startup/readiness/liveness probes, topology spread, and a disruption budget.
- Runtime dependencies are reproducibly hash locked for Python 3.12.
- GitHub governance matches the Atlas baseline: squash-only review flow, signed commits, CODEOWNERS, protected checks, selected Actions, and automatic branch cleanup.

### Added

- Added deterministic source provenance and mandatory claim-level report citations.
- Added dedicated Docker Compose and Kubernetes worker topology with graceful shutdown semantics.
- Added a manually dispatched, protected live-provider grounding evaluation that never runs on pull requests.
- Deterministic agent quality evals and runtime validation for model-generated report enums, bounds, lengths, and extra fields.
- Architecture, operations, failure-mode, contribution, security, and documentation-map guidance.
- A contributor-facing `make gate` covering lint, strict typing, tests, evals, and dependency audit.
