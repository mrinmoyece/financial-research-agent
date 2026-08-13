"""
Integration tests for the FastAPI server.

These tests use TestClient and mock the graph's ainvoke to avoid
hitting real LLMs or APIs in CI.  They verify HTTP contract, status
codes, and job lifecycle.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.governance import GovernanceStore
from src.api.job_queue import JobEnvelope, JobQueue
from src.api.job_store import JobStore
from src.api.server import (
    ResearchRequest,
    _governance,
    _jobs,
    _results,
    app,
    create_app,
)
from src.config.settings import Settings
from src.worker import ResearchProcessor


@pytest.fixture(autouse=True)
def clear_job_store():
    """Reset the job store (Redis-backed or in-memory) between tests."""
    _jobs.clear()
    _results.clear()
    yield
    _jobs.clear()
    _results.clear()


@pytest.fixture
def client():
    return TestClient(app)


_MOCK_REPORT = {
    "executive_summary": "NVDA shows exceptional growth driven by AI compute demand.",
    "investment_thesis": "Market leader in AI accelerators with dominant software moat.",
    "bull_case": "Blackwell ramp drives 100%+ data centre revenue growth in FY2025.",
    "bear_case": "Export controls and high valuation limit upside.",
    "risk_rating": "MEDIUM",
    "recommended_action": "BUY",
    "price_target_12m": 1050.0,
    "confidence_score": 0.87,
    "data_sources_used": ["market_data", "news", "macro"],
    "citations": [{"source_id": "src_aaaaaaaaaaaa", "claim": "Summary"}],
    "generated_at": "2024-11-21T20:00:00Z",
}

_MOCK_FINAL_STATE = {
    "query": "Analyse NVDA",
    "tickers": ["NVDA"],
    "research_depth": "standard",
    "ticker_analyses": [],
    "news_items": [],
    "macro_indicators": [],
    "sources": [],
    "tool_calls_log": [
        {
            "tool": "get_market_data",
            "args": {"ticker": "NVDA"},
            "result_type": "dict",
            "timestamp": "2024-11-21T20:00:00Z",
            "iteration": 1,
        }
    ],
    "messages": [],
    "model_calls": 1,
    "input_tokens": 100,
    "output_tokens": 50,
    "report": _MOCK_REPORT,
    "error": None,
}


class TestHealthEndpoints:
    def test_health_returns_ok(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_ready_returns_200_or_503(self, client):
        resp = client.get("/api/v1/ready")
        # 200 (configured) or 503 (no key set in test env) — both are valid responses
        assert resp.status_code in (200, 503)

    def test_security_headers_are_applied(self, client):
        resp = client.get("/api/v1/health")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["cache-control"] == "no-store"


class TestResearchJobLifecycle:
    def test_submit_returns_202_with_job_id(self, client):
        resp = client.post(
            "/api/v1/research",
            json={
                "query": "Analyse NVDA for long-term investment",
                "tickers": ["NVDA"],
                "research_depth": "standard",
            },
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "pending"

    def test_get_job_returns_pending_immediately(self, client):
        submit = client.post(
            "/api/v1/research",
            json={
                "query": "Analyse NVDA",
                "tickers": ["NVDA"],
                "research_depth": "quick",
            },
        )
        job_id = submit.json()["job_id"]

        resp = client.get(f"/api/v1/research/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == job_id

    def test_get_unknown_job_returns_404(self, client):
        resp = client.get("/api/v1/research/nonexistent-job-id")
        assert resp.status_code == 404

    def test_submit_validates_empty_query(self, client):
        resp = client.post(
            "/api/v1/research",
            json={
                "query": "hi",  # too short (min_length=5)
                "tickers": ["NVDA"],
            },
        )
        assert resp.status_code == 422

    def test_submit_validates_empty_tickers(self, client):
        resp = client.post(
            "/api/v1/research",
            json={
                "query": "Analyse something please",
                "tickers": [],
            },
        )
        assert resp.status_code == 422

    def test_submit_normalises_lowercase_tickers(self, client):
        resp = client.post(
            "/api/v1/research",
            json={
                "query": "Analyse nvidia stock please",
                "tickers": ["nvda"],
            },
        )
        assert resp.status_code == 202

    @pytest.mark.parametrize("tickers", [[], [""], ["$$$"], ["A" * 11]])
    def test_submit_rejects_invalid_tickers(self, client, tickers):
        resp = client.post(
            "/api/v1/research",
            json={
                "query": "Analyse this stock please",
                "tickers": tickers,
            },
        )
        assert resp.status_code == 422

    def test_list_jobs_returns_empty_initially(self, client):
        resp = client.get("/api/v1/research")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_jobs_validates_limit(self, client):
        assert client.get("/api/v1/research?limit=0").status_code == 422
        assert client.get("/api/v1/research?limit=101").status_code == 422

    def test_list_jobs_after_submission(self, client):
        client.post(
            "/api/v1/research",
            json={
                "query": "Analyse NVDA stock now",
                "tickers": ["NVDA"],
            },
        )
        resp = client.get("/api/v1/research")
        assert resp.json()["total"] == 1

    def test_rejects_oversized_request_body(self, client):
        resp = client.post(
            "/api/v1/research",
            content=b"x" * 70000,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413

    def test_idempotency_key_returns_same_job(self, client):
        payload = {"query": "Analyse NVDA stock now", "tickers": ["NVDA"]}
        headers = {"Idempotency-Key": "request-12345678"}
        first = client.post("/api/v1/research", json=payload, headers=headers)
        second = client.post("/api/v1/research", json=payload, headers=headers)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]


class TestProductionControls:
    def test_authentication_is_enforced(self):
        settings = Settings(
            llm_provider="github_models",
            github_token="provider-token",
            api_auth_required=True,
            api_key="service-secret",
        )
        with patch("src.api.server.get_settings", return_value=settings):
            protected_app = create_app()
            with TestClient(protected_app) as protected:
                payload = {
                    "query": "Analyse NVDA stock",
                    "tickers": ["NVDA"],
                }
                assert protected.post("/api/v1/research", json=payload).status_code == 401
                assert (
                    protected.post(
                        "/api/v1/research",
                        json=payload,
                        headers={"X-API-Key": "wrong"},
                    ).status_code
                    == 401
                )
                response = protected.post(
                    "/api/v1/research",
                    json=payload,
                    headers={"X-API-Key": "service-secret"},
                )
                assert response.status_code == 202

    def test_authentication_configuration_fails_fast(self):
        settings = Settings(api_auth_required=True, api_key="")
        with (
            patch("src.api.server.get_settings", return_value=settings),
            pytest.raises(ValueError, match="must be configured"),
        ):
            create_app()

    @pytest.mark.asyncio
    async def test_worker_job_success_and_safe_failure(self):
        settings = Settings(require_report_approval=False, job_max_attempts=1)
        jobs = JobStore(namespace="worker-test-status")
        results = JobStore(namespace="worker-test-result")
        processor = ResearchProcessor(settings, jobs, results, _governance)
        request = ResearchRequest(
            query="Analyse NVDA stock",
            tickers=["NVDA"],
        )
        job_id = "job-success"
        jobs.set(
            job_id,
            {
                "job_id": job_id,
                "status": "pending",
                "tenant_id": "tenant",
                "principal_id": "principal",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        envelope = JobEnvelope(
            job_id=job_id,
            request=request.model_dump(mode="json"),
            principal_id="principal",
            tenant_id="tenant",
            attempt=1,
            enqueued_at=0,
        )
        with patch("src.worker.run_research", new_callable=AsyncMock) as run:
            run.return_value = {
                "query": request.query,
                "tickers": request.tickers,
                "research_depth": request.research_depth,
                "ticker_analyses": [],
                "news_items": [],
                "macro_indicators": [],
                "sources": [],
                "tool_calls_log": [],
                "messages": [],
                "model_calls": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "report": None,
                "error": None,
            }
            await processor(envelope)
        assert jobs.get(job_id)["status"] == "completed"

        failure_id = "job-failure"
        jobs.set(
            failure_id,
            {
                "job_id": failure_id,
                "status": "pending",
                "tenant_id": "tenant",
                "principal_id": "principal",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        with patch(
            "src.worker.run_research",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sensitive provider detail"),
        ):
            with pytest.raises(RuntimeError, match="sensitive provider detail"):
                await processor(
                    JobEnvelope(
                        job_id=failure_id,
                        request=request.model_dump(mode="json"),
                        principal_id="principal",
                        tenant_id="tenant",
                        attempt=1,
                        enqueued_at=0,
                    )
                )
        failed = jobs.get(failure_id)
        assert failed["status"] == "failed"
        assert "sensitive provider detail" not in failed["error"]

    @pytest.mark.asyncio
    async def test_stale_final_attempt_reconciles_status_and_tenant_slot(self):
        now = [1000.0]
        settings = Settings(job_max_attempts=1, max_concurrent_jobs_per_tenant=1)
        queue = JobQueue(
            max_attempts=1,
            lease_seconds=10,
            clock=lambda: now[0],
        )
        jobs = JobStore(namespace="stale-status")
        results = JobStore(namespace="stale-result")
        governance = GovernanceStore(settings)
        processor = ResearchProcessor(settings, jobs, results, governance)
        assert governance.acquire_job_slot("tenant")
        jobs.set(
            "stale-job",
            {
                "job_id": "stale-job",
                "status": "running",
                "tenant_id": "tenant",
                "principal_id": "principal",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        assert queue.enqueue(
            "stale-job",
            {"query": "Analyse NVDA", "tickers": ["NVDA"]},
            principal_id="principal",
            tenant_id="tenant",
        )
        assert queue.claim(0.01) is not None
        now[0] += 11
        assert queue.recover_stale() == 1
        await processor.reconcile_dead_letters(queue)
        assert jobs.get("stale-job")["status"] == "failed"
        assert queue.dead_letter_count == 0
        assert governance.acquire_job_slot("tenant")

    def test_tenant_isolation_rbac_and_approval(self):
        settings = Settings(
            llm_provider="github_models",
            github_token="provider-token",
            api_auth_required=True,
            api_principals_json=json.dumps(
                [
                    {
                        "principal_id": "researcher",
                        "tenant_id": "tenant-a",
                        "roles": ["reader", "researcher"],
                        "api_key": "researcher-key-123",
                    },
                    {
                        "principal_id": "approver",
                        "tenant_id": "tenant-a",
                        "roles": ["reader", "approver"],
                        "api_key": "approver-key-1234",
                    },
                    {
                        "principal_id": "other",
                        "tenant_id": "tenant-b",
                        "roles": ["reader"],
                        "api_key": "other-tenant-key",
                    },
                ]
            ),
        )
        with patch("src.api.server.get_settings", return_value=settings):
            protected_app = create_app()
        with TestClient(protected_app) as protected:
            payload = {"query": "Analyse NVDA stock", "tickers": ["NVDA"]}
            submitted = protected.post(
                "/api/v1/research",
                json=payload,
                headers={"X-API-Key": "researcher-key-123"},
            )
            assert submitted.status_code == 202
            job_id = submitted.json()["job_id"]
            assert (
                protected.get(
                    f"/api/v1/research/{job_id}",
                    headers={"X-API-Key": "other-tenant-key"},
                ).status_code
                == 404
            )
            assert (
                protected.post(
                    f"/api/v1/research/{job_id}/approve",
                    headers={"X-API-Key": "researcher-key-123"},
                ).status_code
                == 403
            )

            raw = _jobs.get(job_id)
            raw["status"] = "awaiting_approval"
            _jobs.set(job_id, raw)
            _results.set(job_id, {"report": _MOCK_REPORT})
            approved = protected.post(
                f"/api/v1/research/{job_id}/approve",
                headers={"X-API-Key": "approver-key-1234"},
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"
            assert approved.json()["report"]["recommended_action"] == "BUY"
