"""Small shared helpers for deterministic CLI output."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, TextIO


@dataclass(frozen=True)
class Issue:
    """A redacted validation issue."""

    path: str
    reason: str


@dataclass(frozen=True)
class CliResult:
    """Machine-readable CLI result."""

    ok: bool
    code: str
    message: str
    details: list[Issue]
    data: dict[str, Any] = field(default_factory=dict)


def emit_result(result: CliResult, output_format: str, stream: TextIO | None = None) -> None:
    """Write a result in text or JSON format."""

    target = stream if stream is not None else sys.stdout
    if output_format == "json":
        target.write(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        target.write("\n")
        return

    if result.ok:
        target.write(f"{result.code}: {result.message}\n")
        for key, value in result.data.items():
            target.write(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}\n")
        return

    target.write(f"{result.code}: {result.message}\n")
    for issue in result.details:
        target.write(f"- {issue.path}: {issue.reason}\n")
    for key, value in result.data.items():
        target.write(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}\n")
