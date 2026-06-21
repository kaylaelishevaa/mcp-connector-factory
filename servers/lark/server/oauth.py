"""OAuth 2.0 wrapper for MCP custom-connector compatibility.

Why: Claude desktop/mobile app's "Add custom connector" UI only accepts a
URL + optional OAuth Client ID / Client Secret — no field for a raw Bearer
header. Our /mcp endpoint speaks Bearer auth. This module wraps that bearer
auth in an OAuth 2.0 facade so Claude app can complete its handshake.

Design summary:
- Access token issued by /oauth/token IS the LARK_MCP_TOKEN. Subsequent
  /mcp Bearer validation works without code changes.
- The actual access gate is /oauth/token requiring client_secret to equal
  LARK_MCP_TOKEN. This preserves the existing auth posture (caller must
  know the same shared secret).
- Discovery + DCR + authorize endpoints all "succeed easily" so Claude
  app's preflight/probe doesn't fail. Real enforcement is at token endpoint.
- Authorize auto-approves (no user-consent UI). The trycloudflare URL is
  considered the access-control boundary alongside the secret.

the operator's setup flow:
  Add custom connector →
    Name: Lark Acme Operations
    URL: https://<tunnel>.trycloudflare.com
    Advanced > OAuth Client ID: anything (e.g. "operator")
    Advanced > OAuth Client Secret: <LARK_MCP_TOKEN>
  Save → Claude does discovery + authorize + token → uses LARK_MCP_TOKEN
  as Bearer on every /mcp call.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from server.logger import get_logger, log_anomaly

_log = get_logger("oauth")
oauth_router = APIRouter()

# Allowlist for redirect_uri hosts on /oauth/authorize. Prevents this server
# from being used as an open-redirect phishing vector. Add hosts as needed.
_ALLOWED_REDIRECT_HOSTS: frozenset[str] = frozenset({
    "claude.ai",
    "claude.com",   # Claude app migrated app domain → claude.com; MCP callback
                    # is https://claude.com/api/mcp/auth_callback (subdomains via endswith)
    "anthropic.com",
    "localhost",  # local dev / smoke tests
    "127.0.0.1",
})


def _is_redirect_uri_allowed(redirect_uri: str) -> bool:
    """Validate redirect_uri host against allowlist.

    Accepts: exact host match OR subdomain of an allowed host.
    Requires https (except localhost / 127.0.0.1 which may use http for dev).
    """
    if not redirect_uri:
        return False
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    is_local = host in {"localhost", "127.0.0.1"}
    if parsed.scheme != "https" and not is_local:
        return False
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in _ALLOWED_REDIRECT_HOSTS
    )

# In-memory store: code -> {client_id, redirect_uri, code_challenge,
#                           code_challenge_method, expires_at, used}
_codes: dict[str, dict] = {}
_CODE_TTL_S = 300

# In-memory client registry (DCR). client_id -> {client_secret, redirect_uris, registered_at}
# Note: registered clients can complete the OAuth dance but still cannot get
# an access_token unless they prove client_secret == LARK_MCP_TOKEN.
_clients: dict[str, dict] = {}


def _server_base_url(request: Request) -> str:
    """Derive public-facing base URL from request headers.

    Cloudflared sets X-Forwarded-Proto / X-Forwarded-Host. Trust them since
    the only ingress path is the tunnel.
    """
    proto = request.headers.get("x-forwarded-proto", "https")
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host", "localhost")
    )
    return f"{proto}://{host}"


def _expected_secret() -> str:
    tok = os.environ.get("LARK_MCP_TOKEN", "").strip()
    if not tok:
        raise HTTPException(
            status_code=503,
            detail="LARK_MCP_TOKEN missing on server. Contact admin.",
        )
    return tok


def _gc_codes() -> None:
    """Drop expired codes — keeps in-memory store bounded."""
    now = time.time()
    for k in [k for k, v in _codes.items() if v.get("expires_at", 0) < now]:
        _codes.pop(k, None)


def _b64url_no_pad(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """RFC 7636 PKCE check. S256 default; plain accepted for legacy clients."""
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode()).digest()
        return hmac.compare_digest(_b64url_no_pad(digest), code_challenge)
    if method == "plain":
        return hmac.compare_digest(code_verifier, code_challenge)
    return False


# Discovery (RFC 8414, RFC 9728)

@oauth_router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request) -> JSONResponse:
    """OAuth 2.0 Authorization Server Metadata.

    Claude app fetches this first to discover endpoints + supported flows.
    """
    base = _server_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
            "none",
        ],
        "scopes_supported": ["mcp"],
    })


@oauth_router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> JSONResponse:
    """Protected Resource Metadata — points Claude at our auth server."""
    base = _server_base_url(request)
    return JSONResponse({
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    })


# Dynamic Client Registration (RFC 7591)

@oauth_router.post("/oauth/register")
async def register_client(request: Request) -> JSONResponse:
    """Open registration — succeeds for any caller.

    Registered clients can still NOT obtain an access_token from /oauth/token
    unless their client_secret equals LARK_MCP_TOKEN. The purpose of DCR
    support here is purely to let Claude app's discovery probe pass before
    the operator has filled in OAuth fields. The token endpoint is the real gate.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list):
        redirect_uris = []

    client_id = f"client_{secrets.token_urlsafe(8)}"
    client_secret = secrets.token_urlsafe(32)
    _clients[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "registered_at": time.time(),
    }
    _log.info(
        "oauth client registered",
        extra={"client_id": client_id, "redirect_uris_count": len(redirect_uris)},
    )

    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })


