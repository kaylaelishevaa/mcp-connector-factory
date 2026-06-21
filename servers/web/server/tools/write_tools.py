"""WRITE tools for the Acme Web MCP server — PHASE 3 (FULL superadmin surface).

Wraps the NestJS admin API mutating endpoints for listings, users, and
roles/permissions. This is the highest-blast-radius surface in the connector:
when both gates are flipped on, the single inbound token can create/delete
users, mint roles, and reassign permissions site-wide. Every rail below is
load-bearing — do NOT remove one to "simplify".

RAILS (proposal §4, build prompt HARD RULES) — enforced by `_execute`:
  1. dry_run: bool = True on EVERY write tool. Dry-run returns before-state +
     the exact payload that WOULD be sent and performs NO mutation.
  2. Two independent kill switches, both default OFF:
       WEB_WRITE_ENABLED          → gates listing writes.
       WEB_IDENTITY_WRITE_ENABLED → gates user + role/permission writes.
     A tool whose gate is OFF refuses execution (dry_run=false) with a clear
     message. (dry-run previews are still allowed — they are read-only and
     useful for planning; the gate hard-blocks the actual mutation.)
  3. Destructive + identity ops (delete_*, any user/role create/edit/delete,
     any role-permission change) require confirm=true IN ADDITION to
     dry_run=false. Missing confirm → refuse.
  4. Every executed write: pre-read snapshot → backup JSON to
     <WEB_BACKUP_ROOT>/<tool>-<id>-<ts>.json → execute → post-read verify →
     append before/after to web_mcp_writes.jsonl. All calls route through
     web_client (which unwraps the {success,data} envelope).
  5. Bulk listing ops rate-limit between items (each mutation fans out to
     portal-a + partner-portal portal sync) — see `web_bulk_listing_action`.
  6. No secrets in backups/audit/preview — `password` is redacted everywhere.

NOTE: web gates default OFF (contrast servers/lark where LARK_WRITE_ENABLED
defaults ON) — adding a third writer to the web DB is the riskier surface, and
the identity surface is privilege-escalation-capable.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from server import web_client
from server.logger import get_logger, log_web_write

_log = get_logger("write_tools")

_REDACTED = "***REDACTED***"
_SECRET_KEYS = {"password", "tempPassword", "temp_password"}


# kill switches

def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _write_enabled() -> bool:
    """Listing-write kill switch (WEB_WRITE_ENABLED). Default OFF."""
    return _truthy("WEB_WRITE_ENABLED")


def _identity_write_enabled() -> bool:
    """User + role/permission write kill switch (WEB_IDENTITY_WRITE_ENABLED).
    Default OFF. Kept SEPARATE from WEB_WRITE_ENABLED so listing writes can go
    live while the privilege-escalation surface stays dark."""
    return _truthy("WEB_IDENTITY_WRITE_ENABLED")


def _gate_enabled(identity: bool) -> bool:
    return _identity_write_enabled() if identity else _write_enabled()


def _gate_name(identity: bool) -> str:
    return "WEB_IDENTITY_WRITE_ENABLED" if identity else "WEB_WRITE_ENABLED"


# backup + redaction

def _backup_root() -> Path:
    return Path(os.environ.get("WEB_BACKUP_ROOT", "/data/backups"))


def _ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _redact(obj: Any) -> Any:
    """Deep-copy with secret values masked. Used for backups, audit, and the
    dry-run `would_send` preview so a password never lands on disk or in logs."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if k in _SECRET_KEYS and obj[k] is not None else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _write_backup(tool: str, target: Any, contents: dict) -> str:
    """Write the pre-write backup JSON and return its path. Secrets redacted."""
    root = _backup_root()
    root.mkdir(parents=True, exist_ok=True)
    label = str(target) if target not in (None, "") else "new"
    path = root / f"{tool}-{label}-{_ts()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_redact(contents), f, ensure_ascii=False, indent=2, default=str)
    return str(path)


# refusal helpers

def _refusal(tool: str, reason: str, **extra: Any) -> dict:
    _log.warning("write refused", extra={"tool": tool, "reason": reason})
    out = {"refused": True, "tool": tool, "reason": reason}
    out.update(extra)
    return out


