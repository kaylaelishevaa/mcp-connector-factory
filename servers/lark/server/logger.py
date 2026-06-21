"""Structured JSON-line logger for the Lark MCP server.

Writes to LOG_ROOT/<date>/<component>.log plus a single rolling
LOG_ROOT/lark_mcp_calls.jsonl audit stream.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path


def _log_root() -> Path:
    return Path(os.environ.get("LOG_ROOT", "/data/logs"))


def _today_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def _now_iso_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ensure_daily_dir() -> Path:
    daily = _log_root() / _today_str()
    daily.mkdir(parents=True, exist_ok=True)
    return daily


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _now_iso_utc(),
            "level": record.levelname,
            "component": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
            }:
                continue
            payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_LOGGERS: dict[str, logging.Logger] = {}


def get_logger(component: str) -> logging.Logger:
    if component in _LOGGERS:
        return _LOGGERS[component]
    logger = logging.getLogger(component)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    daily = _ensure_daily_dir()
    fh = logging.FileHandler(daily / f"{component}.log", encoding="utf-8")
    fh.setFormatter(_JSONFormatter())
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(_JSONFormatter())
    logger.addHandler(sh)
    _LOGGERS[component] = logger
    return logger


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def log_mcp_call(
    *,
    tool: str,
    args: dict,
    result_size: int,
    latency_ms: int,
    ok: bool,
    error: str | None = None,
) -> None:
    """Append MCP tool call to the audit stream."""
    _append_jsonl(_log_root() / "lark_mcp_calls.jsonl", {
        "ts": _now_iso_utc(),
        "tool": tool,
        "args": args,
        "result_size": result_size,
        "latency_ms": latency_ms,
        "ok": ok,
        "error": error,
    })


def log_lark_write(
    *,
    tool: str,
    table_name: str,
    table_id: str,
    record_id: str | None,
    fields_before: dict | None,
    fields_after: dict,
    auth_token_hash: str,
    success: bool,
    error: str | None = None,
) -> None:
    """Append a write to the append-only audit stream lark_mcp_writes.jsonl.

    `fields_before` snapshots only the keys being modified (None for create);
    `fields_after` is the requested patch / new fields. Together they form the
    forensic trail + manual-rollback reference (Lark also keeps native per-record
    revision history). `record_id` is None for a failed create.
    """
    _append_jsonl(_log_root() / "lark_mcp_writes.jsonl", {
        "ts": _now_iso_utc(),
        "tool": tool,
        "table_name": table_name,
        "table_id": table_id,
        "record_id": record_id,
        "fields_before": fields_before,
        "fields_after": fields_after,
        "auth_token_hash": auth_token_hash,
        "success": success,
        "error": error,
    })


def log_anomaly(*, kind: str, component: str, detail: dict) -> None:
    _append_jsonl(_log_root() / "anomalies.jsonl", {
        "ts": _now_iso_utc(),
        "kind": kind,
        "component": component,
        "detail": detail,
    })
