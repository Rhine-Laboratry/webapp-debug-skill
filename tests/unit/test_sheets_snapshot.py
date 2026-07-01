from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes.google_sheets_service import FakeGoogleApiError, FakeGoogleSheetsService
from webapp_debug_skill.google_sheets_backend import GoogleSheetsBackend
from webapp_debug_skill.sheets_init import (
    CanonicalSheetsSchema,
    CanonicalTab,
    load_canonical_schema,
)
from webapp_debug_skill.sheets_snapshot import (
    SheetsSnapshotError,
    SheetsSnapshotExporter,
    bounded_a1_range,
    parse_tab_rows,
)

SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SNAPSHOT"


def schema() -> CanonicalSheetsSchema:
    return load_canonical_schema(SCHEMA)


def canonical_sheets(
    *, inventory_rows: list[list[str]] | None = None
) -> dict[str, list[list[str]]]:
    result: dict[str, list[list[str]]] = {}
    for tab in schema().tabs:
        result[tab.name] = [list(tab.headers)]
    if inventory_rows is None:
        inventory_rows = [inventory_row("INV-001", "MAPPED", "HIGH")]
    result["Inventory"].extend(inventory_rows)
    return result


def inventory_row(inventory_id: str, status: str, risk: str, notes: str = "") -> list[str]:
    headers = next(tab.headers for tab in schema().tabs if tab.name == "Inventory")
    values = {header: "" for header in headers}
    values.update(
        {
            "inventory_id": inventory_id,
            "feature_area": "Accounts",
            "item_type": "UI_PAGE",
            "name": "Login",
            "source_path": "src/login.php",
            "source_fingerprint": "sha256:test",
            "test_scope": "UI_PAGE",
            "discovery_status": status,
            "risk": risk,
            "discovered_at": "2026-07-01T00:00:00Z",
            "last_seen_commit": "abc",
            "last_seen_at": "2026-07-01T00:00:00Z",
            "notes": notes,
        }
    )
    return [values[header] for header in headers]


def exporter(service: FakeGoogleSheetsService) -> SheetsSnapshotExporter:
    backend = GoogleSheetsBackend(spreadsheet_id=service.spreadsheet_id, service=service)
    return SheetsSnapshotExporter(
        reader=backend,
        clock=lambda: datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
    )


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_canonical_snapshot_top_level_inventory_and_selected_tabs() -> None:
    service = FakeGoogleSheetsService(sheets=canonical_sheets())

    snapshot = exporter(service).export(schema(), tabs=("Inventory",), max_rows_per_tab=100)

    assert list(snapshot.payload["tabs"]) == ["Inventory"]
    assert snapshot.payload["Inventory"] == snapshot.payload["tabs"]["Inventory"]
    assert snapshot.payload["Inventory"][0]["inventory_id"] == "INV-001"
    assert snapshot.payload["Inventory"][0]["status"] == "MAPPED"
    assert snapshot.summary.row_counts == {"Inventory": 1}
    assert service.batch_update_requests == []
    assert service.create_requests == []


def test_multiple_tabs_and_unknown_tab_is_not_read() -> None:
    sheets = canonical_sheets()
    sheets["Unknown"] = [["secret"], ["keep"]]
    service = FakeGoogleSheetsService(sheets=sheets)

    snapshot = exporter(service).export(schema(), tabs=("Inventory", "Scenarios"))

    assert list(snapshot.payload["tabs"]) == ["Inventory", "Scenarios"]
    ranges = service.batch_get_requests[-1]["ranges"]
    assert not any("Unknown" in item for item in ranges)


def test_missing_tab_header_conflicts_and_unsafe_headers() -> None:
    missing = canonical_sheets()
    missing.pop("Inventory")
    with pytest.raises(SheetsSnapshotError) as missing_exc:
        exporter(FakeGoogleSheetsService(sheets=missing)).export(schema(), tabs=("Inventory",))

    inventory_tab = next(tab for tab in schema().tabs if tab.name == "Inventory")
    conflict_cases = [
        [["wrong", *list(inventory_tab.headers[1:])]],
        [[inventory_tab.headers[0], "", *list(inventory_tab.headers[2:])]],
        [[*list(inventory_tab.headers), "", "human_extra"]],
        [[*list(inventory_tab.headers), inventory_tab.headers[0]]],
        [["=formula", *list(inventory_tab.headers[1:])]],
    ]
    for rows in conflict_cases:
        with pytest.raises(SheetsSnapshotError) as exc_info:
            parse_tab_rows(
                inventory_tab,
                rows,
                max_rows_per_tab=10,
                redaction_counts=Counter(),
            )
        assert exc_info.value.code == "SHEETS_SNAPSHOT_HEADER_CONFLICT"

    assert missing_exc.value.code == "SHEETS_SNAPSHOT_TAB_MISSING"


def test_unknown_trailing_column_short_row_empty_row_and_redaction() -> None:
    inventory_tab = next(tab for tab in schema().tabs if tab.name == "Inventory")
    header = [*list(inventory_tab.headers), "human_extra"]
    short = inventory_row("INV-001", "DISCOVERY_GAP", "LOW")[:5]
    secret = inventory_row(
        "INV-002",
        "MAPPED",
        "HIGH",
        notes=f"Bearer {SECRET}",
    )

    result = parse_tab_rows(
        inventory_tab,
        [header, short, [], secret],
        max_rows_per_tab=10,
        redaction_counts=Counter(),
    )

    assert len(result.rows) == 2
    assert result.unknown_trailing_columns == 1
    assert result.rows[0]["source_path"] == ""
    assert result.rows[1]["notes"] == "<REDACTED:AUTHORIZATION>"
    assert_no_secret(result.rows)


def test_max_rows_exceeded_and_tab_name_quote_escaping() -> None:
    inventory_tab = next(tab for tab in schema().tabs if tab.name == "Inventory")
    with pytest.raises(SheetsSnapshotError) as exc_info:
        parse_tab_rows(
            inventory_tab,
            [
                list(inventory_tab.headers),
                inventory_row("1", "MAPPED", "LOW"),
                inventory_row("2", "MAPPED", "LOW"),
            ],
            max_rows_per_tab=1,
            redaction_counts=Counter(),
        )

    assert exc_info.value.code == "SHEETS_SNAPSHOT_ROW_LIMIT_EXCEEDED"
    assert bounded_a1_range("Bob's Tab", 2, 10).startswith("'Bob''s Tab'!A1:")


def test_custom_quote_tab_range_is_read_with_escaped_a1() -> None:
    custom_schema = CanonicalSheetsSchema(
        schema_version=1,
        tabs=(CanonicalTab("Bob's Tab", ("id", "status")),),
    )
    service = FakeGoogleSheetsService(sheets={"Bob's Tab": [["id", "status"], ["1", "MAPPED"]]})

    snapshot = exporter(service).export(custom_schema)

    assert snapshot.payload["tabs"]["Bob's Tab"] == [{"id": "1", "status": "MAPPED"}]
    assert service.batch_get_requests[-1]["ranges"][0].startswith("'Bob''s Tab'!A1:")


def test_google_read_failure_is_safe() -> None:
    service = FakeGoogleSheetsService(sheets=canonical_sheets())
    for _ in range(3):
        service.add_failure("values.batchGet", FakeGoogleApiError(500, SECRET))

    with pytest.raises(Exception) as exc_info:
        exporter(service).export(schema(), tabs=("Inventory",))

    assert_no_secret(exc_info.value)
