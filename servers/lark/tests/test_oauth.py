"""OAuth 2.0 facade tests.

Validates that Claude app's expected handshake works AND that the actual
access gate (client_secret == LARK_MCP_TOKEN) is enforced.
"""
from __future__ import annotations

import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import pytest

VALID_SECRET = "test-token-xyz123"  # matches conftest fixture


# Discovery

class TestDiscovery:
    def test_authorization_server_metadata_advertises_endpoints(self, test_client):
        r = test_client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"]
        assert body["authorization_endpoint"].endswith("/oauth/authorize")
        assert body["token_endpoint"].endswith("/oauth/token")
        assert body["registration_endpoint"].endswith("/oauth/register")
        assert "code" in body["response_types_supported"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "S256" in body["code_challenge_methods_supported"]

    def test_protected_resource_metadata(self, test_client):
        r = test_client.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"].endswith("/mcp")
        assert isinstance(body["authorization_servers"], list)
        assert len(body["authorization_servers"]) >= 1
        assert "header" in body["bearer_methods_supported"]

    def test_healthcheck_signals_oauth_enabled(self, test_client):
        r = test_client.get("/")
        assert r.status_code == 200
        assert r.json().get("oauth_enabled") is True


# Dynamic Client Registration

class TestDCR:
    def test_register_returns_client_id_and_secret(self, test_client):
        r = test_client.post(
            "/oauth/register",
            json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["client_id"].startswith("client_")
        assert len(body["client_secret"]) >= 20
        assert body["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]

    def test_register_with_empty_body_still_succeeds(self, test_client):
        r = test_client.post("/oauth/register", json={})
        assert r.status_code == 200
        assert r.json()["client_id"]

    def test_register_returns_secret_NOT_equal_to_lark_token(self, test_client):
        """DCR-issued secrets must NOT be the real bearer token —
        registering shouldn't grant access."""
        r = test_client.post("/oauth/register", json={})
        body = r.json()
        assert body["client_secret"] != VALID_SECRET


# Authorize endpoint

class TestAuthorize:
    def test_authorize_redirects_with_code(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
                "state": "xyz",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        parsed = urlparse(loc)
        qs = parse_qs(parsed.query)
        assert qs["code"][0]
        assert qs["state"][0] == "xyz"

    def test_authorize_preserves_redirect_query_params(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://claude.ai/cb?existing=1",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "existing=1" in loc
        assert "code=" in loc

    def test_authorize_rejects_non_code_response_type(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://claude.ai/cb",
                "response_type": "token",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400


class TestRedirectURIAllowlist:
    """Open-redirect mitigation — host must be claude.ai / anthropic.com / localhost."""

    def test_rejects_arbitrary_host(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://evil.example.com/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_rejects_lookalike_substring(self, test_client):
        """evilclaude.ai must NOT match — host comparison should be exact / suffix-bound."""
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://evilclaude.ai/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_rejects_http_scheme_for_remote(self, test_client):
        """Non-https rejected (except localhost)."""
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "http://claude.ai/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 400

    def test_accepts_claude_ai(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_accepts_claude_ai_subdomain(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://api.claude.ai/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_accepts_anthropic_com(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "https://anthropic.com/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_accepts_localhost_http_for_dev(self, test_client):
        r = test_client.get(
            "/oauth/authorize",
            params={
                "client_id": "operator",
                "redirect_uri": "http://localhost:3000/cb",
                "response_type": "code",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302


# Token endpoint — the real auth gate

def _get_code(test_client, code_challenge=None) -> str:
    params = {
        "client_id": "operator",
        "redirect_uri": "https://claude.ai/cb",
        "response_type": "code",
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    r = test_client.get("/oauth/authorize", params=params, follow_redirects=False)
    loc = r.headers["location"]
    qs = parse_qs(urlparse(loc).query)
    return qs["code"][0]


class TestToken:
    def test_token_success_with_valid_secret(self, test_client):
        code = _get_code(test_client)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"] == VALID_SECRET
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] > 0

    def test_token_rejects_wrong_secret(self, test_client):
        code = _get_code(test_client)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": "wrong-secret",
            },
        )
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"

    def test_token_rejects_missing_secret(self, test_client):
        code = _get_code(test_client)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
            },
        )
        assert r.status_code == 401

    def test_token_rejects_dcr_secret(self, test_client):
        """A DCR'd client_secret must NOT unlock the token endpoint."""
        reg = test_client.post("/oauth/register", json={}).json()
        code = _get_code(test_client)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": reg["client_id"],
                "client_secret": reg["client_secret"],
            },
        )
        assert r.status_code == 401

    def test_token_rejects_unknown_code(self, test_client):
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": "nonexistent-code-12345",
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_token_rejects_replayed_code(self, test_client):
        code = _get_code(test_client)
        # First exchange succeeds
        r1 = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r1.status_code == 200
        # Second exchange with same code must fail
        r2 = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r2.status_code == 400
        assert r2.json()["error"] == "invalid_grant"

    def test_token_rejects_unsupported_grant_type(self, test_client):
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    def test_token_supports_http_basic_auth(self, test_client):
        code = _get_code(test_client)
        basic = base64.b64encode(f"operator:{VALID_SECRET}".encode()).decode()
        r = test_client.post(
            "/oauth/token",
            data={"grant_type": "authorization_code", "code": code},
            headers={"Authorization": f"Basic {basic}"},
        )
        assert r.status_code == 200
        assert r.json()["access_token"] == VALID_SECRET


# PKCE

class TestPKCE:
    def _challenge(self, verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def test_pkce_s256_valid_verifier_succeeds(self, test_client):
        verifier = "x" * 64
        challenge = self._challenge(verifier)
        code = _get_code(test_client, code_challenge=challenge)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
                "code_verifier": verifier,
            },
        )
        assert r.status_code == 200

    def test_pkce_s256_wrong_verifier_rejected(self, test_client):
        verifier = "x" * 64
        challenge = self._challenge(verifier)
        code = _get_code(test_client, code_challenge=challenge)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
                "code_verifier": "wrong-verifier-zzz",
            },
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"

    def test_pkce_missing_verifier_when_challenge_set_rejected(self, test_client):
        verifier = "x" * 64
        challenge = self._challenge(verifier)
        code = _get_code(test_client, code_challenge=challenge)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        assert r.status_code == 400


# End-to-end (full flow)

class TestFullFlow:
    def test_issued_token_works_on_mcp_endpoint(self, test_client):
        """Token from /oauth/token must be accepted as Bearer on /mcp."""
        code = _get_code(test_client)
        r = test_client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "operator",
                "client_secret": VALID_SECRET,
            },
        )
        access_token = r.json()["access_token"]

        # Use that token on /mcp
        mcp_r = test_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert mcp_r.status_code == 200
        assert mcp_r.json()["result"]["protocolVersion"]

    def test_mcp_rejects_unauthenticated_request(self, test_client):
        """Sanity: /mcp still rejects without Bearer."""
        r = test_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 401


class TestCORS:
    """CORS regex must actually match claude.ai (subdomains too) — wildcard
    in allow_origins was inert. Regression test for that fix."""

    def test_cors_accepts_claude_ai(self, test_client):
        r = test_client.options(
            "/.well-known/oauth-authorization-server",
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "https://claude.ai"

    def test_cors_accepts_claude_ai_subdomain(self, test_client):
        r = test_client.options(
            "/.well-known/oauth-authorization-server",
            headers={
                "Origin": "https://api.claude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "https://api.claude.ai"

    def test_cors_rejects_arbitrary_origin(self, test_client):
        r = test_client.options(
            "/.well-known/oauth-authorization-server",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Either no ACAO header, or ACAO != evil.example.com
        assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_cors_rejects_lookalike(self, test_client):
        r = test_client.options(
            "/.well-known/oauth-authorization-server",
            headers={
                "Origin": "https://evilclaude.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.headers.get("access-control-allow-origin") != "https://evilclaude.ai"
