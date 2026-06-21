"""Lark Bitable client — full access (no permission filter).

Adapted from the upstream AI bot platform's shared `lark_client.py` but stripped down to
what the MCP server needs. KEY DIFFERENCE: NO owner field stripping. the operator is
owner-tier — sees everything.

Token cached + auto-refresh on expiry.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from server.logger import get_logger, log_anomaly

_log = get_logger("lark_client")

BASE_URL = "https://open.larksuite.com/open-apis"

# Table IDs (verified live 16 Apr 2026, locked per Acme Operations schema)
TABLE_LISTINGS = "tblLISTINGS00001"
TABLE_CONTACTS = "tblCONTACTS00001"
TABLE_ACTIVITIES = "tblACTIVITY00001"

_token_state: dict[str, Any] = {"token": None, "expires_at": 0.0}
_listings_cache: dict[str, Any] = {"items": None, "fetched_at": 0.0}
_contacts_cache: dict[str, Any] = {"items": None, "fetched_at": 0.0}
_activities_cache: dict[str, Any] = {"items": None, "fetched_at": 0.0}
_LISTINGS_CACHE_TTL = 3600  # 1 hour
_CONTACTS_CACHE_TTL = 3600
_ACTIVITIES_CACHE_TTL = 600  # 10 min — smaller dataset, fresher useful


class LarkClientError(RuntimeError):
    pass


def _config() -> tuple[str, str, str]:
    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    base_id = os.environ.get("LARK_BASE_ID", "").strip()
    if not all([app_id, app_secret, base_id]):
        raise LarkClientError("LARK_APP_ID / LARK_APP_SECRET / LARK_BASE_ID missing in env")
    return app_id, app_secret, base_id


def get_token(force: bool = False) -> str:
    """Tenant access token. Refresh ~5 min before expiry."""
    now = time.time()
    if not force and _token_state["token"] and _token_state["expires_at"] > now + 60:
        return _token_state["token"]
    app_id, app_secret, _ = _config()
    r = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    if r.status_code != 200:
        raise LarkClientError(f"get_token failed {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("code") != 0:
        raise LarkClientError(f"get_token Lark error: {data}")
    _token_state["token"] = data["tenant_access_token"]
    _token_state["expires_at"] = now + int(data.get("expire", 7200))
    return _token_state["token"]


def _hdr() -> dict:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }


def _base_id() -> str:
    return _config()[2]


def _ss(v):
    """SingleSelect value normalizer (Lark returns str or dict)."""
    if isinstance(v, dict):
        return v.get("text") or v.get("name")
    return v


# Generic Lark Query Contract v1.0 — schema + extra-field support

# Raw Lark field names that each normalizer already exposes as curated keys.
# `_extra_fields` returns everything in the raw record NOT in these sets, so
# custom fields (Priority Tier, Owner Notes, Deal Stage, ...) reach the AI.
_CURATED_LISTING_RAW: frozenset[str] = frozenset({
    "Nama Properti", "Unit", "Area/Kawasan", "Kamar Tidur", "Kamar Mandi",
    "Luas Bangunan", "Luas Tanah", "Lantai", "Harga Sewa", "Harga Jual",
    "Tipe Listing", "Tipe Properti", "Kondisi", "Status", "Agen",
    "Pemilik", "Flag Audit",
})
_CURATED_CONTACT_RAW: frozenset[str] = frozenset({
    "Nama", "No HP", "Tipe Contact", "Sumber Klien", "Agen",
    "History Chat Qontak",
})
_CURATED_ACTIVITY_RAW: frozenset[str] = frozenset({
    "Judul", "Waktu Mulai", "Waktu Selesai", "Tipe", "Status", "Lokasi",
    "Hasil", "Source", "Agen", "Contact", "Listings",
})


def _extra_fields(f: dict, curated: frozenset[str]) -> dict:
    """All raw Lark fields NOT already exposed as curated keys.

    Preserves Lark field-name casing. Drops nulls, empty strings, empty
    lists/dicts. This is what surfaces custom fields (per Generic Query
    Contract v1.0 §3) without us having to enumerate every field.
    """
    out: dict[str, Any] = {}
    for k, v in f.items():
        if k in curated:
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        out[k] = v
    return out


# Lark field type code → human-readable label (for describe_table output).
FIELD_TYPE_NAMES: dict[int, str] = {
    1: "text", 2: "number", 3: "single_select", 4: "multi_select",
    5: "datetime", 7: "checkbox", 11: "person", 13: "phone", 15: "url",
    17: "attachment", 18: "single_link", 19: "lookup", 20: "formula",
    21: "duplex_link", 22: "location", 23: "group_chat",
    1001: "created_time", 1002: "modified_time", 1003: "created_user",
    1004: "modified_user", 1005: "auto_number",
}

# PII flag — canonical pattern from shared-architecture.md
# (kept identical to the Q&A bot for cross-project consistency). On this
# admin-tier server the flag is INFORMATIONAL ONLY — nothing is stripped.
_PII_FIELD_PATTERN = re.compile(
    r"(?i)\b(hp|phone|telepon|telp|nomor|owner|pemilik|kontak.{0,5}name|name.{0,5}owner)\b"
)
_PII_ALLOWLIST: frozenset[str] = frozenset({
    "Phone Brand",      # device brand, not a number
    "Owner Building",   # building owned-by, not a person
})


def is_pii_field(field_name: str) -> bool:
    """Pattern-based PII flag for a field name. Informational on this server."""
    if not field_name or field_name in _PII_ALLOWLIST:
        return False
    return bool(_PII_FIELD_PATTERN.search(field_name))


# low-level CRUD

def get_record(table_id: str, record_id: str) -> dict:
    """Fetch a single record. Returns the raw record dict {record_id, fields}."""
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records/{record_id}"
    r = requests.get(url, headers=_hdr(), timeout=15)
    if r.status_code >= 400:
        raise LarkClientError(f"get_record failed {r.status_code}: {r.text[:300]}")
    data = r.json().get("data") or {}
    return data.get("record") or data  # Lark sometimes wraps in 'record', sometimes not


def update_record(table_id: str, record_id: str, fields: dict) -> dict:
    """Update fields on an existing record. Returns the updated record dict.

    Lark uses PUT verb but PATCH semantics — only fields in body are modified
    (fields omitted from the body are left untouched, NOT cleared). The
    spec said PATCH; Lark's single-record update endpoint is actually HTTP PUT
    and a literal PATCH request 404s.

    Raises LarkClientError on non-2xx HTTP or a non-zero Lark `code`.
    """
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records/{record_id}"
    r = requests.put(url, headers=_hdr(), json={"fields": fields}, timeout=20)
    body = _json_or_empty(r)
    if r.status_code >= 400 or body.get("code") not in (0, None):
        raise LarkClientError(
            f"Lark update_record error (HTTP {r.status_code}, code={body.get('code')}): "
            f"{body.get('msg') or r.text[:300]}"
        )
    data = body.get("data") or {}
    return data.get("record") or data


def create_record(table_id: str, fields: dict) -> dict:
    """Create a new record in a table. Returns the created record dict
    (includes the server-generated record_id).

    Raises LarkClientError on non-2xx HTTP or a non-zero Lark `code`.
    """
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records"
    r = requests.post(url, headers=_hdr(), json={"fields": fields}, timeout=20)
    body = _json_or_empty(r)
    if r.status_code >= 400 or body.get("code") not in (0, None):
        raise LarkClientError(
            f"Lark create_record error (HTTP {r.status_code}, code={body.get('code')}): "
            f"{body.get('msg') or r.text[:300]}"
        )
    data = body.get("data") or {}
    return data.get("record") or data


def _json_or_empty(r) -> dict:
    """Parse a response body as JSON, returning {} if it isn't JSON."""
    try:
        return r.json() or {}
    except ValueError:
        return {}


