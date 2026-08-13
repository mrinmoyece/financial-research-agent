# Operations Guide

This guide defines the supported production controls and the limits of the reference deployment.

## Required production configuration

| Setting | Production value | Purpose |
|---|---|---|
| `ALLOW_MOCK_DATA` | `false` | Prevent illustrative records from being presented as current data |
| `REDIS_REQUIRED` | `true` | Fail startup when shared job state is unavailable |
| `REDIS_URL` | Managed Redis endpoint | Share state across API replicas |
| `API_AUTH_REQUIRED` | `true` | Require the service API key on research routes |
| `API_KEY` | Secret reference | Authenticate the calling service |
| `MAX_REQUEST_BODY_BYTES` | `65536` | Bound buffered and chunked HTTP request bodies |
| `CORS_ALLOW_ORIGINS` | Explicit HTTPS origins | Restrict browser callers |
| `EXPOSE_API_DOCS` | `false` | Remove public OpenAPI interfaces |
| `LLM_PROVIDER` | Approved provider | Select the controlled model endpoint |
| Provider credentials | Secret references | Keep credentials outside images and manifests |

The built-in API key authenticates a calling service with constant-time comparison. It is not an end-user identity or RBAC system. Terminate TLS and enforce user authentication, authorization, rate limits, stricter edge body limits, and abuse controls at an ingress or API gateway.

## Data integrity

When mock data is disabled, unavailable market/news providers return no research data instead of silently substituting examples. The job fails if no usable market or news data is gathered. Provider responses and generated reports still require human review; the application does not guarantee accuracy, timeliness, or suitability for investment decisions.

## Health model

- `/api/v1/health` is a process liveness endpoint and performs no dependency calls.
- `/api/v1/ready` validates credentials for the selected LLM provider and confirms that a required Redis backend was established.
- A successful readiness response does not guarantee that external market-data or LLM providers will accept the next request.

Kubernetes uses startup, liveness, and readiness probes. Do not make liveness depend on external services; that can amplify an upstream outage through restart loops.

## Job execution and recovery

Redis stores status and result records in separate namespaces with TTLs. Research execution still uses FastAPI `BackgroundTasks`, so work is tied to the API process:

- a process restart can interrupt a running job;
- interrupted jobs may remain `pending` or `running` until their TTL expires;
- there is no durable retry, dead-letter queue, or exactly-once guarantee.

For strict delivery guarantees, move execution to a durable queue and dedicated workers before production adoption.

## Deployment

1. Build and scan the image in CI.
2. Deploy the immutable image digest, never `latest`.
3. Use a managed, highly available Redis service over TLS. The production manifest does not deploy Redis.
4. Apply secrets through the platform secret manager.
5. Confirm `/api/v1/ready` returns `200` and `job_store` is `redis`.
6. Submit a non-sensitive canary job with `X-API-Key` and verify completion.
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

The service key is supplied only through the platform secret manager. To rotate it, update the secret, roll all pods, verify authenticated canary traffic, and then revoke the old gateway credential. The application supports one active key; use the gateway for overlap windows and per-client credentials.

Every response receives no-store, HSTS, CSP, permissions-policy, referrer-policy, frame, and MIME-sniffing headers. Research request bodies are bounded in-process, including chunked requests.
