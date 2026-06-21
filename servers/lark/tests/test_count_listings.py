"""Tests for count_listings — cheap aggregation tool."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


COUNT_SAMPLE = [
    # Sunset District, 2BR, Available
    {"record_id": "rec1", "fields": {
        "Nama Properti": "Sunset Residences", "Unit": "U1",
        "Area/Kawasan": "Sunset District - Metro South",
        "Kamar Tidur": 2, "Status": "Available",
        "Agen": [{"id": "ou_2222222222222222222222222222bbbb"}],  # Rina
    }},
    # Sunset District, 2BR, Sold
    {"record_id": "rec2", "fields": {
        "Nama Properti": "Greenview", "Unit": "U2",
        "Area/Kawasan": "Sunset District - Metro South",
        "Kamar Tidur": 2, "Status": "Sold",
        "Agen": [{"id": "ou_1111111111111111111111111111aaaa"}],  # Maya
    }},
    # Sunset District, 3BR, Available
    {"record_id": "rec3", "fields": {
        "Nama Properti": "Sunset Residences", "Unit": "U3",
        "Area/Kawasan": "Sunset District - Metro South",
        "Kamar Tidur": 3, "Status": "Available",
        "Agen": [{"id": "ou_2222222222222222222222222222bbbb"}],  # Rina
    }},
    # Eastside, 2BR, Available
    {"record_id": "rec4", "fields": {
        "Nama Properti": "Casa Grande", "Unit": "U4",
        "Area/Kawasan": "Eastside",
        "Kamar Tidur": 2, "Status": "Available",
        "Agen": [{"id": "ou_3333333333333333333333333333cccc"}],  # Sari
    }},
    # Midtown, 1BR, Available
    {"record_id": "rec5", "fields": {
        "Nama Properti": "Anandamaya", "Unit": "U5",
        "Area/Kawasan": "Midtown",
        "Kamar Tidur": 1, "Status": "Available",
        "Agen": [{"id": "ou_1111111111111111111111111111aaaa"}],  # Maya
    }},
]


@pytest.fixture(autouse=True)
def _mock_listings():
    with patch("server.lark_client.fetch_all_listings", return_value=COUNT_SAMPLE):
        yield


class TestCountListings:
    def test_no_filter_no_group_total_all(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({}))
        assert r["total"] == 5
        assert r["total_lark"] == 5
        assert "breakdown" not in r

    def test_filter_by_area(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"area": "Sunset District"}))
        assert r["total"] == 3  # rec1, rec2, rec3

    def test_filter_by_kt_and_status(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"kt": 2, "status": "Available"}))
        assert r["total"] == 2  # rec1, rec4

    def test_filter_by_agen(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"agen_ou_id": "ou_2222222222222222222222222222bbbb"}))
        assert r["total"] == 2  # rec1, rec3 (both Rina's)

    def test_group_by_area(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"group_by": "area"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["Sunset District - Metro South"] == 3
        assert breakdown_by_key["Eastside"] == 1
        assert breakdown_by_key["Midtown"] == 1
        # Sorted desc
        assert r["breakdown"][0]["count"] >= r["breakdown"][1]["count"]

    def test_group_by_status(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"group_by": "status"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["Available"] == 4
        assert breakdown_by_key["Sold"] == 1

    def test_group_by_kt(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"group_by": "kt"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["KT=2"] == 3
        assert breakdown_by_key["KT=3"] == 1
        assert breakdown_by_key["KT=1"] == 1

    def test_group_by_agen_resolves_names(self):
        """Group by agen should show Rina / Maya / Sari names not ou_ids."""
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"group_by": "agen"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["Rina"] == 2
        assert breakdown_by_key["Maya"] == 2
        assert breakdown_by_key["Sari"] == 1

    def test_filter_plus_group_combined(self):
        """Filter + group_by → only matching listings counted in breakdown."""
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"status": "Available", "group_by": "area"}))
        # Available only: rec1 (Sunset District), rec3 (Sunset District), rec4 (Eastside), rec5 (Midtown)
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["Sunset District - Metro South"] == 2
        assert breakdown_by_key["Eastside"] == 1
        assert breakdown_by_key["Midtown"] == 1
        assert r["total"] == 4
        # Sold excluded → no Sold in breakdown by area

    def test_no_match_returns_zero(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"area": "Antartika"}))
        assert r["total"] == 0
        assert "breakdown" not in r

    def test_no_match_with_group_empty_breakdown(self):
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"area": "Antartika", "group_by": "status"}))
        assert r["total"] == 0
        assert r["breakdown"] == []

    def test_filters_applied_in_output(self):
        """Output includes which filters were applied for transparency."""
        from server.tools.read_tools import count_listings
        r = _run(count_listings({"area": "Sunset District", "kt": 2}))
        assert r["filters_applied"] == {"area": "Sunset District", "kt": 2}


# Regression: multi-agen listing counts in EVERY agen's bucket

MULTI_AGEN_SAMPLE = [
    # rec1: assigned to BOTH Rina AND Sari
    {"record_id": "rec1", "fields": {
        "Nama Properti": "X", "Unit": "U1", "Status": "Available",
        "Agen": [
            {"id": "ou_2222222222222222222222222222bbbb"},  # Rina
            {"id": "ou_3333333333333333333333333333cccc"},  # Sari
        ],
    }},
    # rec2: Rina only
    {"record_id": "rec2", "fields": {
        "Nama Properti": "Y", "Unit": "U2", "Status": "Available",
        "Agen": [{"id": "ou_2222222222222222222222222222bbbb"}],
    }},
    # rec3: Sari only
    {"record_id": "rec3", "fields": {
        "Nama Properti": "Z", "Unit": "U3", "Status": "Available",
        "Agen": [{"id": "ou_3333333333333333333333333333cccc"}],
    }},
]


class TestMultiAgenGrouping:
    def test_multi_agen_counted_in_each_bucket(self):
        """Listing assigned to N agen should add 1 to EACH agen's bucket."""
        from server.tools.read_tools import count_listings
        from unittest.mock import patch
        with patch("server.lark_client.fetch_all_listings", return_value=MULTI_AGEN_SAMPLE):
            r = _run(count_listings({"group_by": "agen"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        # rec1 counted in both Rina AND Sari
        # rec2 counted in Rina only
        # rec3 counted in Sari only
        assert breakdown_by_key["Rina"] == 2  # rec1 + rec2
        assert breakdown_by_key["Sari"] == 2  # rec1 + rec3
        # No "Rina, Sari" joined bucket (regression fix)
        assert "Rina, Sari" not in breakdown_by_key
        assert "Sari, Rina" not in breakdown_by_key
        # total_matched stays 3 (listings, not agen-listing pairs)
        assert r["total"] == 3

    def test_multi_agen_no_overlap_unchanged(self):
        """When no multi-agen overlap, counts identical to old behavior."""
        from server.tools.read_tools import count_listings
        from unittest.mock import patch
        single_agen = [MULTI_AGEN_SAMPLE[1], MULTI_AGEN_SAMPLE[2]]  # no multi
        with patch("server.lark_client.fetch_all_listings", return_value=single_agen):
            r = _run(count_listings({"group_by": "agen"}))
        breakdown_by_key = {b["key"]: b["count"] for b in r["breakdown"]}
        assert breakdown_by_key["Rina"] == 1
        assert breakdown_by_key["Sari"] == 1
