FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY tests/ ./tests/
# scripts/ holds the read-only acceptance gate (smoke_test.py), run in-container
# post-deploy via `docker compose exec web-mcp python scripts/smoke_test.py`.
COPY scripts/ ./scripts/

VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Jakarta \
    HOST=0.0.0.0 \
    PORT=8081

EXPOSE 8081

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8081", "--log-level", "info"]
