"""READ tools for Lark MCP — the operator tier full access (NO field stripping).

Initial read tools:
- search_listings(area?, kt?, km?, harga_max?, status?, building?, limit=20)
- get_listing(record_id) — full record incl. owner
- find_activities(date?, agen_ou_id?, tipe?, status?, listing_id?)

Additional read tools:
- search_contacts(query, tipe?, limit=20) — fuzzy name search
- get_contact(record_id) — full contact detail
- get_lark_url(record_id, table) — direct Lark Bitable URL
- find_listings_by_owner(owner_name OR contact_id) — all listings of an owner
- get_agen_listings(agen_ou_id) — all listings handled by an agen
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from server import agen_registry, lark_client
from server.logger import get_logger

_log = get_logger("read_tools")
WIB = ZoneInfo("Asia/Jakarta")


# Tool schemas

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_listings",
        "description": (
            "Search Acme listings by criteria. Returns up to `limit` matches "
            "with full spec including owner info. Filters are AND-combined."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "area": {
                    "type": "string",
                    "description": "Area / kawasan substring match (e.g. 'Sunset District', 'Midtown'). Case-insensitive.",
                },
                "building": {
                    "type": "string",
                    "description": "Nama Properti substring match (e.g. 'Casa Grande', 'Riverside Suites').",
                },
                "kt": {
                    "type": "integer",
                    "description": "Kamar Tidur exact match (number of bedrooms).",
                },
                "km": {
                    "type": "integer",
                    "description": "Kamar Mandi exact match.",
                },
                "harga_max": {
                    "type": "number",
                    "description": "Max price filter — applied to whichever of Harga Sewa or Harga Jual is set (treat as 'OR').",
                },
                "status": {
                    "type": "string",
                    "description": "Status filter (e.g. 'Available', 'Sold', 'Tersewa'). Case-insensitive.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Max listings returned. Default 20.",
                },
            },
        },
    },
    {
        "name": "get_listing",
        "description": (
            "Fetch a single listing by record_id. Returns FULL record including "
            "Pemilik (owner) info — the operator tier access. When the user asks to "
            "MODIFY a value, after identifying the record use update_record "
            "rather than reporting a change you cannot perform."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "Lark record_id (e.g. 'recvgWbXXX').",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "find_activities",
        "description": (
            "Find Activities (showings, meetings, signings, etc.) by date / agen / "
            "tipe / status / listing. All filters optional and AND-combined. "
            "Date format: YYYY-MM-DD or 'today' / 'besok' / 'kemarin'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date filter: 'YYYY-MM-DD', 'today', 'besok', 'kemarin'. Matches Waktu Mulai date.",
                },
                "agen_ou_id": {
                    "type": "string",
                    "description": "Filter by Agen ou_id.",
                },
                "tipe": {
                    "type": "string",
                    "description": "Activity tipe (e.g. 'Showing', 'Meeting', 'Signing'). Case-insensitive.",
                },
                "status": {
                    "type": "string",
                    "description": "Activity status (e.g. 'Dijadwalkan', 'Selesai', 'Tidak Dilakukan').",
                },
                "listing_id": {
                    "type": "string",
                    "description": "Filter activities linked to this listing record_id.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "maximum": 100,
                },
            },
        },
    },
]


# Handlers

def _resolve_date(date_str: str) -> str:
    """Convert 'today' / 'besok' / 'kemarin' / 'YYYY-MM-DD' → ISO date string."""
    if not date_str:
        return ""
    s = date_str.strip().lower()
    today = datetime.now(WIB).date()
    if s in {"today", "hari ini"}:
        return today.isoformat()
    if s in {"besok", "tomorrow"}:
        return (today + timedelta(days=1)).isoformat()
    if s in {"kemarin", "yesterday"}:
        return (today - timedelta(days=1)).isoformat()
    return s


def _epoch_ms_to_date(ms) -> str:
    """Convert epoch ms → YYYY-MM-DD string in WIB."""
    if not isinstance(ms, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=WIB).date().isoformat()
    except (OSError, ValueError):
        return ""


def _as_number(v):
    """Coerce a value to float for numeric comparison, or None if not numeric.

    GAP-5: Lark Number fields (Harga Sewa/Jual, Luas, ...) often come back as
    STRINGS ('59200000'). Numeric/range filters must coerce them or every
    price comparison silently matches nothing. Booleans are NOT numbers here.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace(" ", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


async def search_listings(args: dict) -> dict:
    """Filter listings by criteria. Returns {matches: [...], total_lark: N, returned: N}."""
    area = (args.get("area") or "").strip().lower()
    building = (args.get("building") or "").strip().lower()
    kt = args.get("kt")
    km = args.get("km")
    harga_max = args.get("harga_max")
    status = (args.get("status") or "").strip().lower()
    limit = min(int(args.get("limit") or 20), 100)

    all_listings = lark_client.fetch_all_listings()
    matches: list[dict] = []
    for raw in all_listings:
        norm = lark_client.normalize_listing(raw)
        if area and area not in (norm["area"] or "").lower():
            continue
        if building and building not in (norm["nama_properti"] or "").lower():
            continue
        if kt is not None:
            # KT may be int or "2+1" study room — coerce int compare
            try:
                if int(str(norm["kt"]).split("+")[0]) != int(kt):
                    continue
            except (ValueError, TypeError):
                continue
        if km is not None:
            try:
                if int(str(norm["km"]).split("+")[0]) != int(km):
                    continue
            except (ValueError, TypeError):
                continue
        if status and status not in (norm["status"] or "").lower():
            continue
        if harga_max is not None:
            # GAP-5: Lark stores Harga as strings → coerce before comparing.
            sewa = _as_number(norm.get("harga_sewa")) or 0
            jual = _as_number(norm.get("harga_jual")) or 0
            # OR semantics: match if EITHER price is under cap (sewa is monthly,
            # jual is total — caller should be specific via 'building' too)
            if sewa and sewa > harga_max and (not jual or jual > harga_max):
                continue
            if jual and jual > harga_max and not sewa:
                continue
        # Drop _raw_fields from output (verbose)
        out = {k: v for k, v in norm.items() if k != "_raw_fields"}
        matches.append(out)
        if len(matches) >= limit:
            break

    return {
        "total_lark": len(all_listings),
        "returned": len(matches),
        "matches": matches,
    }


async def get_listing(args: dict) -> dict:
    """Fetch single listing by record_id. Full owner-tier access.

    Enriches output with:
    - pemilik_resolved: list of {contact_id, nama, no_hp} — eager-resolves
      the Pemilik link so the operator sees actual owner name + HP, not opaque IDs.
    - agen_names: list of agen display names for Agen ou_ids.
    """
    rid = (args.get("record_id") or "").strip()
    if not rid:
        return {"error": "record_id required"}
    try:
        raw = lark_client.get_record(lark_client.TABLE_LISTINGS, rid)
    except lark_client.LarkClientError as e:
        return {"error": f"Lark fetch failed: {str(e)[:200]}"}
    norm = lark_client.normalize_listing(raw)
    out = {k: v for k, v in norm.items() if k != "_raw_fields"}

    # Enrich pemilik link → resolved contact info (the operator sees full)
    pemilik_ids = _extract_link_ids(out.get("pemilik"))
    pemilik_resolved: list[dict] = []
    for cid in pemilik_ids:
        contact_raw = lark_client.get_contact(cid)
        if contact_raw:
            cnorm = lark_client.normalize_contact(contact_raw)
            pemilik_resolved.append({
                "contact_id": cid,
                "nama": cnorm.get("nama"),
                "no_hp": cnorm.get("no_hp"),
                "tipe_contact": cnorm.get("tipe_contact"),
            })
        else:
            pemilik_resolved.append({"contact_id": cid, "error": "contact fetch failed"})
    out["pemilik_resolved"] = pemilik_resolved

    # Enrich agen → display names
    agen_field = out.get("agen") or []
    if not isinstance(agen_field, list):
        agen_field = [agen_field]
    ou_ids = [
        (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
        for a in agen_field
    ]
    ou_ids = [o for o in ou_ids if o]
    out["agen_names"] = agen_registry.resolve_ou_ids_to_names(ou_ids)

    return out


async def find_activities(args: dict) -> dict:
    """Filter activities by date / agen / tipe / status / listing."""
    date_filter = _resolve_date(args.get("date") or "")
    agen_ou_id = (args.get("agen_ou_id") or "").strip()
    tipe = (args.get("tipe") or "").strip().lower()
    status = (args.get("status") or "").strip().lower()
    listing_id = (args.get("listing_id") or "").strip()
    limit = min(int(args.get("limit") or 20), 100)

    all_acts = lark_client.fetch_all_activities()
    matches: list[dict] = []
    for raw in all_acts:
        norm = lark_client.normalize_activity(raw)
        # Date filter
        if date_filter:
            act_date = _epoch_ms_to_date(norm.get("waktu_mulai"))
            if act_date != date_filter:
                continue
        # Agen filter
        if agen_ou_id:
            agen_field = norm.get("agen") or []
            if not isinstance(agen_field, list):
                agen_field = [agen_field]
            ou_ids = [
                (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
                for a in agen_field
            ]
            if agen_ou_id not in ou_ids:
                continue
        # Tipe filter
        if tipe and tipe not in (norm.get("tipe") or "").lower():
            continue
        # Status filter
        if status and status not in (norm.get("status") or "").lower():
            continue
        # Listing filter
        if listing_id:
            listings_field = norm.get("listings") or []
            if not isinstance(listings_field, list):
                listings_field = [listings_field]
            ids = []
            for item in listings_field:
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict):
                    rid = (
                        item.get("record_id")
                        or item.get("id")
                        or item.get("link_record_id")
                    )
                    if rid:
                        ids.append(rid)
            if listing_id not in ids:
                continue

        out = {k: v for k, v in norm.items() if k != "_raw_fields"}
        # enrich agen opaque ou_ids → human-readable names
        agen_field = out.get("agen") or []
        if not isinstance(agen_field, list):
            agen_field = [agen_field]
        out_ou_ids = [
            (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
            for a in agen_field
        ]
        out_ou_ids = [o for o in out_ou_ids if o]
        out["agen_names"] = agen_registry.resolve_ou_ids_to_names(out_ou_ids)
        matches.append(out)
        if len(matches) >= limit:
            break

    return {
        "total_lark": len(all_acts),
        "returned": len(matches),
        "matches": matches,
    }


# additional read tool schemas

TOOL_SCHEMAS.extend([
    {
        "name": "search_contacts",
        "description": (
            "Fuzzy search Acme contacts (Owner / Buyer / Tenant / "
            "Agen Eksternal / TRO). Full access — returns Nama + No HP + Tipe. "
            "Use this to find people; chain with find_listings_by_owner for their listings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring match on Nama (case-insensitive). e.g. 'Susanto', 'Sari Rahmawati'.",
                },
                "tipe": {
                    "type": "string",
                    "description": "Optional filter on Tipe Contact (e.g. 'Owner', 'Buyer'). Case-insensitive substring.",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "maximum": 100,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_contact",
        "description": (
            "Fetch single contact by record_id. Full detail including HP, "
            "Sumber Klien, history chat summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "Lark contact record_id (e.g. 'recvgWbXXX').",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "get_lark_url",
        "description": (
            "Generate direct Lark Bitable URL for a record so the operator can click "
            "and open in browser/app."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "Lark record_id.",
                },
                "table": {
                    "type": "string",
                    "enum": ["listings", "contacts", "activities"],
                    "default": "listings",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "find_listings_by_owner",
        "description": (
            "Find all listings owned by a specific contact. Either pass "
            "contact_id (precise) or owner_name (fuzzy — auto-resolves to "
            "first matching contact)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Exact contact record_id (precise lookup).",
                },
                "owner_name": {
                    "type": "string",
                    "description": "Fuzzy owner name. Auto-resolves to first Owner contact matching. Use when contact_id unknown.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "maximum": 200,
                },
            },
        },
    },
    {
        "name": "get_agen_listings",
        "description": (
            "Find all listings assigned to a specific agen (via Agen field). "
            "agen_ou_id is the Lark User open_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agen_ou_id": {
                    "type": "string",
                    "description": "Lark User open_id of the agen (e.g. 'ou_5555555...').",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter (e.g. 'Available').",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "maximum": 200,
                },
            },
            "required": ["agen_ou_id"],
        },
    },
])


# additional read tool handlers

def _extract_link_ids(value) -> list[str]:
    """Extract record_ids from a Lark DuplexLink field value (str / list / dict)."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        ids: list[str] = []
        for item in value:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                rid = item.get("record_id") or item.get("id") or item.get("link_record_id")
                if rid:
                    ids.append(rid)
        return ids
    if isinstance(value, dict):
        return list(value.get("link_record_ids") or [])
    return []


async def search_contacts(args: dict) -> dict:
    """Fuzzy search contacts by Nama, optionally filter by Tipe Contact."""
    query = (args.get("query") or "").strip().lower()
    if not query:
        return {"error": "query required"}
    tipe_filter = (args.get("tipe") or "").strip().lower()
    limit = min(int(args.get("limit") or 20), 100)

    contacts_raw = lark_client.fetch_all_contacts()
    matches: list[dict] = []
    for raw in contacts_raw:
        norm = lark_client.normalize_contact(raw)
        nama = (norm.get("nama") or "").lower()
        if query not in nama:
            continue
        if tipe_filter:
            tipes_lower = [str(t).lower() for t in (norm.get("tipe_contact") or [])]
            if not any(tipe_filter in t for t in tipes_lower):
                continue
        out = {k: v for k, v in norm.items() if k != "_raw_fields"}
        matches.append(out)
        if len(matches) >= limit:
            break

    return {
        "total_lark": len(contacts_raw),
        "returned": len(matches),
        "matches": matches,
    }


async def get_contact(args: dict) -> dict:
    """Fetch single contact by record_id."""
    rid = (args.get("record_id") or "").strip()
    if not rid:
        return {"error": "record_id required"}
    raw = lark_client.get_contact(rid)
    if not raw:
        return {"error": f"contact {rid} not found or fetch failed"}
    norm = lark_client.normalize_contact(raw)
    return {k: v for k, v in norm.items() if k != "_raw_fields"}


async def get_lark_url(args: dict) -> dict:
    """Generate Lark Bitable URL for a record."""
    rid = (args.get("record_id") or "").strip()
    if not rid:
        return {"error": "record_id required"}
    table = (args.get("table") or "listings").strip().lower()
    url = lark_client.get_lark_url(rid, table)
    return {"url": url, "record_id": rid, "table": table}


async def find_listings_by_owner(args: dict) -> dict:
    """Find all listings whose Pemilik link contains the target contact.

    Resolves owner_name → contact_id (first match) if contact_id not provided.
    """
    contact_id = (args.get("contact_id") or "").strip()
    owner_name = (args.get("owner_name") or "").strip()
    limit = min(int(args.get("limit") or 50), 200)

    # Resolve owner_name → contact_id if needed
    if not contact_id and owner_name:
        search_res = await search_contacts({
            "query": owner_name,
            "tipe": "Owner",
            "limit": 5,
        })
        candidates = search_res.get("matches") or []
        if not candidates:
            return {
                "error": f"no Owner contact matching '{owner_name}'",
                "hint": "try search_contacts to find the right name first",
            }
        contact_id = candidates[0]["record_id"]
        resolved_from_name = candidates[0].get("nama")
    else:
        resolved_from_name = None

    if not contact_id:
        return {"error": "either contact_id or owner_name required"}

    # Scan all listings, filter where Pemilik link contains contact_id
    all_listings = lark_client.fetch_all_listings()
    matches: list[dict] = []
    for raw in all_listings:
        norm = lark_client.normalize_listing(raw)
        owner_ids = _extract_link_ids(norm.get("pemilik"))
        if contact_id not in owner_ids:
            continue
        out = {k: v for k, v in norm.items() if k != "_raw_fields"}
        matches.append(out)
        if len(matches) >= limit:
            break

    result = {
        "contact_id": contact_id,
        "total_lark": len(all_listings),
        "returned": len(matches),
        "matches": matches,
    }
    if resolved_from_name:
        result["resolved_from_owner_name"] = resolved_from_name
    return result


async def get_agen_listings(args: dict) -> dict:
    """Find all listings where Agen contains the target ou_id."""
    agen_ou_id = (args.get("agen_ou_id") or "").strip()
    if not agen_ou_id:
        return {"error": "agen_ou_id required"}
    status_filter = (args.get("status") or "").strip().lower()
    limit = min(int(args.get("limit") or 50), 200)

    all_listings = lark_client.fetch_all_listings()
    matches: list[dict] = []
    for raw in all_listings:
        norm = lark_client.normalize_listing(raw)
        agen_field = norm.get("agen") or []
        if not isinstance(agen_field, list):
            agen_field = [agen_field]
        ou_ids = [
            (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
            for a in agen_field
        ]
        if agen_ou_id not in ou_ids:
            continue
        if status_filter and status_filter not in (norm.get("status") or "").lower():
            continue
        out = {k: v for k, v in norm.items() if k != "_raw_fields"}
        matches.append(out)
        if len(matches) >= limit:
            break

    return {
        "agen_ou_id": agen_ou_id,
        "total_lark": len(all_listings),
        "returned": len(matches),
        "matches": matches,
    }


# agen-resolution tool schemas

TOOL_SCHEMAS.extend([
    {
        "name": "list_agen",
        "description": (
            "List all Acme agen with ou_id ↔ display name mapping. "
            "Use this when the operator wants to know who works at Acme, or before "
            "calling get_agen_listings (which needs ou_id)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_vip": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include Budi (VIP, separate workflow). Default True.",
                },
            },
        },
    },
    {
        "name": "resolve_agen",
        "description": (
            "Resolve an agen by name (fuzzy) OR ou_id (exact). Returns the "
            "agen's full info {nama_display, ou_id, phone_normalized, email}. "
            "Use this to convert 'Rina' → ou_2222222... before calling tools that need ou_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Agen name (e.g. 'Rina', 'Maya', 'Putri') OR ou_id (starts with 'ou_').",
                },
            },
            "required": ["query"],
        },
    },
])


async def list_agen(args: dict) -> dict:
    """Return all 6 Acme agen with ou_id ↔ name mapping."""
    include_vip = args.get("include_vip", True)
    agen = agen_registry.all_agen()
    if not include_vip:
        agen = [a for a in agen if a.get("role") != "vip"]
    return {"total": len(agen), "agen": agen}


async def resolve_agen(args: dict) -> dict:
    """Resolve query (name OR ou_id) → agen info."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query required (agen name or ou_id)"}
    info = agen_registry.resolve(query)
    if not info:
        return {
            "error": f"no agen matching '{query}'",
            "hint": "call list_agen() to see all available agen",
        }
    return {k: v for k, v in info.items() if k != "aliases"}


# count_listings — cheap aggregation

TOOL_SCHEMAS.append({
    "name": "count_listings",
    "description": (
        "Count listings matching filters. Optional group_by for breakdown "
        "(e.g. group_by='area' to see distribution across areas). MUCH cheaper "
        "than search_listings when the operator only wants a number / breakdown, "
        "not the actual listings (no record details returned)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "area": {"type": "string", "description": "Optional area filter."},
            "building": {"type": "string", "description": "Optional Nama Properti substring."},
            "kt": {"type": "integer", "description": "Optional KT exact match."},
            "km": {"type": "integer", "description": "Optional KM exact match."},
            "harga_max": {"type": "number"},
            "status": {"type": "string", "description": "Optional status filter."},
            "agen_ou_id": {"type": "string", "description": "Optional Agen filter (one ou_id)."},
            "group_by": {
                "type": "string",
                "enum": [
                    "area", "status", "kt", "tipe_listing", "tipe_properti",
                    "kondisi", "agen", "nama_properti",
                ],
                "description": (
                    "Optional grouping dimension. Returns breakdown count per group, "
                    "sorted desc. Without group_by, returns single total. "
                    "'agen' groups each listing into EVERY agen it's assigned to "
                    "(multi-agen listing counted in each agen's bucket)."
                ),
            },
        },
    },
})


def _listing_matches_filters(norm: dict, f: dict) -> bool:
    """Apply filter dict to normalized listing. Returns True if all filters pass."""
    if f.get("area") and f["area"].lower() not in (norm.get("area") or "").lower():
        return False
    if f.get("building") and f["building"].lower() not in (norm.get("nama_properti") or "").lower():
        return False
    if f.get("kt") is not None:
        try:
            if int(str(norm.get("kt")).split("+")[0]) != int(f["kt"]):
                return False
        except (ValueError, TypeError):
            return False
    if f.get("km") is not None:
        try:
            if int(str(norm.get("km")).split("+")[0]) != int(f["km"]):
                return False
        except (ValueError, TypeError):
            return False
    if f.get("status") and f["status"].lower() not in (norm.get("status") or "").lower():
        return False
    if f.get("harga_max") is not None:
        # GAP-5: Harga fields may be strings in Lark — coerce before comparing.
        sewa = _as_number(norm.get("harga_sewa")) or 0
        jual = _as_number(norm.get("harga_jual")) or 0
        if sewa and sewa > f["harga_max"] and (not jual or jual > f["harga_max"]):
            return False
        if jual and jual > f["harga_max"] and not sewa:
            return False
    if f.get("agen_ou_id"):
        agen_field = norm.get("agen") or []
        if not isinstance(agen_field, list):
            agen_field = [agen_field]
        ou_ids = [
            (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
            for a in agen_field
        ]
        if f["agen_ou_id"] not in ou_ids:
            return False
    return True


def _group_keys(norm: dict, group_by: str) -> list[str]:
    """Compute group keys for a listing. Returns LIST because some dimensions
    (e.g. 'agen') can map one listing to multiple keys (multi-agen listing
    counted in each agen's bucket).
    """
    if group_by == "area":
        return [norm.get("area") or "(no area)"]
    if group_by == "status":
        return [norm.get("status") or "(no status)"]
    if group_by == "kt":
        v = norm.get("kt")
        return [f"KT={v}" if v is not None else "(no KT)"]
    if group_by == "tipe_listing":
        return [norm.get("tipe_listing") or "(no tipe_listing)"]
    if group_by == "tipe_properti":
        return [norm.get("tipe_properti") or "(no tipe_properti)"]
    if group_by == "kondisi":
        return [norm.get("kondisi") or "(no kondisi)"]
    if group_by == "nama_properti":
        return [norm.get("nama_properti") or "(no name)"]
    if group_by == "agen":
        agen_field = norm.get("agen") or []
        if not isinstance(agen_field, list):
            agen_field = [agen_field]
        ou_ids = [
            (a.get("id") or a.get("ou_id")) if isinstance(a, dict) else a
            for a in agen_field
        ]
        ou_ids = [o for o in ou_ids if o]
        if not ou_ids:
            return ["(no agen)"]
        # Multi-agen: return ALL names — listing counted once per agen
        # (fix per CC review #8: previously joined as "Rina, Sari" single bucket)
        return agen_registry.resolve_ou_ids_to_names(ou_ids)
    return ["(unknown group_by)"]


async def count_listings(args: dict) -> dict:
    """Count listings matching filters. Optional group_by → breakdown.

    Returns:
        Without group_by: {total, total_lark, filters_applied}
        With group_by:    {total, total_lark, filters_applied, breakdown: [{key, count}, ...]}
    """
    filters = {
        "area": args.get("area"),
        "building": args.get("building"),
        "kt": args.get("kt"),
        "km": args.get("km"),
        "harga_max": args.get("harga_max"),
        "status": args.get("status"),
        "agen_ou_id": args.get("agen_ou_id"),
    }
    group_by = (args.get("group_by") or "").strip().lower()

    all_listings = lark_client.fetch_all_listings()
    total_matched = 0
    breakdown: dict[str, int] = {}

    for raw in all_listings:
        norm = lark_client.normalize_listing(raw)
        if not _listing_matches_filters(norm, filters):
            continue
        total_matched += 1
        if group_by:
            # _group_keys returns LIST — multi-agen listings increment all buckets
            for key in _group_keys(norm, group_by):
                breakdown[key] = breakdown.get(key, 0) + 1

    out: dict = {
        "total": total_matched,
        "total_lark": len(all_listings),
        "filters_applied": {k: v for k, v in filters.items() if v is not None},
    }
    if group_by:
        # Sorted desc by count
        sorted_breakdown = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
        out["group_by"] = group_by
        out["breakdown"] = [{"key": k, "count": v} for k, v in sorted_breakdown]
    return out


# Handler registry

TOOL_HANDLERS = {
    # initial read tools
    "search_listings": search_listings,
    "get_listing": get_listing,
    "find_activities": find_activities,
    # additional read tools
    "search_contacts": search_contacts,
    "get_contact": get_contact,
    "get_lark_url": get_lark_url,
    "find_listings_by_owner": find_listings_by_owner,
    "get_agen_listings": get_agen_listings,
    # agen resolution
    "list_agen": list_agen,
    "resolve_agen": resolve_agen,
    # cheap aggregation
    "count_listings": count_listings,
}


# refresh_cache (manual warm)

TOOL_SCHEMAS.append({
    "name": "refresh_cache",
    "description": (
        "Force-refresh server-side Lark cache. Use when you suspect data "
        "is stale (e.g. after a known Lark edit). Without this, caches "
        "auto-expire (listings/contacts: 1h, activities: 10min). "
        "Returns counts of refreshed records."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "enum": ["all", "listings", "contacts", "activities"],
                "default": "all",
                "description": "Which cache to refresh. 'all' refreshes everything.",
            },
        },
    },
})


async def refresh_cache(args: dict) -> dict:
    """Force-refresh Lark caches. Returns count per table."""
    table = (args.get("table") or "all").strip().lower()
    result: dict = {"refreshed": table, "counts": {}}

    if table in ("all", "listings"):
        try:
            items = lark_client.fetch_all_listings(force_refresh=True)
            result["counts"]["listings"] = len(items)
        except Exception as e:
            result["counts"]["listings"] = f"error: {str(e)[:100]}"

    if table in ("all", "contacts"):
        try:
            items = lark_client.fetch_all_contacts(force_refresh=True)
            result["counts"]["contacts"] = len(items)
        except Exception as e:
            result["counts"]["contacts"] = f"error: {str(e)[:100]}"

    if table in ("all", "activities"):
        try:
            items = lark_client.fetch_all_activities(force_refresh=True)
            result["counts"]["activities"] = len(items)
        except Exception as e:
            result["counts"]["activities"] = f"error: {str(e)[:100]}"

    return result


TOOL_HANDLERS["refresh_cache"] = refresh_cache


# Generic Lark Query (Contract v1.0)
#
# Three tools so the AI can reach ANY field/table, not just the curated 12:
#   list_tables()      — what tables exist
#   describe_table()   — what fields a table has (+ samples + PII flag)
#   query_records()    — generic filter/search/sort over any table
#
# Filters are evaluated CLIENT-SIDE against fetched records (the three big
# tables are served from the warm cache; other tables are tiny/empty). A Lark
# formula string is still constructed — but only for the audit/debug log, not
# pushed to Lark. Rationale (flagged in the generic-query brief): client-side gives an exact
# matched_count + correct text_search/sort, and avoids Lark formula
# field-name escaping hazards (e.g. "Area/Kawasan", "Harga Jual per m²").