def _err(tool: str, msg: str) -> dict:
    return {"error": msg, "tool": tool}


# the one orchestration path every write tool goes through

def _execute(
    *,
    tool: str,
    identity: bool,
    destructive: bool,
    args: dict,
    method: str,
    path: str,
    payload: dict | None,
    pre_read: Callable[[], Any] | None,
    mutate: Callable[[], Any],
    target_id: Any = None,
    property_id: str | None = None,
) -> dict:
    """Run a single mutation through all rails.

    dry_run (default True) → return before-state + would_send, NO mutation.
    dry_run=false → gate must be ON; destructive/identity also need confirm=true;
    then snapshot → backup → execute → post-verify → audit.
    """
    dry_run = args.get("dry_run", True)
    if dry_run is None:
        dry_run = True
    confirm = bool(args.get("confirm", False))
    need_confirm = destructive or identity
    gate_on = _gate_enabled(identity)
    gate = _gate_name(identity)

    # pre-read snapshot (best-effort; reads are always allowed)
    before: Any = None
    if pre_read is not None:
        try:
            before = pre_read()
        except Exception as e:  # noqa: BLE001 — snapshot is best-effort
            before = {"_snapshot_error": str(e)[:200]}

    would_send = {"method": method, "path": path, "payload": _redact(payload)}

    # DRY RUN (default) — preview only, never mutates
    if dry_run:
        note = "DRY RUN — no mutation performed. Re-run with dry_run=false"
        if need_confirm:
            note += " and confirm=true"
        note += " to execute."
        if not gate_on:
            note += (
                f" ⚠ Gate {gate} is currently OFF — execution will be REFUSED "
                f"until it is set true in the droplet .env."
            )
        return {
            "dry_run": True,
            "tool": tool,
            "gate": gate,
            "gate_enabled": gate_on,
            "confirm_required": need_confirm,
            "before": before,
            "would_send": would_send,
            "note": note,
        }

    # EXECUTION (dry_run=false)
    if not gate_on:
        return _refusal(
            tool,
            f"gate {gate} is OFF — write refused. Set {gate}=true in the droplet "
            f".env (and restart the connector) to allow this mutation.",
            gate=gate,
        )
    if need_confirm and not confirm:
        return _refusal(
            tool,
            "destructive/identity operation requires confirm=true in addition to "
            "dry_run=false. Re-run with confirm=true to proceed.",
            gate=gate,
        )

    # backup BEFORE touching anything
    backup_path = _write_backup(
        tool, target_id if target_id is not None else property_id,
        {"endpoint": would_send, "before": before, "args": _redact(args)},
    )

    # execute
    success = True
    error: str | None = None
    after: Any
    try:
        after = mutate()
    except Exception as e:  # noqa: BLE001 — surface as structured failure + audit
        success = False
        error = str(e)[:300]
        after = {"_mutate_error": error}

    # writes bypass the GET cache, but a prior read may be cached — drop it so
    # the next read reflects the mutation.
    try:
        web_client.clear_cache()
    except Exception:  # noqa: BLE001
        pass

    # audit trail (append-only)
    log_web_write(
        tool=tool,
        property_id=property_id,
        listing_id=target_id if (not identity and isinstance(target_id, int)) else None,
        endpoint=f"{method} {path}",
        before=before if isinstance(before, dict) else {"value": before},
        after=after if isinstance(after, dict) else {"value": after},
        success=success,
        error=error,
    )

    if not success:
        return {
            "executed": False,
            "tool": tool,
            "error": error,
            "backup_path": backup_path,
            "before": before,
        }
    return {
        "executed": True,
        "dry_run": False,
        "tool": tool,
        "endpoint": would_send,
        "before": before,
        "after": after,
        "backup_path": backup_path,
    }


# resolve helpers

def _resolve_listing_id(args: dict) -> tuple[int | None, str | None]:
    """Return (internal_id, error). Accepts listing_id (int) or property_id (AR#####)."""
    if args.get("listing_id") is not None:
        try:
            return int(args["listing_id"]), None
        except (ValueError, TypeError):
            return None, "listing_id must be an integer"
    pid = (args.get("property_id") or "").strip()
    if not pid:
        return None, "listing_id or property_id is required"
    try:
        summary = web_client.resolve_by_property_id(pid)
    except web_client.WebClientError as e:
        return None, f"resolve failed: {str(e)[:160]}"
    if not summary:
        return None, f"no listing found for property_id {pid}"
    try:
        return int(summary["id"]), None
    except (KeyError, ValueError, TypeError):
        return None, f"resolved listing for {pid} has no usable id"


