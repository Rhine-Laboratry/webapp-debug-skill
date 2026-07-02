#!/usr/bin/env python3
"""Check v0.2 release readiness without creating tags or publishing."""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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
from webapp_debug_skill.redaction import secret_findings  # noqa: E402

DEFAULT_VERSION = "0.2.0"
SECRET_MARKER = "SECRET_MARKER"
REQUIRED_SCRIPTS = (
    "scripts/validate_skill.py",
    "scripts/validate_config.py",
    "scripts/validate_sheets_schema.py",
    "scripts/init_sheets.py",
    "scripts/redact_artifact.py",
    "scripts/evaluate_coverage.py",
    "scripts/export_sheets_snapshot.py",
    "scripts/discover_cakephp_inventory.py",
    "scripts/release_check.py",
)
REQUIRED_FILES = (
    "AGENTS.md",
    ".github/workflows/ci.yml",
    ".agents/skills/webapp-debug/SKILL.md",
    ".claude/skills/webapp-debug/SKILL.md",
    "skills/webapp-debug/SKILL.md",
    "skills/webapp-debug/assets/google-sheets-schema.json",
    "skills/webapp-debug/assets/config.schema.json",
    "CHANGELOG.md",
    "docs/RELEASE_CHECKLIST.md",
)
RELEASE_FILES = (
    "README.md",
    "INSTALL.md",
    "CHANGELOG.md",
    "docs/RELEASE_CHECKLIST.md",
    ".github/workflows/ci.yml",
)
CREDENTIAL_FILE_SUFFIXES = (".pem", ".p12", ".key")
CREDENTIAL_FILE_FRAGMENTS = (
    "service-account",
    "credentials",
    "private_key",
)
CACHE_FILE_FRAGMENTS = (
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".egg-info/",
)
CONTENT_SECRET_MARKERS = (
    "PRIVATE KEY",
    "private_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "Authorization:",
    "Cookie:",
)
CONTENT_SCAN_ALLOWLIST_PREFIXES = (
    "tests/",
    "docs/IMPLEMENTATION_PLAN.md",
    "scripts/release_check.py",
    "src/webapp_debug_skill/config.py",
    "src/webapp_debug_skill/redaction.py",
)
CI_FORBIDDEN_STRINGS = (
    "secrets.",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE",
    "WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID",
    "WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID",
    "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION",
    "gcloud",
    "permissions.create",
)


class ReleaseCheckGitError(RuntimeError):
    """Safe git command failure."""


@dataclass(frozen=True)
class ReleaseCheckDependencies:
    """Injectable dependencies for unit tests."""

    repository_root: Path = REPO_ROOT
    git_ls_files: Callable[[Path], Sequence[str]] | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(description="Check release readiness without publishing.")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(
    argv: list[str] | None = None,
    deps: ReleaseCheckDependencies | None = None,
) -> int:
    """Run release readiness checks."""

    parser = build_parser()
    args = parser.parse_args(argv)
    dependencies = deps or ReleaseCheckDependencies()
    try:
        result = run(args.version, dependencies)
    except ReleaseCheckGitError:
        result = CliResult(
            ok=False,
            code="RELEASE_CHECK_GIT_FAILED",
            message="Release readiness check could not inspect tracked files.",
            details=[Issue("git", "LS_FILES_FAILED")],
        )
        emit_result(result, args.format)
        return EXIT_EXTERNAL_FAILURE
    except Exception:
        result = CliResult(
            ok=False,
            code="RELEASE_CHECK_UNEXPECTED",
            message="Release readiness check failed unexpectedly.",
            details=[Issue("release", "UNEXPECTED")],
        )
        emit_result(result, args.format)
        return EXIT_UNEXPECTED
    emit_result(result, args.format)
    return EXIT_OK if result.ok else EXIT_POLICY_BLOCKED


