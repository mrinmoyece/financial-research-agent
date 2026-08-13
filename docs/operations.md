# Operations Guide

This guide defines the supported production controls and the limits of the reference deployment.

## Required production configuration

| Setting | Production value | Purpose |
|---|---|---|
| `ALLOW_MOCK_DATA` | `false` | Prevent illustrative records from being presented as current data |
| `REDIS_REQUIRED` | `true` | Fail startup when shared job state is unavailable |
| `REDIS_URL` | Managed Redis endpoint | Share state across API replicas |
| `API_AUTH_REQUIRED` | `true` | Require the service API key on research routes |
| `API_PRINCIPALS_JSON` | Secret reference | Define tenant-scoped service principals and roles |
| `REQUIRE_REPORT_APPROVAL` | `true` | Withhold generated reports pending human approval |
| `JOB_RETENTION_SECONDS` | Review-window plus incident margin | Retain status and report payloads through approval |
| `MAX_MODEL_CALLS_PER_JOB` / `MAX_TOOL_CALLS_PER_JOB` | Reviewed bounds | Limit autonomous execution and cost |
| `PROMPT_INJECTION_POLICY` | `block` | Reject provider content matching the injection policy |
| `MAX_REQUEST_BODY_BYTES` | `65536` | Bound buffered and chunked HTTP request bodies |
| `CORS_ALLOW_ORIGINS` | Explicit HTTPS origins | Restrict browser callers |
| `EXPOSE_API_DOCS` | `false` | Remove public OpenAPI interfaces |
| `LLM_PROVIDER` | Approved provider | Select the controlled model endpoint |
| Provider credentials | Secret references | Keep credentials outside images and manifests |

`API_PRINCIPALS_JSON` is an array of `{principal_id, tenant_id, roles, api_key}` objects. Credentials are converted to independently salted PBKDF2-HMAC-SHA256 digests at startup and compared in constant time. `API_KEY` remains a legacy single-tenant compatibility path with all roles. Terminate TLS and enforce end-user authorization, stricter edge limits, and abuse controls at the gateway.

## Data integrity

When mock data is disabled, unavailable market/news providers return no research data instead of silently substituting examples. The job fails if no usable market or news data is gathered. Provider responses and generated reports still require human review; the application does not guarantee accuracy, timeliness, or suitability for investment decisions.

## Health model

- `/api/v1/health` is a process liveness endpoint and performs no dependency calls.
- `/api/v1/ready` validates the selected LLM configuration and confirms required Redis job-store, queue, and governance backends.
- A successful readiness response does not guarantee that external market-data or LLM providers will accept the next request.

Kubernetes uses startup, liveness, and readiness probes. Do not make liveness depend on external services; that can amplify an upstream outage through restart loops.

## Job execution and recovery

The API and workers share Redis. Submission is idempotent when callers provide an `Idempotency-Key` of 8-128 safe characters. Workers claim with a lease, retry with exponential backoff, recover stale leases, and dead-letter after the configured attempt cap. A worker maintenance pass reconciles dead letters to terminal status and releases tenant capacity. Delivery is at least once, not exactly once.

Run the API and worker as separate process groups. Graceful worker termination must exceed `JOB_LEASE_SECONDS`; the reference manifest uses 330 seconds for a 300-second lease. Inspect dead letters and audit events during incidents before replaying work.

Completed output normally enters `awaiting_approval`. `POST /api/v1/research/{job_id}/approve` releases it; `/reject` records a bounded reason and keeps the report hidden. Both require an `approver` or `admin` principal in the job tenant.

## Deployment

1. Build and scan the image in CI.
2. Deploy the immutable image digest, never `latest`.
3. Use a managed, highly available Redis service over TLS. The production manifest does not deploy Redis.
4. Apply secrets through the platform secret manager.
5. Confirm `/api/v1/ready` returns `200` and all three backend fields are `redis`.
6. Confirm both API and worker Deployments are available.
7. Submit a non-sensitive canary with a researcher principal, approve it with a separate approver principal, and verify the audit sequence.
7. Monitor error rate and latency during rollout before increasing traffic.

The PodDisruptionBudget protects one application replica during voluntary disruptions, and the manifest spreads replicas across zones when possible. Use multi-zone Redis according to the target platform's availability requirements.

## Observability

Prometheus metrics are exposed at `/metrics`. Restrict that endpoint to the monitoring network. Alert at minimum on:

- HTTP 5xx rate and latency;
- readiness failures;
- failed and long-running jobs;
- Redis connection failures;
- provider throttling, timeouts, and authentication errors;
- abnormal LLM token consumption.
- queue age, retry rate, dead-letter count, approval age, and worker availability.

LangSmith tracing can contain prompts, provider data, and generated financial analysis. Enable it only after approving data residency, retention, redaction, and access controls.

## Incident response

1. Remove the service from traffic if outputs may be unsafe or data integrity is uncertain.
2. Preserve application, gateway, deployment, and provider audit logs.
3. Revoke affected credentials through the secret manager.
4. Identify impacted job IDs and downstream consumers.
5. Deploy a reviewed immutable image; do not mutate a running container.
6. Document scope, customer impact, corrective actions, and follow-up controls.

Report suspected vulnerabilities using the process in [SECURITY.md](../SECURITY.md).

## Security and key rotation

Principal configuration is supplied only through the platform secret manager. For rotation, add the replacement credential, roll API pods, verify it, remove the old credential, and roll again. Worker pods do not need principal credentials, but the shared reference secret is mounted for deployment simplicity; use separate least-privilege secrets in a mature platform.

Every response receives no-store, HSTS, CSP, permissions-policy, referrer-policy, frame, and MIME-sniffing headers. Research request bodies are bounded in-process, including chunked requests.

## Service objectives and recovery

Reference objectives are 99.9% monthly submission/poll availability, p95 accepted-submission latency under 500 ms excluding gateway transit, and p95 queue wait under 60 seconds at planned load. Alert when approval age exceeds the business review window. The target RTO is 60 minutes and target RPO is 5 minutes; achieving them requires multi-zone Redis persistence, tested backups, immutable image availability, and quarterly restore exercises.