# Authorize endpoint

@oauth_router.get("/oauth/authorize")
async def authorize(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "code",
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = "S256",
    scope: Optional[str] = None,
) -> RedirectResponse:
    """Auto-approving authorization endpoint.

    No user-consent UI — we trust whoever reaches this URL (the trycloudflare
    URL secrecy is the first access boundary). Real enforcement is at the
    token endpoint, which requires client_secret == LARK_MCP_TOKEN.
    """
    if response_type != "code":
        raise HTTPException(
            status_code=400,
            detail=f"unsupported response_type: {response_type}",
        )
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="missing redirect_uri")
    if not _is_redirect_uri_allowed(redirect_uri):
        log_anomaly(
            kind="oauth_redirect_uri_rejected",
            component="oauth",
            detail={"redirect_uri": redirect_uri[:200], "client_id": client_id},
        )
        raise HTTPException(
            status_code=400,
            detail="redirect_uri host not in allowlist",
        )

    _gc_codes()
    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge or "",
        "code_challenge_method": code_challenge_method or "S256",
        "expires_at": time.time() + _CODE_TTL_S,
        "used": False,
    }
    _log.info(
        "oauth code issued",
        extra={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "has_pkce": bool(code_challenge),
        },
    )

    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(url=location, status_code=302)


# Token endpoint

@oauth_router.post("/oauth/token")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(default=None),
    redirect_uri: Optional[str] = Form(default=None),
    client_id: Optional[str] = Form(default=None),
    client_secret: Optional[str] = Form(default=None),
    code_verifier: Optional[str] = Form(default=None),
) -> JSONResponse:
    """OAuth 2.0 token endpoint.

    Required: grant_type=authorization_code, code, client_secret.
    Access enforcement: client_secret MUST equal LARK_MCP_TOKEN.
    On success, access_token = LARK_MCP_TOKEN itself (one shared bearer).
    """
    # HTTP Basic credentials fallback (RFC 6749 §2.3.1)
    if not client_id or not client_secret:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode()
                cid, csec = decoded.split(":", 1)
                client_id = client_id or cid
                client_secret = client_secret or csec
            except Exception:
                pass

    if grant_type != "authorization_code":
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": f"only authorization_code supported, got {grant_type}",
            },
            status_code=400,
        )

    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "missing code"},
            status_code=400,
        )

    _gc_codes()
    code_data = _codes.get(code)
    if not code_data:
        log_anomaly(
            kind="oauth_invalid_code",
            component="oauth",
            detail={"reason": "unknown code"},
        )
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "code not found"},
            status_code=400,
        )

    if code_data.get("used"):
        log_anomaly(
            kind="oauth_code_replay",
            component="oauth",
            detail={"code_id": code[:8]},
        )
        _codes.pop(code, None)
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "code already used"},
            status_code=400,
        )

    if code_data.get("expires_at", 0) < time.time():
        _codes.pop(code, None)
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "code expired"},
            status_code=400,
        )

    # Mark used immediately (single-use, even if PKCE/secret checks fail below)
    code_data["used"] = True

    # PKCE check (if challenge was registered at authorize step)
    challenge = code_data.get("code_challenge", "")
    if challenge:
        if not code_verifier:
            log_anomaly(kind="oauth_pkce_missing", component="oauth", detail={})
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "code_verifier required",
                },
                status_code=400,
            )
        if not _verify_pkce(
            code_verifier,
            challenge,
            code_data.get("code_challenge_method", "S256"),
        ):
            log_anomaly(kind="oauth_pkce_mismatch", component="oauth", detail={})
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "code_verifier mismatch",
                },
                status_code=400,
            )

    # Real access gate: client_secret must equal LARK_MCP_TOKEN
    expected = _expected_secret()
    if not client_secret or not hmac.compare_digest(client_secret, expected):
        log_anomaly(
            kind="oauth_token_secret_mismatch",
            component="oauth",
            detail={
                "client_id": client_id,
                "has_secret": bool(client_secret),
                "secret_len": len(client_secret) if client_secret else 0,
            },
        )
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "client_secret invalid",
            },
            status_code=401,
        )

    _log.info("oauth token issued", extra={"client_id": client_id})
    return JSONResponse({
        "access_token": expected,
        "token_type": "Bearer",
        "expires_in": 31536000,  # 1 year — effectively non-expiring
        "scope": "mcp",
    })
