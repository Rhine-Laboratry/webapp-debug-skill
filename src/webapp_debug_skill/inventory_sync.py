"""Local Inventory sync plan generation."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections import Counter
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
from webapp_debug_skill.inventory_identity import (
    duplicate_values,
    fingerprint_payload,
    match_existing_rows,
    operation_id,
    row_actor,
    row_feature_name,
    row_route,
    row_status,
    row_value,
    stable_source_fingerprint,
)
from webapp_debug_skill.inventory_model import dumps_snapshot, rfc3339_utc, safe_text
from webapp_debug_skill.redaction import (
    redact_inline_text,
    redact_secret_value,
    secret_findings,
    secret_type_for_key,
)
from webapp_debug_skill.risk import RiskError, normalize_risk_value
from webapp_debug_skill.sheets_init import load_canonical_schema
from webapp_debug_skill.sheets_client import (
    AppendRows,
    SheetsBackend,
    SheetsBackendError,
    UpdateRowValues,
    validate_batch,
)
from webapp_debug_skill.status_model import (
    InventoryStatus,
    StatusModelError,
    normalize_inventory_status,
)
from webapp_debug_skill.wal import AppendOnlyWal, WalError

PLAN_SCHEMA_VERSION = 1
DEFAULT_MAX_OPERATIONS = 10_000
REQUIRED_INVENTORY_COLUMNS = (
    "inventory_id",
    "feature_area",
    "name",
    "source_fingerprint",
    "discovery_status",
    "risk",
)
AUTO_UPDATE_COLUMNS = {
    "feature_area",
    "item_type",
    "name",
    "actor_roles",
    "route_or_trigger",
    "source_path",
    "source_symbol",
    "source_lines",
    "source_fingerprint",
    "test_scope",
    "recommended_test_type",
    "discovery_status",
    "exclusion_reason",
    "reachability",
    "risk",
    "mapped_scenario_ids",
    "discovered_at",
    "last_seen_commit",
    "last_seen_at",
}
HUMAN_EDITABLE_COLUMNS = {
    "review_status",
    "notes",
    "manual_override",
    "manual_expected_behavior",
    "manual_exclusion_reason",
    "manual_priority",
    "issue_url",
}
LOCKED_STATUSES = {
    InventoryStatus.MAPPED,
    InventoryStatus.EXCLUDED_WITH_REASON,
    InventoryStatus.RETIRED,
    InventoryStatus.MERGED,
}
RETIRE_EXCLUDED_STATUSES = {
    InventoryStatus.EXCLUDED_WITH_REASON,
    InventoryStatus.RETIRED,
    InventoryStatus.MERGED,
}
DISCOVERY_MANAGED_SOURCES = ("cakephp_", "cakephp")
ROW_NUMBER_KEY = "_row_number"
INVENTORY_TAB = "Inventory"
INVENTORY_APPLY_OPERATION = "inventory.apply"


class InventorySyncError(RuntimeError):
    """Safe Inventory sync planning error."""

    def __init__(
        self,
        code: str,
        path: str = "inventory_sync",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_POLICY_BLOCKED,
    ) -> None:
        safe_code = "INVENTORY_SYNC_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "inventory_sync" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code


@dataclass(frozen=True)
class InventorySyncDependencies:
    """Injectable dependencies for CLI tests."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class InventoryApplyResult:
    """Result for fake/unit Inventory apply execution."""

    outcome: str
    applied_mutation_count: int
    wal_pending_written: bool
    wal_acknowledged: bool
    read_back_verified: bool
    operation_count: int


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(description="Plan local Inventory sync operations.")
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
    deps: InventorySyncDependencies | None = None,
) -> int:
    """Run CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or InventorySyncDependencies()
    try:
        result = run(args, dependencies)
    except InventorySyncError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Inventory sync planning failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="INVENTORY_SYNC_UNEXPECTED",
            message="Inventory sync planning failed unexpectedly.",
            details=[Issue("inventory_sync", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    return EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: InventorySyncDependencies) -> CliResult:
    """Run local planning and write plan JSON."""

    discovery_path = args.discovery_json.resolve(strict=False)
    snapshot_path = args.snapshot_json.resolve(strict=False)
    schema_path = args.schema.resolve(strict=False)
    output_path = absolute_no_resolve(args.output)
    if args.max_operations < 1:
        raise InventorySyncError(
            "INVENTORY_SYNC_MAX_OPERATIONS_INVALID",
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
    headers, schema_version = inventory_headers(schema_path)
    plan = build_sync_plan(
        discovery_payload,
        snapshot_payload,
        headers=headers,
        schema_version=schema_version,
        generated_at=rfc3339_utc(deps.clock()),
        allow_retire_missing=args.allow_retire_missing,
    )
    if int(plan["summary"]["operation_count"]) > args.max_operations:
        raise InventorySyncError(
            "INVENTORY_SYNC_TOO_MANY_OPERATIONS",
            "operations",
            "MAX_OPERATIONS_EXCEEDED",
        )
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
            code="INVENTORY_SYNC_CONFLICT",
            message="Inventory sync plan contains conflicts.",
            details=[Issue("conflicts", "REVIEW_REQUIRED")],
            data=data,
        )
    return CliResult(
        ok=True,
        code="INVENTORY_SYNC_OK",
        message="Inventory sync plan generated.",
        details=[],
        data=data,
    )


def build_sync_plan(
    discovery_payload: Mapping[str, Any],
    snapshot_payload: Mapping[str, Any],
    *,
    headers: Sequence[str],
    schema_version: int,
    generated_at: str,
    allow_retire_missing: bool = False,
) -> dict[str, Any]:
    """Build a deterministic local sync plan."""

    discovery_rows, discovery_gaps = extract_discovery_rows(discovery_payload)
    snapshot_rows = extract_snapshot_inventory(snapshot_payload)
    redaction_counts: Counter[str] = Counter()
    discovery_rows = [sanitize_row(row, redaction_counts) for row in discovery_rows]
    snapshot_rows = [sanitize_row(row, redaction_counts) for row in snapshot_rows]
    discovery_gaps = [sanitize_row(gap, redaction_counts) for gap in discovery_gaps]
    snapshot_fingerprint = inventory_apply_fingerprint(headers, snapshot_rows)
    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    noop_count = 0

    unknown_columns = sorted(
        {
            key
            for row in (*discovery_rows, *snapshot_rows)
            for key in row
            if key not in headers
            and key not in alias_columns()
            and not key.startswith(("manual_", "human_"))
        }
    )
    if unknown_columns:
        warnings.append(
            {"code": "INVENTORY_SYNC_UNKNOWN_COLUMNS_IGNORED", "columns": unknown_columns}
        )

    conflicts.extend(duplicate_conflicts(discovery_rows, "discovery"))
    conflicts.extend(duplicate_conflicts(snapshot_rows, "snapshot"))
    conflicts.extend(validate_existing_rows(snapshot_rows))
    conflicts.extend(validate_discovery_rows(discovery_rows))
    conflicts.extend(inventory_id_fingerprint_conflicts(discovery_rows, snapshot_rows))

    matches: dict[int, tuple[int, str]] = {}
    discovery_to_snapshot: dict[int, int] = {}
    for discovery_index, row in enumerate(discovery_rows):
        priority, indexes, match_key = match_existing_rows(row, snapshot_rows)
        if len(indexes) > 1:
            conflicts.append(
                conflict("AMBIGUOUS_MATCH", "discovery", row, match_key, snapshot_indexes=indexes)
            )
            continue
        if len(indexes) == 1:
            snapshot_index = indexes[0]
            matches.setdefault(snapshot_index, (discovery_index, match_key))
            discovery_to_snapshot[discovery_index] = snapshot_index
            if snapshot_index in matches and matches[snapshot_index][0] != discovery_index:
                conflicts.append(
                    conflict(
                        "MULTIPLE_DISCOVERY_MATCH_ONE_ROW",
                        "snapshot",
                        snapshot_rows[snapshot_index],
                        match_key,
                    )
                )
        elif priority is None:
            continue

    conflicted_discoveries = conflict_discovery_indexes(conflicts)
    conflicted_snapshots = conflict_snapshot_indexes(conflicts)
    for discovery_index, row in enumerate(discovery_rows):
        if discovery_index in conflicted_discoveries:
            continue
        snapshot_index = discovery_to_snapshot.get(discovery_index)
        if snapshot_index is None:
            operations.append(
                append_operation(
                    row,
                    headers,
                    snapshot_row_count=len(snapshot_rows),
                    snapshot_fingerprint=snapshot_fingerprint,
                )
            )
            continue
        if snapshot_index in conflicted_snapshots:
            continue
        existing = snapshot_rows[snapshot_index]
        planned, noop = update_or_noop_operation(
            existing,
            row,
            headers,
            snapshot_index=snapshot_index,
            snapshot_fingerprint=snapshot_fingerprint,
        )
        if noop:
            noop_count += 1
        elif isinstance(planned, dict) and "operation" in planned:
            operations.append(planned)
        else:
            conflicts.append(planned)

    matched_snapshots = set(discovery_to_snapshot.values())
    for snapshot_index, row in enumerate(snapshot_rows):
        if snapshot_index in matched_snapshots or snapshot_index in conflicted_snapshots:
            continue
        if allow_retire_missing and can_retire(row):
            operations.append(
                retire_operation(
                    row,
                    snapshot_index=snapshot_index,
                    snapshot_fingerprint=snapshot_fingerprint,
                )
            )
        elif is_managed_source(row):
            warnings.append(
                {
                    "code": "INVENTORY_SYNC_RETIRE_DISABLED",
                    "inventory_id": row_value(row, "inventory_id"),
                }
            )
            noop_count += 1

    for gap in discovery_gaps:
        if not any(same_gap(gap, row) for row in discovery_rows):
            priority, indexes, _match_key = match_existing_rows(gap, snapshot_rows)
            if not indexes and priority is None:
                operations.append(
                    append_gap_operation(
                        gap,
                        headers,
                        snapshot_row_count=len(snapshot_rows),
                        snapshot_fingerprint=snapshot_fingerprint,
                    )
                )

    summary = summarize(operations, conflicts, noop_count, redaction_counts)
    return {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "source": {
            "kind": "inventory_sync_plan",
            "discovery_fingerprint": fingerprint_payload(discovery_payload),
            "snapshot_fingerprint": snapshot_fingerprint,
            "schema_version": schema_version,
            "generated_at": generated_at,
        },
        "summary": summary,
        "operations": operations,
        "conflicts": conflicts,
        "warnings": warnings,
    }


def extract_discovery_rows(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract discovery Inventory rows and top-level gaps."""

    rows = payload.get("Inventory")
    if not isinstance(rows, list):
        raise InventorySyncError(
            "INVENTORY_SYNC_JSON_INVALID",
            "discovery_json.Inventory",
            "LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    inventory = [dict(row) for row in rows if isinstance(row, Mapping)]
    gaps: list[dict[str, Any]] = []
    raw_gaps = payload.get("Discovery Gaps", [])
    if isinstance(raw_gaps, list):
        for gap in raw_gaps:
            if isinstance(gap, Mapping):
                gaps.append(gap_to_row(gap))
    return inventory, gaps


def extract_snapshot_inventory(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract Inventory rows from top-level or tabs.Inventory snapshot shapes."""

    tabs = payload.get("tabs")
    if isinstance(tabs, Mapping) and isinstance(tabs.get("Inventory"), list):
        rows = tabs["Inventory"]
    else:
        rows = payload.get("Inventory")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise InventorySyncError(
            "INVENTORY_SYNC_JSON_INVALID",
            "snapshot_json.Inventory",
            "LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def gap_to_row(gap: Mapping[str, Any]) -> dict[str, Any]:
    """Convert Discovery Gaps entry to an Inventory-like row."""

    reason = row_value(gap, "reason_code", "feature_name", "name")
    reference = row_value(gap, "source_code_reference")
    source_path, source_line = split_reference(reference)
    return {
        "inventory_id": row_value(gap, "inventory_id") or generated_gap_id(gap),
        "source_key": f"gap|{reason}|{reference}",
        "feature_area": "Discovery Gap",
        "feature_name": reason,
        "name": reason,
        "item_type": "UI_PAGE",
        "actor": "unknown",
        "actor_roles": ["unknown"],
        "route_or_url": f"unresolved:{reason}",
        "route_or_trigger": f"unresolved:{reason}",
        "source_code_reference": reference,
        "source_path": source_path,
        "source_lines": source_line,
        "source_symbol": reason,
        "source_fingerprint": stable_source_fingerprint(gap),
        "test_scope": "NOT_TESTABLE_WITH_CURRENT_ACCESS",
        "recommended_test_type": "manual-review",
        "discovery_status": "DISCOVERY_GAP",
        "status": "DISCOVERY_GAP",
        "risk": "MEDIUM",
        "discovery_source": "cakephp_static_gap",
        "confidence": row_value(gap, "confidence") or "LOW",
        "notes": row_value(gap, "summary"),
    }


def generated_gap_id(gap: Mapping[str, Any]) -> str:
    """Return deterministic temporary ID for a gap-like row."""

    return "INV-TEMP-" + fingerprint_payload(gap)[7:23].upper()


def split_reference(reference: str) -> tuple[str, str]:
    """Split source_code_reference into path and line text."""

    if ":" not in reference:
        return reference, ""
    path, line = reference.rsplit(":", 1)
    return path, line


def sanitize_row(row: Mapping[str, Any], counts: Counter[str]) -> dict[str, Any]:
    """Redact all string values in a row without leaking raw content."""

    sanitized: dict[str, Any] = {}
    for key, value in row.items():
        key_text = str(key)
        kind = secret_type_for_key(key_text)
        if kind is not None:
            sanitized[key_text] = redact_secret_value(value, kind)
            counts[kind] += 1
        else:
            sanitized[key_text] = sanitize_value(value, counts)
    return sanitized


def sanitize_value(value: Any, counts: Counter[str]) -> Any:
    """Recursively redact one JSON-compatible value."""

    if isinstance(value, Mapping):
        return sanitize_row(value, counts)
    if isinstance(value, list):
        return [sanitize_value(item, counts) for item in value]
    if isinstance(value, str):
        redacted = redact_inline_text(value, counts, {})
        if secret_findings(redacted):
            counts["SECRET"] += 1
            return "<REDACTED:SECRET>"
        return safe_text(redacted)
    return value


def alias_columns() -> set[str]:
    """Return accepted non-canonical aliases used by local discovery output."""

    return {
        ROW_NUMBER_KEY,
        "source_key",
        "feature_name",
        "actor",
        "route_or_url",
        "http_methods",
        "source_code_reference",
        "discovery_source",
        "confidence",
        "status",
    }


def snapshot_row_number(row: Mapping[str, Any], index: int) -> int:
    """Return the 1-based physical sheet row number for an Inventory data row."""

    value = row.get(ROW_NUMBER_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) >= 2:
        return int(value)
    return index + 2


def row_coordinate(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Return a safe row coordinate for an existing Inventory row."""

    return {
        "tab": INVENTORY_TAB,
        "data_index": index,
        "row_number": snapshot_row_number(row, index),
    }


def append_coordinate(snapshot_row_count: int) -> dict[str, Any]:
    """Return a safe append coordinate guarded by snapshot row count."""

    return {
        "tab": INVENTORY_TAB,
        "mode": "append",
        "expected_after_data_rows": snapshot_row_count,
    }


def inventory_apply_fingerprint(
    headers: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Fingerprint row values with coordinates and canonical Inventory headers."""

    payload = {
        "headers": list(headers),
        "rows": [
            {
                "row_coordinate": row_coordinate(row, index),
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


def duplicate_conflicts(rows: Sequence[Mapping[str, Any]], source: str) -> list[dict[str, Any]]:
    """Return duplicate fingerprint and inventory_id conflicts."""

    conflicts: list[dict[str, Any]] = []
    for field, reason in (
        ("source_fingerprint", "DUPLICATE_SOURCE_FINGERPRINT"),
        ("inventory_id", "DUPLICATE_INVENTORY_ID"),
    ):
        for value, indexes in duplicate_values(rows, field).items():
            conflicts.append(
                {
                    "reason_code": reason,
                    "source": source,
                    "field": field,
                    "match_key": f"{field}:{value}",
                    "row_indexes": indexes,
                }
            )
    return conflicts


def validate_existing_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate snapshot statuses, risks, and required IDs."""

    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not row_value(row, "inventory_id"):
            conflicts.append(conflict("MISSING_INVENTORY_ID", "snapshot", row, f"snapshot:{index}"))
        try:
            normalize_inventory_status(row_status(row), path=f"snapshot[{index}].status")
        except StatusModelError:
            conflicts.append(conflict("INVALID_STATUS", "snapshot", row, f"snapshot:{index}"))
        try:
            normalize_risk_value(row.get("risk"), path=f"snapshot[{index}].risk", strict=True)
        except RiskError:
            conflicts.append(conflict("INVALID_RISK", "snapshot", row, f"snapshot:{index}"))
    return conflicts


def validate_discovery_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate discovery rows have enough identity and safe status/risk."""

    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not any(
            (
                stable_source_fingerprint(row),
                row_value(row, "source_key"),
                row_value(row, "inventory_id"),
                row_value(row, "source_code_reference"),
                row_route(row),
            )
        ):
            conflicts.append(
                conflict("REQUIRED_IDENTITY_MISSING", "discovery", row, f"discovery:{index}")
            )
        try:
            normalize_inventory_status(row_status(row), path=f"discovery[{index}].status")
        except StatusModelError:
            conflicts.append(conflict("INVALID_STATUS", "discovery", row, f"discovery:{index}"))
        try:
            normalize_risk_value(row.get("risk"), path=f"discovery[{index}].risk", strict=True)
        except RiskError:
            conflicts.append(conflict("INVALID_RISK", "discovery", row, f"discovery:{index}"))
    return conflicts


def inventory_id_fingerprint_conflicts(
    discovery_rows: Sequence[Mapping[str, Any]],
    snapshot_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Detect same inventory_id with mismatching source fingerprint."""

    conflicts: list[dict[str, Any]] = []
    by_id = {
        row_value(row, "inventory_id"): row
        for row in snapshot_rows
        if row_value(row, "inventory_id")
    }
    for row in discovery_rows:
        inventory_id = row_value(row, "inventory_id")
        existing = by_id.get(inventory_id)
        if existing is None:
            continue
        discovery_fp = stable_source_fingerprint(row)
        existing_fp = stable_source_fingerprint(existing)
        if discovery_fp and existing_fp and discovery_fp != existing_fp:
            conflicts.append(
                conflict(
                    "SOURCE_FINGERPRINT_MISMATCH_WITH_SAME_INVENTORY_ID",
                    "inventory_id",
                    row,
                    f"inventory_id:{safe_text(inventory_id)}",
                )
            )
    return conflicts


def conflict(
    reason_code: str,
    source: str,
    row: Mapping[str, Any],
    match_key: str,
    *,
    snapshot_indexes: Sequence[int] = (),
) -> dict[str, Any]:
    """Build a redaction-safe conflict record."""

    payload: dict[str, Any] = {
        "reason_code": reason_code,
        "source": source,
        "match_key": safe_text(match_key),
        "inventory_id": row_value(row, "inventory_id"),
        "source_fingerprint": stable_source_fingerprint(row),
        "source_key": row_value(row, "source_key"),
        "feature_name": row_feature_name(row),
        "source_code_reference": row_value(row, "source_code_reference"),
    }
    if snapshot_indexes:
        payload["snapshot_indexes"] = list(snapshot_indexes)
    return payload


def conflict_discovery_indexes(conflicts: Sequence[Mapping[str, Any]]) -> set[int]:
    """Return discovery indexes that are globally conflicted.

    Conflict records intentionally avoid raw row indexes for most cases, so this is
    conservative and currently only applies to duplicate records by explicit row indexes.
    """

    indexes: set[int] = set()
    for item in conflicts:
        if item.get("source") == "discovery" and isinstance(item.get("row_indexes"), list):
            indexes.update(int(value) for value in item["row_indexes"])
    return indexes


def conflict_snapshot_indexes(conflicts: Sequence[Mapping[str, Any]]) -> set[int]:
    """Return snapshot indexes that are globally conflicted."""

    indexes: set[int] = set()
    for item in conflicts:
        if item.get("source") == "snapshot" and isinstance(item.get("row_indexes"), list):
            indexes.update(int(value) for value in item["row_indexes"])
    return indexes


def append_operation(
    row: Mapping[str, Any],
    headers: Sequence[str],
    *,
    snapshot_row_count: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    """Build APPEND operation."""

    operation = "APPEND_DISCOVERY_GAP" if row_status(row) == "DISCOVERY_GAP" else "APPEND_INVENTORY"
    canonical = canonical_row(row, headers)
    return {
        "operation_id": operation_id(operation, stable_source_fingerprint(row), canonical),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": stable_source_fingerprint(row) or row_value(row, "inventory_id"),
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": append_coordinate(snapshot_row_count),
        "expected_absent": {
            "inventory_id": row_value(row, "inventory_id"),
            "source_fingerprint": stable_source_fingerprint(row),
        },
        "row": canonical,
        "reason": "NEW_DISCOVERY",
    }


def append_gap_operation(
    row: Mapping[str, Any],
    headers: Sequence[str],
    *,
    snapshot_row_count: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    """Build gap append operation."""

    canonical = canonical_row(row, headers)
    operation = "APPEND_DISCOVERY_GAP"
    return {
        "operation_id": operation_id(operation, stable_source_fingerprint(row), canonical),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": stable_source_fingerprint(row) or row_value(row, "inventory_id"),
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": append_coordinate(snapshot_row_count),
        "expected_absent": {
            "inventory_id": row_value(row, "inventory_id"),
            "source_fingerprint": stable_source_fingerprint(row),
        },
        "row": canonical,
        "reason": "NEW_DISCOVERY_GAP",
    }


def update_or_noop_operation(
    existing: Mapping[str, Any],
    discovery: Mapping[str, Any],
    headers: Sequence[str],
    *,
    snapshot_index: int,
    snapshot_fingerprint: str,
) -> tuple[dict[str, Any] | dict[str, Any], bool] | tuple[None, bool]:
    """Build update, conflict, or noop decision."""

    existing_status = normalize_inventory_status(row_status(existing))
    if existing_status in LOCKED_STATUSES:
        return None, True
    changed = changed_fields(existing, discovery, headers)
    if not changed:
        return None, True
    protected_changed = any(field in changed for field in ("discovery_status", "risk"))
    if manual_override_enabled(existing) and protected_changed:
        return (
            conflict(
                "MANUAL_OVERRIDE_BLOCKS_PROTECTED_UPDATE",
                "snapshot",
                existing,
                row_value(existing, "inventory_id"),
            ),
            False,
        )
    operation = "UPDATE_INVENTORY_FIELDS"
    payload = {
        "inventory_id": row_value(existing, "inventory_id"),
        "fields": changed,
    }
    return (
        {
            "operation_id": operation_id(operation, row_value(existing, "inventory_id"), payload),
            "operation": operation,
            "target_tab": "Inventory",
            "match_key": row_value(existing, "inventory_id"),
            "expected_snapshot_fingerprint": snapshot_fingerprint,
            "row_coordinate": row_coordinate(existing, snapshot_index),
            "expected_old_values": expected_old_values(existing, changed),
            "inventory_id": row_value(existing, "inventory_id"),
            "fields": changed,
            "reason": "DISCOVERY_CHANGED",
        },
        False,
    )


def changed_fields(
    existing: Mapping[str, Any],
    discovery: Mapping[str, Any],
    headers: Sequence[str],
) -> dict[str, Any]:
    """Return auto-update fields that differ."""

    existing_status = normalize_inventory_status(row_status(existing))
    canonical = canonical_row(discovery, headers)
    changes: dict[str, Any] = {}
    for key in sorted(allowed_update_columns(headers)):
        if key not in canonical:
            continue
        if key == "inventory_id":
            continue
        value = canonical[key]
        if value in ("", [], None):
            continue
        if key == "last_seen_commit" and value == "UNKNOWN":
            continue
        if key == "discovery_status" and existing_status in LOCKED_STATUSES:
            continue
        if str(existing.get(key, "")) != str(value):
            changes[key] = value
    return changes


def render_cell_value(value: Any) -> str:
    """Render one JSON-compatible value as a plain spreadsheet cell string."""

    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ",".join(safe_text(str(item)) for item in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return safe_text(str(value))


def expected_old_values(
    existing: Mapping[str, Any],
    changed: Mapping[str, Any],
) -> dict[str, str]:
    """Return old values that must still match before applying an update."""

    expected: dict[str, str] = {
        "inventory_id": render_cell_value(row_value(existing, "inventory_id")),
        "source_fingerprint": render_cell_value(stable_source_fingerprint(existing)),
    }
    for field in sorted(changed):
        expected[field] = render_cell_value(existing.get(field, ""))
    return expected


def retire_operation(
    row: Mapping[str, Any],
    *,
    snapshot_index: int,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    """Build retire update operation."""

    fields = {"discovery_status": "RETIRED"}
    operation = "MARK_INVENTORY_RETIRED"
    payload = {"inventory_id": row_value(row, "inventory_id"), "fields": fields}
    return {
        "operation_id": operation_id(operation, row_value(row, "inventory_id"), payload),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": row_value(row, "inventory_id"),
        "expected_snapshot_fingerprint": snapshot_fingerprint,
        "row_coordinate": row_coordinate(row, snapshot_index),
        "expected_old_values": expected_old_values(row, fields),
        "inventory_id": row_value(row, "inventory_id"),
        "fields": fields,
        "reason": "MISSING_FROM_DISCOVERY",
    }


def canonical_row(row: Mapping[str, Any], headers: Sequence[str]) -> dict[str, Any]:
    """Return canonical Inventory columns only."""

    values: dict[str, Any] = {
        "inventory_id": row_value(row, "inventory_id"),
        "feature_area": row_value(row, "feature_area"),
        "item_type": row_value(row, "item_type") or "UI_PAGE",
        "name": row_feature_name(row),
        "actor_roles": row.get("actor_roles")
        if row.get("actor_roles") not in (None, "")
        else [row_actor(row)],
        "route_or_trigger": row_route(row),
        "source_path": row_value(row, "source_path"),
        "source_symbol": row_value(row, "source_symbol"),
        "source_lines": row_value(row, "source_lines"),
        "source_fingerprint": stable_source_fingerprint(row),
        "test_scope": row_value(row, "test_scope") or "E2E_PLAYWRIGHT",
        "recommended_test_type": row_value(row, "recommended_test_type") or "playwright",
        "discovery_status": row_status(row) or "DISCOVERED",
        "exclusion_reason": row_value(row, "exclusion_reason"),
        "reachability": row_value(row, "reachability") or "CODE_ONLY",
        "risk": row_value(row, "risk") or "MEDIUM",
        "mapped_scenario_ids": row.get("mapped_scenario_ids", []),
        "discovered_at": row_value(row, "discovered_at"),
        "last_seen_commit": row_value(row, "last_seen_commit") or "UNKNOWN",
        "last_seen_at": row_value(row, "last_seen_at"),
        "notes": row_value(row, "notes"),
    }
    return {header: values[header] for header in headers if header in values}


def allowed_update_columns(headers: Sequence[str]) -> set[str]:
    """Return auto-update columns allowed by schema."""

    return {
        header
        for header in headers
        if header in AUTO_UPDATE_COLUMNS
        and header not in HUMAN_EDITABLE_COLUMNS
        and not header.startswith(("manual_", "human_"))
    }


def manual_override_enabled(row: Mapping[str, Any]) -> bool:
    """Return whether manual override is truthy."""

    value = str(row.get("manual_override", "")).strip().lower()
    return value in {"true", "1", "yes", "y", "on"}


def can_retire(row: Mapping[str, Any]) -> bool:
    """Return whether a missing row may be marked retired."""

    if manual_override_enabled(row) or not is_managed_source(row):
        return False
    try:
        status = normalize_inventory_status(row_status(row))
    except StatusModelError:
        return False
    return status not in RETIRE_EXCLUDED_STATUSES


def is_managed_source(row: Mapping[str, Any]) -> bool:
    """Return whether row appears managed by static discovery."""

    source = row_value(row, "discovery_source").lower()
    if source.startswith(DISCOVERY_MANAGED_SOURCES):
        return True
    return bool(stable_source_fingerprint(row) and row_value(row, "source_path"))


def same_gap(gap: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    """Return whether a gap row is already represented."""

    return row_status(row) == "DISCOVERY_GAP" and stable_source_fingerprint(
        gap
    ) == stable_source_fingerprint(row)


def summarize(
    operations: Sequence[Mapping[str, Any]],
    conflicts: Sequence[Mapping[str, Any]],
    noop_count: int,
    redaction_counts: Counter[str],
) -> dict[str, Any]:
    """Build summary counters."""

    append_count = sum(1 for item in operations if item["operation"] == "APPEND_INVENTORY")
    gap_count = sum(1 for item in operations if item["operation"] == "APPEND_DISCOVERY_GAP")
    update_count = sum(1 for item in operations if item["operation"] == "UPDATE_INVENTORY_FIELDS")
    retire_count = sum(1 for item in operations if item["operation"] == "MARK_INVENTORY_RETIRED")
    return {
        "append_count": append_count + gap_count,
        "update_count": update_count,
        "retire_count": retire_count,
        "conflict_count": len(conflicts),
        "noop_count": noop_count,
        "operation_count": len(operations),
        "redaction_count": sum(redaction_counts.values()),
        "redactions": dict(sorted(redaction_counts.items())),
    }


def apply_inventory_sync_plan(
    plan: Mapping[str, Any],
    fresh_snapshot_payload: Mapping[str, Any],
    *,
    headers: Sequence[str],
    backend: SheetsBackend,
    wal: AppendOnlyWal,
) -> InventoryApplyResult:
    """Apply a conflict-free Inventory sync plan using fake/unit backend contracts."""

    operations = validate_applicable_plan(plan)
    fresh_rows = extract_snapshot_inventory(fresh_snapshot_payload)
    ensure_snapshot_fingerprint(plan, headers, fresh_rows)
    ensure_backend_snapshot_fingerprint(plan, headers, backend)
    mutations = inventory_apply_mutations(plan, operations, fresh_rows, headers)
    validate_batch(mutations)
    if not mutations:
        return InventoryApplyResult(
            outcome="NOOP",
            applied_mutation_count=0,
            wal_pending_written=False,
            wal_acknowledged=False,
            read_back_verified=True,
            operation_count=0,
        )
    pending_payload = inventory_apply_wal_payload(plan, operations)
    pending = append_inventory_pending(wal, pending_payload)
    try:
        backend.apply_batch(mutations)
    except SheetsBackendError as exc:
        if not exc.may_have_applied:
            raise InventorySyncError(
                "INVENTORY_APPLY_BACKEND_FAILED",
                "backend",
                exc.code,
                exit_code=exc.exit_code,
            ) from None
        if verify_inventory_read_back(backend, operations):
            append_inventory_ack(wal, pending.operation_id, pending_payload)
            return InventoryApplyResult(
                outcome="APPLIED_AFTER_AMBIGUOUS",
                applied_mutation_count=len(mutations),
                wal_pending_written=True,
                wal_acknowledged=True,
                read_back_verified=True,
                operation_count=len(operations),
            )
        raise InventorySyncError(
            "INVENTORY_APPLY_OUTCOME_UNKNOWN",
            "backend",
            "READ_BACK_UNPROVEN",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if not verify_inventory_read_back(backend, operations):
        raise InventorySyncError(
            "INVENTORY_APPLY_READ_BACK_MISMATCH",
            "inventory",
            "POSTCONDITION_FAILED",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    append_inventory_ack(wal, pending.operation_id, pending_payload)
    return InventoryApplyResult(
        outcome="APPLIED",
        applied_mutation_count=len(mutations),
        wal_pending_written=True,
        wal_acknowledged=True,
        read_back_verified=True,
        operation_count=len(operations),
    )


def validate_applicable_plan(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return operations if a plan is safe to apply."""

    if plan.get("plan_schema_version") != PLAN_SCHEMA_VERSION:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            "plan_schema_version",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    conflicts = plan.get("conflicts", [])
    if isinstance(conflicts, Sequence) and not isinstance(conflicts, str) and len(conflicts) > 0:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_CONFLICT",
            "conflicts",
            "REVIEW_REQUIRED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    operations = plan.get("operations", [])
    if not isinstance(operations, list):
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            "operations",
            "LIST_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    safe_operations: list[Mapping[str, Any]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise InventorySyncError(
                "INVENTORY_APPLY_PLAN_INVALID",
                f"operations.[{index}]",
                "OBJECT_REQUIRED",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        safe_operations.append(operation)
    return safe_operations


def ensure_snapshot_fingerprint(
    plan: Mapping[str, Any],
    headers: Sequence[str],
    fresh_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Ensure the apply input still matches the plan snapshot fingerprint."""

    expected = plan_snapshot_fingerprint(plan)
    actual = inventory_apply_fingerprint(headers, fresh_rows)
    if expected != actual:
        raise InventorySyncError(
            "INVENTORY_APPLY_STALE_SNAPSHOT",
            "snapshot",
            "FINGERPRINT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )


def ensure_backend_snapshot_fingerprint(
    plan: Mapping[str, Any],
    headers: Sequence[str],
    backend: SheetsBackend,
) -> None:
    """Ensure backend rows still match the fresh snapshot before WAL/write."""

    try:
        state = backend.read_spreadsheet()
    except SheetsBackendError as exc:
        raise InventorySyncError(
            "INVENTORY_APPLY_BACKEND_FAILED",
            "backend",
            exc.code,
            exit_code=exc.exit_code,
        ) from None
    backend_rows = state.rows_dict().get(INVENTORY_TAB)
    if backend_rows is None:
        raise InventorySyncError(
            "INVENTORY_APPLY_BACKEND_STATE_INVALID",
            "Inventory",
            "TAB_MISSING",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    if plan_snapshot_fingerprint(plan) != inventory_apply_fingerprint(headers, backend_rows):
        raise InventorySyncError(
            "INVENTORY_APPLY_STALE_SNAPSHOT",
            "backend",
            "FINGERPRINT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )


def inventory_apply_mutations(
    plan: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    fresh_rows: Sequence[Mapping[str, Any]],
    headers: Sequence[str],
) -> list[AppendRows | UpdateRowValues]:
    """Translate safe Inventory operations to typed row mutations."""

    mutations: list[AppendRows | UpdateRowValues] = []
    expected_snapshot = plan_snapshot_fingerprint(plan)
    for index, operation in enumerate(operations):
        if operation.get("expected_snapshot_fingerprint") != expected_snapshot:
            raise InventorySyncError(
                "INVENTORY_APPLY_PLAN_INVALID",
                f"operations.[{index}].expected_snapshot_fingerprint",
                "MISMATCH",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        kind = operation.get("operation")
        if kind in {"APPEND_INVENTORY", "APPEND_DISCOVERY_GAP"}:
            ensure_append_absent(operation, fresh_rows, index)
            row = operation.get("row")
            if not isinstance(row, Mapping):
                raise InventorySyncError(
                    "INVENTORY_APPLY_PLAN_INVALID",
                    f"operations.[{index}].row",
                    "OBJECT_REQUIRED",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            mutations.append(
                AppendRows(
                    tab_name=INVENTORY_TAB,
                    rows=(row_values(row, headers),),
                )
            )
        elif kind in {"UPDATE_INVENTORY_FIELDS", "MARK_INVENTORY_RETIRED"}:
            data_index = operation_data_index(operation, index)
            ensure_expected_old_values(operation, fresh_rows, data_index, index)
            fields = operation.get("fields")
            if not isinstance(fields, Mapping) or not fields:
                raise InventorySyncError(
                    "INVENTORY_APPLY_PLAN_INVALID",
                    f"operations.[{index}].fields",
                    "OBJECT_REQUIRED",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            expected = operation.get("expected_old_values")
            if not isinstance(expected, Mapping) or not expected:
                raise InventorySyncError(
                    "INVENTORY_APPLY_PLAN_INVALID",
                    f"operations.[{index}].expected_old_values",
                    "OBJECT_REQUIRED",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            mutations.append(
                UpdateRowValues(
                    tab_name=INVENTORY_TAB,
                    row_index=data_index,
                    values=row_values(fields, headers, allow_subset=True),
                    expected_values=row_values(expected, headers, allow_subset=True),
                )
            )
        else:
            raise InventorySyncError(
                "INVENTORY_APPLY_PLAN_INVALID",
                f"operations.[{index}].operation",
                "UNSUPPORTED_OPERATION",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
    return mutations


def ensure_append_absent(
    operation: Mapping[str, Any],
    fresh_rows: Sequence[Mapping[str, Any]],
    index: int,
) -> None:
    """Ensure an append operation still targets an absent Inventory identity."""

    coordinate = operation.get("row_coordinate")
    if not isinstance(coordinate, Mapping) or coordinate.get("mode") != "append":
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate",
            "APPEND_COORDINATE_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    expected_count = coordinate.get("expected_after_data_rows")
    if not isinstance(expected_count, int) or expected_count != len(fresh_rows):
        raise InventorySyncError(
            "INVENTORY_APPLY_STALE_SNAPSHOT",
            f"operations.[{index}].row_coordinate",
            "ROW_COUNT_MISMATCH",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    expected_absent = operation.get("expected_absent")
    if not isinstance(expected_absent, Mapping):
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            f"operations.[{index}].expected_absent",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    inventory_id = render_cell_value(expected_absent.get("inventory_id", ""))
    source_fingerprint = render_cell_value(expected_absent.get("source_fingerprint", ""))
    for row in fresh_rows:
        if inventory_id and row_value(row, "inventory_id") == inventory_id:
            raise InventorySyncError(
                "INVENTORY_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].inventory_id",
                "ALREADY_EXISTS",
                exit_code=EXIT_LOCK_CONFLICT,
            )
        if source_fingerprint and stable_source_fingerprint(row) == source_fingerprint:
            raise InventorySyncError(
                "INVENTORY_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].source_fingerprint",
                "ALREADY_EXISTS",
                exit_code=EXIT_LOCK_CONFLICT,
            )


def operation_data_index(operation: Mapping[str, Any], index: int) -> int:
    """Extract a zero-based data row index from an operation coordinate."""

    coordinate = operation.get("row_coordinate")
    if not isinstance(coordinate, Mapping):
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    data_index = coordinate.get("data_index")
    if not isinstance(data_index, int) or isinstance(data_index, bool) or data_index < 0:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            f"operations.[{index}].row_coordinate.data_index",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return data_index


def ensure_expected_old_values(
    operation: Mapping[str, Any],
    fresh_rows: Sequence[Mapping[str, Any]],
    data_index: int,
    index: int,
) -> None:
    """Ensure expected old values still match a fresh snapshot row."""

    if data_index >= len(fresh_rows):
        raise InventorySyncError(
            "INVENTORY_APPLY_STALE_SNAPSHOT",
            f"operations.[{index}].row_coordinate",
            "ROW_MISSING",
            exit_code=EXIT_LOCK_CONFLICT,
        )
    expected = operation.get("expected_old_values")
    if not isinstance(expected, Mapping) or not expected:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            f"operations.[{index}].expected_old_values",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    row = fresh_rows[data_index]
    for column, expected_value in expected.items():
        if render_cell_value(row.get(str(column), "")) != render_cell_value(expected_value):
            raise InventorySyncError(
                "INVENTORY_APPLY_STALE_SNAPSHOT",
                f"operations.[{index}].expected_old_values.{safe_text(str(column))}",
                "VALUE_MISMATCH",
                exit_code=EXIT_LOCK_CONFLICT,
            )


def row_values(
    values: Mapping[str, Any],
    headers: Sequence[str],
    *,
    allow_subset: bool = False,
) -> tuple[tuple[str, str], ...]:
    """Return safe column/value pairs in canonical header order."""

    allowed = set(headers)
    unknown = sorted(str(key) for key in values if str(key) not in allowed)
    if unknown:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            "row",
            "UNKNOWN_COLUMN",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return tuple(
        (header, render_cell_value(values.get(header, "")))
        for header in headers
        if header in values
    )


def plan_snapshot_fingerprint(plan: Mapping[str, Any]) -> str:
    """Return the expected source snapshot fingerprint from a plan."""

    source = plan.get("source")
    if not isinstance(source, Mapping):
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            "source",
            "OBJECT_REQUIRED",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    fingerprint = source.get("snapshot_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise InventorySyncError(
            "INVENTORY_APPLY_PLAN_INVALID",
            "source.snapshot_fingerprint",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return fingerprint


def inventory_apply_wal_payload(
    plan: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a redaction-safe WAL payload for Inventory apply."""

    operation_ids = []
    for index, operation in enumerate(operations):
        operation_id_value = operation.get("operation_id")
        if not isinstance(operation_id_value, str) or operation_id_value == "":
            raise InventorySyncError(
                "INVENTORY_APPLY_PLAN_INVALID",
                f"operations.[{index}].operation_id",
                "INVALID",
                exit_code=EXIT_ARGUMENT_OR_SCHEMA,
            )
        operation_ids.append(operation_id_value)
    payload = {
        "target_tab": INVENTORY_TAB,
        "plan_fingerprint": fingerprint_payload(plan),
        "snapshot_fingerprint": plan_snapshot_fingerprint(plan),
        "operation_count": len(operations),
        "operation_ids": operation_ids,
    }
    if secret_findings(payload):
        raise InventorySyncError(
            "INVENTORY_APPLY_PAYLOAD_UNSAFE",
            "wal.payload",
            "SECRET_DETECTED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    return payload


def append_inventory_pending(wal: AppendOnlyWal, payload: dict[str, Any]) -> Any:
    """Append Inventory apply pending WAL entry safely."""

    try:
        return wal.append_pending(
            INVENTORY_APPLY_OPERATION,
            payload,
            operation_id="INV-APPLY-" + fingerprint_payload(payload)[7:23].upper(),
        )
    except WalError as exc:
        raise InventorySyncError(
            "INVENTORY_APPLY_WAL_FAILED",
            "wal",
            exc.code,
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None


def append_inventory_ack(
    wal: AppendOnlyWal,
    operation_id_value: str,
    payload: Mapping[str, Any],
) -> None:
    """Append Inventory apply ack WAL entry safely."""

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
        raise InventorySyncError(
            "INVENTORY_APPLY_WAL_FAILED",
            "wal",
            exc.code,
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None


def verify_inventory_read_back(
    backend: SheetsBackend,
    operations: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether backend rows satisfy all operation postconditions."""

    try:
        state = backend.read_spreadsheet()
    except SheetsBackendError:
        return False
    rows = state.rows_dict().get(INVENTORY_TAB, [])
    for operation in operations:
        kind = operation.get("operation")
        if kind in {"APPEND_INVENTORY", "APPEND_DISCOVERY_GAP"}:
            row = operation.get("row")
            if not isinstance(row, Mapping):
                return False
            if not any(row_contains_values(candidate, row) for candidate in rows):
                return False
        elif kind in {"UPDATE_INVENTORY_FIELDS", "MARK_INVENTORY_RETIRED"}:
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


def inventory_headers(schema_path: Path) -> tuple[tuple[str, ...], int]:
    """Load canonical Inventory headers and validate minimum columns."""

    try:
        schema = load_canonical_schema(schema_path)
    except Exception:
        raise InventorySyncError(
            "INVENTORY_SYNC_SCHEMA_INVALID",
            "schema",
            "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    for tab in schema.tabs:
        if tab.name == "Inventory":
            headers = tab.headers
            missing = [name for name in REQUIRED_INVENTORY_COLUMNS if name not in headers]
            if missing:
                raise InventorySyncError(
                    "INVENTORY_SYNC_SCHEMA_INVALID",
                    "schema.Inventory",
                    "REQUIRED_COLUMN_MISSING",
                    exit_code=EXIT_ARGUMENT_OR_SCHEMA,
                )
            return headers, schema.schema_version
    raise InventorySyncError(
        "INVENTORY_SYNC_SCHEMA_INVALID",
        "schema.Inventory",
        "TAB_MISSING",
        exit_code=EXIT_ARGUMENT_OR_SCHEMA,
    )


def validate_input_file(path: Path, label: str) -> None:
    """Validate one input path."""

    if not path.exists():
        raise InventorySyncError(
            "INVENTORY_SYNC_INPUT_INVALID",
            label,
            "MISSING",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not path.is_file():
        raise InventorySyncError(
            "INVENTORY_SYNC_INPUT_INVALID",
            label,
            "NOT_FILE",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )


def read_json_file(path: Path, label: str) -> Mapping[str, Any]:
    """Read a JSON object safely."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise InventorySyncError(
            "INVENTORY_SYNC_JSON_INVALID",
            label,
            "JSON_INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        ) from None
    except OSError:
        raise InventorySyncError(
            "INVENTORY_SYNC_READ_FAILED",
            label,
            "READ_FAILED",
            exit_code=EXIT_EXTERNAL_FAILURE,
        ) from None
    if not isinstance(data, Mapping):
        raise InventorySyncError(
            "INVENTORY_SYNC_JSON_INVALID",
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
            raise InventorySyncError(
                "INVENTORY_SYNC_OUTPUT_UNSAFE",
                "output",
                "SYMLINK_REJECTED",
            )
        if not stat.S_ISREG(stat_result.st_mode):
            raise InventorySyncError("INVENTORY_SYNC_OUTPUT_UNSAFE", "output", "NOT_REGULAR_FILE")
        if not force:
            raise InventorySyncError("INVENTORY_SYNC_OUTPUT_EXISTS", "output", "EXISTS")
    output_resolved = path.resolve(strict=False)
    for protected in protected_paths:
        if output_resolved == protected.resolve(strict=False):
            raise InventorySyncError(
                "INVENTORY_SYNC_OUTPUT_UNSAFE",
                "output",
                "INPUT_PATH_REJECTED",
            )


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
        raise InventorySyncError(
            "INVENTORY_SYNC_WRITE_FAILED",
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
