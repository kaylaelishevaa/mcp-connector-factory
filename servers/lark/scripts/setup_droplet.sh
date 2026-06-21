#!/usr/bin/env bash
#
# Lark MCP Server — droplet bootstrap.
#
# Run ONCE on the droplet as deploy user. Idempotent.
# ASSUMES Activities + Q&A bot setup_droplet.sh already ran (Docker present).
#
# Adds:
#   1. /home/deploy/lark-mcp-server/data/logs directory
#   2. logrotate config for MCP logs
#   3. lark-mcp-tail helper script
#
# After this:
#   1. cd /home/deploy/lark-mcp-server
#   2. cp .env.example .env && nano .env  (paste LARK_MCP_TOKEN, Lark creds)
#   3. docker compose build
#   4. docker compose up -d
#   5. docker compose logs cloudflared  (grep for tunnel URL)
#
set -euo pipefail

echo "=== Lark MCP Server — Droplet Setup ==="

PROJECT_DIR="/home/deploy/lark-mcp-server"

# 1. Sanity check Docker
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker not found. Run Activities bot setup_droplet.sh first."
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: docker compose plugin missing."
    exit 1
fi

# 2. Create dirs
mkdir -p "$PROJECT_DIR/data/logs"

# 3. Logrotate
sudo tee /etc/logrotate.d/lark-mcp-server >/dev/null <<EOF
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
sudo tee /usr/local/bin/lark-mcp-tail >/dev/null <<EOF
#!/usr/bin/env bash
TODAY=\$(date +%Y-%m-%d)
cd ${PROJECT_DIR}
echo "=== Lark MCP container status ==="
docker compose ps
echo ""
echo "=== Last 50 lines from main ==="
tail -n 50 "data/logs/\${TODAY}/main.log" 2>/dev/null || echo "(no log file yet)"
echo ""
echo "=== Last 50 lark_client lines ==="
tail -n 50 "data/logs/\${TODAY}/lark_client.log" 2>/dev/null || echo "(no log file yet)"
echo ""
echo "=== Last 20 MCP tool calls (audit stream) ==="
tail -n 20 "data/logs/lark_mcp_calls.jsonl" 2>/dev/null || echo "(no calls yet)"
echo ""
echo "=== Last 10 anomalies ==="
tail -n 10 "data/logs/anomalies.jsonl" 2>/dev/null || echo "(no anomalies yet)"
echo ""
echo "=== Cloudflared tunnel URL (last 20 lines) ==="
docker compose logs --tail=20 cloudflared 2>/dev/null | grep -E "trycloudflare.com|tunnel" || echo "(tunnel not running)"
EOF
sudo chmod +x /usr/local/bin/lark-mcp-tail

echo ""
echo "=== Done ==="
echo "Next:"
echo "  1. cd ${PROJECT_DIR}"
echo "  2. cp .env.example .env && nano .env"
echo "     (generate LARK_MCP_TOKEN: python -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "  3. docker compose build"
echo "  4. docker compose up -d"
echo "  5. docker compose logs cloudflared | grep trycloudflare"
echo "     → copy URL, paste to the operator's Claude app + LARK_MCP_TOKEN"
echo ""
echo "Health check: lark-mcp-tail"
