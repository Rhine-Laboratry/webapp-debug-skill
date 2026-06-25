#!/usr/bin/env python3
"""Validate webapp-debug Google Sheets schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from webapp_debug_skill.cli import emit_result  # noqa: E402
from webapp_debug_skill.errors import (  # noqa: E402
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_OK,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.sheets_schema import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_SHEETS_META_SCHEMA,
    validate_sheets_schema,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Validate webapp-debug Google Sheets schema.")
    parser.add_argument(
        "--schema", type=Path, required=True, help="Google Sheets schema JSON path."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Config YAML path used for human-editable column allow-list.",
    )
    parser.add_argument(
        "--meta-schema",
        type=Path,
        default=DEFAULT_SHEETS_META_SCHEMA,
        help="Google Sheets schema meta-schema JSON path.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the Sheets schema validator."""

    parser = build_parser()
    args = parser.parse_args(argv)
    result = validate_sheets_schema(
        schema_path=args.schema.resolve(),
        config_path=args.config.resolve(),
        meta_schema_path=args.meta_schema.resolve(),
    )
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    if result.code == "DEPENDENCY_MISSING":
        return EXIT_UNEXPECTED
    return EXIT_ARGUMENT_OR_SCHEMA


if __name__ == "__main__":
    raise SystemExit(main())
