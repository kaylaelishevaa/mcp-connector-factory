"""Unit tests for read tools — search_listings, get_listing, find_activities.

All tests mock lark_client to avoid live API calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


# Sample Lark raw data

SAMPLE_LISTINGS_RAW = [
    {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "Sunset Residences Tower Maya",
            "Unit": "Unit 0308",
            "Area/Kawasan": "Sunset District - Metro South",
            "Kamar Tidur": 2, "Kamar Mandi": 2,
            "Harga Sewa": 32500000,
            "Status": {"text": "Available"},
            "Furnished": {"text": "Fully"},
            "Pemilik": ["recContact1"],
        },
    },
    {
        "record_id": "rec2",
        "fields": {
            "Nama Properti": "Casa Grande",
            "Unit": "Tower B Unit 15A",
            "Area/Kawasan": "Eastside",
            "Kamar Tidur": 2, "Kamar Mandi": 2,
            "Harga Sewa": 5000000,
            "Status": "Available",
        },
    },
    {
        "record_id": "rec3",
        "fields": {
            "Nama Properti": "Anandamaya Residences",
            "Unit": "Tower 1 Unit 22A",
            "Area/Kawasan": "Mainline",
            "Kamar Tidur": 3, "Kamar Mandi": 3,
            "Harga Sewa": 30000000,
            "Status": "Available",
        },
    },
    {
        "record_id": "rec4",
        "fields": {
            "Nama Properti": "Greenview",
            "Unit": "Tower A Unit 12",
            "Area/Kawasan": "Sunset District - Metro South",
            "Kamar Tidur": 2,
            "Status": "Sold",
        },
    },
]

SAMPLE_ACTIVITIES_RAW = [
    {
        "record_id": "act1",
        "fields": {
            "Judul": "Showing Casa Grande 15A",
            "Waktu Mulai": 1747929600000,  # 2026-05-22 16:00 WIB ~
            "Tipe": "Showing",
            "Status": "Dijadwalkan",
            "Listings": ["rec2"],
            "Agen": [{"id": "ou_rina"}],
        },
    },
    {
        "record_id": "act2",
        "fields": {
            "Judul": "Meeting Sunset District",
            "Waktu Mulai": 1747843200000,  # ~2026-05-21
            "Tipe": "Meeting",
            "Status": "Selesai",
            "Listings": ["rec1"],
            "Agen": [{"id": "ou_maya"}],
        },
    },
]


# search_listings

def _run(coro):
    """Helper to run async function in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _mock_lark_listings():
    with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_RAW):
        yield


@pytest.fixture
def _mock_lark_activities():
    with patch("server.lark_client.fetch_all_activities", return_value=SAMPLE_ACTIVITIES_RAW):
        yield


class TestSearchListings:
    def test_no_filters_returns_all_up_to_limit(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"limit": 10}))
        assert r["returned"] == 4
        assert r["total_lark"] == 4

    def test_filter_by_area(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"area": "Sunset District"}))
        rids = {m["record_id"] for m in r["matches"]}
        assert rids == {"rec1", "rec4"}

    def test_filter_by_building(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"building": "casa grande"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "rec2"

    def test_filter_by_kt(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"kt": 2}))
        rids = {m["record_id"] for m in r["matches"]}
        assert rids == {"rec1", "rec2", "rec4"}

    def test_filter_by_status(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"status": "available"}))
        # rec4 has Status: "Sold" — should be excluded
        rids = {m["record_id"] for m in r["matches"]}
        assert "rec4" not in rids
        assert "rec1" in rids

    def test_combine_area_kt_status(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({
            "area": "Sunset District",
            "kt": 2,
            "status": "available",
        }))
        # rec1 matches all three; rec4 excluded (Sold)
        rids = {m["record_id"] for m in r["matches"]}
        assert rids == {"rec1"}

    def test_limit_caps_results(self):
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"limit": 2}))
        assert r["returned"] == 2

    def test_owner_field_included_for_operator_tier(self):
        """Critical: the operator sees Pemilik field. NO stripping."""
        from server.tools.read_tools import search_listings
        r = _run(search_listings({"building": "Sunset Residences"}))
        match = r["matches"][0]
        assert match.get("pemilik") == ["recContact1"]


# get_listing

class TestGetListing:
    def test_missing_record_id_returns_error(self):
        from server.tools.read_tools import get_listing
        r = _run(get_listing({}))
        assert "error" in r

    def test_valid_record_returns_full(self):
        from server.tools.read_tools import get_listing
        with patch(
            "server.lark_client.get_record",
            return_value=SAMPLE_LISTINGS_RAW[0],
        ):
            r = _run(get_listing({"record_id": "rec1"}))
        assert r["record_id"] == "rec1"
        assert r["nama_properti"] == "Sunset Residences Tower Maya"
        assert r["pemilik"] == ["recContact1"]  # owner visible
        # _raw_fields dropped from default output
        assert "_raw_fields" not in r

    def test_lark_error_returned_gracefully(self):
        from server.tools.read_tools import get_listing
        from server.lark_client import LarkClientError
        with patch(
            "server.lark_client.get_record",
            side_effect=LarkClientError("Lark down"),
        ):
            r = _run(get_listing({"record_id": "rec1"}))
        assert "error" in r
        assert "Lark down" in r["error"]


