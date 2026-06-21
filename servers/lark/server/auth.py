"""Bearer token auth for Lark MCP server.

Single token from env (`LARK_MCP_TOKEN`). Compared in constant-time to avoid
timing attacks (overkill for one user, but cheap).

Use as FastAPI dependency:

    from fastapi import Depends
    from server.auth import verify_bearer

    @app.post("/mcp")
    async def handler(_: bool = Depends(verify_bearer), ...):
        ...
"""
from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request

from server.logger import log_anomaly


def _prm_url(request: Request) -> str:
    """Public URL of our Protected Resource Metadata (RFC 9728).

    Cloudflared sets X-Forwarded-Proto / X-Forwarded-Host; trust them since the
    only ingress is the tunnel. Falls back to Host for direct/local calls.
    """
    proto = request.headers.get("x-forwarded-proto", "https")
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host", "localhost")
    )
    return f"{proto}://{host}/.well-known/oauth-protected-resource"


def _challenge(request: Request) -> dict[str, str]:
    """WWW-Authenticate header pointing Claude at our OAuth metadata.

    The MCP authorization spec (RFC 9728 §5.1) requires a 401 from the protected
    resource to advertise its metadata URL here. WITHOUT this header the Claude
    app receives a bare 401 on /mcp and never starts the OAuth handshake — the
    connector just fails to connect.
    """
    return {
        "WWW-Authenticate": f'Bearer resource_metadata="{_prm_url(request)}"'
    }


def _expected_token() -> str:
    tok = os.environ.get("LARK_MCP_TOKEN", "").strip()
    if not tok:
        # Fail-CLOSED — empty token in env means server refuses ALL requests
        # rather than silently auth-bypass.
        raise HTTPException(
            status_code=503,
            detail="LARK_MCP_TOKEN missing on server. Contact admin.",
        )
    return tok


def verify_bearer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> bool:
    """FastAPI dependency: validate Authorization: Bearer <token>.

    Raises 401 (with the RFC 9728 WWW-Authenticate challenge so Claude can
    discover our OAuth server) on missing / malformed / mismatched token.
    Returns True on success.
    """
    if not authorization or not authorization.startswith("Bearer "):
        log_anomaly(
            kind="auth_missing",
            component="auth",
            detail={"reason": "missing or non-Bearer header"},
        )
        raise HTTPException(
            status_code=401, detail="missing Bearer token",
            headers=_challenge(request),
        )
    provided = authorization[len("Bearer "):].strip()
    expected = _expected_token()
    if not hmac.compare_digest(provided, expected):
        log_anomaly(
            kind="auth_invalid_token",
            component="auth",
            detail={"provided_len": len(provided)},
        )
        raise HTTPException(
            status_code=401, detail="invalid token",
            headers=_challenge(request),
        )
    return True
