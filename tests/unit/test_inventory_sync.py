from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
from webapp_debug_skill.inventory_sync import (
    InventorySyncError,
    apply_inventory_sync_plan,
    build_sync_plan,
    inventory_headers,
    render_cell_value,
)
from webapp_debug_skill.wal import AppendOnlyWal

FIXTURES = Path("tests/fixtures/inventory_sync")
SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SYNC"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def headers() -> tuple[str, ...]:
    return inventory_headers(SCHEMA)[0]


def plan(
    discovery: dict[str, object], snapshot: dict[str, object], **kwargs: object
) -> dict[str, object]:
    return build_sync_plan(
        discovery,
        snapshot,
        headers=headers(),
        schema_version=1,
        generated_at="2026-07-02T00:00:00Z",
        **kwargs,
    )


def test_empty_snapshot_appends_and_operation_is_canonical() -> None:
    result = plan(load("discovery_new.json"), load("snapshot_empty.json"))
    operation = result["operations"][0]

    assert result["summary"]["append_count"] == 1
    assert operation["operation"] == "APPEND_INVENTORY"
    assert operation["operation_id"].startswith("SYNC-")
    assert "feature_name" not in operation["row"]
    assert "notes" in operation["row"]
    assert operation["row_coordinate"] == {
        "tab": "Inventory",
        "mode": "append",
        "expected_after_data_rows": 0,
    }
    assert operation["expected_snapshot_fingerprint"] == result["source"]["snapshot_fingerprint"]


def test_existing_exact_match_noop_and_update_preserves_human_columns() -> None:
    exact = plan(load("discovery_new.json"), load("snapshot_existing.json"))
    changed_discovery = load("discovery_new.json")
    changed_discovery["Inventory"][0]["risk"] = "HIGH"  # type: ignore[index]
    changed_discovery["Inventory"][0]["notes"] = "auto note"  # type: ignore[index]
    updated = plan(changed_discovery, load("snapshot_existing.json"))

    assert exact["summary"]["noop_count"] == 1
    assert exact["summary"]["operation_count"] == 0
    assert updated["operations"][0]["operation"] == "UPDATE_INVENTORY_FIELDS"
    assert updated["operations"][0]["fields"]["risk"] == "HIGH"
    assert updated["operations"][0]["row_coordinate"]["row_number"] == 2
    assert updated["operations"][0]["expected_old_values"]["risk"] == "MEDIUM"
    assert "notes" not in updated["operations"][0]["fields"]


def test_status_transitions_do_not_revive_closed_or_manual_rows() -> None:
    discovery = load("discovery_new.json")
    mapped = load("snapshot_existing.json")
    mapped["tabs"]["Inventory"][0]["discovery_status"] = "MAPPED"  # type: ignore[index]
    excluded = load("snapshot_existing.json")
    excluded["tabs"]["Inventory"][0]["discovery_status"] = "EXCLUDED_WITH_REASON"  # type: ignore[index]
    retired = load("snapshot_existing.json")
    retired["tabs"]["Inventory"][0]["discovery_status"] = "RETIRED"  # type: ignore[index]
    blocked = load("snapshot_existing.json")
    blocked["tabs"]["Inventory"][0]["discovery_status"] = "BLOCKED"  # type: ignore[index]
    manual = load("snapshot_manual_override.json")

    assert plan(discovery, mapped)["summary"]["noop_count"] == 1
    assert plan(discovery, excluded)["summary"]["noop_count"] == 1
    assert plan(discovery, retired)["summary"]["noop_count"] == 1
    assert plan(discovery, blocked)["operations"][0]["fields"]["discovery_status"] == "DISCOVERED"
    assert plan(discovery, manual)["summary"]["conflict_count"] == 1


def test_discovery_gaps_duplicates_invalid_values_and_secret_redaction() -> None:
    gap_plan = plan(load("discovery_with_gaps.json"), load("snapshot_empty.json"))
    conflict_plan = plan(load("discovery_conflict.json"), load("snapshot_empty.json"))
    bad_snapshot = load("snapshot_existing.json")
    bad_snapshot["tabs"]["Inventory"][0]["risk"] = "BAD"  # type: ignore[index]
    secret_discovery = load("discovery_new.json")
    secret_discovery["Inventory"][0]["route_or_url"] = f"/users?token={SECRET}"  # type: ignore[index]
    secret_plan = plan(secret_discovery, load("snapshot_empty.json"))

    assert gap_plan["operations"][0]["operation"] == "APPEND_DISCOVERY_GAP"
    assert conflict_plan["summary"]["conflict_count"] >= 1
    assert plan(load("discovery_new.json"), bad_snapshot)["summary"]["conflict_count"] == 1
    assert SECRET not in json.dumps(secret_plan)
    assert secret_plan["summary"]["redaction_count"] > 0


