"""
LangGraph Workflow — the core agentic graph definition.

Graph topology:
  START
    └─► validate_input
          └─► research_node          (data gathering — ReAct tool loop)
                └─► [conditional]
                      ├─ error?  ──► END (with error state)
                      └─ ok?    ──► analyst_node  (synthesis)
                                          └─► END

The validate_input node is a lightweight pre-check that normalises
tickers and sets research_depth defaults before touching any LLM or API.

The conditional edge after research_node short-circuits to END if the
research node populated an error, preventing the analyst from running
on empty data.
"""

import logging
import re
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from src.agents.analyst_agent import analyst_node
from src.agents.research_agent import research_node
from src.models.state import AgentState

logger = logging.getLogger(__name__)


# ── Validation node ────────────────────────────────────────────────────────────


def validate_input(state: AgentState) -> dict[str, Any]:
    """
    Lightweight pre-flight check before invoking LLMs or external APIs.

    Normalises tickers, sets research_depth default, and validates
    that we have something to analyse.
    """
    query = (state.get("query") or "").strip()
    tickers = [t.upper().strip() for t in (state.get("tickers") or []) if t.strip()]
    depth = state.get("research_depth") or "standard"

    if depth not in ("quick", "standard", "deep"):
        logger.warning("Invalid research_depth '%s' — defaulting to 'standard'", depth)
        depth = "standard"

    if not tickers and query:
        # Attempt naive ticker extraction from query (e.g. "analyse NVDA")
        found = re.findall(r"\b([A-Z]{1,5})\b", query)
        tickers = found[:3]  # cap at 3 tickers per run
        logger.info("validate_input: extracted tickers=%s from query", tickers)

    if not tickers:
        return {
            "tickers": [],
            "query": query,
            "research_depth": depth,
            "error": "No tickers provided or extractable from query.",
            "ticker_analyses": [],
            "news_items": [],
            "macro_indicators": [],
            "sources": [],
            "tool_calls_log": [],
            "messages": [],
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "report": None,
        }

    logger.info("validate_input: tickers=%s depth=%s", tickers, depth)
    return {
        "tickers": tickers,
        "query": query,
        "research_depth": depth,
        "error": None,
        "ticker_analyses": [],
        "news_items": [],
        "macro_indicators": [],
        "sources": [],
        "tool_calls_log": [],
        "messages": [],
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


# ── Conditional routing ────────────────────────────────────────────────────────


def route_after_research(state: AgentState) -> Literal["analyst_node", "__end__"]:
    """
    After research_node: proceed to analyst if data gathered, stop on error.
    """
    if state.get("error"):
        logger.warning("route_after_research: error detected — short-circuiting to END")
        return "__end__"
    if not state.get("ticker_analyses") and not state.get("news_items"):
        logger.warning("route_after_research: no data gathered — short-circuiting to END")
        return "__end__"
    return "analyst_node"


def route_after_validation(state: AgentState) -> Literal["research_node", "__end__"]:
    """Short-circuit to END if validation failed (e.g. no tickers)."""
    if state.get("error"):
        return "__end__"
    return "research_node"


# ── Graph construction ─────────────────────────────────────────────────────────


def build_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """
    Construct and compile the LangGraph StateGraph.

    The compiled graph is thread-safe and can be invoked concurrently
    from multiple FastAPI request handlers.
    """
    builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(
        AgentState,
        input_schema=AgentState,
        output_schema=AgentState,
    )

    # Nodes
    builder.add_node("validate_input", validate_input)
    builder.add_node("research_node", research_node)
    builder.add_node("analyst_node", analyst_node)

    # Edges
    builder.add_edge(START, "validate_input")
    builder.add_conditional_edges("validate_input", route_after_validation)
    builder.add_conditional_edges("research_node", route_after_research)
    builder.add_edge("analyst_node", END)

    graph = builder.compile()
    logger.info("LangGraph workflow compiled successfully")
    return graph


# Singleton graph — compiled once, reused across all requests
_graph: CompiledStateGraph[AgentState, None, AgentState, AgentState] | None = None


def get_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """Return the compiled singleton graph, building it on first call."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


_VALID_RESEARCH_DEPTHS: tuple[str, ...] = ("quick", "standard", "deep")


async def run_research(
    query: str,
    tickers: list[str],
    research_depth: Literal["quick", "standard", "deep"] = "standard",
) -> AgentState:
    """
    High-level async entry point for running a full research workflow.

    Args:
        query:          Natural language query, e.g. "Should I buy NVDA?"
        tickers:        List of ticker symbols, e.g. ["NVDA"]
        research_depth: "quick" | "standard" | "deep"

    Raises:
        ValueError: if research_depth is not one of the supported literals.
            This guards against callers (e.g. a CLI entry point) that only
            have a plain `str` at the type-checker level and could otherwise
            pass an arbitrary value through to AgentState, where it would
            later cause a KeyError in research_agent.py's depth_instruction
            dict lookup instead of failing fast here.

    Returns:
        Final AgentState with report populated (or error set).
    """
    if research_depth not in _VALID_RESEARCH_DEPTHS:
        raise ValueError(
            f"Invalid research_depth={research_depth!r}; "
            f"must be one of {_VALID_RESEARCH_DEPTHS}"
        )

    graph = get_graph()
    initial_state: AgentState = {
        "query": query,
        "tickers": tickers,
        "research_depth": research_depth,
        "ticker_analyses": [],
        "news_items": [],
        "macro_indicators": [],
        "sources": [],
        "tool_calls_log": [],
        "messages": [],
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "report": None,
        "error": None,
    }
    logger.info(
        "run_research: invoking graph query='%s' tickers=%s depth=%s",
        query,
        tickers,
        research_depth,
    )

    # LangGraph ainvoke is async — compatible with FastAPI's event loop
    final_state = cast(AgentState, await graph.ainvoke(initial_state))

    logger.info(
        "run_research: complete report=%s error=%s",
        "generated" if final_state.get("report") else "absent",
        final_state.get("error"),
    )
    return final_state
