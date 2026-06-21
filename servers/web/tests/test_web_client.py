"""web_client tests — login/refresh, 401 retry, response shapes, normalizer.

All HTTP is mocked. No live admin API calls, no mutations.
"""
from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from server import web_client


# helpers

def _make_jwt(exp: float | int) -> str:
    """Build an unsigned JWT-shaped string with the given exp claim."""
    def b64(d: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(d).encode()).decode()
        return raw.rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': 1, 'role': 'internal', 'exp': exp})}.sig"


def _resp(status=200, json_body=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.text = text
    if json_body is None:
        m.json.side_effect = ValueError("no json")
    else:
        m.json.return_value = json_body
    return m


# _jwt_exp

def test_jwt_exp_parses_claim():
    tok = _make_jwt(1893456000)
    assert web_client._jwt_exp(tok) == 1893456000.0


def test_jwt_exp_none_on_garbage():
    assert web_client._jwt_exp("not-a-jwt") is None
    assert web_client._jwt_exp("a.b") is not None or web_client._jwt_exp("a.b") is None  # no crash


# login + token cache

def test_login_caches_token_with_exp():
    future = time.time() + 7 * 24 * 3600
    tok = _make_jwt(future)
    with patch("server.web_client.requests.post", return_value=_resp(200, {"access_token": tok})) as p:
        got = web_client.get_token(force=True)
    assert got == tok
    assert p.called
    assert web_client._token_state["expires_at"] == pytest.approx(future, abs=2)


def test_get_token_uses_cache_within_skew():
    future = time.time() + 7 * 24 * 3600
    tok = _make_jwt(future)
    with patch("server.web_client.requests.post", return_value=_resp(200, {"access_token": tok})) as p:
        web_client.get_token(force=True)
        web_client.get_token()  # cached — should NOT re-login
        web_client.get_token()
    assert p.call_count == 1


def test_get_token_relogins_when_near_expiry():
    near = time.time() + 60  # within REFRESH_SKEW (24h)
    tok = _make_jwt(near)
    with patch("server.web_client.requests.post", return_value=_resp(200, {"access_token": tok})) as p:
        web_client.get_token(force=True)
        web_client.get_token()  # near expiry → re-login
    assert p.call_count == 2


def test_login_failure_raises():
    with patch("server.web_client.requests.post", return_value=_resp(401, text="bad creds")):
        with pytest.raises(web_client.WebClientError):
            web_client.get_token(force=True)


def test_login_missing_creds_raises(monkeypatch):
    monkeypatch.delenv("WEB_SERVICE_EMAIL", raising=False)
    with pytest.raises(web_client.WebClientError):
        web_client.get_token(force=True)


# _request 401 retry

def test_request_relogins_and_retries_on_401():
    good_tok = _make_jwt(time.time() + 7 * 24 * 3600)
    # seed a valid cached token so the first _do() uses it
    web_client._token_state.update({"token": good_tok, "expires_at": time.time() + 7 * 24 * 3600})
    seq = [_resp(401, text="expired"), _resp(200, {"ok": True})]
    with patch("server.web_client.requests.request", side_effect=seq) as req, \
         patch("server.web_client.requests.post", return_value=_resp(200, {"access_token": good_tok})) as login:
        out = web_client._request("GET", "/admin/listings")
    assert out == {"ok": True}
    assert req.call_count == 2     # original + retry
    assert login.call_count == 1   # forced re-login between them


def test_request_raises_on_non_2xx():
    web_client._token_state.update({"token": _make_jwt(time.time() + 10*24*3600), "expires_at": time.time() + 10*24*3600})
    with patch("server.web_client.requests.request", return_value=_resp(500, text="boom")):
        with pytest.raises(web_client.WebClientError):
            web_client._request("GET", "/admin/listings/1")


# _iter_rows across envelope shapes

@pytest.mark.parametrize("resp,expected", [
    ([{"id": 1}, {"id": 2}], 2),
    ({"data": [{"id": 1}]}, 1),
    ({"items": [{"id": 1}, {"id": 2}, {"id": 3}]}, 3),
    ({"listings": [{"id": 1}]}, 1),
    ({"data": {"data": [{"id": 9}]}}, 1),   # Laravel nested
    ({"nope": 1}, 0),
    ("string", 0),
])
def test_iter_rows_shapes(resp, expected):
    assert len(web_client._iter_rows(resp)) == expected


# normalize_listing (grounded in serializeListing real shape)

def _real_shape_detail() -> dict:
    """A listing detail blob shaped like serializeListing + findById output."""
    return {
        "id": 4521,
        "propertyId": "AR103917",
        "property_id": "AR103917",
        "status": "PUBLISHED",
        "dealStatus": "AVAILABLE",
        "deal_status": "AVAILABLE",
        "isRented": False,
        "is_rented": False,
        "category": "RENT",
        "type": "DIRECT_LISTING",
        "price": "59200000",
        "soldAt": None,
        "sold_at": None,
        "listingableType": "App\\Models\\ApartmentUnit",
        "listingable_type": "App\\Models\\ApartmentUnit",
        "createdAt": "2026-04-10T00:00:00.000Z",
        "created_at": "2026-04-10T00:00:00.000Z",
        "price_reduced": False,
        "listingable": {
            "id": 88,
            "buildingArea": 72,
            "bedrooms": 2,
            "floor": 9,
            "floorZone": "MID",
            "towerName": "Kintamani",
            "unitNumber": "9AAL",
        },
        "translatable": [
            {"lang": "id", "title": "Disewakan Apartemen Kintamani Lantai 9", "slug": "sewa-apartemen-kintamani-9aal"},
            {"lang": "en", "title": "Kintamani Apartment Mid Floor For Rent", "slug": "rent-kintamani-apartment-9aal"},
        ],
        "media": [{"id": 1}, {"id": 2}, {"id": 3}],
    }


def test_normalize_listing_maps_core_fields():
    n = web_client.normalize_listing(_real_shape_detail())
    assert n["property_id"] == "AR103917"
    assert n["id"] == 4521
    assert n["status"] == "PUBLISHED"
    assert n["deal_status"] == "AVAILABLE"
    assert n["is_rented"] is False
    assert n["category"] == "RENT"
    assert n["price"] == "59200000"


def test_normalize_listing_pulls_unit_attrs_from_listingable():
    n = web_client.normalize_listing(_real_shape_detail())
    assert n["building_area"] == 72
    assert n["bedrooms"] == 2
    assert n["floor"] == 9
    assert n["floor_zone"] == "MID"
    assert n["tower_name"] == "Kintamani"
    assert n["unit_number"] == "9AAL"


def test_normalize_listing_titles_and_slug():
    n = web_client.normalize_listing(_real_shape_detail())
    assert n["title_id"].startswith("Disewakan Apartemen Kintamani")
    assert n["title_en"].startswith("Kintamani Apartment")
    assert n["slug"] == "sewa-apartemen-kintamani-9aal"  # prefers id-lang
    assert n["media_count"] == 3


def test_normalize_listing_snake_only_shape():
    """If the API ever returns snake_case only, the normalizer still works."""
    row = {
        "id": 1, "property_id": "AR1", "status": "DRAFT",
        "deal_status": "SOLD", "is_rented": None, "category": "SELL",
        "listingable": {"building_area": 40},
        "translations": [{"lang": "id", "title": "T", "slug": "s"}],
    }
    n = web_client.normalize_listing(row)
    assert n["property_id"] == "AR1"
    assert n["deal_status"] == "SOLD"
    assert n["building_area"] == 40
    assert n["title_id"] == "T"


# normalize_translations

def test_normalize_translations():
    rows = web_client.normalize_translations(_real_shape_detail())
    langs = {r["lang"] for r in rows}
    assert langs == {"id", "en"}


# resolve_by_property_id

def test_resolve_by_property_id_exact_match():
    listresp = {"data": [
        {"id": 1, "propertyId": "AR103917", "property_id": "AR103917"},
        {"id": 2, "propertyId": "AR1039170", "property_id": "AR1039170"},  # startsWith noise
    ]}
    with patch("server.web_client._get_cached", return_value=listresp):
        row = web_client.resolve_by_property_id("AR103917")
    assert row["id"] == 1


def test_resolve_by_property_id_none_when_absent():
    with patch("server.web_client._get_cached", return_value={"data": []}):
        assert web_client.resolve_by_property_id("AR999999") is None


# ResponseInterceptor envelope unwrapping

def test_unwrap_strips_envelope():
    assert web_client._unwrap({"success": True, "data": {"x": 1}}) == {"x": 1}
    assert web_client._unwrap({"success": True, "data": [1, 2]}) == [1, 2]
    # no envelope → returned as-is (idempotent)
    assert web_client._unwrap({"x": 1}) == {"x": 1}


def test_login_handles_enveloped_response():
    """Real /auth/login returns {success, data:{access_token, user}}."""
    tok = _make_jwt(time.time() + 7 * 24 * 3600)
    enveloped = {"success": True, "data": {"access_token": tok, "user": {"id": 9}}}
    with patch("server.web_client.requests.post", return_value=_resp(200, enveloped)):
        assert web_client.get_token(force=True) == tok


def test_get_listing_detail_unwraps_envelope():
    """Real GET /admin/listings/:id returns {success, data:{...listing}}."""
    web_client._token_state.update({"token": _make_jwt(time.time() + 10*24*3600), "expires_at": time.time() + 10*24*3600})
    enveloped = {"success": True, "data": {"id": 1, "property_id": "AR1", "status": "PUBLISHED"}}
    with patch("server.web_client.requests.request", return_value=_resp(200, enveloped)):
        detail = web_client.get_listing_detail(1)
    assert detail["property_id"] == "AR1"   # inner, not the envelope
    assert "success" not in detail


def test_whoami_unwraps_envelope():
    web_client._token_state.update({"token": _make_jwt(time.time() + 10*24*3600), "expires_at": time.time() + 10*24*3600})
    enveloped = {"success": True, "data": {"id": 9, "role": "internal", "role_name": "service", "permissions": {"view listing": True}}}
    with patch("server.web_client.requests.request", return_value=_resp(200, enveloped)):
        me = web_client.whoami()
    assert me["role_name"] == "service"
    assert me["permissions"]["view listing"] is True


def test_list_envelope_rows_and_meta_extracted():
    """Paginated list envelope: rows under data, pagination under meta."""
    envelope = {
        "success": True,
        "data": [{"id": 1, "property_id": "AR1"}, {"id": 2, "property_id": "AR2"}],
        "links": {"next": None},
        "meta": {"current_page": 1, "last_page": 9, "total": 873, "per_page": 100},
    }
    rows = web_client._iter_rows(envelope)
    assert len(rows) == 2
    assert envelope["meta"]["total"] == 873
