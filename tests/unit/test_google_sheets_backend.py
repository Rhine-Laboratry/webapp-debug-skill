from __future__ import annotations

import socket
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.fakes.google_sheets_service import (
    FakeGoogleApiError,
    FakeGoogleSheetsService,
    FakeGoogleTransportError,
)
from webapp_debug_skill.google_sheets_backend import GoogleRetryPolicy, GoogleSheetsBackend
from webapp_debug_skill.sheets_client import (
    AddHeaders,
    AppendRows,
    ClearMetadata,
    CreateTab,
    SetMetadata,
    SheetsBackendError,
    SheetsBatchInvalidError,
    UpdateRowValues,
)

SECRET_MARKER = "SECRET_MARKER_GOOGLE_BACKEND"
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----SECRET_MARKER_GOOGLE_BACKEND"


def backend(
    service: FakeGoogleSheetsService,
    *,
    now: datetime = datetime(2026, 6, 26, 0, 0, 0, tzinfo=UTC),
    sleeps: list[float] | None = None,
) -> GoogleSheetsBackend:
    return GoogleSheetsBackend(
        spreadsheet_id=service.spreadsheet_id,
        service=service,
        clock=lambda: now,
        retry_policy=GoogleRetryPolicy(max_attempts=3, initial_delay=1.0, max_delay=4.0),
        sleeper=(sleeps.append if sleeps is not None else (lambda _delay: None)),
        jitter=lambda attempt: attempt / 10,
    )


def metadata_rows(*rows: Sequence[str]) -> list[list[str]]:
    return [["key", "value", "updated_at", "notes"], *[list(row) for row in rows]]


def service_with(
    *,
    metadata: Sequence[Sequence[str]] | None = None,
    features: Sequence[Sequence[str]] | None = None,
    extra: dict[str, Sequence[Sequence[str]]] | None = None,
) -> FakeGoogleSheetsService:
    sheets: dict[str, Sequence[Sequence[str]]] = {
        "Metadata": metadata if metadata is not None else metadata_rows(),
        "Features": features if features is not None else [["Feature ID", "Title"]],
    }
    if extra:
        sheets.update(extra)
    return FakeGoogleSheetsService(sheets=sheets)


def assert_no_secret(*values: object) -> None:
    for value in values:
        rendered = str(value)
        assert SECRET_MARKER not in rendered
        assert PRIVATE_KEY_MARKER not in rendered


def latest_batch_body(service: FakeGoogleSheetsService) -> dict[str, Any]:
    return service.batch_update_requests[-1]["body"]


def user_values(request: dict[str, Any]) -> list[str]:
    values = request["updateCells"]["rows"][0]["values"]
    return [cell["userEnteredValue"]["stringValue"] for cell in values]


def test_read_tab_headers_metadata_unknowns_and_internal_empty_header() -> None:
    service = service_with(
        metadata=metadata_rows(
            ["known", "value", "old", "note"],
            ["empty", "", "old", "empty-value-note"],
            ["unknown", "keep", "old", "human-note"],
        ),
        features=[["Feature ID", "", "Title"]],
        extra={"Unknown": [["U1"]]},
    )

    state = backend(service).read_spreadsheet()
    mutated = state.tabs_dict()
    mutated["Features"].append("mutated")

    assert state.metadata_dict() == {"empty": "", "known": "value", "unknown": "keep"}
    assert state.tabs_dict()["Features"] == ["Feature ID", "", "Title"]
    assert state.tabs_dict()["Unknown"] == ["U1"]
    assert "human-note" not in state.metadata_dict().values()
    assert backend(service).read_spreadsheet().tabs_dict()["Features"] == [
        "Feature ID",
        "",
        "Title",
    ]


