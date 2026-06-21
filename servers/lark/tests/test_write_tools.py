"""Unit tests for WRITE tools — update_record + create_record.

All tests mock lark_client at the network boundary. Write tools RAISE on any
failure; main.py turns that into an MCP isError=True response (asserted via the
TestClient end-to-end tests). The append-only audit stream
(lark_mcp_writes.jsonl) is asserted by reading it back from the per-test
tmp LOG_ROOT.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from server import lark_client
from server.tools.write_tools import (
    TOOL_HANDLERS,
    TOOL_SCHEMAS,
    create_record_tool,
    update_record_tool,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _writes_log() -> list[dict]:
    path = Path(os.environ["LOG_ROOT"]) / "lark_mcp_writes.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _resolve(name):
    """Fake table-name resolver: Listings/Contacts known, else None."""
    return {
        "listings": "tblLISTINGS00001",
        "contacts": "tblCONTACTS00001",
    }.get((name or "").strip().lower())


# lark_client write plumbing (HTTP status / Lark code / parsing)

class TestLarkClientWriteMethods:
    """Exercise the actual lark_client.update_record/create_record HTTP layer
    (the tool tests above mock these out, so the status/code handling needs
    its own coverage)."""

    @staticmethod
    def _resp(status=200, body=None):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = body if body is not None else {
            "code": 0, "msg": "success",
            "data": {"record": {"record_id": "recX", "fields": {"Lantai": 12}}},
        }
        m.text = json.dumps(body) if body is not None else ""
        return m

    def test_update_success_returns_record_and_puts_fields(self):
        with patch("server.lark_client.get_token", return_value="tok"), \
             patch("server.lark_client.requests.put", return_value=self._resp()) as m_put:
            rec = lark_client.update_record("tblL", "recX", {"Lantai": 12})
        assert rec["record_id"] == "recX"
        assert m_put.call_args.kwargs["json"] == {"fields": {"Lantai": 12}}
        assert "/records/recX" in m_put.call_args.args[0]

    def test_update_http_400_raises(self):
        body = {"code": 1254045, "msg": "FieldNameNotFound"}
        with patch("server.lark_client.get_token", return_value="tok"), \
             patch("server.lark_client.requests.put", return_value=self._resp(400, body)):
            with pytest.raises(lark_client.LarkClientError, match="FieldNameNotFound"):
                lark_client.update_record("tblL", "recX", {"Bogus": 1})

    def test_update_http_200_but_lark_code_nonzero_raises(self):
        """Lark sometimes returns HTTP 200 with a non-zero code — must still raise."""
        body = {"code": 1254043, "msg": "RecordIdNotFound", "data": {}}
        with patch("server.lark_client.get_token", return_value="tok"), \
             patch("server.lark_client.requests.put", return_value=self._resp(200, body)):
            with pytest.raises(lark_client.LarkClientError, match="RecordIdNotFound"):
                lark_client.update_record("tblL", "recMISSING", {"Lantai": 12})

    def test_create_success_returns_new_record(self):
        body = {"code": 0, "msg": "success",
                "data": {"record": {"record_id": "recNEW", "fields": {"Nama Properti": "X"}}}}
        with patch("server.lark_client.get_token", return_value="tok"), \
             patch("server.lark_client.requests.post", return_value=self._resp(200, body)) as m_post:
            rec = lark_client.create_record("tblL", {"Nama Properti": "X"})
        assert rec["record_id"] == "recNEW"
        assert m_post.call_args.kwargs["json"] == {"fields": {"Nama Properti": "X"}}

    def test_create_non_json_error_body_raises(self):
        m = MagicMock()
        m.status_code = 500
        m.json.side_effect = ValueError("not json")
        m.text = "Internal Server Error"
        with patch("server.lark_client.get_token", return_value="tok"), \
             patch("server.lark_client.requests.post", return_value=m):
            with pytest.raises(lark_client.LarkClientError):
                lark_client.create_record("tblL", {"Nama Properti": "X"})


# update_record — happy path

class TestUpdateRecordHappyPath:
    def test_returns_updated_record(self):
        updated = {"record_id": "recX", "fields": {"Lantai": 12}}
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.get_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 14}}), \
             patch("server.lark_client.update_record", return_value=updated) as m_upd:
            res = _run(update_record_tool(
                {"table_name": "Listings", "record_id": "recX", "fields": {"Lantai": 12}}
            ))
        assert res["updated_record"] == updated
        assert res["fields_changed"] == ["Lantai"]
        assert res["record_id"] == "recX"
        assert res["table_name"] == "Listings"
        assert "lark_url" in res
        m_upd.assert_called_once_with("tblLISTINGS00001", "recX", {"Lantai": 12})

    def test_audit_entry_written(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.get_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 14, "Status": "Available"}}), \
             patch("server.lark_client.update_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 12}}):
            _run(update_record_tool(
                {"table_name": "Listings", "record_id": "recX", "fields": {"Lantai": 12}}
            ))
        log = _writes_log()
        assert len(log) == 1
        e = log[0]
        assert e["tool"] == "update_record"
        assert e["table_name"] == "Listings"
        assert e["record_id"] == "recX"
        assert e["fields_after"] == {"Lantai": 12}
        assert e["success"] is True
        assert e["error"] is None
        assert e["auth_token_hash"]  # non-empty

    def test_before_snapshot_only_modified_fields(self):
        """fields_before captures ONLY the keys being changed, not the whole record."""
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.get_record",
                   return_value={"record_id": "recX",
                                 "fields": {"Lantai": 14, "Status": "Available", "Harga Sewa": "5000000"}}), \
             patch("server.lark_client.update_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 12}}):
            _run(update_record_tool(
                {"table_name": "Listings", "record_id": "recX", "fields": {"Lantai": 12}}
            ))
        e = _writes_log()[0]
        assert e["fields_before"] == {"Lantai": 14}  # NOT Status / Harga Sewa


# update_record — failures (raise + audit success=False)

class TestUpdateRecordFailures:
    def test_lark_400_invalid_field_raises_and_audits(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.get_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 14}}), \
             patch("server.lark_client.update_record",
                   side_effect=lark_client.LarkClientError(
                       "update_record failed 400 code=1254045: FieldNameNotFound")):
            with pytest.raises(RuntimeError):
                _run(update_record_tool(
                    {"table_name": "Listings", "record_id": "recX", "fields": {"Bogus": 1}}
                ))
        log = _writes_log()
        assert len(log) == 1
        assert log[0]["success"] is False
        assert "FieldNameNotFound" in log[0]["error"]

    def test_lark_404_record_not_found(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.get_record",
                   side_effect=lark_client.LarkClientError("get_record failed 404")), \
             patch("server.lark_client.update_record",
                   side_effect=lark_client.LarkClientError(
                       "update_record failed 404 code=1254043: RecordIdNotFound")):
            with pytest.raises(RuntimeError):
                _run(update_record_tool(
                    {"table_name": "Listings", "record_id": "recMISSING", "fields": {"Lantai": 12}}
                ))
        e = _writes_log()[0]
        assert e["success"] is False
        assert e["fields_before"] is None  # get_record failed → no snapshot

    def test_unknown_table_raises_before_lark_call(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.update_record") as m_upd:
            with pytest.raises(RuntimeError, match="unknown table"):
                _run(update_record_tool(
                    {"table_name": "Nope", "record_id": "recX", "fields": {"a": 1}}
                ))
        m_upd.assert_not_called()
        assert _writes_log() == []  # no write attempted → no audit entry

    def test_missing_record_id_raises(self):
        with pytest.raises(ValueError, match="record_id required"):
            _run(update_record_tool({"table_name": "Listings", "fields": {"a": 1}}))

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="fields required"):
            _run(update_record_tool(
                {"table_name": "Listings", "record_id": "recX", "fields": {}}
            ))


# create_record

class TestCreateRecord:
    def test_happy_path(self):
        created = {"record_id": "recNEW", "fields": {"Nama Properti": "Apartemen X"}}
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.create_record", return_value=created) as m_create:
            res = _run(create_record_tool(
                {"table_name": "Listings", "fields": {"Nama Properti": "Apartemen X"}}
            ))
        assert res["created_record"] == created
        assert res["record_id"] == "recNEW"
        assert "lark_url" in res
        m_create.assert_called_once_with("tblLISTINGS00001", {"Nama Properti": "Apartemen X"})

    def test_audit_entry_before_none(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.create_record",
                   return_value={"record_id": "recNEW", "fields": {"Nama Properti": "X"}}):
            _run(create_record_tool({"table_name": "Listings", "fields": {"Nama Properti": "X"}}))
        e = _writes_log()[0]
        assert e["tool"] == "create_record"
        assert e["fields_before"] is None
        assert e["fields_after"] == {"Nama Properti": "X"}
        assert e["record_id"] == "recNEW"
        assert e["success"] is True

    def test_missing_primary_field_lark_error_propagated(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.create_record",
                   side_effect=lark_client.LarkClientError(
                       "create_record failed 400 code=1254045: required field missing")):
            with pytest.raises(RuntimeError):
                _run(create_record_tool({"table_name": "Listings", "fields": {"Unit": "x"}}))
        e = _writes_log()[0]
        assert e["success"] is False
        assert e["fields_before"] is None
        assert e["fields_after"] == {"Unit": "x"}

    def test_unknown_table_raises_before_lark_call(self):
        with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
             patch("server.lark_client.create_record") as m_create:
            with pytest.raises(RuntimeError, match="unknown table"):
                _run(create_record_tool({"table_name": "Nope", "fields": {"a": 1}}))
        m_create.assert_not_called()
        assert _writes_log() == []

    def test_empty_fields_raises(self):
        with pytest.raises(ValueError, match="fields required"):
            _run(create_record_tool({"table_name": "Listings", "fields": {}}))


# Kill switch

class TestWriteKillSwitch:
    def test_update_disabled_refuses_without_lark(self, monkeypatch):
        monkeypatch.setenv("LARK_WRITE_ENABLED", "false")
        with patch("server.lark_client.update_record") as m_upd:
            with pytest.raises(RuntimeError, match="disabled"):
                _run(update_record_tool(
                    {"table_name": "Listings", "record_id": "recX", "fields": {"Lantai": 12}}
                ))
        m_upd.assert_not_called()
        assert _writes_log() == []

    def test_create_disabled_refuses_without_lark(self, monkeypatch):
        monkeypatch.setenv("LARK_WRITE_ENABLED", "0")
        with patch("server.lark_client.create_record") as m_create:
            with pytest.raises(RuntimeError, match="disabled"):
                _run(create_record_tool({"table_name": "Listings", "fields": {"Nama Properti": "X"}}))
        m_create.assert_not_called()

    def test_enabled_by_default_and_for_truthy(self, monkeypatch):
        from server.tools.write_tools import _write_enabled
        monkeypatch.delenv("LARK_WRITE_ENABLED", raising=False)
        assert _write_enabled() is True
        monkeypatch.setenv("LARK_WRITE_ENABLED", "true")
        assert _write_enabled() is True
        monkeypatch.setenv("LARK_WRITE_ENABLED", "off")
        assert _write_enabled() is False


# Audit-log assertion across a batch (spec test #15)

def test_five_writes_produce_five_audit_entries():
    with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
         patch("server.lark_client.get_record",
               return_value={"record_id": "recX", "fields": {"Lantai": 14}}):
        with patch("server.lark_client.update_record",
                   return_value={"record_id": "recX", "fields": {"Lantai": 12}}):
            _run(update_record_tool({"table_name": "Listings", "record_id": "r1", "fields": {"Lantai": 12}}))
            _run(update_record_tool({"table_name": "Listings", "record_id": "r2", "fields": {"Lantai": 8}}))
            _run(update_record_tool({"table_name": "Listings", "record_id": "r3", "fields": {"Lantai": 3}}))
        with patch("server.lark_client.create_record",
                   return_value={"record_id": "recNEW", "fields": {"Nama Properti": "X"}}):
            _run(create_record_tool({"table_name": "Listings", "fields": {"Nama Properti": "X"}}))
        with patch("server.lark_client.update_record",
                   side_effect=lark_client.LarkClientError("update_record failed 400 code=1: boom")):
            with pytest.raises(RuntimeError):
                _run(update_record_tool({"table_name": "Listings", "record_id": "r4", "fields": {"Lantai": 1}}))

    log = _writes_log()
    assert len(log) == 5
    assert sum(1 for e in log if e["success"]) == 4
    assert sum(1 for e in log if not e["success"]) == 1


# Registry + healthcheck

def test_write_tools_in_registry():
    assert set(TOOL_HANDLERS) == {"update_record", "create_record"}
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"update_record", "create_record"}


def test_total_tool_count_17():
    """15 read tools + update_record + create_record = 17 in the merged dispatch."""
    from server.main import TOOL_HANDLERS as ALL_HANDLERS
    assert len(ALL_HANDLERS) == 17
    assert "update_record" in ALL_HANDLERS
    assert "create_record" in ALL_HANDLERS


def test_healthcheck_advertises_write(test_client):
    body = test_client.get("/").json()
    assert body["tools_count"] == 17
    assert body["write_enabled"] is True


def test_update_description_directs_to_write_workflow():
    desc = next(t["description"] for t in TOOL_SCHEMAS if t["name"] == "update_record")
    assert "describe_table" in desc
    assert "do NOT reply that you can't" in desc


# End-to-end isError via the MCP endpoint

def _mcp_call(client, headers, name, arguments):
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def test_mcp_update_failure_is_error_true(test_client, auth_headers):
    with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
         patch("server.lark_client.get_record",
               return_value={"record_id": "recX", "fields": {"Lantai": 14}}), \
         patch("server.lark_client.update_record",
               side_effect=lark_client.LarkClientError("update_record failed 400 code=1: bad field")):
        resp = _mcp_call(test_client, auth_headers, "update_record",
                         {"table_name": "Listings", "record_id": "recX", "fields": {"Bad": 1}})
    body = resp.json()
    assert body["result"]["isError"] is True
    assert "failed" in body["result"]["content"][0]["text"].lower()


def test_mcp_update_success_is_error_false(test_client, auth_headers):
    with patch("server.lark_client.resolve_table_name", side_effect=_resolve), \
         patch("server.lark_client.get_record",
               return_value={"record_id": "recX", "fields": {"Lantai": 14}}), \
         patch("server.lark_client.update_record",
               return_value={"record_id": "recX", "fields": {"Lantai": 12}}):
        resp = _mcp_call(test_client, auth_headers, "update_record",
                         {"table_name": "Listings", "record_id": "recX", "fields": {"Lantai": 12}})
    body = resp.json()
    assert body["result"]["isError"] is False


# Live integration (manual — needs a real test base)

@pytest.mark.skip(reason="live: needs LARK_BASE_ID=<test base> + write scope; run manually post-deploy")
def test_live_update_round_trip():
    """Real PATCH to a test base record → verify Lark returned the new value."""


@pytest.mark.skip(reason="live: needs LARK_BASE_ID=<test base> + write scope; run manually post-deploy")
def test_live_create_round_trip():
    """Real POST to a test base table → verify record created with returned record_id."""
