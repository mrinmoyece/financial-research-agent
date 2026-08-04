"""
Unit tests for all data-gathering tools.

All tests run without real API keys — tools fall back to mock data
automatically when keys are absent, which is the CI-safe contract.
"""

import os
import pytest


# Ensure no real API keys leak into CI
os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "")
os.environ.setdefault("NEWSAPI_API_KEY", "")
os.environ.setdefault("FINNHUB_API_KEY", "")
os.environ.setdefault("LLM_PROVIDER", "github_models")
os.environ.setdefault("GITHUB_TOKEN", "test-token-ci")


from src.tools.market_data_tool import get_market_data
from src.tools.news_tool import get_financial_news, _classify_sentiment
from src.tools.macro_tool import get_macro_indicators


class TestMarketDataTool:

    def test_returns_ticker_analysis_for_known_ticker(self):
        result = get_market_data.invoke({"ticker": "NVDA"})
        assert result["ticker"] == "NVDA"
        assert result["company_name"] == "NVIDIA Corporation"
        assert result["price"] > 0
        assert isinstance(result["key_risks"], list)
        assert isinstance(result["key_catalysts"], list)

    def test_normalises_lowercase_ticker(self):
        result = get_market_data.invoke({"ticker": "nvda"})
        assert result["ticker"] == "NVDA"

    def test_returns_placeholder_for_unknown_ticker(self):
        result = get_market_data.invoke({"ticker": "ZZZZZ"})
        assert result["ticker"] == "ZZZZZ"
        # Should not raise — returns a placeholder
        assert "error" not in str(result).lower() or result["market_cap_billions"] == 0.0

    def test_returns_all_required_fields(self):
        result = get_market_data.invoke({"ticker": "AAPL"})
        required_fields = ["ticker", "company_name", "sector", "price",
                           "market_cap_billions", "analyst_consensus",
                           "key_risks", "key_catalysts"]
        for field in required_fields:
            assert field in result, f"Missing field: {field}"


class TestNewsTool:

    def test_returns_list_of_news_items(self):
        result = get_financial_news.invoke({"ticker": "NVDA"})
        assert isinstance(result, list)
        assert len(result) > 0

    def test_news_items_have_required_fields(self):
        result = get_financial_news.invoke({"ticker": "NVDA"})
        for item in result:
            assert "headline" in item
            assert "sentiment" in item
            assert item["sentiment"] in ("positive", "neutral", "negative")
            assert 0.0 <= item["relevance_score"] <= 1.0

    def test_max_articles_respected(self):
        result = get_financial_news.invoke({"ticker": "NVDA", "max_articles": 2})
        assert len(result) <= 2

    @pytest.mark.parametrize("headline,expected", [
        ("NVIDIA beats earnings expectations", "positive"),
        ("Stock falls after regulatory ban", "negative"),
        ("Company reports quarterly results", "neutral"),
        ("Revenue surges; profit margin record high", "positive"),
        ("Layoffs announced amid revenue miss", "negative"),
    ])
    def test_classify_sentiment(self, headline, expected):
        result = _classify_sentiment(headline)
        assert result == expected


class TestMacroTool:

    def test_returns_list_of_indicators(self):
        result = get_macro_indicators.invoke({})
        assert isinstance(result, list)
        assert len(result) >= 3

    def test_indicators_have_required_fields(self):
        result = get_macro_indicators.invoke({})
        for indicator in result:
            assert "name" in indicator
            assert "value" in indicator
            assert "unit" in indicator
            assert "trend" in indicator
            assert indicator["trend"] in ("rising", "flat", "falling")
            assert "impact_on_equities" in indicator
