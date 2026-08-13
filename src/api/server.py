"""
FastAPI server — exposes the LangGraph research workflow as a REST API.

Endpoints:
  POST /api/v1/research          Run a full research workflow
  GET  /api/v1/research/{job_id} Poll for async job result
  GET  /api/v1/health            Liveness check
  GET  /api/v1/ready             Readiness check (verifies LLM connectivity)
  GET  /metrics                  Prometheus metrics (via prometheus-fastapi-instrumentator)
"""

import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, field_validator
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.job_store import JobStore, JobStoreError
from src.config.llm import configure_langsmith
from src.config.settings import get_settings
from src.graph.workflow import run_research

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


async def _require_api_key(provided_key: str | None = Depends(_api_key_header)) -> None:
    settings = get_settings()
    if not settings.api_auth_required:
        return
    expected_key = settings.api_key.get_secret_value()
    if not provided_key or not secrets.compare_digest(provided_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Request / response models ──────────────────────────────────────────────────


class ResearchRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=5,
        max_length=500,
        description="Natural language research query, e.g. 'Analyse NVDA for long-term hold'",
    )
    tickers: list[str] = Field(
        ..., min_length=1, max_length=5, description="List of ticker symbols"
    )
    research_depth: Literal["quick", "standard", "deep"] = Field(
        "standard",
        description="quick=market data only | standard=+news+macro | deep=+SEC filings",
    )

    @field_validator("tickers")
    @classmethod
    def normalise_tickers(cls, v: list[str]) -> list[str]:
        tickers = list(dict.fromkeys(t.upper().strip() for t in v if t.strip()))
        if not tickers:
            raise ValueError("at least one non-blank ticker is required")
        invalid = [
            ticker for ticker in tickers if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker)
        ]
        if invalid:
            raise ValueError(f"invalid ticker symbol(s): {', '.join(invalid)}")
        return tickers


class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    completed_at: str | None = None
    report: dict[str, object] | None = None
    error: str | None = None
    tool_calls_count: int = 0


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


def _get_job(job_id: str) -> JobStatus | None:
    raw = _jobs.get(job_id)
    return JobStatus.model_validate(raw) if raw is not None else None


def _save_job(job: JobStatus) -> None:
    _jobs.set(job.job_id, job.model_dump(mode="json"))


async def _execute_research(job_id: str, request: ResearchRequest) -> None:
    """Background task that runs the graph and updates job status."""
    job = _get_job(job_id)
    if job is None:
        logger.error("job %s: not found in store at execution time", job_id)
        return
    try:
        job.status = "running"
        _save_job(job)

        final_state = await run_research(
            query=request.query,
            tickers=request.tickers,
            research_depth=request.research_depth,
        )
        _results.set(job_id, dict(final_state))

        job.status = "completed" if not final_state.get("error") else "failed"
        job.completed_at = datetime.now(UTC).isoformat()
        report = final_state.get("report")
        job.report = dict(report) if report is not None else None
        job.error = final_state.get("error")
        job.tool_calls_count = len(final_state.get("tool_calls_log", []))
        _save_job(job)
        logger.info("job %s completed status=%s", job_id, job.status)
    except Exception as exc:
        logger.error("job %s failed: %s", job_id, exc, exc_info=True)
        job.status = "failed"
        job.error = "Research execution failed. Check server logs with the job ID."
        job.completed_at = datetime.now(UTC).isoformat()
        _save_job(job)


# ── Application factory ────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.api_auth_required and not settings.api_key.get_secret_value():
        raise ValueError("API_KEY must be configured when API_AUTH_REQUIRED=true")
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

    # Prometheus metrics
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    # ── Routes ─────────────────────────────────────────────────────────────────

    auth = [Depends(_require_api_key)]

    @app.post(
        "/api/v1/research",
        response_model=JobStatus,
        status_code=202,
        dependencies=auth,
    )
    async def submit_research(
        request: ResearchRequest, background_tasks: BackgroundTasks
    ) -> JobStatus:
        """
        Submit a research job. Returns immediately with a job_id.
        Poll GET /api/v1/research/{job_id} for the result.
        """
        job_id = str(uuid.uuid4())
        job = JobStatus(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
        )
        _save_job(job)
        background_tasks.add_task(_execute_research, job_id, request)
        logger.info("research job submitted job_id=%s tickers=%s", job_id, request.tickers)
        return job

    @app.get(
        "/api/v1/research/{job_id}",
        response_model=JobStatus,
        dependencies=auth,
    )
    async def get_research_result(job_id: str) -> JobStatus:
        """Poll for job result. Status: pending | running | completed | failed."""
        job = _get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
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
            if _jobs.backend != "redis" or _results.backend != "redis":
                issues.append("required Redis job store unavailable")
            else:
                try:
                    _jobs.ping()
                    _results.ping()
                except JobStoreError:
                    issues.append("required Redis job store unhealthy")
        if issues:
            return JSONResponse(status_code=503, content={"status": "not_ready", "issues": issues})
        return JSONResponse(content={"status": "ready", "job_store": _jobs.backend})

    @app.get("/api/v1/research", dependencies=auth)
    async def list_jobs(limit: int = Query(20, ge=1, le=100)) -> dict[str, object]:
        """List recent research jobs."""
        jobs = sorted(_jobs.values(), key=lambda item: item.get("created_at", ""), reverse=True)
        return {"jobs": jobs[:limit], "total": len(jobs)}

    return app


app = create_app()