def test_inventory_id_conflict_and_retire_missing_policy() -> None:
    discovery = load("discovery_new.json")
    mismatch = load("snapshot_existing.json")
    mismatch["tabs"]["Inventory"][0]["inventory_id"] = "INV-TEMP-NEW"  # type: ignore[index]
    mismatch["tabs"]["Inventory"][0]["source_fingerprint"] = "sha256:different"  # type: ignore[index]
    disabled = plan(load("snapshot_empty.json"), load("snapshot_existing.json"))
    enabled = plan(
        load("snapshot_empty.json"),
        load("snapshot_existing.json"),
        allow_retire_missing=True,
    )
    manual = plan(
        load("snapshot_empty.json"),
        load("snapshot_manual_override.json"),
        allow_retire_missing=True,
    )

    assert plan(discovery, mismatch)["summary"]["conflict_count"] >= 1
    assert disabled["summary"]["retire_count"] == 0
    assert enabled["operations"][0]["operation"] == "MARK_INVENTORY_RETIRED"
    assert manual["summary"]["retire_count"] == 0


def test_top_level_inventory_schema_missing_and_deterministic_except_generated_at(
    tmp_path: Path,
) -> None:
    discovery = load("discovery_new.json")
    snapshot = {"Inventory": load("snapshot_existing.json")["tabs"]["Inventory"]}
    first = plan(discovery, snapshot)
    second = build_sync_plan(
        discovery,
        snapshot,
        headers=headers(),
        schema_version=1,
        generated_at="2026-07-03T00:00:00Z",
    )
    bad_schema = tmp_path / "schema.json"
    bad_schema.write_text(
        json.dumps({"schema_version": 1, "tabs": [{"name": "Inventory", "columns": []}]}),
        encoding="utf-8",
    )

    assert first["summary"] == second["summary"]
    assert first["operations"] == second["operations"]
    with pytest.raises(InventorySyncError):
        inventory_headers(bad_schema)


def test_max_operations_checked_by_cli_layer_shape() -> None:
    result = plan(load("discovery_new.json"), load("snapshot_empty.json"))

    assert result["summary"]["operation_count"] == 1
    assert json.loads(json.dumps(result))["plan_schema_version"] == 1


def rendered_inventory_rows(snapshot: dict[str, object]) -> list[dict[str, str]]:
    rows = snapshot.get("tabs", {}).get("Inventory", [])  # type: ignore[union-attr]
    rendered: list[dict[str, str]] = []
    for row in rows:  # type: ignore[assignment]
        assert isinstance(row, dict)
        rendered.append({key: render_cell_value(value) for key, value in row.items()})
    return rendered


def backend_for_snapshot(snapshot: dict[str, object]) -> FakeSheetsBackend:
    return FakeSheetsBackend(
        tabs={"Inventory": headers()},
        rows={"Inventory": rendered_inventory_rows(snapshot)},
    )


def make_wal(tmp_path: Path, events: list[str] | None = None) -> AppendOnlyWal:
    def fsync(_fd: int) -> None:
        if events is not None:
            if "inventory_read_back" in events:
                events.append("wal_ack_fsync")
            else:
                events.append("wal_pending_fsync")

    return AppendOnlyWal(
        tmp_path / "inventory.jsonl",
        "run-6c1",
        clock=lambda: "2026-07-02T00:00:00Z",
        uuid_factory=lambda: "unused",
        fsync_func=fsync,
    )


def test_apply_append_with_wal_readback_and_secret_redaction(tmp_path: Path) -> None:
    discovery = load("discovery_new.json")
    discovery["Inventory"][0]["route_or_url"] = f"/users?token={SECRET}"  # type: ignore[index]
    sync_plan = plan(discovery, load("snapshot_empty.json"))
    events: list[str] = []
    backend = backend_for_snapshot(load("snapshot_empty.json"))
    original_apply_batch = backend.apply_batch
    original_read_spreadsheet = backend.read_spreadsheet

    def apply_batch_with_event(mutations: object) -> object:
        events.append("inventory_batch")
        return original_apply_batch(mutations)  # type: ignore[arg-type]

    def read_spreadsheet_with_event() -> object:
        if "inventory_batch" in events and "inventory_read_back" not in events:
            events.append("inventory_read_back")
        return original_read_spreadsheet()

    backend.apply_batch = apply_batch_with_event  # type: ignore[method-assign]
    backend.read_spreadsheet = read_spreadsheet_with_event  # type: ignore[method-assign]
    wal = make_wal(tmp_path, events)

    result = apply_inventory_sync_plan(
        sync_plan,
        load("snapshot_empty.json"),
        headers=headers(),
        backend=backend,
        wal=wal,
    )

    rows = backend.read_spreadsheet().rows_dict()["Inventory"]
    wal_text = wal.path.read_text(encoding="utf-8")
    assert result.outcome == "APPLIED"
    assert result.wal_pending_written is True
    assert result.wal_acknowledged is True
    assert result.read_back_verified is True
    assert rows[0]["inventory_id"] == "INV-TEMP-NEW"
    assert SECRET not in json.dumps(rows)
    assert SECRET not in wal_text
    assert '"status":"pending"' in wal_text
    assert '"status":"acknowledged"' in wal_text
    assert events == [
        "wal_pending_fsync",
        "inventory_batch",
        "inventory_read_back",
        "wal_ack_fsync",
    ]