def test_read_metadata_selected_keys_and_a1_quote_for_single_quote_tab() -> None:
    service = service_with(extra={"Bob's Tab": [["H"]]})

    result = backend(service).read_metadata(["missing"])

    assert result == {}
    ranges = service.batch_get_requests[-1]["ranges"]
    assert "'Bob''s Tab'!1:1" in ranges
    assert "'Metadata'!A:D" in ranges


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        (None, "SHEETS_LOCK_STORAGE_UNAVAILABLE"),
        ([["wrong", "value", "updated_at", "notes"]], "SHEETS_METADATA_SCHEMA_INVALID"),
        (
            metadata_rows(["dup", "a", "", ""], ["dup", "b", "", ""]),
            "SHEETS_METADATA_DUPLICATE_KEY",
        ),
        (metadata_rows(["", "value", "", "note"]), "SHEETS_METADATA_ROW_INVALID"),
        (metadata_rows(["=key", "value", "", ""]), "SHEETS_METADATA_FORMULA_REJECTED"),
        (metadata_rows(["key", "=value", "", ""]), "SHEETS_METADATA_FORMULA_REJECTED"),
    ],
)
def test_metadata_schema_rejections(
    metadata: Sequence[Sequence[str]] | None,
    code: str,
) -> None:
    if metadata is None:
        service = FakeGoogleSheetsService(sheets={"Features": [["Feature ID"]]})
    else:
        service = service_with(metadata=metadata)

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).read_spreadsheet()

    assert exc_info.value.code == code
    assert exc_info.value.exit_code == 3


def test_malformed_google_response_is_safe() -> None:
    service = service_with()
    service.malformed_values_response = True

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).read_spreadsheet()

    assert exc_info.value.code == "SHEETS_RESPONSE_INVALID"
    assert exc_info.value.exit_code == 2


def test_create_tab_and_add_headers_are_one_batch_update_in_order() -> None:
    service = service_with()

    result = backend(service).apply_batch(
        [CreateTab("Scenarios"), AddHeaders("Scenarios", ("Scenario ID", "Title"))]
    )

    body = latest_batch_body(service)
    assert len(service.batch_update_requests) == 1
    assert [next(iter(request)) for request in body["requests"]] == ["addSheet", "updateCells"]
    assert result.applied_mutations == 2
    assert result.spreadsheet_state.tabs_dict()["Scenarios"] == ["Scenario ID", "Title"]
    assert service.execute_num_retries[-1] == 0


