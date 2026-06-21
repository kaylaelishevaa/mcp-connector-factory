"""Schema introspection (list_tables, describe_table) + generic
query_records. Implements the test shapes from
shared-architecture.md §"Tests both must include" (items
1-9; PII-strip items 10-14 are Q&A-bot-only).

All Lark access is mocked — no live network.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from server import lark_client


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# Sample data

FIELD_DEFS = [
    {"field_name": "Nama", "type": 1},
    {"field_name": "Priority Tier", "type": 3},
    {"field_name": "Alert Kategori", "type": 4},
    {"field_name": "Agen", "type": 11},
    {"field_name": "Deal Budget", "type": 2},
    {"field_name": "No HP", "type": 13},
    {"field_name": "Tanggal Aksi Berikutnya", "type": 5},
]

RECORDS = [
    {"record_id": "r1", "fields": {
        "Nama": "Budi Santoso",
        "Priority Tier": "🔴 Kritis",
        "Alert Kategori": [{"text": "Deal Stall"}],
        "Agen": [{"id": "ou_eri", "name": "Maya"}],
        "Deal Budget": 5_000_000_000,
        "No HP": "+628111",
        "Tanggal Aksi Berikutnya": 1747000000000,
    }},
    {"record_id": "r2", "fields": {
        "Nama": "Sari Rahmawati",
        "Priority Tier": "🟡 Tinggi",
        "Agen": [{"id": "ou_kar", "name": "Rina"}],
        "Deal Budget": 2_000_000_000,
        "No HP": "+628222",
    }},
    {"record_id": "r3", "fields": {
        "Nama": "Budiman Wijaya",
        "Priority Tier": "🔴 Kritis",
        "Agen": [{"id": "ou_eri", "name": "Maya"}],
        "Deal Budget": 8_000_000_000,
        "No HP": "+628333",
    }},
]

TABLES = [
    {"table_id": "tblContacts", "name": "Contacts", "record_count": 9563},
    {"table_id": "tblListings", "name": "Listings", "record_count": 9786},
]


@pytest.fixture
def _mock_schema():
    """Mock the schema-layer functions used by the three tools."""
    def fake_resolve(name):
        for t in TABLES:
            if t["name"].lower() == (name or "").strip().lower():
                return t["table_id"]
        return None

    with patch("server.lark_client.resolve_table_name", side_effect=fake_resolve), \
         patch("server.lark_client.get_table_fields", return_value=FIELD_DEFS), \
         patch("server.lark_client.get_tables", return_value=TABLES), \
         patch("server.lark_client.get_all_records", return_value=RECORDS), \
         patch("server.lark_client.schema_synced_at_iso", return_value="2026-05-24T00:00:00Z"):
        yield


# Schema cache populates at startup (contract test #1)

def test_schema_cache_populates_at_startup():
    with patch("server.lark_client._fetch_tables", return_value=[{"table_id": "tblX", "name": "Contacts"}]), \
         patch("server.lark_client._fetch_fields", return_value=FIELD_DEFS), \
         patch("server.lark_client._table_total", return_value=9563):
        schema = lark_client.fetch_schema(force_refresh=True)
    assert schema["tables"][0]["name"] == "Contacts"
    assert schema["tables"][0]["record_count"] == 9563
    assert schema["fields"]["tblX"] == FIELD_DEFS


# list_tables (contract test #2)

class TestListTables:
    def test_returns_expected_tables(self, _mock_schema):
        from server.tools.read_tools import list_tables
        r = _run(list_tables({}))
        names = {t["table_name"] for t in r["tables"]}
        assert names == {"Contacts", "Listings"}
        assert r["total"] == 2

    def test_includes_record_count_and_sync_time(self, _mock_schema):
        from server.tools.read_tools import list_tables
        r = _run(list_tables({}))
        contacts = next(t for t in r["tables"] if t["table_name"] == "Contacts")
        assert contacts["record_count"] == 9563
        assert contacts["last_synced_at"] == "2026-05-24T00:00:00Z"


# describe_table (contract test #3)

class TestDescribeTable:
    def test_lists_fields_with_types(self, _mock_schema):
        from server.tools.read_tools import describe_table
        r = _run(describe_table({"table_name": "Contacts"}))
        by_name = {f["name"]: f for f in r["fields"]}
        assert by_name["Priority Tier"]["type"] == "single_select"
        assert by_name["Deal Budget"]["type"] == "number"
        assert by_name["Agen"]["type"] == "person"

    def test_flags_pii_fields(self, _mock_schema):
        from server.tools.read_tools import describe_table
        r = _run(describe_table({"table_name": "Contacts"}))
        by_name = {f["name"]: f for f in r["fields"]}
        assert by_name["No HP"]["is_pii"] is True       # matches \bhp\b
        assert by_name["Nama"]["is_pii"] is False
        assert by_name["Priority Tier"]["is_pii"] is False

    def test_sample_values_extracted_human_readable(self, _mock_schema):
        from server.tools.read_tools import describe_table
        r = _run(describe_table({"table_name": "Contacts"}))
        by_name = {f["name"]: f for f in r["fields"]}
        # Person field samples should be names, not raw dicts
        assert "Maya" in by_name["Agen"]["sample_values"]
        # SingleSelect samples include the emoji-prefixed value
        assert "🔴 Kritis" in by_name["Priority Tier"]["sample_values"]

    def test_unknown_table_graceful_error(self, _mock_schema):
        from server.tools.read_tools import describe_table
        r = _run(describe_table({"table_name": "Nonexistent"}))
        assert "error" in r

    def test_missing_table_name_error(self, _mock_schema):
        from server.tools.read_tools import describe_table
        r = _run(describe_table({}))
        assert "error" in r


# query_records (contract tests #4-8)

class TestQueryRecords:
    def test_field_filter_exact(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Priority Tier": "🔴 Kritis"},
        }))
        rids = {rec["record_id"] for rec in r["records"]}
        assert rids == {"r1", "r3"}
        assert r["matched_count"] == 2

    def test_emoji_normalization_plain_kritis_matches(self, _mock_schema):
        """The acceptance case: 'Kritis' matches stored '🔴 Kritis'."""
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Priority Tier": "Kritis"},
        }))
        rids = {rec["record_id"] for rec in r["records"]}
        assert rids == {"r1", "r3"}

    def test_person_field_filter_by_name(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Agen": "Maya"},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1", "r3"}

    def test_acceptance_query_alert_tier_and_agen(self, _mock_schema):
        """Full the operator query shape: Priority Tier=Kritis AND Agen=Maya."""
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Priority Tier": "Kritis", "Agen": "Maya"},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1", "r3"}
        # Full record returned (not curated subset) — Priority Tier present
        assert r["records"][0]["fields"]["Priority Tier"] == "🔴 Kritis"

    def test_text_search(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts", "text_search": "budi"}))
        # "Budi Santoso" + "Budiman Wijaya"
        assert {rec["record_id"] for rec in r["records"]} == {"r1", "r3"}

    def test_field_filter_and_text_search_combined(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Priority Tier": "Kritis"},
            "text_search": "budiman",
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r3"}

    def test_op_gt(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "gt", "value": 3_000_000_000}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1", "r3"}

    def test_op_lte(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "lte", "value": 2_000_000_000}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r2"}

    def test_op_contains(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Nama": {"op": "contains", "value": "wijaya"}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r3"}

    def test_list_any_of(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Agen": ["Rina", "Nobody"]},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r2"}

    def test_empty_filter_returns_all(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts"}))
        assert r["matched_count"] == 3
        assert r["truncated"] is False

    def test_limit_and_truncated_flag(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts", "limit": 1}))
        assert len(r["records"]) == 1
        assert r["matched_count"] == 3   # true total despite truncation
        assert r["truncated"] is True

    def test_sort_by_descending(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts", "sort_by": "-Deal Budget"}))
        budgets = [rec["fields"]["Deal Budget"] for rec in r["records"]]
        assert budgets == [8_000_000_000, 5_000_000_000, 2_000_000_000]

    def test_sort_by_ascending(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts", "sort_by": "Deal Budget"}))
        budgets = [rec["fields"]["Deal Budget"] for rec in r["records"]]
        assert budgets == [2_000_000_000, 5_000_000_000, 8_000_000_000]

    def test_return_fields_projection(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Nama": "Budi Santoso"},
            "return_fields": ["Nama", "Priority Tier"],
        }))
        rec = r["records"][0]
        assert set(rec["fields"].keys()) == {"Nama", "Priority Tier"}
        assert "No HP" not in rec["fields"]

    def test_return_fields_star_returns_all(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Nama": "Budi Santoso"},
            "return_fields": "*",
        }))
        assert "No HP" in r["records"][0]["fields"]

    # error paths

    def test_unknown_table_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Ghost"}))
        assert "error" in r

    def test_unknown_field_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Made Up Field": "x"},
        }))
        assert "error" in r
        assert "available_fields" in r

    def test_invalid_op_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "between", "value": 5}},
        }))
        assert "error" in r
        assert "supported_ops" in r

    def test_missing_table_name_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({}))
        assert "error" in r

    def test_lark_error_wrapped(self, _mock_schema):
        from server.tools.read_tools import query_records
        with patch(
            "server.lark_client.get_all_records",
            side_effect=lark_client.LarkClientError("Lark down"),
        ):
            r = _run(query_records({"table_name": "Contacts"}))
        assert "error" in r
        assert "Lark down" in r["error"]


# Registry

def test_new_tools_in_registry():
    from server.tools.read_tools import TOOL_HANDLERS, TOOL_SCHEMAS
    for name in ("list_tables", "describe_table", "query_records"):
        assert name in TOOL_HANDLERS
    schema_names = {t["name"] for t in TOOL_SCHEMAS}
    assert {"list_tables", "describe_table", "query_records"} <= schema_names


def test_query_records_schema_uses_contract_param_names():
    """Naming consistency (contract §"Naming consistency"): exact param names."""
    from server.tools.read_tools import TOOL_SCHEMAS
    qr = next(t for t in TOOL_SCHEMAS if t["name"] == "query_records")
    props = qr["inputSchema"]["properties"]
    assert "table_name" in props
    assert "field_filters" in props
    assert "text_search" in props
    assert "return_fields" in props
    assert "sort_by" in props
    # forbidden legacy names
    assert "table" not in props
    assert "filters" not in props
    assert "query" not in props


# Curated tool now returns _extra_fields (contract test #9)

def test_get_contact_output_includes_extra_fields():
    from server.tools.read_tools import get_contact
    raw = {
        "record_id": "recC1",
        "fields": {
            "Nama": "Pak Budi",
            "No HP": "+628123",
            "Priority Tier": "🔴 Kritis",
        },
    }
    with patch("server.lark_client.get_contact", return_value=raw):
        r = _run(get_contact({"record_id": "recC1"}))
    assert "_extra_fields" in r
    assert r["_extra_fields"]["Priority Tier"] == "🔴 Kritis"
    # _raw_fields still stripped from tool output
    assert "_raw_fields" not in r


# GAP-2: between op

class TestBetweenOp:
    def test_between_number_inclusive_low_exclusive_high(self, _mock_schema):
        """[5e9, 8e9] → r1 (5e9, included) but NOT r3 (8e9, excluded)."""
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "between", "value": [5_000_000_000, 8_000_000_000]}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1"}

    def test_between_number_range(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "between", "value": [3_000_000_000, 9_000_000_000]}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1", "r3"}

    def test_between_datetime(self, _mock_schema):
        """Only r1 has Tanggal Aksi Berikutnya=1747000000000."""
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Tanggal Aksi Berikutnya": {"op": "between", "value": [1_746_000_000_000, 1_748_000_000_000]}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"r1"}

    def test_between_reversed_bounds_graceful_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "between", "value": [9, 1]}},
        }))
        assert "error" in r
        assert "reversed" in r["error"].lower()

    def test_between_non_list_value_graceful_error(self, _mock_schema):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "field_filters": {"Deal Budget": {"op": "between", "value": 5}},
        }))
        assert "error" in r

    def test_between_in_supported_ops(self):
        from server.tools.read_tools import _FILTER_OPS
        assert "between" in _FILTER_OPS


# GAP-4: heavy-field exclusion + value cap

HEAVY_FIELD_DEFS = [
    {"field_name": "Nama", "type": 1},
    {"field_name": "History Chat WA Budi", "type": 1},
    {"field_name": "Ringkasan Situasi", "type": 1},
    {"field_name": "Aksi Berikutnya", "type": 1},
]

HEAVY_RECORDS = [
    {"record_id": "h1", "fields": {
        "Nama": "Budi",
        "History Chat WA Budi": "x" * 5000,   # heavy + oversized
        "Ringkasan Situasi": "long note",
        "Aksi Berikutnya": "follow up",
    }},
]


@pytest.fixture
def _mock_heavy():
    def fake_resolve(name):
        return "tblHeavy" if (name or "").strip().lower() == "contacts" else None
    with patch("server.lark_client.resolve_table_name", side_effect=fake_resolve), \
         patch("server.lark_client.get_table_fields", return_value=HEAVY_FIELD_DEFS), \
         patch("server.lark_client.get_tables", return_value=[{"table_id": "tblHeavy", "name": "Contacts"}]), \
         patch("server.lark_client.get_all_records", return_value=HEAVY_RECORDS):
        yield


class TestHeavyFieldGuards:
    def test_star_excludes_heavy_fields(self, _mock_heavy):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts"}))
        fields = r["records"][0]["fields"]
        assert "History Chat WA Budi" not in fields
        assert "Ringkasan Situasi" not in fields
        assert "Nama" in fields                       # non-heavy kept
        assert "Aksi Berikutnya" in fields            # 'aksi' is not 'notes'

    def test_excluded_heavy_fields_listed(self, _mock_heavy):
        from server.tools.read_tools import query_records
        r = _run(query_records({"table_name": "Contacts"}))
        assert "History Chat WA Budi" in r["_excluded_heavy_fields"]
        assert "Ringkasan Situasi" in r["_excluded_heavy_fields"]

    def test_explicit_return_fields_opts_back_in(self, _mock_heavy):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "return_fields": ["History Chat WA Budi"],
        }))
        # opted in → present (but capped), and no _excluded_heavy_fields key
        assert "History Chat WA Budi" in r["records"][0]["fields"]
        assert "_excluded_heavy_fields" not in r

    def test_value_capped_at_2000(self, _mock_heavy):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Contacts",
            "return_fields": ["History Chat WA Budi"],
        }))
        val = r["records"][0]["fields"]["History Chat WA Budi"]
        assert val.endswith("…[truncated]")
        assert len(val) <= 2000 + len("…[truncated]")

    def test_allowlist_override_prevents_exclusion(self, _mock_heavy, monkeypatch):
        import server.tools.read_tools as rt
        monkeypatch.setattr(rt, "_HEAVY_FIELD_ALLOWLIST", frozenset({"Ringkasan Situasi"}))
        r = _run(rt.query_records({"table_name": "Contacts"}))
        fields = r["records"][0]["fields"]
        assert "Ringkasan Situasi" in fields                          # allowlisted → kept
        assert "History Chat WA Budi" not in fields                # still excluded
        assert "Ringkasan Situasi" not in r.get("_excluded_heavy_fields", [])


# GAP-1 + GAP-3: tool description copy (regression)

def test_query_records_description_has_contains_and_between_guidance():
    from server.tools.read_tools import TOOL_SCHEMAS
    desc = next(t for t in TOOL_SCHEMAS if t["name"] == "query_records")["description"]
    assert "contains" in desc                 # GAP-1 compound-value guidance
    assert "between" in desc                   # GAP-2 range guidance
    assert "describe_table FIRST" in desc      # GAP-3 directive


def test_describe_table_description_is_directive():
    from server.tools.read_tools import TOOL_SCHEMAS
    desc = next(t for t in TOOL_SCHEMAS if t["name"] == "describe_table")["description"]
    assert "ALWAYS call describe_table" in desc  # GAP-3 directive trigger


# GAP-5: Lark Number fields stored as strings

def test_as_number_coercion():
    from server.tools.read_tools import _as_number
    assert _as_number("59200000") == 59200000.0
    assert _as_number("1,500,000") == 1500000.0   # commas stripped
    assert _as_number(50) == 50.0
    assert _as_number(3.5) == 3.5
    assert _as_number("abc") is None
    assert _as_number(None) is None
    assert _as_number(True) is None               # bool is NOT a number here
    assert _as_number("") is None


NUMSTR_FIELD_DEFS = [
    {"field_name": "Nama Properti", "type": 3},
    {"field_name": "Harga Sewa", "type": 2},
]
NUMSTR_RECORDS = [
    {"record_id": "n1", "fields": {"Nama Properti": "A", "Harga Sewa": "40000000"}},   # string
    {"record_id": "n2", "fields": {"Nama Properti": "B", "Harga Sewa": "59200000"}},   # string
    {"record_id": "n3", "fields": {"Nama Properti": "C", "Harga Sewa": 30000000}},     # int
    {"record_id": "n4", "fields": {"Nama Properti": "D"}},                              # missing
]


@pytest.fixture
def _mock_numstr():
    def fake_resolve(name):
        return "tblNum" if (name or "").strip().lower() == "listings" else None
    with patch("server.lark_client.resolve_table_name", side_effect=fake_resolve), \
         patch("server.lark_client.get_table_fields", return_value=NUMSTR_FIELD_DEFS), \
         patch("server.lark_client.get_tables", return_value=[{"table_id": "tblNum", "name": "Listings"}]), \
         patch("server.lark_client.get_all_records", return_value=NUMSTR_RECORDS):
        yield


class TestNumericStringFields:
    def test_between_coerces_string_numbers(self, _mock_numstr):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Listings",
            "field_filters": {"Harga Sewa": {"op": "between", "value": [0, 50_000_000]}},
        }))
        # n1 (40M str) + n3 (30M int) in range; n2 (59.2M) excluded; n4 missing
        assert {rec["record_id"] for rec in r["records"]} == {"n1", "n3"}

    def test_gt_coerces_string_numbers(self, _mock_numstr):
        from server.tools.read_tools import query_records
        r = _run(query_records({
            "table_name": "Listings",
            "field_filters": {"Harga Sewa": {"op": "gt", "value": 35_000_000}},
        }))
        assert {rec["record_id"] for rec in r["records"]} == {"n1", "n2"}  # 40M, 59.2M


def test_curated_harga_max_handles_string_prices():
    """Regression: search_listings harga_max must not crash / under-match on
    string-valued Harga Sewa (latent since the first read tools)."""
    from server.tools.read_tools import search_listings
    sample = [
        {"record_id": "s1", "fields": {"Nama Properti": "A", "Unit": "1", "Harga Sewa": "40000000", "Status": "Available"}},
        {"record_id": "s2", "fields": {"Nama Properti": "B", "Unit": "2", "Harga Sewa": "80000000", "Status": "Available"}},
    ]
    with patch("server.lark_client.fetch_all_listings", return_value=sample):
        r = _run(search_listings({"harga_max": 50_000_000}))
    rids = {m["record_id"] for m in r["matches"]}
    assert rids == {"s1"}  # 40M kept, 80M filtered — no TypeError
