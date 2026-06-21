"""READ tools for the Acme Web MCP server.

Web-only, read-only. Wraps the NestJS admin API via server.web_client.

Tools:
- web_get_listing(property_id)        — full record by AR##### id
- web_search_listings({...})          — server-side filtered search
- web_list_listings({status, category, page}) — bulk pull (replaces xlsx export)
- web_get_translations(property_id)   — per-lang title/desc/content/slug
- refresh_cache()                     — drop the client GET cache

NOT in this connector (deliberate — see 00_DESIGN.md §"web_diff_against_lark"):
  web_diff_against_lark is DEFERRED. Pulling Lark records would make this
  connector touch Lark, violating the web-only rule (build prompt rule #1).
  Claude orchestrates the web↔Lark diff across the two separate connectors
  instead. Revisit only if a read-only lark_client import is explicitly OK'd.
"""
from __future__ import annotations

from server import web_client
from server.logger import get_logger

_log = get_logger("read_tools")


# Tool schemas

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "web_get_listing",
        "description": (
            "Fetch a single example.com listing by its public property_id "
            "(the AR##### number, e.g. 'AR103917'). Returns the full record: "
            "status, deal_status, is_rented, price, category, unit/building "
            "attributes (building_area, bedrooms, floor, floor_zone, tower, "
            "unit), ID+EN titles, slug, media count. Resolves AR# → internal id "
            "via search, then fetches full detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {
                    "type": "string",
                    "description": "Public AR##### id, e.g. 'AR103917'. The 'AR' prefix is required.",
                },
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "web_search_listings",
        "description": (
            "Search example.com listings with server-side filters "
            "(AND-combined). Replaces JSON-LD scraping. `apartment_name`/`tower`/"
            "`unit` are matched via the admin `search` param (propertyId/address "
            "startsWith/contains) — for precise apartment/unit matching prefer "
            "web_get_listing by property_id. Returns normalized summaries."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Free-text — matches propertyId (startsWith) or address (contains).",
                },
                "apartment_name": {"type": "string", "description": "Alias for `search` (building/apartment name)."},
                "tower": {"type": "string", "description": "Tower hint (folded into search)."},
                "unit": {"type": "string", "description": "Unit hint (folded into search)."},
                "status": {"type": "string", "description": "DRAFT | PUBLISHED."},
                "deal_status": {"type": "string", "description": "AVAILABLE | SOLD | RENTED | ARCHIVED."},
                "category": {"type": "string", "description": "SELL | RENT."},
                "min_price": {"type": "number"},
                "max_price": {"type": "number"},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
                    "description": "Max rows returned. Default 20.",
                },
            },
        },
    },
    {
        "name": "web_list_listings",
        "description": (
            "Bulk-pull example.com listings by status/category with "
            "pagination. Replaces the manual listings-export xlsx. Returns "
            "normalized summaries plus pagination info."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "DRAFT | PUBLISHED."},
                "category": {"type": "string", "description": "SELL | RENT."},
                "deal_status": {"type": "string", "description": "AVAILABLE | SOLD | RENTED | ARCHIVED."},
                "page": {"type": "integer", "minimum": 1, "default": 1},
                "per_page": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
        },
    },
    {
        "name": "web_get_translations",
        "description": (
            "Get the per-language translations (title, short_description, "
            "content, slug) for a listing by property_id. Public URLs read the "
            "slug from translations, so this is the source of truth for "
            "title-vs-content and slug audits."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "property_id": {"type": "string", "description": "Public AR##### id."},
            },
            "required": ["property_id"],
        },
    },
    {
        "name": "refresh_cache",
        "description": (
            "Drop the connector's in-memory GET cache so the next read hits the "
            "admin API live. Use after a known external change (manual admin "
            "edit, WA-bot update) to avoid stale reads within the 1h TTL window."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# Handlers

async def web_get_listing(args: dict) -> dict:
    pid = (args.get("property_id") or "").strip()
    if not pid:
        return {"error": "property_id required"}
    try:
        summary = web_client.resolve_by_property_id(pid)
    except web_client.WebClientError as e:
        return {"error": f"admin API fetch failed: {str(e)[:200]}"}
    if not summary:
        return {"error": f"no listing found for property_id {pid}"}
    internal_id = summary.get("id") or summary.get("propertyId")
    try:
        detail = web_client.get_listing_detail(int(summary["id"]))
    except (web_client.WebClientError, KeyError, ValueError, TypeError) as e:
        # fall back to the search summary if detail fetch fails
        _log.warning(f"detail fetch failed for {pid}: {str(e)[:160]}")
        return {"listing": web_client.normalize_listing(summary), "detail_partial": True}
    return {"listing": web_client.normalize_listing(detail)}


async def web_search_listings(args: dict) -> dict:
    # fold apartment_name/tower/unit into the single server-side `search` param
    search = (
        args.get("search")
        or args.get("apartment_name")
        or " ".join(
            x for x in [args.get("tower"), args.get("unit")] if x
        ).strip()
        or None
    )
    limit = min(int(args.get("limit") or 20), 100)
    params: dict = {"per_page": limit}
    if search:
        params["search"] = search
    for k in ("status", "deal_status", "category"):
        if args.get(k):
            params[k] = args[k]
    if args.get("min_price") is not None:
        params["min_price"] = args["min_price"]
    if args.get("max_price") is not None:
        params["max_price"] = args["max_price"]

    try:
        resp = web_client.list_listings(params)
    except web_client.WebClientError as e:
        return {"error": f"admin API search failed: {str(e)[:200]}"}
    rows = web_client._iter_rows(resp)
    matches = [web_client.normalize_listing(r) for r in rows[:limit]]
    return {"returned": len(matches), "matches": matches}


async def web_list_listings(args: dict) -> dict:
    params: dict = {
        "page": int(args.get("page") or 1),
        "per_page": min(int(args.get("per_page") or 50), 200),
    }
    for k in ("status", "category", "deal_status"):
        if args.get(k):
            params[k] = args[k]
    try:
        resp = web_client.list_listings(params)
    except web_client.WebClientError as e:
        return {"error": f"admin API list failed: {str(e)[:200]}"}
    rows = web_client._iter_rows(resp)
    listings = [web_client.normalize_listing(r) for r in rows]
    out: dict = {
        "page": params["page"],
        "per_page": params["per_page"],
        "returned": len(listings),
        "listings": listings,
    }
    # surface pagination metadata. The ResponseInterceptor flattens Laravel
    # pagination into {data:[...], links, meta:{current_page,last_page,total,...}}
    # so `meta` is the canonical source; tolerate flat shapes too.
    if isinstance(resp, dict):
        for k in ("meta", "links", "total", "last_page", "current_page"):
            if k in resp:
                out[k] = resp[k]
    return out


async def web_get_translations(args: dict) -> dict:
    pid = (args.get("property_id") or "").strip()
    if not pid:
        return {"error": "property_id required"}
    try:
        summary = web_client.resolve_by_property_id(pid)
        if not summary:
            return {"error": f"no listing found for property_id {pid}"}
        detail = web_client.get_listing_detail(int(summary["id"]))
    except (web_client.WebClientError, KeyError, ValueError, TypeError) as e:
        return {"error": f"admin API fetch failed: {str(e)[:200]}"}
    return {
        "property_id": pid,
        "translations": web_client.normalize_translations(detail),
    }


async def refresh_cache(args: dict) -> dict:
    web_client.clear_cache()
    return {"refreshed": True}


TOOL_HANDLERS = {
    "web_get_listing": web_get_listing,
    "web_search_listings": web_search_listings,
    "web_list_listings": web_list_listings,
    "web_get_translations": web_get_translations,
    "refresh_cache": refresh_cache,
}