def search_records(
    table_id: str, formula_filter: str = "", page_size: int = 100
) -> list[dict]:
    """List records using optional formula-filter query parameter.

    Note: single page only (matches Activities bot pattern). For full pagination
    use fetch_all_*_paginated helpers.
    """
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records"
    params: dict = {"page_size": page_size}
    if formula_filter:
        params["filter"] = formula_filter
    r = requests.get(url, headers=_hdr(), params=params, timeout=20)
    if r.status_code >= 400:
        raise LarkClientError(f"search_records failed {r.status_code}: {r.text[:300]}")
    return (r.json().get("data") or {}).get("items") or []


def _fetch_all_paginated(table_id: str) -> list[dict]:
    """Page through table until has_more=false. Returns raw records."""
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records"
    out: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=_hdr(), params=params, timeout=60)
        if r.status_code >= 400:
            raise LarkClientError(f"fetch_all failed {r.status_code}: {r.text[:300]}")
        data = r.json().get("data") or {}
        out.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


# Listings

def fetch_all_listings(*, force_refresh: bool = False) -> list[dict]:
    """All listings, FULL fields (no owner strip). Cached 1h.

    Unlike Q&A bot's summary version, this returns full records — the operator sees
    everything.
    """
    now = time.time()
    if (
        not force_refresh
        and _listings_cache["items"] is not None
        and (now - _listings_cache["fetched_at"]) < _LISTINGS_CACHE_TTL
    ):
        return _listings_cache["items"]
    raw = _fetch_all_paginated(TABLE_LISTINGS)
    _listings_cache["items"] = raw
    _listings_cache["fetched_at"] = now
    _log.info(f"listings cache refreshed: {len(raw)} records")
    return raw


