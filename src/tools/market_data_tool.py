"""
Market Data Tool — wraps Alpha Vantage REST API.

Returns fundamental metrics and price data for a given ticker.
Gracefully degrades to mock data when the API key is absent (dev/CI mode).
"""

import logging
from datetime import datetime

import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings
from src.models.state import TickerAnalysis

logger = logging.getLogger(__name__)

_MOCK_DATA: dict[str, TickerAnalysis] = {
    "NVDA": TickerAnalysis(
        ticker="NVDA", company_name="NVIDIA Corporation", sector="Semiconductors",
        price=875.40, market_cap_billions=2150.0, pe_ratio=68.2,
        revenue_growth_yoy=0.94, free_cash_flow_margin=0.42,
        analyst_consensus="BUY", target_price=1050.0,
        key_risks=["AI capex slowdown", "Export controls", "Competition from AMD/Intel"],
        key_catalysts=["Blackwell ramp", "Data centre demand", "Sovereign AI budgets"],
    ),
    "AAPL": TickerAnalysis(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        price=189.30, market_cap_billions=2940.0, pe_ratio=29.4,
        revenue_growth_yoy=0.02, free_cash_flow_margin=0.28,
        analyst_consensus="HOLD", target_price=200.0,
        key_risks=["China revenue exposure", "iPhone cycle maturity", "Regulatory pressure"],
        key_catalysts=["AI features in iOS 18", "Services growth", "India expansion"],
    ),
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_from_alpha_vantage(ticker: str, api_key: str) -> dict:
    """Fetch OVERVIEW endpoint from Alpha Vantage with retry."""
    url = "https://www.alphavantage.co/query"
    params = {"function": "OVERVIEW", "symbol": ticker, "apikey": api_key}
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


@tool
def get_market_data(ticker: str) -> TickerAnalysis:
    """
    Retrieve fundamental market data for a stock ticker.

    Returns price, valuation multiples, revenue growth, FCF margin,
    analyst consensus, and key risks/catalysts.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA", "AAPL"

    Returns:
        TickerAnalysis with all available fundamental data.
    """
    ticker = ticker.upper().strip()
    logger.info("market_data_tool: fetching ticker=%s", ticker)
    settings = get_settings()
    api_key = settings.alpha_vantage_api_key.get_secret_value()

    if not api_key:
        logger.warning("Alpha Vantage key absent — returning mock data for %s", ticker)
        return _MOCK_DATA.get(ticker, _build_placeholder(ticker))

    try:
        raw = _fetch_from_alpha_vantage(ticker, api_key)
        if "Note" in raw:
            logger.warning("Alpha Vantage rate-limit hit — falling back to mock")
            return _MOCK_DATA.get(ticker, _build_placeholder(ticker))

        return TickerAnalysis(
            ticker=ticker,
            company_name=raw.get("Name", ticker),
            sector=raw.get("Sector", "Unknown"),
            price=float(raw.get("50DayMovingAverage", 0)),
            market_cap_billions=round(float(raw.get("MarketCapitalization", 0)) / 1e9, 2),
            pe_ratio=_safe_float(raw.get("PERatio")),
            revenue_growth_yoy=_safe_float(raw.get("QuarterlyRevenueGrowthYOY")),
            free_cash_flow_margin=None,   # not in OVERVIEW; needs separate call
            analyst_consensus="N/A",      # OVERVIEW endpoint has no simple consensus string
            target_price=_safe_float(raw.get("AnalystTargetPrice")),
            key_risks=[],
            key_catalysts=[],
        )
    except Exception as exc:
        logger.error("market_data_tool error ticker=%s error=%s", ticker, exc, exc_info=True)
        return _MOCK_DATA.get(ticker, _build_placeholder(ticker))


def _safe_float(value) -> float | None:
    try:
        f = float(value)
        return None if f == 0 else f
    except (TypeError, ValueError):
        return None


def _build_placeholder(ticker: str) -> TickerAnalysis:
    return TickerAnalysis(
        ticker=ticker, company_name=ticker, sector="Unknown",
        price=0.0, market_cap_billions=0.0, pe_ratio=None,
        revenue_growth_yoy=None, free_cash_flow_margin=None,
        analyst_consensus="N/A", target_price=None,
        key_risks=["Data unavailable"], key_catalysts=[],
    )