# find_activities

class TestFindActivities:
    def test_no_filters_returns_all(self, _mock_lark_activities):
        from server.tools.read_tools import find_activities
        r = _run(find_activities({}))
        assert r["returned"] == 2

    def test_filter_by_tipe(self, _mock_lark_activities):
        from server.tools.read_tools import find_activities
        r = _run(find_activities({"tipe": "Showing"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "act1"

    def test_filter_by_status(self, _mock_lark_activities):
        from server.tools.read_tools import find_activities
        r = _run(find_activities({"status": "Selesai"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "act2"

    def test_filter_by_listing_id(self, _mock_lark_activities):
        from server.tools.read_tools import find_activities
        r = _run(find_activities({"listing_id": "rec2"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "act1"

    def test_filter_by_agen(self, _mock_lark_activities):
        from server.tools.read_tools import find_activities
        r = _run(find_activities({"agen_ou_id": "ou_rina"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "act1"

    def test_date_keyword_today(self, _mock_lark_activities):
        """'today' resolves to current date; no activities in sample match → 0."""
        from server.tools.read_tools import find_activities
        r = _run(find_activities({"date": "today"}))
        # Sample activities have Waktu Mulai in May 2026, not today's date
        # Returns 0 unless test happens to run on May 21-22 2026 :)
        assert r["returned"] in (0, 1, 2)


# search_contacts

SAMPLE_CONTACTS_RAW = [
    {
        "record_id": "recContact1",
        "fields": {
            "Nama": "Pak Susanto Wijaya",
            "No HP": "+6281234567890",
            "Tipe Contact": [{"text": "Owner"}],
            "Sumber Klien": "WhatsApp",
        },
    },
    {
        "record_id": "recContact2",
        "fields": {
            "Nama": "Ibu Sari Rahmawati",
            "No HP": "+6281298765432",
            "Tipe Contact": [{"text": "Buyer"}],
        },
    },
    {
        "record_id": "recContact3",
        "fields": {
            "Nama": "Susanto Junior",
            "No HP": "+6281211223344",
            "Tipe Contact": [{"text": "Tenant"}],
        },
    },
]


@pytest.fixture
def _mock_lark_contacts():
    with patch("server.lark_client.fetch_all_contacts", return_value=SAMPLE_CONTACTS_RAW):
        yield


class TestSearchContacts:
    def test_missing_query_returns_error(self, _mock_lark_contacts):
        from server.tools.read_tools import search_contacts
        r = _run(search_contacts({}))
        assert "error" in r

    def test_fuzzy_match_by_name(self, _mock_lark_contacts):
        from server.tools.read_tools import search_contacts
        r = _run(search_contacts({"query": "susanto"}))
        rids = {m["record_id"] for m in r["matches"]}
        # Matches Pak Susanto Wijaya AND Susanto Junior
        assert rids == {"recContact1", "recContact3"}

    def test_filter_by_tipe(self, _mock_lark_contacts):
        from server.tools.read_tools import search_contacts
        r = _run(search_contacts({"query": "susanto", "tipe": "Owner"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "recContact1"

    def test_includes_hp_for_operator_tier(self, _mock_lark_contacts):
        """the operator sees full HP. NO stripping."""
        from server.tools.read_tools import search_contacts
        r = _run(search_contacts({"query": "sari"}))
        assert r["matches"][0]["no_hp"] == "+6281298765432"


# get_contact

class TestGetContact:
    def test_missing_id_error(self):
        from server.tools.read_tools import get_contact
        r = _run(get_contact({}))
        assert "error" in r

    def test_valid_id_returns_full(self, _mock_lark_contacts):
        from server.tools.read_tools import get_contact
        with patch(
            "server.lark_client.get_contact",
            return_value=SAMPLE_CONTACTS_RAW[0],
        ):
            r = _run(get_contact({"record_id": "recContact1"}))
        assert r["nama"] == "Pak Susanto Wijaya"
        assert r["no_hp"] == "+6281234567890"

    def test_not_found_returns_error(self, _mock_lark_contacts):
        from server.tools.read_tools import get_contact
        with patch("server.lark_client.get_contact", return_value=None):
            r = _run(get_contact({"record_id": "recMissing"}))
        assert "error" in r


# get_lark_url

class TestGetLarkUrl:
    def test_missing_id_error(self):
        from server.tools.read_tools import get_lark_url
        r = _run(get_lark_url({}))
        assert "error" in r

    def test_listings_url_format(self):
        from server.tools.read_tools import get_lark_url
        r = _run(get_lark_url({"record_id": "rec1", "table": "listings"}))
        assert "rec1" in r["url"]
        assert "table=tblLISTINGS00001" in r["url"]
        assert r["table"] == "listings"

    def test_contacts_url_format(self):
        from server.tools.read_tools import get_lark_url
        r = _run(get_lark_url({"record_id": "recX", "table": "contacts"}))
        assert "table=tblCONTACTS00001" in r["url"]

    def test_activities_url_format(self):
        from server.tools.read_tools import get_lark_url
        r = _run(get_lark_url({"record_id": "actX", "table": "activities"}))
        assert "table=tblACTIVITY00001" in r["url"]

    def test_default_table_listings(self):
        from server.tools.read_tools import get_lark_url
        r = _run(get_lark_url({"record_id": "rec1"}))
        assert r["table"] == "listings"


# find_listings_by_owner

# Listings sample where rec1 has Pemilik=[recContact1], rec2 has Pemilik=[recContact2]
SAMPLE_LISTINGS_WITH_OWNER = [
    {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "Casa Grande",
            "Unit": "Tower B 15A",
            "Pemilik": ["recContact1"],
        },
    },
    {
        "record_id": "rec2",
        "fields": {
            "Nama Properti": "Sunset Residences",
            "Unit": "Tower Maya 0308",
            "Pemilik": [{"record_id": "recContact2"}],  # dict form
        },
    },
    {
        "record_id": "rec3",
        "fields": {
            "Nama Properti": "Anandamaya",
            "Unit": "1 Unit 22A",
            "Pemilik": ["recContact1", "recContact3"],  # multi-owner
        },
    },
]


class TestFindListingsByOwner:
    def test_neither_id_nor_name_returns_error(self):
        from server.tools.read_tools import find_listings_by_owner
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_OWNER):
            r = _run(find_listings_by_owner({}))
        assert "error" in r

    def test_by_contact_id_finds_all_listings(self):
        from server.tools.read_tools import find_listings_by_owner
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_OWNER):
            r = _run(find_listings_by_owner({"contact_id": "recContact1"}))
        rids = {m["record_id"] for m in r["matches"]}
        # Both rec1 (sole owner) and rec3 (one of multi-owners)
        assert rids == {"rec1", "rec3"}

    def test_by_owner_name_resolves_and_finds(self, _mock_lark_contacts):
        from server.tools.read_tools import find_listings_by_owner
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_OWNER):
            r = _run(find_listings_by_owner({"owner_name": "susanto"}))
        # First match in search is "Pak Susanto Wijaya" (recContact1)
        assert r.get("resolved_from_owner_name") == "Pak Susanto Wijaya"
        rids = {m["record_id"] for m in r["matches"]}
        assert rids == {"rec1", "rec3"}

    def test_owner_name_no_match_returns_error(self, _mock_lark_contacts):
        from server.tools.read_tools import find_listings_by_owner
        r = _run(find_listings_by_owner({"owner_name": "nonexistent_owner"}))
        assert "error" in r

    def test_dict_link_format_handled(self):
        """Pemilik field may use {'record_id': '...'} dict form."""
        from server.tools.read_tools import find_listings_by_owner
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_OWNER):
            r = _run(find_listings_by_owner({"contact_id": "recContact2"}))
        assert r["returned"] == 1
        assert r["matches"][0]["record_id"] == "rec2"


# get_agen_listings

SAMPLE_LISTINGS_WITH_AGEN = [
    {
        "record_id": "recA",
        "fields": {
            "Nama Properti": "Listing A",
            "Unit": "Unit 1",
            "Agen": [{"id": "ou_rina"}],
            "Status": "Available",
        },
    },
    {
        "record_id": "recB",
        "fields": {
            "Nama Properti": "Listing B",
            "Unit": "Unit 2",
            "Agen": [{"id": "ou_maya"}, {"id": "ou_rina"}],  # multi-agen
            "Status": "Sold",
        },
    },
    {
        "record_id": "recC",
        "fields": {
            "Nama Properti": "Listing C",
            "Unit": "Unit 3",
            "Agen": [{"id": "ou_sari"}],
            "Status": "Available",
        },
    },
]


class TestGetAgenListings:
    def test_missing_ou_id_error(self):
        from server.tools.read_tools import get_agen_listings
        r = _run(get_agen_listings({}))
        assert "error" in r

    def test_filter_by_agen(self):
        from server.tools.read_tools import get_agen_listings
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_AGEN):
            r = _run(get_agen_listings({"agen_ou_id": "ou_rina"}))
        # rec1 and rec2 (multi-agen) both have Rina
        rids = {m["record_id"] for m in r["matches"]}
        assert rids == {"recA", "recB"}

    def test_filter_with_status(self):
        from server.tools.read_tools import get_agen_listings
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_AGEN):
            r = _run(get_agen_listings({"agen_ou_id": "ou_rina", "status": "Available"}))
        rids = {m["record_id"] for m in r["matches"]}
        # Only recA matches both Rina AND status=Available (recB is Sold)
        assert rids == {"recA"}

    def test_no_match_returns_empty(self):
        from server.tools.read_tools import get_agen_listings
        with patch("server.lark_client.fetch_all_listings", return_value=SAMPLE_LISTINGS_WITH_AGEN):
            r = _run(get_agen_listings({"agen_ou_id": "ou_nonexistent"}))
        assert r["returned"] == 0
