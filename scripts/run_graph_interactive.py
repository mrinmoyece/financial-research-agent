"""
Interactive script to run the research graph from the command line.
Useful for local debugging and prompt engineering.

Usage:
  python scripts/run_graph_interactive.py --ticker NVDA --depth standard
  python scripts/run_graph_interactive.py --ticker AAPL --depth deep
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")


async def main():
    parser = argparse.ArgumentParser(description="Run financial research agent interactively")
    parser.add_argument("--ticker", required=True, help="Stock ticker e.g. NVDA")
    parser.add_argument("--depth", default="standard",
                        choices=["quick", "standard", "deep"],
                        help="Research depth")
    parser.add_argument("--query", default=None, help="Custom research query")
    args = parser.parse_args()

    query = args.query or f"Analyse {args.ticker} for investment decision"

    print(f"\n{'='*60}")
    print(f"Running research: {args.ticker} | depth={args.depth}")
    print(f"Query: {query}")
    print(f"{'='*60}\n")

    from src.graph.workflow import run_research
    final_state = await run_research(
        query=query,
        tickers=[args.ticker],
        research_depth=args.depth,
    )

    if final_state.get("error"):
        print(f"\n❌ Error: {final_state['error']}")
        sys.exit(1)

    report = final_state.get("report")
    if not report:
        print("\n❌ No report generated")
        sys.exit(1)

    print(f"\n{'='*60}")
    print("RESEARCH REPORT")
    print(f"{'='*60}")
    print(f"\n📊 Recommendation: {report['recommended_action']}")
    print(f"⚠️  Risk Rating:    {report['risk_rating']}")
    print(f"🎯 12m Target:     ${report.get('price_target_12m', 'N/A')}")
    print(f"📈 Confidence:     {report['confidence_score']:.0%}")
    print(f"\n{'-'*60}")
    print(f"Executive Summary:\n{report['executive_summary']}")
    print(f"\nInvestment Thesis:\n{report['investment_thesis']}")
    print(f"\n🐂 Bull Case:\n{report['bull_case']}")
    print(f"\n🐻 Bear Case:\n{report['bear_case']}")
    print(f"\nSources: {', '.join(report['data_sources_used'])}")
    print(f"Tool calls made: {len(final_state.get('tool_calls_log', []))}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