def normalize_listing(raw: dict) -> dict:
    """Convert raw Lark record to friendlier dict with snake_case keys.

    Includes owner info (the operator-tier). Used by MCP tools to format responses.

    Field-name reference: 01_SCHEMA_DAN_DATA_RULES/02_LARK_TABLES_FIELDS.md
    - "Tipe Listing" (#8 fldTY8T1Hp): Sewa / Jual
    - "Kondisi" (#17 fldbhbSX31): Furnished / Semi Furnished / Unfurnished
    - "Tipe Properti" (#26 fldhaYRJMJ): Apartment / House / Office / etc.
    - "Flag Audit" (fldYCDVQE4 MultiSelect): audit flags for follow-up
      (Listings does NOT have "Audit Trail"; that's a Contacts-only field)
    """
    f = raw.get("fields") or {}
    flag_audit = f.get("Flag Audit") or []
    # Flag Audit is MultiSelect — values may be list of dicts or list of strs
    if isinstance(flag_audit, list):
        flag_audit_normalized = [
            (item.get("text") or item.get("name") if isinstance(item, dict) else str(item))
            for item in flag_audit
        ]
        flag_audit_normalized = [x for x in flag_audit_normalized if x]
    else:
        flag_audit_normalized = []

    return {
        "record_id": raw.get("record_id"),
        "nama_properti": (f.get("Nama Properti") or "").strip(),
        "unit": (f.get("Unit") or "").strip(),
        "area": f.get("Area/Kawasan") or None,
        "kt": f.get("Kamar Tidur") or None,
        "km": f.get("Kamar Mandi") or None,
        "luas_bangunan": f.get("Luas Bangunan") or None,
        "luas_tanah": f.get("Luas Tanah") or None,
        "lantai": f.get("Lantai") or None,
        "harga_sewa": f.get("Harga Sewa") or None,
        "harga_jual": f.get("Harga Jual") or None,
        "tipe_listing": _ss(f.get("Tipe Listing")),   # Sewa / Jual
        "tipe_properti": _ss(f.get("Tipe Properti")),  # Apartment / House / etc
        "kondisi": _ss(f.get("Kondisi")),              # Furnished / Semi / Unfurnished
        "status": _ss(f.get("Status")),
        "agen": f.get("Agen") or [],   # User multi-select
        "pemilik": f.get("Pemilik") or [],    # Contact link (full access for the operator)
        "flag_audit": flag_audit_normalized,  # MultiSelect: audit flags for follow-up
        "_extra_fields": _extra_fields(f, _CURATED_LISTING_RAW),  # all custom fields
        "_raw_fields": f,  # full untouched fields for debug
    }


# Contacts

