"""Focused tests for research and analyst agent behavior."""

import json
import time
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from src.agents.analyst_agent import _build_analyst_prompt, analyst_node
from src.agents.research_agent import _invoke_tool_with_timeout, research_node
from src.models.state import AgentState


def _state(**overrides) -> AgentState:
    state: AgentState = {
        "query": "Analyse NVDA",
        "tickers": ["NVDA"],
        "research_depth": "standard",
        "ticker_analyses": [],
        "news_items": [],
        "macro_indicators": [],
        "sources": [],
        "tool_calls_log": [],
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "messages": [],
        "report": None,
        "error": None,
    }
    state.update(overrides)
    return state


def _ticker_analysis():
    return {
        "ticker": "NVDA",
        "company_name": "NVIDIA",
        "sector": "Technology",
        "price": 100.0,
        "market_cap_billions": 1000.0,
        "pe_ratio": 30.0,
        "revenue_growth_yoy": 0.2,
        "free_cash_flow_margin": 0.3,
        "analyst_consensus": "BUY",
        "target_price": 120.0,
        "key_risks": ["valuation"],
        "key_catalysts": ["growth"],
    }


class _SequencedLlm:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, _tools):
        return self

    def invoke(self, _messages):
        return next(self.responses)


def test_research_node_classifies_tool_results():
    first = AIMessage(
        content="",
        tool_calls=[
            {"name": "get_market_data", "args": {"ticker": "NVDA"}, "id": "1"},
            {"name": "get_financial_news", "args": {"ticker": "NVDA"}, "id": "2"},
            {"name": "get_macro_indicators", "args": {}, "id": "3"},
            {"name": "unknown", "args": {}, "id": "4"},
        ],
    )
    llm = _SequencedLlm([first, AIMessage(content="done")])
    news = {
        "headline": "News",
        "source": "Source",
        "published_at": "2026-01-01T00:00:00Z",
        "sentiment": "neutral",
        "relevance_score": 0.5,
        "summary": "Summary",
    }
    macro = {
        "name": "CPI",
        "value": 2.0,
        "unit": "%",
        "trend": "falling",
        "impact_on_equities": "Supportive",
    }

    def invoke(tool, _args):
        return {
            "get_market_data": _ticker_analysis(),
            "get_financial_news": [news],
            "get_macro_indicators": [macro],
        }[tool.name]

    with (
        patch("src.agents.research_agent.build_llm_candidates", return_value=[llm]),
        patch("src.agents.research_agent._invoke_tool_with_timeout", side_effect=invoke),
    ):
        result = research_node(_state())

    assert result["ticker_analyses"] == [_ticker_analysis()]
    assert result["news_items"] == [news]
    assert result["macro_indicators"] == [macro]
    assert len(result["tool_calls_log"]) == 4
    assert result["error"] is None


def test_research_node_reports_no_data_and_handles_tool_failure():
    response = AIMessage(
        content="",
        tool_calls=[{"name": "get_market_data", "args": {"ticker": "NVDA"}, "id": "1"}],
    )
    llm = _SequencedLlm([response, AIMessage(content="done")])
    with (
        patch("src.agents.research_agent.build_llm_candidates", return_value=[llm]),
        patch(
            "src.agents.research_agent._invoke_tool_with_timeout",
            side_effect=RuntimeError("provider down"),
        ),
    ):
        result = research_node(_state(research_depth="quick"))
    assert result["ticker_analyses"] == []
    assert result["error"] == "No research data was available from the configured providers."


def test_tool_timeout_is_enforced():
    tool = MagicMock()
    tool.invoke.side_effect = lambda _args: time.sleep(0.02)
    try:
        _invoke_tool_with_timeout(tool, {}, timeout=0.001)
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected timeout")


def test_analyst_prompt_and_valid_report():
    state = _state(
        ticker_analyses=[_ticker_analysis()],
        news_items=[
            {
                "headline": "News",
                "source": "Source",
                "published_at": "2026-01-01T00:00:00Z",
                "sentiment": "positive",
                "relevance_score": 0.9,
                "summary": "Growth continues",
            }
        ],
        macro_indicators=[
            {
                "name": "CPI",
                "value": 2.0,
                "unit": "%",
                "trend": "falling",
                "impact_on_equities": "Supportive",
            }
        ],
        sources=[
            {
                "source_id": "src_aaaaaaaaaaaa",
                "provider": "get_market_data",
                "source_type": "market_data",
                "locator": "https://example.com",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "ticker": "NVDA",
            }
        ],
    )
    prompt = _build_analyst_prompt(state)
    assert "Fundamental Data" in prompt
    assert "Recent News" in prompt
    assert "Macroeconomic Context" in prompt

    payload = {
        "executive_summary": "Summary",
        "investment_thesis": "Thesis",
        "bull_case": "Bull",
        "bear_case": "Bear",
        "risk_rating": "MEDIUM",
        "recommended_action": "HOLD",
        "price_target_12m": 120,
        "confidence_score": 0.8,
        "data_sources_used": ["market_data"],
        "citations": [{"source_id": "src_aaaaaaaaaaaa", "claim": "Summary"}],
    }
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content=f"```json\n{json.dumps(payload)}\n```")
    with patch("src.agents.analyst_agent.build_llm_candidates", return_value=[llm]):
        result = analyst_node(state)
    assert result["report"]["recommended_action"] == "HOLD"
    assert result["error"] is None


def test_analyst_node_rejects_invalid_responses():
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content="not json")
    with patch("src.agents.analyst_agent.build_llm_candidates", return_value=[llm]):
        result = analyst_node(_state())
    assert result["report"] is None
    assert "JSON parse error" in result["error"]

    llm.invoke.return_value = AIMessage(content=[{"type": "text", "text": "not plain"}])
    with patch("src.agents.analyst_agent.build_llm_candidates", return_value=[llm]):
        result = analyst_node(_state())
    assert result["error"] == "Analyst response must be plain JSON text"
