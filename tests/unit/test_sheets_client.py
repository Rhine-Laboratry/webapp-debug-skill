from __future__ import annotations

import json
import socket

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
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


SECRET = "SECRET_MARKER_DO_NOT_LEAK"


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_atomic_batch_apply_and_order() -> None:
    backend = FakeSheetsBackend(metadata={"unknown": "keep"}, tabs={"Known": ["old"]})

    result = backend.apply_batch(
        [
            CreateTab("New", headers=("a",)),
            AddHeaders("New", headers=("b", "c")),
            SetMetadata.from_mapping({"first": "1"}),
            ClearMetadata(keys=("unknown",)),
            SetMetadata.from_mapping({"second": "2"}),
        ]
    )

    state = result.spreadsheet_state
    assert state.tabs_dict()["New"] == ["a", "b", "c"]
    assert state.metadata_dict() == {"first": "1", "second": "2"}
    assert backend.write_count == 1
    assert backend.call_log == [("apply_batch", 5)]


def test_invalid_mutation_leaves_batch_unapplied() -> None:
    backend = FakeSheetsBackend(metadata={"keep": "yes"}, tabs={"Known": ["old"]})

    with pytest.raises(SheetsBatchInvalidError) as exc_info:
        backend.apply_batch(
            [
                SetMetadata.from_mapping({"new": "value"}),
                AddHeaders("Missing", headers=("bad",)),
            ]
        )

    assert exc_info.value.code == "SHEETS_BATCH_INVALID"
    assert backend.read_spreadsheet().metadata_dict() == {"keep": "yes"}
    assert backend.read_spreadsheet().tabs_dict() == {"Known": ["old"]}
    assert backend.write_count == 0


def test_fail_before_apply_does_not_change_state() -> None:
    backend = FakeSheetsBackend(metadata={"keep": "yes"})
    backend.fail_before_apply = True

    with pytest.raises(SheetsBackendError) as exc_info:
        backend.apply_batch([SetMetadata.from_mapping({"new": "value"})])

    assert exc_info.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert backend.read_spreadsheet().metadata_dict() == {"keep": "yes"}
    assert backend.write_count == 0


def test_fail_after_apply_changes_state_but_reports_failure() -> None:
    backend = FakeSheetsBackend(metadata={"keep": "yes"})
    backend.fail_after_apply = True

    with pytest.raises(SheetsBackendError) as exc_info:
        backend.apply_batch([SetMetadata.from_mapping({"new": "value"})])

    assert exc_info.value.code == "SHEETS_BACKEND_IO_FAILED"
    assert backend.read_spreadsheet().metadata_dict() == {"keep": "yes", "new": "value"}
    assert backend.write_count == 1


def test_read_has_no_side_effect_and_returned_state_is_defensive_copy() -> None:
    backend = FakeSheetsBackend(metadata={"key": "value"}, tabs={"Tab": ["a"]})

    state = backend.read_spreadsheet()
    mutated_metadata = state.metadata_dict()
    mutated_tabs = state.tabs_dict()
    mutated_metadata["key"] = "changed"
    mutated_tabs["Tab"].append("b")

    assert backend.read_spreadsheet().metadata_dict() == {"key": "value"}
    assert backend.read_spreadsheet().tabs_dict() == {"Tab": ["a"]}
    assert backend.read_count == 3
    assert backend.write_count == 0


def test_unknown_tab_column_and_metadata_are_preserved() -> None:
    backend = FakeSheetsBackend(metadata={"unknown_metadata": "keep"}, tabs={"Unknown": ["x"]})

    backend.apply_batch(
        [
            CreateTab("Known", headers=("a",)),
            AddHeaders("Known", headers=("b",)),
            SetMetadata.from_mapping({"known_metadata": "set"}),
        ]
    )

    state = backend.read_spreadsheet()
    assert state.metadata_dict()["unknown_metadata"] == "keep"
    assert state.tabs_dict()["Unknown"] == ["x"]
    assert state.tabs_dict()["Known"] == ["a", "b"]


def test_after_apply_hook_models_conflicting_change_before_readback() -> None:
    backend = FakeSheetsBackend()

    def conflict(fake: FakeSheetsBackend) -> None:
        fake.set_metadata_direct({"writer_lock_owner": "other"})

    backend.after_apply_hook = conflict
    backend.apply_batch([SetMetadata.from_mapping({"writer_lock_owner": "mine"})])

    assert backend.read_metadata(["writer_lock_owner"]) == {"writer_lock_owner": "other"}
    assert backend.write_count == 1


