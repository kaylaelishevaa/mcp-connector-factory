# Lessons learned — war stories from building two MCP connectors

These are the real problems hit while building the two connectors, sanitized.
They're here because the bugs and the reversed decisions taught more than the
code that shipped. Roughly chronological within each server.

---

## Lark connector

### 1. "Worked in curl, worked in the CLI, dead in the app."

The connector passed every unit test and every `curl` smoke test, and worked
fine from the Claude Code CLI with a plain `Authorization: Bearer <token>`
header. Then it simply could not be added in the Claude desktop/mobile app:
the *Add custom connector* dialog has **no bearer-token field** — only OAuth
Client ID / Client Secret under Advanced. The auth scheme was correct against
the MCP spec and invisible-ly wrong against the actual client UI.

**Fix:** an OAuth-2.0 *facade* (`oauth.py`) — discovery endpoints, open Dynamic
Client Registration, an auto-approving `/authorize`, and a `/token` endpoint.
The real gate never moved: `/oauth/token` only issues a token if
`client_secret == LARK_MCP_TOKEN`, and the access token it hands back *is*
`LARK_MCP_TOKEN`, so `/mcp` validation is unchanged. The user pastes the bearer
token into the "Client Secret" field and everything downstream stays a single
static token.

**Lesson:** verify your auth scheme against the *actual client UI*, not just the
protocol spec. A design can be spec-correct and still un-connectable.

### 2. Two security holes in my own OAuth shim (caught in self-review)

Writing the facade introduced two bugs, both found before deploy:

- **The CORS wildcard was inert.** `allow_origins=["https://*.claude.ai"]` looks
  right but Starlette does *exact-string* matching — the `*` is silently
  ignored, so the intended subdomains were never actually allowed. Switched to
  `allow_origin_regex`.
- **Open redirect on `/authorize`.** `redirect_uri` was accepted as-is. Added a
  host allowlist using `host == allowed or host.endswith("." + allowed)` — a
  bare `endswith` would have happily accepted `evilexample.com`. There are
  regression tests that try to sneak lookalike hosts past it.

Auth codes are also marked single-use *before* the PKCE/secret checks run, so a
replayed code can't slip through on the second attempt.

**Lesson:** a security shim is itself attack surface. The wildcard that does
nothing and the `endswith` that matches too much are both classics worth a test.

### 3. The "Authorization failed" that was really a wrong URL

During live integration the app reported "Authorization failed" — which sent us
hunting through the token logic. The actual cause: the MCP handler lived only at
`/mcp`, and Claude POSTs JSON-RPC to *whatever URL the user pasted*, not to the
`resource` URL advertised in discovery metadata. Paste the URL without the
`/mcp` suffix and the POST lands on `/` → `405` → the app surfaces it as a
generic auth failure.

**Lesson:** server-side discovery metadata is informational; real clients use the
URL the human typed. And error messages from a layer you don't control will lie
to you — confirm the actual status code.

### 4. The bot said a field "doesn't exist" — but it did

A real query asked for leads at a certain priority tier. The bot answered that
the field wasn't available. It was — the curated `normalize_*` functions
returned a hand-picked subset of columns and silently dropped every custom field
(priority tier, deal stage, owner motivation, …).

**Fix:** add an `_extra_fields` bag to every normalizer, plus a generic trio —
`list_tables`, `describe_table`, `query_records` — so the model can *discover*
the schema and reach any field on any table instead of being limited to the
curated shape.

**Lesson:** a curated tool surface is a guess about what users will ask. Give the
model an escape hatch to the full schema, or it will confidently report that
real data doesn't exist.

### 5. Numbers that secretly matched nothing

The SaaS backend returns numeric fields as **strings** — a price comes back as
`"59200000"`, not `59200000`. Every range filter (`harga_max`, between-clauses)
compared a string to a number and silently matched *nothing*, with no error.

**Fix:** a small `_as_number()` coercion (strip commas/spaces, guard `bool`/
`None`) applied before any numeric comparison.

**Lesson:** invisible data-type mismatches are the worst kind — no crash, no log,
just empty results. Coerce at the boundary and test the range filters explicitly.

### 6. Cold start (~44s) blew past the client timeout

