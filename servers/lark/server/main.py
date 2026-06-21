"""FastAPI app exposing Lark Bitable via MCP protocol.

MCP protocol (JSON-RPC 2.0) handled at POST /mcp. Hand-rolled rather than
using the official Anthropic mcp SDK because we only need 3 methods
(initialize, tools/list, tools/call) and want explicit control over auth +
logging per request.

Health check at GET / for cloudflared / load balancer liveness probes.
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
from fastapi.responses import JSONResponse

from server import lark_client
from server.auth import verify_bearer
from server.logger import get_logger, log_anomaly, log_mcp_call
from server.oauth import oauth_router
from server.tools.read_tools import TOOL_HANDLERS as READ_HANDLERS
from server.tools.read_tools import TOOL_SCHEMAS as READ_SCHEMAS
from server.tools.write_tools import TOOL_HANDLERS as WRITE_HANDLERS
from server.tools.write_tools import TOOL_SCHEMAS as WRITE_SCHEMAS
from server.tools.write_tools import _write_enabled

# Curated/generic READ tools + WRITE tools share one dispatch table.
TOOL_HANDLERS = {**READ_HANDLERS, **WRITE_HANDLERS}
TOOL_SCHEMAS = READ_SCHEMAS + WRITE_SCHEMAS

_log = get_logger("main")


def _background_prefetch() -> None:
    """Run on container startup — pre-warm Lark caches so the first user
    query doesn't pay the ~44s cold-fetch latency that exceeds Claude app's
    MCP timeout (~30s).

    Runs in a daemon thread so uvicorn startup completes immediately. If a
    query arrives before prefetch finishes, it'll still cold-fetch (no
    locking) — same behavior as before, just slower for the unlucky first
    query during the prefetch window.

    Errors are swallowed (logged as anomaly) so a misconfigured Lark cred
    doesn't kill the server before auth/health endpoints respond.
    """
    try:
        _log.info("background prefetch starting")
        t0 = time.time()
        listings = lark_client.fetch_all_listings()
        contacts = lark_client.fetch_all_contacts()
        schema = lark_client.fetch_schema()  # warm tables+fields cache
        _log.info(
            "background prefetch complete",
            extra={
                "listings_count": len(listings),
                "contacts_count": len(contacts),
                "tables_count": len(schema.get("tables") or []),
                "elapsed_s": round(time.time() - t0, 1),
            },
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
    """FastAPI lifespan: spawn daemon thread for background prefetch on startup."""
    if not _should_skip_prefetch():
        thread = threading.Thread(
            target=_background_prefetch,
            name="lark-prefetch",
            daemon=True,
        )
        thread.start()
        _log.info("background prefetch thread spawned")
    yield
    # Shutdown — nothing to clean (daemon thread exits with process)


def _should_skip_prefetch() -> bool:
    """Skip prefetch in test contexts (LARK_BASE_ID=base_test indicates fixture)."""
    return os.environ.get("LARK_BASE_ID", "").strip() in {"", "base_test", "test"}


app = FastAPI(
    title="Lark MCP Server",
    description="MCP protocol server exposing Lark Bitable for the operator's Claude app.",
    version="0.1.0",
    lifespan=lifespan,
)

# OAuth 2.0 endpoints for Claude app custom-connector compatibility.
# See server/oauth.py for the rationale + flow design.
app.include_router(oauth_router)

# CORS — Claude.ai web app may make discovery calls. Desktop/mobile don't need
# this, but it's free insurance.
#
# NOTE: Starlette's CORSMiddleware.allow_origins is *exact-string match only* —
# wildcard subdomain entries like "https://*.claude.ai" are silently ignored.
# Use allow_origin_regex for subdomain matching.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-z0-9-]+\.)*(claude\.ai|anthropic\.com)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MCP_PROTOCOL_VERSION = "2025-03-26"


@app.get("/")
async def healthcheck() -> dict:
    """Liveness probe. No auth required."""
    return {
        "service": "lark-mcp-server",
        "status": "ok",
        "protocol_version": MCP_PROTOCOL_VERSION,
        "tools_count": len(TOOL_SCHEMAS),
        "oauth_enabled": True,
        "write_enabled": _write_enabled(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Alias for /."""
    return await healthcheck()


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def _handle_initialize(req_id: Any, params: dict) -> dict:
    """MCP initialize handshake."""
    return _jsonrpc_result(req_id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {},
            # No resources or prompts in this server
        },
        "serverInfo": {
            "name": "lark-mcp-server",
            "version": "0.1.0",
        },
    })


async def _handle_tools_list(req_id: Any, params: dict) -> dict:
    """Return all registered tool schemas."""
    return _jsonrpc_result(req_id, {"tools": TOOL_SCHEMAS})


async def _handle_tools_call(req_id: Any, params: dict) -> dict:
    """Execute a tool by name. Returns MCP-formatted text content."""
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
            req_id,
            -32602,
            f"Unknown tool: {tool_name}",
            {"available_tools": list(TOOL_HANDLERS.keys())},
        )

    t0 = time.time()
    try:
        result = await handler(arguments)
        latency_ms = int((time.time() - t0) * 1000)
        result_text = _format_tool_result(result)
        log_mcp_call(
            tool=tool_name,
            args=arguments,
            result_size=len(result_text),
            latency_ms=latency_ms,
            ok=True,
        )
        return _jsonrpc_result(req_id, {
            "content": [{"type": "text", "text": result_text}],
            "isError": False,
        })
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        tb = traceback.format_exc()
        _log.error(
            "tool execution failed",
            extra={"tool": tool_name, "exc": str(e)[:300]},
        )
        log_mcp_call(
            tool=tool_name,
            args=arguments,
            result_size=0,
            latency_ms=latency_ms,
            ok=False,
            error=str(e)[:300],
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
    """Serialize a tool's dict result to a human-readable text block.

    MCP tool responses are text-based — Claude reads this text and incorporates
    into its answer. JSON is fine; Claude understands. We prettify slightly.
    """
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@app.post("/mcp")
async def mcp_handler(
    request: Request,
    _auth: bool = Depends(verify_bearer),
) -> JSONResponse:
    """Main MCP JSON-RPC endpoint."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, -32700, "Parse error: invalid JSON"),
            status_code=400,
        )

    # Single request or batch — JSON-RPC supports both. For MCP we mostly see single.
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
        # MCP notifications (no response expected per JSON-RPC spec, but we
        # return an empty result for safety)
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    log_anomaly(
        kind="mcp_unknown_method",
        component="main",
        detail={"method": method, "id": req_id},
    )
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    """Standard HTTPException → JSON-RPC error response shape."""
    # Wrap as JSON-RPC error envelope (best-effort — no req_id for non-MCP errors).
    # Propagate exc.headers — notably the WWW-Authenticate challenge on 401 from
    # verify_bearer (RFC 9728), which tells the Claude app where to find our OAuth
    # metadata to start the handshake. Dropping it leaves the connector stuck.
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
