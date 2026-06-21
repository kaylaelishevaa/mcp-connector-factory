# Lark MCP Server

Custom MCP connector exposing Acme Lark Bitable to the operator's Claude app.

**Status:** Complete — read tools, agen resolution, count_listings, startup prefetch, refresh_cache, OAuth facade, generic query, and write tools. **17 tools, 225 tests pass.** Deployed on named tunnel `https://lark-mcp.example.com`.

Implements Lark Generic Query Contract v1.0 (`../../shared-architecture.md`).

## What it does

the operator opens the Claude app (mobile or desktop) → chats: "who has a showing today?" → Claude calls `find_activities(date='today', tipe='Showing')` via our MCP server → returns scheduled activities with agen names resolved → Claude crafts an answer for the operator.

17 tools exposed (11 curated READ + refresh_cache + list_tables + describe_table + query_records + update_record + create_record). The 3 generic tools let the AI reach ANY field/table — curated `normalize_*` only surfaces a subset, so custom fields (Priority Tier, Owner Notes, Deal Stage, ...) now also flow through via `_extra_fields` + `query_records`. Background prefetch on startup warms listing/contact + schema caches so first user query avoids the ~44s cold-fetch latency. WRITE tools — `update_record` + `create_record` — are now live: full access (no field allowlist), no two-stage confirm (single user), every write append-logged to `lark_mcp_writes.jsonl` with before/after snapshots; kill switch `LARK_WRITE_ENABLED`. Rollback story = Lark's native per-record revision history.

## Read first

1. `00_DESIGN.md` — architecture, decisions, tool list
2. `../../DEPLOY.md` — generic deploy guide + connector setup

## Architecture

```
[the operator Claude app] → MCP JSON-RPC over HTTPS → [Cloudflare Tunnel]
                                                      ↓
                                              [FastAPI MCP server in Docker]
                                                      ↓
                                              [Lark Bitable API]
```

Same droplet as the Activities + Q&A bot (`203.0.113.10`).

## Tools (17 total — 11 curated READ + refresh_cache + 3 generic-query + 2 write)

| Tool | Use case |
|---|---|
| `search_listings` | Filter by area/KT/harga/status/building |
| `get_listing` | Full listing detail (incl. owner name+HP, agen names) |
| `find_activities` | Schedule + log query (with agen names resolved) |
| `search_contacts` | Fuzzy name search |
| `get_contact` | Full contact detail |
| `get_lark_url` | Direct Lark Bitable click-link |
| `find_listings_by_owner` | Listings per owner (name or contact_id) |
| `get_agen_listings` | Listings per agen (by ou_id) |
| `list_agen` | Map ou_id ↔ display name (6 agen + VIP) |
| `resolve_agen` | Fuzzy "Rina" → ou_xxx + full info |
| `count_listings` | Cheap count + optional group_by (area/status/kt/agen) — no records returned |
| `refresh_cache` | Force-refresh server cache (listings / contacts / activities / all) |
| `list_tables` | Discover all tables in the base + record counts |
| `describe_table` | Field names + types + sample values + PII flag for a table |
| `query_records` | Generic filter/text-search/sort over any table + any field |
| `update_record` | Patch fields on an existing record (any table). Audit-logged. |
| `create_record` | Create a new record in a table. Audit-logged. |

## Local dev

```bash
cp .env.example .env  # fill LARK_MCP_TOKEN + Lark creds
docker compose build
docker compose up -d
docker compose logs cloudflared | grep trycloudflare  # get tunnel URL
```

## Tests

```bash
pip install -r requirements.txt
LOG_ROOT=/tmp/mcp_logs pytest tests/ -v
# 225 tests pass (2 skipped — live write round-trips, run manually post-deploy)
```

## Auth

Single bearer token in `LARK_MCP_TOKEN` env. Exposed two ways:

1. **Direct Bearer** — for curl smoke tests + Claude Code CLI usage: `Authorization: Bearer <LARK_MCP_TOKEN>` on `/mcp`.
2. **OAuth 2.0 facade** — for Claude desktop/mobile app's custom connector UI (which only accepts OAuth). User pastes token as `OAuth Client Secret`. `/oauth/token` validates secret == `LARK_MCP_TOKEN`, then issues that same token as `access_token`. See `server/oauth.py`.

Rotate quarterly. Lost → regenerate + paste new value in Claude app's Client Secret field.

## Permission policy

**FULL access for the operator.** No field stripping. Owner name + HP visible. Reads + writes (no field allowlist on writes either — admin tier). Audit log every call to `/data/logs/lark_mcp_calls.jsonl`; writes additionally logged with before/after snapshots to `/data/logs/lark_mcp_writes.jsonl` for forensics. No two-stage confirm (single trusted user) — the audit log + Lark's native per-record revision history are the safety net. Kill switch: `LARK_WRITE_ENABLED=false` disables writes without touching reads.

## Repo structure

```
servers/lark/
├── 00_DESIGN.md
├── README.md (this file)
├── .env.example
├── .gitignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── server/
│   ├── main.py            FastAPI + MCP JSON-RPC handler
│   ├── auth.py            Bearer token validation
│   ├── lark_client.py     Lark Bitable client (full access)
│   ├── agen_registry.py   ou_id ↔ name mapping (6 agen)
│   ├── logger.py          JSON-line logger + audit stream
│   └── tools/
│       ├── read_tools.py  14 READ tools (curated + refresh + generic-query) + schemas
│       ├── write_tools.py update_record + create_record + kill switch
│       └── __init__.py
├── scripts/
│   ├── setup_droplet.sh
│   └── deploy.sh
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_mcp_protocol.py
│   ├── test_tools_read.py
│   └── test_agen_registry.py
└── data/  (gitignored, runtime)
```

## Operational

- Health check: `ssh deploy@<droplet> 'lark-mcp-tail'`
- Live logs: `ssh deploy@<droplet> 'cd /home/deploy/lark-mcp-server && docker compose logs -f'`
- Restart: `docker compose restart`
- Audit (all calls): `tail data/logs/lark_mcp_calls.jsonl`
- Audit (writes only, before/after): `tail data/logs/lark_mcp_writes.jsonl`
- Kill writes: set `LARK_WRITE_ENABLED=false` in `.env` → `docker compose up -d`
- Cost: $0 hosting (existing droplet + Cloudflare free tier). the operator's Pro covers Anthropic.