def test_apply_update_preserves_human_and_unknown_columns(tmp_path: Path) -> None:
    discovery = load("discovery_new.json")
    discovery["Inventory"][0]["risk"] = "HIGH"  # type: ignore[index]
    snapshot = load("snapshot_existing.json")
    rows = rendered_inventory_rows(snapshot)
    rows[0]["human_extra"] = "keep"
    sync_plan = plan(discovery, snapshot)
    backend = FakeSheetsBackend(
        tabs={"Inventory": headers()},
        rows={"Inventory": rows},
    )

    result = apply_inventory_sync_plan(
        sync_plan,
        snapshot,
        headers=headers(),
        backend=backend,
        wal=make_wal(tmp_path),
    )

    applied = backend.read_spreadsheet().rows_dict()["Inventory"][0]
    assert result.outcome == "APPLIED"
    assert applied["risk"] == "HIGH"
    assert applied["notes"] == "human note"
    assert applied["human_extra"] == "keep"


def test_conflicted_or_stale_plan_is_rejected_before_wal_and_write(tmp_path: Path) -> None:
    conflicted = plan(load("discovery_conflict.json"), load("snapshot_empty.json"))
    stale_snapshot = load("snapshot_empty.json")
    stale_snapshot["tabs"]["Inventory"] = [  # type: ignore[index]
        {"inventory_id": "INV-TEMP-NEW", "source_fingerprint": "sha256:new"}
    ]
    append_plan = plan(load("discovery_new.json"), load("snapshot_empty.json"))
    backend = backend_for_snapshot(load("snapshot_empty.json"))
    wal = make_wal(tmp_path)

    with pytest.raises(InventorySyncError) as conflict_exc:
        apply_inventory_sync_plan(
            conflicted,
            load("snapshot_empty.json"),
            headers=headers(),
            backend=backend,
            wal=wal,
        )
    with pytest.raises(InventorySyncError) as stale_exc:
        apply_inventory_sync_plan(
            append_plan,
            stale_snapshot,
            headers=headers(),
            backend=backend,
            wal=wal,
        )

    assert conflict_exc.value.code == "INVENTORY_APPLY_PLAN_CONFLICT"
    assert stale_exc.value.code == "INVENTORY_APPLY_STALE_SNAPSHOT"
    assert backend.write_count == 0
    assert not wal.path.exists()


def test_backend_stale_state_is_rejected_before_wal_and_write(tmp_path: Path) -> None:
    sync_plan = plan(load("discovery_new.json"), load("snapshot_empty.json"))
    backend = backend_for_snapshot(load("snapshot_empty.json"))
    backend.set_rows_direct(
        "Inventory",
        [{"inventory_id": "INV-TEMP-NEW", "source_fingerprint": "sha256:new"}],
    )
    wal = make_wal(tmp_path)

    with pytest.raises(InventorySyncError) as exc_info:
        apply_inventory_sync_plan(
            sync_plan,
            load("snapshot_empty.json"),
            headers=headers(),
            backend=backend,
            wal=wal,
        )

    assert exc_info.value.code == "INVENTORY_APPLY_STALE_SNAPSHOT"
    assert backend.write_count == 0
    assert not wal.path.exists()


def test_ambiguous_apply_acknowledges_only_after_readback(tmp_path: Path) -> None:
    sync_plan = plan(load("discovery_new.json"), load("snapshot_empty.json"))
    backend = backend_for_snapshot(load("snapshot_empty.json"))
    backend.fail_after_apply = True
    wal = make_wal(tmp_path)

    result = apply_inventory_sync_plan(
        sync_plan,
        load("snapshot_empty.json"),
        headers=headers(),
        backend=backend,
        wal=wal,
    )

    assert result.outcome == "APPLIED_AFTER_AMBIGUOUS"
    assert result.wal_acknowledged is True
    assert len(wal.path.read_text(encoding="utf-8").splitlines()) == 2


def test_readback_mismatch_leaves_pending_without_ack(tmp_path: Path) -> None:
    discovery = load("discovery_new.json")
    discovery["Inventory"][0]["risk"] = "HIGH"  # type: ignore[index]
    snapshot = load("snapshot_existing.json")
    sync_plan = plan(discovery, snapshot)
    backend = backend_for_snapshot(snapshot)

    def conflict(fake: FakeSheetsBackend) -> None:
        row = fake.read_spreadsheet().rows_dict()["Inventory"][0]
        row["risk"] = "LOW"
        fake.set_rows_direct("Inventory", [row])

    backend.after_apply_hook = conflict
    wal = make_wal(tmp_path)

    with pytest.raises(InventorySyncError) as exc_info:
        apply_inventory_sync_plan(
            sync_plan,
            snapshot,
            headers=headers(),
            backend=backend,
            wal=wal,
        )

    assert exc_info.value.code == "INVENTORY_APPLY_READ_BACK_MISMATCH"
    wal_text = wal.path.read_text(encoding="utf-8")
    assert '"status":"pending"' in wal_text
    assert '"status":"acknowledged"' not in wal_text
    assert SECRET not in str(exc_info.value)