def run(version: str, deps: ReleaseCheckDependencies) -> CliResult:
    """Run checks and return a safe CLI result."""

    root = deps.repository_root.resolve()
    issues: list[Issue] = []
    expected_tag = f"v{version}"

    if not valid_version(version):
        issues.append(Issue("version", "INVALID"))

    expected_release_note = f"docs/RELEASE_NOTES_v{version}.md"
    required_files = (*REQUIRED_FILES, expected_release_note)
    issues.extend(check_required_files(root, required_files))
    issues.extend(check_required_scripts(root, REQUIRED_SCRIPTS))
    issues.extend(check_versions(root, version))
    issues.extend(check_changelog(root, version))
    issues.extend(check_release_note(root, expected_release_note, version, expected_tag))
    issues.extend(check_readme_install(root))
    issues.extend(check_ci_workflow(root))
    tracked_files = get_tracked_files(root, deps)
    issues.extend(check_tracked_file_names(tracked_files))
    issues.extend(check_tracked_file_contents(root, tracked_files))
    issues.extend(
        check_release_files_for_secret_markers(root, (*RELEASE_FILES, expected_release_note))
    )

    data: dict[str, Any] = {
        "version": version,
        "tag": expected_tag,
        "checked_files": len(required_files),
        "tracked_files": len(tracked_files),
    }
    if issues:
        return CliResult(
            ok=False,
            code="RELEASE_CHECK_FAILED",
            message="Release readiness checks failed.",
            details=issues,
            data=data,
        )
    return CliResult(
        ok=True,
        code="RELEASE_CHECK_OK",
        message="Release readiness checks passed.",
        details=[],
        data=data,
    )


def valid_version(value: str) -> bool:
    """Return whether value is a simple semantic version."""

    parts = value.split(".")
    return len(parts) == 3 and all(part.isdigit() and part != "" for part in parts)


def check_required_files(root: Path, required_files: Iterable[str]) -> list[Issue]:
    """Check required release files exist."""

    issues: list[Issue] = []
    for relative in required_files:
        if not (root / relative).is_file():
            issues.append(Issue(relative, "MISSING"))
    return issues


def check_required_scripts(root: Path, required_scripts: Iterable[str]) -> list[Issue]:
    """Check required scripts exist."""

    return [
        Issue(relative, "MISSING")
        for relative in required_scripts
        if not (root / relative).is_file()
    ]


def check_versions(root: Path, version: str) -> list[Issue]:
    """Check pyproject and package versions."""

    issues: list[Issue] = []
    pyproject_version = read_pyproject_version(root / "pyproject.toml")
    if pyproject_version != version:
        issues.append(Issue("pyproject.toml:project.version", "VERSION_MISMATCH"))
    init_version = read_init_version(root / "src/webapp_debug_skill/__init__.py")
    if init_version is not None and init_version != version:
        issues.append(Issue("src/webapp_debug_skill/__init__.py:__version__", "VERSION_MISMATCH"))
    return issues


