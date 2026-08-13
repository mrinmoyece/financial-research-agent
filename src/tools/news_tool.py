"""
News Tool — fetches and sentiment-classifies recent financial news.

Uses NewsAPI for headlines and a lightweight LLM call for per-article
sentiment scoring.  Falls back to mock items in dev/CI mode.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from langchain_core.tools import tool
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings
from src.models.state import NewsItem

logger = logging.getLogger(__name__)

_MOCK_NEWS: list[NewsItem] = [
    NewsItem(
        headline="NVIDIA beats Q3 earnings; data centre revenue up 206% YoY",
        source="Reuters",
        published_at="2024-11-21T20:35:00Z",
        sentiment="positive",
        relevance_score=0.97,
        summary=(
            "NVIDIA posted record quarterly revenue driven by Hopper GPU demand from hyperscalers."
        ),
    ),
    NewsItem(
        headline="US export controls tightened on AI chips shipped to China",
        source="FT",
        published_at="2024-11-18T14:12:00Z",
        sentiment="negative",
        relevance_score=0.89,
        summary=(
            "Commerce Department expanded restrictions, potentially impacting "
            "NVIDIA's China business."
        ),
    ),
    NewsItem(
        headline="Blackwell GPU production ramp on track for Q1 2025",
        source="Bloomberg",
        published_at="2024-11-15T09:00:00Z",
        sentiment="positive",
        relevance_score=0.92,
        summary=(
            "Supply chain checks confirm Blackwell shipments beginning Q1, easing demand concerns."
        ),
    ),
]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
def _fetch_newsapi(ticker: str, api_key: str) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    url = "https://newsapi.org/v2/everything"
    params: dict[str, str | int] = {
        "q": ticker,
        "from": since,
        "sortBy": "relevancy",
        "language": "en",
        "pageSize": 10,
        "apiKey": api_key,
    }
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [dict(article) for article in articles]


def _classify_sentiment(headline: str) -> Literal["positive", "neutral", "negative"]:
    """
    Lightweight keyword-based pre-classifier (avoids an extra LLM call per article).
    The analyst node re-evaluates sentiment in full context.
    """
    text = headline.lower()
    positive_signals = ["beat", "record", "surge", "growth", "raised", "upgrade", "rally"]
    negative_signals = ["miss", "fall", "restrict", "cut", "downgrade", "loss", "risk", "ban"]
    pos = sum(1 for w in positive_signals if w in text)
    neg = sum(1 for w in negative_signals if w in text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _estimate_relevance(
    ticker: str, position: int, total: int, headline: str, summary: str
) -> float:
    """
    Simple, cheap relevance heuristic for real (non-mock) NewsAPI articles.

    NewsAPI's `sortBy=relevancy` already orders results by its own relevance
    signal, so we combine:
      1. Rank decay — earlier results score higher (API already sorted by relevance).
      2. Keyword overlap — does the ticker symbol itself appear in the headline
         or summary? Direct mentions are a strong relevance signal for
         single-ticker queries.

    This is not a learned model — just a deterministic, explainable score in
    [0, 1] that is meaningfully better than a flat constant.
    """
    if total <= 1:
        rank_score = 1.0
    else:
        rank_score = 1.0 - (position / (total - 1)) * 0.5  # decays from 1.0 to 0.5

    text = f"{headline} {summary}".lower()
    keyword_score = 0.15 if ticker.lower() in text else 0.0

    return round(min(1.0, rank_score + keyword_score), 2)


@tool
def get_financial_news(ticker: str, max_articles: int = 8) -> list[NewsItem]:
    """
    Fetch recent financial news articles for a ticker, with sentiment labels.

    Args:
        ticker: Stock ticker symbol, e.g. "NVDA"
        max_articles: Maximum number of articles to return (default 8)

    Returns:
        List of NewsItem with headline, source, sentiment, relevance score and summary.
    """
    ticker = ticker.upper().strip()
    logger.info("news_tool: fetching news ticker=%s max=%d", ticker, max_articles)
    settings = get_settings()
    api_key = settings.newsapi_api_key.get_secret_value()

    if not api_key:
        if settings.allow_mock_data and ticker == "NVDA":
            logger.warning("NewsAPI key absent — returning illustrative news for %s", ticker)
            return _MOCK_NEWS[:max_articles]
        return []

    try:
        articles = _fetch_newsapi(ticker, api_key)
        selected = articles[:max_articles]
        results: list[NewsItem] = []
        for position, a in enumerate(selected):
            headline = a.get("title", "")
            summary = a.get("description") or a.get("content") or ""
            results.append(
                NewsItem(
                    headline=headline,
                    source=a.get("source", {}).get("name", "Unknown"),
                    published_at=a.get("publishedAt", ""),
                    sentiment=_classify_sentiment(headline),
                    relevance_score=_estimate_relevance(
                        ticker, position, len(selected), headline, summary
                    ),
                    summary=summary,
                    url=a.get("url", ""),
                )
            )
        return results
    except Exception as exc:
        logger.error("news_tool error ticker=%s error=%s", ticker, exc, exc_info=True)
        if settings.allow_mock_data and ticker == "NVDA":
            return _MOCK_NEWS[:max_articles]
        return []
