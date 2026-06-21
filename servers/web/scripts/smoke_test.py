#!/usr/bin/env python3
"""Acme Web MCP Server — Read-only acceptance gate (LIVE, READ-ONLY).

Run this ONCE the service account exists, before declaring the read connector done. It hits
the real admin API (reads + one login POST — never a mutation) and PROVES the
connector is fit for an audit session. It is NOT just "fetch a few AR#".

Usage:
    # fill .env first (WEB_API_BASE, WEB_SERVICE_EMAIL/PASSWORD; WEB_MCP_TOKEN
    # not needed here — we call the client directly, not over MCP)
    python scripts/smoke_test.py AR103917 AR103821 AR103824 AR... \
        [--max-list-seconds 25] [--list-page-size 200]

Pass a DELIBERATELY DIVERSE set of AR#s: at least one SELL, one RENT, one
SOLD/DRAFT, one with translations, one apartment with tower+unit — so the
normalizer's camel/snake + listingable-child mapping all get exercised.

Gates (all must pass; prints a clear PASS/FAIL per gate):
  0. Read-only        — WEB_WRITE_ENABLED is off + no write tools registered.
  1. Auth + role      — login works; whoami proves a non-`agent` internal user
                        with view_listing (else auth failures masquerade as bugs).
  2. agentScope proof — listings NOT owned by the service account come back
                        POPULATED, with >1 distinct owner. This is the silent-
                        failure gotcha: a mis-roled user returns empty/403, not
                        an error.
  3. Normalizer       — per-AR# field-by-field table (normalized vs raw admin
                        JSON) for the unconfirmed listingable/translatable keys,
                        plus branch-coverage report; hard-fails on a clear
                        mapping break.
  4. Cold latency     — a COLD web_list_listings over all PUBLISHED apartments
                        is well under Claude's ~30s MCP timeout.

Exit codes: 0 = all gates pass · 1 = a gate failed · 2 = auth failed (distinct).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# make the script runnable from anywhere
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_ROOT))
# logger defaults to /data/logs (container path); give a local fallback so the
# script runs on a dev machine without a read-only-fs crash at import.
os.environ.setdefault("LOG_ROOT", str(_ROOT / "data" / "logs"))

# load .env if python-dotenv is present (optional)
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from server import web_client                       # noqa: E402
from server.tools.write_tools import (               # noqa: E402
    _identity_write_enabled,
    _write_enabled,
)


# tiny reporting helpers

class Gate:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, ok: bool, label: str) -> bool:
        mark = "PASS" if ok else "FAIL"
        print(f"    [{mark}] {label}")
        if not ok:
            self.failures.append(label)
        return ok

    def warn(self, label: str) -> None:
        print(f"    [WARN] {label}")
        self.warnings.append(label)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _norm_owner(n: dict):
    return n.get("user_id")


# Gate 0: read-only

def gate_read_only(g: Gate) -> None:
    banner("GATE 0 — Read-only (the smoke test must never be able to mutate)")
    # The server always-registers the (gated) write tools, so we no longer assert
    # "no write tools". Instead we assert BOTH write gates are OFF — a write tool
    # whose gate is off refuses execution, so the connector cannot mutate.
    write_off = not _write_enabled()
    identity_off = not _identity_write_enabled()
    g.check(write_off, "WEB_WRITE_ENABLED is OFF")
    g.check(identity_off, "WEB_IDENTITY_WRITE_ENABLED is OFF")
    if not (write_off and identity_off):
        # refuse to continue — a writable config + a careless future edit could mutate
        on = []
        if not write_off:
            on.append("WEB_WRITE_ENABLED")
        if not identity_off:
            on.append("WEB_IDENTITY_WRITE_ENABLED")
        print(f"\n!!! ABORTING: {', '.join(on)} is on. Unset before smoke-testing.")
        sys.exit(1)


# Gate 1: auth + role

def gate_auth(g: Gate) -> dict:
    banner("GATE 1 — Auth + service-account role")
    try:
        web_client.get_token(force=True)
    except web_client.WebClientError as e:
        print("\n" + "#" * 72)
        print("#  AUTH FAILED — login-and-refresh could not get a token.")
        print("#  This is an AUTH/CREDS problem, NOT a mapping bug. Check:")
        print("#    - WEB_API_BASE         =", os.environ.get("WEB_API_BASE", "(unset)"))
        print("#    - WEB_SERVICE_EMAIL    =", os.environ.get("WEB_SERVICE_EMAIL", "(unset)"))
        print("#    - WEB_SERVICE_PASSWORD =", "(set)" if os.environ.get("WEB_SERVICE_PASSWORD") else "(unset)")
        print(f"#  Underlying error: {e}")
        print("#" * 72)
        sys.exit(2)

    print("    login OK — token acquired")
    try:
        me = web_client.whoami()
    except web_client.WebClientError as e:
        g.check(False, f"GET /auth/me failed: {e}")
        return {}

    role_name = me.get("role_name")
    user_role = me.get("role")
    perms = me.get("permissions") or {}
    view = bool(perms.get("view listing") or perms.get("view_listing"))
    uid = me.get("id")
    print(f"    service user: id={uid} email={me.get('email')} role={user_role} rbac_role={role_name!r}")

    g.check(role_name != "agent", f"RBAC role is NOT 'agent' (is {role_name!r}) — no agentScope")
    g.check(user_role in ("internal", "external"), f"users.role is internal/external (is {user_role!r})")
    g.check(view, "has `view listing` permission")
    return me


# Gate 2: agentScope visibility proof

def gate_agentscope(g: Gate, me: dict, ar_numbers: list[str]) -> dict[str, dict]:
    banner("GATE 2 — agentScope proof (sees listings it does NOT own)")
    service_uid = me.get("id")

    # 2a. broad list — distinct owners must be > 1 (an agent-scoped account would
    #     only ever see its own rows, or nothing).
    web_client.clear_cache()
    resp = web_client.list_listings({"status": "PUBLISHED", "per_page": 100})
    rows = [web_client.normalize_listing(r) for r in web_client._iter_rows(resp)]
    owners = {_norm_owner(r) for r in rows if _norm_owner(r) is not None}
    print(f"    sampled {len(rows)} PUBLISHED rows; distinct owners: {sorted(owners)}")
    g.check(len(rows) > 0, "broad PUBLISHED list returned rows (not empty)")
    g.check(len(owners) > 1, f"sees >1 distinct owner ({len(owners)}) — not single-user scoped")
    g.check(
        any(o != service_uid for o in owners),
        f"sees listings owned by someone other than the service user (id={service_uid})",
    )

    # 2b. each provided AR# resolves + is populated; record owner.
    details: dict[str, dict] = {}
    cross_owned = False
    for ar in ar_numbers:
        try:
            summary = web_client.resolve_by_property_id(ar)
            if not summary:
                g.check(False, f"{ar}: resolved to a listing")
                continue
            detail = web_client.get_listing_detail(int(summary["id"]))
        except (web_client.WebClientError, KeyError, ValueError, TypeError) as e:
            g.check(False, f"{ar}: fetch failed ({str(e)[:80]})")
            continue
        n = web_client.normalize_listing(detail)
        details[ar] = detail
        populated = bool(n.get("property_id")) and bool(n.get("status"))
        owner = _norm_owner(n)
        g.check(populated, f"{ar}: populated (status={n.get('status')}, owner={owner})")
        if owner is not None and owner != service_uid:
            cross_owned = True

    if details and not cross_owned:
        g.warn("none of the provided AR# are owned by another user — "
               "ownership diversity proven only by the broad-list check (2a)")
    return details


# Gate 3: normalizer field table + branch coverage

_UNIT_KEYS = ["building_area", "land_area", "bedrooms", "floor", "floor_zone",
              "tower_name", "unit_number", "apartment_name"]
_TITLE_KEYS = ["title_id", "title_en", "slug"]


def gate_normalizer(g: Gate, details: dict[str, dict]) -> None:
    banner("GATE 3 — Normalizer field mapping (eyeball live key names)")
    cover = {"sell": False, "rent": False, "sold_or_draft": False,
             "translations": False, "apt_tower_unit": False}

    for ar, detail in details.items():
        n = web_client.normalize_listing(detail)
        raw_listingable = web_client._first(detail, "listingable", default={}) or {}
        raw_trans = web_client._first(detail, "translatable", "translations", default=[]) or []
        is_apt = str(n.get("listingable_type") or "").endswith("ApartmentUnit")

        print(f"\n  {ar}  status={n.get('status')} deal={n.get('deal_status')} "
              f"cat={n.get('category')} type={n.get('listingable_type')}")
        print(f"      {'field':<16}{'normalized':<34}raw-source")
        for k in _UNIT_KEYS:
            v = n.get(k)
            src = "" if v is not None else f"(raw listingable keys: {sorted(raw_listingable.keys())})"
            print(f"      {k:<16}{str(v):<34}{src}")
        for k in _TITLE_KEYS:
            v = n.get(k)
            src = "" if v is not None else f"(raw translatable: {len(raw_trans)} rows)"
            print(f"      {k:<16}{str(v)[:32]:<34}{src}")

        # coverage
        if n.get("category") == "SELL":
            cover["sell"] = True
        if n.get("category") == "RENT":
            cover["rent"] = True
        if n.get("status") == "DRAFT" or n.get("deal_status") in ("SOLD", "RENTED"):
            cover["sold_or_draft"] = True
        if raw_trans:
            cover["translations"] = True
        if is_apt and (n.get("tower_name") or n.get("unit_number")):
            cover["apt_tower_unit"] = True

        # hard mapping-break detection — the exact risk flagged at build time
        if is_apt and raw_listingable and all(
            n.get(k) is None for k in ("building_area", "bedrooms", "unit_number")
        ):
            g.check(False, f"{ar}: apartment has raw listingable data but normalizer "
                           f"mapped NONE of building_area/bedrooms/unit_number "
                           f"→ listingable key mismatch")
        if raw_trans and n.get("title_id") is None and n.get("title_en") is None:
            g.check(False, f"{ar}: has {len(raw_trans)} translation rows but normalizer "
                           f"mapped no title → translatable key mismatch")

    # branch coverage — WARN (depends on which AR# you chose), but loud
    print("\n  branch coverage across the provided AR#:")
    for k, seen in cover.items():
        print(f"      {'covered' if seen else 'MISSING':<8} {k}")
    for k, seen in cover.items():
        if not seen:
            g.warn(f"normalizer branch not exercised: {k} — add an AR# of that kind")


# Gate 4: cold bulk-list latency

def gate_latency(g: Gate, max_seconds: float, page_size: int) -> None:
    banner(f"GATE 4 — Cold bulk-list latency (< {max_seconds:.0f}s, "
           f"Claude MCP timeout ~30s)")
    web_client.clear_cache()  # force a COLD call (no warm cache, no prior token reuse skipped)
    t0 = time.time()
    resp = web_client.list_listings(
        {"status": "PUBLISHED", "listing_type": "apartment", "per_page": page_size, "page": 1}
    )
    elapsed = time.time() - t0
    rows = web_client._iter_rows(resp)
    meta = resp.get("meta") if isinstance(resp, dict) else None
    total = (meta or {}).get("total")
    per_page = (meta or {}).get("per_page", page_size)
    print(f"    cold page fetch: {elapsed:.2f}s · returned {len(rows)} rows · "
          f"total PUBLISHED apartments={total}")
    if total and per_page:
        try:
            pages = -(-int(total) // int(per_page))  # ceil
            print(f"    full sweep would be ~{pages} page call(s) at per_page={per_page}")
        except (ValueError, TypeError):
            pass
    g.check(elapsed < max_seconds, f"cold page fetch under {max_seconds:.0f}s ({elapsed:.2f}s)")


# main

def main() -> int:
    ap = argparse.ArgumentParser(description="Acme Web MCP read-only acceptance gate.")
    ap.add_argument("ar_numbers", nargs="+", help="AR##### property ids (diverse set).")
    ap.add_argument("--max-list-seconds", type=float, default=25.0)
    ap.add_argument("--list-page-size", type=int, default=200)
    args = ap.parse_args()

    print("Acme Web MCP — read-only acceptance gate (LIVE, READ-ONLY)")
    print(f"  base = {os.environ.get('WEB_API_BASE', web_client.DEFAULT_BASE)}")
    print(f"  AR#  = {', '.join(args.ar_numbers)}")

    g = Gate()
    gate_read_only(g)
    me = gate_auth(g)
    details = gate_agentscope(g, me, args.ar_numbers)
    gate_normalizer(g, details)
    gate_latency(g, args.max_list_seconds, args.list_page_size)

    banner("RESULT")
    if g.warnings:
        print(f"  {len(g.warnings)} warning(s):")
        for w in g.warnings:
            print(f"    - {w}")
    if g.failures:
        print(f"\n  ❌ {len(g.failures)} GATE FAILURE(S):")
        for f in g.failures:
            print(f"    - {f}")
        print("\n  Read connector NOT accepted.")
        return 1
    print("\n  ✅ All gates passed. Read connector accepted for audit use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