def read_pyproject_version(path: Path) -> str | None:
    """Read project version from pyproject."""

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, Mapping):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def read_init_version(path: Path) -> str | None:
    """Read __version__ assignment without importing package code."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def check_changelog(root: Path, version: str) -> list[Issue]:
    """Check changelog references the target version."""

    text = safe_read_text(root / "CHANGELOG.md")
    if version not in text:
        return [Issue("CHANGELOG.md", "VERSION_MISSING")]
    added = section_between(text, "### Added", "### Changed")
    for phrase in (
        "JavaScript parsing",
        "Playwright Scenario generation",
        "Playwright runner orchestration",
    ):
        if phrase in added:
            return [Issue("CHANGELOG.md", "FUTURE_FEATURE_IN_ADDED")]
    return []


def check_release_note(root: Path, relative: str, version: str, tag: str) -> list[Issue]:
    """Check release note exists and references version/tag."""

    text = safe_read_text(root / relative)
    if not text:
        return []
    issues: list[Issue] = []
    if version not in text:
        issues.append(Issue(relative, "VERSION_MISSING"))
    if tag not in text:
        issues.append(Issue(relative, "TAG_MISSING"))
    for phrase in (
        "Dynamic browser discovery and Sheets sync from discovery output are not implemented",
        "Playwright scenario generation is not implemented",
    ):
        if phrase not in text:
            issues.append(Issue(relative, "KNOWN_LIMITATION_MISSING"))
    return issues


def check_readme_install(root: Path) -> list[Issue]:
    """Check README and INSTALL mention major CLI names."""

    required = REQUIRED_SCRIPTS
    issues: list[Issue] = []
    for relative in ("README.md", "INSTALL.md"):
        text = safe_read_text(root / relative)
        for script in required:
            if script not in text:
                issues.append(Issue(relative, f"MISSING_{Path(script).name}"))
    return issues


def check_ci_workflow(root: Path) -> list[Issue]:
    """Check CI workflow is safe and includes release_check."""

    path = root / ".github/workflows/ci.yml"
    text = safe_read_text(path)
    if not text:
        return []
    issues: list[Issue] = []
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        return [Issue(".github/workflows/ci.yml", "YAML_INVALID")]
    if not isinstance(workflow, Mapping):
        return [Issue(".github/workflows/ci.yml", "OBJECT_REQUIRED")]
    rendered = "\n".join(walk_strings(workflow))
    for forbidden in CI_FORBIDDEN_STRINGS:
        if forbidden in rendered:
            issues.append(Issue(".github/workflows/ci.yml", "FORBIDDEN_SECRET_OR_SERVICE"))
            break
    if "python scripts/release_check.py --version 0.2.0" not in rendered:
        issues.append(Issue(".github/workflows/ci.yml", "RELEASE_CHECK_MISSING"))
    if "secrets" in workflow:
        issues.append(Issue(".github/workflows/ci.yml", "SECRETS_DECLARED"))
    return issues


def get_tracked_files(root: Path, deps: ReleaseCheckDependencies) -> list[str]:
    """Return git tracked files."""

    if deps.git_ls_files is not None:
        try:
            return sorted(str(path) for path in deps.git_ls_files(root))
        except Exception:
            raise ReleaseCheckGitError("git ls-files failed") from None
    try:
        process = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        raise ReleaseCheckGitError("git ls-files failed") from None
    return sorted(line for line in process.stdout.splitlines() if line)


def check_tracked_file_names(tracked_files: Sequence[str]) -> list[Issue]:
    """Reject credential-like and cache-like tracked paths."""

    issues: list[Issue] = []
    for relative in tracked_files:
        normalized = relative.replace("\\", "/")
        basename = Path(normalized).name.lower()
        lower = normalized.lower()
        if any(fragment in lower for fragment in CACHE_FILE_FRAGMENTS):
            issues.append(Issue(safe_path(normalized), "TRACKED_CACHE_FILE"))
            continue
        if any(lower.endswith(suffix) for suffix in CREDENTIAL_FILE_SUFFIXES) or (
            basename.endswith(".json")
            and any(fragment in basename for fragment in CREDENTIAL_FILE_FRAGMENTS)
        ):
            issues.append(Issue(safe_path(normalized), "TRACKED_CREDENTIAL_FILE"))
    return issues


def check_tracked_file_contents(root: Path, tracked_files: Sequence[str]) -> list[Issue]:
    """Reject obvious raw secret markers outside explicit safe implementation/test files."""

    issues: list[Issue] = []
    for relative in tracked_files:
        normalized = relative.replace("\\", "/")
        if is_content_scan_allowlisted(normalized):
            continue
        path = root / normalized
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            issues.append(Issue(safe_path(normalized), "READ_FAILED"))
            continue
        if any(marker in text for marker in CONTENT_SECRET_MARKERS):
            issues.append(Issue(safe_path(normalized), "SECRET_MARKER_PRESENT"))
    return issues


def check_release_files_for_secret_markers(root: Path, release_files: Sequence[str]) -> list[Issue]:
    """Check release-facing files do not contain test secret markers."""

    issues: list[Issue] = []
    for relative in release_files:
        path = root / relative
        if not path.is_file():
            continue
        text = safe_read_text(path)
        if SECRET_MARKER in text or secret_findings(text):
            issues.append(Issue(relative, "SECRET_MARKER_PRESENT"))
    return issues


def is_content_scan_allowlisted(relative: str) -> bool:
    """Return whether a tracked file may contain safe test/implementation marker literals."""

    return any(
        relative == prefix or relative.startswith(prefix)
        for prefix in CONTENT_SCAN_ALLOWLIST_PREFIXES
    )


def safe_read_text(path: Path) -> str:
    """Read text without surfacing raw file contents in exceptions."""

    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def section_between(text: str, start: str, end: str) -> str:
    """Return text section between headings."""

    if start not in text:
        return ""
    tail = text.split(start, 1)[1]
    if end not in tail:
        return tail
    return tail.split(end, 1)[0]


def walk_strings(value: Any) -> Iterable[str]:
    """Yield scalar strings from a nested structure."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif value is not None:
        yield str(value)


def safe_path(value: str) -> str:
    """Return a safe path for diagnostics."""

    return "release" if secret_findings(value) else value


if __name__ == "__main__":
    raise SystemExit(main())