def test_add_headers_uses_string_value_not_formula_value() -> None:
    service = service_with()

    backend(service).apply_batch([AddHeaders("Features", ("Status",))])

    request = latest_batch_body(service)["requests"][0]
    cell = request["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]
    assert cell == {"stringValue": "Status"}
    assert "formulaValue" not in cell
    assert service.snapshot()["Features"][0] == ["Feature ID", "Title", "Status"]


def test_metadata_set_clear_preserve_notes_unknowns_and_timestamp() -> None:
    service = service_with(
        metadata=metadata_rows(
            ["known", "old", "old-time", "keep-note"],
            ["unknown", "keep", "old-time", "unknown-note"],
        )
    )

    result = backend(service).apply_batch(
        [SetMetadata.from_mapping({"known": "new", "added": "value"}), ClearMetadata(("known",))]
    )

    rows = service.snapshot()["Metadata"]
    assert rows[1] == ["known", "", "2026-06-26T00:00:00Z", "keep-note"]
    assert rows[2] == ["unknown", "keep", "old-time", "unknown-note"]
    assert rows[3] == ["added", "value", "2026-06-26T00:00:00Z", ""]
    assert result.spreadsheet_state.metadata_dict() == {
        "added": "value",
        "known": "",
        "unknown": "keep",
    }
    assert len(service.batch_update_requests) == 1
    assert [user_values(request) for request in latest_batch_body(service)["requests"]] == [
        ["new", "2026-06-26T00:00:00Z"],
        ["added", "value", "2026-06-26T00:00:00Z", ""],
        ["", "2026-06-26T00:00:00Z"],
    ]


def test_domain_mutation_order_and_unknown_tab_preserved() -> None:
    service = service_with(extra={"Unknown": [["U"]]})

    backend(service).apply_batch(
        [
            SetMetadata.from_mapping({"first": "1"}),
            AddHeaders("Features", ("Status",)),
            SetMetadata.from_mapping({"second": "2"}),
        ]
    )

    body = latest_batch_body(service)
    assert [next(iter(request)) for request in body["requests"]] == [
        "updateCells",
        "updateCells",
        "updateCells",
    ]
    assert service.snapshot()["Unknown"] == [["U"]]


def test_inventory_row_append_update_and_readback_use_string_values() -> None:
    service = service_with(
        extra={
            "Inventory": [
                ["inventory_id", "risk", "discovery_status", "notes"],
                ["INV-001", "LOW", "DISCOVERED", "keep"],
            ]
        }
    )

    result = backend(service).apply_batch(
        [
            AppendRows(
                "Inventory",
                rows=((("inventory_id", "INV-002"), ("risk", "HIGH")),),
            ),
            UpdateRowValues(
                "Inventory",
                row_index=0,
                values=(("risk", "MEDIUM"),),
                expected_values=(("inventory_id", "INV-001"), ("risk", "LOW")),
            ),
        ]
    )

    rows = result.spreadsheet_state.rows_dict()["Inventory"]
    requests = latest_batch_body(service)["requests"]
    assert rows[0]["risk"] == "MEDIUM"
    assert rows[0]["notes"] == "keep"
    assert rows[1]["inventory_id"] == "INV-002"
    assert rows[1]["risk"] == "HIGH"
    assert all("formulaValue" not in str(request) for request in requests)
    assert all("stringValue" in str(request) for request in requests)


def test_inventory_row_expected_mismatch_writes_zero() -> None:
    service = service_with(
        extra={
            "Inventory": [
                ["inventory_id", "risk"],
                ["INV-001", "LOW"],
            ]
        }
    )

    with pytest.raises(SheetsBatchInvalidError) as exc_info:
        backend(service).apply_batch(
            [
                UpdateRowValues(
                    "Inventory",
                    row_index=0,
                    values=(("risk", "HIGH"),),
                    expected_values=(("risk", "MEDIUM"),),
                )
            ]
        )

    assert exc_info.value.reason == "EXPECTED_VALUE_MISMATCH"
    assert service.batch_update_requests == []
    assert service.snapshot()["Inventory"][1] == ["INV-001", "LOW"]


def test_inventory_multiple_appends_in_one_batch_use_distinct_rows() -> None:
    service = service_with(extra={"Inventory": [["inventory_id", "risk"]]})

    backend(service).apply_batch(
        [
            AppendRows("Inventory", rows=((("inventory_id", "INV-001"), ("risk", "LOW")),)),
            AppendRows("Inventory", rows=((("inventory_id", "INV-002"), ("risk", "HIGH")),)),
        ]
    )

    assert service.snapshot()["Inventory"] == [
        ["inventory_id", "risk"],
        ["INV-001", "LOW"],
        ["INV-002", "HIGH"],
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        SetMetadata.from_mapping({"safe": SECRET_MARKER}),
        AddHeaders("Missing", ("Header",)),
    ],
)
def test_invalid_mutation_or_missing_target_writes_zero(mutation: object) -> None:
    service = service_with()

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).apply_batch([mutation])  # type: ignore[list-item]

    assert service.batch_update_requests == []
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_duplicate_metadata_state_writes_zero() -> None:
    service = service_with(metadata=metadata_rows(["dup", "a", "", ""], ["dup", "b", "", ""]))

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).apply_batch([SetMetadata.from_mapping({"new": "value"})])

    assert exc_info.value.code == "SHEETS_METADATA_DUPLICATE_KEY"
    assert service.batch_update_requests == []


def test_naive_datetime_rejected_before_write() -> None:
    service = service_with()

    with pytest.raises(SheetsBatchInvalidError) as exc_info:
        backend(service, now=datetime(2026, 6, 26, 0, 0, 0)).apply_batch(
            [SetMetadata.from_mapping({"key": "value"})]
        )

    assert exc_info.value.reason == "NAIVE_DATETIME"
    assert service.batch_update_requests == []


