"""Local Scenario sync plan generation."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.inventory_identity import fingerprint_payload, stable_source_fingerprint
from webapp_debug_skill.inventory_model import dumps_snapshot, rfc3339_utc, safe_text
from webapp_debug_skill.inventory_sync import (
    INVENTORY_TAB,
    ROW_NUMBER_KEY,
    extract_discovery_rows,
    extract_snapshot_inventory,
    inventory_apply_fingerprint,
    inventory_headers,
    render_cell_value,
    row_coordinate as inventory_row_coordinate,
    row_values as inventory_row_values,
    row_value,
)
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.scenario_model import (
    SCENARIO_MANUAL_COLUMNS,
    ScenarioContract,
    ScenarioGenerationStatus,
    ScenarioLifecycleStatus,
    ScenarioModelError,
    automatic_scenario_columns,
    scenario_from_inventory_row,
)
from webapp_debug_skill.sheets_client import (
    AppendRows,
    SheetsBackend,
    SheetsBackendError,
    UpdateRowValues,
    validate_batch,
)
from webapp_debug_skill.status_model import InventoryStatus, normalize_inventory_status
from webapp_debug_skill.wal import AppendOnlyWal, WalError

PLAN_SCHEMA_VERSION = 1
DEFAULT_MAX_OPERATIONS = 10_000
SCENARIOS_TAB = "Scenarios"
SCENARIO_APPLY_OPERATION = "scenario.apply"
SCENARIO_ID_PREFIX = "SCN"
FEATURE_ID_PREFIX = "FEAT"
STORY_ID_PREFIX = "STORY"
ID_WIDTH = 6
LOCKED_INVENTORY_STATUSES = {
    InventoryStatus.EXCLUDED_WITH_REASON,
    InventoryStatus.RETIRED,
    InventoryStatus.MERGED,
}
AUTO_PROTECTED_WHEN_MANUAL_OVERRIDE = {
    "expected_results",
    "structured_assertions",
    "expectation_status",
    "expectation_source_refs",
    "priority",
}


class ScenarioSyncError(RuntimeError):
    """Safe Scenario sync planning error."""

    def __init__(
        self,
        code: str,
        path: str = "scenario_sync",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
    ) -> None:
        safe_code = "SCENARIO_SYNC_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "scenario_sync" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code


@dataclass(frozen=True)
class ScenarioSyncDependencies:
    """Injectable dependencies for CLI tests."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class ScenarioApplyResult:
    """Result for fake/unit Scenario apply execution."""

    outcome: str
    applied_mutation_count: int
    wal_pending_written: bool
    wal_acknowledged: bool
    read_back_verified: bool
    operation_count: int


@dataclass(frozen=True)
class ScenarioReconcileResult:
    """Read-only reconciliation result for pending Scenario apply WAL entries."""

    outcome: str
    pending_count: int
    wal_acknowledged: bool
    read_back_verified: bool


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(description="Plan local Scenario sync operations.")
    parser.add_argument("--discovery-json", type=Path, required=True)
    parser.add_argument("--snapshot-json", type=Path, required=True)
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("skills/webapp-debug/assets/google-sheets-schema.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-retire-missing", action="store_true")
    parser.add_argument("--max-operations", type=int, default=DEFAULT_MAX_OPERATIONS)
    return parser