def get_contact(contact_id: str) -> dict | None:
    try:
        return get_record(TABLE_CONTACTS, contact_id)
    except LarkClientError as e:
        log_anomaly(
            kind="lark_get_contact_failed",
            component="lark_client",
            detail={"contact_id": contact_id, "exc": str(e)[:200]},
        )
        return None


def fetch_all_contacts(*, force_refresh: bool = False) -> list[dict]:
    """All contacts, full fields. Cached 1h. ~9,800 records, ~5-10s on first fetch."""
    now = time.time()
    if (
        not force_refresh
        and _contacts_cache["items"] is not None
        and (now - _contacts_cache["fetched_at"]) < _CONTACTS_CACHE_TTL
    ):
        return _contacts_cache["items"]
    raw = _fetch_all_paginated(TABLE_CONTACTS)
    _contacts_cache["items"] = raw
    _contacts_cache["fetched_at"] = now
    _log.info(f"contacts cache refreshed: {len(raw)} records")
    return raw


def normalize_contact(raw: dict) -> dict:
    f = raw.get("fields") or {}
    tipe = f.get("Tipe Contact") or []
    if not isinstance(tipe, list):
        tipe = [tipe]
    return {
        "record_id": raw.get("record_id"),
        "nama": (f.get("Nama") or "").strip(),
        "no_hp": f.get("No HP") or None,
        "tipe_contact": [_ss(t) for t in tipe],
        "sumber_klien": _ss(f.get("Sumber Klien")),
        "agen": f.get("Agen") or [],
        "history_chat_qontak": (f.get("History Chat Qontak") or "")[:1000],
        "_extra_fields": _extra_fields(f, _CURATED_CONTACT_RAW),  # all custom fields
        "_raw_fields": f,
    }


# Activities

def fetch_all_activities(*, force_refresh: bool = False) -> list[dict]:
    """All activities, full fields. Cached 10min (smaller dataset, fresher useful)."""
    now = time.time()
    if (
        not force_refresh
        and _activities_cache["items"] is not None
        and (now - _activities_cache["fetched_at"]) < _ACTIVITIES_CACHE_TTL
    ):
        return _activities_cache["items"]
    raw = _fetch_all_paginated(TABLE_ACTIVITIES)
    _activities_cache["items"] = raw
    _activities_cache["fetched_at"] = now
    _log.info(f"activities cache refreshed: {len(raw)} records")
    return raw


def normalize_activity(raw: dict) -> dict:
    f = raw.get("fields") or {}
    return {
        "record_id": raw.get("record_id"),
        "judul": f.get("Judul") or None,
        "waktu_mulai": f.get("Waktu Mulai") or None,
        "waktu_selesai": f.get("Waktu Selesai") or None,
        "tipe": _ss(f.get("Tipe")),
        "status": _ss(f.get("Status")),
        "lokasi": f.get("Lokasi") or None,
        "hasil": f.get("Hasil") or None,
        "source": _ss(f.get("Source")),
        "agen": f.get("Agen") or [],
        "contact": f.get("Contact") or [],
        "listings": f.get("Listings") or [],
        "_extra_fields": _extra_fields(f, _CURATED_ACTIVITY_RAW),  # all custom fields
        "_raw_fields": f,
    }


# URLs

def get_lark_url(record_id: str, table: str = "listings") -> str:
    """Generate direct Lark Bitable URL for a record.

    Format: https://www.larksuite.com/base/{base_id}?table={table_id}&view=&record={record_id}
    """
    table_map = {
        "listings": TABLE_LISTINGS,
        "contacts": TABLE_CONTACTS,
        "activities": TABLE_ACTIVITIES,
    }
    table_id = table_map.get(table.lower(), TABLE_LISTINGS)
    return get_lark_url_by_table(table_id, record_id)


def get_lark_url_by_table(table_id: str, record_id: str) -> str:
    """Direct Lark Bitable URL from an explicit table_id (works for ANY table,
    unlike get_lark_url which only knows the 3 curated kinds)."""
    return (
        f"https://www.larksuite.com/base/{_base_id()}"
        f"?table={table_id}&record={record_id}"
    )


# Schema introspection (Generic Query Contract v1.0 §1)

_schema_cache: dict[str, Any] = {"tables": None, "fields": {}, "fetched_at": 0.0}
_SCHEMA_CACHE_TTL = 86400  # 24h — Lark schema rarely changes


