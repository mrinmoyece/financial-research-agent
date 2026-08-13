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
from datetime import UTC, datetime
from typing import Any, cast

from src.config.llm import build_llm_candidates
from src.config.settings import get_settings
from src.models.state import AgentState, ResearchReport, ResearchReportPayload

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
  "data_sources_used": ["market_data", "news", "macro", "sec_filings"],
  "citations": [{"source_id": "src_...", "claim": "specific claim supported by this source"}]
}

Every material claim must have at least one citation. Use only source IDs from
the supplied Source Registry. Never cite a source that was not supplied.
"""


def _build_analyst_prompt(state: AgentState) -> str:
    """Formats all gathered state data into a structured prompt."""
    sections = [
        f"# Research Brief: {', '.join(state['tickers'])}",
        f"**Query**: {state['query']}",
        f"**Research depth**: {state['research_depth']}",
        "",
    ]

    # Ticker fundamentals
    if state.get("ticker_analyses"):
        sections.append("## Fundamental Data")
        for ta in state["ticker_analyses"]:
            sections.append(
                f"### {ta['ticker']} — {ta['company_name']} ({ta['sector']})\n"
                f"- Price: ${ta['price']:.2f}  |  Market Cap: ${ta['market_cap_billions']:.1f}B\n"
                f"- P/E: {ta.get('pe_ratio') or 'N/A'}  |  "
                f"Revenue Growth YoY: {(ta.get('revenue_growth_yoy') or 0) * 100:.1f}%  |  "
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
            label = f"{m['name']}: {m['value']}{m['unit']} ({m['trend']})"
            sections.append(f"- **{label}** — {m['impact_on_equities']}")

    sections.append("\n## Source Registry")
    for source in state.get("sources", []):
        sections.append(
            f"- {source['source_id']}: {source.get('label', source['source_type'])} via "
            f"{source['provider']} ({source['locator']}); "
            f"content_sha256={source.get('content_sha256', 'unavailable')}"
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
    llms = build_llm_candidates(settings)
    model_calls = state.get("model_calls", 0)
    input_tokens = state.get("input_tokens", 0)
    output_tokens = state.get("output_tokens", 0)

    prompt = _build_analyst_prompt(state)

    messages = [
        {"role": "system", "content": _ANALYST_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        response = None
        last_error: Exception | None = None
        for index, llm in enumerate(llms):
            if model_calls >= settings.max_model_calls_per_job:
                break
            model_calls += 1
            try:
                response = llm.invoke(messages)
                if index:
                    logger.warning("analyst_node: LLM fallback provider index=%d used", index)
                break
            except Exception as exc:
                last_error = exc
                logger.error("analyst_node: LLM provider index=%d failed", index)
        if response is None:
            if model_calls >= settings.max_model_calls_per_job:
                raise RuntimeError("Per-job model-call budget exhausted") from last_error
            raise RuntimeError("All configured LLM providers failed") from last_error
        if response.usage_metadata is not None:
            input_tokens += response.usage_metadata.get("input_tokens", 0)
            output_tokens += response.usage_metadata.get("output_tokens", 0)
        if not isinstance(response.content, str):
            raise ValueError("Analyst response must be plain JSON text")
        raw_json = response.content.strip()

        # Strip markdown fences if the model returned them despite instructions
        if raw_json.startswith("```"):
            raw_json = "\n".join(
                line for line in raw_json.splitlines() if not line.strip().startswith("```")
            ).strip()

        report_data = json.loads(raw_json)
        validated = ResearchReportPayload.model_validate(
            {
                **report_data,
                "generated_at": datetime.now(UTC).isoformat(),
            }
        )
        allowed_source_ids = {source["source_id"] for source in state.get("sources", [])}
        cited_source_ids = {citation.source_id for citation in validated.citations}
        if not cited_source_ids.issubset(allowed_source_ids):
            raise ValueError("Report cited a source outside the supplied registry")
        report = cast(ResearchReport, validated.model_dump())
        logger.info(
            "analyst_node: report generated action=%s risk=%s confidence=%.2f",
            report["recommended_action"],
            report["risk_rating"],
            report["confidence_score"],
        )
        return {
            "report": report,
            "error": None,
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    except json.JSONDecodeError as exc:
        logger.error("analyst_node: failed to parse LLM JSON output: %s", exc)
        return {
            "report": None,
            "error": f"JSON parse error: {exc}",
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    except Exception as exc:
        logger.error("analyst_node: unexpected error: %s", exc, exc_info=True)
        return {
            "report": None,
            "error": str(exc),
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
