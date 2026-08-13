# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and semantic versioning.

## [Unreleased]

### Security

- Added constant-time service API-key authentication, bounded request bodies, explicit CORS controls, security headers, private vulnerability reporting guidance, and fail-fast production configuration.
- Pinned workflow actions and container bases to immutable SHAs/digests; added history secret scanning, dependency review, CodeQL security-and-quality queries, image scanning, provenance, and SBOM generation.
- Upgraded vulnerable framework and agent dependencies to a clean runtime audit.

### Changed

- Production data providers fail closed when demo data is disabled.
- Redis status/results use isolated namespaces and persistent-store failures no longer silently fall back in process.
- Kubernetes now requires managed Redis and an immutable image digest; it runs non-root with a read-only filesystem, bounded resources, startup/readiness/liveness probes, topology spread, and a disruption budget.
- Runtime dependencies are reproducibly hash locked for Python 3.12.
- GitHub governance matches the Atlas baseline: squash-only review flow, signed commits, CODEOWNERS, protected checks, selected Actions, and automatic branch cleanup.

### Added

- Deterministic agent quality evals and runtime validation for model-generated report enums, bounds, lengths, and extra fields.
- Architecture, operations, failure-mode, contribution, security, and documentation-map guidance.
- A contributor-facing `make gate` covering lint, strict typing, tests, evals, and dependency audit.
