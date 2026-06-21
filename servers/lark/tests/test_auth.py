"""Auth tests — bearer token validation."""
from __future__ import annotations


def test_health_no_auth_required(test_client):
    """GET / should work without auth (liveness probe)."""
    r = test_client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_mcp_without_auth_returns_401(test_client):
    """POST /mcp without Authorization header → 401."""
    r = test_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_mcp_with_wrong_token_returns_401(test_client):
    r = test_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_401_carries_www_authenticate_resource_metadata(test_client):
    """RFC 9728: the 401 from /mcp must advertise our Protected Resource Metadata
    URL via WWW-Authenticate, else the Claude app can't bootstrap OAuth."""
    r = test_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    www = r.headers.get("www-authenticate", "")
    assert www.startswith("Bearer ")
    assert "resource_metadata=" in www
    assert "/.well-known/oauth-protected-resource" in www


def test_mcp_with_correct_token_returns_200(test_client, auth_headers):
    r = test_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=auth_headers,
    )
    assert r.status_code == 200


def test_mcp_non_bearer_header_rejected(test_client):
    r = test_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Basic test-token-xyz123"},
    )
    assert r.status_code == 401


def test_missing_token_env_returns_503(test_client, monkeypatch):
    """LARK_MCP_TOKEN unset → server refuses ALL requests with 503."""
    monkeypatch.delenv("LARK_MCP_TOKEN", raising=False)
    r = test_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 503
