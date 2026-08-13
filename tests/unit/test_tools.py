"""
Unit tests for all data-gathering tools.

All tests run without real API keys — tools fall back to mock data
automatically when keys are absent, which is the CI-safe contract.
"""

import os
from unittest.mock import patch

import pytest

# Ensure no real API keys leak into CI
os.environ.setdefault("ALPHA_VANTAGE_API_KEY", "")
os.environ.setdefault("NEWSAPI_API_KEY", "")
os.environ.setdefault("FINNHUB_API_KEY", "")
os.environ.setdefault("LLM_PROVIDER", "github_models")
os.environ.setdefault("GITHUB_TOKEN", "test-token-ci")


from src.config.settings import Settings
from src.tools.macro_tool import get_macro_indicators
from src.tools.market_data_tool import get_market_data
from src.tools.news_tool import _classify_sentiment, _estimate_relevance, get_financial_news
from src.tools.sec_filing_tool import get_sec_filing_summary


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
        required_fields = [
            "ticker",
            "company_name",
            "sector",
            "price",
            "market_cap_billions",
            "analyst_consensus",
            "key_risks",
            "key_catalysts",
        ]
        for field in required_fields:
            assert field in result, f"Missing field: {field}"

    def test_missing_provider_fails_closed_when_mock_data_disabled(self):
        settings = Settings(
            alpha_vantage_api_key="",
            allow_mock_data=False,
            redis_url="redis://localhost:6379",
        )
        with patch("src.tools.market_data_tool.get_settings", return_value=settings):
            result = get_market_data.invoke({"ticker": "NVDA"})
        assert result == {"error": "Alpha Vantage is not configured"}

    def test_maps_live_provider_response(self):
        settings = Settings(alpha_vantage_api_key="secret", allow_mock_data=False)
        raw = {
            "Name": "Example Corp",
            "Sector": "Technology",
            "50DayMovingAverage": "25",
            "MarketCapitalization": "2000000000",
            "PERatio": "12.5",
            "QuarterlyRevenueGrowthYOY": "0.1",
            "AnalystTargetPrice": "30",
        }
        with (
            patch("src.tools.market_data_tool.get_settings", return_value=settings),
            patch("src.tools.market_data_tool._fetch_from_alpha_vantage", return_value=raw),
        ):
            result = get_market_data.invoke({"ticker": "EXM"})
        assert result["company_name"] == "Example Corp"
        assert result["market_cap_billions"] == 2.0
        assert result["pe_ratio"] == 12.5


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

    @pytest.mark.parametrize(
        "headline,expected",
        [
            ("NVIDIA beats earnings expectations", "positive"),
            ("Stock falls after regulatory ban", "negative"),
            ("Company reports quarterly results", "neutral"),
            ("Revenue surges; profit margin record high", "positive"),
            ("Layoffs announced amid revenue miss", "negative"),
        ],
    )
    def test_classify_sentiment(self, headline, expected):
        result = _classify_sentiment(headline)
        assert result == expected

    def test_maps_live_news_response_and_relevance(self):
        settings = Settings(newsapi_api_key="secret", allow_mock_data=False)
        articles = [
            {
                "title": "EXM reports record growth",
                "description": "EXM revenue increased",
                "source": {"name": "Wire"},
                "publishedAt": "2026-01-01T00:00:00Z",
            },
            {
                "title": "Sector update",
                "description": None,
                "content": "General market coverage",
                "source": {},
                "publishedAt": "2026-01-02T00:00:00Z",
            },
        ]
        with (
            patch("src.tools.news_tool.get_settings", return_value=settings),
            patch("src.tools.news_tool._fetch_newsapi", return_value=articles),
        ):
            result = get_financial_news.invoke({"ticker": "EXM"})
        assert len(result) == 2
        assert result[0]["source"] == "Wire"
        assert result[0]["sentiment"] == "positive"
        assert _estimate_relevance("EXM", 0, 1, "EXM news", "") == 1.0


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

    def test_maps_live_macro_responses_without_demo_supplement(self):
        settings = Settings(alpha_vantage_api_key="secret", allow_mock_data=False)
        with (
            patch("src.tools.macro_tool.get_settings", return_value=settings),
            patch("src.tools.macro_tool._fetch_indicator", side_effect=[2.5, 4.0, 1.5]),
        ):
            result = get_macro_indicators.invoke({})
        assert [item["name"] for item in result] == [
            "US CPI YoY",
            "Fed Funds Rate",
            "US Real GDP Growth",
        ]
        assert result[0]["trend"] == "falling"
        assert result[2]["trend"] == "falling"


class TestSecFilingTool:
    def test_returns_live_filing_metadata(self):
        with (
            patch("src.tools.sec_filing_tool._edgar_cik_lookup", return_value="0001234567"),
            patch(
                "src.tools.sec_filing_tool._fetch_recent_filing_meta",
                return_value={"form_type": "10-Q", "filed_at": "2026-08-01"},
            ),
        ):
            result = get_sec_filing_summary.invoke({"ticker": "EXM"})
        assert result["form_type"] == "10-Q"
        assert "0001234567" in result["mda_excerpt"]

    def test_reports_missing_company(self):
        with patch("src.tools.sec_filing_tool._edgar_cik_lookup", return_value=None):
            result = get_sec_filing_summary.invoke({"ticker": "UNKNOWN"})
        assert result == {"error": "CIK not found for ticker UNKNOWN"}

    def test_provider_failure_returns_stable_safe_error(self):
        with (
            patch(
                "src.tools.sec_filing_tool._edgar_cik_lookup",
                side_effect=RuntimeError("sensitive upstream detail"),
            ),
            patch(
                "src.tools.sec_filing_tool.get_settings",
                return_value=Settings(allow_mock_data=False),
            ),
        ):
            result = get_sec_filing_summary.invoke({"ticker": "EXM"})
        assert result == {"error": "SEC filing provider request failed"}
