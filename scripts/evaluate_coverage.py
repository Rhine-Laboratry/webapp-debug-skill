#!/usr/bin/env python3
"""Evaluate bounded discovery coverage from a local Inventory JSON file."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from webapp_debug_skill.cli import CliResult, Issue, emit_result  # noqa: E402
from webapp_debug_skill.config import DEFAULT_CONFIG_SCHEMA, load_yaml_file, validate_config  # noqa: E402
from webapp_debug_skill.coverage import (  # noqa: E402
    CoverageError,
    CoverageOutcome,
    CoveragePolicy,
    evaluate_inventory,
)
from webapp_debug_skill.errors import (  # noqa: E402
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Evaluate webapp-debug coverage gate.")
    parser.add_argument("--config", type=Path, required=True, help="Config YAML path.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inventory-json", type=Path, help="Local Inventory JSON path.")
    source.add_argument(
        "--from-sheets",
        action="store_true",
        help="Reserved for Phase 4B; not implemented in Phase 4A.",
    )
    parser.add_argument(
        "--current-pass",
        type=int,
        default=None,
        help="Current discovery pass number.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the coverage evaluator."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.from_sheets:
            result = CliResult(
                ok=False,
                code="COVERAGE_FROM_SHEETS_NOT_IMPLEMENTED",
                message="Google Sheets coverage input is not implemented in Phase 4A.",
                details=[Issue("from_sheets", "NOT_IMPLEMENTED")],
            )
            emit_result(result, args.format)
            return EXIT_ARGUMENT_OR_SCHEMA

        config_validation = validate_config(
            config_path=args.config.resolve(),
            mode="init",
            schema_path=DEFAULT_CONFIG_SCHEMA,
        )
        if not config_validation.ok:
            emit_result(config_validation, args.format)
            return (
                EXIT_POLICY_BLOCKED
                if config_validation.code == "CONFIG_DB_GUARD_INCOMPLETE"
                else EXIT_ARGUMENT_OR_SCHEMA
            )
        config, config_issues = load_yaml_file(args.config.resolve())
        if config_issues or config is None:
            issue = config_issues[0] if config_issues else Issue("config", "READ_FAILED")
            result = CliResult(
                ok=False,
                code="CONFIG_INVALID",
                message="Config could not be loaded.",
                details=[issue],
            )
            emit_result(result, args.format)
            return EXIT_ARGUMENT_OR_SCHEMA

        policy = CoveragePolicy.from_config(config, current_pass=args.current_pass)
        rows = load_inventory_rows(args.inventory_json.resolve())
        report = evaluate_inventory(rows, policy)
    except CoverageError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Coverage policy is invalid.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return EXIT_ARGUMENT_OR_SCHEMA
    except InventoryLoadError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Inventory JSON could not be loaded.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return EXIT_ARGUMENT_OR_SCHEMA
    except SystemExit:
        raise
    except Exception:
        result = CliResult(
            ok=False,
            code="COVERAGE_UNEXPECTED",
            message="Coverage evaluation failed unexpectedly.",
            details=[Issue("coverage", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED

    data = report.to_dict()
    ok = report.outcome in {CoverageOutcome.PASS, CoverageOutcome.PASS_WITH_GAPS}
    result = CliResult(
        ok=ok,
        code=report.outcome.value,
        message="Coverage gate evaluated.",
        details=[Issue("coverage", reason) for reason in report.reason_codes],
        data=data,
    )
    emit_result(result, args.format)
    if ok:
        return EXIT_OK
    return EXIT_POLICY_BLOCKED


class InventoryLoadError(RuntimeError):
    """Safe inventory JSON loading error."""

    def __init__(self, code: str, path: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.path = path
        self.reason = reason


def load_inventory_rows(path: Path) -> list[Mapping[str, Any]]:
    """Load supported Inventory JSON shapes."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise InventoryLoadError(
            "INVENTORY_JSON_INVALID", "inventory_json", "READ_FAILED"
        ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise InventoryLoadError(
            "INVENTORY_JSON_INVALID", "inventory_json", "JSON_INVALID"
        ) from None

    if isinstance(data, Mapping):
        rows = data.get("Inventory", data.get("inventory"))
    else:
        rows = data
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise InventoryLoadError("INVENTORY_JSON_INVALID", "inventory_json", "LIST_REQUIRED")
    if not all(isinstance(row, Mapping) for row in rows):
        raise InventoryLoadError("INVENTORY_JSON_INVALID", "inventory_json", "ROW_OBJECT_REQUIRED")
    return list(rows)


if __name__ == "__main__":
    raise SystemExit(main())
