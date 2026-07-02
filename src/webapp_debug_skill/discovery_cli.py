"""CLI for read-only CakePHP Inventory discovery."""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from webapp_debug_skill.cakephp_discovery import CakePHPDiscoveryError, discover_cakephp
from webapp_debug_skill.cli import CliResult, Issue, emit_result
from webapp_debug_skill.errors import (
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_EXTERNAL_FAILURE,
    EXIT_OK,
    EXIT_POLICY_BLOCKED,
    EXIT_UNEXPECTED,
)
from webapp_debug_skill.inventory_model import dumps_snapshot


@dataclass(frozen=True)
class DiscoveryCliDependencies:
    """Injectable dependencies for tests."""

    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    discover: Callable[..., Any] = discover_cakephp


def build_parser() -> argparse.ArgumentParser:
    """Build parser."""

    parser = argparse.ArgumentParser(description="Discover CakePHP Inventory via static analysis.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-files", type=int, default=5000)
    parser.add_argument("--include-plugins", action="store_true")
    parser.add_argument(
        "--cakephp-version",
        choices=("auto", "2", "3", "4", "5", "generic"),
        default="auto",
    )
    return parser


def main(
    argv: list[str] | None = None,
    deps: DiscoveryCliDependencies | None = None,
) -> int:
    """Run CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or DiscoveryCliDependencies()
    try:
        result = run(args, dependencies)
    except CakePHPDiscoveryError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="CakePHP static discovery failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return EXIT_POLICY_BLOCKED
    except DiscoveryCliError as exc:
        result = CliResult(
            ok=False,
            code=exc.code,
            message="CakePHP static discovery failed.",
            details=[Issue(exc.path, exc.reason)],
        )
        emit_result(result, args.format)
        return exc.exit_code
    except Exception:
        result = CliResult(
            ok=False,
            code="DISCOVERY_UNEXPECTED",
            message="CakePHP static discovery failed unexpectedly.",
            details=[Issue("discovery", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK


@dataclass(frozen=True)
class DiscoveryCliError(RuntimeError):
    """Safe CLI error."""

    code: str
    path: str
    reason: str
    exit_code: int = EXIT_POLICY_BLOCKED


def run(args: argparse.Namespace, deps: DiscoveryCliDependencies) -> CliResult:
    """Run discovery and write snapshot."""

    root = args.root.expanduser().resolve(strict=False)
    output = absolute_no_resolve(args.output)
    if not root.exists():
        raise DiscoveryCliError(
            "DISCOVERY_ROOT_INVALID",
            "root",
            "MISSING",
            EXIT_ARGUMENT_OR_SCHEMA,
        )
    if not root.is_dir():
        raise DiscoveryCliError(
            "DISCOVERY_ROOT_INVALID",
            "root",
            "NOT_DIRECTORY",
            EXIT_ARGUMENT_OR_SCHEMA,
        )
    if args.max_files < 1:
        raise DiscoveryCliError(
            "DISCOVERY_MAX_FILES_INVALID",
            "max_files",
            "BELOW_MINIMUM",
            EXIT_ARGUMENT_OR_SCHEMA,
        )
    validate_output_path(output, force=args.force, root=root)
    try:
        result = deps.discover(
            root,
            include_plugins=args.include_plugins,
            cakephp_version=args.cakephp_version,
            max_files=args.max_files,
        )
        payload = result.to_payload(clock=deps.clock)
        validate_output_not_source(output, root, result.source_paths)
        atomic_write_json(output, payload)
    except CakePHPDiscoveryError:
        raise
    except OSError:
        raise DiscoveryCliError(
            "DISCOVERY_WRITE_FAILED",
            "output",
            "WRITE_FAILED",
            EXIT_EXTERNAL_FAILURE,
        ) from None
    data = {
        "output": output.name,
        "cakephp_version": payload["source"]["cakephp_version"],
        "inventory_count": payload["summary"]["inventory_count"],
        "discovery_gaps": payload["summary"]["discovery_gaps"],
        "files_scanned": payload["summary"]["files_scanned"],
    }
    return CliResult(
        ok=True,
        code="DISCOVERY_OK",
        message="CakePHP static discovery completed.",
        details=[],
        data=data,
    )


def validate_output_path(path: Path, *, force: bool, root: Path) -> None:
    """Validate output path before writing."""

    try:
        stat_result = path.lstat()
    except OSError:
        stat_result = None
    if stat_result is not None:
        if stat.S_ISLNK(stat_result.st_mode):
            raise DiscoveryCliError("DISCOVERY_OUTPUT_UNSAFE", "output", "SYMLINK_REJECTED")
        if not stat.S_ISREG(stat_result.st_mode):
            raise DiscoveryCliError("DISCOVERY_OUTPUT_UNSAFE", "output", "NOT_REGULAR_FILE")
        if not force:
            raise DiscoveryCliError("DISCOVERY_OUTPUT_EXISTS", "output", "EXISTS")
    output_resolved = path.resolve(strict=False)
    try:
        output_resolved.relative_to(root.resolve(strict=False))
    except ValueError:
        return


def validate_output_not_source(path: Path, root: Path, source_paths: tuple[str, ...]) -> None:
    """Reject output targets that are one of the scanned source files."""

    output_resolved = path.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    for source_path in source_paths:
        if output_resolved == (root_resolved / source_path).resolve(strict=False):
            raise DiscoveryCliError("DISCOVERY_OUTPUT_UNSAFE", "output", "SOURCE_PATH_REJECTED")
    if output_resolved.name in {"routes.php", "composer.json"} or output_resolved.name.endswith(
        "Controller.php"
    ):
        try:
            output_resolved.relative_to(root_resolved)
        except ValueError:
            return
        raise DiscoveryCliError("DISCOVERY_OUTPUT_UNSAFE", "output", "SOURCE_PATH_REJECTED")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON payload."""

    rendered = dumps_snapshot(payload)
    tmp_path: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
        fsync_dir(path.parent)
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
    """Return absolute path without following a final symlink."""

    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded
