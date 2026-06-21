# Lark MCP Server — Design

**Audience:** the AI implementing this (or a future maintainer).
**Author:** the author, via a Cowork session, 2026-05-20.
**For:** the operator (Acme owner), via the Claude app (mobile + desktop) custom connector.

---

## 1. Mission

The operator installs a custom MCP connector in the Claude app → asks questions about the Lark Bitable "Acme Operations" base (listings, contacts, activities, deals) through Claude chat. Claude calls the tools we expose → pulls data from Lark → formats a natural-language answer.

**Permission tier:** the operator = full owner, no filter. Sees every field including owner name + phone (unlike the agent Q&A bot, which strips identity).

**Read-only first**, then **write tools** (shipped 2026-05-28) after read usage was validated. `update_record` + `create_record`, full access (no field allowlist), direct write (NO two-stage confirm — single trusted user). Safety net = append-only audit log (`lark_mcp_writes.jsonl`, before/after snapshots) + Lark's native per-record revision history for rollback + `LARK_WRITE_ENABLED` kill switch (default on).

---

## 2. Architecture

```
[the operator's Claude app — mobile + desktop]
            │  MCP JSON-RPC over HTTPS
            │  Authorization: Bearer <LARK_MCP_TOKEN>
            ▼
[Cloudflare Tunnel] — https://xxx-yyy-zzz.trycloudflare.com
   (cloudflared daemon on the droplet; free, no domain needed)
            │  forward to localhost:8080
            ▼
[FastAPI MCP server] — Python 3.11, in a Docker container on the droplet
            │
            ├─→ Lark Bitable Open API (read tools — full access)
            └─→ Logging /data/logs/lark_mcp_calls.jsonl
```

Same droplet as the Activities + Q&A bot. Add 2 docker compose services:
1. `lark-mcp` — FastAPI app (Python)
2. `cloudflared` — tunnel daemon

---

## 3. Decisions locked (the author, 18–20 May 2026)

| Topic | Decision |
|---|---|
| Use case | the operator interactively queries Lark via Claude chat |
| Permission tier | Full (the operator sees owner name + phone) — NO filter |
| Hosting | Existing droplet 203.0.113.10, Cloudflare Tunnel generic URL |
| Auth | Single bearer token from `.env` |
| Protocol | MCP JSON-RPC over HTTPS |
| MVP scope | READ-only first, writes deferred |
| Tech stack | Python 3.11, FastAPI, hand-rolled MCP handler (lightweight) |
| Reuse | `lark_client.py` pattern from the upstream AI bot platform — adapted for full access (NO owner strip) |

---

## 4. MCP Protocol (subset)

Client (Claude app) → Server: JSON-RPC 2.0 POST to `/mcp`

**Methods supported:**

- `initialize` — handshake, return capabilities + protocol version
- `tools/list` — return all available tool schemas
- `tools/call` — execute a tool with arguments

**Auth:** every request must carry the header `Authorization: Bearer <token>`. Mismatch → 401.

**Example tool call:**
```json
POST /mcp
Authorization: Bearer xyz123...
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "search_listings",
    "arguments": {"area": "Sunset District", "kt": 2, "status": "Available"}
  }
}
```