def main(
    argv: list[str] | None = None,
    deps: ScenarioSyncDependencies | None = None,
) -> int:
    """Run CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or ScenarioSyncDependencies()
    try:
        result = run(args, dependencies)
    except ScenarioSyncError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Scenario sync planning failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="SCENARIO_SYNC_UNEXPECTED",
            message="Scenario sync planning failed unexpectedly.",
            details=[Issue("scenario_sync", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    return EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: ScenarioSyncDependencies) -> CliResult:
    """Run local planning and write Scenario plan JSON."""

    discovery_path = args.discovery_json.resolve(strict=False)
    snapshot_path = args.snapshot_json.resolve(strict=False)
    schema_path = args.schema.resolve(strict=False)
    output_path = absolute_no_resolve(args.output)
    if args.max_operations < 1:
        raise ScenarioSyncError(
            "SCENARIO_SYNC_MAX_OPERATIONS_INVALID",
            "max_operations",
            "BELOW_MINIMUM",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    validate_input_file(discovery_path, "discovery_json")
    validate_input_file(snapshot_path, "snapshot_json")
    validate_input_file(schema_path, "schema")
    validate_output_path(
        output_path,
        force=args.force,
        protected_paths=(discovery_path, snapshot_path, schema_path),
    )
    discovery_payload = read_json_file(discovery_path, "discovery_json")
    snapshot_payload = read_json_file(snapshot_path, "snapshot_json")
    scenario_headers = scenario_headers_from_schema(schema_path)
    inventory_header_values, _schema_version = inventory_headers(schema_path)
    plan = build_scenario_sync_plan(
        discovery_payload,
        snapshot_payload,
        scenario_headers=scenario_headers,
        inventory_headers=inventory_header_values,
        generated_at=rfc3339_utc(deps.clock()),
        allow_retire_missing=args.allow_retire_missing,
    )
    if int(plan["summary"]["operation_count"]) > args.max_operations:
        raise ScenarioSyncError("SCENARIO_SYNC_TOO_MANY_OPERATIONS", "operations", "MAX_EXCEEDED")
    atomic_write_plan(output_path, plan)
    summary = plan["summary"]
    data = {
        "output": output_path.name,
        "append_count": summary["append_count"],
        "update_count": summary["update_count"],
        "retire_count": summary["retire_count"],
        "conflict_count": summary["conflict_count"],
        "noop_count": summary["noop_count"],
        "operation_count": summary["operation_count"],
    }
    if int(summary["conflict_count"]) > 0:
        return CliResult(
            ok=False,
            code="SCENARIO_SYNC_CONFLICT",
            message="Scenario sync plan contains conflicts.",
            details=[Issue("conflicts", "REVIEW_REQUIRED")],
            data=data,
        )
    return CliResult(
        ok=True,
        code="SCENARIO_SYNC_OK",
        message="Scenario sync plan generated.",
        details=[],
        data=data,
    )


def build_scenario_sync_plan(
    discovery_payload: Mapping[str, Any],
    snapshot_payload: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str] | None = None,
    generated_at: str,
    allow_retire_missing: bool = False,
) -> dict[str, Any]:
    """Build a deterministic local Scenario sync plan."""

    discovery_rows, discovery_gaps = extract_discovery_rows(discovery_payload)
    snapshot_inventory_rows = extract_snapshot_inventory(snapshot_payload)
    inventory_rows = snapshot_inventory_rows if snapshot_inventory_rows else discovery_rows
    scenario_rows = extract_snapshot_scenarios(snapshot_payload)
    inventory_rows_by_id = {
        row_value(row, "inventory_id"): (index, row)
        for index, row in enumerate(snapshot_inventory_rows)
        if row_value(row, "inventory_id")
    }
    scenario_index_by_id = {
        row_value(row, "scenario_id"): index
        for index, row in enumerate(scenario_rows)
        if row_value(row, "scenario_id")
    }
    conflicts: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    noop_count = 0
    scenario_headers = tuple(scenario_headers)
    inventory_header_values = tuple(inventory_headers or ())
    scenario_snapshot = scenario_snapshot_fingerprint(scenario_headers, scenario_rows)
    inventory_snapshot = (
        inventory_apply_fingerprint(inventory_header_values, snapshot_inventory_rows)
        if inventory_header_values
        else ""
    )
    existing = parse_existing_scenarios(scenario_rows, conflicts)
    manual_overrides = manual_override_by_id(scenario_rows)
    id_state = allocation_state(existing)
    inventory_candidates = sorted(
        [row for row in (*inventory_rows, *discovery_gaps) if scenario_candidate(row)],
        key=inventory_sort_key,
    )
    inventory_ids = {row_value(row, "inventory_id") for row in inventory_candidates}

    matched_scenarios: set[str] = set()
    for row in inventory_candidates:
        matches = matching_scenarios(row, existing)
        if len(matches) > 1:
            conflicts.append(conflict("AMBIGUOUS_SCENARIO_MAPPING", row, matches))
            continue
        if len(matches) == 1:
            scenario = matches[0]
            matched_scenarios.add(scenario.scenario_id)
            try:
                update = update_operation(
                    row,
                    scenario,
                    scenario_headers,
                    manual_override=manual_overrides.get(scenario.scenario_id, False),
                    snapshot_index=scenario_index_by_id.get(scenario.scenario_id, -1),
                    snapshot_fingerprint=scenario_snapshot,
                )
            except ScenarioModelError as exc:
                conflicts.append(scenario_model_conflict(row, exc))
                continue
            if update is None:
                noop_count += 1
            else:
                operations.append(update)
            mapping_update = inventory_mapping_operation(
                row,
                scenario.scenario_id,
                inventory_header_values,
                inventory_rows_by_id,
                inventory_snapshot,
            )
            if mapping_update is not None:
                operations.append(mapping_update)
            continue
        try:
            scenario = new_scenario(row, id_state)
        except ScenarioModelError as exc:
            conflicts.append(scenario_model_conflict(row, exc))
            continue
        operations.append(
            append_operation(
                scenario,
                scenario_headers,
                snapshot_row_count=len(scenario_rows),
                snapshot_fingerprint=scenario_snapshot,
            )
        )
        mapping_update = inventory_mapping_operation(
            row,
            scenario.scenario_id,
            inventory_header_values,
            inventory_rows_by_id,
            inventory_snapshot,
        )
        if mapping_update is not None:
            operations.append(mapping_update)

    if allow_retire_missing:
        for scenario in sorted(existing, key=lambda item: item.scenario_id):
            if scenario.scenario_id in matched_scenarios:
                continue
            if scenario.lifecycle_status != ScenarioLifecycleStatus.ACTIVE:
                continue
            if set(scenario.inventory_ids) & inventory_ids:
                continue
            operations.append(
                retire_operation(
                    scenario,
                    snapshot_index=scenario_index_by_id.get(scenario.scenario_id, -1),
                    snapshot_fingerprint=scenario_snapshot,
                )
            )

    summary = {
        "append_count": sum(1 for item in operations if item["operation"] == "APPEND_SCENARIO"),
        "update_count": sum(
            1 for item in operations if item["operation"] == "UPDATE_SCENARIO_FIELDS"
        ),
        "retire_count": sum(
            1 for item in operations if item["operation"] == "MARK_SCENARIO_RETIRED"
        ),
        "inventory_mapping_count": sum(
            1 for item in operations if item["operation"] == "UPDATE_INVENTORY_MAPPING"
        ),
        "conflict_count": len(conflicts),
        "noop_count": noop_count,
        "operation_count": len(operations),
    }
    plan = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "source": {
            "kind": "scenario_sync",
            "snapshot_fingerprint": scenario_snapshot,
            "scenario_snapshot_fingerprint": scenario_snapshot,
            "inventory_snapshot_fingerprint": inventory_snapshot,
            "discovery_fingerprint": fingerprint_payload(discovery_payload),
            "generated_at": generated_at,
        },
        "summary": summary,
        "operations": operations,
        "conflicts": conflicts,
        "warnings": [],
    }
    if secret_value_findings(plan):
        raise ScenarioSyncError("SCENARIO_SYNC_SECRET_DETECTED", "plan", "SECRET_DETECTED")
    return plan


@dataclass
class IdAllocationState:
    """Deterministic ID allocation state."""

    next_feature: int
    next_story: int
    next_scenario: int
    feature_ids: dict[str, str]
    story_ids: dict[str, str]


def allocation_state(existing: Sequence[ScenarioContract]) -> IdAllocationState:
    """Return deterministic ID allocation state from existing Scenarios."""

    feature_ids = {scenario.feature_name: scenario.feature_id for scenario in existing}
    story_ids = {
        story_key(scenario.feature_name, scenario.story_title): scenario.story_id
        for scenario in existing
    }
    return IdAllocationState(
        next_feature=max_id(existing, "feature_id") + 1,
        next_story=max_id(existing, "story_id") + 1,
        next_scenario=max_id(existing, "scenario_id") + 1,
        feature_ids=feature_ids,
        story_ids=story_ids,
    )


def new_scenario(row: Mapping[str, Any], state: IdAllocationState) -> ScenarioContract:
    """Create a new deterministic Scenario contract for one Inventory row."""

    feature_name = row_value(row, "feature_area", "feature_name", "name") or "未分類"
    feature_id = state.feature_ids.get(feature_name)
    if feature_id is None:
        feature_id = make_id(FEATURE_ID_PREFIX, state.next_feature)
        state.next_feature += 1
        state.feature_ids[feature_name] = feature_id
    story_title = f"{feature_name}を利用できる"
    story_key_value = story_key(feature_name, story_title)
    story_id = state.story_ids.get(story_key_value)
    if story_id is None:
        story_id = make_id(STORY_ID_PREFIX, state.next_story)
        state.next_story += 1
        state.story_ids[story_key_value] = story_id
    scenario_id = make_id(SCENARIO_ID_PREFIX, state.next_scenario)
    state.next_scenario += 1
    return scenario_from_inventory_row(
        row,
        feature_id=feature_id,
        story_id=story_id,
        scenario_id=scenario_id,
    )


def update_operation(
    row: Mapping[str, Any],
    existing: ScenarioContract,
    headers: Sequence[str],
    *,
    manual_override: bool,
    snapshot_index: int,
    snapshot_fingerprint: str,
) -> dict[str, Any] | None:
    """Return update operation if automatic Scenario fields changed."""

    expected = existing.to_sheet_row()
    replacement = scenario_from_inventory_row(
        row,
        feature_id=existing.feature_id,
        story_id=existing.story_id,
        scenario_id=existing.scenario_id,
        scenario_version=existing.scenario_version + 1,
    ).to_sheet_row()
    existing_row = scenario_row_subset(expected, headers)
    replacement_row = scenario_row_subset(replacement, headers)
    changed = {
        key: value
        for key, value in replacement_row.items()
        if value != existing_row.get(key, "")
        and key not in SCENARIO_MANUAL_COLUMNS
        and not (manual_override and key in AUTO_PROTECTED_WHEN_MANUAL_OVERRIDE)
    }
    if not changed:
        return None
    return {
        "operation_id": scenario_operation_id(
            "UPDATE_SCENARIO_FIELDS", existing.scenario_id, changed
        ),
        "operation": "UPDATE_SCENARIO_FIELDS",
        "target_tab": SCENARIOS_TAB,
        "scenario_id": existing.scenario_id,
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": scenario_row_coordinate(existing_row, snapshot_index),
        "expected_old_values": expected_scenario_old_values(existing_row, changed),
        "fields": changed,
        "reason": "INVENTORY_CHANGED",
    }


def append_operation(
    scenario: ScenarioContract,
    headers: Sequence[str],
    *,
    snapshot_row_count: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    """Return append operation for a new Scenario."""

    row = scenario_row_subset(scenario.to_sheet_row(), headers)
    return {
        "operation_id": scenario_operation_id("APPEND_SCENARIO", scenario.scenario_id, row),
        "operation": "APPEND_SCENARIO",
        "target_tab": SCENARIOS_TAB,
        "scenario_id": scenario.scenario_id,
        "feature_id": scenario.feature_id,
        "story_id": scenario.story_id,
        "inventory_ids": list(scenario.inventory_ids),
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": scenario_append_coordinate(snapshot_row_count),
        "expected_absent": {
            "scenario_id": scenario.scenario_id,
            "source_fingerprint": scenario.source_fingerprint,
        },
        "row": row,
        "reason": "NEW_SCENARIO",
    }


def retire_operation(
    scenario: ScenarioContract,
    *,
    snapshot_index: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    """Return retire operation for an existing Scenario."""

    fields = {
        "lifecycle_status": ScenarioLifecycleStatus.RETIRED.value,
        "generation_status": ScenarioGenerationStatus.BLOCKED.value,
    }
    return {
        "operation_id": scenario_operation_id(
            "MARK_SCENARIO_RETIRED", scenario.scenario_id, fields
        ),
        "operation": "MARK_SCENARIO_RETIRED",
        "target_tab": SCENARIOS_TAB,
        "scenario_id": scenario.scenario_id,
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": scenario_row_coordinate(scenario.to_sheet_row(), snapshot_index),
        "expected_old_values": {
            "scenario_id": scenario.scenario_id,
            "source_fingerprint": scenario.source_fingerprint,
            "lifecycle_status": scenario.lifecycle_status.value,
            "generation_status": scenario.generation_status.value,
        },
        "fields": fields,
        "reason": "INVENTORY_MISSING",
    }


def parse_existing_scenarios(
    rows: Sequence[Mapping[str, Any]],
    conflicts: list[dict[str, Any]],
) -> list[ScenarioContract]:
    """Parse existing Scenario rows and record safe conflicts."""

    parsed: list[ScenarioContract] = []
    seen_inventory: dict[str, str] = {}
    seen_source: dict[str, str] = {}
    for index, row in enumerate(rows):
        try:
            scenario = ScenarioContract.from_sheet_row(row)
        except ScenarioModelError as exc:
            conflicts.append(
                {
                    "reason_code": "SCENARIO_ROW_INVALID",
                    "source": "Scenarios",
                    "row_index": index,
                    "path": exc.path,
                    "reason": exc.reason,
                }
            )
            continue
        for inventory_id in scenario.inventory_ids:
            previous = seen_inventory.setdefault(inventory_id, scenario.scenario_id)
            if previous != scenario.scenario_id:
                conflicts.append(
                    {
                        "reason_code": "DUPLICATE_INVENTORY_MAPPING",
                        "source": "Scenarios",
                        "inventory_id": inventory_id,
                    }
                )
        previous_source = seen_source.setdefault(scenario.source_fingerprint, scenario.scenario_id)
        if previous_source != scenario.scenario_id:
            conflicts.append(
                {
                    "reason_code": "DUPLICATE_SCENARIO_SOURCE",
                    "source": "Scenarios",
                    "source_fingerprint": scenario.source_fingerprint,
                }
            )
        parsed.append(scenario)
    return parsed


def matching_scenarios(
    row: Mapping[str, Any],
    scenarios: Sequence[ScenarioContract],
) -> list[ScenarioContract]:
    """Return existing Scenarios that match an Inventory row."""

    inventory_id = row_value(row, "inventory_id")
    source_fingerprint = stable_source_fingerprint(row)
    matches = []
    for scenario in scenarios:
        if inventory_id and inventory_id in scenario.inventory_ids:
            matches.append(scenario)
        elif source_fingerprint and source_fingerprint == scenario.source_fingerprint:
            matches.append(scenario)
    return matches


def scenario_candidate(row: Mapping[str, Any]) -> bool:
    """Return whether Inventory row should produce a Scenario plan candidate."""

    inventory_id = row_value(row, "inventory_id")
    if not inventory_id:
        return False
    try:
        status = normalize_inventory_status(
            row_value(row, "discovery_status", "status") or "DISCOVERED"
        )
    except Exception:
        return False
    return status not in LOCKED_INVENTORY_STATUSES


def conflict(
    reason_code: str,
    row: Mapping[str, Any],
    matches: Sequence[ScenarioContract],
) -> dict[str, Any]:
    """Return safe conflict record."""

    return {
        "reason_code": reason_code,
        "source": "Inventory",
        "inventory_id": safe_text(row_value(row, "inventory_id")),
        "source_fingerprint": stable_source_fingerprint(row),
        "scenario_ids": [scenario.scenario_id for scenario in matches],
    }


def scenario_model_conflict(row: Mapping[str, Any], exc: ScenarioModelError) -> dict[str, Any]:
    """Return safe Scenario model conflict without row payload values."""

    return {
        "reason_code": "SCENARIO_MODEL_INVALID",
        "source": "Inventory",
        "inventory_id": safe_text(row_value(row, "inventory_id")),
        "source_fingerprint": stable_source_fingerprint(row),
        "path": exc.path,
        "reason": exc.reason,
    }


def scenario_row_subset(row: Mapping[str, Any], headers: Sequence[str]) -> dict[str, Any]:
    """Return row values limited to automatic Scenario columns."""

    allowed = set(automatic_scenario_columns(headers))
    return {key: value for key, value in row.items() if key in allowed}


def scenario_row_coordinate(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Return a safe row coordinate for an existing Scenario row."""

    if index < 0:
        raise ScenarioSyncError(
            "SCENARIO_SYNC_SNAPSHOT_INVALID",
            "Scenarios",
            "ROW_COORDINATE_MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return {
        "tab": SCENARIOS_TAB,
        "data_index": index,
        "row_number": snapshot_row_number(row, index),
    }


