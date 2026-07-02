from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes.sheets_backend import FakeSheetsBackend
from webapp_debug_skill.inventory_sync import inventory_headers, render_cell_value
from webapp_debug_skill.scenario_model import scenario_from_inventory_row
from webapp_debug_skill.scenario_sync import (
    SCENARIOS_TAB,
    ScenarioSyncDependencies,
    ScenarioSyncError,
    apply_scenario_sync_plan,
    build_scenario_sync_plan,
    main,
    reconcile_scenario_apply_plan,
    scenario_headers_from_schema,
)
from webapp_debug_skill.wal import AppendOnlyWal

SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SCENARIO_SYNC"


def headers() -> tuple[str, ...]:
    return scenario_headers_from_schema(SCHEMA)


def inv_headers() -> tuple[str, ...]:
    return inventory_headers(SCHEMA)[0]


def inventory_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "inventory_id": "INV-001",
        "feature_area": "Users",
        "name": "Users::index",
        "actor_roles": ["admin"],
        "route_or_trigger": "/users",
        "source_path": "src/Controller/UsersController.php",
        "source_symbol": "Users::index",
        "source_lines": "6",
        "source_fingerprint": "sha256:users-index",
        "test_scope": "E2E_PLAYWRIGHT",
        "discovery_status": "DISCOVERED",
        "risk": "MEDIUM",
    }
    row.update(overrides)
    return row


def discovery(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "snapshot_schema_version": 1,
        "source": {"kind": "test"},
        "Inventory": list(rows),
        "Discovery Gaps": [],
    }


def snapshot(
    *,
    inventory: list[dict[str, object]] | None = None,
    scenarios: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "snapshot_schema_version": 1,
        "source": {"kind": "google_sheets"},
        "tabs": {
            "Inventory": inventory or [],
            "Scenarios": scenarios or [],
        },
    }


def scenario_row(row: dict[str, object] | None = None, **ids: str) -> dict[str, object]:
    source = row or inventory_row()
    scenario = scenario_from_inventory_row(
        source,
        feature_id=ids.get("feature_id", "FEAT-000001"),
        story_id=ids.get("story_id", "STORY-000001"),
        scenario_id=ids.get("scenario_id", "SCN-000001"),
    )
    return scenario.to_sheet_row()


def plan(
    discovery_payload: dict[str, object],
    snapshot_payload: dict[str, object],
    *,
    allow_retire_missing: bool = False,
    include_inventory_headers: bool = False,
) -> dict[str, object]:
    return build_scenario_sync_plan(
        discovery_payload,
        snapshot_payload,
        scenario_headers=headers(),
        inventory_headers=inv_headers() if include_inventory_headers else None,
        generated_at="2026-07-02T00:00:00Z",
        allow_retire_missing=allow_retire_missing,
    )


