# Financial Research Agent

Autonomous equity research agent built with **LangGraph** and **LangChain**. Given a ticker (e.g. `NVDA`), the agent gathers market data, financial news, macroeconomic indicators, and SEC filings, then synthesises a structured research report.

> [!IMPORTANT]
> This software produces machine-generated research, not investment advice. Outputs can be incomplete or incorrect and require qualified human review before any financial decision.

Demonstrates: LangGraph stateful workflows · LangChain tool calling · ReAct agent loop · FastAPI async API · CI security and quality gates.

---

## Architecture

```
POST /api/v1/research
        │
        ▼
  [FastAPI Server]  ── background task ──►  LangGraph Graph
                                                    │
                                           validate_input node
                                           (normalise tickers, set depth)
                                                    │
                                           research_node (ReAct loop)
                                           ┌─────────────────────────┐
                                           │  LLM decides which tools │
                                           │  ┌─────────────────────┐ │
                                           │  │ get_market_data     │ │  Alpha Vantage
                                           │  │ get_financial_news  │ │  NewsAPI
                                           │  │ get_macro_indicators│ │  Alpha Vantage
                                           │  │ get_sec_filing      │ │  SEC EDGAR (free)
                                           │  └─────────────────────┘ │
                                           └─────────────────────────┘
                                                    │
                                           analyst_node
                                           (synthesis → ResearchReport JSON)
                                                    │
                                                   END
                                                    │
                                            ◄── poll GET /api/v1/research/{job_id}
```

### Key LangGraph patterns used
- **`TypedDict` + `Annotated[list[T], operator.add]`** — state fields accumulate across parallel branches without mutation
- **Conditional edges** — `route_after_research()` short-circuits to `END` on error, preventing the analyst node from running on empty data
- **Partial state returns** — each node returns only the fields it updates; LangGraph merges
- **`graph.ainvoke()`** — async invocation compatible with FastAPI's event loop

---

## Project structure

```
financial-research-agent/
├── src/
│   ├── models/
│   │   └── state.py              # AgentState TypedDict + domain models
│   ├── config/
│   │   ├── settings.py           # Pydantic-settings (all config from env vars)
│   │   └── llm.py                # LLM factory: Azure OpenAI / GitHub Models / OpenAI
│   ├── tools/
│   │   ├── market_data_tool.py   # Alpha Vantage fundamentals
│   │   ├── news_tool.py          # NewsAPI + sentiment classification
│   │   ├── macro_tool.py         # CPI, rates, GDP indicators
│   │   └── sec_filing_tool.py    # SEC EDGAR 10-K/10-Q (free, no key)
│   ├── agents/
│   │   ├── research_agent.py     # ReAct tool loop node
│   │   └── analyst_agent.py      # Synthesis / report generation node
│   ├── graph/
│   │   └── workflow.py           # LangGraph graph definition + run_research()
│   └── api/
│       ├── server.py             # FastAPI: auth, jobs, health, metrics
│       └── job_store.py          # Namespaced Redis/in-memory job state
├── tests/
│   ├── unit/
│   │   ├── test_tools.py         # Tool unit tests (fully offline — no API keys needed)
│   │   └── test_workflow.py      # Graph routing + node logic tests
│   └── integration/
│       └── test_api.py           # FastAPI contract tests
│   └── evals/
│       └── test_quality_gate.py  # Deterministic model-output safety gates
├── scripts/
│   ├── test_local.sh             # curl-based end-to-end test
│   └── run_graph_interactive.py  # CLI runner for local debugging
├── k8s/deployment.yaml           # Hardened Deployment + Service + HPA + PDB
├── docker-compose.yml            # Full local stack: agent + Redis + Prometheus + Grafana
├── .github/
│   ├── agent.md                  # GitHub Copilot agent instructions
│   ├── skills/add-data-source.yml
│   ├── skills/add-graph-node.yml
│   └── workflows/ci.yml          # Lint → SAST → test → Docker build → push
├── Dockerfile                    # Multi-stage, non-root, minimal runtime image
├── docs/                         # Architecture, operations, and failure modes
├── SECURITY.md                   # Vulnerability reporting and support policy
├── pyproject.toml                # ruff + mypy + pytest config
├── requirements.txt              # Direct runtime requirements
├── requirements.lock             # Hash-locked transitive runtime graph
└── .env.example
```

---

## Quickstart (local demo)

With `ALLOW_MOCK_DATA=true`, missing or unavailable providers may return **illustrative, stale demo records** for supported examples. Demo records are not live market data and must never be enabled in a shared or production environment. The Kubernetes manifest disables them.

```bash
# 1. Clone and set up environment
git clone <repo-url> && cd financial-research-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Copy env file (leave API keys blank to use mock data)
cp .env.example .env
# For free LLM: set LLM_PROVIDER=github_models and GITHUB_TOKEN=<your PAT>
# Keep ALLOW_MOCK_DATA=true only for this local demo.

# 3. Run the agent interactively (CLI)
python scripts/run_graph_interactive.py --ticker NVDA --depth standard

# 4. Or start the API server
uvicorn main:app --port 8080 --reload

# 5. Submit a research job
curl -X POST http://localhost:8080/api/v1/research \
  -H "Content-Type: application/json" \
  -d '{"query":"Analyse NVDA for long-term hold","tickers":["NVDA"],"research_depth":"standard"}'

# 6. Poll for result (replace <job_id> with value from step 5)
curl http://localhost:8080/api/v1/research/<job_id>
```

---

## LLM provider configuration

The agent supports three providers with zero code change — only env vars differ:

| Provider | Use case | Cost |
|---|---|---|
| `azure_openai` | Production | Pay-per-token |
| `github_models` | Dev / CI | Free (via GitHub PAT) |
| `openai` | Direct fallback | Pay-per-token |

```bash
# GitHub Models (free) — ideal for development
LLM_PROVIDER=github_models
GITHUB_TOKEN=github_pat_xxxxx

# Azure OpenAI (production)
LLM_PROVIDER=azure_openai
AZURE_OPENAI_ENDPOINT=https://YOUR_RESOURCE.openai.azure.com/
AZURE_OPENAI_API_KEY=xxxxx
AZURE_CHAT_DEPLOYMENT=gpt-4o
```

---

## Running tests

```bash
# Install runtime and development dependencies
pip install -r requirements-dev.txt

# Unit tests (no API keys or LLM required — fully offline)
pytest tests/unit/ -v

# Integration tests (API contract tests — no real LLM calls)
pytest tests/integration/ -v

# Full suite with coverage
make gate
```

---

## Docker Compose (full local stack)

```bash
# Start the development stack: agent + Redis + Prometheus + Grafana
GITHUB_TOKEN=<your-token> GRAFANA_ADMIN_PASSWORD=<local-password> docker compose up -d

# Run an end-to-end test
./scripts/test_local.sh

# View metrics
open http://localhost:3000   # Grafana (admin / GRAFANA_ADMIN_PASSWORD)
open http://localhost:9090   # Prometheus
open http://localhost:8080/docs  # Swagger UI
```

---

## Production deployment (Kubernetes)

### Prerequisites
- Kubernetes 1.28+
- `kubectl` configured for your cluster
- A container image digest pushed to your registry
- An authenticated ingress or API gateway for user identity, authorization, TLS, and rate limits
- A managed, highly available Redis service for production workloads

### Deploy

```bash
# 1. Create secrets (never commit these)
kubectl create namespace agentforge
kubectl create secret generic financial-research-agent-secrets \
  --namespace agentforge \
  --from-literal=AZURE_OPENAI_ENDPOINT=https://YOUR_RESOURCE.openai.azure.com/ \
  --from-literal=AZURE_OPENAI_API_KEY=<key> \
  --from-literal=ALPHA_VANTAGE_API_KEY=<key> \
  --from-literal=NEWSAPI_API_KEY=<key> \
  --from-literal=LANGSMITH_API_KEY=<key> \
  --from-literal=API_KEY=<long-random-service-key> \
  --from-literal=REDIS_URL=rediss://<managed-redis-endpoint>:6379

# 2. Update the immutable image digest and CORS origin in k8s/deployment.yaml
# ghcr.io/YOUR_ORG/financial-research-agent@sha256:REPLACE_WITH_IMAGE_DIGEST
# CORS_ALLOW_ORIGINS=https://research.example.com

# 3. Apply manifests
kubectl apply -f k8s/deployment.yaml

# 4. Verify rollout
kubectl rollout status deployment/financial-research-agent -n agentforge
kubectl get pods -n agentforge

# 5. Test the deployment
kubectl port-forward svc/financial-research-agent 8080:80 -n agentforge
./scripts/test_local.sh
```

The production manifest intentionally does not deploy Redis; `REDIS_URL` must reference a managed, highly available TLS endpoint. See [Operations](docs/operations.md) for rollout, health, failure, and scaling guidance.

### Scaling

The HPA automatically scales between 2–8 replicas based on CPU (70%) and memory (80%) utilisation. To manually scale:

