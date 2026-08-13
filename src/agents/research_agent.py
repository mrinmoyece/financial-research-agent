"""
Research Agent — LangGraph ReAct node that gathers raw data.

This node is responsible for:
  - Determining which tools to call based on the query and tickers
  - Running tool calls sequentially (respecting API rate limits)
  - Populating AgentState with ticker_analyses, news_items, macro_indicators
  - Appending all tool calls to the audit log

It uses LangChain's bind_tools() to enable structured tool calling
without writing bespoke JSON schemas.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage

from src.config.llm import build_llm_candidates
from src.config.settings import get_settings
from src.models.state import AgentState, MacroIndicator, NewsItem, SourceRecord, TickerAnalysis
from src.resilience import CircuitBreaker, CircuitOpenError
from src.security.content import UnsafeContentError, sanitize_tool_result
from src.tools.macro_tool import get_macro_indicators
from src.tools.market_data_tool import get_market_data
from src.tools.news_tool import get_financial_news
from src.tools.sec_filing_tool import get_sec_filing_summary

logger = logging.getLogger(__name__)

# Hard ceiling on a single tool call, independent of the tool's own internal
# (e.g. httpx) timeout. Protects the ReAct loop from stalling forever if a
# tool call hangs in a way its own timeout doesn't cover (e.g. DNS hang,
# thread/lock contention, a buggy retry loop).
TOOL_CALL_TIMEOUT_SECONDS = 30

# Single shared executor for tool-call timeout enforcement. Tool calls are
# synchronous (httpx.Client, not AsyncClient), so we bound their wall-clock
# time by running them in a worker thread and enforcing a timeout on the
# future rather than on the call itself.
_TOOL_CALL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-call")


def _invoke_tool_with_timeout(
    tool_fn: Any, tool_args: dict[str, Any], timeout: float = TOOL_CALL_TIMEOUT_SECONDS
) -> Any:
    """
    Run a (synchronous) LangChain tool's .invoke() with a hard wall-clock
    timeout, so a single hung tool call cannot stall the entire ReAct loop.

    Note: if the underlying call is a blocking I/O call without its own
    cancellation support, the worker thread itself may continue running in
    the background after the timeout fires — this still unblocks the caller,
    which is the primary goal here.
    """
    future = _TOOL_CALL_EXECUTOR.submit(tool_fn.invoke, tool_args)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        raise TimeoutError(f"Tool call timed out after {timeout}s") from exc


# All tools the research agent can call
RESEARCH_TOOLS = [
    get_market_data,
    get_financial_news,
    get_macro_indicators,
    get_sec_filing_summary,
]

TOOL_MAP = {t.name: t for t in RESEARCH_TOOLS}
_CIRCUITS: dict[str, CircuitBreaker] = {}

_SOURCE_LOCATORS = {
    "get_market_data": "https://www.alphavantage.co/",
    "get_financial_news": "https://newsapi.org/",
    "get_macro_indicators": "https://www.alphavantage.co/",
    "get_sec_filing_summary": "https://www.sec.gov/edgar/search/",
}
_SOURCE_TYPES: dict[str, Literal["market_data", "news", "macro", "sec_filing"]] = {
    "get_market_data": "market_data",
    "get_financial_news": "news",
    "get_macro_indicators": "macro",
    "get_sec_filing_summary": "sec_filing",
}


def _source_records(
    tool_name: str,
    tool_args: dict[str, Any],
    result: Any,
) -> list[SourceRecord]:
    records = result if isinstance(result, list) else [result]
    sources: list[SourceRecord] = []
    for record in records:
        serialized = json.dumps(record, sort_keys=True, default=str)
        digest = sha256(serialized.encode()).hexdigest()
        locator = _SOURCE_LOCATORS[tool_name]
        label = tool_name
        if isinstance(record, dict):
            locator = str(record.get("url") or record.get("filing_url") or locator)
            label = str(
                record.get("headline")
                or record.get("name")
                or record.get("company_name")
                or record.get("form_type")
                or tool_name
            )[:200]
        ticker = tool_args.get("ticker")
        source_seed = json.dumps(
            {"tool": tool_name, "args": tool_args, "content_sha256": digest},
            sort_keys=True,
        )
        sources.append(
            SourceRecord(
                source_id=f"src_{sha256(source_seed.encode()).hexdigest()[:12]}",
                provider=tool_name,
                source_type=_SOURCE_TYPES[tool_name],
                locator=locator,
                retrieved_at=datetime.now(UTC).isoformat(),
                ticker=str(ticker) if ticker else None,
                label=label,
                content_sha256=digest,
            )
        )
    return sources


_SYSTEM_PROMPT = """\
You are a senior financial research analyst with 20+ years of experience covering global equities.
Your job is to gather comprehensive data about the requested tickers and macroeconomic environment.

You have access to these tools:
- get_market_data: fundamental data (P/E, revenue growth, FCF, analyst consensus)
- get_financial_news: recent news with sentiment labels
- get_macro_indicators: macro-economic indicators (CPI, rates, GDP)
- get_sec_filing_summary: latest 10-K/10-Q from SEC EDGAR

Rules:
1. Always call get_market_data AND get_financial_news for each ticker.
2. Always call get_macro_indicators once — macro context applies to all tickers.
3. For deep research, also call get_sec_filing_summary.
4. Do not fabricate numbers. If data is unavailable, state that clearly.
5. Use tools in parallel where possible (but respect rate limits — max 2 concurrent).
6. Stop calling tools once you have gathered all required data.
"""


def research_node(state: AgentState) -> dict[str, Any]:
    """
    LangGraph node: gathers market data, news, and macro indicators.

    Returns partial state updates — LangGraph merges these into the full state
    using the Annotated operator.add reducers defined in AgentState.
    """
    logger.info(
        "research_node: starting ticker=%s depth=%s", state["tickers"], state["research_depth"]
    )

    settings = get_settings()
    llms = [candidate.bind_tools(RESEARCH_TOOLS) for candidate in build_llm_candidates(settings)]

    # Build initial message for the LLM
    depth_instruction = {
        "quick": "Call get_market_data only for each ticker.",
        "standard": "Call get_market_data, get_financial_news, and get_macro_indicators.",
        "deep": "Call all available tools including get_sec_filing_summary.",
    }[state["research_depth"]]

    user_message = (
        f"Research the following tickers: {', '.join(state['tickers'])}.\n"
        f"Research depth: {state['research_depth']}. {depth_instruction}\n"
        f"Original query: {state['query']}"
    )

    messages: list[Any] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    ticker_analyses: list[TickerAnalysis] = []
    news_items: list[NewsItem] = []
    macro_indicators: list[MacroIndicator] = []
    sources: list[SourceRecord] = []
    tool_calls_log: list[dict[str, Any]] = []
    iterations = 0
    max_iterations = settings.react_max_iterations
    model_calls = state.get("model_calls", 0)
    input_tokens = state.get("input_tokens", 0)
    output_tokens = state.get("output_tokens", 0)

    while iterations < max_iterations and model_calls < settings.max_model_calls_per_job:
        iterations += 1
        last_error: Exception | None = None
        response: AIMessage | None = None
        for index, llm in enumerate(llms):
            if model_calls >= settings.max_model_calls_per_job:
                break
            model_calls += 1
            try:
                response = llm.invoke(messages)
                if index:
                    logger.warning("research_node: LLM fallback provider index=%d used", index)
                break
            except Exception as exc:
                last_error = exc
                logger.error("research_node: LLM provider index=%d failed", index)
        if response is None:
            raise RuntimeError("All configured LLM providers failed") from last_error
        if response.usage_metadata is not None:
            input_tokens += response.usage_metadata.get("input_tokens", 0)
            output_tokens += response.usage_metadata.get("output_tokens", 0)
        messages.append(
            {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [dict(tc) for tc in (response.tool_calls or [])],
            }
        )

        if not response.tool_calls:
            logger.info("research_node: no more tool calls — done after %d iterations", iterations)
            break

        # Execute each tool call
        for tc in response.tool_calls:
            if len(tool_calls_log) >= settings.max_tool_calls_per_job:
                logger.warning("research_node: tool-call budget exhausted")
                break
            tool_name = tc["name"]
            tool_args = tc["args"]
            tool_call_id = tc["id"]

            logger.info("research_node: tool=%s args=%s", tool_name, tool_args)

            tool_fn = TOOL_MAP.get(tool_name)
            if not tool_fn:
                result = {"error": f"Unknown tool: {tool_name}"}
            else:
                try:
                    circuit = _CIRCUITS.setdefault(
                        tool_name,
                        CircuitBreaker(
                            settings.circuit_breaker_failure_threshold,
                            settings.circuit_breaker_recovery_seconds,
                        ),
                    )
                    result = circuit.call(partial(_invoke_tool_with_timeout, tool_fn, tool_args))
                    result = sanitize_tool_result(
                        result,
                        max_chars=settings.max_external_content_chars,
                        policy=settings.prompt_injection_policy,
                    )
                except CircuitOpenError as exc:
                    logger.error("research_node: tool=%s circuit open", tool_name)
                    result = {"error": str(exc)}
                except UnsafeContentError as exc:
                    logger.error("research_node: tool=%s unsafe content", tool_name)
                    result = {"error": str(exc)}
                except TimeoutError as exc:
                    logger.error("research_node: tool=%s timed out error=%s", tool_name, exc)
                    result = {"error": str(exc)}
                except Exception as exc:
                    logger.error("research_node: tool=%s failed error=%s", tool_name, exc)
                    result = {"error": str(exc)}

            # Classify result into the correct state bucket
            if (
                tool_name == "get_market_data"
                and isinstance(result, dict)
                and "error" not in result
            ):
                ticker_analyses.append(cast(TickerAnalysis, result))
            elif tool_name == "get_financial_news" and isinstance(result, list):
                news_items.extend(result)
            elif tool_name == "get_macro_indicators" and isinstance(result, list):
                macro_indicators.extend(result)

            if (
                tool_name in _SOURCE_LOCATORS
                and not (isinstance(result, dict) and "error" in result)
                and result
            ):
                sources.extend(_source_records(tool_name, tool_args, result))

            # Always log every tool call for audit/observability
            tool_calls_log.append(
                {
                    "tool": tool_name,
                    "args": tool_args,
                    "result_type": type(result).__name__,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "iteration": iterations,
                }
            )

            # Feed result back to LLM as ToolMessage
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps(result, default=str),
                }
            )

    if iterations >= max_iterations:
        logger.warning("research_node: hit max_iterations=%d — stopping", max_iterations)

    logger.info(
        "research_node: complete analyses=%d news=%d macro=%d tool_calls=%d",
        len(ticker_analyses),
        len(news_items),
        len(macro_indicators),
        len(tool_calls_log),
    )

    error = None
    if not ticker_analyses and not news_items:
        error = "No research data was available from the configured providers."
    elif not sources:
        error = "Research data lacked verifiable source provenance."

    return {
        "ticker_analyses": ticker_analyses,
        "news_items": news_items,
        "macro_indicators": macro_indicators,
        "sources": sources,
        "tool_calls_log": tool_calls_log,
        "messages": [
            {
                "role": "system",
                "content": f"Research gathered {len(ticker_analyses)} "
                f"ticker analyses, {len(news_items)} news items, "
                f"{len(macro_indicators)} macro indicators.",
            }
        ],
        "error": error,
        "model_calls": model_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
