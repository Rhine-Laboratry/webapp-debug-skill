from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fakes.google_sheets_service import (
    FakeGoogleApiError,
    FakeGoogleSheetsService,
    FakeGoogleTransportError,
)
from webapp_debug_skill.google_sheets_backend import METADATA_HEADERS, GoogleSheetsBackend
from webapp_debug_skill.sheets_bootstrap import (
    BOOTSTRAP_OPERATION,
    BootstrapOutcome,
    SheetsBootstrapError,
    SheetsBootstrapper,
)
from webapp_debug_skill.wal import AppendOnlyWal, WalError

SECRET = "SECRET_MARKER_BOOTSTRAP_BODY"


def missing_metadata_service() -> FakeGoogleSheetsService:
    return FakeGoogleSheetsService(sheets={"Features": [["Feature ID", "Title"], ["data", "keep"]]})


def backend(service: FakeGoogleSheetsService) -> GoogleSheetsBackend:
    return GoogleSheetsBackend(spreadsheet_id=service.spreadsheet_id, service=service)


def wal(tmp_path: Path, events: list[str] | None = None) -> AppendOnlyWal:
    def fsync(_fd: int) -> None:
        if events is not None:
            events.append("wal_fsync")

    return AppendOnlyWal(
        tmp_path / "wal.jsonl",
        "run-1",
        clock=lambda: "2026-07-01T00:00:00Z",
        fsync_func=fsync,
    )


def bootstrapper(
    service: FakeGoogleSheetsService,
    tmp_path: Path,
    events: list[str] | None = None,
) -> SheetsBootstrapper:
    return SheetsBootstrapper(
        backend=backend(service),
        wal=wal(tmp_path, events),
        operation_id_factory=lambda: "op-bootstrap",
    )


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_bootstrap_create_tab_and_headers_one_atomic_batch(tmp_path: Path) -> None:
    service = missing_metadata_service()

    result = bootstrapper(service, tmp_path).bootstrap()

    assert result.outcome == BootstrapOutcome.BOOTSTRAPPED
    assert result.bootstrapped is True
    assert result.wal_acknowledged is True
    assert len(service.batch_update_requests) == 1
    requests = service.batch_update_requests[0]["body"]["requests"]
    assert [next(iter(request)) for request in requests] == ["addSheet", "updateCells"]
    assert service.snapshot()["Metadata"][0] == list(METADATA_HEADERS)
    cell = requests[1]["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]
    assert cell == {"stringValue": "key"}
    assert "formulaValue" not in cell


def test_bootstrap_wal_pending_fsync_before_batch_and_ack_after_readback(tmp_path: Path) -> None:
    service = missing_metadata_service()
    events: list[str] = []
    original = service.apply_batch_update

    def record_batch_update(body: Any) -> Any:
        events.append("batch")
        return original(body)

    service.apply_batch_update = record_batch_update  # type: ignore[method-assign]

    result = bootstrapper(service, tmp_path, events).bootstrap()

    entries = wal(tmp_path).read_entries()
    assert events.index("wal_fsync") < events.index("batch")
    assert entries[0].operation == BOOTSTRAP_OPERATION
    assert entries[0].status == "pending"
    assert entries[1].status == "acknowledged"
    assert result.read_back_verified is True


class FailingWal:
    def append_pending(self, *_args: object, **_kwargs: object) -> None:
        raise WalError("WAL_WRITE_FAILED", "wal", "WRITE_FAILED")


def test_pending_wal_failure_prevents_google_write(tmp_path: Path) -> None:
    service = missing_metadata_service()
    bootstrap = SheetsBootstrapper(
        backend=backend(service),
        wal=FailingWal(),  # type: ignore[arg-type]
        operation_id_factory=lambda: "op",
    )

    with pytest.raises(SheetsBootstrapError) as exc_info:
        bootstrap.bootstrap()

    assert exc_info.value.code == "SHEETS_INIT_WAL_FAILED"
    assert service.batch_update_requests == []


def test_batch_apply_failure_before_apply_keeps_pending(tmp_path: Path) -> None:
    service = missing_metadata_service()
    service.add_failure("batchUpdate", FakeGoogleApiError(400, SECRET))

    with pytest.raises(SheetsBootstrapError) as exc_info:
        bootstrapper(service, tmp_path).bootstrap()

    assert exc_info.value.code == "SHEETS_API_BAD_REQUEST"
    assert "Metadata" not in service.snapshot()
    assert len(wal(tmp_path).read_entries()) == 1
    assert_no_secret(exc_info.value, exc_info.value.reason)


def test_ambiguous_write_success_acks_without_resend(tmp_path: Path) -> None:
    service = missing_metadata_service()
    service.fail_batch_after_apply = FakeGoogleTransportError(SECRET)

    result = bootstrapper(service, tmp_path).bootstrap()

    assert result.outcome == BootstrapOutcome.ALREADY_APPLIED
    assert result.wal_acknowledged is True
    assert len(service.batch_update_requests) == 1


def test_ambiguous_write_missing_metadata_keeps_pending(tmp_path: Path) -> None:
    service = missing_metadata_service()
    service.fail_batch_after_apply = FakeGoogleTransportError(SECRET)

    def remove_metadata(fake: FakeGoogleSheetsService) -> None:
        fake.sheet_ids.pop("Metadata", None)
        fake.rows.pop("Metadata", None)

    service.after_batch_apply_hook = remove_metadata

    result = bootstrapper(service, tmp_path).bootstrap()

    assert result.outcome == BootstrapOutcome.OUTCOME_UNKNOWN
    assert result.wal_acknowledged is False
    assert len(wal(tmp_path).read_entries()) == 1


def test_ambiguous_write_partial_header_is_conflict(tmp_path: Path) -> None:
    service = missing_metadata_service()
    service.fail_batch_after_apply = FakeGoogleTransportError(SECRET)

    def partial_metadata(fake: FakeGoogleSheetsService) -> None:
        fake.rows["Metadata"] = [["key", "value"]]

    service.after_batch_apply_hook = partial_metadata

    with pytest.raises(SheetsBootstrapError) as exc_info:
        bootstrapper(service, tmp_path).bootstrap()

    assert exc_info.value.code == "SHEETS_BOOTSTRAP_CONFLICT"
    assert len(wal(tmp_path).read_entries()) == 1


def test_second_bootstrap_noops_and_preserves_other_data(tmp_path: Path) -> None:
    service = missing_metadata_service()
    bootstrapper(service, tmp_path).bootstrap()
    writes = len(service.batch_update_requests)

    result = bootstrapper(service, tmp_path).bootstrap()

    assert result.outcome == BootstrapOutcome.NOOP
    assert len(service.batch_update_requests) == writes
    assert service.snapshot()["Features"][1] == ["data", "keep"]


def test_case_collision_and_invalid_header_are_blocked(tmp_path: Path) -> None:
    lower = FakeGoogleSheetsService(sheets={"metadata": [["key", "value", "updated_at", "notes"]]})
    invalid = FakeGoogleSheetsService(
        sheets={"Metadata": [["KEY", "value", "updated_at", "notes"]]}
    )

    with pytest.raises(SheetsBootstrapError) as lower_exc:
        bootstrapper(lower, tmp_path).bootstrap()
    with pytest.raises(SheetsBootstrapError) as invalid_exc:
        bootstrapper(invalid, tmp_path).bootstrap()

    assert lower_exc.value.code == "SHEETS_BOOTSTRAP_CONFLICT"
    assert invalid_exc.value.code == "SHEETS_METADATA_SCHEMA_INVALID"
