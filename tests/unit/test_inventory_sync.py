from __future__ import annotations

import json
from pathlib import Path

import pytest

from webapp_debug_skill.inventory_sync import (
    InventorySyncError,
    build_sync_plan,
    inventory_headers,
)

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
