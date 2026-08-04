# Financial Research Agent — GitHub Copilot Agent Instructions

## Project overview
LangGraph stateful multi-node agentic workflow for autonomous equity research.
Stack: Python 3.12 · LangChain 0.2 · LangGraph 0.2 · FastAPI · Pydantic v2

## Architecture
```
AgentState (TypedDict with operator.add reducers)
    │
    ├── validate_input node     — normalises tickers, sets depth
    ├── research_node           — ReAct tool loop (LLM + 4 tools)
    │       ├── get_market_data         (Alpha Vantage)
    │       ├── get_financial_news      (NewsAPI + sentiment)
    │       ├── get_macro_indicators    (Alpha Vantage macro)
    │       └── get_sec_filing_summary  (SEC EDGAR, no key)
    └── analyst_node            — synthesises ResearchReport JSON
```

## Coding standards
- Python 3.12 features: use `X | Y` union types, `match` statements where appropriate
- Type hints on every function — mypy strict mode must pass
- All classes are Pydantic v2 `BaseModel` or `TypedDict` — no bare dicts for domain objects
- All tools use `@tool` decorator from `langchain_core.tools`
- All external HTTP calls use `httpx` with explicit timeouts (never `requests`)
- Retry logic via `tenacity` — every external call has `@retry(stop=stop_after_attempt(3))`
- Logging: use `logging.getLogger(__name__)` — never `print()`
- No secrets in code — all credentials via `Settings` (pydantic-settings, env vars)
- LLM temperature ≤ 0.2 for analytical tasks

## LangGraph patterns
- State fields that accumulate across nodes MUST be `Annotated[list[T], operator.add]`
- Nodes return partial state dicts — never mutate the input state directly
- Conditional edges always have an explicit fallback to `END` for error paths
- Graph is compiled once at startup and reused (singleton via `get_graph()`)
- Use `graph.ainvoke()` from async context (FastAPI endpoint / background task)

## Tool development rules
- Every new tool MUST:
  1. Use `@tool` decorator with a clear docstring explaining args and return type
  2. Degrade gracefully to mock data when API keys are absent
  3. Have `@retry` with `stop_after_attempt(3)` and `wait_exponential`
  4. Log at INFO level on entry, ERROR on failure
  5. Never raise unhandled exceptions — return `{"error": str(exc)}` instead
- Tool mock data lives in `_MOCK_*` module-level constants
- Tools are registered in `RESEARCH_TOOLS` list in `research_agent.py`
- Add new tools to `TOOL_MAP` in `research_agent.py`

## Testing requirements
- Unit tests: mock all external APIs — tests MUST run offline (no API keys in CI)
- Test naming: `test_<behaviour>_when_<condition>`
- Coverage threshold: 80% minimum (enforced by pytest-cov)
- `pytest -x` before every commit
- Integration tests go in `tests/integration/` — may need env vars

## Adding a new data source
Use the skill: `.github/skills/add-data-source.yml`

## PR checklist
- [ ] `ruff check . && ruff format --check .` passes
- [ ] `mypy src/` passes
- [ ] `pytest -x --cov=src` passes with ≥80% coverage
- [ ] New tool has mock data for offline testing
- [ ] `.env.example` updated if new env var added
- [ ] `README.md` updated if new capability added
