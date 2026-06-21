"""Pytest fixtures for the Acme Web MCP server tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Per-test isolation: tmp LOG_ROOT + dummy creds + a `test` base so the
    startup prefetch is skipped and no real HTTP is attempted."""
    monkeypatch.setenv("LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WEB_MCP_TOKEN", "test-token-xyz123")
    monkeypatch.setenv("WEB_API_BASE", "https://admin.test.local/api")
    monkeypatch.setenv("WEB_SERVICE_EMAIL", "svc@test.local")
    monkeypatch.setenv("WEB_SERVICE_PASSWORD", "test-pass")
    # Write-tool isolation: tmp backup dir, no inter-item sleep, both gates
    # OFF by default (individual write tests opt-in by setenv on the gate).
    monkeypatch.setenv("WEB_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("WEB_BULK_THROTTLE_S", "0")
    monkeypatch.delenv("WEB_WRITE_ENABLED", raising=False)
    monkeypatch.delenv("WEB_IDENTITY_WRITE_ENABLED", raising=False)
    # Reset client module-level token/cache between tests
    from server import web_client
    web_client._token_state.update({"token": None, "expires_at": 0.0})
    web_client._get_cache.clear()
    yield


@pytest.fixture
def test_client():
    from fastapi.testclient import TestClient
    from server.main import app
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token-xyz123"}
