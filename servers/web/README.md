# Acme Web MCP Server

MCP connector giving Claude read (later write) access to **example.com**,
by wrapping the existing NestJS admin REST API. Sibling to `servers/lark`.

- **Spec:** internal proposal (not included in this case study)
- **Design:** `00_DESIGN.md` · **Investigation / auth decision:** `00_INVESTIGATION.md`

## Status
- ✅ **Read-only connector** — built, 56 unit tests passing (all HTTP mocked).
- ⏳ **Live acceptance gate** (`scripts/smoke_test.py`) — written; run once the service account exists (see below).
- 🔒 **Write surface** — placeholder only, gated OFF (`WEB_WRITE_ENABLED`), not implemented.

## Read tools (4) + a cache utility
`web_get_listing` · `web_search_listings` · `web_list_listings` · `web_get_translations`
(plus `refresh_cache`, a utility that drops the client GET cache — not a read tool).

(`web_diff_against_lark` is intentionally deferred — Claude diffs across this
connector + the Lark connector, keeping this one web-only. See `00_DESIGN.md`.)

## Quickstart

```bash
# Tests
pip install -r requirements.txt
LOG_ROOT=/tmp/web-mcp-logs pytest -q

# Run
cp .env.example .env       # fill WEB_MCP_TOKEN + WEB_SERVICE_EMAIL/PASSWORD
docker compose build && docker compose up -d
curl -s localhost:8081/ | jq
```

## Auth (two layers)
- **Inbound** (Claude→MCP): `WEB_MCP_TOKEN` bearer.
- **Outbound** (MCP→admin API): service-user login-and-refresh JWT
  (`WEB_SERVICE_EMAIL`/`WEB_SERVICE_PASSWORD`). The user must be
  `users.role='internal'` with a non-`agent` RBAC role granting `view listing`.

## Before the live acceptance gate (human steps)
1. Provision the internal service user + RBAC role (admin panel; see `00_INVESTIGATION.md` §1b).
2. Store creds in the secrets store; fill `.env`.
3. Run the gate with a diverse AR# set:
   ```bash
   python scripts/smoke_test.py AR103917 AR103821 AR103824 ...
   ```
   It proves agentScope visibility, normalizer field mapping (with a live field
   table to eyeball), and cold bulk-list latency. Exit 0 = read-only connector accepted.

## Deploy
`scripts/setup_droplet.sh` (once) then `scripts/deploy.sh deploy@<DROPLET_IP>`.
Exposed to Claude via cloudflared, same pattern as the Lark connector.

## Safety posture
Read-only today. Writes are blocked until the Lark↔web reconciliation
decision (proposal §5/§8) and ship dry-run-default + batch-approve + backup +
audit-log + portal-throttle. Write default is **OFF** (stricter than the Lark
connector) because this adds a third writer to the web DB.
