"""
Analyst Agent — synthesises raw data into an investment research report.

This LangGraph node receives the fully populated AgentState from the
research node and produces a structured ResearchReport.  It does NOT
call external tools — it reasons purely from the data already gathered.

Design principle: separate data gathering (research_node) from
reasoning (analyst_node) so each can be independently tested,
retried, or scaled.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from src.config.llm import build_llm
from src.config.settings import get_settings
from src.models.state import AgentState, ResearchReport

logger = logging.getLogger(__name__)

_ANALYST_SYSTEM_PROMPT = """\
You are a senior equity research analyst at a top-tier investment bank.
You have been given raw data gathered by a research assistant.

Your task is to synthesise this data into a structured investment research report.

Guidelines:
- Be specific — cite actual numbers from the data, not generalities.
- Risk rating must reflect earnings quality, balance sheet strength, sector volatility.
- Recommended action must be defensible from the data provided.
- Bull and bear cases should be distinct and grounded in specific data points.
- Confidence score reflects data completeness: 0.9+ = full dataset, 0.6 = partial, <0.5 = thin.
- Do NOT fabricate data. If something is unknown, say so.

Output ONLY valid JSON matching this schema (no markdown fences):
{
  "executive_summary": "...",
  "investment_thesis": "...",
  "bull_case": "...",
  "bear_case": "...",
  "risk_rating": "LOW|MEDIUM|HIGH|VERY_HIGH",
  "recommended_action": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL",
  "price_target_12m": <float or null>,
  "confidence_score": <0.0–1.0>,
  "data_sources_used": ["market_data", "news", "macro", "sec_filings"]
}
"""


def _build_analyst_prompt(state: AgentState) -> str:
    """Formats all gathered state data into a structured prompt."""
    sections = [f"# Research Brief: {', '.join(state['tickers'])}",
                f"**Query**: {state['query']}",
                f"**Research depth**: {state['research_depth']}",
                ""]

    # Ticker fundamentals
    if state.get("ticker_analyses"):
        sections.append("## Fundamental Data")
        for ta in state["ticker_analyses"]:
            sections.append(
                f"### {ta['ticker']} — {ta['company_name']} ({ta['sector']})\n"
                f"- Price: ${ta['price']:.2f}  |  Market Cap: ${ta['market_cap_billions']:.1f}B\n"
                f"- P/E: {ta.get('pe_ratio') or 'N/A'}  |  "
                f"Revenue Growth YoY: {ta.get('revenue_growth_yoy', 0) * 100:.1f}%  |  "
                f"FCF Margin: {(ta.get('free_cash_flow_margin') or 0) * 100:.1f}%\n"
                f"- Analyst Consensus: {ta['analyst_consensus']}  |  "
                f"Target Price: ${ta.get('target_price') or 'N/A'}\n"
                f"- Key Risks: {', '.join(ta.get('key_risks', []) or ['None identified'])}\n"
                f"- Key Catalysts: {', '.join(ta.get('key_catalysts', []) or ['None identified'])}"
            )

    # News sentiment
    if state.get("news_items"):
        pos = sum(1 for n in state["news_items"] if n["sentiment"] == "positive")
        neg = sum(1 for n in state["news_items"] if n["sentiment"] == "negative")
        neu = len(state["news_items"]) - pos - neg
        sections.append(
            f"\n## Recent News ({len(state['news_items'])} articles: "
            f"{pos} positive / {neu} neutral / {neg} negative)"
        )
        # Top 5 most relevant
        top = sorted(state["news_items"], key=lambda x: x["relevance_score"], reverse=True)[:5]
        for item in top:
            sections.append(
                f"- [{item['sentiment'].upper()}] {item['headline']} "
                f"({item['source']}, {item['published_at'][:10]})\n"
                f"  {item['summary'][:200]}"
            )

    # Macro context
    if state.get("macro_indicators"):
        sections.append("\n## Macroeconomic Context")
        for m in state["macro_indicators"]:
            sections.append(
                f"- **{m['name']}**: {m['value']}{m['unit']} ({m['trend']}) — {m['impact_on_equities']}"
            )

    sections.append("\nBased on all data above, produce the investment research report JSON.")
    return "\n".join(sections)


def analyst_node(state: AgentState) -> dict[str, Any]:
    """
    LangGraph node: synthesises research data into a ResearchReport.

    Returns partial state update with the 'report' field populated.
    """
    logger.info("analyst_node: synthesising report for tickers=%s", state["tickers"])

    settings = get_settings()
    llm = build_llm(settings)

    prompt = _build_analyst_prompt(state)

    messages = [
        {"role": "system", "content": _ANALYST_SYSTEM_PROMPT},
        {"role": "user",   "content": prompt},
    ]

    try:
        response = llm.invoke(messages)
        raw_json = response.content.strip()

        # Strip markdown fences if the model returned them despite instructions
        if raw_json.startswith("```"):
            raw_json = "\n".join(
                line for line in raw_json.splitlines()
                if not line.strip().startswith("```")
            ).strip()

        report_data = json.loads(raw_json)
        report = ResearchReport(
            executive_summary=report_data["executive_summary"],
            investment_thesis=report_data["investment_thesis"],
            bull_case=report_data["bull_case"],
            bear_case=report_data["bear_case"],
            risk_rating=report_data["risk_rating"],
            recommended_action=report_data["recommended_action"],
            price_target_12m=report_data.get("price_target_12m"),
            confidence_score=float(report_data.get("confidence_score", 0.7)),
            data_sources_used=report_data.get("data_sources_used", []),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "analyst_node: report generated action=%s risk=%s confidence=%.2f",
            report["recommended_action"], report["risk_rating"], report["confidence_score"],
        )
        return {"report": report, "error": None}

    except json.JSONDecodeError as exc:
        logger.error("analyst_node: failed to parse LLM JSON output: %s", exc)
        return {"report": None, "error": f"JSON parse error: {exc}"}
    except Exception as exc:
        logger.error("analyst_node: unexpected error: %s", exc, exc_info=True)
        return {"report": None, "error": str(exc)}
