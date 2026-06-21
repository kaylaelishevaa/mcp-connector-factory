"""WRITE tools for Lark MCP — the operator tier full access.

Two tools:
- update_record(table_name, record_id, fields) — patch existing record
- create_record(table_name, fields)            — create new record

No field-level allowlist (admin tier), no two-stage confirm (single user).
The audit log (`lark_mcp_writes.jsonl`) + Lark's native per-record revision
history are the safety net / rollback story.

Kill switch: env `LARK_WRITE_ENABLED` (default true). Set to a falsey value
(`false`/`0`/`no`/`off`) → write tools refuse without touching Lark.

Failure model: write tools RAISE on any failure (validation, disabled,
record-not-found, Lark error). main.py's tools/call handler turns a raised
exception into an MCP `isError: true` response, so the model never mistakes a
failed write for success. (Read tools, by contrast, return {"error": ...}
dicts — writes diverge deliberately.)
"""
from __future__ import annotations

import hashlib
import os

from server import lark_client
from server.logger import get_logger, log_lark_write

_log = get_logger("write_tools")


_WRITE_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})


def _write_enabled() -> bool:
    """Kill switch — default ON. Only explicit falsey values disable writes."""
    return os.environ.get("LARK_WRITE_ENABLED", "true").strip().lower() not in _WRITE_DISABLED_VALUES


def _ensure_write_enabled() -> None:
    if not _write_enabled():
        raise RuntimeError(
            "Writes are disabled by admin (LARK_WRITE_ENABLED=false). "
            "No change was made to Lark."
        )


def _auth_token_hash() -> str:
    """SHA256(LARK_MCP_TOKEN)[:16] — identifies the caller in the audit log
    without storing the token itself."""
    tok = os.environ.get("LARK_MCP_TOKEN", "").strip()
    if not tok:
        return "no-token"
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16]


def _resolve_or_raise(table_name: str) -> str:
    """Resolve a human table name → table_id, raising a clear error if unknown."""
    try:
        table_id = lark_client.resolve_table_name(table_name)
    except lark_client.LarkClientError as e:
        raise RuntimeError(f"Lark schema fetch failed: {str(e)[:200]}")
    if not table_id:
        raise RuntimeError(
            f"unknown table '{table_name}' — call list_tables for valid table names"
        )
    return table_id


# Tool schemas

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "update_record",
        "description": (
            "Update fields on an existing Lark record. Use this WHENEVER the user "
            "asks to change/fix/correct a value — do NOT reply that you can't "
            "write. REQUIRED workflow:\n"
            "1. Call describe_table(table_name) to see exact field names + types.\n"
            "2. Identify the record via query_records / get_listing / get_contact "
            "(you need its record_id).\n"
            "3. Call update_record(table_name, record_id, fields={field_name: new_value, ...}).\n"
            "CRITICAL:\n"
            "- Field names must match describe_table output exactly (case + spacing).\n"
            "- Match the field type: number fields take numbers, SingleSelect takes "
            "the text label (e.g. 'Available', not a dict), dates take epoch ms, "
            "link/DuplexLink fields take a LIST of record_ids (['recXxx']) even for "
            "a single link, Person fields take ou_id strings.\n"
            "- For values stored with an emoji prefix (e.g. '🔴 Kritis'), prefer the "
            "exact stored label.\n"
            "- Only the fields you pass are changed; other fields are untouched.\n"
            "- Read-only fields (formula / lookup / auto-number / created/modified) "
            "will be rejected by Lark — do NOT try to update them.\n"
            "- After success, confirm to the user: '[Record] [field]: [old] → [new]' "
            "and offer the Lark link (get_lark_url).\n"
            "Example — fix Lantai 14→12 on listing recXyz: "
            "update_record('Listings', 'recXyz', {'Lantai': 12})."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Exact table name from list_tables (e.g. 'Listings', 'Contacts').",
                },
                "record_id": {
                    "type": "string",
                    "description": "record_id of the record to update (e.g. 'recXyz').",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Map of field_name -> new value. Field names must match "
                        "describe_table exactly. Only listed fields are modified."
                    ),
                },
            },
            "required": ["table_name", "record_id", "fields"],
        },
    },
    {
        "name": "create_record",
        "description": (
            "Create a new record in a Lark table. REQUIRED workflow:\n"
            "1. Call describe_table(table_name) for the field list + types.\n"
            "2. Build a fields dict with the primary field (usually a name/title — "
            "typically required) plus any others.\n"
            "3. Call create_record(table_name, fields={...}).\n"
            "Common gotchas:\n"
            "- link/DuplexLink fields take a LIST of record_ids (['recXxx']) — "
            "resolve the linked record first (query_records).\n"
            "- SingleSelect takes the text label ('Available'); Person fields take "
            "ou_id strings; dates take epoch ms.\n"
            "- Do NOT set computed/formula/auto fields (record_id, created time, "
            "etc.) — Lark generates them.\n"
            "After success, confirm the created record + offer its Lark link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Exact table name from list_tables.",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Map of field_name -> value for the new record. Include the "
                        "primary field. Field names must match describe_table exactly."
                    ),
                },
            },
            "required": ["table_name", "fields"],
        },
    },
]


