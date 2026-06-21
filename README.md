# MCP Connector Factory — a case study

[![tests](https://github.com/kaylaelishevaa/mcp-connector-factory/actions/workflows/tests.yml/badge.svg)](https://github.com/kaylaelishevaa/mcp-connector-factory/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11-blue.svg)

**TL;DR** — Two production-style **Model Context Protocol** servers that let Claude
safely read and write real business data, built from one reusable, hardened
skeleton. **Stack:** Python · FastAPI · JSON-RPC 2.0 · OAuth 2.0 · Docker ·
Cloudflare Tunnel. **348 tests** (225 + 123, fully mocked). **Best demonstrates:**
backend & messy-API integration, AI agent tooling, and security-minded design
(bearer auth, audit logging, kill-switched writes). Sanitized from real internal work.

---

> A sanitized engineering case study. Two internal Model Context Protocol (MCP)
> servers — one wrapping a SaaS database (Lark Bitable), one wrapping a team's own
> REST API — built from **the same hardened skeleton, forked per backend**. All
> identities, hostnames, IPs, and record IDs have been replaced with fictional
> placeholders ("Acme Property" / `example.com`); the engineering is unchanged.

## The thesis

The interesting artifact here is **not** "a Lark connector." It is a reusable,
hardened **MCP-server skeleton** that you fork once per backend. Two servers are
included precisely to prove the skeleton is a template and not a one-off: the same
scaffolding shows up twice, wearing two different backends *and* two deliberately
different security postures.

```
                         ┌───────────────────────────────────┐
   Claude app ──MCP──▶   │   HARDENED MCP SKELETON (~80–95%    │
   (Bearer token)        │   identical across both forks)     │
                         │   • JSON-RPC 2.0: initialize /      │
                         │     tools/list / tools/call         │
                         │   • bearer auth (constant-time)     │
                         │   • OAuth-2.0 facade for Claude UI  │
                         │   • JSON-line audit logging         │
                         │   • Docker + Cloudflare tunnel      │
                         └──────────────┬────────────────────┘
                    fork per backend ───┴───────────────┐
                            │                            │
                ┌───────────▼───────────┐    ┌───────────▼────────────┐
                │  lark_client → Lark    │    │  web_client → NestJS    │
                │  Bitable (SaaS API)    │    │  admin REST API         │
                ├────────────────────────┤    ├─────────────────────────┤
                │ POSTURE: writes LIVE,  │    │ POSTURE: read-only;     │
                │ admin tier, 1 kill     │    │ writes dark behind TWO  │
                │ switch. Net: audit log │    │ kill switches + dry-run │
                │ + Lark revision hist.  │    │ default. (3rd DB writer)│
                └────────────────────────┘    └─────────────────────────┘
```

The skeleton bundles the parts that are annoying to get right and identical every
time:

- **Hand-rolled JSON-RPC 2.0** over a single HTTPS endpoint, implementing exactly
  the three methods a custom connector needs: `initialize`, `tools/list`,
  `tools/call`. No MCP SDK dependency.
- **Bearer-token inbound auth** — every request carries `Authorization: Bearer
  <token>`; mismatches 401 via a constant-time compare.
- **An OAuth-2.0 facade** (`oauth.py`) so the server drops into Claude's
  custom-connector UI, which expects an OAuth handshake even for a single-user
  static token.
- **Structured JSON-line audit logging** (`logger.py`) — one line per tool call
  plus a separate append-only audit stream for writes.
- **Docker + Cloudflare named-tunnel deploy** to a stable hostname, with the
  bearer token pasted into Claude's connector UI.

You fork it, drop in a backend client and a tool set, and ship. See
[`DEPLOY.md`](DEPLOY.md) for the generic deploy path.

## Proof it's a template, not a coincidence

The shared scaffolding files are nearly byte-identical between the two servers.
Measured on the sanitized tree (`diff` line count = lines that differ in either
direction):

| Shared file | Lark | Web | Lines that differ | What the delta is |
|---|---|---|---|---|
| `oauth.py`   | 413 | 413 | ~36  | branding / docstrings only |
| `auth.py`    | 96  | 105 | ~25  | env-var + service naming |
| `logger.py`  | 141 | 143 | ~50  | audit-stream names |
| `main.py`    | 312 | 272 | ~152 | tool dispatch + lifespan wiring |

So ~80–95% of the scaffolding is identical. The genuine, intentional deltas are
exactly three things:

1. **The backend client.** `lark_client.py` wraps a SaaS Bitable API (no
   server-side filtering → it pre-fetches all records into memory and filters
   locally). `web_client.py` wraps the team's own NestJS admin REST API (filters
   server-side → no fetch-all; instead a per-request GET cache + a JWT
   login-and-refresh loop).
2. **The tool set.** Lark exposes 15 read handlers + generic `update_record` /
   `create_record` writes; Web exposes 5 read tools + a (dark) write surface.
3. **`agen_registry.py`** — a Lark-only `ou_id` ↔ display-name map used to enrich
   opaque user IDs into names.

## Two contrasting security postures (the lesson)

Same skeleton, deliberately different risk envelope — this is the part worth
copying into your own thinking, not the code.

**Lark connector — writes live, full-access.** The single user is the business
owner ("the operator"), trusted at admin tier. Writes (`update_record`,
`create_record`) ship **on** by default behind one kill switch
(`LARK_WRITE_ENABLED`, default `true`). There is *no* two-stage confirm and *no*
field allowlist — a single trusted user didn't warrant the friction. The safety
net is **append-only audit logging** (before/after snapshots, caller-token hash)
plus **Lark's native per-record revision history** for rollback. `delete_record`
is intentionally not exposed.

**Web connector — read-only today, writes designed but dark.** This server adds a
*third* writer to a production web database (alongside the admin UI and portal
sync), so the posture is inverted. Writes are built but gated **off** behind
**two independent kill switches**:

- `WEB_WRITE_ENABLED` — listing create/update/publish/sold/delete/bulk.
- `WEB_IDENTITY_WRITE_ENABLED` — user + role/permission writes, kept *separate*
  precisely because that is the privilege-escalation surface and you want to be
  able to enable listing edits without ever opening the identity surface.

Both default `false`. Every write tool is `dry_run=True` by default and follows
pre-read → backup → execute → post-read-verify → audit, with a portal-fan-out
rate limit. Same skeleton; the difference is entirely policy.

## Lessons learned

The three architectural takeaways are below. For the full set of war stories —
the auth scheme that passed every test but couldn't connect in the app, the
silent scoping footgun, the numbers that secretly matched nothing, the design
decisions made then reversed — see **[`LESSONS.md`](LESSONS.md)**.

- **The "OAuth facade" is a single-user shortcut, not an authorization server.**
  `/oauth/token` issues the *static bearer token itself* back as the
  `access_token`, and only does so if the caller proves `client_secret ==
  LARK_MCP_TOKEN`. It satisfies Claude's connector UI (which insists on an OAuth
  dance) for one trusted user. It is **not** multi-tenant OAuth — there is no per-
  user identity, no real token issuance, no scoping. Do not copy this into a
  system with more than one principal.