def write_json(tmp_path: Path, name: str, payload: dict[str, object]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def rendered_rows(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    return [{key: render_cell_value(value) for key, value in row.items()} for row in rows]


def backend_for_snapshot(snapshot_payload: dict[str, object]) -> FakeSheetsBackend:
    tabs = snapshot_payload["tabs"]
    assert isinstance(tabs, dict)
    inventory_rows = tabs.get("Inventory", [])
    scenario_rows = tabs.get("Scenarios", [])
    assert isinstance(inventory_rows, list)
    assert isinstance(scenario_rows, list)
    return FakeSheetsBackend(
        tabs={"Inventory": inv_headers(), "Scenarios": headers()},
        rows={
            "Inventory": rendered_rows(inventory_rows),  # type: ignore[arg-type]
            "Scenarios": rendered_rows(scenario_rows),  # type: ignore[arg-type]
        },
    )


def make_wal(tmp_path: Path, events: list[str] | None = None) -> AppendOnlyWal:
    def fsync(_fd: int) -> None:
        if events is not None:
            if "scenario_read_back" in events:
                events.append("wal_ack_fsync")
            else:
                events.append("wal_pending_fsync")

    return AppendOnlyWal(
        tmp_path / "scenario.jsonl",
        "run-7c",
        clock=lambda: "2026-07-02T00:00:00Z",
        uuid_factory=lambda: "unused",
        fsync_func=fsync,
    )


def test_new_scenario_plan_allocates_deterministic_ids_and_structured_row() -> None:
    result = plan(
        discovery(inventory_row()),
        snapshot(inventory=[inventory_row()]),
        include_inventory_headers=True,
    )
    operation = result["operations"][0]
    row = operation["row"]

    assert result["summary"]["append_count"] == 1
    assert result["summary"]["inventory_mapping_count"] == 1
    assert operation["operation"] == "APPEND_SCENARIO"
    assert operation["scenario_id"] == "SCN-000001"
    assert operation["row_coordinate"]["mode"] == "append"
    assert row["feature_id"] == "FEAT-000001"
    assert row["story_id"] == "STORY-000001"
    assert row["inventory_ids"] == "INV-001"
    assert json.loads(row["structured_actions"])[0]["kind"] == "NAVIGATE"
    assert json.loads(row["structured_assertions"])[0]["kind"] == "VISIBLE"
    assert "manual_override" not in row
    assert operation["operation_id"].startswith("SCENARIO-")
    mapping = result["operations"][1]
    assert mapping["operation"] == "UPDATE_INVENTORY_MAPPING"
    assert mapping["fields"]["mapped_scenario_ids"] == "SCN-000001"
    assert mapping["fields"]["discovery_status"] == "MAPPED"


def test_inventory_mapping_does_not_close_discovery_gap() -> None:
    gap = inventory_row(discovery_status="DISCOVERY_GAP")

    result = plan(discovery(gap), snapshot(inventory=[gap]), include_inventory_headers=True)

    mapping = result["operations"][1]
    assert mapping["operation"] == "UPDATE_INVENTORY_MAPPING"
    assert mapping["fields"] == {"mapped_scenario_ids": "SCN-000001"}


def test_existing_scenario_update_preserves_manual_and_unknown_fields() -> None:
    existing = scenario_row()
    existing["manual_override"] = True
    existing["manual_expected_behavior"] = "手動期待"
    existing["human_extra"] = "keep"
    changed = inventory_row(route_or_trigger="/users/new", risk="HIGH")

    result = plan(
        discovery(changed),
        snapshot(inventory=[changed], scenarios=[existing]),
    )
    operation = result["operations"][0]

    assert operation["operation"] == "UPDATE_SCENARIO_FIELDS"
    assert "manual_override" not in operation["fields"]
    assert "manual_expected_behavior" not in operation["fields"]
    assert "expected_results" not in operation["fields"]
    assert "structured_assertions" not in operation["fields"]
    assert "human_extra" not in operation["fields"]
    assert operation["fields"]["route_or_url"] == "/users/new"


def test_ambiguous_mapping_conflict_blocks_cli_success() -> None:
    first = scenario_row(scenario_id="SCN-000001")
    second = scenario_row(
        scenario_id="SCN-000002", feature_id="FEAT-000002", story_id="STORY-000002"
    )

    result = plan(
        discovery(inventory_row()),
        snapshot(inventory=[inventory_row()], scenarios=[first, second]),
    )

    assert result["summary"]["conflict_count"] >= 1
    assert result["conflicts"][0]["reason_code"] in {
        "DUPLICATE_INVENTORY_MAPPING",
        "AMBIGUOUS_SCENARIO_MAPPING",
    }


def test_retire_policy_is_explicit() -> None:
    existing = scenario_row()
    disabled = plan(discovery(), snapshot(inventory=[], scenarios=[existing]))
    enabled = plan(
        discovery(), snapshot(inventory=[], scenarios=[existing]), allow_retire_missing=True
    )

    assert disabled["summary"]["retire_count"] == 0
    assert enabled["operations"][0]["operation"] == "MARK_SCENARIO_RETIRED"
    assert enabled["operations"][0]["fields"]["lifecycle_status"] == "RETIRED"


def test_secret_marker_is_not_written_to_plan_or_errors() -> None:
    secret_row = inventory_row(route_or_trigger=f"/users?token={SECRET}")

    result = plan(discovery(secret_row), snapshot(inventory=[secret_row]))

    assert_no_secret(json.dumps(result, ensure_ascii=False))


def test_cli_text_json_exit_codes_and_output_safety(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    discovery_path = write_json(tmp_path, "discovery.json", discovery(inventory_row()))
    snapshot_path = write_json(tmp_path, "snapshot.json", snapshot(inventory=[inventory_row()]))
    output_path = tmp_path / "scenario-plan.json"
    deps = ScenarioSyncDependencies(clock=lambda: datetime(2026, 7, 2, tzinfo=UTC))

    text_code = main(
        [
            "--discovery-json",
            str(discovery_path),
            "--snapshot-json",
            str(snapshot_path),
            "--schema",
            str(SCHEMA),
            "--output",
            str(output_path),
        ],
        deps,
    )
    text = capsys.readouterr()
    json_code = main(
        [
            "--discovery-json",
            str(discovery_path),
            "--snapshot-json",
            str(snapshot_path),
            "--schema",
            str(SCHEMA),
            "--output",
            str(tmp_path / "scenario-plan-2.json"),
            "--format",
            "json",
        ],
        deps,
    )
    payload = json.loads(capsys.readouterr().out)

    assert text_code == 0
    assert "SCENARIO_SYNC_OK" in text.out
    assert json_code == 0
    assert payload["ok"] is True
    assert output_path.exists()

    existing_code = main(
        [
            "--discovery-json",
            str(discovery_path),
            "--snapshot-json",
            str(snapshot_path),
            "--schema",
            str(SCHEMA),
            "--output",
            str(output_path),
        ],
        deps,
    )
    captured = capsys.readouterr()
    assert existing_code == 3
    assert "SCENARIO_SYNC_OUTPUT_EXISTS" in captured.out
    assert_no_secret(captured.out, captured.err)


def test_cli_conflict_exit_3_writes_reviewable_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = scenario_row(scenario_id="SCN-000001")
    second = scenario_row(
        scenario_id="SCN-000002", feature_id="FEAT-000002", story_id="STORY-000002"
    )
    discovery_path = write_json(tmp_path, "discovery.json", discovery(inventory_row()))
    snapshot_path = write_json(
        tmp_path,
        "snapshot.json",
        snapshot(inventory=[inventory_row()], scenarios=[first, second]),
    )
    output_path = tmp_path / "conflicted-plan.json"

    code = main(
        [
            "--discovery-json",
            str(discovery_path),
            "--snapshot-json",
            str(snapshot_path),
            "--schema",
            str(SCHEMA),
            "--output",
            str(output_path),
            "--format",
            "json",
        ],
        ScenarioSyncDependencies(clock=lambda: datetime(2026, 7, 2, tzinfo=UTC)),
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["ok"] is False
    assert payload["code"] == "SCENARIO_SYNC_CONFLICT"
    assert json.loads(output_path.read_text(encoding="utf-8"))["summary"]["conflict_count"] >= 1


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--discovery-json" in captured.out
    assert "--allow-retire-missing" in captured.out


def test_apply_append_mapping_with_wal_readback_and_secret_safety(tmp_path: Path) -> None:
    base_snapshot = snapshot(inventory=[inventory_row()])
    sync_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    backend = backend_for_snapshot(base_snapshot)
    events: list[str] = []
    original_apply_batch = backend.apply_batch
    original_read_spreadsheet = backend.read_spreadsheet

    def apply_batch_with_event(mutations: object) -> object:
        events.append("scenario_batch")
        return original_apply_batch(mutations)  # type: ignore[arg-type]

    def read_spreadsheet_with_event() -> object:
        if "scenario_batch" in events and "scenario_read_back" not in events:
            events.append("scenario_read_back")
        return original_read_spreadsheet()

    backend.apply_batch = apply_batch_with_event  # type: ignore[method-assign]
    backend.read_spreadsheet = read_spreadsheet_with_event  # type: ignore[method-assign]
    wal = make_wal(tmp_path, events)

    result = apply_scenario_sync_plan(
        sync_plan,
        base_snapshot,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=backend,
        wal=wal,
    )

    rows = backend.read_spreadsheet().rows_dict()
    wal_text = wal.path.read_text(encoding="utf-8")
    assert result.outcome == "APPLIED"
    assert result.wal_pending_written is True
    assert result.wal_acknowledged is True
    assert result.read_back_verified is True
    assert rows["Scenarios"][0]["scenario_id"] == "SCN-000001"
    assert rows["Inventory"][0]["mapped_scenario_ids"] == "SCN-000001"
    assert rows["Inventory"][0]["discovery_status"] == "MAPPED"
    assert '"operation":"scenario.apply"' in wal_text
    assert '"status":"pending"' in wal_text
    assert '"status":"acknowledged"' in wal_text
    assert SECRET not in wal_text
    assert events == [
        "wal_pending_fsync",
        "scenario_batch",
        "scenario_read_back",
        "wal_ack_fsync",
    ]


def test_apply_update_preserves_manual_and_unknown_columns(tmp_path: Path) -> None:
    existing = scenario_row()
    existing["manual_override"] = True
    existing["manual_expected_behavior"] = "手動期待"
    existing["human_extra"] = "keep"
    changed = inventory_row(route_or_trigger="/users/new", risk="HIGH")
    base_snapshot = snapshot(inventory=[changed], scenarios=[existing])
    sync_plan = plan(
        discovery(changed),
        base_snapshot,
        include_inventory_headers=True,
    )
    backend = backend_for_snapshot(base_snapshot)

    result = apply_scenario_sync_plan(
        sync_plan,
        base_snapshot,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=backend,
        wal=make_wal(tmp_path),
    )

    applied = backend.read_spreadsheet().rows_dict()["Scenarios"][0]
    assert result.outcome == "APPLIED"
    assert applied["route_or_url"] == "/users/new"
    assert applied["manual_expected_behavior"] == "手動期待"
    assert applied["human_extra"] == "keep"
    assert applied["expected_results"] == render_cell_value(existing["expected_results"])


def test_conflicted_stale_and_tampered_plan_rejected_before_wal_and_write(tmp_path: Path) -> None:
    first = scenario_row(scenario_id="SCN-000001")
    second = scenario_row(
        scenario_id="SCN-000002", feature_id="FEAT-000002", story_id="STORY-000002"
    )
    conflicted = plan(
        discovery(inventory_row()),
        snapshot(inventory=[inventory_row()], scenarios=[first, second]),
        include_inventory_headers=True,
    )
    base_snapshot = snapshot(inventory=[inventory_row()])
    append_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    stale_snapshot = snapshot(
        inventory=[inventory_row(inventory_id="INV-002", source_fingerprint="sha256:other")]
    )
    tampered = json.loads(json.dumps(append_plan))
    tampered["operations"][0]["row"]["manual_override"] = "true"
    backend = backend_for_snapshot(base_snapshot)
    wal = make_wal(tmp_path)

    with pytest.raises(ScenarioSyncError) as conflict_exc:
        apply_scenario_sync_plan(
            conflicted,
            base_snapshot,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=backend,
            wal=wal,
        )
    with pytest.raises(ScenarioSyncError) as stale_exc:
        apply_scenario_sync_plan(
            append_plan,
            stale_snapshot,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=backend,
            wal=wal,
        )
    with pytest.raises(ScenarioSyncError) as tampered_exc:
        apply_scenario_sync_plan(
            tampered,
            base_snapshot,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=backend,
            wal=wal,
        )

    assert conflict_exc.value.code == "SCENARIO_APPLY_PLAN_CONFLICT"
    assert stale_exc.value.code == "SCENARIO_APPLY_STALE_SNAPSHOT"
    assert tampered_exc.value.code == "SCENARIO_APPLY_PLAN_INVALID"
    assert backend.write_count == 0
    assert not wal.path.exists()
    assert_no_secret(conflict_exc.value, stale_exc.value, tampered_exc.value)


def test_backend_stale_state_is_rejected_before_wal_and_write(tmp_path: Path) -> None:
    base_snapshot = snapshot(inventory=[inventory_row()])
    sync_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    backend = backend_for_snapshot(base_snapshot)
    backend.set_rows_direct(
        "Inventory",
        rendered_rows([inventory_row(inventory_id="INV-002", source_fingerprint="sha256:other")]),
    )
    wal = make_wal(tmp_path)

    with pytest.raises(ScenarioSyncError) as exc_info:
        apply_scenario_sync_plan(
            sync_plan,
            base_snapshot,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=backend,
            wal=wal,
        )

    assert exc_info.value.code == "SCENARIO_APPLY_STALE_SNAPSHOT"
    assert backend.write_count == 0
    assert not wal.path.exists()


def test_ambiguous_apply_acknowledges_only_after_readback(tmp_path: Path) -> None:
    base_snapshot = snapshot(inventory=[inventory_row()])
    sync_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    backend = backend_for_snapshot(base_snapshot)
    backend.fail_after_apply = True
    wal = make_wal(tmp_path)

    result = apply_scenario_sync_plan(
        sync_plan,
        base_snapshot,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=backend,
        wal=wal,
    )

    assert result.outcome == "APPLIED_AFTER_AMBIGUOUS"
    assert result.wal_acknowledged is True
    assert len(wal.path.read_text(encoding="utf-8").splitlines()) == 2


def test_readback_mismatch_leaves_pending_without_ack(tmp_path: Path) -> None:
    base_snapshot = snapshot(inventory=[inventory_row()])
    sync_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    backend = backend_for_snapshot(base_snapshot)

    def conflict(fake: FakeSheetsBackend) -> None:
        rows = fake.read_spreadsheet().rows_dict()[SCENARIOS_TAB]
        rows[0]["scenario_title"] = "changed"
        fake.set_rows_direct(SCENARIOS_TAB, rows)

    backend.after_apply_hook = conflict
    wal = make_wal(tmp_path)

    with pytest.raises(ScenarioSyncError) as exc_info:
        apply_scenario_sync_plan(
            sync_plan,
            base_snapshot,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=backend,
            wal=wal,
        )

    wal_text = wal.path.read_text(encoding="utf-8")
    assert exc_info.value.code == "SCENARIO_APPLY_READ_BACK_MISMATCH"
    assert '"status":"pending"' in wal_text
    assert '"status":"acknowledged"' not in wal_text
    assert SECRET not in str(exc_info.value)


def test_reconcile_pending_applied_unapplied_and_conflict(tmp_path: Path) -> None:
    base_snapshot = snapshot(inventory=[inventory_row()])
    sync_plan = plan(
        discovery(inventory_row()),
        base_snapshot,
        include_inventory_headers=True,
    )
    applied_backend = backend_for_snapshot(base_snapshot)
    wal = make_wal(tmp_path)
    apply_scenario_sync_plan(
        sync_plan,
        base_snapshot,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=applied_backend,
        wal=wal,
    )

    no_pending = reconcile_scenario_apply_plan(
        sync_plan,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=applied_backend,
        wal=wal,
    )
    assert no_pending.outcome == "NO_PENDING"

    pending_backend = backend_for_snapshot(base_snapshot)
    pending_wal = make_wal(tmp_path / "pending")
    from webapp_debug_skill.scenario_sync import scenario_apply_wal_payload

    payload = scenario_apply_wal_payload(sync_plan, sync_plan["operations"])  # type: ignore[arg-type]
    pending_wal.append_pending("scenario.apply", payload, operation_id="SCN-APPLY-PENDING")
    retry = reconcile_scenario_apply_plan(
        sync_plan,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=pending_backend,
        wal=pending_wal,
    )
    assert retry.outcome == "RETRY_REQUIRED"
    assert '"status":"acknowledged"' not in pending_wal.path.read_text(encoding="utf-8")

    applied_pending = make_wal(tmp_path / "applied-pending")
    applied_pending.append_pending("scenario.apply", payload, operation_id="SCN-APPLY-APPLIED")
    already = reconcile_scenario_apply_plan(
        sync_plan,
        scenario_headers=headers(),
        inventory_headers=inv_headers(),
        backend=applied_backend,
        wal=applied_pending,
    )
    assert already.outcome == "ALREADY_APPLIED"
    assert already.wal_acknowledged is True

    conflict_backend = backend_for_snapshot(base_snapshot)
    conflict_backend.set_rows_direct("Scenarios", [{"scenario_id": "SCN-999999"}])
    conflict_wal = make_wal(tmp_path / "conflict")
    conflict_wal.append_pending("scenario.apply", payload, operation_id="SCN-APPLY-CONFLICT")
    with pytest.raises(ScenarioSyncError) as exc_info:
        reconcile_scenario_apply_plan(
            sync_plan,
            scenario_headers=headers(),
            inventory_headers=inv_headers(),
            backend=conflict_backend,
            wal=conflict_wal,
        )
    assert exc_info.value.code == "SCENARIO_APPLY_RECONCILE_CONFLICT"
