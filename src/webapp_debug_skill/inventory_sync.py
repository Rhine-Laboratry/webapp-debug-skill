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
from webapp_debug_skill.status_model import (
    InventoryStatus,
    StatusModelError,
    normalize_inventory_status,
)

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
            operations.append(append_operation(row, headers))
            continue
        if snapshot_index in conflicted_snapshots:
            continue
        existing = snapshot_rows[snapshot_index]
        planned, noop = update_or_noop_operation(existing, row, headers)
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
            operations.append(retire_operation(row))
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
                operations.append(append_gap_operation(gap, headers))

    summary = summarize(operations, conflicts, noop_count, redaction_counts)
    return {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "source": {
            "kind": "inventory_sync_plan",
            "discovery_fingerprint": fingerprint_payload(discovery_payload),
            "snapshot_fingerprint": fingerprint_payload(
                extract_snapshot_inventory(snapshot_payload)
            ),
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


def append_operation(row: Mapping[str, Any], headers: Sequence[str]) -> dict[str, Any]:
    """Build APPEND operation."""

    operation = "APPEND_DISCOVERY_GAP" if row_status(row) == "DISCOVERY_GAP" else "APPEND_INVENTORY"
    canonical = canonical_row(row, headers)
    return {
        "operation_id": operation_id(operation, stable_source_fingerprint(row), canonical),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": stable_source_fingerprint(row) or row_value(row, "inventory_id"),
        "row": canonical,
        "reason": "NEW_DISCOVERY",
    }


def append_gap_operation(row: Mapping[str, Any], headers: Sequence[str]) -> dict[str, Any]:
    """Build gap append operation."""

    canonical = canonical_row(row, headers)
    operation = "APPEND_DISCOVERY_GAP"
    return {
        "operation_id": operation_id(operation, stable_source_fingerprint(row), canonical),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": stable_source_fingerprint(row) or row_value(row, "inventory_id"),
        "row": canonical,
        "reason": "NEW_DISCOVERY_GAP",
    }


def update_or_noop_operation(
    existing: Mapping[str, Any],
    discovery: Mapping[str, Any],
    headers: Sequence[str],
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


def retire_operation(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build retire update operation."""

    fields = {"discovery_status": "RETIRED"}
    operation = "MARK_INVENTORY_RETIRED"
    payload = {"inventory_id": row_value(row, "inventory_id"), "fields": fields}
    return {
        "operation_id": operation_id(operation, row_value(row, "inventory_id"), payload),
        "operation": operation,
        "target_tab": "Inventory",
        "match_key": row_value(row, "inventory_id"),
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