Response:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "Found 12 listings: ..."}],
    "isError": false
  }
}
```

---

## 5. Tools

### READ tier

| Tool | Args | Returns |
|---|---|---|
| `search_listings` | area?, kt?, km?, harga_max?, status?, building?, limit=20 | List of listings matching filter |
| `get_listing` | record_id | Full record incl. owner name + phone |
| `find_activities` | date?, agen_ou_id?, tipe?, status?, listing_id? | List activities |
| `search_contacts` | query (free-text name match) | List contacts |
| `get_lark_url` | record_id, table (listings/contacts/activities) | Direct Lark URL |
| `find_listings_by_owner` | owner_name OR contact_id | All listings of that owner |
| `get_agen_listings` | agen_ou_id | All listings assigned to an agen |

### WRITE tier (shipped 2026-05-28)

Generic, not per-field tools (supersedes the original per-field confirm-gated
plan below). Two tools cover all the original use cases:

- `update_record(table_name, record_id, fields)` — patch any field on any record
  (covers update_listing_status / update_listing_price / assign_listing_to_agen).
- `create_record(table_name, fields)` — create any record (covers create_activity).

**No two-stage `confirm` protocol** (the original plan). The operator is the single
trusted owner-tier user; the audit log + Lark revision history are the safety
net instead of a confirm gate. Tools RAISE on failure → MCP `isError: true`, so
the AI never reports a failed write as success. `delete_record` is intentionally
NOT exposed (soft/hard delete via the Lark UI). Per-token read/write scoping +
batch writes are backlog.

~~Original plan (not built): two-tier confirm protocol with `confirm: bool`,
per-field tools assign_listing_to_agen / share_record_collaborator /
update_listing_status / update_listing_price / create_activity.~~

---

## 6. Repo structure

```
servers/lark/
├── 00_DESIGN.md                   this file
├── README.md
├── .env.example
├── .gitignore
├── requirements.txt               fastapi + uvicorn + httpx + python-dotenv
├── Dockerfile                     Python 3.11-slim + uvicorn
├── docker-compose.yml             lark-mcp + cloudflared services
├── server/
│   ├── __init__.py
│   ├── main.py                    FastAPI app + MCP endpoints
│   ├── auth.py                    bearer token check
│   ├── lark_client.py             Lark API client (full access — no owner strip)
│   ├── agen_registry.py           ou_id ↔ display name mapping (6 agen)
│   ├── logger.py                  JSON line logger + audit stream
│   └── tools/
│       ├── __init__.py
│       └── read_tools.py          TOOL_SCHEMAS + handlers + TOOL_HANDLERS registry
│                                  (consolidated single file — all 11 tools)
├── scripts/
│   ├── setup_droplet.sh           cloudflared install + tunnel init
│   ├── deploy.sh                  rsync + docker rebuild
│   └── start_tunnel.sh            manual cloudflared run (testing)
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_tools_read.py
│   └── test_mcp_protocol.py
└── data/                          runtime state + logs (gitignored)
```

---

## 7. Permission policy

**NONE.** The operator = full owner, sees everything. No field stripping, no name redaction, no phone masking.

Defense is limited to:
- **Auth gate**: token-based, single user. If the token leaks → revoke + regenerate.
- **Audit log**: every tool call → a JSON line to `/data/logs/lark_mcp_calls.jsonl` (timestamp, tool, args summary, result size, latency). For after-the-fact review.
- **Rate limit** (future): currently none. The Lark API has a 100 req/min/app limit — a natural ceiling.

---

## 8. Build order

Built read-first; writes only after read usage was real.

### Scaffold + auth + first read tools
- Folder structure
- `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`
- `server/main.py` — FastAPI + MCP JSON-RPC handler
- `server/auth.py` — bearer token validation
- `server/lark_client.py` — full-access Lark wrapper
- `server/tools/read_tools.py` (consolidated schemas + handlers + registry) for: `search_listings`, `get_listing`, `find_activities`
- `tests/conftest.py`, `test_auth.py`, `test_tools_read.py`, `test_mcp_protocol.py`

Smoke: `docker compose build` + unit tests pass.

### Remaining read tools
- `search_contacts`, `get_lark_url`, `find_listings_by_owner`, `get_agen_listings`
- Integration tests with mocked Lark
- Update tool list in schemas

### Deploy + Cloudflare Tunnel + operator setup
- `scripts/setup_droplet.sh` — install cloudflared, init tunnel
- `scripts/deploy.sh` — rsync + docker build
- `DEPLOY.md` — the operator pastes the tunnel URL + token into the Claude app
- Live smoke test from the operator

### Monitor + iterate
- Watch `lark_mcp_calls.jsonl` for actual usage
- Heavy read usage → green-light writes
- Used only 1–2×/week → maybe over-built, reassess

### Write tools (shipped 2026-05-28, after read usage validated)
- `update_record` + `create_record` (generic, any table/field — no per-field tools, no confirm gate)
- Kill switch `LARK_WRITE_ENABLED` (default on)
- Append-only audit log `lark_mcp_writes.jsonl` (before/after snapshots, success flag, caller token hash)
- Tools raise on failure → MCP `isError: true`
- Tests: `tests/test_write_tools.py` (mock happy/failure paths, audit-log assertions, kill switch, end-to-end isError, lark_client HTTP plumbing).

---

## 9. Risks I want explicit

1. **Cloudflare Tunnel free-tier limit**: ~50 req/sec, no SLA. Fine for the operator solo. Upgrade to a branded URL if usage gets heavy.
2. **Single token = single point of failure**: forget to rotate → security degrades. Mitigation: rotate quarterly, document the procedure.
3. **Lark API rate limit (100/min)**: if the Claude app retries aggressively → hit the limit. Add a rate limiter at the MCP server layer.
4. **The operator asking for owner info → full data dump**: this is BY DESIGN per the author (the operator = owner trust). But chat history with Claude is stored on their device. If the device is compromised → owner data exposed. Out of our scope.
5. **MCP protocol evolution**: Anthropic's spec changes over time. Lock to a specific protocol version, test compatibility after Claude app updates.

---

## 10. Out of scope (this folder)

- ~~WRITE tools (deferred)~~ → shipped (update_record + create_record)
- `delete_record`, batch writes, per-token read/write scoping (write backlog)
- Multi-user support (the operator only)
- Web UI (the Claude app IS the UI)
- Cron / scheduled jobs (MCP is request-response only)
- Direct integration with the Q&A bot or Activities bot (separate concerns)

---

**End of design.** Build proceeds with this as the contract. Changes → update the doc first, then the code.