_FILTER_OPS: frozenset[str] = frozenset({
    "gt", "gte", "lt", "lte", "contains", "starts_with", "between",
})


def _coerce_between_bounds(value) -> tuple[float, float]:
    """Validate a `between` value → (lo, hi). Raises ValueError on a non
    2-element list or reversed bounds (lo must be <= hi)."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("between requires value [lo, hi] (a 2-element list)")
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        raise ValueError("between bounds must be numbers (or epoch ms for dates)")
    if lo > hi:
        raise ValueError(f"between bounds reversed: lo ({value[0]}) > hi ({value[1]})")
    return lo, hi


def _select_norm(s) -> str:
    """Normalize a select-ish string for matching: strip leading non-word
    chars (emoji/symbols, e.g. '🔴 Kritis' → 'kritis') + lowercase."""
    return re.sub(r"^\W+", "", str(s)).strip().lower()


def _scalars(value) -> list:
    """Flatten any Lark field value into comparable scalar candidates.

    Handles str/num/bool, SingleSelect/MultiSelect dicts ({text|name}),
    Person dicts ({id|name|en_name}), DuplexLink dicts ({record_ids,text}),
    and arbitrarily nested lists.
    """
    out: list = []

    def add(v):
        if v is None:
            return
        if isinstance(v, bool):
            out.append(v)
        elif isinstance(v, (int, float)):
            out.append(v)
        elif isinstance(v, str):
            if v.strip():
                out.append(v)
        elif isinstance(v, dict):
            for key in ("text", "name", "en_name", "value"):
                if v.get(key):
                    add(v[key])
            for key in ("record_ids", "link_record_ids"):
                for rid in (v.get(key) or []):
                    add(rid)
        elif isinstance(v, list):
            for item in v:
                add(item)

    add(value)
    return out


def _exact_match(field_val, target) -> bool:
    """Exact-match a Lark field value against a scalar target.

    Strings: case-insensitive, with leading-symbol normalization so a plain
    'Kritis' matches a stored '🔴 Kritis'. Numbers/bools: strict.
    """
    cands = _scalars(field_val)
    if isinstance(target, bool):
        return any(isinstance(c, bool) and c is target for c in cands)
    if isinstance(target, (int, float)):
        return any(
            isinstance(c, (int, float)) and not isinstance(c, bool)
            and float(c) == float(target)
            for c in cands
        )
    tlow = str(target).lower()
    tnorm = _select_norm(target)
    for c in cands:
        cs = str(c)
        if cs.lower() == tlow or _select_norm(cs) == tnorm:
            return True
    return False


def _cmp_op(field_val, op: str, target) -> bool:
    """Apply a predicate op to a Lark field value."""
    cands = _scalars(field_val)
    # GAP-5: Lark Number fields can be strings ('59200000') — coerce candidates.
    nums = [n for n in (_as_number(c) for c in cands) if n is not None]
    if op in {"gt", "gte", "lt", "lte"}:
        t = _as_number(target)
        if t is None:
            return False
        for n in nums:
            if op == "gt" and n > t:
                return True
            if op == "gte" and n >= t:
                return True
            if op == "lt" and n < t:
                return True
            if op == "lte" and n <= t:
                return True
        return False
    if op == "between":
        # value=[lo, hi], inclusive low / exclusive high (lo <= n < hi).
        # Validation raises ValueError (reversed/non-2-list) → caller surfaces it.
        lo, hi = _coerce_between_bounds(target)
        return any(lo <= n < hi for n in nums)
    if op == "contains":
        ts = str(target).lower()
        return any(ts in str(c).lower() for c in cands)
    if op == "starts_with":
        ts = str(target).lower()
        return any(str(c).lower().startswith(ts) for c in cands)
    return False


def _record_matches(fields: dict, field_filters: dict) -> bool:
    """Client-side evaluation of field_filters against one record's fields.

    Raises ValueError on an unsupported op (caller returns graceful error).
    """
    for fname, cond in field_filters.items():
        fval = fields.get(fname)
        if isinstance(cond, dict) and "op" in cond:
            op = cond.get("op")
            if op not in _FILTER_OPS:
                raise ValueError(f"unsupported op '{op}'")
            if not _cmp_op(fval, op, cond.get("value")):
                return False
        elif isinstance(cond, list):
            if not any(_exact_match(fval, v) for v in cond):
                return False
        else:
            if not _exact_match(fval, cond):
                return False
    return True


def _text_search_match(fields: dict, q: str) -> bool:
    """Case-insensitive substring match across all string scalars in a record."""
    ql = q.lower()
    for v in fields.values():
        for c in _scalars(v):
            if isinstance(c, str) and ql in c.lower():
                return True
    return False


def _sort_key(field_val):
    """Sort key that never raises on mixed types: numbers before strings
    before missing."""
    cands = _scalars(field_val)
    if not cands:
        return (2, "")
    c = cands[0]
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        return (0, float(c))
    return (1, str(c).lower())


# GAP-4: heavy fields (full chat transcripts, long notes) bloat query_records
# output → context blowout / client truncation. On return_fields="*" we drop
# these by name pattern and list them under _excluded_heavy_fields so the AI
# can opt back in via an explicit return_fields list. Every returned string
# value is also capped (see _VALUE_CAP).
_HEAVY_FIELD_PATTERN = re.compile(
    r"(?i)(history.*chat|chat.*history|transcript|notes|ringkasan.*situasi|raw_message)"
)
_HEAVY_FIELD_ALLOWLIST: frozenset[str] = frozenset()  # curate false-positives here
_VALUE_CAP = 2000


def _is_heavy_field(name: str) -> bool:
    if not name or name in _HEAVY_FIELD_ALLOWLIST:
        return False
    return bool(_HEAVY_FIELD_PATTERN.search(name))


def _cap_value(v):
    """Cap a single field value's string size to keep payloads sane."""
    if isinstance(v, str) and len(v) > _VALUE_CAP:
        return v[:_VALUE_CAP] + "…[truncated]"
    return v


