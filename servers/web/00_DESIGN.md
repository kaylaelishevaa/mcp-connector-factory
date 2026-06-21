# servers/web — Design

**Status:** Read-only connector built + unit-tested (56 passing). Live
acceptance gate (`scripts/smoke_test.py`) written; awaiting the service account
to run. The write surface is a gated placeholder.
**Spec:** internal proposal (not included in this case study) · **Investigation:** `00_INVESTIGATION.md`
**Template:** mirrors `../lark/`.

---

## What this is

An MCP server that gives Claude read (later write) access to example.com,
by wrapping the **existing NestJS admin REST API** at
`https://admin.example.com/api`. It is a thin HTTP adapter — it never
touches MySQL directly and never touches Lark, so every backend invariant
(price_histories, slug, FK, cache self-invalidation, portal sync) is preserved.

```
Claude ──MCP(JSON-RPC)──▶ Acme Web MCP Server ──HTTPS+JWT──▶ NestJS admin API ──▶ MySQL
                          (this folder)        (login-      (admin.example.com/api)    (+ portal sync)
                                                 refresh)
```

## Two independent auth layers

| Layer | Direction | Mechanism | Env |
|---|---|---|---|
| **Inbound** | Claude → MCP | static bearer, constant-time compare (`server/auth.py`) | `WEB_MCP_TOKEN` |
| **Outbound** | MCP → admin API | service-user JWT, login-and-refresh (`server/web_client.py`) | `WEB_SERVICE_EMAIL` / `WEB_SERVICE_PASSWORD` |

**Outbound = approach (i-b)** (chosen 2026-06-09, see investigation §1a). The
admin `AdminGuard` is JWT-only with hardcoded 7d expiry and no API-key path. So
the client logs in with service creds, caches the JWT (expiry parsed from the
`exp` claim, no signature verification), re-logins proactively within 24h of
expiry and reactively (once) on any 401.

Service user requirement (investigation §1b): `users.role='internal'` + an RBAC
role **not** named `agent` granting `view listing` (read tier). A no-RBAC-role
internal user 403s every guarded route; an `agent`-role user gets silently
scoped to its own listings.

## Caching — deliberately unlike 13

The Lark connector pre-fetches *all* records into memory because Lark has no
server-side filter. The admin API **does** filter server-side, so we do **not**
fetch-all. Instead:
- a small **per-request GET cache** (`_get_cache`, 1h TTL, keyed by path+params)
  cuts repeat-read latency; `refresh_cache` clears it.
- **startup prefetch warms only the JWT** (one login) so the first user query
  doesn't pay login latency inside Claude's ~30s MCP timeout.

## Tools (read tier)

| Tool | Endpoint(s) | Notes |
|---|---|---|
| `web_get_listing(property_id)` | `GET /admin/listings?search=AR#` → `GET /admin/listings/:id` | No by-property_id route; resolve AR# via search (propertyId startsWith), then full detail. |
| `web_search_listings({...})` | `GET /admin/listings?...` | Server-side filters; `apartment_name`/`tower`/`unit` fold into the `search` param. |
| `web_list_listings({status,category,page})` | `GET /admin/listings?...` | Bulk pull; replaces the manual xlsx export. Surfaces pagination meta if present. |
| `web_get_translations(property_id)` | `GET /admin/listings/:id` (`translatable`) | Per-lang title/short_description/content/slug — source of truth for slug/title audits. |
| `refresh_cache()` | — | Drops the GET cache for fresh reads. |

Normalizer (`web_client.normalize_listing`) is grounded in the real
`serializeListing` output (`admin-listing.service.ts:1820`), which emits BOTH
camelCase and snake_case; the mapper reads either, and pulls unit attributes
(building_area/bedrooms/floor/floor_zone/tower/unit) from the polymorphic
`listingable` child.

**Response envelope (caught during smoke-test build):** a global NestJS
`ResponseInterceptor` (`common/interceptors/response.interceptor.ts`) wraps every
response as `{success, data}`, and flattens Laravel pagination into
`{success, data:[rows], links, meta:{current_page,last_page,total,per_page}}`.
`web_client._unwrap()` strips it: detail/whoami/login unwrap to `.data`; list
calls keep the envelope so `_iter_rows` reads `data` and the tools read `meta`.

