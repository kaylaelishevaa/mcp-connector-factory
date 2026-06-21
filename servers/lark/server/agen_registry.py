"""Agen registry — ou_id ↔ display name mapping.

Sourced from an upstream platform registry (canonical, maintained by the author).
6 active agen + 1 VIP (the principal, separate workflow).

Used by tools/read_tools.py to:
- expose `list_agen()` so the operator can see who's who
- enrich activity / listing output with resolved agen names (not opaque ou_xxx)

If new agen added or ou_id changes, update HERE — not duplicate elsewhere.
"""
from __future__ import annotations

# Source of truth — keep in sync with the upstream registry.
AGEN_LIST: list[dict] = [
    {
        "nama_display": "Maya",
        "ou_id": "ou_1111111111111111111111111111aaaa",
        "phone_normalized": "+6281200000001",
        "email": "sales@example.com",
        "role": "production",
    },
    {
        "nama_display": "Rina",
        "aliases": ["Rini"],
        "ou_id": "ou_2222222222222222222222222222bbbb",
        "phone_normalized": "+6281200000002",
        "email": "marketing@example.com",
        "role": "production",
    },
    {
        "nama_display": "Sari",
        "ou_id": "ou_3333333333333333333333333333cccc",
        "phone_normalized": "+6281200000003",
        "email": "busdev1@example.com",
        "role": "production",
    },
    {
        "nama_display": "Dimas",
        "ou_id": "ou_4444444444444444444444444444dddd",
        "phone_normalized": "+6281200000004",
        "email": "contact@example.com",
        "role": "production",
    },
    {
        "nama_display": "Putri",
        "ou_id": "ou_5555555555555555555555555555eeee",
        "phone_normalized": "+6281200000005",
        "email": "busdev@example.com",
        "role": "production",
    },
    {
        "nama_display": "Budi",
        "ou_id": "ou_6666666666666666666666666666ffff",
        "email": "vip@example.com",
        "role": "vip",
        "note": "VIP, separate workflow",
    },
]


# Index for fast lookup
_BY_OU_ID: dict[str, dict] = {a["ou_id"]: a for a in AGEN_LIST}
_BY_NAME_LOWER: dict[str, dict] = {}
for _a in AGEN_LIST:
    _BY_NAME_LOWER[_a["nama_display"].lower()] = _a
    for _alias in _a.get("aliases", []):
        _BY_NAME_LOWER[_alias.lower()] = _a


def lookup_by_ou_id(ou_id: str) -> dict | None:
    """Exact lookup by Lark User open_id. Returns full agen dict or None."""
    if not ou_id:
        return None
    return _BY_OU_ID.get(ou_id.strip())


def lookup_by_name(name: str) -> dict | None:
    """Fuzzy lookup by display name or alias. Case-insensitive substring +
    exact alias match. Returns first match or None.

    Priority:
    1. Exact name / alias match (case-insensitive)
    2. Substring match against display name
    """
    if not name:
        return None
    q = name.strip().lower()
    if q in _BY_NAME_LOWER:
        return _BY_NAME_LOWER[q]
    for nlower, agen in _BY_NAME_LOWER.items():
        if q in nlower or nlower in q:
            return agen
    return None


def resolve(query: str) -> dict | None:
    """Resolve a query string (ou_id OR name) to an agen dict.

    Tries ou_id exact match first (cheap), then name fuzzy match.
    """
    if not query:
        return None
    q = query.strip()
    if q.startswith("ou_"):
        return lookup_by_ou_id(q)
    return lookup_by_name(q)


def all_agen() -> list[dict]:
    """Return list of all 6 agen + VIP (no internal-only metadata)."""
    return [
        {k: v for k, v in a.items() if k != "aliases"}
        for a in AGEN_LIST
    ]


def resolve_ou_ids_to_names(ou_ids: list[str]) -> list[str]:
    """Bulk resolve list of ou_ids to display names. Unknown ou_ids passed
    through as `'<unknown:ou_xxx>'` so caller sees the raw ID."""
    out: list[str] = []
    for ou in ou_ids:
        if not ou:
            continue
        info = lookup_by_ou_id(ou)
        if info:
            out.append(info["nama_display"])
        else:
            out.append(f"<unknown:{ou[:12]}...>")
    return out