def _fetch_tables() -> list[dict]:
    """List all tables in the base. Returns [{table_id, name}]."""
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables"
    out: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=_hdr(), params=params, timeout=30)
        if r.status_code >= 400:
            raise LarkClientError(f"list_tables failed {r.status_code}: {r.text[:300]}")
        data = r.json().get("data") or {}
        for t in data.get("items") or []:
            out.append({"table_id": t.get("table_id"), "name": t.get("name")})
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


def _fetch_fields(table_id: str) -> list[dict]:
    """List field definitions for a table. Returns raw Lark field dicts."""
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/fields"
    out: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=_hdr(), params=params, timeout=30)
        if r.status_code >= 400:
            raise LarkClientError(f"list_fields failed {r.status_code}: {r.text[:300]}")
        data = r.json().get("data") or {}
        out.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


def _table_total(table_id: str) -> int:
    """Cheap record count — reads data.total from a 1-record query."""
    url = f"{BASE_URL}/bitable/v1/apps/{_base_id()}/tables/{table_id}/records"
    try:
        r = requests.get(url, headers=_hdr(), params={"page_size": 1}, timeout=20)
        if r.status_code >= 400:
            return -1
        return int((r.json().get("data") or {}).get("total") or 0)
    except (requests.RequestException, ValueError, TypeError):
        return -1


def fetch_schema(*, force_refresh: bool = False) -> dict:
    """Populate + return the schema cache (tables + fields + counts). 24h TTL.

    Called at startup by the background prefetch and lazily by the schema
    tools. Per-table field-fetch failures are logged but don't abort the
    whole schema build.
    """
    now = time.time()
    if (
        not force_refresh
        and _schema_cache["tables"] is not None
        and (now - _schema_cache["fetched_at"]) < _SCHEMA_CACHE_TTL
    ):
        return _schema_cache

    tables = _fetch_tables()
    fields: dict[str, list[dict]] = {}
    for t in tables:
        tid = t.get("table_id")
        if not tid:
            continue
        try:
            fields[tid] = _fetch_fields(tid)
        except LarkClientError as e:
            log_anomaly(
                kind="schema_fields_fetch_failed",
                component="lark_client",
                detail={"table_id": tid, "exc": str(e)[:200]},
            )
            fields[tid] = []
        t["record_count"] = _table_total(tid)

    _schema_cache["tables"] = tables
    _schema_cache["fields"] = fields
    _schema_cache["fetched_at"] = now
    _log.info(f"schema cache refreshed: {len(tables)} tables")
    return _schema_cache


def get_tables(*, force_refresh: bool = False) -> list[dict]:
    """Cached list of tables: [{table_id, name, record_count}]."""
    return fetch_schema(force_refresh=force_refresh)["tables"] or []


def get_table_fields(table_id: str, *, force_refresh: bool = False) -> list[dict]:
    """Cached raw field defs for a table_id."""
    return fetch_schema(force_refresh=force_refresh)["fields"].get(table_id, [])


def schema_synced_at_iso() -> str | None:
    """ISO-8601 UTC of the last schema cache refresh, or None if never."""
    ts = _schema_cache.get("fetched_at") or 0.0
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_table_name(name: str) -> str | None:
    """Resolve a human table name → table_id (case-insensitive)."""
    if not name:
        return None
    nl = name.strip().lower()
    for t in get_tables():
        if (t.get("name") or "").strip().lower() == nl:
            return t.get("table_id")
    return None


def get_all_records(table_id: str, *, force_refresh: bool = False) -> list[dict]:
    """Fetch all raw records for a table. Uses the warm cache for the three
    big tables (listings/contacts/activities); paginates directly for the
    other (small / empty) tables."""
    if table_id == TABLE_LISTINGS:
        return fetch_all_listings(force_refresh=force_refresh)
    if table_id == TABLE_CONTACTS:
        return fetch_all_contacts(force_refresh=force_refresh)
    if table_id == TABLE_ACTIVITIES:
        return fetch_all_activities(force_refresh=force_refresh)
    return _fetch_all_paginated(table_id)