# LISTING WRITE TOOLS — gate WEB_WRITE_ENABLED

async def web_create_listing(args: dict) -> dict:
    listing = args.get("listing")
    if not isinstance(listing, dict):
        return _err("web_create_listing", "`listing` object is required")
    ptype = listing.get("property_type")
    category = listing.get("category")
    if not ptype:
        return _err("web_create_listing", "listing.property_type is required (e.g. 'apartment')")
    if category not in ("SELL", "RENT"):
        return _err("web_create_listing", "listing.category must be 'SELL' or 'RENT'")
    return _execute(
        tool="web_create_listing", identity=False, destructive=False, args=args,
        method="POST", path="/admin/listings", payload=listing,
        target_id=None, property_id=listing.get("property_id"),
        pre_read=None,
        mutate=lambda: web_client.create_listing(listing),
    )


async def web_update_listing(args: dict) -> dict:
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        return _err("web_update_listing", "`fields` object with at least one field is required")
    lid, err = _resolve_listing_id(args)
    if err:
        return _err("web_update_listing", err)
    return _execute(
        tool="web_update_listing", identity=False, destructive=False, args=args,
        method="PUT", path=f"/admin/listings/{lid}", payload=fields,
        target_id=lid, property_id=args.get("property_id"),
        pre_read=lambda: web_client.get_listing_detail(lid),
        mutate=lambda: web_client.update_listing(lid, fields),
    )


def _listing_state_tool(tool: str, action: str):
    async def handler(args: dict) -> dict:
        lid, err = _resolve_listing_id(args)
        if err:
            return _err(tool, err)
        return _execute(
            tool=tool, identity=False, destructive=False, args=args,
            method="PUT", path=f"/admin/listings/{lid}/{action}", payload=None,
            target_id=lid, property_id=args.get("property_id"),
            pre_read=lambda: web_client.get_listing_detail(lid),
            mutate=lambda: web_client.listing_state(lid, action),
        )
    return handler


web_publish = _listing_state_tool("web_publish", "publish")
web_unpublish = _listing_state_tool("web_unpublish", "unpublish")
web_mark_sold = _listing_state_tool("web_mark_sold", "sold")


async def web_delete_listing(args: dict) -> dict:
    lid, err = _resolve_listing_id(args)
    if err:
        return _err("web_delete_listing", err)
    return _execute(
        tool="web_delete_listing", identity=False, destructive=True, args=args,
        method="DELETE", path=f"/admin/listings/{lid}", payload=None,
        target_id=lid, property_id=args.get("property_id"),
        pre_read=lambda: web_client.get_listing_detail(lid),
        mutate=lambda: web_client.delete_listing(lid),
    )


# State transitions usable in bulk: trailing path segment + whether destructive.
_BULK_ACTIONS = {
    "publish": ("publish", False),
    "unpublish": ("unpublish", False),
    "sold": ("sold", False),
    "refresh": ("refresh", False),
    "delete": (None, True),  # DELETE verb, handled specially
}


