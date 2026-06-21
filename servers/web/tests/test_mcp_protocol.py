"""MCP protocol-level tests — initialize, tools/list, tools/call dispatch."""
from __future__ import annotations

from unittest.mock import patch


def _post(client, headers, **body):
    
    payload = {"jsonrpc": "2.0", "id": 1, **body}
    return client.post("/mcp", json=payload, headers=headers)


# initialize

def test_initialize_returns_protocol_version(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="initialize", params={})
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in body["result"]["capabilities"]
    assert body["result"]["serverInfo"]["name"] == "cb-web-mcp-server"


# tools/list

def test_tools_list_returns_read_tools(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="tools/list")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "web_get_listing" in names
    assert "web_search_listings" in names
    assert "web_list_listings" in names
    assert "web_get_translations" in names
    assert "refresh_cache" in names


def test_tools_list_includes_write_tools_phase3(test_client, auth_headers):
    """Write tools are ALWAYS registered/discoverable (so a gated tool
    can refuse with a clear message at call-time rather than read as 'unknown
    tool'). Gating happens in the handler, not by hiding the schema."""
    r = _post(test_client, auth_headers, method="tools/list")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    for w in ("web_create_listing", "web_update_listing", "web_publish",
              "web_unpublish", "web_mark_sold", "web_delete_listing",
              "web_create_user", "web_delete_user", "web_create_role",
              "web_grant_permission", "web_revoke_permission"):
        assert w in names


def test_tools_list_schemas_have_required_fields(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="tools/list")
    for tool in r.json()["result"]["tools"]:
        assert "name" in tool
        assert "description" in tool
        assert tool["inputSchema"].get("type") == "object"


# tools/call

def test_tools_call_unknown_tool_returns_error(test_client, auth_headers):
    r = _post(
        test_client, auth_headers,
        method="tools/call",
        params={"name": "nonexistent_tool", "arguments": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "nonexistent_tool" in body["error"]["message"]


def test_tools_call_handler_exception_returns_isError(test_client, auth_headers):
    from server.main import TOOL_HANDLERS
    async def crashing(*a, **kw):
        raise RuntimeError("simulated tool crash")
    with patch.dict(TOOL_HANDLERS, {"web_get_listing": crashing}):
        r = _post(
            test_client, auth_headers,
            method="tools/call",
            params={"name": "web_get_listing", "arguments": {"property_id": "AR1"}},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["isError"] is True
    assert "simulated tool crash" in body["result"]["content"][0]["text"]


# unknown method / notifications / malformed / batch

def test_unknown_method_returns_error(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="resources/list")
    assert r.json()["error"]["code"] == -32601


def test_notifications_method_no_op(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="notifications/initialized")
    assert r.status_code == 200
    assert "result" in r.json()


def test_invalid_json_returns_400(test_client, auth_headers):
    r = test_client.post(
        "/mcp",
        data="not-valid-json",
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_batch_request_handled(test_client, auth_headers):
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    ]
    r = test_client.post("/mcp", json=payload, headers=auth_headers)
    assert r.status_code == 200
    responses = r.json()
    assert {resp["id"] for resp in responses} == {1, 2}