def test_batch_success_readback_failure_is_ambiguous() -> None:
    service = service_with()

    def fail_next_read(fake: FakeGoogleSheetsService) -> None:
        for _ in range(3):
            fake.add_failure("get", FakeGoogleApiError(503, SECRET_MARKER))

    service.after_batch_apply_hook = fail_next_read

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).apply_batch([AddHeaders("Features", ("Status",))])

    assert exc_info.value.code == "SHEETS_WRITE_OUTCOME_UNKNOWN"
    assert exc_info.value.reason == "UNKNOWN_WRITE_RESULT"
    assert exc_info.value.may_have_applied is True
    assert service.snapshot()["Features"][0] == ["Feature ID", "Title", "Status"]


def test_batch_update_failure_after_apply_is_ambiguous_without_resend() -> None:
    service = service_with()
    service.fail_batch_after_apply = FakeGoogleTransportError(SECRET_MARKER)

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).apply_batch([AddHeaders("Features", ("Status",))])

    assert exc_info.value.code == "SHEETS_WRITE_OUTCOME_UNKNOWN"
    assert exc_info.value.may_have_applied is True
    assert service.snapshot()["Features"][0] == ["Feature ID", "Title", "Status"]
    assert len(service.batch_update_requests) == 1
    assert_no_secret(exc_info.value, exc_info.value.reason)


def test_secret_markers_from_cells_do_not_enter_state_or_batch_result() -> None:
    header_secret = service_with(features=[["Feature ID", SECRET_MARKER]])
    with pytest.raises(SheetsBackendError) as header_exc:
        backend(header_secret).read_spreadsheet()
    assert header_exc.value.code == "SHEETS_RESPONSE_INVALID"
    assert_no_secret(header_exc.value, header_exc.value.path, header_exc.value.reason)

    metadata_secret = service_with(metadata=metadata_rows(["safe", PRIVATE_KEY_MARKER, "", ""]))
    with pytest.raises(SheetsBackendError) as metadata_exc:
        backend(metadata_secret).read_spreadsheet()
    assert metadata_exc.value.code == "SHEETS_RESPONSE_INVALID"
    assert_no_secret(metadata_exc.value, metadata_exc.value.path, metadata_exc.value.reason)


@pytest.mark.parametrize("status", [429, 500])
def test_read_retryable_http_status_eventually_succeeds(status: int) -> None:
    service = service_with()
    service.add_failure("get", FakeGoogleApiError(status, SECRET_MARKER))
    sleeps: list[float] = []

    state = backend(service, sleeps=sleeps).read_spreadsheet()

    assert state.tabs_dict()["Features"] == ["Feature ID", "Title"]
    assert sleeps == [1.1]
    assert service.execute_num_retries == [0, 0, 0]


def test_read_transport_error_retries_then_succeeds() -> None:
    service = service_with()
    service.add_failure("values.batchGet", FakeGoogleTransportError(SECRET_MARKER))
    sleeps: list[float] = []

    state = backend(service, sleeps=sleeps).read_spreadsheet()

    assert state.metadata_dict() == {}
    assert sleeps == [1.1]


def test_read_retry_limit_and_non_retryable_statuses() -> None:
    retry_service = service_with()
    for _ in range(3):
        retry_service.add_failure("get", FakeGoogleApiError(500, SECRET_MARKER))
    with pytest.raises(SheetsBackendError) as retry_exc:
        backend(retry_service).read_spreadsheet()
    assert retry_exc.value.code == "SHEETS_SERVICE_UNAVAILABLE"

    for status, code in [
        (400, "SHEETS_API_BAD_REQUEST"),
        (401, "SHEETS_AUTH_FAILED"),
        (403, "SHEETS_PERMISSION_DENIED"),
        (404, "SHEETS_NOT_FOUND"),
    ]:
        service = service_with()
        service.add_failure("get", FakeGoogleApiError(status, SECRET_MARKER))
        with pytest.raises(SheetsBackendError) as exc_info:
            backend(service).read_spreadsheet()
        assert exc_info.value.code == code
        assert service.execute_count == 1
        assert_no_secret(exc_info.value, exc_info.value.reason)


