"""Background prefetch on startup + refresh_cache tool tests."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# Background prefetch

def test_background_prefetch_runs_on_startup(monkeypatch):
    """When LARK_BASE_ID is set (non-test), prefetch thread spawns."""
    monkeypatch.setenv("LARK_BASE_ID", "base_prod_real")  # not test sentinel
    mock_listings = MagicMock(return_value=[{"record_id": "rec1", "fields": {}}])
    mock_contacts = MagicMock(return_value=[{"record_id": "recC1", "fields": {}}])
    with patch("server.lark_client.fetch_all_listings", mock_listings), \
         patch("server.lark_client.fetch_all_contacts", mock_contacts):
        # Direct call to the function (not via lifespan since TestClient may
        # not always trigger lifespan in older FastAPI; verify the function works)
        from server.main import _background_prefetch
        _background_prefetch()
    assert mock_listings.called
    assert mock_contacts.called


def test_background_prefetch_swallows_exception(monkeypatch):
    """Lark errors during prefetch should NOT crash — logged as anomaly."""
    monkeypatch.setenv("LARK_BASE_ID", "base_prod_real")
    with patch(
        "server.lark_client.fetch_all_listings",
        side_effect=RuntimeError("simulated Lark down"),
    ):
        from server.main import _background_prefetch
        # Should not raise
        _background_prefetch()


def test_prefetch_skipped_in_test_env(monkeypatch):
    """Test env (LARK_BASE_ID=base_test) skips prefetch — tests don't spawn threads."""
    monkeypatch.setenv("LARK_BASE_ID", "base_test")
    from server.main import _should_skip_prefetch
    assert _should_skip_prefetch() is True


def test_prefetch_runs_in_prod_env(monkeypatch):
    monkeypatch.setenv("LARK_BASE_ID", "base_production_xyz")
    from server.main import _should_skip_prefetch
    assert _should_skip_prefetch() is False


def test_prefetch_skipped_when_lark_base_id_unset(monkeypatch):
    monkeypatch.delenv("LARK_BASE_ID", raising=False)
    from server.main import _should_skip_prefetch
    assert _should_skip_prefetch() is True  # empty → skip (defensive)


# refresh_cache tool

class TestRefreshCache:
    def test_refresh_all(self):
        from server.tools.read_tools import refresh_cache
        mock_l = MagicMock(return_value=[{"r": 1}] * 100)
        mock_c = MagicMock(return_value=[{"r": 1}] * 50)
        mock_a = MagicMock(return_value=[{"r": 1}] * 20)
        with patch("server.lark_client.fetch_all_listings", mock_l), \
             patch("server.lark_client.fetch_all_contacts", mock_c), \
             patch("server.lark_client.fetch_all_activities", mock_a):
            r = _run(refresh_cache({"table": "all"}))
        assert r["refreshed"] == "all"
        assert r["counts"]["listings"] == 100
        assert r["counts"]["contacts"] == 50
        assert r["counts"]["activities"] == 20
        # Verify force_refresh=True passed
        mock_l.assert_called_with(force_refresh=True)
        mock_c.assert_called_with(force_refresh=True)
        mock_a.assert_called_with(force_refresh=True)

    def test_refresh_listings_only(self):
        from server.tools.read_tools import refresh_cache
        mock_l = MagicMock(return_value=[{"r": 1}] * 9786)
        mock_c = MagicMock()
        mock_a = MagicMock()
        with patch("server.lark_client.fetch_all_listings", mock_l), \
             patch("server.lark_client.fetch_all_contacts", mock_c), \
             patch("server.lark_client.fetch_all_activities", mock_a):
            r = _run(refresh_cache({"table": "listings"}))
        assert r["refreshed"] == "listings"
        assert r["counts"]["listings"] == 9786
        assert "contacts" not in r["counts"]
        assert "activities" not in r["counts"]
        assert mock_l.called
        assert not mock_c.called
        assert not mock_a.called

    def test_refresh_default_is_all(self):
        from server.tools.read_tools import refresh_cache
        with patch("server.lark_client.fetch_all_listings", return_value=[]), \
             patch("server.lark_client.fetch_all_contacts", return_value=[]), \
             patch("server.lark_client.fetch_all_activities", return_value=[]):
            r = _run(refresh_cache({}))
        assert r["refreshed"] == "all"

    def test_refresh_handles_lark_error_gracefully(self):
        """Lark failure on one table doesn't crash the whole refresh."""
        from server.tools.read_tools import refresh_cache
        from server.lark_client import LarkClientError
        with patch(
            "server.lark_client.fetch_all_listings",
            side_effect=LarkClientError("Lark down"),
        ), patch(
            "server.lark_client.fetch_all_contacts",
            return_value=[{"r": 1}] * 50,
        ), patch(
            "server.lark_client.fetch_all_activities",
            return_value=[{"r": 1}] * 20,
        ):
            r = _run(refresh_cache({"table": "all"}))
        # Listings reports error string; others still succeed
        assert "error" in str(r["counts"]["listings"])
        assert r["counts"]["contacts"] == 50
        assert r["counts"]["activities"] == 20


# Tool registry

def test_refresh_cache_in_tool_registry():
    """refresh_cache should be discoverable via tools/list."""
    from server.tools.read_tools import TOOL_HANDLERS, TOOL_SCHEMAS
    assert "refresh_cache" in TOOL_HANDLERS
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert "refresh_cache" in names


def test_total_tool_count_now_15():
    """11 READ tools + refresh_cache + list_tables + describe_table + query_records = 15."""
    from server.tools.read_tools import TOOL_HANDLERS
    assert len(TOOL_HANDLERS) == 15
