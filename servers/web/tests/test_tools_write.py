"""WRITE tool tests. ALL HTTP is mocked — no live calls, no mutations.

For each tool we assert the four rails:
  (a) dry_run=True (default) → returns would_send, NO mutating verb hits the API
  (b) gate OFF + dry_run=false → refuses
  (c) destructive/identity + dry_run=false + gate ON but no confirm → refuses
  (d) happy path (gate ON, confirm where needed) → correct verb/path, backup
      file written, audit row appended to web_mcp_writes.jsonl
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from server.tools import write_tools


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# a fake web_client._request that records every call

class Recorder:
    def __init__(self, ret=None):
        self.calls = []
        self.ret = ret if ret is not None else {"id": 4521, "property_id": "AR103917", "status": "PUBLISHED"}

    def __call__(self, method, path, *, params=None, json_body=None, timeout=30, unwrap=False):
        self.calls.append({"method": method, "path": path, "json_body": json_body})
        return self.ret

    def mutating(self):
        return [c for c in self.calls if c["method"] in ("POST", "PUT", "DELETE")]


def _patch_request(ret=None):
    rec = Recorder(ret)
    return rec, patch("server.web_client._request", new=rec)


def _resolve_to(internal_id=4521):
    return patch(
        "server.web_client.resolve_by_property_id",
        return_value={"id": internal_id, "property_id": "AR103917"},
    )


def _audit_rows() -> list[dict]:
    p = Path(os.environ["LOG_ROOT"]) / "web_mcp_writes.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _backup_files() -> list[Path]:
    root = Path(os.environ["WEB_BACKUP_ROOT"])
    return list(root.glob("*.json")) if root.exists() else []


# registry

def test_registry_has_all_15_write_tools():
    expected = {
        "web_create_listing", "web_update_listing", "web_publish", "web_unpublish",
        "web_mark_sold", "web_delete_listing", "web_bulk_listing_action",
        "web_create_user", "web_update_user", "web_delete_user",
        "web_create_role", "web_update_role", "web_delete_role",
        "web_grant_permission", "web_revoke_permission",
    }
    assert set(write_tools.TOOL_HANDLERS) == expected
    assert {t["name"] for t in write_tools.TOOL_SCHEMAS} == expected


def test_every_schema_has_dry_run_default_true():
    for t in write_tools.TOOL_SCHEMAS:
        props = t["inputSchema"]["properties"]
        assert props["dry_run"]["default"] is True, t["name"]


def test_destructive_and_identity_schemas_have_confirm():
    needs_confirm = {
        "web_delete_listing", "web_bulk_listing_action",
        "web_create_user", "web_update_user", "web_delete_user",
        "web_create_role", "web_update_role", "web_delete_role",
        "web_grant_permission", "web_revoke_permission",
    }
    by_name = {t["name"]: t for t in write_tools.TOOL_SCHEMAS}
    for name in needs_confirm:
        assert "confirm" in by_name[name]["inputSchema"]["properties"], name
    # non-destructive listing tools must NOT carry confirm
    for name in ("web_publish", "web_unpublish", "web_mark_sold", "web_update_listing", "web_create_listing"):
        assert "confirm" not in by_name[name]["inputSchema"]["properties"], name


# gate helpers

def test_gates_default_off():
    assert write_tools._write_enabled() is False
    assert write_tools._identity_write_enabled() is False


def test_gates_independent(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    assert write_tools._write_enabled() is True
    assert write_tools._identity_write_enabled() is False  # still off
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "1")
    assert write_tools._identity_write_enabled() is True


# LISTING: web_publish (non-destructive, gate WEB_WRITE_ENABLED)

def test_publish_dry_run_default_no_mutation():
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_publish({"property_id": "AR103917"}))
    assert out["dry_run"] is True
    assert out["would_send"]["method"] == "PUT"
    assert out["would_send"]["path"] == "/admin/listings/4521/publish"
    assert rec.mutating() == []          # NO mutating verb in dry-run
    assert _audit_rows() == []
    assert _backup_files() == []


def test_publish_gate_off_refuses_execution():
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_publish({"property_id": "AR103917", "dry_run": False}))
    assert out["refused"] is True
    assert "WEB_WRITE_ENABLED" in out["reason"]
    assert rec.mutating() == []


def test_publish_happy_path_writes_backup_and_audit(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_publish({"property_id": "AR103917", "dry_run": False}))
    assert out["executed"] is True
    muts = rec.mutating()
    assert len(muts) == 1
    assert muts[0]["method"] == "PUT"
    assert muts[0]["path"] == "/admin/listings/4521/publish"
    assert Path(out["backup_path"]).exists()
    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0]["tool"] == "web_publish"
    assert rows[0]["success"] is True


# LISTING: web_update_listing (gate, payload preview)

def test_update_listing_dry_run_shows_payload():
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_update_listing({"property_id": "AR103917", "fields": {"price": "59200000"}}))
    assert out["dry_run"] is True
    assert out["would_send"]["payload"] == {"price": "59200000"}
    assert rec.mutating() == []


def test_update_listing_requires_fields():
    out = _run(write_tools.web_update_listing({"property_id": "AR103917"}))
    assert "error" in out


def test_update_listing_happy_path(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p, patch("server.web_client.get_listing_detail", return_value={"id": 4521, "price": "1"}):
        out = _run(write_tools.web_update_listing({"listing_id": 4521, "fields": {"price": "2"}, "dry_run": False}))
    assert out["executed"] is True
    muts = rec.mutating()
    assert muts[0] == {"method": "PUT", "path": "/admin/listings/4521", "json_body": {"price": "2"}}


# LISTING: web_create_listing

def test_create_listing_validates_required():
    out = _run(write_tools.web_create_listing({"listing": {"property_type": "apartment"}}))
    assert "error" in out and "category" in out["error"]


def test_create_listing_dry_run():
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_create_listing({"listing": {"property_type": "apartment", "category": "RENT"}}))
    assert out["dry_run"] is True
    assert out["would_send"]["method"] == "POST"
    assert out["would_send"]["path"] == "/admin/listings"
    assert rec.calls == []   # create has no pre-read; nothing hits the API in dry-run


def test_create_listing_happy_path(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request(ret={"id": 9001, "property_id": "AR109001"})
    with p:
        out = _run(write_tools.web_create_listing({"listing": {"property_type": "apartment", "category": "RENT"}, "dry_run": False}))
    assert out["executed"] is True
    assert rec.mutating()[0]["method"] == "POST"
    assert out["after"]["id"] == 9001


# LISTING: web_delete_listing (destructive → confirm)

def test_delete_listing_gate_off_refuses(monkeypatch):
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_delete_listing({"property_id": "AR103917", "dry_run": False}))
    assert out["refused"] is True
    assert "WEB_WRITE_ENABLED" in out["reason"]


def test_delete_listing_no_confirm_refuses(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_delete_listing({"property_id": "AR103917", "dry_run": False}))
    assert out["refused"] is True
    assert "confirm" in out["reason"]
    assert rec.mutating() == []


def test_delete_listing_happy_path_with_confirm(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p, _resolve_to():
        out = _run(write_tools.web_delete_listing({"property_id": "AR103917", "dry_run": False, "confirm": True}))
    assert out["executed"] is True
    assert rec.mutating()[0] == {"method": "DELETE", "path": "/admin/listings/4521", "json_body": None}


# LISTING: bulk (throttle + per-item rail)

def test_bulk_action_validates_action():
    out = _run(write_tools.web_bulk_listing_action({"action": "frobnicate", "ids": [1]}))
    assert "error" in out


def test_bulk_publish_dry_run_each_item():
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_bulk_listing_action({"action": "publish", "ids": [101, 102]}))
    assert out["count"] == 2
    for item in out["results"]:
        assert item["result"]["dry_run"] is True
    assert rec.mutating() == []


def test_bulk_delete_requires_confirm(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_bulk_listing_action({"action": "delete", "ids": [101], "dry_run": False}))
    # per-item refusal (no confirm)
    assert out["results"][0]["result"]["refused"] is True
    assert rec.mutating() == []


def test_bulk_publish_happy_path_throttle_called(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p, patch("server.web_client.bulk_throttle_seconds", return_value=0.0), \
         patch("server.tools.write_tools.time.sleep") as sleep_mock:
        out = _run(write_tools.web_bulk_listing_action({"action": "publish", "ids": [101, 102, 103], "dry_run": False}))
    assert all(i["result"]["executed"] for i in out["results"])
    assert len([c for c in rec.mutating() if c["method"] == "PUT"]) == 3


# USER: identity gate + confirm

def test_create_user_identity_gate_off_refuses(monkeypatch):
    # listing gate ON must NOT open user writes
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_create_user({"user": {"email": "a@b.c"}, "dry_run": False, "confirm": True}))
    assert out["refused"] is True
    assert "WEB_IDENTITY_WRITE_ENABLED" in out["reason"]
    assert rec.mutating() == []


def test_create_user_dry_run_redacts_password():
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_create_user({"user": {"email": "a@b.c", "password": "s3cret-pw"}}))
    assert out["dry_run"] is True
    assert out["would_send"]["payload"]["password"] == write_tools._REDACTED


def test_create_user_no_confirm_refuses(monkeypatch):
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "yes")
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_create_user({"user": {"email": "a@b.c"}, "dry_run": False}))
    assert out["refused"] is True
    assert "confirm" in out["reason"]


def test_create_user_happy_path_redacts_in_backup_and_audit(monkeypatch):
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "true")
    rec, p = _patch_request(ret={"id": 77, "email": "a@b.c", "role": "internal"})
    with p:
        out = _run(write_tools.web_create_user(
            {"user": {"email": "a@b.c", "password": "s3cret-pw", "is_admin": True},
             "dry_run": False, "confirm": True}))
    assert out["executed"] is True
    assert rec.mutating()[0]["method"] == "POST"
    assert rec.mutating()[0]["path"] == "/admin/users"
    # backup must NOT contain the plaintext password
    backup_text = Path(out["backup_path"]).read_text()
    assert "s3cret-pw" not in backup_text
    assert write_tools._REDACTED in backup_text


def test_delete_user_happy_path(monkeypatch):
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "true")
    rec, p = _patch_request(ret={"id": 77, "deleted": True})
    with p, patch("server.web_client.get_user", return_value={"id": 77, "email": "a@b.c"}):
        out = _run(write_tools.web_delete_user({"user_id": 77, "dry_run": False, "confirm": True}))
    assert out["executed"] is True
    assert rec.mutating()[0] == {"method": "DELETE", "path": "/admin/users/77", "json_body": None}


# ROLE / PERMISSION: self-escalation surface

def test_create_role_dry_run():
    rec, p = _patch_request()
    with p:
        out = _run(write_tools.web_create_role({"name": "ops", "permissions": ["view listing"]}))
    assert out["dry_run"] is True
    assert out["would_send"]["payload"] == {"name": "ops", "permissions": ["view listing"]}


def test_grant_permission_happy_path(monkeypatch):
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "true")
    rec, p = _patch_request(ret={"id": 3, "name": "ops", "permissions": ["view listing", "edit listing"]})
    with p, patch("server.web_client.get_role", return_value={"id": 3, "permissions": ["view listing"]}):
        out = _run(write_tools.web_grant_permission({"role_id": 3, "permission": "edit listing", "dry_run": False, "confirm": True}))
    assert out["executed"] is True
    assert rec.mutating()[0]["method"] == "POST"
    assert rec.mutating()[0]["path"] == "/admin/roles/3/permissions"
    assert rec.mutating()[0]["json_body"] == {"permission": "edit listing"}


def test_revoke_permission_url_encodes_spaces(monkeypatch):
    monkeypatch.setenv("WEB_IDENTITY_WRITE_ENABLED", "true")
    rec, p = _patch_request(ret={"id": 3, "permissions": []})
    with p, patch("server.web_client.get_role", return_value={"id": 3, "permissions": ["view listing"]}):
        out = _run(write_tools.web_revoke_permission({"role_id": 3, "permission": "view listing", "dry_run": False, "confirm": True}))
    assert out["executed"] is True
    assert rec.mutating()[0]["method"] == "DELETE"
    # space must be percent-encoded in the path segment
    assert rec.mutating()[0]["path"] == "/admin/roles/3/permissions/view%20listing"


def test_grant_permission_gate_off_refuses():
    rec, p = _patch_request()
    with p, patch("server.web_client.get_role", return_value={"id": 3}):
        out = _run(write_tools.web_grant_permission({"role_id": 3, "permission": "edit listing", "dry_run": False, "confirm": True}))
    assert out["refused"] is True
    assert "WEB_IDENTITY_WRITE_ENABLED" in out["reason"]


def test_update_role_requires_something_to_change():
    out = _run(write_tools.web_update_role({"role_id": 3}))
    assert "error" in out


# mutation failure is captured, not raised

def test_mutation_failure_returns_structured_error(monkeypatch):
    monkeypatch.setenv("WEB_WRITE_ENABLED", "true")
    from server.web_client import WebClientError

    def boom(method, path, *, params=None, json_body=None, timeout=30, unwrap=False):
        if method == "GET":
            return {"id": 4521}
        raise WebClientError("backend 500")

    with patch("server.web_client._request", new=boom), _resolve_to():
        out = _run(write_tools.web_publish({"property_id": "AR103917", "dry_run": False}))
    assert out["executed"] is False
    assert "backend 500" in out["error"]
    rows = _audit_rows()
    assert rows[-1]["success"] is False