The first query after a container restart triggered a full fetch-all of the
dataset (the SaaS API has no server-side filtering, so the connector pulls
everything into memory and filters locally). That cold fetch took ~44s —
longer than the client's ~30s MCP timeout — so the *first* query after every
deploy timed out.

**Fix:** a daemon thread on FastAPI's startup lifespan warms the listing /
contact / schema caches in the background, so the first real query doesn't pay
the cold-fetch cost. (Honest caveat left in the code: a query arriving
mid-prefetch still cold-fetches — there's no lock.)

**Lesson:** "fetch-all + filter locally" is a fine answer to a backend with no
filtering, but it moves the cost to cold start. Warm it before the user arrives.

---

## Web connector

### 7. Reverse-engineering an auth model that wasn't documented

The web connector wraps an existing NestJS admin API, and its auth had to be
discovered from the source, not docs. The guard turned out to be **JWT-only**
(cookie or Bearer) with a **hardcoded 7-day expiry** and no API-key path — and
its behavior didn't match its own docstring (the docstring claimed an
admin-flag check the code never enforced).

A static long-lived token was rejected as the worst blast radius (tied to the
shared signing secret, whose rotation would log out every human admin). The
choice: a **login-and-refresh** client that stores service credentials, logs in
for the 7-day JWT, and re-logs-in proactively (within 24h of expiry) and
reactively (once) on any 401 — parsing the `exp` claim without verifying the
signature.

**Lesson:** when you wrap someone else's API, read the guard's *code*, not its
docstring. Pick the credential with the smallest blast radius you can operate.

### 8. The silent scoping footgun

The backend's permission guard scoped results to the caller's own records if the
caller's RBAC role was literally named `agent`. Get the service account's role
name wrong and every read/write silently returns *its own* rows — empty or 403,
**never an error**. Separately, an internal user with *no* RBAC role 403s every
guarded route.

To defend against this, the live acceptance gate has one job at its core: prove
the connector is *not* silently scoped, by asserting a broad list returns more
than one distinct owner — including records the service account doesn't own.

**Lesson:** the dangerous failures are the silent ones. If a misconfiguration
returns plausible-but-empty data instead of an error, write a test whose entire
purpose is to prove the absence of that failure.

### 9. The response envelope nobody mentioned

A global response interceptor wrapped *every* API response as `{success, data}`
and flattened pagination into `{success, data:[rows], meta:{…}}`. The tools
initially read the raw payload and got the wrapper instead of the rows.

**Fix:** an `_unwrap()` that strips the envelope for detail/login calls but keeps
it for list calls (so the tools can read `meta` for pagination), plus a
defensive row-iterator that handles bare-list, `{data}`, `{items}`, and nested
shapes.

**Lesson:** integration surprises live at the serialization boundary. Build the
unwrapper defensively across the shapes you might get, not the one you saw first.

### 10. The comment that lied

While fact-checking the write design against the backend source, a stale inline
comment ("…stays published") contradicted the code three lines below it
(`status = 'DRAFT'`). The wrong comment misled *two* rounds of design review
before someone read past it.

**Lesson:** trust the code, not the comment. A confident comment that's wrong is
worse than no comment.

---

## Decisions made, then reversed

- **Per-field, confirm-gated write tools → two generic ones.** The original Lark
  write design was a two-stage `confirm: bool` protocol with named tools
  (`update_listing_status`, `update_listing_price`, `assign_listing_to_agen`,
  `create_activity`). It shipped instead as generic `update_record` /
  `create_record` with **no confirm gate** — for a single trusted owner-tier
  user, the audit log plus the SaaS platform's native per-record revision
  history are the safety net, and the confirm step was friction without payoff.

- **Writes were deferred, then shipped once read usage was real.** Writes sat as
  a deferred phase until the operator actually tried to edit a record through
  chat; that real demand is what justified building them.

- **The highest-value cross-system tool was deliberately *not* built here.** A
  "diff the two backends" tool would have been the single most useful feature,
  but building it inside the web connector would make it call the Lark API and
  break the one-backend-per-connector rule. The decision: let the model
  orchestrate the diff across the two separate connectors instead.

- **Shipped "dark."** The web connector's write surface — which can create users,
  mint roles, and delete listings — went to production behind **two** independent
  kill switches, both defaulting **off**, dry-run by default, with confirm
  required on destructive ops. The restraint was the point: build the capability,
  ship it inert, and let a human flip it deliberately.
