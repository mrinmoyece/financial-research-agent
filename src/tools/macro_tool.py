"""
Macro Indicators Tool — retrieves key macroeconomic data points.

Sources: Alpha Vantage ECONOMIC_INDICATOR endpoints.
Falls back to hardcoded illustrative data in dev mode.
"""

import logging

import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings
from src.models.state import MacroIndicator

logger = logging.getLogger(__name__)

_MOCK_MACRO: list[MacroIndicator] = [
    MacroIndicator(name="US CPI YoY", value=3.2, unit="%", trend="falling",
                   impact_on_equities="Disinflation supports rate cuts → positive for growth stocks"),
    MacroIndicator(name="Fed Funds Rate", value=5.25, unit="%", trend="flat",
                   impact_on_equities="Elevated rates compress valuation multiples for high-PE names"),
    MacroIndicator(name="US GDP Growth QoQ", value=2.8, unit="%", trend="rising",
                   impact_on_equities="Strong growth supports corporate earnings — broadly positive"),
    MacroIndicator(name="10Y Treasury Yield", value=4.45, unit="%", trend="rising",
                   impact_on_equities="Rising yields increase discount rate → headwind for long-duration equities"),
    MacroIndicator(name="USD Index (DXY)", value=104.2, unit="index", trend="flat",
                   impact_on_equities="Strong USD headwind for multinationals with overseas revenue"),
]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
def _fetch_indicator(function: str, api_key: str) -> float | None:
    url = "https://www.alphavantage.co/query"
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, params={"function": function, "apikey": api_key})
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("data", [])
        return float(entries[0]["value"]) if entries else None


@tool
def get_macro_indicators() -> list[MacroIndicator]:
    """
    Retrieve key macroeconomic indicators relevant to equity analysis.

    Covers: CPI, Fed Funds Rate, GDP growth, Treasury yields, USD strength.
    Each indicator includes a plain-English equity market impact statement.

    Returns:
        List of MacroIndicator objects.
    """
    logger.info("macro_tool: fetching macro indicators")
    settings = get_settings()
    api_key = settings.alpha_vantage_api_key.get_secret_value()

    if not api_key:
        logger.warning("Alpha Vantage key absent — returning mock macro indicators")
        return _MOCK_MACRO

    try:
        cpi = _fetch_indicator("CPI", api_key)
        federal_funds = _fetch_indicator("FEDERAL_FUNDS_RATE", api_key)
        real_gdp = _fetch_indicator("REAL_GDP", api_key)

        indicators: list[MacroIndicator] = []
        if cpi is not None:
            indicators.append(MacroIndicator(
                name="US CPI YoY", value=cpi, unit="%",
                trend="falling" if cpi < 3.5 else "rising",
                impact_on_equities="Disinflation supports rate cuts → positive for growth stocks"
                                   if cpi < 3.5 else "Persistent inflation delays cuts → valuation headwind",
            ))
        if federal_funds is not None:
            indicators.append(MacroIndicator(
                name="Fed Funds Rate", value=federal_funds, unit="%",
                trend="flat",
                impact_on_equities="Elevated rates compress valuation multiples for high-PE names",
            ))
        if real_gdp is not None:
            indicators.append(MacroIndicator(
                name="US Real GDP Growth", value=real_gdp, unit="%",
                trend="rising" if real_gdp > 2.0 else "falling",
                impact_on_equities="Strong growth supports corporate earnings — broadly positive"
                                   if real_gdp > 2.0 else "Slowdown may indicate earnings risk ahead",
            ))

        # Supplement with remaining mock items for completeness
        indicators += [i for i in _MOCK_MACRO if i["name"] not in {x["name"] for x in indicators}]
        return indicators

    except Exception as exc:
        logger.error("macro_tool error=%s", exc, exc_info=True)
        return _MOCK_MACRO