def _project(rec: dict, return_fields) -> dict:
    """Project a raw record to {record_id, fields} honoring return_fields.

    `*` (default) drops heavy-pattern fields; an explicit list opts back into
    any field (including heavy ones). All string values are capped at
    _VALUE_CAP chars either way.
    """
    fields = rec.get("fields") or {}
    if isinstance(return_fields, list) and return_fields:
        out_fields = {k: _cap_value(fields.get(k)) for k in return_fields if k in fields}
    else:  # "*" / None / [] → all fields minus heavy ones
        out_fields = {
            k: _cap_value(v) for k, v in fields.items() if not _is_heavy_field(k)
        }
    return {"record_id": rec.get("record_id"), "fields": out_fields}


def _fmt_formula_val(v) -> str:
    """Format a value for the debug Lark formula string."""
    if isinstance(v, bool):
        return "TRUE()" if v else "FALSE()"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'


def _build_formula(field_filters: dict) -> str:
    """Construct a Lark Bitable filter formula STRING (debug/audit only —
    not executed; filtering happens client-side). Raises ValueError on an
    unsupported op so query_records can report it gracefully."""
    clauses: list[str] = []
    op_map = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for fname, cond in field_filters.items():
        ref = f"CurrentValue.[{fname}]"
        if isinstance(cond, dict) and "op" in cond:
            op = cond.get("op")
            if op not in _FILTER_OPS:
                raise ValueError(f"unsupported op '{op}'")
            val = cond.get("value")
            if op in op_map:
                clauses.append(f"{ref} {op_map[op]} {_fmt_formula_val(val)}")
            elif op == "between":
                lo, hi = _coerce_between_bounds(val)  # raises on reversed/bad
                clauses.append(f"AND({ref} >= {lo}, {ref} < {hi})")
            elif op == "contains":
                clauses.append(f'FIND("{val}", {ref}) > 0')
            elif op == "starts_with":
                clauses.append(f'LEFT({ref}, {len(str(val))}) = {_fmt_formula_val(val)}')
        elif isinstance(cond, list):
            ors = ", ".join(f"{ref} = {_fmt_formula_val(v)}" for v in cond)
            clauses.append(f"OR({ors})")
        else:
            clauses.append(f"{ref} = {_fmt_formula_val(cond)}")
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    return "AND(" + ", ".join(clauses) + ")"