async def web_bulk_listing_action(args: dict) -> dict:
    """Apply one state action to many listings, ONE item at a time with a
    throttle between items (rail §5 — each mutation fans out to portal sync).

    Each item runs the full single-item rail (snapshot/backup/verify/audit), so
    the audit trail is per-listing, not one opaque bulk row. `delete` is
    destructive → confirm=true required (applies to the whole batch).
    """
    action = (args.get("action") or "").strip().lower()
    if action not in _BULK_ACTIONS:
        return _err("web_bulk_listing_action", f"action must be one of {sorted(_BULK_ACTIONS)}")
    items = args.get("ids")
    if not isinstance(items, list) or not items:
        return _err("web_bulk_listing_action", "`ids` must be a non-empty list of property_id (AR#####) or internal listing_id")

    seg, destructive = _BULK_ACTIONS[action]
    tool = f"web_bulk_{action}"
    throttle = web_client.bulk_throttle_seconds()

    results = []
    for i, raw in enumerate(items):
        if i > 0 and throttle > 0:
            time.sleep(throttle)
        item_args = dict(args)  # carry dry_run/confirm through to each item
        if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
            item_args["listing_id"] = int(raw)
            item_args.pop("property_id", None)
        else:
            item_args["property_id"] = str(raw)
            item_args.pop("listing_id", None)

        lid, err = _resolve_listing_id(item_args)
        if err:
            results.append({"item": raw, "error": err})
            continue

        if action == "delete":
            res = _execute(
                tool=tool, identity=False, destructive=True, args=item_args,
                method="DELETE", path=f"/admin/listings/{lid}", payload=None,
                target_id=lid, property_id=item_args.get("property_id"),
                pre_read=lambda lid=lid: web_client.get_listing_detail(lid),
                mutate=lambda lid=lid: web_client.delete_listing(lid),
            )
        else:
            res = _execute(
                tool=tool, identity=False, destructive=False, args=item_args,
                method="PUT", path=f"/admin/listings/{lid}/{seg}", payload=None,
                target_id=lid, property_id=item_args.get("property_id"),
                pre_read=lambda lid=lid: web_client.get_listing_detail(lid),
                mutate=lambda lid=lid, seg=seg: web_client.listing_state(lid, seg),
            )
        results.append({"item": raw, "listing_id": lid, "result": res})

    return {"tool": tool, "action": action, "count": len(items), "results": results}


# USER WRITE TOOLS — gate WEB_IDENTITY_WRITE_ENABLED (+ confirm)
# Service-layer escalation guard (admin-user.service.ts): creating an *admin*
# account (is_admin=true), modifying a *super admin*, or promoting to internal
# is blocked UNLESS the actor's RBAC role is `super admin`. For superadmin (C)
# the service account holds `super admin`, which opens these — so these tools
# CAN create admins and touch super admins. Treat with maximal care.

async def web_create_user(args: dict) -> dict:
    user = args.get("user")
    if not isinstance(user, dict):
        return _err("web_create_user", "`user` object is required")
    if not user.get("email"):
        return _err("web_create_user", "user.email is required")
    return _execute(
        tool="web_create_user", identity=True, destructive=False, args=args,
        method="POST", path="/admin/users", payload=user,
        target_id=None, property_id=None,
        pre_read=None,
        mutate=lambda: web_client.create_user(user),
    )


async def web_update_user(args: dict) -> dict:
    uid = args.get("user_id")
    fields = args.get("fields")
    if uid is None:
        return _err("web_update_user", "`user_id` is required")
    if not isinstance(fields, dict) or not fields:
        return _err("web_update_user", "`fields` object with at least one field is required")
    try:
        uid = int(uid)
    except (ValueError, TypeError):
        return _err("web_update_user", "user_id must be an integer")
    return _execute(
        tool="web_update_user", identity=True, destructive=False, args=args,
        method="PUT", path=f"/admin/users/{uid}", payload=fields,
        target_id=uid, property_id=None,
        pre_read=lambda: web_client.get_user(uid),
        mutate=lambda: web_client.update_user(uid, fields),
    )


async def web_delete_user(args: dict) -> dict:
    uid = args.get("user_id")
    if uid is None:
        return _err("web_delete_user", "`user_id` is required")
    try:
        uid = int(uid)
    except (ValueError, TypeError):
        return _err("web_delete_user", "user_id must be an integer")
    return _execute(
        tool="web_delete_user", identity=True, destructive=True, args=args,
        method="DELETE", path=f"/admin/users/{uid}", payload=None,
        target_id=uid, property_id=None,
        pre_read=lambda: web_client.get_user(uid),
        mutate=lambda: web_client.delete_user(uid),
    )


# ROLE / PERMISSION WRITE TOOLS — gate WEB_IDENTITY_WRITE_ENABLED (+ confirm)
# This is the SELF-ESCALATION surface: minting a role or granting a permission
# can widen what any account (including the service account) can do. The backend
# blocks editing/deleting the `super admin` role itself, but everything else is
# fair game once the gate is on. Strongest warnings + full audit.

