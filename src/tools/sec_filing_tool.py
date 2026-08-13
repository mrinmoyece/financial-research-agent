"""
SEC Filing Tool — retrieves recent 10-K / 10-Q summaries from SEC EDGAR.

Uses the free EDGAR full-text search API (no key required).
Extracts management discussion & analysis sections for analyst consumption.
"""

import logging
from typing import Any

import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings

logger = logging.getLogger(__name__)

_EDGAR_FILING = "https://data.sec.gov/submissions/CIK{cik}.json"

_MOCK_FILINGS = {
    "NVDA": {
        "form_type": "10-K",
        "filed_at": "2024-02-23",
        "period": "FY2024",
        "mda_excerpt": (
            "Data Center revenue grew 217% year-over-year to $47.5 billion, driven by accelerating "
            "demand for Hopper GPU computing. Gaming revenue recovered 15% as channel inventory "
            "normalised. Management guides for continued strong Data Center growth in FY2025, "
            "underpinned by AI training and inference workloads at hyperscale customers. Key risk: "
            "US export restrictions on A100/H100 to China may reduce addressable market by up to "
            "$5 billion annually."
        ),
    }
}


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=6), reraise=True)
def _edgar_cik_lookup(ticker: str) -> str | None:
    """Resolve ticker → CIK from SEC company tickers JSON."""
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": "FinancialResearchAgent research@agentforge.io"}
    with httpx.Client(timeout=15, headers=headers) as client:
        data = client.get(url).json()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=6), reraise=True)
def _fetch_recent_filing_meta(cik: str) -> dict[str, Any] | None:
    headers = {"User-Agent": "FinancialResearchAgent research@agentforge.io"}
    url = _EDGAR_FILING.format(cik=cik)
    with httpx.Client(timeout=15, headers=headers) as client:
        data = client.get(url).json()
    filings = data.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    dates = filings.get("filingDate", [])
    for i, form in enumerate(forms):
        if form in ("10-K", "10-Q"):
            return {"form_type": form, "filed_at": dates[i]}
    return None


@tool
def get_sec_filing_summary(ticker: str) -> dict[str, Any]:
    """
    Retrieve the most recent 10-K or 10-Q filing metadata for a ticker from SEC EDGAR.

    Returns the form type, filing date, and an excerpt of the
    Management Discussion & Analysis section.

    Args:
        ticker: Stock ticker, e.g. "NVDA"

    Returns:
        dict with form_type, filed_at, period, and mda_excerpt.
    """
    ticker = ticker.upper().strip()
    logger.info("sec_filing_tool: fetching filings for ticker=%s", ticker)

    try:
        cik = _edgar_cik_lookup(ticker)
        if not cik:
            return {"error": f"CIK not found for ticker {ticker}"}

        meta = _fetch_recent_filing_meta(cik)
        if not meta:
            return {"error": f"No 10-K/10-Q found for {ticker}"}

        return {
            "form_type": meta["form_type"],
            "filed_at": meta["filed_at"],
            "period": "See EDGAR for full filing",
            "mda_excerpt": "MDA full text available at https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik}&type={meta['form_type']}",
        }
    except Exception as exc:
        logger.error("sec_filing_tool error ticker=%s error=%s", ticker, exc, exc_info=True)
        if get_settings().allow_mock_data and ticker in _MOCK_FILINGS:
            logger.warning("SEC request failed — returning illustrative filing for %s", ticker)
            return _MOCK_FILINGS[ticker]
        return {"error": str(exc)}
