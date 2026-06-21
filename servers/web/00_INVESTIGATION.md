# servers/web — Investigation

**Date:** 2026-06-09
**By:** Claude Code (investigation)
**Spec:** internal proposal (not included in this case study)
**Status:** ✅ Investigation complete. ⛔ **STOP — waiting for auth green-light before the read connector.**
**Scope rule honored:** read-only against the repo; 0 prod mutations; 0 Lark touches. The only write performed is the authorized 1-line comment cleanup (§1e).

All findings verified against the NestJS admin codebase on 2026-06-09. File:line references are clickable.

---

## TL;DR

| Task | Result |
|---|---|
| **1a Auth model** | Confirmed: JWT-only, `role ∈ {internal,external}` + per-route permission, no admin-flag, no X-Api-Key. **Token expiry is hardcoded `7d`** (`admin.module.ts:21`, `auth.module.ts:22`). → recommend **(i-b) login-and-refresh** service account; no backend change, no eternal token. |
| **1b Agentscope trap** | Confirmed: `agentScope = (RBAC roleName === 'agent')` (`permission.guard.ts:43`). Service user needs an RBAC role **not** named `agent`, carrying the 6 perms. A bare `internal` user with **no** RBAC role is rejected by every guarded route. |
| **1c Read endpoints** | Confirmed: `GET /api/admin/listings` (rich filters incl. `search=AR…`) + `GET /api/admin/listings/:id` (full record w/ translations, media, price history, listingable). No GET-by-property_id route → resolve AR# via `search`. |
| **1d Truth-table cell** | Confirmed: `bulk-unpublish` leaves `dealStatus` **untouched** (`·` in §3a is correct). No proposal edit needed. Minor caveat: dealStatus can go stale. |
| **1e Landmine** | Confirmed stale comment at `admin-listing.service.ts:1595–1596` ("stay PUBLISHED"). **Fixed** in this pass (comment-only). |

**One decision blocks everything (reads included): which auth approach (§1a).** Recommendation below; needs the author/the operator green-light.

---

## 1a. Auth model + service-account path  *(GATES EVERYTHING)*

### What the code does
- `AdminGuard` (`common/guards/admin.guard.ts`) extracts a JWT from cookie `access_token` **or** `Authorization: Bearer`, verifies it with `JWT_SECRET`, loads the user, and accepts only `user.role ∈ {internal, external}` (`:60`). **No `isAdmin`/admin-flag check** (the class docstring claims one; the code does not enforce it). No `X-Api-Key` path anywhere. ✔ matches v3 §1.
- JWT payload = `{ sub, email, role }` (`auth.service.ts:58–62`).
- **Token lifetime is hardcoded `signOptions: { expiresIn: '7d' }`** in both JWT module registrations (`common/admin.module.ts:21`, `modules/auth/auth.module.ts:22`). Any token minted by the normal login flow dies after 7 days.
- `POST /api/auth/login` is **public** (only `LoginThrottleGuard`, `auth.controller.ts:37–39`) → returns `{ access_token, user, permissions, role_name }`.
- `POST /api/auth/refresh` is behind `JwtAuthGuard` (`auth.controller.ts:62–64`) → re-issues a 7d token, **but only while you still hold a valid token** (can't refresh after expiry).

### Service-account options
- **(i-a) Self-signed long-lived JWT.** Sign a token out-of-band with `JWT_SECRET` (`{sub,email,role:'internal'}`, long/no expiry). `AdminGuard` only `verify()`s against the secret, so it passes. **No backend change.** ✗ Downside: a near-eternal bearer; if leaked it's valid until `JWT_SECRET` rotation (which would also log out every human admin).
- **(i-b) Login-and-refresh (RECOMMENDED).** Connector stores the service user's email+password in a secrets store, calls `POST /api/auth/login` to get a 7d token, caches it, and re-logs-in on expiry/401. **No backend change, no eternal credential**, and it mirrors how the Lark connector already auto-refreshes `tenant_access_token` (`servers/lark/server/lark_client.py`). ✗ Downside: connector holds a password (acceptable — secrets store + rotatable).
- **(ii) New `X-Api-Key` guard.** Cleanest long-term (a service principal, scoped, revocable without touching human sessions), but it's backend work + review + deploy before *any* tool — including reads — can ship.

### Recommendation → DECISION
**✅ the author chose (i-b) login-and-refresh (2026-06-09).** (ii) X-Api-Key remains the north star for when writes go live.

Original recommendation: **(i-b) now, (ii) as the north star.** (i-b) unblocks the read tools today with zero backend change and no never-expiring secret; promote to (ii) when/if writes go live and you want a revocable, scoped principal. Avoid (i-a) — an eternal bearer tied to the shared `JWT_SECRET` is the worst leak blast-radius.

### What the author/the operator must green-light before the read connector
1. **Approve (i-b)** (or pick i-a / ii).
2. **Create the service account** (see §1b for exact requirements). I will **not** create users, roles, or mint tokens without this green-light.
3. **Confirm the secrets-store location** for the service creds (per v3 §7 — *not* plaintext in a Drive `.md`).

---

## 1b. Agentscope trap  *(must get the service user's role right)*

- `permission.guard.ts:43`: `user.agentScope = (roleName === 'agent')`, where `roleName` comes from `modelHasRole → role.name` (first role) — the **RBAC role**, which is *separate* from `users.role` (`internal`/`external`).
- Two independent layers, both must pass:
  - `AdminGuard`: `users.role ∈ {internal, external}`.
  - `PermissionGuard`: the RBAC role must grant the route's `@RequirePermission`. **If the user has no RBAC role (`roleIds.length === 0`) every guarded route 403s** (`permission.guard.ts:46`). Permission name match is underscore-or-space normalized (`view_listing` ≡ `view listing`).
- Filtering: when `agentScope` is true, listing reads/writes are silently scoped to `userId === user.id` (`admin-listing.controller.ts:37` etc., and `findAll` applies `where.userId = scopeUserId` at `admin-listing.service.ts:~408`).

**Requirement for the service user (exact):**
- `users.role = 'internal'`.
- An RBAC role whose **`name !== 'agent'`** (e.g. a dedicated `service` role, or an existing admin/staff role) that grants all six: `view listing`, `add listing`, `edit listing`, `publish listing`, `delete listing`, `export listinglead`.
- For reads alone, `view listing` (+ `export listinglead` if we wrap export) suffices; the rest are for the write tier.

---

## 1c. Read endpoints

### `GET /api/admin/listings` — `findAll` (`admin-listing.controller.ts:30`, service `:~388`)
Server-side filters (query params), all AND-combined:
- `status` (DRAFT|PUBLISHED…), `deal_status` (AVAILABLE|SOLD|RENTED|ARCHIVED), `category` (SELL|RENT)
- `listing_type`/`listingable_type` (mapped via `TYPE_MAP`), `is_new` (NEW|SECONDARY)
- `user_id`, `location_id`
- **`search`** → `OR[ propertyId startsWith , address contains ]` — **this is the AR# lookup** (e.g. `?search=AR103917`).
- `min_price`/`max_price` (BigInt), `min_building_area`/`max_building_area`
- `is_direct_listing` (DIRECT_LISTING vs COBROKE), `is_sold` (category-gated soldAt + rented-child scan)
- pagination: `page`, `load`/`per_page`

### `GET /api/admin/listings/:id` — `findById` (`controller:57`, service `:~636`)
Returns the full record — Promise.all fetches **translations, media, priceHistories**, plus the polymorphic listingable (building_area/bedrooms/floor/etc. mapped per type, `service:~2200,~2460`).

### Mapping notes for the read tools
- **No GET-by-property_id route exists.** `web_get_listing(property_id)` = `findAll?search=AR#####` → take the exact `propertyId` match → (optionally) `GET :id` for the full detail blob. `propertyId` is stored **with** the `AR` prefix in MySQL, so `search=AR103917` matches via `startsWith`.
- `web_get_translations` can reuse `findById` (translations are already embedded) — no separate route needed.
- Export-parity columns (`property_id, apartment_name, tower_name, unit_number, status, sold_at, category, building_area, bedrooms, floor, floor_zone, price`) are all derivable from `findById`/`findAll` + listingable; tower/unit come off the listingable child.

---

## 1d. Truth-table confirmation — `bulk-unpublish` × `dealStatus`

`bulkUnpublish` (`admin-listing.service.ts:1527`) sets **only** `{ status: 'DRAFT', publishedAt: null }`, then `dispatchSync(id,'deactivate')` + `invalidateListingCache(id)`. It **does not touch `dealStatus`**.

→ v3 §3a row "Dedup / pull from public" cell `deal = ·` (untouched) is **correct. No proposal edit required.**

⚠️ Minor caveat to record for tool design: because dealStatus is untouched, an unpublished listing keeps its prior dealStatus (e.g. a previously-RENTED listing stays `RENTED`, an available one stays `AVAILABLE`). Harmless for the dedup/pull-from-public intent, but `web_unpublish`'s post-read verify should assert on `status`/`publishedAt`, **not** on `dealStatus`.

---

## 1e. Landmine — FIXED

`admin-listing.service.ts:1595–1596` previously read:
```
// Rentals close as RENTED (flag the unit, stay PUBLISHED); SELL keeps
// the SOLD + soldAt path. See markAsSold for the rationale.
```
"stay PUBLISHED" is wrong — three lines below, the RENT branch sets `status: 'DRAFT', publishedAt: null` (and the canonical comment at `:1442` says closing hides the listing). Both proposal review rounds were initially misled by it.

**Fixed (comment-only)** to:
```
// Closing HIDES the listing (status=DRAFT, publishedAt=null) either way;
// dealStatus records the real condition. RENT -> RENTED + unit is_rented;
// SELL -> SOLD + soldAt. See the canonical comment at :1442 / markAsSold.
```
No behavior change. This is the only repo write in the investigation; commit at the author's discretion (it sits alongside the already-uncommitted `wa-listing.processor.ts` change — keep them separate when committing).

---

## Open items carried into the read connector (after green-light)
- Auth approach chosen (§1a) + service account provisioned (§1b) + secrets-store location.
- Confirm `cb-prod` has a staging/restore DB for eventual write validation (v3 §7).
- The read connector mirrors `servers/lark/` structure (`server/{main,auth,web_client,logger}.py`, `server/tools/read_tools.py`, `tests/`, `Dockerfile`, `docker-compose.yml`, `scripts/`), with outbound auth = the service-user JWT from (i-b).
- `web_diff_against_lark` design call (reuse a read-only `lark_client` vs ship 4 read tools first) — deferred to the read build.