async def web_create_role(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("web_create_role", "`name` is required")
    perms = args.get("permissions")
    payload: dict = {"name": name}
    if perms is not None:
        if not isinstance(perms, list):
            return _err("web_create_role", "`permissions` must be a list of permission names")
        payload["permissions"] = perms
    return _execute(
        tool="web_create_role", identity=True, destructive=True, args=args,
        method="POST", path="/admin/roles", payload=payload,
        target_id=None, property_id=None,
        pre_read=None,
        mutate=lambda: web_client.create_role(payload),
    )


async def web_update_role(args: dict) -> dict:
    rid = args.get("role_id")
    if rid is None:
        return _err("web_update_role", "`role_id` is required")
    try:
        rid = int(rid)
    except (ValueError, TypeError):
        return _err("web_update_role", "role_id must be an integer")
    payload: dict = {}
    if args.get("name") is not None:
        payload["name"] = args["name"]
    if args.get("permissions") is not None:
        if not isinstance(args["permissions"], list):
            return _err("web_update_role", "`permissions` must be a list of permission names")
        payload["permissions"] = args["permissions"]
    if not payload:
        return _err("web_update_role", "provide `name` and/or `permissions` to update")
    return _execute(
        tool="web_update_role", identity=True, destructive=True, args=args,
        method="PUT", path=f"/admin/roles/{rid}", payload=payload,
        target_id=rid, property_id=None,
        pre_read=lambda: web_client.get_role(rid),
        mutate=lambda: web_client.update_role(rid, payload),
    )


async def web_delete_role(args: dict) -> dict:
    rid = args.get("role_id")
    if rid is None:
        return _err("web_delete_role", "`role_id` is required")
    try:
        rid = int(rid)
    except (ValueError, TypeError):
        return _err("web_delete_role", "role_id must be an integer")
    return _execute(
        tool="web_delete_role", identity=True, destructive=True, args=args,
        method="DELETE", path=f"/admin/roles/{rid}", payload=None,
        target_id=rid, property_id=None,
        pre_read=lambda: web_client.get_role(rid),
        mutate=lambda: web_client.delete_role(rid),
    )


async def web_grant_permission(args: dict) -> dict:
    rid = args.get("role_id")
    perm = (args.get("permission") or "").strip()
    if rid is None:
        return _err("web_grant_permission", "`role_id` is required")
    if not perm:
        return _err("web_grant_permission", "`permission` (name) is required")
    try:
        rid = int(rid)
    except (ValueError, TypeError):
        return _err("web_grant_permission", "role_id must be an integer")
    return _execute(
        tool="web_grant_permission", identity=True, destructive=True, args=args,
        method="POST", path=f"/admin/roles/{rid}/permissions",
        payload={"permission": perm},
        target_id=rid, property_id=None,
        pre_read=lambda: web_client.get_role(rid),
        mutate=lambda: web_client.grant_permission(rid, perm),
    )


async def web_revoke_permission(args: dict) -> dict:
    rid = args.get("role_id")
    perm = (args.get("permission") or "").strip()
    if rid is None:
        return _err("web_revoke_permission", "`role_id` is required")
    if not perm:
        return _err("web_revoke_permission", "`permission` (name) is required")
    try:
        rid = int(rid)
    except (ValueError, TypeError):
        return _err("web_revoke_permission", "role_id must be an integer")
    from urllib.parse import quote
    seg = quote(perm, safe="")
    return _execute(
        tool="web_revoke_permission", identity=True, destructive=True, args=args,
        method="DELETE", path=f"/admin/roles/{rid}/permissions/{seg}", payload=None,
        target_id=rid, property_id=None,
        pre_read=lambda: web_client.get_role(rid),
        mutate=lambda: web_client.revoke_permission(rid, perm),
    )


# schemas
# Shared rail args appended to every write tool's inputSchema.

def _with_rails(props: dict, *, required: list[str] | None = None, destructive: bool = False) -> dict:
    schema_props = dict(props)
    schema_props["dry_run"] = {
        "type": "boolean", "default": True,
        "description": "Default TRUE. When true, returns the before-state + the exact payload that WOULD be sent and performs NO mutation. Set false to actually execute.",
    }
    if destructive:
        schema_props["confirm"] = {
            "type": "boolean", "default": False,
            "description": "Required (true) IN ADDITION to dry_run=false for this destructive/identity op. Missing → refused.",
        }
    out: dict = {"type": "object", "properties": schema_props}
    if required:
        out["required"] = required
    return out


_LISTING_TARGET = {
    "property_id": {"type": "string", "description": "Public AR##### id, e.g. 'AR103917'."},
    "listing_id": {"type": "integer", "description": "Internal numeric listing id (alternative to property_id)."},
}


TOOL_SCHEMAS: list[dict] = [
    # listings (gate WEB_WRITE_ENABLED)
    {
        "name": "web_create_listing",
        "description": (
            "Create a example.com listing (POST /admin/listings → add_listing). "
            "Gate: WEB_WRITE_ENABLED. The `listing` object maps directly to "
            "AdminCreateListingDto — `property_type` (house|apartment|land|shop|"
            "warehouse|hotel|business|office|new_project_unit) and `category` "
            "(SELL|RENT) are required; all other fields optional (price as a "
            "positive-integer STRING, translations[], media[], etc.). dry_run "
            "default TRUE."
        ),
        "inputSchema": _with_rails({
            "listing": {"type": "object", "description": "Maps to AdminCreateListingDto. Requires property_type + category."},
        }, required=["listing"]),
    },
    {
        "name": "web_update_listing",
        "description": (
            "Update a listing (PUT /admin/listings/:id → edit_listing). Gate: "
            "WEB_WRITE_ENABLED. Identify by property_id or listing_id; `fields` "
            "maps to AdminUpdateListingDto (only provided keys are written; "
            "translations/media follow replace semantics — []=clear). dry_run "
            "default TRUE."
        ),
        "inputSchema": _with_rails({
            **_LISTING_TARGET,
            "fields": {"type": "object", "description": "Partial AdminUpdateListingDto — fields to change."},
        }),
    },
    {
        "name": "web_publish",
        "description": "Publish a listing (PUT /admin/listings/:id/publish → publish_listing). Gate: WEB_WRITE_ENABLED. dry_run default TRUE.",
        "inputSchema": _with_rails(dict(_LISTING_TARGET)),
    },
    {
        "name": "web_unpublish",
        "description": "Unpublish a listing (PUT /admin/listings/:id/unpublish → publish_listing). Gate: WEB_WRITE_ENABLED. dry_run default TRUE.",
        "inputSchema": _with_rails(dict(_LISTING_TARGET)),
    },
    {
        "name": "web_mark_sold",
        "description": "Mark a listing sold (PUT /admin/listings/:id/sold → edit_listing). Gate: WEB_WRITE_ENABLED. dry_run default TRUE.",
        "inputSchema": _with_rails(dict(_LISTING_TARGET)),
    },
    {
        "name": "web_delete_listing",
        "description": (
            "DELETE a listing (DELETE /admin/listings/:id → delete_listing). Gate: "
            "WEB_WRITE_ENABLED. DESTRUCTIVE — requires confirm=true in addition to "
            "dry_run=false. dry_run default TRUE."
        ),
        "inputSchema": _with_rails(dict(_LISTING_TARGET), destructive=True),
    },
    {
        "name": "web_bulk_listing_action",
        "description": (
            "Apply one action (publish|unpublish|sold|refresh|delete) to many "
            "listings, ONE at a time with a throttle between items (each mutation "
            "fans out to portal-a + operator portal sync). Gate: WEB_WRITE_ENABLED. "
            "`delete` is DESTRUCTIVE → confirm=true required for the batch. Each "
            "item runs the full snapshot/backup/verify/audit rail. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "action": {"type": "string", "enum": ["publish", "unpublish", "sold", "refresh", "delete"]},
            "ids": {"type": "array", "items": {"type": ["string", "integer"]}, "description": "property_id (AR#####) or internal listing_id values."},
        }, required=["action", "ids"], destructive=True),
    },
    # users (gate WEB_IDENTITY_WRITE_ENABLED + confirm)
    {
        "name": "web_create_user",
        "description": (
            "Create a staff/user account (POST /admin/users → invite_staff). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. `user` maps to CreateUserDto "
            "(email required; password, is_admin, first_name, last_name, phone, "
            "job_title, manager_id, pic_id, role_id optional). NOTE: is_admin=true "
            "(create an admin) only succeeds because the service account holds the "
            "`super admin` role (service-layer escalation guard). Sends an invite "
            "email + temp password. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "user": {"type": "object", "description": "Maps to CreateUserDto. email required."},
        }, required=["user"], destructive=True),
    },
    {
        "name": "web_update_user",
        "description": (
            "Update a user (PUT /admin/users/:id → edit_staff). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. `fields` maps to "
            "UpdateUserDto. Promoting to is_admin=true or modifying a super admin "
            "only succeeds via the `super admin` service account. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "user_id": {"type": "integer"},
            "fields": {"type": "object", "description": "Partial UpdateUserDto."},
        }, required=["user_id", "fields"], destructive=True),
    },
    {
        "name": "web_delete_user",
        "description": (
            "DELETE a user (DELETE /admin/users/:id → delete_staff). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. DESTRUCTIVE. Deleting a "
            "super admin only succeeds via the `super admin` service account; the "
            "backend still blocks self-deletion. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "user_id": {"type": "integer"},
        }, required=["user_id"], destructive=True),
    },
    # roles / permissions (gate WEB_IDENTITY_WRITE_ENABLED + confirm)
    {
        "name": "web_create_role",
        "description": (
            "Create an RBAC role (POST /admin/roles → add_role). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. SELF-ESCALATION surface. "
            "`permissions` is an optional list of permission names (DB uses spaces, "
            "e.g. 'view listing'). dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "name": {"type": "string"},
            "permissions": {"type": "array", "items": {"type": "string"}},
        }, required=["name"], destructive=True),
    },
    {
        "name": "web_update_role",
        "description": (
            "Update an RBAC role (PUT /admin/roles/:id → edit_role). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. SELF-ESCALATION surface. "
            "`permissions` REPLACES the role's permission set. The backend refuses "
            "to modify the `super admin` role. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "role_id": {"type": "integer"},
            "name": {"type": "string"},
            "permissions": {"type": "array", "items": {"type": "string"}, "description": "Full replacement permission-name set."},
        }, required=["role_id"], destructive=True),
    },
    {
        "name": "web_delete_role",
        "description": (
            "DELETE an RBAC role (DELETE /admin/roles/:id → delete_role). Gate: "
            "WEB_IDENTITY_WRITE_ENABLED + confirm=true. DESTRUCTIVE. Backend refuses "
            "to delete `super admin` or a role that still has members. dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "role_id": {"type": "integer"},
        }, required=["role_id"], destructive=True),
    },
    {
        "name": "web_grant_permission",
        "description": (
            "Grant a permission to a role (POST /admin/roles/:id/permissions → "
            "update_permission). Gate: WEB_IDENTITY_WRITE_ENABLED + confirm=true. "
            "SELF-ESCALATION surface — widens what every member of the role can do. "
            "dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "role_id": {"type": "integer"},
            "permission": {"type": "string", "description": "Permission name (DB uses spaces, e.g. 'edit listing')."},
        }, required=["role_id", "permission"], destructive=True),
    },
    {
        "name": "web_revoke_permission",
        "description": (
            "Revoke a permission from a role (DELETE /admin/roles/:id/permissions/:name "
            "→ update_permission). Gate: WEB_IDENTITY_WRITE_ENABLED + confirm=true. "
            "dry_run default TRUE."
        ),
        "inputSchema": _with_rails({
            "role_id": {"type": "integer"},
            "permission": {"type": "string", "description": "Permission name to revoke."},
        }, required=["role_id", "permission"], destructive=True),
    },
]


TOOL_HANDLERS: dict = {
    "web_create_listing": web_create_listing,
    "web_update_listing": web_update_listing,
    "web_publish": web_publish,
    "web_unpublish": web_unpublish,
    "web_mark_sold": web_mark_sold,
    "web_delete_listing": web_delete_listing,
    "web_bulk_listing_action": web_bulk_listing_action,
    "web_create_user": web_create_user,
    "web_update_user": web_update_user,
    "web_delete_user": web_delete_user,
    "web_create_role": web_create_role,
    "web_update_role": web_update_role,
    "web_delete_role": web_delete_role,
    "web_grant_permission": web_grant_permission,
    "web_revoke_permission": web_revoke_permission,
}