# Schema introspection tools

TOOL_SCHEMAS.extend([
    {
        "name": "list_tables",
        "description": (
            "List all tables in the Acme Operations Lark base with record counts. "
            "Use this to discover what data exists beyond the curated listing/"
            "contact/activity tools — e.g. Transactions, Commissions, KPI Agen. "
            "Pair with describe_table to see a table's fields, then query_records."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": (
            "Describe a table's fields: name, type, 3 sample values, and a PII "
            "flag. When a user question references ANY field name you don't "
            "recognize in your current tool schemas, ALWAYS call describe_table "
            "FIRST before assuming the field doesn't exist — do NOT reply "
            "'field not available'. Common example: 'Priority Tier', 'Deal Stage', "
            "'Owner Notes' are custom fields in Contacts. Call "
            "describe_table('Contacts'), find the field (note its exact sample "
            "values), then query_records."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Exact table name from list_tables (e.g. 'Contacts', 'Listings').",
                },
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "query_records",
        "description": (
            "Generic query over ANY table by field filters / text search / sort. "
            "Use when the curated tools can't express the query (e.g. filtering "
            "on a custom field like 'Priority Tier'). If the user references a field "
            "you don't recognize, call describe_table FIRST to get exact field "
            "names + sample values — never assume a field is unavailable. "
            "MATCHING: scalar filters are exact (case- and emoji-insensitive). "
            "For text / single_select fields whose values are compound, use "
            "{op: contains} instead of exact — e.g. Area/Kawasan is stored like "
            "'Riverside - Metro South' and Tipe Listing as 'Jual & Sewa', so "
            "{\"Area/Kawasan\": {\"op\":\"contains\",\"value\":\"Midtown\"}} matches but "
            "{\"Area/Kawasan\": \"Midtown\"} returns nothing. Exact stays correct for "
            "canonical values like {\"Status\": \"Available\"}. "
            "RANGES/DATES: use {op: between, value: [lo, hi]} (lo<=v<hi). Dates "
            "are epoch milliseconds — compute the month/quarter bounds yourself, "
            "e.g. May 2026 = {\"op\":\"between\",\"value\":[1777647600000,1780239600000]}. "
            "By default heavy fields (chat history, long notes) are omitted and "
            "listed in _excluded_heavy_fields; request them explicitly via "
            "return_fields if needed. Values are capped at 2000 chars. "
            "When the user asks to MODIFY a value, after identifying the record "
            "use update_record rather than reporting a change you cannot perform."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Table to query (exact name from list_tables).",
                },
                "field_filters": {
                    "type": "object",
                    "description": (
                        "Filters keyed by field name. Value forms: scalar (exact, "
                        "case/emoji-insensitive), list (any-of OR), or "
                        '{"op": "gt|gte|lt|lte|contains|starts_with|between", "value": ...}. '
                        "Use 'contains' for compound text/single_select values "
                        "(area, tipe). Use 'between' with [lo, hi] for number/date "
                        'ranges (lo<=v<hi). Example: {"Priority Tier": "Kritis", '
                        '"Agen": "Maya", "Harga Sewa": {"op":"between","value":[0,50000000]}}.'
                    ),
                },
                "text_search": {
                    "type": "string",
                    "description": "Fuzzy case-insensitive substring across all text fields. AND-combined with field_filters.",
                },
                "return_fields": {
                    "description": (
                        "List of field names to include, or '*' for all (default). "
                        "'*' omits heavy fields (chat history / long notes) — they're "
                        "listed in _excluded_heavy_fields; pass them explicitly here to opt back in."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "maximum": 500,
                    "description": "Max records returned. matched_count reflects the true total.",
                },
                "sort_by": {
                    "type": "string",
                    "description": "Field name to sort by; prefix with '-' for descending.",
                },
            },
            "required": ["table_name"],
        },
    },
])


