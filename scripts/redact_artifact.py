#!/usr/bin/env python3
"""Redact supported textual artifacts without exposing secret values."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from webapp_debug_skill.cli import CliResult, Issue, emit_result  # noqa: E402
from webapp_debug_skill.errors import (  # noqa: E402
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.redaction import RedactionError, redact_artifact  # noqa: E402

FORMATS = ("auto", "text", "json", "jsonl", "yaml", "har")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Redact a supported webapp-debug artifact.")
    parser.add_argument("--input", type=Path, required=True, help="Input artifact path.")
    parser.add_argument("--output", type=Path, required=True, help="Output artifact path.")
    parser.add_argument(
        "--secret-env",
        action="append",
        default=[],
        help="Environment variable name whose value should be redacted if set.",
    )
    parser.add_argument("--format", choices=FORMATS, default="auto", help="Input artifact format.")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing output/report path."
    )
    parser.add_argument("--report", type=Path, help="Optional JSON redaction report path.")
    parser.add_argument(
        "--format-output",
        choices=("text", "json"),
        default="text",
        help="CLI output format.",
    )
    return parser


def exit_code_for_error(error: RedactionError) -> int:
    """Map safe redaction errors to CLI exit codes."""

    if error.code in {"ARTIFACT_READ_FAILED", "ARTIFACT_WRITE_FAILED"}:
        return EXIT_EXTERNAL_FAILURE
    if error.code == "DEPENDENCY_MISSING":
        return EXIT_UNEXPECTED
    return EXIT_POLICY_BLOCKED


def main(argv: list[str] | None = None) -> int:
    """Run the redact artifact CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = redact_artifact(
            input_path=args.input,
            output_path=args.output,
            artifact_format=args.format,
            secret_env_names=args.secret_env,
            force=args.force,
            report_path=args.report,
        )
    except RedactionError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="Artifact redaction failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format_output)
        return exit_code_for_error(exc)
    except SystemExit:
        raise
    except Exception:
        result = CliResult(
            ok=False,
            code="UNEXPECTED_ERROR",
            message="Artifact redaction failed unexpectedly.",
            details=[Issue("internal", "UNEXPECTED")],
        )
        emit_result(result, args.format_output)
        return EXIT_UNEXPECTED

    result = CliResult(
        ok=True,
        code="OK",
        message="Artifact redaction completed.",
        details=[],
        data={"report": report.to_safe_dict()},
    )
    emit_result(result, args.format_output)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
