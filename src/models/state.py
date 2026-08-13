"""
Typed state definitions for the Financial Research Agent.

LangGraph passes the entire AgentState through every node.  We use
TypedDict + Annotated so the graph can merge list fields automatically
(operator.add) rather than overwrite them — this is the correct LangGraph
pattern for accumulating tool outputs across a multi-step workflow.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


class TickerAnalysis(TypedDict):
    ticker: str
    company_name: str
    sector: str
    price: float
    market_cap_billions: float
    pe_ratio: float | None
    revenue_growth_yoy: float | None
    free_cash_flow_margin: float | None
    analyst_consensus: str  # BUY / HOLD / SELL
    target_price: float | None
    key_risks: list[str]
    key_catalysts: list[str]


class NewsItem(TypedDict):
    headline: str
    source: str
    published_at: str
    sentiment: Literal["positive", "neutral", "negative"]
    relevance_score: float  # 0-1
    summary: str


class MacroIndicator(TypedDict):
    name: str  # e.g. "US CPI YoY"
    value: float
    unit: str  # "%", "bps", "USD"
    trend: Literal["rising", "flat", "falling"]
    impact_on_equities: str  # concise one-liner


class ResearchReport(TypedDict):
    executive_summary: str
    investment_thesis: str
    bull_case: str
    bear_case: str
    risk_rating: Literal["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
    recommended_action: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    price_target_12m: float | None
    confidence_score: float  # 0-1
    data_sources_used: list[str]
    generated_at: str  # ISO-8601


class ResearchReportPayload(BaseModel):
    """Runtime validation boundary for model-generated report JSON."""

    model_config = ConfigDict(extra="forbid")

    executive_summary: str = Field(min_length=1, max_length=4000)
    investment_thesis: str = Field(min_length=1, max_length=4000)
    bull_case: str = Field(min_length=1, max_length=4000)
    bear_case: str = Field(min_length=1, max_length=4000)
    risk_rating: Literal["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
    recommended_action: Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]
    price_target_12m: float | None = Field(default=None, allow_inf_nan=False)
    confidence_score: float = Field(ge=0, le=1)
    data_sources_used: list[str] = Field(max_length=20)
    generated_at: str


class AgentState(TypedDict):
    """
    Central state flowing through every LangGraph node.

    Annotated list fields use operator.add so parallel branches can
    independently append without clobbering each other.
    """

    # ── Input ──────────────────────────────────────────────────────
    query: str  # e.g. "Analyse NVDA"
    tickers: list[str]  # ["NVDA"]
    research_depth: Literal["quick", "standard", "deep"]

    # ── Accumulated tool outputs ────────────────────────────────────
    ticker_analyses: Annotated[list[TickerAnalysis], operator.add]
    news_items: Annotated[list[NewsItem], operator.add]
    macro_indicators: Annotated[list[MacroIndicator], operator.add]
    tool_calls_log: Annotated[list[dict[str, Any]], operator.add]  # audit trail

    # ── Intermediate reasoning ──────────────────────────────────────
    messages: Annotated[list[dict[str, Any]], operator.add]  # LLM message history

    # ── Final output ────────────────────────────────────────────────
    report: ResearchReport | None
    error: str | None
