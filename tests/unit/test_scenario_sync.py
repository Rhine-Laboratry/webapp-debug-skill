from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from webapp_debug_skill.scenario_model import scenario_from_inventory_row
from webapp_debug_skill.scenario_sync import (
    ScenarioSyncDependencies,
    build_scenario_sync_plan,
    main,
    scenario_headers_from_schema,
)

SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SCENARIO_SYNC"


def headers() -> tuple[str, ...]:
    return scenario_headers_from_schema(SCHEMA)


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
) -> dict[str, object]:
    return build_scenario_sync_plan(
        discovery_payload,
        snapshot_payload,
        scenario_headers=headers(),
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


def test_new_scenario_plan_allocates_deterministic_ids_and_structured_row() -> None:
    result = plan(discovery(inventory_row()), snapshot(inventory=[inventory_row()]))
    operation = result["operations"][0]
    row = operation["row"]

    assert result["summary"]["append_count"] == 1
    assert operation["operation"] == "APPEND_SCENARIO"
    assert operation["scenario_id"] == "SCN-000001"
    assert row["feature_id"] == "FEAT-000001"
    assert row["story_id"] == "STORY-000001"
    assert row["inventory_ids"] == "INV-001"
    assert json.loads(row["structured_actions"])[0]["kind"] == "NAVIGATE"
    assert json.loads(row["structured_assertions"])[0]["kind"] == "VISIBLE"
    assert "manual_override" not in row
    assert operation["operation_id"].startswith("SCENARIO-")


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
