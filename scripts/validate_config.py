#!/usr/bin/env python3
"""Validate webapp-debug config schema and mode safety."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from webapp_debug_skill.cli import emit_result  # noqa: E402
from webapp_debug_skill.config import (  # noqa: E402
    CAPABILITIES,
    DEFAULT_CONFIG_SCHEMA,
    MODES,
    validate_config,
)
from webapp_debug_skill.errors import (  # noqa: E402
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Validate webapp-debug config.")
    parser.add_argument("--config", type=Path, required=True, help="Config YAML path.")
    parser.add_argument(
        "--mode",
        choices=sorted(MODES),
        required=True,
        help="Execution mode to validate.",
    )
    parser.add_argument(
        "--capability",
        action="append",
        choices=sorted(CAPABILITIES),
        help="Capability to validate. May be repeated. Defaults depend on --mode.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_CONFIG_SCHEMA,
        help="Config JSON Schema path.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the config validator."""

    parser = build_parser()
    args = parser.parse_args(argv)
    result = validate_config(
        config_path=args.config.resolve(),
        mode=args.mode,
        explicit_capabilities=args.capability,
        schema_path=args.schema.resolve(),
    )
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    if result.code == "CONFIG_DB_GUARD_INCOMPLETE":
        return EXIT_POLICY_BLOCKED
    if result.code == "DEPENDENCY_MISSING":
        return EXIT_UNEXPECTED
    return EXIT_ARGUMENT_OR_SCHEMA


if __name__ == "__main__":
    raise SystemExit(main())