def scenario_append_coordinate(snapshot_row_count: int) -> dict[str, Any]:
    """Return a safe append coordinate guarded by snapshot row count."""

    return {
        "tab": SCENARIOS_TAB,
        "mode": "append",
        "expected_after_data_rows": snapshot_row_count,
    }


def snapshot_row_number(row: Mapping[str, Any], index: int) -> int:
    """Return a 1-based physical row number for a data row."""

    value = row.get(ROW_NUMBER_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) >= 2:
        return int(value)
    return index + 2


def expected_scenario_old_values(
    existing_row: Mapping[str, Any],
    changed: Mapping[str, Any],
) -> dict[str, str]:
    """Return old Scenario values that must match before update."""

    expected = {
        "scenario_id": render_cell_value(existing_row.get("scenario_id", "")),
        "source_fingerprint": render_cell_value(existing_row.get("source_fingerprint", "")),
    }
    for field in sorted(changed):
        expected[field] = render_cell_value(existing_row.get(field, ""))
    return expected


def inventory_mapping_operation(
    row: Mapping[str, Any],
    scenario_id: str,
    headers: Sequence[str],
    rows_by_id: Mapping[str, tuple[int, Mapping[str, Any]]],
    snapshot_fingerprint: str,
) -> dict[str, Any] | None:
    """Return an Inventory mapping update for a Scenario, if needed."""

    if not headers:
        return None
    inventory_id = row_value(row, "inventory_id")
    if not inventory_id:
        return None
    indexed = rows_by_id.get(inventory_id)
    if indexed is None:
        return None
    snapshot_index, existing = indexed
    mapped_ids = parse_id_list(existing.get("mapped_scenario_ids", ""))
    if scenario_id not in mapped_ids:
        mapped_ids = (*mapped_ids, scenario_id)
    fields: dict[str, str] = {}
    mapped_value = ",".join(mapped_ids)
    if render_cell_value(existing.get("mapped_scenario_ids", "")) != mapped_value:
        fields["mapped_scenario_ids"] = mapped_value
    try:
        status = normalize_inventory_status(row_value(existing, "discovery_status", "status"))
    except Exception:
        status = InventoryStatus.BLOCKED
    if status in {InventoryStatus.NEW, InventoryStatus.DISCOVERED}:
        fields["discovery_status"] = InventoryStatus.MAPPED.value
    if not fields:
        return None
    payload = {"inventory_id": inventory_id, "scenario_id": scenario_id, "fields": fields}
    return {
        "operation_id": scenario_operation_id("UPDATE_INVENTORY_MAPPING", inventory_id, payload),
        "operation": "UPDATE_INVENTORY_MAPPING",
        "target_tab": INVENTORY_TAB,
        "inventory_id": inventory_id,
        "scenario_id": scenario_id,
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": inventory_row_coordinate(existing, snapshot_index),
        "expected_old_values": expected_inventory_mapping_old_values(existing, fields),
        "fields": fields,
        "reason": "SCENARIO_MAPPED",
    }


