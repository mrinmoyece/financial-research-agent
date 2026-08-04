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
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage

from src.config.llm import build_llm
from src.config.settings import get_settings
from src.models.state import AgentState
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


def _invoke_tool_with_timeout(tool_fn: Any, tool_args: dict[str, Any],
                               timeout: float = TOOL_CALL_TIMEOUT_SECONDS) -> Any:
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
        raise TimeoutError(
            f"Tool call timed out after {timeout}s"
        ) from exc

# All tools the research agent can call
RESEARCH_TOOLS = [
    get_market_data,
    get_financial_news,
    get_macro_indicators,
    get_sec_filing_summary,
]

TOOL_MAP = {t.name: t for t in RESEARCH_TOOLS}

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
    logger.info("research_node: starting ticker=%s depth=%s",
                state["tickers"], state["research_depth"])

    settings = get_settings()
    llm = build_llm(settings).bind_tools(RESEARCH_TOOLS)

    # Build initial message for the LLM
    depth_instruction = {
        "quick":    "Call get_market_data only for each ticker.",
        "standard": "Call get_market_data, get_financial_news, and get_macro_indicators.",
        "deep":     "Call all available tools including get_sec_filing_summary.",
    }[state["research_depth"]]

    user_message = (
        f"Research the following tickers: {', '.join(state['tickers'])}.\n"
        f"Research depth: {state['research_depth']}. {depth_instruction}\n"
        f"Original query: {state['query']}"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_message},
    ]

    ticker_analyses = []
    news_items = []
    macro_indicators = []
    tool_calls_log = []
    iterations = 0
    max_iterations = settings.react_max_iterations

    while iterations < max_iterations:
        iterations += 1
        response: AIMessage = llm.invoke(messages)
        messages.append({"role": "assistant", "content": response.content or "",
                          "tool_calls": [tc.model_dump() for tc in (response.tool_calls or [])]})

        if not response.tool_calls:
            logger.info("research_node: no more tool calls — done after %d iterations", iterations)
            break

        # Execute each tool call
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            tool_call_id = tc["id"]

            logger.info("research_node: tool=%s args=%s", tool_name, tool_args)

            tool_fn = TOOL_MAP.get(tool_name)
            if not tool_fn:
                result = {"error": f"Unknown tool: {tool_name}"}
            else:
                try:
                    result = _invoke_tool_with_timeout(tool_fn, tool_args)
                except TimeoutError as exc:
                    logger.error("research_node: tool=%s timed out error=%s", tool_name, exc)
                    result = {"error": str(exc)}
                except Exception as exc:
                    logger.error("research_node: tool=%s failed error=%s", tool_name, exc)
                    result = {"error": str(exc)}

            # Classify result into the correct state bucket
            if tool_name == "get_market_data" and isinstance(result, dict) and "error" not in result:
                ticker_analyses.append(result)
            elif tool_name == "get_financial_news" and isinstance(result, list):
                news_items.extend(result)
            elif tool_name == "get_macro_indicators" and isinstance(result, list):
                macro_indicators.extend(result)

            # Always log every tool call for audit/observability
            tool_calls_log.append({
                "tool": tool_name,
                "args": tool_args,
                "result_type": type(result).__name__,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "iteration": iterations,
            })

            # Feed result back to LLM as ToolMessage
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result, default=str),
            })

    if iterations >= max_iterations:
        logger.warning("research_node: hit max_iterations=%d — stopping", max_iterations)

    logger.info(
        "research_node: complete analyses=%d news=%d macro=%d tool_calls=%d",
        len(ticker_analyses), len(news_items), len(macro_indicators), len(tool_calls_log),
    )

    return {
        "ticker_analyses": ticker_analyses,
        "news_items":      news_items,
        "macro_indicators": macro_indicators,
        "tool_calls_log":  tool_calls_log,
        "messages": [{"role": "system", "content": f"Research gathered {len(ticker_analyses)} "
                                                     f"ticker analyses, {len(news_items)} news items, "
                                                     f"{len(macro_indicators)} macro indicators."}],
    }
