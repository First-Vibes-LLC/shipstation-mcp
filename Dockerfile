FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

COPY shipstation_mcp_server.py mcp_oauth.py ./

# --- Runtime stage ---
FROM python:3.12-slim

RUN useradd -m app

WORKDIR /app

COPY --from=builder /app .
COPY --from=builder /root/.local/bin/uv /usr/local/bin/uv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "shipstation_mcp_server.py"]
