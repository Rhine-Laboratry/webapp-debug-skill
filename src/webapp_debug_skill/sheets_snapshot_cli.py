"""CLI orchestration for read-only Sheets snapshot export."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_file, validate_config
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    build_sheets_service,
    load_service_account_credentials,
)
from webapp_debug_skill.google_sheets_backend import GoogleSheetsBackend
from webapp_debug_skill.sheets_client import SheetsBackendError
from webapp_debug_skill.sheets_init import load_canonical_schema
from webapp_debug_skill.sheets_init_cli import resolve_repository_root, validate_spreadsheet_id
from webapp_debug_skill.sheets_snapshot import (
    DEFAULT_MAX_ROWS_PER_TAB,
    SheetsSnapshotError,
    SheetsSnapshotExporter,
    atomic_write_snapshot,
    validate_output_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(".webapp-debug/config.yml")
DEFAULT_SCHEMA = REPO_ROOT / "skills/webapp-debug/assets/google-sheets-schema.json"


@dataclass(frozen=True)
class SnapshotCliDependencies:
    """Injectable dependencies for tests."""

    credential_loader: Callable[..., Any] = load_service_account_credentials
    service_builder: Callable[..., Any] = build_sheets_service
    backend_factory: Callable[..., GoogleSheetsBackend] = GoogleSheetsBackend
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


def build_parser() -> argparse.ArgumentParser:
    """Build parser."""

    parser = argparse.ArgumentParser(description="Export a read-only Google Sheets snapshot.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tabs", help="Comma-separated canonical tab names.")
    parser.add_argument("--max-rows-per-tab", type=int, default=DEFAULT_MAX_ROWS_PER_TAB)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--force", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    deps: SnapshotCliDependencies | None = None,
) -> int:
    """Run snapshot export CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or SnapshotCliDependencies()
    try:
        result = run(args, dependencies)
    except (
        GoogleCredentialError,
        SheetsBackendError,
        SheetsSnapshotError,
    ) as exc:
        result = CliResult(
            ok=False,
            code=getattr(exc, "code", "SHEETS_SNAPSHOT_FAILED"),
            message="Sheets snapshot export failed.",
            details=[Issue(getattr(exc, "path", "snapshot"), getattr(exc, "reason", "FAILED"))],
        )
        emit_result(result, args.format)
        return getattr(exc, "exit_code", EXIT_EXTERNAL_FAILURE)
    except Exception:
        result = CliResult(
            ok=False,
            code="SHEETS_SNAPSHOT_UNEXPECTED",
            message="Sheets snapshot export failed unexpectedly.",
            details=[Issue("snapshot", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK


def run(args: argparse.Namespace, deps: SnapshotCliDependencies) -> CliResult:
    """Run snapshot export."""

    config_path = args.config.resolve()
    schema_path = args.schema.resolve()
    output_path = absolute_no_resolve(args.output)
    if args.max_rows_per_tab < 1:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_SCHEMA_INVALID",
            "max_rows_per_tab",
            "BELOW_MINIMUM",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    validate_output_path(output_path, force=args.force, protected_paths=(config_path, schema_path))
    validation = validate_config(config_path, "init", schema_path=DEFAULT_CONFIG_SCHEMA)
    if not validation.ok:
        raise SheetsSnapshotError(
            validation.code,
            validation.details[0].path if validation.details else "config",
            validation.details[0].reason if validation.details else "INVALID",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    config, issues = load_yaml_file(config_path)
    if issues or config is None:
        issue = issues[0] if issues else Issue("config", "READ_FAILED")
        raise SheetsSnapshotError("CONFIG_INVALID", issue.path, issue.reason, exit_code=2)
    sheets = config.get("sheets", {})
    spreadsheet_id = str(sheets.get("spreadsheet_id", "")) if isinstance(sheets, dict) else ""
    if spreadsheet_id == "":
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_SCHEMA_INVALID",
            "sheets.spreadsheet_id",
            "EMPTY",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    validate_spreadsheet_id(spreadsheet_id)
    schema = load_canonical_schema(schema_path)
    requested_tabs = parse_tabs(args.tabs)
    repository_root = resolve_repository_root(config_path, config)
    env_name = (
        str(sheets.get("service_account_credentials_env", "")) if isinstance(sheets, dict) else ""
    )
    credential_result = deps.credential_loader(env_name=env_name, repository_root=repository_root)
    service = deps.service_builder(credential_result.credentials)
    backend = deps.backend_factory(spreadsheet_id=spreadsheet_id, service=service)
    snapshot = SheetsSnapshotExporter(reader=backend, clock=deps.clock).export(
        schema,
        tabs=requested_tabs,
        max_rows_per_tab=args.max_rows_per_tab,
    )
    atomic_write_snapshot(output_path, snapshot.payload)
    data = {
        "spreadsheet_id": spreadsheet_id,
        "output": output_path.name,
        "tabs": list(snapshot.payload["tabs"].keys()),
        "row_counts": snapshot.summary.row_counts,
        "warnings": [warning.to_dict() for warning in snapshot.summary.warnings],
        "redactions": snapshot.summary.redactions,
    }
    code = (
        "SHEETS_SNAPSHOT_REDACTED_VALUES" if snapshot.summary.redactions else "SHEETS_SNAPSHOT_OK"
    )
    return CliResult(
        ok=True,
        code=code,
        message="Sheets snapshot exported.",
        details=[],
        data=data,
    )


def parse_tabs(value: str | None) -> tuple[str, ...] | None:
    """Parse comma-separated tabs."""

    if value is None:
        return None
    tabs = tuple(item for item in (part.strip() for part in value.split(",")) if item)
    if not tabs:
        raise SheetsSnapshotError(
            "SHEETS_SNAPSHOT_SCHEMA_INVALID",
            "tabs",
            "EMPTY",
            exit_code=EXIT_ARGUMENT_OR_SCHEMA,
        )
    return tabs


def absolute_no_resolve(path: Path) -> Path:
    """Return an absolute path without following a final symlink."""

    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded
