"""
FastAPI server — exposes the LangGraph research workflow as a REST API.

Endpoints:
  POST /api/v1/research          Run a full research workflow
  GET  /api/v1/research/{job_id} Poll for async job result
  GET  /api/v1/health            Liveness check
  GET  /api/v1/ready             Readiness check (verifies LLM connectivity)
  GET  /metrics                  Prometheus metrics (via prometheus-fastapi-instrumentator)
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.contracts import JobStatus, ResearchRequest
from src.api.governance import GovernanceStore
from src.api.job_queue import JobQueue, JobQueueError
from src.api.job_store import JobStore, JobStoreError
from src.config.llm import configure_langsmith
from src.config.settings import get_settings
from src.security.identity import Principal, PrincipalRegistry, Role, has_role

logger = logging.getLogger(__name__)


class RequestBodyTooLargeError(Exception):
    """Raised when an HTTP request exceeds the configured body limit."""


class RequestBodyLimitMiddleware:
    """Bound request bodies even when a caller omits Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                await JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )(scope, receive, send)
                return
            if declared_length > self.max_bytes:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds the configured limit"},
                )(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await JSONResponse(
                status_code=413,
                content={"detail": "Request body exceeds the configured limit"},
            )(scope, receive, send)


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class RejectionRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


# ── Job store ────────────────────────────────────────────────────────────────
#
# Redis-backed when REDIS_URL / settings.redis_url points at a reachable
# Redis instance (see docker-compose.yml and k8s/deployment.yaml — both
# provision Redis specifically so job state is shared across the HPA's
# 2-8 replicas). Falls back automatically to an in-process dict if Redis
# is unset or unreachable, so local dev / CI / sandboxes without Redis
# keep working — see src/api/job_store.py.
#
# _jobs stores JobStatus and _results stores the full AgentState produced
# by the graph, each as JSON-able dicts keyed by job_id.

_jobs = JobStore(get_settings(), namespace="status")
_results = JobStore(get_settings(), namespace="result")
_queue = JobQueue(get_settings())
_governance = GovernanceStore(get_settings())


def _get_job(job_id: str) -> JobStatus | None:
    raw = _jobs.get(job_id)
    return JobStatus.model_validate(raw) if raw is not None else None


def _save_job(job: JobStatus) -> None:
    _jobs.set(job.job_id, job.model_dump(mode="json"))


# ── Application factory ────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    settings = get_settings()
    principals = PrincipalRegistry(settings)
    configure_langsmith(settings)

    docs_url = "/docs" if settings.expose_api_docs else None
    app = FastAPI(
        title="Financial Research Agent",
        description=(
            "LangGraph-powered autonomous agent for equity research. "
            "Gathers market data, news, macro indicators and SEC filings, "
            "then synthesises a structured research report. "
            "Machine-generated output requires qualified human review."
        ),
        version="1.0.0",
        docs_url=docs_url,
        redoc_url="/redoc" if settings.expose_api_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-API-Key"],
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(JobStoreError)
    async def job_store_error_handler(_request: Request, exc: JobStoreError) -> JSONResponse:
        logger.error("Job store operation failed: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Job store unavailable"})

    @app.exception_handler(JobQueueError)
    async def job_queue_error_handler(_request: Request, exc: JobQueueError) -> JSONResponse:
        logger.error("Job queue operation failed: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Job queue unavailable"})

    # Prometheus metrics
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    # ── Routes ─────────────────────────────────────────────────────────────────

    async def require_principal(
        provided_key: str | None = Depends(_api_key_header),
    ) -> Principal:
        if not settings.api_auth_required:
            return Principal(
                principal_id="local-development",
                tenant_id="default",
                roles=frozenset({"reader", "researcher", "approver", "admin"}),
            )
        principal = principals.authenticate(provided_key)
        if principal is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return principal

    def require_role(principal: Principal, *roles: Role) -> None:
        if not has_role(principal, *roles):
            raise HTTPException(status_code=403, detail="Insufficient role")

    def tenant_job(job_id: str, principal: Principal) -> JobStatus:
        job = _get_job(job_id)
        if job is None or job.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job

    @app.post(
        "/api/v1/research",
        response_model=JobStatus,
        status_code=202,
    )
    async def submit_research(
        request: ResearchRequest,
        principal: Principal = Depends(require_principal),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> JobStatus:
        """
        Submit a research job. Returns immediately with a job_id.
        Poll GET /api/v1/research/{job_id} for the result.
        """
        require_role(principal, "researcher")
        try:
            _governance.enforce_rate_limit(principal)
        except PermissionError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9_.:-]{8,128}", idempotency_key
        ):
            raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
        job_id = (
            "idem-"
            + hashlib.sha256(
                f"{principal.tenant_id}:{principal.principal_id}:{idempotency_key}".encode()
            ).hexdigest()[:32]
            if idempotency_key
            else str(uuid.uuid4())
        )
        existing = _get_job(job_id)
        if existing is not None:
            return existing
        if not _governance.acquire_job_slot(principal.tenant_id):
            raise HTTPException(status_code=429, detail="Tenant concurrent-job limit exceeded")
        job = JobStatus(
            job_id=job_id,
            status="pending",
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            if not _jobs.set_if_absent(job_id, job.model_dump(mode="json")):
                _governance.release_job_slot(principal.tenant_id)
                raced_job = _get_job(job_id)
                if raced_job is None:
                    raise JobStoreError(f"Concurrent job {job_id} became unavailable")
                return raced_job
            enqueued = _queue.enqueue(
                job_id,
                request.model_dump(mode="json"),
                principal_id=principal.principal_id,
                tenant_id=principal.tenant_id,
            )
            if not enqueued:
                _jobs.delete(job_id)
                raise JobQueueError(f"Queue rejected new job {job_id}")
            _governance.append_audit(
                "job_submitted",
                principal,
                job_id=job_id,
                details={"tickers": request.tickers, "depth": request.research_depth},
            )
        except Exception:
            _governance.release_job_slot(principal.tenant_id)
            raise
        logger.info("research job submitted")
        return job

    @app.get(
        "/api/v1/research/{job_id}",
        response_model=JobStatus,
    )
    async def get_research_result(
        job_id: str,
        principal: Principal = Depends(require_principal),
    ) -> JobStatus:
        """Poll for a tenant-scoped job result."""
        require_role(principal, "reader", "researcher", "approver")
        return tenant_job(job_id, principal)

    @app.post("/api/v1/research/{job_id}/approve", response_model=JobStatus)
    async def approve_research(
        job_id: str,
        principal: Principal = Depends(require_principal),
    ) -> JobStatus:
        require_role(principal, "approver")
        job = tenant_job(job_id, principal)
        if job.status != "awaiting_approval":
            raise HTTPException(status_code=409, detail="Job is not awaiting approval")
        result = _results.get(job_id)
        if result is None or not isinstance(result.get("report"), dict):
            raise HTTPException(status_code=409, detail="Job report is unavailable")
        job.status = "approved"
        job.report = result["report"]
        job.approved_at = datetime.now(UTC).isoformat()
        job.approved_by = principal.principal_id
        _save_job(job)
        _governance.append_audit("report_approved", principal, job_id=job_id)
        return job

    @app.post("/api/v1/research/{job_id}/reject", response_model=JobStatus)
    async def reject_research(
        job_id: str,
        rejection: RejectionRequest,
        principal: Principal = Depends(require_principal),
    ) -> JobStatus:
        require_role(principal, "approver")
        job = tenant_job(job_id, principal)
        if job.status != "awaiting_approval":
            raise HTTPException(status_code=409, detail="Job is not awaiting approval")
        job.status = "rejected"
        job.error = "Report rejected during human review."
        job.approved_at = datetime.now(UTC).isoformat()
        job.approved_by = principal.principal_id
        _save_job(job)
        _governance.append_audit(
            "report_rejected",
            principal,
            job_id=job_id,
            details={"reason": rejection.reason},
        )
        return job

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}

    @app.get("/api/v1/ready")
    async def ready() -> JSONResponse:
        """
        Readiness probe — verifies configuration is loaded.
        Does NOT call the LLM (expensive); Kubernetes should call /health for liveness.
        """
        cfg = get_settings()
        issues = []
        if cfg.llm_provider == "azure_openai":
            if not cfg.azure_openai_endpoint:
                issues.append("AZURE_OPENAI_ENDPOINT not configured")
            if not cfg.azure_openai_api_key.get_secret_value():
                issues.append("AZURE_OPENAI_API_KEY not configured")
        elif cfg.llm_provider == "github_models" and not cfg.github_token.get_secret_value():
            issues.append("GITHUB_TOKEN not configured")
        elif cfg.llm_provider == "openai" and not cfg.openai_api_key.get_secret_value():
            issues.append("OPENAI_API_KEY not configured")
        if cfg.redis_required:
            if (
                _jobs.backend != "redis"
                or _results.backend != "redis"
                or _queue.backend != "redis"
                or _governance.backend != "redis"
            ):
                issues.append("required Redis platform backend unavailable")
            else:
                try:
                    _jobs.ping()
                    _results.ping()
                    if not _queue.healthy:
                        issues.append("required Redis job queue unhealthy")
                except (JobStoreError, JobQueueError):
                    issues.append("required Redis platform backend unhealthy")
        if issues:
            return JSONResponse(status_code=503, content={"status": "not_ready", "issues": issues})
        return JSONResponse(
            content={
                "status": "ready",
                "job_store": _jobs.backend,
                "job_queue": _queue.backend,
                "governance": _governance.backend,
            }
        )

    @app.get("/api/v1/research")
    async def list_jobs(
        limit: int = Query(20, ge=1, le=100),
        principal: Principal = Depends(require_principal),
    ) -> dict[str, object]:
        """List recent research jobs."""
        require_role(principal, "reader", "researcher", "approver")
        jobs = sorted(
            (item for item in _jobs.values() if item.get("tenant_id") == principal.tenant_id),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        return {"jobs": jobs[:limit], "total": len(jobs)}

    return app


app = create_app()
