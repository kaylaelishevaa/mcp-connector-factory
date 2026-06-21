"""Regression: normalize_listing reads CORRECT Lark field names.

CC review caught my drift: was reading "Tipe" / "Furnished" / "Audit Trail"
which DON'T EXIST on Listings table. Correct fields per schema doc:
- "Tipe Listing" (Sewa/Jual)
- "Tipe Properti" (Apt/House/etc)
- "Kondisi" (Furnished/Semi/Unfurnished)
- "Flag Audit" (MultiSelect audit flags)
"""
from __future__ import annotations

from server.lark_client import (
    normalize_activity,
    normalize_contact,
    normalize_listing,
)


def test_reads_tipe_listing_not_tipe():
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "Casa Grande",
            "Unit": "15A",
            "Tipe Listing": {"text": "Sewa"},
            "Tipe": "should-be-ignored",  # this is a legacy/wrong key
        },
    }
    norm = normalize_listing(raw)
    assert norm["tipe_listing"] == "Sewa"
    # No "tipe" key — only tipe_listing + tipe_properti
    assert "tipe" not in norm


def test_reads_tipe_properti():
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Tipe Properti": {"text": "Apartment"},
        },
    }
    norm = normalize_listing(raw)
    assert norm["tipe_properti"] == "Apartment"


def test_reads_kondisi_not_furnished():
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Kondisi": {"text": "Furnished"},
            "Furnished": "should-be-ignored",
        },
    }
    norm = normalize_listing(raw)
    assert norm["kondisi"] == "Furnished"
    assert "furnished" not in norm


def test_reads_flag_audit_not_audit_trail():
    """Flag Audit is MultiSelect on Listings; Audit Trail doesn't exist."""
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Flag Audit": [
                {"text": "Dedup field berbeda"},
                {"text": "TRO bisa ke Pemilik"},
            ],
            "Audit Trail": "should-be-ignored (doesn't exist on Listings)",
        },
    }
    norm = normalize_listing(raw)
    assert norm["flag_audit"] == ["Dedup field berbeda", "TRO bisa ke Pemilik"]
    assert "audit_trail" not in norm


def test_flag_audit_empty_list_when_missing():
    raw = {"record_id": "rec1", "fields": {"Nama Properti": "X", "Unit": "1"}}
    norm = normalize_listing(raw)
    assert norm["flag_audit"] == []


def test_flag_audit_handles_string_form():
    """Some Lark responses may give MultiSelect as list of strings."""
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Flag Audit": ["Pemilik belum terisi"],
        },
    }
    norm = normalize_listing(raw)
    assert norm["flag_audit"] == ["Pemilik belum terisi"]


def test_all_three_renamed_fields_present_together():
    """End-to-end: realistic Listing record with all 3 renamed fields."""
    raw = {
        "record_id": "recFull",
        "fields": {
            "Nama Properti": "Casa Grande",
            "Unit": "Tower B 15A",
            "Area/Kawasan": "Eastside",
            "Kamar Tidur": 2, "Kamar Mandi": 2,
            "Harga Sewa": 5000000,
            "Tipe Listing": "Sewa",
            "Tipe Properti": {"text": "Apartment"},
            "Kondisi": {"text": "Fully Furnished"},
            "Flag Audit": [{"text": "Pemilik belum terisi"}],
            "Status": "Available",
        },
    }
    norm = normalize_listing(raw)
    assert norm["tipe_listing"] == "Sewa"
    assert norm["tipe_properti"] == "Apartment"
    assert norm["kondisi"] == "Fully Furnished"
    assert norm["flag_audit"] == ["Pemilik belum terisi"]
    # Other fields still work
    assert norm["nama_properti"] == "Casa Grande"
    assert norm["kt"] == 2
    assert norm["harga_sewa"] == 5000000


# _extra_fields exposes non-curated custom fields

def test_listing_extra_fields_holds_custom_fields():
    """Custom Listing fields (not in the curated set) surface in _extra_fields."""
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Jalur Pemasaran": "Direct",          # custom
            "Sumber Data": "Stok Properti",       # custom
            "Tersewa Sampai": 1748000000000,      # custom
        },
    }
    norm = normalize_listing(raw)
    assert "_extra_fields" in norm
    assert norm["_extra_fields"]["Jalur Pemasaran"] == "Direct"
    assert norm["_extra_fields"]["Sumber Data"] == "Stok Properti"
    # Curated fields are NOT duplicated into _extra_fields
    assert "Nama Properti" not in norm["_extra_fields"]
    assert "Unit" not in norm["_extra_fields"]


def test_extra_fields_drops_nulls_and_empties():
    raw = {
        "record_id": "rec1",
        "fields": {
            "Nama Properti": "X", "Unit": "1",
            "Empty Str": "   ",
            "Null Field": None,
            "Empty List": [],
            "Real Custom": "keep me",
        },
    }
    extra = normalize_listing(raw)["_extra_fields"]
    assert "Empty Str" not in extra
    assert "Null Field" not in extra
    assert "Empty List" not in extra
    assert extra["Real Custom"] == "keep me"


def test_contact_extra_fields_holds_alert_tier():
    """The custom field that motivated generic query — 'Priority Tier' must reach _extra_fields."""
    raw = {
        "record_id": "recC1",
        "fields": {
            "Nama": "Pak Budi",
            "No HP": "+628123",
            "Priority Tier": "🔴 Kritis",            # custom — the regression
            "Owner Notes": "Butuh dana cepat",
            "Deal Stage": "Negosiasi",
        },
    }
    norm = normalize_contact(raw)
    assert norm["_extra_fields"]["Priority Tier"] == "🔴 Kritis"
    assert norm["_extra_fields"]["Owner Notes"] == "Butuh dana cepat"
    assert norm["_extra_fields"]["Deal Stage"] == "Negosiasi"
    # Curated identity field not duplicated
    assert "Nama" not in norm["_extra_fields"]


def test_activity_extra_fields_present():
    raw = {
        "record_id": "recA1",
        "fields": {
            "Judul": "Showing X",
            "Custom Activity Field": "value",
        },
    }
    norm = normalize_activity(raw)
    assert norm["_extra_fields"]["Custom Activity Field"] == "value"
    assert "Judul" not in norm["_extra_fields"]
