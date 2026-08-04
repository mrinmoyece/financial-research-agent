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
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field, field_validator

from src.api.job_store import JobStore
from src.config.llm import configure_langsmith
from src.config.settings import get_settings
from src.graph.workflow import run_research

logger = logging.getLogger(__name__)

# ── Request / response models ──────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=5, max_length=500,
                       description="Natural language research query, e.g. 'Analyse NVDA for long-term hold'")
    tickers: list[str] = Field(..., min_length=1, max_length=5,
                               description="List of ticker symbols")
    research_depth: Literal["quick", "standard", "deep"] = Field(
        "standard",
        description="quick=market data only | standard=+news+macro | deep=+SEC filings",
    )

    @field_validator("tickers")
    @classmethod
    def normalise_tickers(cls, v: list[str]) -> list[str]:
        return [t.upper().strip() for t in v if t.strip()]


class JobStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    created_at: str
    completed_at: str | None = None
    report: dict | None = None
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

_jobs = JobStore(get_settings())
_results = JobStore(get_settings())


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
        job.completed_at = datetime.now(timezone.utc).isoformat()
        job.report = final_state.get("report")
        job.error = final_state.get("error")
        job.tool_calls_count = len(final_state.get("tool_calls_log", []))
        _save_job(job)
        logger.info("job %s completed status=%s", job_id, job.status)
    except Exception as exc:
        logger.error("job %s failed: %s", job_id, exc, exc_info=True)
        job.status = "failed"
        job.error = str(exc)
        job.completed_at = datetime.now(timezone.utc).isoformat()
        _save_job(job)


# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()
    configure_langsmith(settings)

    app = FastAPI(
        title="Financial Research Agent",
        description=(
            "LangGraph-powered autonomous agent for equity research. "
            "Gathers market data, news, macro indicators and SEC filings, "
            "then synthesises a structured investment report."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],   # tighten for production
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Prometheus metrics
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    # ── Routes ─────────────────────────────────────────────────────────────────

    @app.post("/api/v1/research", response_model=JobStatus, status_code=202)
    async def submit_research(request: ResearchRequest, background_tasks: BackgroundTasks):
        """
        Submit a research job. Returns immediately with a job_id.
        Poll GET /api/v1/research/{job_id} for the result.
        """
        job_id = str(uuid.uuid4())
        job = JobStatus(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        _save_job(job)
        background_tasks.add_task(_execute_research, job_id, request)
        logger.info("research job submitted job_id=%s tickers=%s", job_id, request.tickers)
        return job

    @app.get("/api/v1/research/{job_id}", response_model=JobStatus)
    async def get_research_result(job_id: str):
        """Poll for job result. Status: pending | running | completed | failed."""
        job = _get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/api/v1/ready")
    async def ready():
        """
        Readiness probe — verifies configuration is loaded.
        Does NOT call the LLM (expensive); Kubernetes should call /health for liveness.
        """
        cfg = get_settings()
        issues = []
        if not cfg.azure_openai_endpoint and cfg.llm_provider == "azure_openai":
            issues.append("AZURE_OPENAI_ENDPOINT not configured")
        if issues:
            return JSONResponse(status_code=503, content={"status": "not_ready", "issues": issues})
        return {"status": "ready"}

    @app.get("/api/v1/research")
    async def list_jobs(limit: int = 20):
        """List recent research jobs."""
        jobs = list(_jobs.values())[-limit:]
        return {"jobs": jobs, "total": len(_jobs)}

    return app


app = create_app()
