"""CLI orchestration for applying Scenario sync plans to Google Sheets."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_file, validate_config
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_LOCK_CONFLICT,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    build_sheets_service,
    load_service_account_credentials,
)
from webapp_debug_skill.google_sheets_backend import GoogleSheetsBackend
from webapp_debug_skill.inventory_identity import fingerprint_payload
from webapp_debug_skill.inventory_sync import INVENTORY_TAB, inventory_headers
from webapp_debug_skill.redaction import secret_findings
from webapp_debug_skill.scenario_sync import (
    SCENARIOS_TAB,
    ScenarioApplyResult,
    ScenarioSyncError,
    apply_scenario_sync_plan,
    ensure_backend_snapshot_fingerprints,
    ensure_snapshot_fingerprints,
    extract_snapshot_inventory,
    extract_snapshot_scenarios,
    read_json_file,
    scenario_apply_mutations,
    scenario_headers_from_schema,
    validate_applicable_plan,
    validate_input_file,
)
from webapp_debug_skill.sheets_client import SheetsBackend, SheetsBackendError, validate_batch
from webapp_debug_skill.sheets_init import InitPlanningError, load_canonical_schema
from webapp_debug_skill.sheets_init_cli import (
    DEFAULT_CONFIG,
    DEFAULT_SCHEMA,
    RUN_ID_PATTERN,
    resolve_repository_root,
)
from webapp_debug_skill.sheets_lock import SheetsLockError, SheetsLockManager
from webapp_debug_skill.sheets_snapshot import SheetsSnapshotError, SheetsSnapshotExporter
from webapp_debug_skill.wal import AppendOnlyWal, WalError, default_clock, default_wal_path


class ScenarioApplyCliError(RuntimeError):
    """Safe Scenario apply CLI error."""

    def __init__(
        self,
        code: str,
        path: str = "scenario_apply",
        reason: str = "FAILED",
        *,
        exit_code: int = EXIT_ARGUMENT_OR_SCHEMA,
        data: dict[str, Any] | None = None,
    ) -> None:
        safe_code = "SCENARIO_APPLY_FAILED" if secret_findings(code) else code
        super().__init__(safe_code)
        self.code = safe_code
        self.path = "scenario_apply" if secret_findings(path) else path
        self.reason = "FAILED" if secret_findings(reason) else reason
        self.exit_code = exit_code
        self.data = data or {}


@dataclass(frozen=True)
class ScenarioApplyDependencies:
    """Injectable dependencies for tests and production."""

    credential_loader: Callable[..., Any] = load_service_account_credentials
    service_builder: Callable[..., Any] = build_sheets_service
    backend_factory: Callable[..., SheetsBackend] = GoogleSheetsBackend
    wal_factory: Callable[..., AppendOnlyWal] = AppendOnlyWal
    lock_manager_factory: Callable[[SheetsBackend], SheetsLockManager] = SheetsLockManager
    run_id_factory: Callable[[], str] = lambda: f"run-{uuid.uuid4().hex}"
    clock: Callable[[], str] = default_clock
    snapshot_clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def build_parser() -> argparse.ArgumentParser:
    """Build apply_scenario_sync parser."""

    parser = argparse.ArgumentParser(description="Apply a Scenario sync plan to Google Sheets.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--confirm-spreadsheet-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wal", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(
    argv: list[str] | None = None,
    deps: ScenarioApplyDependencies | None = None,
) -> int:
    """Run apply_scenario_sync CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or ScenarioApplyDependencies()
    try:
        result = run(args, dependencies)
    except (
        GoogleCredentialError,
        SheetsBackendError,
        SheetsLockError,
        SheetsSnapshotError,
        InitPlanningError,
        ScenarioSyncError,
        WalError,
    ) as exc:
        result = CliResult(
            ok=False,
            code=getattr(exc, "code", "SCENARIO_APPLY_FAILED"),
            message="Scenario apply failed.",
            details=[
                Issue(
                    getattr(exc, "path", "scenario_apply"),
                    getattr(exc, "reason", "FAILED"),
                )
            ],
            data=getattr(exc, "data", {}),
        )
        emit_result(result, args.format)
        return getattr(exc, "exit_code", EXIT_EXTERNAL_FAILURE)
    except ScenarioApplyCliError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Scenario apply failed.",
            details=[Issue(exc.path, exc.reason)],
            data=exc.data,
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="SCENARIO_APPLY_UNEXPECTED",
            message="Unexpected Scenario apply failure.",
            details=[Issue("scenario_apply", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK if result.ok else EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: ScenarioApplyDependencies) -> CliResult:
    """Run Scenario apply orchestration and return a safe result."""

    validate_args(args)
    config_path = args.config.resolve()
    schema_path = args.schema.resolve()
    plan_path = args.plan.resolve()
    config, config_validation = load_and_validate_apply_config(config_path)
    schema = load_canonical_schema(schema_path)
    scenario_headers = scenario_headers_from_schema(schema_path)
    inventory_header_values, _schema_version = inventory_headers(schema_path)
    validate_input_file(plan_path, "plan")
    plan = read_json_file(plan_path, "plan")
    validate_applicable_plan(plan)

    repository_root = resolve_repository_root(config_path, config)
    sheets_config = config.get("sheets", {})
    if not isinstance(sheets_config, Mapping):
        raise ScenarioApplyCliError("CONFIG_VALIDATION_FAILED", "sheets", "INVALID")
    spreadsheet_id = str(sheets_config.get("spreadsheet_id", ""))
    if not spreadsheet_id:
        raise ScenarioApplyCliError(
            "SCENARIO_APPLY_SPREADSHEET_ID_REQUIRED",
            "sheets.spreadsheet_id",
            "EMPTY",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    validate_spreadsheet_id(spreadsheet_id)
    if not args.dry_run:
        validate_confirmation(args.confirm_spreadsheet_id, spreadsheet_id)

    run_id = args.run_id or deps.run_id_factory()
    validate_run_id(run_id)
    base_data = {
        "action": "apply_scenario_sync",
        "dry_run": args.dry_run,
        "spreadsheet_id": spreadsheet_id,
        "run_id": run_id,
        "config_validation": config_validation.code,
        "plan_fingerprint": fingerprint_payload(plan),
    }

    env_name = str(sheets_config.get("service_account_credentials_env", ""))
    credential_result = deps.credential_loader(env_name=env_name, repository_root=repository_root)
    service = deps.service_builder(credential_result.credentials)
    backend = deps.backend_factory(spreadsheet_id=spreadsheet_id, service=service)

    if args.dry_run:
        return dry_run_apply(
            deps,
            schema,
            scenario_headers,
            inventory_header_values,
            plan,
            backend,
            base_data,
        )
    return apply_with_lock(
        args,
        deps,
        schema,
        scenario_headers,
        inventory_header_values,
        plan,
        backend,
        config,
        repository_root,
        run_id,
        base_data,
    )


def validate_args(args: argparse.Namespace) -> None:
    """Validate argument shape before side effects."""

    if args.run_id:
        validate_run_id(args.run_id)
    if args.confirm_spreadsheet_id is not None and args.confirm_spreadsheet_id == "":
        raise ScenarioApplyCliError(
            "SCENARIO_APPLY_CONFIRMATION_REQUIRED",
            "confirm_spreadsheet_id",
            "EMPTY",
            exit_code=EXIT_POLICY_BLOCKED,
        )


def load_and_validate_apply_config(config_path: Path) -> tuple[dict[str, Any], CliResult]:
    """Load config and validate the Scenario Sheets write capability."""

    validation = validate_config(
        config_path,
        "init",
        explicit_capabilities=("base", "sheets-write"),
        schema_path=DEFAULT_CONFIG_SCHEMA,
    )
    if not validation.ok:
        issue = validation.details[0] if validation.details else Issue("config", "INVALID")
        raise ScenarioApplyCliError(
            validation.code,
            issue.path,
            issue.reason,
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    config, issues = load_yaml_file(config_path)
    if issues or config is None:
        issue = issues[0] if issues else Issue("config", "READ_FAILED")
        raise ScenarioApplyCliError("CONFIG_INVALID", issue.path, issue.reason)
    return config, validation


def validate_run_id(value: str) -> None:
    """Validate run id."""

    if not value or len(value) > 128 or any(char not in RUN_ID_PATTERN for char in value):
        raise ScenarioApplyCliError("ARGUMENT_INVALID", "run_id", "INVALID_RUN_ID")


def validate_spreadsheet_id(value: str) -> None:
    """Validate spreadsheet id shape."""

    if len(value) < 5 or len(value) > 256 or any(char not in RUN_ID_PATTERN for char in value):
        raise ScenarioApplyCliError("ARGUMENT_INVALID", "spreadsheet_id", "INVALID_ID")


def validate_confirmation(confirmation: str | None, spreadsheet_id: str) -> None:
    """Require exact Spreadsheet ID confirmation before write."""

    if confirmation is None:
        raise ScenarioApplyCliError(
            "SCENARIO_APPLY_CONFIRMATION_REQUIRED",
            "confirm_spreadsheet_id",
            "REQUIRED",
            exit_code=EXIT_POLICY_BLOCKED,
        )
    if confirmation != spreadsheet_id:
        raise ScenarioApplyCliError(
            "SCENARIO_APPLY_CONFIRMATION_MISMATCH",
            "confirm_spreadsheet_id",
            "MISMATCH",
            exit_code=EXIT_POLICY_BLOCKED,
        )


def export_scenario_snapshot(
    deps: ScenarioApplyDependencies,
    schema: Any,
    backend: SheetsBackend,
) -> Mapping[str, Any]:
    """Read a fresh Inventory and Scenarios snapshot from the backend."""

    exporter = SheetsSnapshotExporter(reader=backend, clock=deps.snapshot_clock)
    return exporter.export(schema, tabs=(INVENTORY_TAB, SCENARIOS_TAB)).payload


def validate_plan_against_snapshot(
    plan: Mapping[str, Any],
    snapshot_payload: Mapping[str, Any],
    *,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    backend: SheetsBackend,
) -> tuple[int, int]:
    """Validate a Scenario plan against fresh state without mutating."""

    operations = validate_applicable_plan(plan)
    fresh_scenarios = extract_snapshot_scenarios(snapshot_payload)
    fresh_inventory = extract_snapshot_inventory(snapshot_payload)
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
    if mutations:
        validate_batch(mutations)
    return len(operations), len(mutations)


def dry_run_apply(
    deps: ScenarioApplyDependencies,
    schema: Any,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    plan: Mapping[str, Any],
    backend: SheetsBackend,
    base_data: dict[str, Any],
) -> CliResult:
    """Validate a Scenario apply plan without lock, WAL or write."""

    snapshot = export_scenario_snapshot(deps, schema, backend)
    operation_count, mutation_count = validate_plan_against_snapshot(
        plan,
        snapshot,
        scenario_headers=scenario_headers,
        inventory_headers=inventory_headers,
        backend=backend,
    )
    data = {
        **base_data,
        "outcome": "PLAN_OK" if mutation_count else "NOOP",
        "advisory": True,
        "lock_acquired": False,
        "lock_released": False,
        "wal_pending_written": False,
        "wal_acknowledged": False,
        "read_back_verified": False,
        "operation_count": operation_count,
        "planned_mutation_count": mutation_count,
        "applied_mutation_count": 0,
    }
    return CliResult(
        ok=True,
        code="SCENARIO_APPLY_PLAN",
        message="Scenario apply plan validated.",
        details=[],
        data=data,
    )


def apply_with_lock(
    args: argparse.Namespace,
    deps: ScenarioApplyDependencies,
    schema: Any,
    scenario_headers: Sequence[str],
    inventory_headers: Sequence[str],
    plan: Mapping[str, Any],
    backend: SheetsBackend,
    config: Mapping[str, Any],
    repository_root: Path,
    run_id: str,
    base_data: dict[str, Any],
) -> CliResult:
    """Acquire the cooperative lock and apply a Scenario plan."""

    lock_manager = deps.lock_manager_factory(backend)
    lease = lock_manager.acquire(
        run_id=run_id,
        ttl=timedelta(seconds=lock_ttl_seconds(config)),
        commit_sha="unknown",
    )
    lock_released = False
    apply_result: ScenarioApplyResult | None = None
    try:
        snapshot = export_scenario_snapshot(deps, schema, backend)
        wal = make_wal(args, deps, repository_root, run_id)
        apply_result = apply_scenario_sync_plan(
            plan,
            snapshot,
            scenario_headers=scenario_headers,
            inventory_headers=inventory_headers,
            backend=backend,
            wal=wal,
        )
    except Exception:
        try:
            lock_manager.release(lease)
        except SheetsLockError:
            pass
        raise
    try:
        lock_manager.release(lease)
        lock_released = True
    except SheetsLockError as exc:
        data = {
            **base_data,
            "outcome": apply_result.outcome if apply_result is not None else "UNKNOWN",
            "lock_released": False,
            "wal_acknowledged": bool(apply_result and apply_result.wal_acknowledged),
            "read_back_verified": bool(apply_result and apply_result.read_back_verified),
        }
        raise ScenarioApplyCliError(
            "SCENARIO_APPLY_LOCK_RELEASE_FAILED",
            exc.path,
            exc.reason,
            exit_code=EXIT_LOCK_CONFLICT,
            data=data,
        ) from None

    assert apply_result is not None
    data = {
        **base_data,
        "outcome": apply_result.outcome,
        "lock_acquired": True,
        "lock_released": lock_released,
        "wal_pending_written": apply_result.wal_pending_written,
        "wal_acknowledged": apply_result.wal_acknowledged,
        "read_back_verified": apply_result.read_back_verified,
        "operation_count": apply_result.operation_count,
        "planned_mutation_count": apply_result.operation_count,
        "applied_mutation_count": apply_result.applied_mutation_count,
    }
    return CliResult(
        ok=True,
        code="SCENARIO_APPLY_OK",
        message="Scenario apply completed.",
        details=[],
        data=data,
    )


def make_wal(
    args: argparse.Namespace,
    deps: ScenarioApplyDependencies,
    repository_root: Path,
    run_id: str,
) -> AppendOnlyWal:
    """Create an append-only WAL for Scenario apply."""

    wal_path = args.wal.resolve() if args.wal else default_wal_path(repository_root, run_id)
    return deps.wal_factory(wal_path, run_id, clock=deps.clock)


def lock_ttl_seconds(config: Mapping[str, Any]) -> int:
    """Return lock TTL seconds from config."""

    sheets = config.get("sheets", {})
    ttl_minutes = sheets.get("lock_ttl_minutes", 120) if isinstance(sheets, Mapping) else 120
    return int(ttl_minutes) * 60