def expected_inventory_mapping_old_values(
    existing: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> dict[str, str]:
    """Return old Inventory values that must match before mapping update."""

    expected = {
        "inventory_id": render_cell_value(row_value(existing, "inventory_id")),
        "source_fingerprint": render_cell_value(stable_source_fingerprint(existing)),
    }
    for field in sorted(fields):
        expected[field] = render_cell_value(existing.get(field, ""))
    return expected


def parse_id_list(value: Any) -> tuple[str, ...]:
    """Parse a comma/list cell into a deterministic tuple preserving order."""

    if isinstance(value, Sequence) and not isinstance(value, str):
        items = [str(item).strip() for item in value]
    else:
        items = str(value or "").split(",")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def scenario_snapshot_fingerprint(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Fingerprint Scenario rows and canonical headers."""

    payload = {
        "headers": list(headers),
        "rows": [
            {
                "row_number": row.get(ROW_NUMBER_KEY, index + 2),
                "values": {
                    header: render_cell_value(row.get(header, ""))
                    for header in headers
                    if header in row
                },
            }
            for index, row in enumerate(rows)
        ],
    }
    return fingerprint_payload(payload)


def scenario_operation_id(operation: str, scenario_id: str, payload: Mapping[str, Any]) -> str:
    """Return deterministic Scenario operation ID."""

    return (
        "SCENARIO-"
        + fingerprint_payload(
            {"operation": operation, "scenario_id": scenario_id, "payload": payload}
        )[7:23].upper()
    )


def extract_snapshot_scenarios(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract Scenarios rows from snapshot JSON."""

    tabs = payload.get("tabs")
    if isinstance(tabs, Mapping) and isinstance(tabs.get(SCENARIOS_TAB), list):
        rows = tabs[SCENARIOS_TAB]
    else:
        rows = payload.get(SCENARIOS_TAB, [])
    if not isinstance(rows, list):
        raise ScenarioSyncError(
            "SCENARIO_SYNC_JSON_INVALID",
            "snapshot_json.Scenarios",
            "LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def scenario_headers_from_schema(schema_path: Path) -> tuple[str, ...]:
    """Return canonical Scenarios headers from Sheets schema."""

    schema = read_json_file(schema_path, "schema")
    tabs = schema.get("tabs")
    if not isinstance(tabs, list):
        raise ScenarioSyncError("SCENARIO_SYNC_SCHEMA_INVALID", "schema.tabs", "LIST_REQUIRED")
    for tab in tabs:
        if isinstance(tab, Mapping) and tab.get("name") == SCENARIOS_TAB:
            columns = tab.get("columns")
            if not isinstance(columns, list):
                break
            headers = tuple(
                str(column[0]) for column in columns if isinstance(column, list) and column
            )
            if not headers:
                break
            return headers
    raise ScenarioSyncError("SCENARIO_SYNC_SCHEMA_INVALID", "schema.Scenarios", "TAB_MISSING")


def inventory_sort_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """Return deterministic sort key for Inventory candidates."""

    return (
        row_value(row, "feature_area", "feature_name", "name"),
        stable_source_fingerprint(row),
        row_value(row, "inventory_id"),
    )


def max_id(scenarios: Sequence[ScenarioContract], field: str) -> int:
    """Return max numeric ID suffix for existing Scenarios."""

    max_value = 0
    for scenario in scenarios:
        value = getattr(scenario, field)
        try:
            max_value = max(max_value, int(str(value).rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return max_value


def make_id(prefix: str, number: int) -> str:
    """Return zero-padded stable ID."""

    return f"{prefix}-{number:0{ID_WIDTH}d}"


def story_key(feature_name: str, story_title: str) -> str:
    """Return stable story grouping key."""

    return fingerprint_payload({"feature_name": feature_name, "story_title": story_title})


def manual_override_enabled(row: Mapping[str, Any]) -> bool:
    """Return whether a row has manual override enabled."""

    value = row.get("manual_override", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def manual_override_by_id(rows: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
    """Return manual override flags keyed by scenario id."""

    return {
        row_value(row, "scenario_id"): manual_override_enabled(row)
        for row in rows
        if row_value(row, "scenario_id")
    }


def apply_scenario_sync_plan(
    plan: Mapping[str, Any],
    fresh_snapshot_payload: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    backend: SheetsBackend,
    wal: AppendOnlyWal,
) -> ScenarioApplyResult:
    """Apply a conflict-free Scenario sync plan using fake/unit backend contracts."""

    operations = validate_applicable_plan(plan)
    fresh_scenarios = extract_snapshot_scenarios(fresh_snapshot_payload)
    fresh_inventory = extract_snapshot_inventory(fresh_snapshot_payload)
    ensure_snapshot_fingerprints(
        plan,
        scenario_headers=scenario_headers,
        inventory_headers=inventory_headers,
        fresh_scenarios=fresh_scenarios,
        fresh_inventory=fresh_inventory,
    )
    ensure_backend_snapshot_fingerprints(
        plan,
        scenario_headers=scenario_headers,
        inventory_headers=inventory_headers,
        backend=backend,
    )
    mutations = scenario_apply_mutations(
        plan,
        operations,
        fresh_scenarios=fresh_scenarios,
        fresh_inventory=fresh_inventory,
        scenario_headers=scenario_headers,
        inventory_headers=inventory_headers,
    )
    validate_batch(mutations)
    if not mutations:
        return ScenarioApplyResult(
            outcome="NOOP",
            applied_mutation_count=0,
            wal_pending_written=False,
            wal_acknowledged=False,
            read_back_verified=True,
            operation_count=0,
        )
    pending_payload = scenario_apply_wal_payload(plan, operations)
    pending = append_scenario_pending(wal, pending_payload)
    try:
        backend.apply_batch(mutations)
    except SheetsBackendError as exc:
        if not exc.may_have_applied:
            raise ScenarioSyncError(
                "SCENARIO_APPLY_BACKEND_FAILED",
                "backend",
                exc.code,
                exit_code=exc.exit_code,
            ) from None
        if verify_scenario_read_back(backend, operations):
            append_scenario_ack(wal, pending.operation_id, pending_payload)
            return ScenarioApplyResult(
                outcome="APPLIED_AFTER_AMBIGUOUS",
                applied_mutation_count=len(mutations),
                wal_pending_written=True,
                wal_acknowledged=True,
                read_back_verified=True,
                operation_count=len(operations),
            )
        raise ScenarioSyncError(
            "SCENARIO_APPLY_OUTCOME_UNKNOWN",
            "backend",
            "READ_BACK_UNPROVEN",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if not verify_scenario_read_back(backend, operations):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_READ_BACK_MISMATCH",
            "scenario_apply",
            "POSTCONDITION_FAILED",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    append_scenario_ack(wal, pending.operation_id, pending_payload)
    return ScenarioApplyResult(
        outcome="APPLIED",
        applied_mutation_count=len(mutations),
        wal_pending_written=True,
        wal_acknowledged=True,
        read_back_verified=True,
        operation_count=len(operations),
    )


def reconcile_scenario_apply_plan(
    plan: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    backend: SheetsBackend,
    wal: AppendOnlyWal,
) -> ScenarioReconcileResult:
    """Evaluate pending Scenario apply WAL entries without resending mutations."""

    operations = validate_applicable_plan(plan)
    pending_entries = [
        entry
        for entry in wal.replay_plan()
        if entry.operation == SCENARIO_APPLY_OPERATION
        and entry.payload.get("plan_fingerprint") == fingerprint_payload(plan)
    ]
    if not pending_entries:
        return ScenarioReconcileResult(
            outcome="NO_PENDING",
            pending_count=0,
            wal_acknowledged=False,
            read_back_verified=False,
        )
    pending = pending_entries[0]
    payload = scenario_apply_wal_payload(plan, operations)
    if verify_scenario_read_back(backend, operations):
        append_scenario_ack(wal, pending.operation_id, payload)
        return ScenarioReconcileResult(
            outcome="ALREADY_APPLIED",
            pending_count=len(pending_entries),
            wal_acknowledged=True,
            read_back_verified=True,
        )
    try:
        state = backend.read_spreadsheet()
    except SheetsBackendError as exc:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_BACKEND_FAILED",
            "backend",
            exc.code,
            exit_code=exc.exit_code,
        ) from None
    rows_by_tab = state.rows_dict()
    scenario_rows = rows_by_tab.get(SCENARIOS_TAB, [])
    inventory_rows = rows_by_tab.get(INVENTORY_TAB, [])
    source_matches = scenario_plan_snapshot_fingerprint(plan) == scenario_snapshot_fingerprint(
        scenario_headers,
        scenario_rows,
    )
    if operations_touch_inventory(plan):
        source_matches = source_matches and inventory_plan_snapshot_fingerprint(
            plan
        ) == inventory_apply_fingerprint(inventory_headers, inventory_rows)
    if source_matches:
        return ScenarioReconcileResult(
            outcome="RETRY_REQUIRED",
            pending_count=len(pending_entries),
            wal_acknowledged=False,
            read_back_verified=False,
        )
    raise ScenarioSyncError(
        "SCENARIO_APPLY_RECONCILE_CONFLICT",
        "scenario_apply",
        "STATE_CONFLICT",
        exit_code=EXIT_LOCK_CONFLICT,
    )


def validate_applicable_plan(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return operations if a Scenario plan is safe to apply."""

    if plan.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "plan_schema_version",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    conflicts = plan.get("conflicts", [])
    if isinstance(conflicts, Sequence) and not isinstance(conflicts, str) and len(conflicts) > 0:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_CONFLICT",
            "conflicts",
            "REVIEW_REQUIRED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "operations",
            "LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    safe_operations: list[Mapping[str, Any]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise ScenarioSyncError(
                "SCENARIO_APPLY_PLAN_INVALID",
                f"operations.[{index}]",
                "OBJECT_REQUIRED",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        safe_operations.append(operation)
    return safe_operations


def ensure_snapshot_fingerprints(
    plan: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    fresh_scenarios: Sequence[Mapping[str, Any]],
    fresh_inventory: Sequence[Mapping[str, Any]],
) -> None:
    """Ensure fresh snapshot rows still match the Scenario plan."""

    if scenario_plan_snapshot_fingerprint(plan) != scenario_snapshot_fingerprint(
        scenario_headers,
        fresh_scenarios,
    ):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_STALE_SNAPSHOT",
            "Scenarios",
            "FINGERPRINT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    if operations_touch_inventory(plan) and inventory_plan_snapshot_fingerprint(
        plan
    ) != inventory_apply_fingerprint(inventory_headers, fresh_inventory):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_STALE_SNAPSHOT",
            "Inventory",
            "FINGERPRINT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )


def ensure_backend_snapshot_fingerprints(
    plan: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    backend: SheetsBackend,
) -> None:
    """Ensure backend rows still match the fresh snapshot before WAL/write."""

    try:
        state = backend.read_spreadsheet()
    except SheetsBackendError as exc:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_BACKEND_FAILED",
            "backend",
            exc.code,
            exit_code=exc.exit_code,
        ) from None
    rows_by_tab = state.rows_dict()
    scenario_rows = rows_by_tab.get(SCENARIOS_TAB)
    if scenario_rows is None:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_BACKEND_STATE_INVALID",
            SCENARIOS_TAB,
            "TAB_MISSING",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    if scenario_plan_snapshot_fingerprint(plan) != scenario_snapshot_fingerprint(
        scenario_headers,
        scenario_rows,
    ):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_STALE_SNAPSHOT",
            "backend.Scenarios",
            "FINGERPRINT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    if operations_touch_inventory(plan):
        inventory_rows = rows_by_tab.get(INVENTORY_TAB)
        if inventory_rows is None:
            raise ScenarioSyncError(
                "SCENARIO_APPLY_BACKEND_STATE_INVALID",
                INVENTORY_TAB,
                "TAB_MISSING",
                exit_code=EXIT_LOCK_CONFLICT,
            )
        if inventory_plan_snapshot_fingerprint(plan) != inventory_apply_fingerprint(
            inventory_headers,
            inventory_rows,
        ):
            raise ScenarioSyncError(
                "SCENARIO_APPLY_STALE_SNAPSHOT",
                "backend.Inventory",
                "FINGERPRINT_MISMATCH",
                exit_code=EXIT_LOCK_CONFLICT,
            )


def scenario_apply_mutations(
    plan: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    *,
    fresh_scenarios: Sequence[Mapping[str, Any]],
    fresh_inventory: Sequence[Mapping[str, Any]],
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
) -> list[AppendRows | UpdateRowValues]:
    """Translate Scenario sync operations to typed row mutations."""

    mutations: list[AppendRows | UpdateRowValues] = []
    scenario_fingerprint = scenario_plan_snapshot_fingerprint(plan)
    inventory_fingerprint = inventory_plan_snapshot_fingerprint(plan)
    for index, operation in enumerate(operations):
        kind = operation.get("operation")
        if operation.get("target_tab") == SCENARIOS_TAB:
            if operation.get("expected_snapshot_fingerprint") != scenario_fingerprint:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].expected_snapshot_fingerprint",
                    "MISMATCH",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            if kind == "APPEND_SCENARIO":
                ensure_scenario_append_absent(operation, fresh_scenarios, index)
                row = operation.get("row")
                if not isinstance(row, Mapping):
                    raise ScenarioSyncError(
                        "SCENARIO_APPLY_PLAN_INVALID",
                        f"operations.[{index}].row",
                        "OBJECT_REQUIRED",
                        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                    )
                mutations.append(
                    AppendRows(
                        tab_name=SCENARIOS_TAB,
                        rows=(scenario_row_values(row, scenario_headers),),
                    )
                )
            elif kind in {"UPDATE_SCENARIO_FIELDS", "MARK_SCENARIO_RETIRED"}:
                data_index = operation_data_index(operation, index)
                ensure_expected_values(operation, fresh_scenarios, data_index, index)
                fields = operation.get("fields")
                expected = operation.get("expected_old_values")
                if not isinstance(fields, Mapping) or not fields:
                    raise ScenarioSyncError(
                        "SCENARIO_APPLY_PLAN_INVALID",
                        f"operations.[{index}].fields",
                        "OBJECT_REQUIRED",
                        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                    )
                if not isinstance(expected, Mapping) or not expected:
                    raise ScenarioSyncError(
                        "SCENARIO_APPLY_PLAN_INVALID",
                        f"operations.[{index}].expected_old_values",
                        "OBJECT_REQUIRED",
                        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                    )
                mutations.append(
                    UpdateRowValues(
                        tab_name=SCENARIOS_TAB,
                        row_index=data_index,
                        values=scenario_row_values(fields, scenario_headers, allow_subset=True),
                        expected_values=scenario_row_values(
                            expected,
                            scenario_headers,
                            allow_subset=True,
                            allow_manual=False,
                        ),
                    )
                )
            else:
                raise unsupported_operation(index)
        elif operation.get("target_tab") == INVENTORY_TAB and kind == "UPDATE_INVENTORY_MAPPING":
            if operation.get("expected_snapshot_fingerprint") != inventory_fingerprint:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].expected_snapshot_fingerprint",
                    "MISMATCH",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            data_index = operation_data_index(operation, index)
            ensure_expected_values(operation, fresh_inventory, data_index, index)
            fields = operation.get("fields")
            expected = operation.get("expected_old_values")
            if not isinstance(fields, Mapping) or not fields:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].fields",
                    "OBJECT_REQUIRED",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            if set(str(key) for key in fields) - {"mapped_scenario_ids", "discovery_status"}:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].fields",
                    "UNSUPPORTED_FIELD",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            if not isinstance(expected, Mapping) or not expected:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].expected_old_values",
                    "OBJECT_REQUIRED",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            try:
                values = inventory_row_values(fields, inventory_headers, allow_subset=True)
                expected_values = inventory_row_values(
                    expected,
                    inventory_headers,
                    allow_subset=True,
                )
            except Exception:
                raise ScenarioSyncError(
                    "SCENARIO_APPLY_PLAN_INVALID",
                    f"operations.[{index}].fields",
                    "INVALID_INVENTORY_VALUES",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                ) from None
            mutations.append(
                UpdateRowValues(
                    tab_name=INVENTORY_TAB,
                    row_index=data_index,
                    values=values,
                    expected_values=expected_values,
                )
            )
        else:
            raise unsupported_operation(index)
    return mutations


def unsupported_operation(index: int) -> ScenarioSyncError:
    """Return a safe unsupported-operation error."""

    return ScenarioSyncError(
        "SCENARIO_APPLY_PLAN_INVALID",
        f"operations.[{index}].operation",
        "UNSUPPORTED_OPERATION",
        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
    )


def ensure_scenario_append_absent(
    operation: Mapping[str, Any],
    fresh_rows: Sequence[Mapping[str, Any]],
    index: int,
) -> None:
    """Ensure a Scenario append still targets an absent identity."""

    coordinate = operation.get("row_coordinate")
    if not isinstance(coordinate, Mapping) or coordinate.get("mode") != "append":
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate",
            "APPEND_COORDINATE_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    expected_count = coordinate.get("expected_after_data_rows")
    if not isinstance(expected_count, int) or expected_count != len(fresh_rows):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_STALE_SNAPSHOT",
            f"operations.[{index}].row_coordinate",
            "ROW_COUNT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    expected_absent = operation.get("expected_absent")
    if not isinstance(expected_absent, Mapping):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            f"operations.[{index}].expected_absent",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    scenario_id = render_cell_value(expected_absent.get("scenario_id", ""))
    source_fingerprint = render_cell_value(expected_absent.get("source_fingerprint", ""))
    for row in fresh_rows:
        if scenario_id and row_value(row, "scenario_id") == scenario_id:
            raise ScenarioSyncError(
                "SCENARIO_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].scenario_id",
                "ALREADY_EXISTS",
                exit_code=EXIT_LOCK_CONFLICT,
            )
        if source_fingerprint and row_value(row, "source_fingerprint") == source_fingerprint:
            raise ScenarioSyncError(
                "SCENARIO_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].source_fingerprint",
                "ALREADY_EXISTS",
                exit_code=EXIT_LOCK_CONFLICT,
            )


def operation_data_index(operation: Mapping[str, Any], index: int) -> int:
    """Extract a zero-based data row index from an operation coordinate."""

    coordinate = operation.get("row_coordinate")
    if not isinstance(coordinate, Mapping):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    data_index = coordinate.get("data_index")
    if not isinstance(data_index, int) or isinstance(data_index, bool) or data_index < 0:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate.data_index",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return data_index


def ensure_expected_values(
    operation: Mapping[str, Any],
    fresh_rows: Sequence[Mapping[str, Any]],
    data_index: int,
    index: int,
) -> None:
    """Ensure expected old values still match a fresh snapshot row."""

    if data_index >= len(fresh_rows):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_STALE_SNAPSHOT",
            f"operations.[{index}].row_coordinate",
            "ROW_MISSING",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    expected = operation.get("expected_old_values")
    if not isinstance(expected, Mapping) or not expected:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            f"operations.[{index}].expected_old_values",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    row = fresh_rows[data_index]
    for column, expected_value in expected.items():
        if render_cell_value(row.get(str(column), "")) != render_cell_value(expected_value):
            raise ScenarioSyncError(
                "SCENARIO_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].expected_old_values.{safe_text(str(column))}",
                "VALUE_MISMATCH",
                exit_code=EXIT_LOCK_CONFLICT,
            )


def scenario_row_values(
    values: Mapping[str, Any],
    headers: Sequence[str],
    *,
    allow_subset: bool = False,
    allow_manual: bool = False,
) -> tuple[tuple[str, str], ...]:
    """Return safe Scenario column/value pairs in canonical header order."""

    allowed = set(headers)
    unknown = sorted(str(key) for key in values if str(key) not in allowed)
    if unknown:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "row",
            "UNKNOWN_COLUMN",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not allow_manual and any(str(key) in SCENARIO_MANUAL_COLUMNS for key in values):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "row",
            "MANUAL_COLUMN_FORBIDDEN",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return tuple(
        (header, render_cell_value(values.get(header, "")))
        for header in headers
        if header in values
    )


def scenario_plan_snapshot_fingerprint(plan: Mapping[str, Any]) -> str:
    """Return the expected Scenario snapshot fingerprint from a plan."""

    source = plan_source(plan)
    fingerprint = source.get("scenario_snapshot_fingerprint") or source.get("snapshot_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "source.scenario_snapshot_fingerprint",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return fingerprint


def inventory_plan_snapshot_fingerprint(plan: Mapping[str, Any]) -> str:
    """Return the expected Inventory snapshot fingerprint from a plan."""

    source = plan_source(plan)
    fingerprint = source.get("inventory_snapshot_fingerprint")
    if not isinstance(fingerprint, str):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "source.inventory_snapshot_fingerprint",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return fingerprint


def plan_source(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a validated source object from a plan."""

    source = plan.get("source")
    if not isinstance(source, Mapping):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PLAN_INVALID",
            "source",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return source


def operations_touch_inventory(plan: Mapping[str, Any]) -> bool:
    """Return whether plan operations include Inventory mapping updates."""

    operations = plan.get("operations", [])
    return (
        isinstance(operations, Sequence)
        and not isinstance(operations, str)
        and any(
            isinstance(operation, Mapping) and operation.get("target_tab") == INVENTORY_TAB
            for operation in operations
        )
    )


def scenario_apply_wal_payload(
    plan: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a redaction-safe WAL payload for Scenario apply."""

    operation_ids = []
    target_tabs: set[str] = set()
    for index, operation in enumerate(operations):
        operation_id_value = operation.get("operation_id")
        if not isinstance(operation_id_value, str) or operation_id_value == "":
            raise ScenarioSyncError(
                "SCENARIO_APPLY_PLAN_INVALID",
                f"operations.[{index}].operation_id",
                "INVALID",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        operation_ids.append(operation_id_value)
        target_tab = operation.get("target_tab")
        if isinstance(target_tab, str):
            target_tabs.add(target_tab)
    payload = {
        "target_tabs": sorted(target_tabs),
        "plan_fingerprint": fingerprint_payload(plan),
        "scenario_snapshot_fingerprint": scenario_plan_snapshot_fingerprint(plan),
        "inventory_snapshot_fingerprint": inventory_plan_snapshot_fingerprint(plan),
        "operation_count": len(operations),
        "operation_ids": operation_ids,
    }
    if secret_findings(payload):
        raise ScenarioSyncError(
            "SCENARIO_APPLY_PAYLOAD_UNSAFE",
            "wal.payload",
            "SECRET_DETECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    return payload


def append_scenario_pending(wal: AppendOnlyWal, payload: dict[str, Any]) -> Any:
    """Append Scenario apply pending WAL entry safely."""

    try:
        return wal.append_pending(
            SCENARIO_APPLY_OPERATION,
            payload,
            operation_id="SCN-APPLY-" + fingerprint_payload(payload)[7:23].upper(),
        )
    except WalError as exc:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_WAL_FAILED",
            "wal",
            exc.code,
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None


def append_scenario_ack(
    wal: AppendOnlyWal,
    operation_id_value: str,
    payload: Mapping[str, Any],
) -> None:
    """Append Scenario apply ack WAL entry safely."""

    try:
        wal.append_ack(
            operation_id_value,
            {
                "acknowledged_operation_id": operation_id_value,
                "plan_fingerprint": payload["plan_fingerprint"],
                "read_back_verified": True,
            },
        )
    except WalError as exc:
        raise ScenarioSyncError(
            "SCENARIO_APPLY_WAL_FAILED",
            "wal",
            exc.code,
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None


def verify_scenario_read_back(
    backend: SheetsBackend,
    operations: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether backend rows satisfy all Scenario apply postconditions."""

    try:
        state = backend.read_spreadsheet()
    except SheetsBackendError:
        return False
    rows_by_tab = state.rows_dict()
    for operation in operations:
        tab = operation.get("target_tab")
        kind = operation.get("operation")
        rows = rows_by_tab.get(str(tab), [])
        if kind == "APPEND_SCENARIO":
            row = operation.get("row")
            if not isinstance(row, Mapping):
                return False
            if not any(row_contains_values(candidate, row) for candidate in rows):
                return False
        elif kind in {
            "UPDATE_SCENARIO_FIELDS",
            "MARK_SCENARIO_RETIRED",
            "UPDATE_INVENTORY_MAPPING",
        }:
            coordinate = operation.get("row_coordinate")
            fields = operation.get("fields")
            if not isinstance(coordinate, Mapping) or not isinstance(fields, Mapping):
                return False
            data_index = coordinate.get("data_index")
            if not isinstance(data_index, int) or data_index < 0 or data_index >= len(rows):
                return False
            if not row_contains_values(rows[data_index], fields):
                return False
        else:
            return False
    return True


def row_contains_values(row: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Return whether a row contains expected cell values."""

    for column, expected_value in expected.items():
        if render_cell_value(row.get(str(column), "")) != render_cell_value(expected_value):
            return False
    return True


def secret_value_findings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    """Detect secret-looking values while ignoring safe plan key names."""

    if isinstance(value, Mapping):
        findings: list[tuple[str, str]] = []
        for child in value.values():
            findings.extend(secret_value_findings(child, path))
        return findings
    if isinstance(value, list):
        findings = []
        for index, child in enumerate(value):
            findings.extend(secret_value_findings(child, f"{path}.[{index}]"))
        return findings
    if isinstance(value, str):
        return secret_findings(value, path)
    return []


def validate_input_file(path: Path, label: str) -> None:
    """Validate one input file."""

    if not path.exists():
        raise ScenarioSyncError(
            "SCENARIO_SYNC_INPUT_INVALID",
            label,
            "MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not path.is_file():
        raise ScenarioSyncError(
            "SCENARIO_SYNC_INPUT_INVALID",
            label,
            "NOT_FILE",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )


def read_json_file(path: Path, label: str) -> Mapping[str, Any]:
    """Read a JSON object safely."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ScenarioSyncError(
            "SCENARIO_SYNC_JSON_INVALID",
            label,
            "JSON_INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    except OSError:
        raise ScenarioSyncError(
            "SCENARIO_SYNC_READ_FAILED",
            label,
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if not isinstance(data, Mapping):
        raise ScenarioSyncError(
            "SCENARIO_SYNC_JSON_INVALID",
            label,
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return data


def validate_output_path(path: Path, *, force: bool, protected_paths: Sequence[Path]) -> None:
    """Validate output path."""

    try:
        stat_result = path.lstat()
    except OSError:
        stat_result = None
    if stat_result is not None:
        if stat.S_ISLNK(stat_result.st_mode):
            raise ScenarioSyncError("SCENARIO_SYNC_OUTPUT_UNSAFE", "output", "SYMLINK_REJECTED")
        if not stat.S_ISREG(stat_result.st_mode):
            raise ScenarioSyncError("SCENARIO_SYNC_OUTPUT_UNSAFE", "output", "NOT_REGULAR_FILE")
        if not force:
            raise ScenarioSyncError("SCENARIO_SYNC_OUTPUT_EXISTS", "output", "EXISTS")
    output_resolved = path.resolve(strict=False)
    for protected in protected_paths:
        if output_resolved == protected.resolve(strict=False):
            raise ScenarioSyncError("SCENARIO_SYNC_OUTPUT_UNSAFE", "output", "INPUT_PATH_REJECTED")


def atomic_write_plan(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write plan JSON."""

    rendered = dumps_snapshot(payload)
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        fsync_dir(path.parent)
    except OSError:
        raise ScenarioSyncError(
            "SCENARIO_SYNC_WRITE_FAILED",
            "output",
            "WRITE_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def fsync_dir(directory: Path) -> None:
    """Fsync directory where supported."""

    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def absolute_no_resolve(path: Path) -> Path:
    """Return absolute path without following final symlink."""

    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded
