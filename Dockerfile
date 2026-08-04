# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="financial-research-agent"
LABEL org.opencontainers.image.description="LangGraph autonomous equity research agent"
LABEL org.opencontainers.image.version="1.0.0"

# Non-root user — security best practice
RUN groupadd --gid 1001 agentuser \
    && useradd --uid 1001 --gid 1001 --no-create-home --shell /bin/false agentuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=agentuser:agentuser src/ ./src/
COPY --chown=agentuser:agentuser main.py .
COPY --chown=agentuser:agentuser logging.json.conf ./

# Switch to non-root
USER agentuser

# Expose application port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/api/v1/health').raise_for_status()"

# Entrypoint: production-grade uvicorn with structured access logging
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--access-log", \
     "--log-config", "logging.json.conf"]
