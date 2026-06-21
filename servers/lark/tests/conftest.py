"""Pytest fixtures for Lark MCP server tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Per-test isolation: tmp LOG_ROOT + dummy creds."""
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("LARK_MCP_TOKEN", "test-token-xyz123")
    monkeypatch.setenv("LARK_APP_ID", "cli_test")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_test")
    monkeypatch.setenv("LARK_BASE_ID", "base_test")
    yield


@pytest.fixture
def test_client():
    """FastAPI TestClient for the MCP server."""
    from fastapi.testclient import TestClient
    from server.main import app
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Bearer header with test token."""
    return {"Authorization": "Bearer test-token-xyz123"}