async def list_tables(args: dict) -> dict:
    """List all tables with record counts + last schema sync time."""
    try:
        tables = lark_client.get_tables()
    except lark_client.LarkClientError as e:
        return {"error": f"Lark schema fetch failed: {str(e)[:200]}"}
    synced = lark_client.schema_synced_at_iso()
    out = [
        {
            "table_name": t.get("name"),
            "description": t.get("description", "") or "",
            "record_count": t.get("record_count", -1),
            "last_synced_at": synced,
        }
        for t in tables
    ]
    return {"total": len(out), "tables": out}


async def describe_table(args: dict) -> dict:
    """Describe a table's fields with sample values + PII flags."""
    table_name = (args.get("table_name") or "").strip()
    if not table_name:
        return {"error": "table_name required"}
    try:
        table_id = lark_client.resolve_table_name(table_name)
    except lark_client.LarkClientError as e:
        return {"error": f"Lark schema fetch failed: {str(e)[:200]}"}
    if not table_id:
        return {
            "error": f"unknown table '{table_name}'",
            "hint": "call list_tables to see available table names",
        }

    field_defs = lark_client.get_table_fields(table_id)
    try:
        records = lark_client.get_all_records(table_id)
    except lark_client.LarkClientError:
        records = []
    sample_pool = records if len(records) <= 500 else random.sample(records, 500)

    fields_out: list[dict] = []
    for fd in field_defs:
        fname = fd.get("field_name")
        ftype = lark_client.FIELD_TYPE_NAMES.get(fd.get("type"), f"type_{fd.get('type')}")
        raw_desc = fd.get("description")
        if isinstance(raw_desc, dict):
            desc = raw_desc.get("text") or ""
        else:
            desc = raw_desc or ""
        samples: list = []
        for rec in sample_pool:
            raw = (rec.get("fields") or {}).get(fname)
            for c in _scalars(raw):
                if c not in samples:
                    samples.append(c)
            if len(samples) >= 3:
                break
        fields_out.append({
            "name": fname,
            "type": ftype,
            "description": desc,
            "sample_values": [_cap_value(s) for s in samples[:3]],  # GAP-4: cap heavy samples
            "is_pii": lark_client.is_pii_field(fname or ""),
        })

    meta = next((t for t in lark_client.get_tables() if t.get("table_id") == table_id), {})
    return {
        "table_name": meta.get("name") or table_name,
        "description": meta.get("description", "") or "",
        "fields": fields_out,
    }


