FROM python:3.11-slim-bookworm

# System deps + Node 20 (for the Claude Code CLI) + ripgrep/git (used by agent tools).
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git ripgrep bash procps \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 ccuc
WORKDIR /app

# Install the package (deps rarely change -> better layer caching).
COPY pyproject.toml README.md /app/
COPY ccuc /app/ccuc
COPY scripts /app/scripts
COPY bundles /app/bundles
RUN pip install --no-cache-dir -e . \
    && if [ -s /app/bundles/mcp/requirements.txt ]; then pip install --no-cache-dir -r /app/bundles/mcp/requirements.txt; fi

ENV CCUC_DATA_DIR=/data \
    CCUC_HOST=0.0.0.0 \
    CCUC_PORT=8080 \
    CCUC_BUNDLES_DIR=/app/bundles
RUN mkdir -p /data && chown -R ccuc:ccuc /data /app
USER ccuc

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health').status==200 else 1)"]

CMD ["ccuc"]
