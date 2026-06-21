"""Read-tool handler tests. web_client network functions are mocked."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.tools import read_tools


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# registry

def test_tool_registry_has_five_read_tools():
    assert set(read_tools.TOOL_HANDLERS) == {
        "web_get_listing", "web_search_listings", "web_list_listings",
        "web_get_translations", "refresh_cache",
    }
    names = {t["name"] for t in read_tools.TOOL_SCHEMAS}
    assert names == set(read_tools.TOOL_HANDLERS)


# web_get_listing

def test_web_get_listing_requires_property_id():
    out = _run(read_tools.web_get_listing({}))
    assert "error" in out


def test_web_get_listing_happy_path():
    summary = {"id": 4521, "property_id": "AR103917"}
    detail = {
        "id": 4521, "property_id": "AR103917", "status": "PUBLISHED",
        "listingable": {"building_area": 72}, "translatable": [],
    }
    with patch("server.web_client.resolve_by_property_id", return_value=summary), \
         patch("server.web_client.get_listing_detail", return_value=detail):
        out = _run(read_tools.web_get_listing({"property_id": "AR103917"}))
    assert out["listing"]["property_id"] == "AR103917"
    assert out["listing"]["building_area"] == 72


def test_web_get_listing_not_found():
    with patch("server.web_client.resolve_by_property_id", return_value=None):
        out = _run(read_tools.web_get_listing({"property_id": "AR000000"}))
    assert "error" in out
    assert "AR000000" in out["error"]


def test_web_get_listing_falls_back_to_summary_on_detail_failure():
    summary = {"id": 4521, "property_id": "AR103917", "status": "PUBLISHED"}
    from server.web_client import WebClientError
    with patch("server.web_client.resolve_by_property_id", return_value=summary), \
         patch("server.web_client.get_listing_detail", side_effect=WebClientError("boom")):
        out = _run(read_tools.web_get_listing({"property_id": "AR103917"}))
    assert out["detail_partial"] is True
    assert out["listing"]["property_id"] == "AR103917"


# web_search_listings

def test_web_search_folds_apartment_name_into_search():
    captured = {}
    def fake_list(params):
        captured.update(params)
        return {"data": [{"id": 1, "property_id": "AR1"}]}
    with patch("server.web_client.list_listings", side_effect=fake_list):
        out = _run(read_tools.web_search_listings({"apartment_name": "Casa Grande", "status": "PUBLISHED"}))
    assert captured["search"] == "Casa Grande"
    assert captured["status"] == "PUBLISHED"
    assert out["returned"] == 1


def test_web_search_respects_limit():
    rows = {"data": [{"id": i, "property_id": f"AR{i}"} for i in range(50)]}
    with patch("server.web_client.list_listings", return_value=rows):
        out = _run(read_tools.web_search_listings({"search": "x", "limit": 5}))
    assert out["returned"] == 5


# web_list_listings

def test_web_list_listings_pagination_passthrough():
    captured = {}
    def fake_list(params):
        captured.update(params)
        return {"data": [{"id": 1, "property_id": "AR1"}], "total": 873, "last_page": 18}
    with patch("server.web_client.list_listings", side_effect=fake_list):
        out = _run(read_tools.web_list_listings({"status": "PUBLISHED", "category": "RENT", "page": 2, "per_page": 50}))
    assert captured["page"] == 2
    assert captured["per_page"] == 50
    assert captured["status"] == "PUBLISHED"
    assert out["total"] == 873
    assert out["last_page"] == 18
    assert out["returned"] == 1


def test_web_list_listings_caps_per_page():
    with patch("server.web_client.list_listings", return_value={"data": []}) as m:
        _run(read_tools.web_list_listings({"per_page": 9999}))
    assert m.call_args[0][0]["per_page"] == 200


# web_get_translations

def test_web_get_translations_happy_path():
    summary = {"id": 4521, "property_id": "AR103917"}
    detail = {"translatable": [
        {"lang": "id", "title": "Judul", "shortDescription": "ringkas", "content": "isi", "slug": "s-id"},
        {"lang": "en", "title": "Title", "short_description": "short", "content": "body", "slug": "s-en"},
    ]}
    with patch("server.web_client.resolve_by_property_id", return_value=summary), \
         patch("server.web_client.get_listing_detail", return_value=detail):
        out = _run(read_tools.web_get_translations({"property_id": "AR103917"}))
    assert {t["lang"] for t in out["translations"]} == {"id", "en"}
    by_lang = {t["lang"]: t for t in out["translations"]}
    assert by_lang["id"]["short_description"] == "ringkas"
    assert by_lang["en"]["short_description"] == "short"  # camelCase alias handled


# refresh_cache

def test_refresh_cache_clears():
    with patch("server.web_client.clear_cache") as m:
        out = _run(read_tools.refresh_cache({}))
    assert out["refreshed"] is True
    assert m.called


# client GET cache TTL behavior

def test_get_cached_hits_api_once_within_ttl():
    from server import web_client
    web_client._get_cache.clear()
    with patch("server.web_client._request", return_value={"data": [1]}) as req:
        a = web_client._get_cached("/admin/listings", {"status": "PUBLISHED"})
        b = web_client._get_cached("/admin/listings", {"status": "PUBLISHED"})
    assert a == b == {"data": [1]}
    assert req.call_count == 1  # second call served from cache
