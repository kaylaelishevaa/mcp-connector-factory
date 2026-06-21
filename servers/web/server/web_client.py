"""example.com NestJS admin-API client.

Web-only. Calls the existing admin REST API at WEB_API_BASE
(default https://admin.example.com/api). It NEVER talks to MySQL directly
and NEVER touches Lark — every backend invariant (price_histories, slug, FK,
cache self-invalidation, portal sync) is preserved because we go through the
same endpoints the admin panel uses.

OUTBOUND AUTH — approach (i-b) "login-and-refresh" (chosen 2026-06-09, see
00_INVESTIGATION.md §1a): the NestJS `AdminGuard` is JWT-only and tokens expire
in 7d, with no API-key path. So we log in with a dedicated service-user's
credentials (WEB_SERVICE_EMAIL / WEB_SERVICE_PASSWORD, from a secrets store),
cache the returned access_token, and:
  - proactively re-login once the cached token is within REFRESH_SKEW of expiry
    (expiry parsed from the JWT `exp` claim; no signature verification — we are
    the client, not the verifier),
  - reactively re-login + retry once on any 401.

The service user must be `users.role='internal'` with a non-`agent` RBAC role
carrying the needed permissions (reads need: `view listing`). See §1b.

Token caching mirrors servers/lark's tenant_access_token refresh. Unlike
the Lark connector we do NOT fetch-all into memory — the admin API filters
server-side — so reads hit the API live, with a small per-request TTL cache to
cut repeat latency.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any
from urllib.parse import quote

import requests

from server.logger import get_logger, log_anomaly

_log = get_logger("web_client")

DEFAULT_BASE = "https://admin.example.com/api"
REFRESH_SKEW = 24 * 3600          # re-login when <24h of the 7d token remains
_ASSUMED_TTL = 7 * 24 * 3600      # fallback if the JWT has no parseable exp
_GET_CACHE_TTL = 3600             # 1h, mirrors 13's listings cache TTL

_token_state: dict[str, Any] = {"token": None, "expires_at": 0.0}
# GET response cache keyed by (path, sorted-params-tuple) → {data, fetched_at}
_get_cache: dict[tuple, dict[str, Any]] = {}


class WebClientError(RuntimeError):
    pass


# config

def _base() -> str:
    return os.environ.get("WEB_API_BASE", DEFAULT_BASE).strip().rstrip("/")


def _service_creds() -> tuple[str, str]:
    email = os.environ.get("WEB_SERVICE_EMAIL", "").strip()
    password = os.environ.get("WEB_SERVICE_PASSWORD", "").strip()
    if not email or not password:
        raise WebClientError(
            "WEB_SERVICE_EMAIL / WEB_SERVICE_PASSWORD missing in env"
        )
    return email, password


# token: login + refresh

def _jwt_exp(token: str) -> float | None:
    """Best-effort parse of the JWT `exp` claim (seconds since epoch).

    No signature verification — we only need to know when to re-login. Returns
    None if the token isn't a parseable JWT.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad base64url
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _login() -> str:
    """POST /auth/login with service creds → access_token. Caches with expiry."""
    email, password = _service_creds()
    url = f"{_base()}/auth/login"
    try:
        r = requests.post(
            url, json={"email": email, "password": password}, timeout=20
        )
    except requests.RequestException as e:
        raise WebClientError(f"login request failed: {str(e)[:200]}") from e
    if r.status_code != 200:
        raise WebClientError(f"login failed {r.status_code}: {r.text[:200]}")
    token = (_unwrap(r.json() or {}) or {}).get("access_token")
    if not token:
        raise WebClientError("login response missing access_token")
    exp = _jwt_exp(token)
    _token_state["token"] = token
    _token_state["expires_at"] = exp if exp is not None else time.time() + _ASSUMED_TTL
    _log.info("service-user login ok", extra={"exp": _token_state["expires_at"]})
    return token


def get_token(force: bool = False) -> str:
    """Cached service-user JWT. Re-login when forced or within REFRESH_SKEW of exp."""
    now = time.time()
    if (
        not force
        and _token_state["token"]
        and _token_state["expires_at"] > now + REFRESH_SKEW
    ):
        return _token_state["token"]
    return _login()


