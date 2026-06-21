"""MCP protocol-level tests — initialize, tools/list, tools/call dispatch."""
from __future__ import annotations

from unittest.mock import patch


def _post(client, headers, **body):
    """Helper: POST JSON-RPC envelope to /mcp."""
    payload = {"jsonrpc": "2.0", "id": 1, **body}
    r = client.post("/mcp", json=payload, headers=headers)
    return r


# initialize

def test_initialize_returns_protocol_version(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="initialize", params={})
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "result" in body
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in body["result"]["capabilities"]
    assert body["result"]["serverInfo"]["name"] == "lark-mcp-server"


# tools/list

def test_tools_list_returns_all_tools(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="tools/list")
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    # these 3 are always present
    assert "search_listings" in names
    assert "get_listing" in names
    assert "find_activities" in names


def test_tools_list_schemas_have_required_fields(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="tools/list")
    for tool in r.json()["result"]["tools"]:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
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
    assert "error" in body
    assert body["error"]["code"] == -32602  # invalid params
    assert "nonexistent_tool" in body["error"]["message"]


def test_tools_call_handler_exception_returns_isError(test_client, auth_headers):
    """If a tool handler raises, result has isError=true (not HTTP error)."""
    # main.py owns the merged read+write dispatch table, so patch THAT object
    # (read_tools.TOOL_HANDLERS is no longer the same dict main dispatches on).
    from server.main import TOOL_HANDLERS
    async def crashing(*a, **kw):
        raise RuntimeError("simulated tool crash")
    with patch.dict(TOOL_HANDLERS, {"search_listings": crashing}):
        r = _post(
            test_client, auth_headers,
            method="tools/call",
            params={"name": "search_listings", "arguments": {}},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["isError"] is True
    assert "simulated tool crash" in body["result"]["content"][0]["text"]


# unknown method

def test_unknown_method_returns_error(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="resources/list")
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32601


def test_notifications_method_no_op(test_client, auth_headers):
    r = _post(test_client, auth_headers, method="notifications/initialized")
    assert r.status_code == 200
    assert "result" in r.json()


# malformed input

def test_invalid_json_returns_400(test_client, auth_headers):
    r = test_client.post(
        "/mcp",
        data="not-valid-json",
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


# batch requests

def test_batch_request_handled(test_client, auth_headers):
    """JSON-RPC batch: list of envelopes → list of responses."""
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
    ]
    r = test_client.post("/mcp", json=payload, headers=auth_headers)
    assert r.status_code == 200
    responses = r.json()
    assert len(responses) == 2
    assert {resp["id"] for resp in responses} == {1, 2}
