"""FastAPI app exposing example.com (NestJS admin API) via MCP.

MCP protocol (JSON-RPC 2.0) handled at POST /mcp. Hand-rolled (3 methods:
initialize, tools/list, tools/call) for explicit auth + logging control —
identical shape to servers/lark.

Health check at GET / for cloudflared / load-balancer liveness probes.

Web-only: read tools wrap the admin API; write tools are a placeholder,
gated OFF.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server import web_client
from server.auth import verify_bearer
from server.oauth import oauth_router
from server.logger import get_logger, log_anomaly, log_mcp_call
from server.tools.read_tools import TOOL_HANDLERS as READ_HANDLERS
from server.tools.read_tools import TOOL_SCHEMAS as READ_SCHEMAS
from server.tools.write_tools import TOOL_HANDLERS as WRITE_HANDLERS
from server.tools.write_tools import TOOL_SCHEMAS as WRITE_SCHEMAS
from server.tools.write_tools import _identity_write_enabled, _write_enabled

# READ tools + WRITE tools (currently empty) share one
# dispatch table — mirrors 13's merged registry.
TOOL_HANDLERS = {**READ_HANDLERS, **WRITE_HANDLERS}
TOOL_SCHEMAS = READ_SCHEMAS + WRITE_SCHEMAS

_log = get_logger("main")

MCP_PROTOCOL_VERSION = "2025-03-26"


def _should_skip_prefetch() -> bool:
    """Skip startup token-warm in test contexts (no real creds / test base)."""
    base = os.environ.get("WEB_API_BASE", "").strip().lower()
    if "test" in base:
        return True
    # No service creds → nothing to warm (and login would just error)
    if not os.environ.get("WEB_SERVICE_EMAIL", "").strip():
        return True
    return False


def _background_prefetch() -> None:
    """On container startup, warm the service-user JWT (login) so the first user
    query doesn't pay login latency inside Claude's ~30s MCP timeout.

    Unlike 13 we do NOT fetch-all into memory — the admin API filters
    server-side, so there's no large cold dataset to pre-load; warming the
    token (and confirming creds work) is the win.

    Runs in a daemon thread; errors are swallowed (logged) so a bad cred
    doesn't kill the server before auth/health endpoints respond.
    """
    try:
        _log.info("background prefetch (token warm) starting")
        t0 = time.time()
        web_client.get_token(force=True)
        _log.info(
            "background prefetch complete",
            extra={"elapsed_s": round(time.time() - t0, 1)},
        )
    except Exception as e:
        log_anomaly(
            kind="background_prefetch_failed",
            component="main",
            detail={"exc": str(e)[:300]},
        )
        _log.warning(f"background prefetch failed: {str(e)[:200]}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not _should_skip_prefetch():
        thread = threading.Thread(
            target=_background_prefetch, name="web-prefetch", daemon=True,
        )
        thread.start()
        _log.info("background prefetch thread spawned")
    yield
    # Shutdown — daemon thread exits with the process


app = FastAPI(
    title="Acme Web MCP Server",
    description="MCP server exposing example.com admin API for Claude.",
    version="0.1.0",
    lifespan=lifespan,
)

# OAuth 2.0 endpoints for Claude app custom-connector compatibility.
# See server/oauth.py for the rationale + flow design.
app.include_router(oauth_router)

# CORS — Claude.ai web app may make discovery calls. Subdomain regex (exact
# allow_origins ignores wildcards — same caveat as 13).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*(claude\.ai|anthropic\.com)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def healthcheck() -> dict:
    """Liveness probe. No auth required."""
    return {
        "service": "cb-web-mcp-server",
        "status": "ok",
        "protocol_version": MCP_PROTOCOL_VERSION,
        "tools_count": len(TOOL_SCHEMAS),
        "oauth_enabled": True,
        "write_enabled": _write_enabled(),
        "identity_write_enabled": _identity_write_enabled(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return await healthcheck()


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def _handle_initialize(req_id: Any, params: dict) -> dict:
    return _jsonrpc_result(req_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "cb-web-mcp-server", "version": "0.1.0"},
    })


async def _handle_tools_list(req_id: Any, params: dict) -> dict:
    return _jsonrpc_result(req_id, {"tools": TOOL_SCHEMAS})


async def _handle_tools_call(req_id: Any, params: dict) -> dict:
    tool_name = params.get("name", "")
    arguments = params.get("arguments") or {}

    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        log_anomaly(
            kind="mcp_unknown_tool",
            component="main",
            detail={"tool": tool_name, "available": list(TOOL_HANDLERS.keys())},
        )
        return _jsonrpc_error(
            req_id, -32602, f"Unknown tool: {tool_name}",
            {"available_tools": list(TOOL_HANDLERS.keys())},
        )

    t0 = time.time()
    try:
        result = await handler(arguments)
        latency_ms = int((time.time() - t0) * 1000)
        result_text = _format_tool_result(result)
        log_mcp_call(
            tool=tool_name, args=arguments, result_size=len(result_text),
            latency_ms=latency_ms, ok=True,
        )
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": result_text}],
            "isError": False,
        })
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        _log.error("tool execution failed", extra={"tool": tool_name, "exc": str(e)[:300]})
        log_mcp_call(
            tool=tool_name, args=arguments, result_size=0,
            latency_ms=latency_ms, ok=False, error=str(e)[:300],
        )
        log_anomaly(
            kind="mcp_tool_exception",
            component="main",
            detail={"tool": tool_name, "exc": str(e)[:300], "tb_tail": tb[-500:]},
        )
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": f"Tool '{tool_name}' failed: {str(e)[:300]}"}],
            "isError": True,
        })


def _format_tool_result(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@app.post("/mcp")
async def mcp_handler(
    request: Request,
    _auth: bool = Depends(verify_bearer),
) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error: invalid JSON"),
            status_code=400,
        )
    if isinstance(body, list):
        responses = [await _dispatch_single(item) for item in body]
        return JSONResponse(responses)
    return JSONResponse(await _dispatch_single(body))


async def _dispatch_single(body: dict) -> dict:
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    _log.info("mcp request", extra={"method": method, "id": req_id})

    if method == "initialize":
        return await _handle_initialize(req_id, params)
    if method == "tools/list":
        return await _handle_tools_list(req_id, params)
    if method == "tools/call":
        return await _handle_tools_call(req_id, params)
    if method.startswith("notifications/"):
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    log_anomaly(
        kind="mcp_unknown_method",
        component="main",
        detail={"method": method, "id": req_id},
    )
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    # Propagate exc.headers — notably the WWW-Authenticate challenge on 401 from
    # verify_bearer (RFC 9728), which is what tells the Claude app where to find
    # our OAuth metadata and start the handshake. Dropping it leaves the connector
    # stuck on a bare 401.
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32000 + (-1 * exc.status_code),
                "message": str(exc.detail),
            },
        },
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )
