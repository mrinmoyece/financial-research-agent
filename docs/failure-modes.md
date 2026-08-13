# Failure Modes

| Failure | Effect | Detection | Recovery |
|---|---|---|---|
| Redis unavailable at startup | Pod fails startup when Redis is required | Crash logs and unavailable readiness | Restore managed Redis connectivity, then restart |
| Redis fails after startup | Job operation returns `503`; no in-memory split-brain fallback | HTTP 503 and job-store error logs | Restore Redis and retry the request |
| API process restarts during research | In-flight background job may remain pending/running until TTL | Job age alert and pod restart event | Retry with a new job; adopt a durable queue for delivery guarantees |
| LLM credentials missing | Readiness returns `503` | Readiness alert | Restore the selected provider secret |
| LLM or data provider timeout | Tool error is recorded; job fails if no usable data remains | Failed job and provider error logs | Retry after provider recovery |
| Provider key missing in production | No illustrative record is substituted | Failed job with provider configuration error | Configure the provider or change the approved research depth |
| Invalid model report | Report is rejected by the runtime schema | Failed job and validation log | Inspect prompt/model behavior and add an eval regression |
| Oversized request | Request returns `413`, including chunked bodies | Gateway/API 413 metrics | Correct the client; raise the bounded setting only after review |
| Invalid service API key | Research route returns `401` | Gateway/API 401 metrics | Rotate or correct the caller credential |
| LangSmith unavailable | Research continues; trace delivery may fail | Trace exporter logs | Restore tracing or disable it; do not treat tracing as the audit source |

## Explicit limitations

- Generated analysis is not investment advice and always requires qualified human review.
- The service has one service-level API key, not end-user RBAC or tenant isolation.
- Background execution is not a durable work queue and has no dead-letter queue or exactly-once guarantee.
- The reference repository does not define a multi-region recovery objective. Set RTO/RPO only after selecting managed Redis, queue, gateway, and secret-management platforms.
