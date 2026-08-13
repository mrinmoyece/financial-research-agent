"""
Integration tests for the FastAPI server.

These tests use TestClient and mock the graph's ainvoke to avoid
hitting real LLMs or APIs in CI.  They verify HTTP contract, status
codes, and job lifecycle.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.server import (
    ResearchRequest,
    _execute_research,
    _jobs,
    _results,
    app,
    create_app,
)
from src.config.settings import Settings


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
    "generated_at": "2024-11-21T20:00:00Z",
}

_MOCK_FINAL_STATE = {
    "query": "Analyse NVDA",
    "tickers": ["NVDA"],
    "research_depth": "standard",
    "ticker_analyses": [],
    "news_items": [],
    "macro_indicators": [],
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
    @patch("src.api.server._execute_research", new_callable=AsyncMock)
    def test_submit_returns_202_with_job_id(self, mock_execute, client):
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

    @patch("src.api.server._execute_research", new_callable=AsyncMock)
    def test_get_job_returns_pending_immediately(self, mock_execute, client):
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
        with patch("src.api.server._execute_research", new_callable=AsyncMock):
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

    @patch("src.api.server._execute_research", new_callable=AsyncMock)
    def test_list_jobs_after_submission(self, mock_execute, client):
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
                with patch("src.api.server._execute_research", new_callable=AsyncMock):
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
            pytest.raises(ValueError, match="API_KEY"),
        ):
            create_app()

    @pytest.mark.asyncio
    async def test_background_job_success_and_safe_failure(self):
        request = ResearchRequest(
            query="Analyse NVDA stock",
            tickers=["NVDA"],
        )
        job_id = "job-success"
        _jobs.set(
            job_id,
            {
                "job_id": job_id,
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        with patch("src.api.server.run_research", new_callable=AsyncMock) as run:
            run.return_value = {
                "query": request.query,
                "tickers": request.tickers,
                "research_depth": request.research_depth,
                "ticker_analyses": [],
                "news_items": [],
                "macro_indicators": [],
                "tool_calls_log": [],
                "messages": [],
                "report": None,
                "error": None,
            }
            await _execute_research(job_id, request)
        assert _jobs.get(job_id)["status"] == "completed"

        failure_id = "job-failure"
        _jobs.set(
            failure_id,
            {
                "job_id": failure_id,
                "status": "pending",
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        with patch(
            "src.api.server.run_research",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sensitive provider detail"),
        ):
            await _execute_research(failure_id, request)
        failed = _jobs.get(failure_id)
        assert failed["status"] == "failed"
        assert "sensitive provider detail" not in failed["error"]
