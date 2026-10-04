FROM python:3.12-slim

# Run as an unprivileged user (uid 10001, matching runAsUser in deploy/vks/deployment.yaml).
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mcp

WORKDIR /app
# Build context: repo root (docker build -t stock-mcp-server .)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/mcp_server/main.py src/mcp_server/healthcheck.py ./

ENV PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
EXPOSE 8080

# Runtime contract: listen on HOST:PORT (default 0.0.0.0:8080), GET /health -> 200.
# healthcheck.py probes the address the server listens on (python, because the slim image has no curl).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "main.py"]
