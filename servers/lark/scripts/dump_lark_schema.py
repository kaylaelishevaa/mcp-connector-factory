"""One-off Lark schema dump.

Usage on droplet:
    docker compose exec lark-mcp python3 /app/scripts/dump_lark_schema.py > /tmp/schema.json

Then copy /tmp/schema.json back to Mac:
    scp deploy@203.0.113.10:/tmp/schema.json ~/Downloads/

Outputs full table+field schema as JSON for implementation planning.
NO record data fetched (just metadata). Safe to run anytime.
"""
from __future__ import annotations

import json
import os
import sys

import requests

BASE_URL = "https://open.larksuite.com/open-apis"


def get_tenant_token(app_id: str, app_secret: str) -> str:
    r = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"token error: {data}")
    return data["tenant_access_token"]


def list_tables(token: str, base_id: str) -> list[dict]:
    """All tables in the Bitable app."""
    out: list[dict] = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{base_id}/tables",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"list_tables error: {data}")
        out.extend(data["data"].get("items", []))
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
        if not page_token:
            break
    return out


def list_fields(token: str, base_id: str, table_id: str) -> list[dict]:
    """All fields in a given table."""
    out: list[dict] = []
    page_token = None
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{base_id}/tables/{table_id}/fields",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            # Some tables may not be field-introspectable; skip gracefully.
            return [{"_error": str(data)}]
        out.extend(data["data"].get("items", []))
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
        if not page_token:
            break
    return out


def get_record_count(token: str, base_id: str, table_id: str) -> int:
    """Approximate record count by fetching page 1 with page_size=1 and reading 'total' if Lark returns it."""
    try:
        r = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{base_id}/tables/{table_id}/records",
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 1},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        # Lark v1 doesn't always return 'total'; fall back to None
        return data.get("data", {}).get("total", -1)
    except Exception:
        return -1


def main() -> int:
    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    base_id = os.environ.get("LARK_BASE_ID", "").strip()

    if not all([app_id, app_secret, base_id]):
        print(json.dumps({"error": "LARK_APP_ID / LARK_APP_SECRET / LARK_BASE_ID missing"}), file=sys.stderr)
        return 1

    token = get_tenant_token(app_id, app_secret)
    tables = list_tables(token, base_id)

    result = {
        "base_id": base_id,
        "table_count": len(tables),
        "tables": [],
    }

    for t in tables:
        table_id = t["table_id"]
        table_name = t["name"]
        revision = t.get("revision")
        fields = list_fields(token, base_id, table_id)
        record_count = get_record_count(token, base_id, table_id)
        result["tables"].append({
            "name": table_name,
            "table_id": table_id,
            "revision": revision,
            "record_count_approx": record_count,
            "field_count": len(fields),
            "fields": [
                {
                    "name": f.get("field_name"),
                    "type": f.get("type"),
                    "ui_type": f.get("ui_type"),
                    "is_primary": f.get("is_primary", False),
                    "description": (f.get("description") or {}).get("text") if isinstance(f.get("description"), dict) else f.get("description"),
                    "property": f.get("property"),  # e.g. dropdown options
                }
                for f in fields
            ],
        })

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
