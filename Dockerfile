FROM python:3.12-slim
WORKDIR /app
# Build context: repo root (docker build -t stock-mcp-server .)
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src/mcp_server/main.py ./main.py
EXPOSE 8080
# Runtime contract: listen 0.0.0.0:8080, GET /health → 200
CMD ["python", "main.py"]
