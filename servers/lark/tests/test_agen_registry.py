"""Unit tests for agen_registry — lookup + resolution."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from server import agen_registry


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# lookup_by_ou_id

def test_lookup_by_ou_id_exact_match():
    info = agen_registry.lookup_by_ou_id("ou_2222222222222222222222222222bbbb")
    assert info["nama_display"] == "Rina"
    assert info["email"] == "marketing@example.com"


def test_lookup_by_ou_id_missing():
    assert agen_registry.lookup_by_ou_id("ou_nonexistent") is None
    assert agen_registry.lookup_by_ou_id("") is None
    assert agen_registry.lookup_by_ou_id(None) is None  # type: ignore


# lookup_by_name

def test_lookup_by_name_exact():
    info = agen_registry.lookup_by_name("Rina")
    assert info["ou_id"] == "ou_2222222222222222222222222222bbbb"


def test_lookup_by_name_case_insensitive():
    info = agen_registry.lookup_by_name("RINA")
    assert info["nama_display"] == "Rina"


def test_lookup_by_name_alias():
    """'Rini' is an alias for Rina."""
    info = agen_registry.lookup_by_name("Rini")
    assert info["nama_display"] == "Rina"


def test_lookup_by_name_substring():
    """'Putr' should substring-match 'Putri'."""
    info = agen_registry.lookup_by_name("Putr")
    assert info["nama_display"] == "Putri"


def test_lookup_by_name_no_match():
    assert agen_registry.lookup_by_name("Nobody") is None


# resolve (combined)

def test_resolve_ou_id_path():
    info = agen_registry.resolve("ou_1111111111111111111111111111aaaa")
    assert info["nama_display"] == "Maya"


def test_resolve_name_path():
    info = agen_registry.resolve("Sari")
    assert info["ou_id"] == "ou_3333333333333333333333333333cccc"


def test_resolve_empty():
    assert agen_registry.resolve("") is None


# all_agen

def test_all_agen_returns_six_plus():
    agen = agen_registry.all_agen()
    # 5 production + 1 VIP = 6
    assert len(agen) == 6
    names = {a["nama_display"] for a in agen}
    assert "Maya" in names
    assert "Rina" in names
    assert "Sari" in names
    assert "Dimas" in names
    assert "Putri" in names
    assert "Budi" in names


def test_all_agen_excludes_aliases_field():
    """Aliases are internal — not exposed in public output."""
    for a in agen_registry.all_agen():
        assert "aliases" not in a


# resolve_ou_ids_to_names

def test_resolve_bulk_known():
    names = agen_registry.resolve_ou_ids_to_names([
        "ou_2222222222222222222222222222bbbb",  # Rina
        "ou_3333333333333333333333333333cccc",  # Sari
    ])
    assert names == ["Rina", "Sari"]


def test_resolve_bulk_unknown_passthrough():
    names = agen_registry.resolve_ou_ids_to_names(["ou_unknownXYZ123abc"])
    assert len(names) == 1
    assert "unknown" in names[0]


def test_resolve_bulk_empty_input():
    assert agen_registry.resolve_ou_ids_to_names([]) == []


# list_agen tool

class TestListAgen:
    def test_default_includes_vip(self):
        from server.tools.read_tools import list_agen
        r = _run(list_agen({}))
        assert r["total"] == 6
        names = {a["nama_display"] for a in r["agen"]}
        assert "Budi" in names

    def test_exclude_vip(self):
        from server.tools.read_tools import list_agen
        r = _run(list_agen({"include_vip": False}))
        assert r["total"] == 5
        names = {a["nama_display"] for a in r["agen"]}
        assert "Budi" not in names


# resolve_agen tool

class TestResolveAgenTool:
    def test_resolve_by_name(self):
        from server.tools.read_tools import resolve_agen
        r = _run(resolve_agen({"query": "Rina"}))
        assert r["nama_display"] == "Rina"
        assert r["ou_id"] == "ou_2222222222222222222222222222bbbb"

    def test_resolve_by_ou_id(self):
        from server.tools.read_tools import resolve_agen
        r = _run(resolve_agen({"query": "ou_1111111111111111111111111111aaaa"}))
        assert r["nama_display"] == "Maya"

    def test_resolve_alias(self):
        from server.tools.read_tools import resolve_agen
        r = _run(resolve_agen({"query": "Rini"}))
        assert r["nama_display"] == "Rina"

    def test_resolve_no_match(self):
        from server.tools.read_tools import resolve_agen
        r = _run(resolve_agen({"query": "Nobody"}))
        assert "error" in r

    def test_resolve_empty_query(self):
        from server.tools.read_tools import resolve_agen
        r = _run(resolve_agen({}))
        assert "error" in r


# find_activities enrichment

SAMPLE_ACTIVITY_WITH_KNOWN_AGEN = [
    {
        "record_id": "act1",
        "fields": {
            "Judul": "Showing X",
            "Waktu Mulai": 1747843200000,
            "Tipe": "Showing",
            "Status": "Dijadwalkan",
            "Agen": [{"id": "ou_2222222222222222222222222222bbbb"}],  # Rina
        },
    },
    {
        "record_id": "act2",
        "fields": {
            "Judul": "Meeting Y",
            "Waktu Mulai": 1747843200000,
            "Agen": [
                {"id": "ou_1111111111111111111111111111aaaa"},  # Maya
                {"id": "ou_3333333333333333333333333333cccc"},  # Sari
            ],
        },
    },
]


def test_find_activities_enriches_with_agen_names():
    from server.tools.read_tools import find_activities
    with patch("server.lark_client.fetch_all_activities", return_value=SAMPLE_ACTIVITY_WITH_KNOWN_AGEN):
        r = _run(find_activities({}))
    by_id = {m["record_id"]: m for m in r["matches"]}
    assert by_id["act1"]["agen_names"] == ["Rina"]
    assert set(by_id["act2"]["agen_names"]) == {"Maya", "Sari"}


# get_listing pemilik_resolved enrichment

SAMPLE_LISTING_WITH_OWNER = {
    "record_id": "rec1",
    "fields": {
        "Nama Properti": "Casa Grande",
        "Unit": "Tower B 15A",
        "Pemilik": ["recContact1"],
        "Agen": [{"id": "ou_2222222222222222222222222222bbbb"}],
    },
}

SAMPLE_CONTACT_FOR_PEMILIK = {
    "record_id": "recContact1",
    "fields": {
        "Nama": "Pak Susanto",
        "No HP": "+6281234567890",
        "Tipe Contact": [{"text": "Owner"}],
    },
}


def test_get_listing_enriches_pemilik_resolved():
    from server.tools.read_tools import get_listing
    with patch(
        "server.lark_client.get_record",
        return_value=SAMPLE_LISTING_WITH_OWNER,
    ), patch(
        "server.lark_client.get_contact",
        return_value=SAMPLE_CONTACT_FOR_PEMILIK,
    ):
        r = _run(get_listing({"record_id": "rec1"}))
    assert "pemilik_resolved" in r
    assert len(r["pemilik_resolved"]) == 1
    assert r["pemilik_resolved"][0]["nama"] == "Pak Susanto"
    assert r["pemilik_resolved"][0]["no_hp"] == "+6281234567890"
    # Also enriched agen_names
    assert r["agen_names"] == ["Rina"]


def test_get_listing_handles_pemilik_fetch_failure():
    from server.tools.read_tools import get_listing
    with patch(
        "server.lark_client.get_record",
        return_value=SAMPLE_LISTING_WITH_OWNER,
    ), patch(
        "server.lark_client.get_contact",
        return_value=None,  # contact fetch failed
    ):
        r = _run(get_listing({"record_id": "rec1"}))
    assert r["pemilik_resolved"][0].get("error")