async def query_records(args: dict) -> dict:
    """Generic filter/search/sort over any table (Contract v1.0 §2)."""
    table_name = (args.get("table_name") or "").strip()
    if not table_name:
        return {"error": "table_name required"}
    field_filters = args.get("field_filters") or {}
    if not isinstance(field_filters, dict):
        return {"error": "field_filters must be an object"}
    text_search = (args.get("text_search") or "").strip()
    return_fields = args.get("return_fields", "*")
    limit = min(int(args.get("limit") or 50), 500)
    sort_by = (args.get("sort_by") or "").strip()

    try:
        table_id = lark_client.resolve_table_name(table_name)
    except lark_client.LarkClientError as e:
        return {"error": f"Lark schema fetch failed: {str(e)[:200]}"}
    if not table_id:
        return {
            "error": f"unknown table '{table_name}'",
            "hint": "call list_tables to see available table names",
        }

    # Validate filter field names against the table schema.
    field_defs = lark_client.get_table_fields(table_id)
    valid_names = {fd.get("field_name") for fd in field_defs if fd.get("field_name")}
    if valid_names:  # only enforce if schema known
        for k in field_filters:
            if k not in valid_names:
                return {
                    "error": f"unknown field '{k}' in table '{table_name}'",
                    "available_fields": sorted(valid_names),
                }

    # Validate ops + build the debug formula string.
    try:
        formula = _build_formula(field_filters)
    except ValueError as e:
        return {"error": str(e), "supported_ops": sorted(_FILTER_OPS)}

    _log.info(
        "query_records",
        extra={
            "table": table_name,
            "formula": formula,
            "text_search": bool(text_search),
            "limit": limit,
        },
    )

    try:
        raw_records = lark_client.get_all_records(table_id)
    except lark_client.LarkClientError as e:
        return {"error": f"Lark fetch failed: {str(e)[:200]}"}

    matched: list[dict] = []
    for rec in raw_records:
        fields = rec.get("fields") or {}
        if field_filters:
            try:
                if not _record_matches(fields, field_filters):
                    continue
            except ValueError as e:
                return {"error": str(e), "supported_ops": sorted(_FILTER_OPS)}
        if text_search and not _text_search_match(fields, text_search):
            continue
        matched.append(rec)

    if sort_by:
        desc = sort_by.startswith("-")
        key_field = sort_by[1:] if desc else sort_by
        matched.sort(key=lambda r: _sort_key((r.get("fields") or {}).get(key_field)), reverse=desc)

    matched_count = len(matched)
    truncated = matched_count > limit
    records_out = [_project(rec, return_fields) for rec in matched[:limit]]

    result = {
        "table_name": table_name,
        "matched_count": matched_count,
        "records": records_out,
        "truncated": truncated,
    }
    # GAP-4: on return_fields="*", tell the AI which heavy fields were dropped
    # so it can opt back in via an explicit return_fields list.
    if not (isinstance(return_fields, list) and return_fields):
        excluded = sorted(n for n in valid_names if _is_heavy_field(n))
        if excluded:
            result["_excluded_heavy_fields"] = excluded
    return result


TOOL_HANDLERS["list_tables"] = list_tables
TOOL_HANDLERS["describe_table"] = describe_table
TOOL_HANDLERS["query_records"] = query_records
