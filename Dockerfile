# Digest-pinned base image; Dependabot keeps the digest current.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS builder

WORKDIR /build

COPY requirements.lock .
RUN pip install --prefix=/install --no-cache-dir --require-hashes -r requirements.lock

FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS runtime

LABEL org.opencontainers.image.title="financial-research-agent"
LABEL org.opencontainers.image.description="LangGraph autonomous equity research agent"
LABEL org.opencontainers.image.version="1.0.0"

RUN groupadd --gid 10001 agentuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agentuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# Copy application source
COPY --chown=agentuser:agentuser src/ ./src/
COPY --chown=agentuser:agentuser main.py .
COPY --chown=agentuser:agentuser logging.json.conf ./

# Switch to non-root
USER agentuser

EXPOSE 8080
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health')"

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--access-log", \
     "--log-config", "logging.json.conf"]