- **Why hand-rolled JSON-RPC instead of the official MCP SDK.** The server needs
  exactly three methods (`initialize`, `tools/list`, `tools/call`). Hand-rolling
  them is a few dozen lines in `main.py` and buys explicit, per-request control
  over auth and audit logging — which is the whole security story here — without
  taking an SDK dependency or its abstractions over the request lifecycle.

- **What we'd do next: extract an `mcp_core/` package.** The ~80% shared
  scaffolding (`oauth.py`, `auth.py`, `logger.py`, the JSON-RPC dispatch in
  `main.py`) is begging to be a library the two servers import. Be honest, though:
  the real project deliberately kept them as **separate forks** — the shared
  contract doc states plainly *"SEPARATE codebases — no shared code"* (see
  [`shared-architecture.md`](shared-architecture.md)) — coordinated only by a
  written contract. The trade-off is real: forking gave fast divergence (the two
  backends and two security postures pulled in different directions immediately)
  and blast-radius isolation (a change to one can't break the other), at the cost
  of DRY and the ~80% duplication measured above. For two servers that was the
  right call; at five it would not be.

## Repo map

```
mcp-connector-factory-casestudy/
├── README.md                 this file — the narrative
├── LESSONS.md                war stories — what broke and how it was fixed
├── DEPLOY.md                 generic, vendor-neutral deploy guide
├── LICENSE                   MIT
├── shared-architecture.md    the sanitized "Generic Lark Query" contract both
│                             servers (and a sibling bot) were built against
└── servers/
    ├── lark/                 wraps Lark Bitable (SaaS) — writes live, admin tier
    │   ├── server/
    │   │   ├── main.py           FastAPI app + JSON-RPC dispatch
    │   │   ├── auth.py           inbound bearer check
    │   │   ├── oauth.py          OAuth-2.0 facade for Claude's connector UI
    │   │   ├── logger.py         JSON-line call log + write audit stream
    │   │   ├── lark_client.py    backend client (fetch-all + local filter)
    │   │   ├── agen_registry.py  ou_id ↔ display-name map (fictional roster)
    │   │   └── tools/            read_tools.py (15) + write_tools.py
    │   ├── tests/               225 passing, 2 skipped
    │   ├── Dockerfile · docker-compose.yml · requirements.txt · .env.example
    │   └── scripts/             deploy.sh · setup_droplet.sh · dump_lark_schema.py
    └── web/                   wraps a NestJS admin REST API — read-only, writes dark
        ├── server/             same skeleton; web_client.py + 5 read tools
        │                       + a dual-kill-switch write surface (gated off)
        ├── tests/              123 passing
        ├── 00_INVESTIGATION.md how the NestJS auth/serialization shape was reverse-engineered
        └── (Dockerfile, compose, scripts, .env.example as above)
```

Each server also keeps its own `00_DESIGN.md` — the original design record, which
is the substance of the case study.

## Running the tests

Each server is self-contained. The code targets **Python 3.11**; it also runs on
3.9 if you add the `eval_type_backport` shim (pydantic needs it to evaluate
`str | None` annotations on < 3.10).

```bash
# Lark connector
cd servers/lark
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LOG_ROOT=/tmp/lark-logs pytest -q        # 225 passed, 2 skipped

# Web connector
cd ../web
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LOG_ROOT=/tmp/web-logs pytest -q         # 123 passed
```

`LOG_ROOT` points the JSON-line logger at a scratch dir so tests don't write into
the repo. No test makes a live network call — the backends are fully mocked.

## License

Released under the [MIT License](LICENSE) — © 2026 Kayla Elisheva Siwi. Fork the
skeleton and use it! 

---

*Sanitization note: the agen roster, all `ou_`/`tbl`/base IDs, phone numbers,
emails, the production IP, SSH user, brand names, market locations, and
third-party portal names in this repo are fictional placeholders. The one
intentional exception is the IANA timezone `Asia/Jakarta`, which is preserved
because it is load-bearing for the date-handling logic (and the tests that depend
on its offset) — it is functional configuration, not a brand fingerprint.*
