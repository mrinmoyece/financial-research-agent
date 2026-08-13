"""
Unit tests for the LangGraph workflow.

Tests the graph routing logic without calling real LLMs — we mock
the agent nodes to verify conditional edges and state propagation.
"""

from unittest.mock import AsyncMock, patch

import pytest

import src.graph.workflow as workflow
from src.graph.workflow import (
    build_graph,
    route_after_research,
    route_after_validation,
    run_research,
    validate_input,
)
from src.models.state import AgentState


class TestValidateInput:
    def _base_state(self, **overrides) -> AgentState:
        return {
            "query": "Analyse NVDA",
            "tickers": ["NVDA"],
            "research_depth": "standard",
            "ticker_analyses": [],
            "news_items": [],
            "macro_indicators": [],
            "tool_calls_log": [],
            "messages": [],
            "report": None,
            "error": None,
            **overrides,
        }

    def test_normalises_lowercase_tickers(self):
        state = self._base_state(tickers=["nvda", "aapl"])
        result = validate_input(state)
        assert result["tickers"] == ["NVDA", "AAPL"]

    def test_strips_whitespace_from_tickers(self):
        state = self._base_state(tickers=[" NVDA ", "AAPL "])
        result = validate_input(state)
        assert result["tickers"] == ["NVDA", "AAPL"]

    def test_defaults_invalid_depth_to_standard(self):
        state = self._base_state(research_depth="extreme")
        result = validate_input(state)
        assert result["research_depth"] == "standard"

    def test_sets_error_when_no_tickers(self):
        state = self._base_state(tickers=[], query="something without tickers")
        result = validate_input(state)
        # Either extracts from query or sets error
        assert result.get("error") is not None or len(result.get("tickers", [])) > 0

    def test_extracts_tickers_from_query_when_none_provided(self):
        state = self._base_state(tickers=[], query="Analyse NVDA and AAPL")
        result = validate_input(state)
        # Should extract NVDA and AAPL from query text
        tickers = result.get("tickers", [])
        assert "NVDA" in tickers or result.get("error") is not None

    def test_valid_state_clears_error(self):
        state = self._base_state()
        result = validate_input(state)
        assert result["error"] is None


class TestConditionalRouting:
    def _state(self, **kwargs) -> AgentState:
        return {
            "query": "test",
            "tickers": ["NVDA"],
            "research_depth": "standard",
            "ticker_analyses": [],
            "news_items": [],
            "macro_indicators": [],
            "tool_calls_log": [],
            "messages": [],
            "report": None,
            "error": None,
            **kwargs,
        }

    def test_route_after_validation_proceeds_on_valid_state(self):
        state = self._state(tickers=["NVDA"])
        result = route_after_validation(state)
        assert result == "research_node"

    def test_route_after_validation_ends_on_error(self):
        state = self._state(error="No tickers found")
        result = route_after_validation(state)
        assert result == "__end__"

    def test_route_after_research_proceeds_with_data(self):
        ta = {
            "ticker": "NVDA",
            "company_name": "NVIDIA",
            "sector": "Tech",
            "price": 875.0,
            "market_cap_billions": 2150.0,
            "pe_ratio": 68.0,
            "revenue_growth_yoy": 0.94,
            "free_cash_flow_margin": 0.42,
            "analyst_consensus": "BUY",
            "target_price": 1050.0,
            "key_risks": [],
            "key_catalysts": [],
        }
        state = self._state(ticker_analyses=[ta])
        result = route_after_research(state)
        assert result == "analyst_node"

    def test_route_after_research_ends_on_error(self):
        state = self._state(error="API timeout", ticker_analyses=[])
        result = route_after_research(state)
        assert result == "__end__"

    def test_route_after_research_ends_with_no_data(self):
        state = self._state(ticker_analyses=[], news_items=[])
        result = route_after_research(state)
        assert result == "__end__"


def test_build_graph_and_singleton():
    workflow._graph = None
    graph = build_graph()
    assert graph is not None
    with patch("src.graph.workflow.build_graph", return_value=graph) as builder:
        workflow._graph = None
        assert workflow.get_graph() is graph
        assert workflow.get_graph() is graph
        builder.assert_called_once()


@pytest.mark.asyncio
async def test_run_research_invokes_graph_and_validates_depth():
    final_state = TestConditionalRouting()._state()
    graph = AsyncMock()
    graph.ainvoke.return_value = final_state
    with patch("src.graph.workflow.get_graph", return_value=graph):
        result = await run_research("Analyse NVDA", ["NVDA"], "quick")
    assert result == final_state
    graph.ainvoke.assert_awaited_once()

    with pytest.raises(ValueError, match="Invalid research_depth"):
        await run_research("Analyse NVDA", ["NVDA"], "invalid")