def test_batch_validation_rejects_secret_without_leaking_payload() -> None:
    backend = FakeSheetsBackend()

    with pytest.raises(SheetsBatchInvalidError) as exc_info:
        backend.apply_batch([SetMetadata.from_mapping({"safe": SECRET})])

    assert exc_info.value.code == "SHEETS_BATCH_INVALID"
    assert_no_secret(exc_info.value, exc_info.value.path, exc_info.value.reason)


def test_batch_validation_rejects_formula_like_values() -> None:
    backend = FakeSheetsBackend()

    with pytest.raises(SheetsBatchInvalidError) as exc_info:
        backend.apply_batch([SetMetadata.from_mapping({"safe": "=formula"})])

    assert exc_info.value.reason == "FORMULA_LIKE_VALUE"


def test_call_log_and_write_count_are_deterministic() -> None:
    backend = FakeSheetsBackend()

    backend.read_spreadsheet()
    backend.apply_batch([CreateTab("A")])
    backend.read_metadata(["missing"])

    assert backend.write_count == 1
    assert backend.call_log == [
        ("read_spreadsheet", 1),
        ("apply_batch", 1),
        ("read_metadata", "missing"),
    ]


def test_fake_backend_does_not_use_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    backend = FakeSheetsBackend()

    backend.read_spreadsheet()
    backend.apply_batch([SetMetadata.from_mapping({"key": "value"})])
    backend.read_metadata(["key"])

    assert backend.read_metadata(["key"]) == {"key": "value"}


def test_create_spreadsheet_fake_only_contract() -> None:
    backend = FakeSheetsBackend(metadata={"old": "gone"}, tabs={"Old": ["x"]})

    state = backend.create_spreadsheet("title")

    assert state.spreadsheet_id == "fake-1"
    assert state.metadata_dict() == {}
    assert state.tabs_dict() == {}
    assert backend.create_count == 1
    assert json.dumps(state.metadata_dict()) == "{}"


def test_row_append_update_readback_and_unknown_column_preservation() -> None:
    backend = FakeSheetsBackend(
        tabs={"Inventory": ["inventory_id", "risk", "discovery_status", "notes"]},
        rows={
            "Inventory": [
                {
                    "inventory_id": "INV-001",
                    "risk": "LOW",
                    "discovery_status": "DISCOVERED",
                    "notes": "keep",
                    "human_extra": "preserve",
                }
            ]
        },
    )

    backend.apply_batch(
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

    rows = backend.read_spreadsheet().rows_dict()["Inventory"]
    assert rows[0]["risk"] == "MEDIUM"
    assert rows[0]["notes"] == "keep"
    assert rows[0]["human_extra"] == "preserve"
    assert rows[1]["inventory_id"] == "INV-002"
    assert backend.write_count == 1


def test_row_mutation_expected_mismatch_or_unknown_column_is_atomic() -> None:
    backend = FakeSheetsBackend(
        tabs={"Inventory": ["inventory_id", "risk"]},
        rows={"Inventory": [{"inventory_id": "INV-001", "risk": "LOW"}]},
    )

    with pytest.raises(SheetsBatchInvalidError) as mismatch_exc:
        backend.apply_batch(
            [
                UpdateRowValues(
                    "Inventory",
                    row_index=0,
                    values=(("risk", "HIGH"),),
                    expected_values=(("risk", "MEDIUM"),),
                )
            ]
        )
    with pytest.raises(SheetsBatchInvalidError) as unknown_exc:
        backend.apply_batch(
            [
                AppendRows(
                    "Inventory",
                    rows=((("inventory_id", "INV-002"), ("unknown", "bad")),),
                )
            ]
        )

    assert mismatch_exc.value.reason == "EXPECTED_VALUE_MISMATCH"
    assert unknown_exc.value.reason == "UNKNOWN_COLUMN"
    assert backend.read_spreadsheet().rows_dict()["Inventory"] == [
        {"inventory_id": "INV-001", "risk": "LOW"}
    ]
    assert backend.write_count == 0


def test_row_mutation_ambiguous_failure_keeps_applied_state() -> None:
    backend = FakeSheetsBackend(
        tabs={"Inventory": ["inventory_id", "risk"]},
        rows={"Inventory": [{"inventory_id": "INV-001", "risk": "LOW"}]},
    )
    backend.fail_after_apply = True

    with pytest.raises(SheetsBackendError) as exc_info:
        backend.apply_batch(
            [
                UpdateRowValues(
                    "Inventory",
                    row_index=0,
                    values=(("risk", "HIGH"),),
                    expected_values=(("risk", "LOW"),),
                )
            ]
        )

    assert exc_info.value.may_have_applied is True
    assert backend.read_spreadsheet().rows_dict()["Inventory"][0]["risk"] == "HIGH"
    assert backend.write_count == 1