```bash
kubectl scale deployment financial-research-agent --replicas=4 -n agentforge
```

---

## LangSmith tracing (recommended for production)

[LangSmith](https://smith.langchain.com) traces every graph invocation — node inputs/outputs, LLM calls, token usage, latency.

```bash
LANGCHAIN_TRACING_V2=true
LANGSMITH_API_KEY=<key>
LANGSMITH_PROJECT=financial-research-agent-prod
```

Each research job appears as a single trace with child spans for each node and tool call. Essential for debugging agent reasoning.

---

## Adding a new data source

Use the GitHub Copilot skill in `.github/skills/add-data-source.yml`:

```
@workspace /add-data-source
```

This guides you through: creating the tool file, registering it in `research_agent.py`, adding the API key to settings and `.env.example`, writing mock data, and adding tests.

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/research` | Submit research job |
| `GET` | `/api/v1/research/{job_id}` | Poll for result |
| `GET` | `/api/v1/research` | List recent jobs |
| `GET` | `/api/v1/health` | Liveness check |
| `GET` | `/api/v1/ready` | Readiness check |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/docs` | Swagger UI |

The production Kubernetes manifest sets `API_AUTH_REQUIRED=true` and `EXPOSE_API_DOCS=false`. Research endpoints require `X-API-Key`; the key identifies the calling service, not an end user. Keep user authorization, rate limits, and TLS at the gateway.

### Sample request

```json
POST /api/v1/research
X-API-Key: <service-key>
{
  "query": "Analyse NVIDIA for long-term investment in the AI infrastructure cycle",
  "tickers": ["NVDA"],
  "research_depth": "deep"
}
```

### Sample response (completed job)

```json
{
  "job_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "status": "completed",
  "created_at": "2024-11-21T20:00:00Z",
  "completed_at": "2024-11-21T20:00:45Z",
  "tool_calls_count": 4,
  "report": {
    "executive_summary": "NVDA is the dominant AI compute infrastructure provider...",
    "investment_thesis": "Market leadership in GPU compute + CUDA software moat...",
    "bull_case": "Blackwell ramp drives 100%+ data centre revenue growth in FY2025...",
    "bear_case": "Export controls and stretched valuation (68x P/E) limit upside...",
    "risk_rating": "MEDIUM",
    "recommended_action": "BUY",
    "price_target_12m": 1050.0,
    "confidence_score": 0.87,
    "data_sources_used": ["market_data", "news", "macro", "sec_filings"],
    "generated_at": "2024-11-21T20:00:44Z"
  }
}
```

---

## Design decisions

**Why LangGraph instead of a simple ReAct loop?**
LangGraph's stateful graph enables explicit error handling via conditional edges, clean separation between data-gathering and synthesis, and the ability to add parallel branches (e.g. running macro and news fetch simultaneously) without rewriting the orchestration logic.

**Why separate research and analyst nodes?**
Single-responsibility principle — the research node is stateless and tool-focused; the analyst node does no I/O and only reasons over the data already gathered. Each can be tested, scaled, and replaced independently.

**Why `operator.add` reducers on list state fields?**
Without reducers, parallel branches would overwrite each other's output. `operator.add` means two branches appending to `news_items` simultaneously both have their results preserved — this is the correct LangGraph pattern for fan-out workflows.

**Why async job pattern instead of synchronous response?**
Research jobs can take 30–60 seconds depending on depth and LLM latency. A synchronous API would timeout at load balancers. The async job pattern (submit → poll) scales cleanly and allows the client to show progress.

FastAPI `BackgroundTasks` executes work in the serving process; Redis shares job state but is not a durable work queue. A process restart can interrupt an in-flight job. Use an external queue and worker service before adopting strict delivery or retry guarantees.

---

## Production safeguards

- Set `ALLOW_MOCK_DATA=false`, `REDIS_REQUIRED=true`, `API_AUTH_REQUIRED=true`, `EXPOSE_API_DOCS=false`, and an explicit `CORS_ALLOW_ORIGINS`.
- Deploy immutable image digests and retain CI provenance and SBOM attestations for every release.
- Rotate the service API key and place the service behind user authentication, authorization, TLS, and rate limiting.
- Treat prompts, provider responses, traces, and reports as sensitive financial data; define retention and access policies.
- Alert on readiness failures, failed jobs, provider latency, HTTP 5xx rates, and Redis health.
- Start with the [documentation map](docs/README.md), then review [Operations](docs/operations.md) and [Security](SECURITY.md) before deployment.
