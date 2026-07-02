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
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.inventory_identity import fingerprint_payload, stable_source_fingerprint
from webapp_debug_skill.inventory_model import dumps_snapshot, rfc3339_utc, safe_text
from webapp_debug_skill.inventory_sync import (
    ROW_NUMBER_KEY,
    extract_discovery_rows,
    extract_snapshot_inventory,
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
from webapp_debug_skill.status_model import InventoryStatus, normalize_inventory_status

PLAN_SCHEMA_VERSION = 1
DEFAULT_MAX_OPERATIONS = 10_000
SCENARIOS_TAB = "Scenarios"
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
    plan = build_scenario_sync_plan(
        discovery_payload,
        snapshot_payload,
        scenario_headers=scenario_headers,
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
    generated_at: str,
    allow_retire_missing: bool = False,
) -> dict[str, Any]:
    """Build a deterministic local Scenario sync plan."""

    discovery_rows, discovery_gaps = extract_discovery_rows(discovery_payload)
    inventory_rows = extract_snapshot_inventory(snapshot_payload)
    if not inventory_rows:
        inventory_rows = discovery_rows
    scenario_rows = extract_snapshot_scenarios(snapshot_payload)
    conflicts: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    noop_count = 0
    scenario_headers = tuple(scenario_headers)
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
                )
            except ScenarioModelError as exc:
                conflicts.append(scenario_model_conflict(row, exc))
                continue
            if update is None:
                noop_count += 1
            else:
                operations.append(update)
            continue
        try:
            scenario = new_scenario(row, id_state)
        except ScenarioModelError as exc:
            conflicts.append(scenario_model_conflict(row, exc))
            continue
        operations.append(append_operation(scenario, scenario_headers))

    if allow_retire_missing:
        for scenario in sorted(existing, key=lambda item: item.scenario_id):
            if scenario.scenario_id in matched_scenarios:
                continue
            if scenario.lifecycle_status != ScenarioLifecycleStatus.ACTIVE:
                continue
            if set(scenario.inventory_ids) & inventory_ids:
                continue
            operations.append(retire_operation(scenario))

    summary = {
        "append_count": sum(1 for item in operations if item["operation"] == "APPEND_SCENARIO"),
        "update_count": sum(
            1 for item in operations if item["operation"] == "UPDATE_SCENARIO_FIELDS"
        ),
        "retire_count": sum(
            1 for item in operations if item["operation"] == "MARK_SCENARIO_RETIRED"
        ),
        "conflict_count": len(conflicts),
        "noop_count": noop_count,
        "operation_count": len(operations),
    }
    plan = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "source": {
            "kind": "scenario_sync",
            "snapshot_fingerprint": scenario_snapshot_fingerprint(scenario_headers, scenario_rows),
            "discovery_fingerprint": fingerprint_payload(discovery_payload),
            "generated_at": generated_at,
        },
        "summary": summary,
        "operations": operations,
        "conflicts": conflicts,
        "warnings": [],
    }
    if secret_findings(plan):
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
        "expected_old_values": {key: existing_row.get(key, "") for key in changed},
        "fields": changed,
        "reason": "INVENTORY_CHANGED",
    }


def append_operation(scenario: ScenarioContract, headers: Sequence[str]) -> dict[str, Any]:
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
        "row": row,
        "reason": "NEW_SCENARIO",
    }


def retire_operation(scenario: ScenarioContract) -> dict[str, Any]:
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
        "expected_old_values": {
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
                "values": {header: row.get(header, "") for header in headers if header in row},
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