# request wrapper

def _unwrap(body: Any) -> Any:
    """Strip the global NestJS ResponseInterceptor envelope.

    Every admin-API response is wrapped as {success, data, links?, meta?}
    (response.interceptor.ts). Callers that want the inner payload pass
    unwrap=True; list callers keep the envelope so they can read `data`
    (rows) + `meta` (pagination). Idempotent if no envelope is present.
    """
    if isinstance(body, dict) and "success" in body and "data" in body:
        return body["data"]
    return body


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 30,
    unwrap: bool = False,
) -> Any:
    """Authenticated request to the admin API. Re-logins + retries once on 401.

    Returns the parsed JSON body (the full {success,data,...} envelope unless
    unwrap=True, which returns the inner `data`). Raises WebClientError on
    non-2xx (after the one 401 retry).
    """
    url = f"{_base()}{path}"

    def _do(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        return requests.request(
            method, url, headers=headers, params=params, json=json_body,
            timeout=timeout,
        )

    token = get_token()
    try:
        r = _do(token)
        if r.status_code == 401:
            # token may have been revoked / rotated — re-login once and retry
            _log.info("401 from admin API — re-login + retry", extra={"path": path})
            r = _do(get_token(force=True))
    except requests.RequestException as e:
        raise WebClientError(f"{method} {path} request failed: {str(e)[:200]}") from e

    if r.status_code >= 400:
        raise WebClientError(
            f"{method} {path} failed {r.status_code}: {r.text[:300]}"
        )
    try:
        body = r.json()
    except ValueError:
        return r.text
    return _unwrap(body) if unwrap else body


def _get_cached(path: str, params: dict | None = None) -> Any:
    """GET with a 1h in-memory TTL cache (keyed by path+params)."""
    key = (path, tuple(sorted((params or {}).items())))
    hit = _get_cache.get(key)
    now = time.time()
    if hit and (now - hit["fetched_at"]) < _GET_CACHE_TTL:
        return hit["data"]
    data = _request("GET", path, params=params)
    _get_cache[key] = {"data": data, "fetched_at": now}
    return data


def clear_cache() -> None:
    """Drop the GET cache (used by refresh_cache tool)."""
    _get_cache.clear()


# read endpoints

def list_listings(params: dict) -> Any:
    """GET /admin/listings with server-side filters. Returns the raw admin
    response (shape: a paginated envelope or a list, depending on the route)."""
    return _get_cached("/admin/listings", params)


def get_listing_detail(listing_id: int) -> dict:
    """GET /admin/listings/:id — full record (translatable, media, listingable,
    price_reduced + serializeListing fields). Envelope unwrapped."""
    return _request("GET", f"/admin/listings/{int(listing_id)}", unwrap=True)


def get_user(user_id: int) -> dict:
    """GET /admin/users/:id — single staff/user record (password stripped by the
    service). Envelope unwrapped. Used as the pre/post-read snapshot for user
    write tools."""
    return _request("GET", f"/admin/users/{int(user_id)}", unwrap=True)


def get_role(role_id: int) -> dict:
    """GET /admin/roles/:id — single role with its permission name list +
    users_count. Envelope unwrapped. Pre/post-read snapshot for role tools."""
    return _request("GET", f"/admin/roles/{int(role_id)}", unwrap=True)


# write endpoints
# Thin wrappers over _request(unwrap=True). They deliberately do NOT go through
# the GET cache (only _get_cached caches). The orchestration layer in
# write_tools.py owns the gate / dry-run / confirm / backup / verify / audit
# flow (proposal §4) and calls clear_cache() after every successful mutation so
# subsequent reads don't serve a stale pre-write row.

BULK_THROTTLE_DEFAULT = 1.0  # seconds between items in a bulk op (rail §5)


def bulk_throttle_seconds() -> float:
    """Delay between items in a bulk listing op. Each listing mutation fans out
    to portal-a + partner-portal portal sync, so we pace iteration rather than
    hammer the backend. Override via WEB_BULK_THROTTLE_S (tests set 0)."""
    raw = os.environ.get("WEB_BULK_THROTTLE_S", "").strip()
    if not raw:
        return BULK_THROTTLE_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return BULK_THROTTLE_DEFAULT


# listings
def create_listing(payload: dict) -> Any:
    """POST /admin/listings → add_listing. Returns the created listing."""
    return _request("POST", "/admin/listings", json_body=payload, unwrap=True)


def update_listing(listing_id: int, payload: dict) -> Any:
    """PUT /admin/listings/:id → edit_listing. Returns the updated listing."""
    return _request("PUT", f"/admin/listings/{int(listing_id)}", json_body=payload, unwrap=True)


def listing_state(listing_id: int, action: str) -> Any:
    """PUT /admin/listings/:id/{publish|unpublish|sold|refresh}. The verb-style
    state transitions — `action` is the trailing path segment (validated by the
    caller against a fixed allow-set)."""
    return _request("PUT", f"/admin/listings/{int(listing_id)}/{action}", unwrap=True)


def delete_listing(listing_id: int) -> Any:
    """DELETE /admin/listings/:id → delete_listing."""
    return _request("DELETE", f"/admin/listings/{int(listing_id)}", unwrap=True)


# users
def create_user(payload: dict) -> Any:
    """POST /admin/users → invite_staff."""
    return _request("POST", "/admin/users", json_body=payload, unwrap=True)


def update_user(user_id: int, payload: dict) -> Any:
    """PUT /admin/users/:id → edit_staff."""
    return _request("PUT", f"/admin/users/{int(user_id)}", json_body=payload, unwrap=True)


def delete_user(user_id: int) -> Any:
    """DELETE /admin/users/:id → delete_staff."""
    return _request("DELETE", f"/admin/users/{int(user_id)}", unwrap=True)


# roles + permissions
def create_role(payload: dict) -> Any:
    """POST /admin/roles → add_role. Body: {name, permissions?:[...]}"""
    return _request("POST", "/admin/roles", json_body=payload, unwrap=True)


def update_role(role_id: int, payload: dict) -> Any:
    """PUT /admin/roles/:id → edit_role. Body: {name?, permissions?:[...]}"""
    return _request("PUT", f"/admin/roles/{int(role_id)}", json_body=payload, unwrap=True)


def delete_role(role_id: int) -> Any:
    """DELETE /admin/roles/:id → delete_role."""
    return _request("DELETE", f"/admin/roles/{int(role_id)}", unwrap=True)


def grant_permission(role_id: int, permission: str) -> Any:
    """POST /admin/roles/:id/permissions → update_permission. Body: {permission}."""
    return _request(
        "POST", f"/admin/roles/{int(role_id)}/permissions",
        json_body={"permission": permission}, unwrap=True,
    )


def revoke_permission(role_id: int, permission: str) -> Any:
    """DELETE /admin/roles/:id/permissions/:name → update_permission. Permission
    names contain spaces (e.g. 'view listing') so the segment is URL-encoded."""
    seg = quote(permission, safe="")
    return _request("DELETE", f"/admin/roles/{int(role_id)}/permissions/{seg}", unwrap=True)


def whoami() -> dict:
    """GET /auth/me — the authenticated service user (id, email, role) plus
    `permissions` + `role_name` (RBAC). Used by the smoke test to PROVE the
    account is a non-`agent` internal user with view_listing — the agentScope
    silent-failure guard."""
    return _request("GET", "/auth/me", unwrap=True)


def resolve_by_property_id(property_id: str) -> dict | None:
    """Resolve an AR##### property_id → the matching listing summary.

    There is no GET-by-property_id route; `?search=AR#####` matches
    propertyId via startsWith (admin-listing.service.ts findAll). We take the
    exact propertyId match from the results.
    """
    pid = (property_id or "").strip()
    if not pid:
        return None
    resp = list_listings({"search": pid, "per_page": 50})
    for row in _iter_rows(resp):
        if _row_property_id(row) == pid:
            return row
    # fall back to a looser match (case-insensitive) if exact failed
    for row in _iter_rows(resp):
        if (_row_property_id(row) or "").upper() == pid.upper():
            return row
    return None


# response shape helpers

def _iter_rows(resp: Any) -> list[dict]:
    """Extract the listing rows from an admin list response.

    findAll may return a bare list or a paginated envelope ({data:[...]},
    {items:[...]}, {listings:[...]}, or Laravel-style {data:{data:[...]}}).
    Defensive across shapes so the tools don't break if the envelope changes.
    """
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        for key in ("data", "items", "listings", "results", "rows"):
            v = resp.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):  # nested {data:{data:[...]}}
                inner = v.get("data") or v.get("items")
                if isinstance(inner, list):
                    return [r for r in inner if isinstance(r, dict)]
    return []


def _row_property_id(row: dict) -> str | None:
    return row.get("property_id") or row.get("propertyId")


# normalizers

def _first(d: dict, *keys, default=None):
    """Return the first present, non-None key from d (camelCase/snake aliases)."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def normalize_listing(row: dict) -> dict:
    """Map an admin-API listing row to a friendly snake_case dict.

    Grounded in serializeListing (admin-listing.service.ts:1820) which emits BOTH
    camelCase (Prisma) and snake_case aliases, plus findById's nested
    translatable / listingable / media. Defensive across both casings.
    """
    listingable = _first(row, "listingable", default={}) or {}
    translations = _first(row, "translatable", "translations", default=[]) or []

    title_by_lang = _titles_by_lang(translations)
    slug = _slug_from_translations(translations)

    return {
        "property_id": _row_property_id(row),
        "id": _first(row, "id"),
        "status": _first(row, "status"),
        "deal_status": _first(row, "deal_status", "dealStatus"),
        "is_rented": _first(row, "is_rented", "isRented"),
        "category": _first(row, "category"),
        "type": _first(row, "type"),  # DIRECT_LISTING | COBROKE
        "user_id": _first(row, "user_id", "userId"),  # owner — agentScope proof
        "price": _first(row, "price"),
        "price_reduced": _first(row, "price_reduced"),
        "sold_at": _first(row, "sold_at", "soldAt"),
        "listingable_type": _first(row, "listingable_type", "listingableType"),
        # unit / building attrs live on the polymorphic child
        "building_area": _first(listingable, "building_area", "buildingArea"),
        "land_area": _first(listingable, "land_area", "landArea"),
        "bedrooms": _first(listingable, "bedrooms", "room", "kamar_tidur"),
        "floor": _first(listingable, "floor"),
        "floor_zone": _first(listingable, "floor_zone", "floorZone"),
        "tower_name": _first(listingable, "tower_name", "towerName"),
        "unit_number": _first(listingable, "unit_number", "unitNumber", "unit"),
        "apartment_name": _first(listingable, "apartment_name", "apartmentName"),
        "title_id": title_by_lang.get("id"),
        "title_en": title_by_lang.get("en"),
        "slug": slug,
        "created_at": _first(row, "created_at", "createdAt"),
        "updated_at": _first(row, "updated_at", "updatedAt"),
        "media_count": len(_first(row, "media", default=[]) or []),
        "_raw": row,  # full untouched row for debug / field discovery
    }


def _titles_by_lang(translations: list) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in translations or []:
        if not isinstance(t, dict):
            continue
        lang = _first(t, "lang", "language")
        title = _first(t, "title")
        if lang and title:
            out[str(lang)] = title
    return out


def _slug_from_translations(translations: list) -> str | None:
    # public URLs read the slug from translations; prefer the id-lang slug
    by_lang = {}
    for t in translations or []:
        if isinstance(t, dict):
            lang = _first(t, "lang", "language")
            slug = _first(t, "slug")
            if lang and slug:
                by_lang[str(lang)] = slug
    return by_lang.get("id") or by_lang.get("en") or (next(iter(by_lang.values()), None))


def normalize_translations(detail: dict) -> list[dict]:
    """Extract per-lang translation rows (title / short_description / content /
    slug) from a findById detail blob."""
    translations = _first(detail, "translatable", "translations", default=[]) or []
    out: list[dict] = []
    for t in translations:
        if not isinstance(t, dict):
            continue
        out.append({
            "lang": _first(t, "lang", "language"),
            "title": _first(t, "title"),
            "short_description": _first(t, "short_description", "shortDescription"),
            "content": _first(t, "content"),
            "slug": _first(t, "slug"),
        })
    return out