# Handlers

async def update_record_tool(args: dict) -> dict:
    """Patch an existing record. Raises on any failure (→ MCP isError)."""
    _ensure_write_enabled()

    table_name = (args.get("table_name") or "").strip()
    record_id = (args.get("record_id") or "").strip()
    fields = args.get("fields")

    if not table_name:
        raise ValueError("table_name required")
    if not record_id:
        raise ValueError("record_id required")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields required (non-empty object of field_name -> value)")

    table_id = _resolve_or_raise(table_name)

    # Snapshot ONLY the keys being modified (keeps the audit log compact).
    fields_before: dict | None = None
    try:
        current = lark_client.get_record(table_id, record_id)
        cur_fields = current.get("fields") or {}
        fields_before = {k: cur_fields.get(k) for k in fields}
    except lark_client.LarkClientError:
        # Record may not exist — let the update call surface the real error.
        fields_before = None

    try:
        updated = lark_client.update_record(table_id, record_id, fields)
    except lark_client.LarkClientError as e:
        log_lark_write(
            tool="update_record",
            table_name=table_name,
            table_id=table_id,
            record_id=record_id,
            fields_before=fields_before,
            fields_after=fields,
            auth_token_hash=_auth_token_hash(),
            success=False,
            error=str(e)[:300],
        )
        raise RuntimeError(str(e)[:300])

    log_lark_write(
        tool="update_record",
        table_name=table_name,
        table_id=table_id,
        record_id=record_id,
        fields_before=fields_before,
        fields_after=fields,
        auth_token_hash=_auth_token_hash(),
        success=True,
    )
    return {
        "updated_record": updated,
        "table_name": table_name,
        "record_id": record_id,
        "fields_changed": list(fields.keys()),
        "lark_url": lark_client.get_lark_url_by_table(table_id, record_id),
    }


async def create_record_tool(args: dict) -> dict:
    """Create a new record. Raises on any failure (→ MCP isError)."""
    _ensure_write_enabled()

    table_name = (args.get("table_name") or "").strip()
    fields = args.get("fields")

    if not table_name:
        raise ValueError("table_name required")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields required (non-empty object of field_name -> value)")

    table_id = _resolve_or_raise(table_name)

    try:
        created = lark_client.create_record(table_id, fields)
    except lark_client.LarkClientError as e:
        log_lark_write(
            tool="create_record",
            table_name=table_name,
            table_id=table_id,
            record_id=None,
            fields_before=None,
            fields_after=fields,
            auth_token_hash=_auth_token_hash(),
            success=False,
            error=str(e)[:300],
        )
        raise RuntimeError(str(e)[:300])

    new_id = created.get("record_id")
    log_lark_write(
        tool="create_record",
        table_name=table_name,
        table_id=table_id,
        record_id=new_id,
        fields_before=None,
        fields_after=fields,
        auth_token_hash=_auth_token_hash(),
        success=True,
    )
    out = {
        "created_record": created,
        "table_name": table_name,
        "record_id": new_id,
    }
    if new_id:
        out["lark_url"] = lark_client.get_lark_url_by_table(table_id, new_id)
    return out


# Handler registry

TOOL_HANDLERS = {
    "update_record": update_record_tool,
    "create_record": create_record_tool,
}
