"""Deterministic quality gates for model-facing financial report behavior."""

import json
import math
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from src.agents.analyst_agent import analyst_node
from src.config.settings import Settings
from src.models.state import AgentState, ResearchReportPayload
from src.tools.market_data_tool import get_market_data


def _state() -> AgentState:
    return {
        "query": "Analyse NVDA",
        "tickers": ["NVDA"],
        "research_depth": "quick",
        "ticker_analyses": [],
        "news_items": [],
        "macro_indicators": [],
        "tool_calls_log": [],
        "messages": [],
        "report": None,
        "error": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confidence_score", -0.1),
        ("confidence_score", 1.1),
        ("recommended_action", "GUARANTEED_BUY"),
        ("risk_rating", "NONE"),
        ("price_target_12m", math.inf),
        ("price_target_12m", math.nan),
    ],
)
def test_report_schema_rejects_unsafe_model_output(field, value):
    report = {
        "executive_summary": "Summary",
        "investment_thesis": "Thesis",
        "bull_case": "Bull",
        "bear_case": "Bear",
        "risk_rating": "MEDIUM",
        "recommended_action": "HOLD",
        "price_target_12m": None,
        "confidence_score": 0.5,
        "data_sources_used": [],
        "generated_at": "2026-01-01T00:00:00Z",
    }
    report[field] = value
    with pytest.raises(ValidationError):
        ResearchReportPayload.model_validate(report)


def test_analyst_fails_closed_on_out_of_contract_report():
    payload = {
        "executive_summary": "Summary",
        "investment_thesis": "Thesis",
        "bull_case": "Bull",
        "bear_case": "Bear",
        "risk_rating": "MEDIUM",
        "recommended_action": "GUARANTEED_BUY",
        "price_target_12m": None,
        "confidence_score": 2,
        "data_sources_used": [],
    }
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content=json.dumps(payload))
    with patch("src.agents.analyst_agent.build_llm", return_value=llm):
        result = analyst_node(_state())
    assert result["report"] is None
    assert result["error"]


def test_production_market_data_never_substitutes_demo_records():
    settings = Settings(alpha_vantage_api_key="", allow_mock_data=False)
    with patch("src.tools.market_data_tool.get_settings", return_value=settings):
        result = get_market_data.invoke({"ticker": "NVDA"})
    assert result == {"error": "Alpha Vantage is not configured"}