## web_diff_against_lark — DEFERRED (deliberate)

The proposal calls this the highest-value tool, but building it *here* would make
this connector call the Lark API — violating the web-only rule (build prompt
rule #1). Decision: **Claude orchestrates the diff across the two separate
connectors** (this one's read tools + the existing servers/lark). Revisit
only if importing a read-only `lark_client` into this server is explicitly OK'd;
that's a scope/security decision, not a default.

## The write surface — not built, gated OFF

`server/tools/write_tools.py` is a placeholder (empty registries + `_write_enabled`
defaulting **OFF** — stricter than 13's default-ON, because this adds a *third*
writer to the web DB). Implement per proposal §3a truth-table only after the
Lark↔web reconciliation decision (§5/§8). Every write tool: `dry_run=True`
default, gate check, pre-read → backup → execute → post-read verify → audit log,
bulk rate-limit (portal fan-out), and ⛔ no "rented + stay public" (no endpoint
produces it).

## Files

```
server/main.py         MCP JSON-RPC dispatch + health + CORS + lifespan prefetch
server/auth.py         inbound bearer (WEB_MCP_TOKEN)
server/web_client.py   outbound admin-API client: login-refresh, GET cache, normalizers
server/logger.py       JSON-line logs + audit streams (web_mcp_calls/writes, anomalies)
server/tools/read_tools.py    5 read tools (read tier)
server/tools/write_tools.py   write-tier placeholder, gated OFF
tests/                 56 tests, all HTTP mocked (no live calls, no mutations)
scripts/smoke_test.py  LIVE acceptance gate (read-only) — see below
Dockerfile, docker-compose.yml, scripts/{deploy,setup_droplet}.sh, .env.example
```

## Read-only acceptance gate (`scripts/smoke_test.py`)

Run once the service account exists. Read-only (GET + one login POST, never a
mutation). Takes AR#s as CLI args; pass a deliberately diverse set. Gates:
0. **Read-only** — asserts `WEB_WRITE_ENABLED` off + no write tools (aborts if on).
1. **Auth + role** — login works; `whoami` proves a non-`agent` internal user with
   `view listing`. Auth failure prints a distinct banner + exit 2 (so it can't be
   mistaken for a mapping bug).
2. **agentScope proof** — a broad PUBLISHED list returns >1 distinct owner incl.
   listings not owned by the service user (the silent mis-role returns empty/403,
   not an error), and each AR# comes back populated.
3. **Normalizer** — per-AR# field-by-field table (normalized vs raw listingable/
   translatable keys) to eyeball live key names; branch-coverage report
   (SELL/RENT/SOLD-or-DRAFT/translations/apartment+tower+unit); hard-fails on a
   clear mapping break (apt with raw listingable but no mapped unit fields, or
   translations present but no mapped title).
4. **Cold latency** — a COLD `web_list_listings` over all PUBLISHED apartments,
   asserted well under Claude's ~30s MCP timeout (default <25s).

Exit: 0 all pass · 1 a gate failed · 2 auth failed.
```bash
python scripts/smoke_test.py AR103917 AR103821 AR103824 ...  # diverse set
```

## Running

```bash
# tests (3.11 target; portable to 3.9+ — annotations are lazy / Optional)
pip install -r requirements.txt
LOG_ROOT=/tmp/web-mcp-logs pytest -q       # 51 passing

# local server
cp .env.example .env   # fill WEB_MCP_TOKEN + WEB_SERVICE_EMAIL/PASSWORD
docker compose build && docker compose up -d
curl -s localhost:8081/ | jq
```

## Open before go-live (read-only smoke test)
1. Provision the `internal` service user + non-`agent` RBAC role w/ `view listing`.
2. Put its creds in the secrets store; fill `.env`.
3. Run the live smoke test: `web_get_listing` on 2–3 real AR# vs the public page,
   and **validate the normalizer field mapping** against the real API response
   (the mapper is defensive across camel/snake + envelope shapes, but the live
   shape should confirm `listingable`/`translatable` key names).
