#!/usr/bin/env bash
#
# Acme Web MCP Server — deploy from local Mac → droplet.
#
# Usage:
#   ./scripts/deploy.sh deploy@<DROPLET_IP>                  # rsync + build + up
#   ./scripts/deploy.sh deploy@<DROPLET_IP> --skip-build     # rsync only
#
# Preserves: .env, data/
#
set -euo pipefail

SKIP_BUILD=0

if [ $# -lt 1 ]; then
    echo "Usage: $0 deploy@<DROPLET_IP> [--skip-build]"
    exit 1
fi

REMOTE="$1"
shift || true
for arg in "$@"; do
    case "$arg" in
        --skip-build) SKIP_BUILD=1 ;;
        *) echo "Unknown arg: $arg"; exit 1 ;;
    esac
done

PROJECT_DIR="/home/deploy/web-mcp-server"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Deploying Acme Web MCP Server ==="
echo "Local:      ${LOCAL_DIR}"
echo "Remote:     ${REMOTE}:${PROJECT_DIR}"
echo "Skip-build: ${SKIP_BUILD}"
echo ""

# 1. rsync
rsync -avz --delete \
    --exclude='.env' \
    --exclude='data/' \
    --exclude='logs/' \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    "${LOCAL_DIR}/" \
    "${REMOTE}:${PROJECT_DIR}/"

if [ "${SKIP_BUILD}" -eq 1 ]; then
    echo ""
    echo "=== Rsync done (--skip-build) ==="
    echo "Next manual on droplet:"
    echo "  1. ssh ${REMOTE} 'cat > ${PROJECT_DIR}/.env' (paste contents)"
    echo "  2. ssh ${REMOTE} 'cd ${PROJECT_DIR} && bash scripts/setup_droplet.sh'"
    echo "  3. ssh ${REMOTE} 'cd ${PROJECT_DIR} && docker compose build && docker compose up -d'"
    exit 0
fi

# 2. Build + restart
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose build && docker compose up -d"

# 3. Tail logs
echo ""
echo "=== Tailing logs for 20s ==="
ssh "${REMOTE}" "cd ${PROJECT_DIR} && timeout 20 docker compose logs -f --tail=20 || true"

echo ""
echo "=== Deploy done ==="
echo "Cloudflared tunnel URL:"
ssh "${REMOTE}" "cd ${PROJECT_DIR} && docker compose logs --tail=30 cloudflared | grep -E 'trycloudflare.com' || echo '(named tunnel — URL is your configured hostname)'"
echo ""
echo "Health: ssh ${REMOTE} 'web-mcp-tail'"
