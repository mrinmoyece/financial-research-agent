# Failure Modes

| Failure | Effect | Detection | Recovery |
|---|---|---|---|
| Redis unavailable at startup | Pod fails startup when Redis is required | Crash logs and unavailable readiness | Restore managed Redis connectivity, then restart |
| Redis fails after startup | Job operation returns `503`; no in-memory split-brain fallback | HTTP 503 and job-store error logs | Restore Redis and retry the request |
| API process restarts after submission | Accepted work remains queued | API restart and queue-age metrics | Restore API; workers continue independently |
| Worker exits during research | Lease eventually expires and the job is redelivered | Worker restart, stale-lease, and retry events | Restore workers; investigate duplicate external calls by job ID |
| Attempts exhausted | Job moves to dead letter and status becomes failed | Dead-letter count and audit event | Correct the cause, document the decision, then replay as a new idempotent request |
| LLM credentials missing | Readiness returns `503` | Readiness alert | Restore the selected provider secret |
| LLM or data provider timeout | Tool error is recorded; job fails if no usable data remains | Failed job and provider error logs | Retry after provider recovery |
| Provider key missing in production | No illustrative record is substituted | Failed job with provider configuration error | Configure the provider or change the approved research depth |
| Invalid model report | Report is rejected by the runtime schema | Failed job and validation log | Inspect prompt/model behavior and add an eval regression |
| Oversized request | Request returns `413`, including chunked bodies | Gateway/API 413 metrics | Correct the client; raise the bounded setting only after review |
| Invalid principal credential or role | Route returns `401` or `403` | Gateway/API auth metrics and audit context | Correct/rotate the credential or role assignment |
| Tenant attempts cross-tenant access | Resource is returned as `404` | Security telemetry | Investigate the caller; do not reveal resource existence |
| Prompt-injection pattern in provider data | Tool result is blocked or redacted | Unsafe-content logs and failed/reduced report | Inspect the source, tune only with an adversarial regression, and rerun |
| Primary LLM unavailable | Ordered fallback is attempted | Provider failure/fallback logs | Restore primary; verify fallback policy, residency, and spend |
| Report awaits approval too long | Report remains hidden | Approval-age alert | Escalate to an authorized reviewer; never bypass by editing Redis |
| LangSmith unavailable | Research continues; trace delivery may fail | Trace exporter logs | Restore tracing or disable it; do not treat tracing as the audit source |

## Explicit limitations

- Generated analysis is not investment advice and always requires qualified human review.
- Identity is for service principals, not interactive end users; user-level authorization remains at the gateway.
- Queue delivery is at least once, so downstream side effects must be idempotent.
- Prompt-injection screening and citation validation reduce risk but do not establish factual correctness.
- The documented RTO/RPO are targets and require the managed-platform controls in the operations guide.
