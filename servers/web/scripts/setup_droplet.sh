#!/usr/bin/env bash
#
# Acme Web MCP Server — droplet bootstrap.
#
# Run ONCE on the droplet as deploy user. Idempotent.
# ASSUMES Docker already present (e.g. the Lark MCP / bot setup ran first).
#
# Adds:
#   1. /home/deploy/web-mcp-server/data/logs directory
#   2. logrotate config for MCP logs
#   3. web-mcp-tail helper script
#
set -euo pipefail

echo "=== Acme Web MCP Server — Droplet Setup ==="

PROJECT_DIR="/home/deploy/web-mcp-server"

# 1. Sanity check Docker
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker not found. Run the base bot setup_droplet.sh first."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose plugin missing."
    exit 1
fi

# 2. Create dirs
mkdir -p "$PROJECT_DIR/data/logs"

# 3. Logrotate
sudo tee /etc/logrotate.d/web-mcp-server >/dev/null <<EOF
${PROJECT_DIR}/data/logs/**/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 deploy deploy
}
${PROJECT_DIR}/data/logs/*.jsonl {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    size 100M
    create 0644 deploy deploy
}
EOF

# 4. Health helper
sudo tee /usr/local/bin/web-mcp-tail >/dev/null <<EOF
#!/usr/bin/env bash
TODAY=\$(date +%Y-%m-%d)
cd ${PROJECT_DIR}
echo "=== Web MCP container status ==="
docker compose ps
echo ""
echo "=== Last 50 lines from main ==="
tail -n 50 "data/logs/\${TODAY}/main.log" 2>/dev/null || echo "(no log file yet)"
echo ""
echo "=== Last 50 web_client lines ==="
tail -n 50 "data/logs/\${TODAY}/web_client.log" 2>/dev/null || echo "(no log file yet)"
echo ""
echo "=== Last 20 MCP tool calls (audit stream) ==="
tail -n 20 "data/logs/web_mcp_calls.jsonl" 2>/dev/null || echo "(no calls yet)"
echo ""
echo "=== Last 10 writes ==="
tail -n 10 "data/logs/web_mcp_writes.jsonl" 2>/dev/null || echo "(no writes yet)"
echo ""
echo "=== Last 10 anomalies ==="
tail -n 10 "data/logs/anomalies.jsonl" 2>/dev/null || echo "(no anomalies yet)"
EOF
sudo chmod +x /usr/local/bin/web-mcp-tail

echo ""
echo "=== Done ==="
echo "Next:"
echo "  1. cd ${PROJECT_DIR}"
echo "  2. cp .env.example .env && nano .env"
echo "     - WEB_MCP_TOKEN: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
echo "     - WEB_SERVICE_EMAIL / WEB_SERVICE_PASSWORD: the internal service user"
echo "  3. docker compose build && docker compose up -d"
echo "  4. curl -s localhost:8081/ | jq   (or via the tunnel hostname)"
echo ""
echo "Health check: web-mcp-tail"