@pytest.mark.parametrize(
    ("operation", "error"),
    [
        ("batchUpdate", FakeGoogleApiError(429, SECRET_MARKER)),
        ("batchUpdate", FakeGoogleApiError(500, SECRET_MARKER)),
        ("batchUpdate", FakeGoogleTransportError(SECRET_MARKER)),
    ],
)
def test_batch_update_is_not_retried(operation: str, error: BaseException) -> None:
    service = service_with()
    service.add_failure(operation, error)

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).apply_batch([AddHeaders("Features", ("Status",))])

    assert service.execute_num_retries.count(0) == service.execute_count
    assert len(service.batch_update_requests) == 1
    assert_no_secret(exc_info.value, exc_info.value.reason)


def test_create_spreadsheet_success_and_no_metadata_bootstrap() -> None:
    service = service_with()

    state = backend(service).create_spreadsheet("New spreadsheet")

    assert state.spreadsheet_id == "created-1"
    assert service.create_requests == [{"body": {"properties": {"title": "New spreadsheet"}}}]
    assert service.batch_update_requests == []
    assert "Metadata" not in service.snapshot()
    assert service.execute_num_retries == [0]


def test_create_rejections_and_missing_id() -> None:
    service = service_with()
    with pytest.raises(SheetsBatchInvalidError):
        backend(service).create_spreadsheet("")
    assert service.create_requests == []

    malformed = service_with()
    malformed.malformed_create_response = True
    with pytest.raises(SheetsBackendError) as exc_info:
        backend(malformed).create_spreadsheet("Title")
    assert exc_info.value.code == "SHEETS_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FakeGoogleTransportError(SECRET_MARKER), "SHEETS_CREATE_OUTCOME_UNKNOWN"),
        (FakeGoogleApiError(500, SECRET_MARKER), "SHEETS_CREATE_OUTCOME_UNKNOWN"),
        (FakeGoogleApiError(429, SECRET_MARKER), "SHEETS_RATE_LIMITED"),
        (FakeGoogleApiError(403, SECRET_MARKER), "SHEETS_PERMISSION_DENIED"),
    ],
)
def test_create_failures_are_not_retried(error: BaseException, code: str) -> None:
    service = service_with()
    service.add_failure("create", error)

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).create_spreadsheet("Title")

    assert exc_info.value.code == code
    assert service.execute_count == 1
    assert service.batch_update_requests == []
    assert_no_secret(exc_info.value, exc_info.value.reason)


def test_create_failure_after_apply_is_outcome_unknown_without_retry() -> None:
    service = service_with()
    service.fail_create_after_apply = FakeGoogleTransportError(SECRET_MARKER)

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).create_spreadsheet("Title")

    assert exc_info.value.code == "SHEETS_CREATE_OUTCOME_UNKNOWN"
    assert exc_info.value.may_have_applied is True
    assert service.create_count == 1
    assert service.execute_count == 1
    assert service.batch_update_requests == []
    assert_no_secret(exc_info.value, exc_info.value.reason)


def test_raw_markers_do_not_escape_errors_or_safe_results() -> None:
    service = service_with()
    service.add_failure("get", FakeGoogleApiError(403, PRIVATE_KEY_MARKER))

    with pytest.raises(SheetsBackendError) as exc_info:
        backend(service).read_spreadsheet()

    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_fake_service_backend_does_not_use_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    service = service_with()

    state = backend(service).read_spreadsheet()
    result = backend(service).apply_batch([AddHeaders("Features", ("Status",))])

    assert state.tabs_dict()["Features"] == ["Feature ID", "Title"]
    assert result.spreadsheet_state.tabs_dict()["Features"] == [
        "Feature ID",
        "Title",
        "Status",
    ]
