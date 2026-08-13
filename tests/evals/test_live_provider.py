"""Opt-in live-provider evaluation; never enabled by the pull-request workflow."""

import os

import pytest

from src.graph.workflow import run_research

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_EVALS") != "true",
    reason="Live provider evaluation requires explicit trusted-environment opt-in",
)


@pytest.mark.asyncio
async def test_live_research_is_grounded_and_complete():
    state = await run_research(
        query="Assess AAPL using current fundamentals, news, and macro conditions",
        tickers=["AAPL"],
        research_depth="standard",
    )
    assert state["error"] is None
    assert state["sources"]
    assert state["report"] is not None
    allowed = {source["source_id"] for source in state["sources"]}
    cited = {citation["source_id"] for citation in state["report"]["citations"]}
    assert cited
    assert cited.issubset(allowed)
