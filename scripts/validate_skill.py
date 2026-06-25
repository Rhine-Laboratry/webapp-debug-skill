#!/usr/bin/env python3
"""Validate webapp-debug Skill metadata and wrappers."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from webapp_debug_skill.cli import CliResult, Issue, emit_result  # noqa: E402
from webapp_debug_skill.errors import (  # noqa: E402
    EXIT_ARGUMENT_OR_SCHEMA,
    EXIT_OK,
    EXIT_UNEXPECTED,
)

EXPECTED_DESCRIPTION = (
    "コードベースとブラウザからWebアプリの機能を棚卸しし、日本語Scenario、Playwrightテスト、"
    "Google Sheetsの進捗・不具合記録を生成する。init、discover、test、full、resume、"
    "reportを明示指定した場合に使用する。"
)
EXPECTED_DEFAULT_PROMPT = "$webapp-debug init"
CANONICAL_ALLOWED_KEYS = {"name", "description"}
CODEX_ALLOWED_KEYS = {"name", "description"}
CLAUDE_ALLOWED_KEYS = {
    "name",
    "description",
    "disable-model-invocation",
    "argument-hint",
}
CLAUDE_REQUIRED_KEYS = CLAUDE_ALLOWED_KEYS
WRAPPER_CANONICAL_RELATIVE_PATH = "../../../skills/webapp-debug/SKILL.md"


class DependencyMissingError(RuntimeError):
    """Raised when a required parser dependency is unavailable."""

    def __init__(self, package: str) -> None:
        super().__init__(package)
        self.package = package


def load_yaml_module() -> Any:
    """Load PyYAML and return the module, with a controlled error if missing."""

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) == "yaml":
            raise DependencyMissingError("PyYAML") from exc
        raise
    return yaml


def parse_frontmatter(path: Path, yaml_module: Any, issues: list[Issue]) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a Skill file."""

    if not path.exists():
        issues.append(Issue(str(path), "missing file"))
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        issues.append(Issue(str(path), "unable to read file"))
        return None

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        issues.append(Issue(str(path), "missing YAML frontmatter"))
        return None

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break

    if end_index is None:
        issues.append(Issue(str(path), "unterminated YAML frontmatter"))
        return None

    frontmatter = "\n".join(lines[1:end_index])
    try:
        data = yaml_module.safe_load(frontmatter) or {}
    except yaml_module.YAMLError:
        issues.append(Issue(str(path), "invalid YAML frontmatter"))
        return None

    if not isinstance(data, dict):
        issues.append(Issue(str(path), "frontmatter must be a mapping"))
        return None

    return dict(data)


def load_yaml_file(path: Path, yaml_module: Any, issues: list[Issue]) -> dict[str, Any] | None:
    """Load a YAML file as a mapping."""

    if not path.exists():
        issues.append(Issue(str(path), "missing file"))
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        issues.append(Issue(str(path), "unable to read file"))
        return None

    try:
        data = yaml_module.safe_load(text) or {}
    except yaml_module.YAMLError:
        issues.append(Issue(str(path), "invalid YAML"))
        return None

    if not isinstance(data, dict):
        issues.append(Issue(str(path), "YAML document must be a mapping"))
        return None

    return dict(data)


def check_name_matches_directory(
    path: Path, metadata: Mapping[str, Any], issues: list[Issue]
) -> None:
    """Validate that frontmatter name matches the Skill directory name."""

    name = metadata.get("name")
    expected = path.parent.name
    if name != expected:
        issues.append(Issue(str(path), f"name must match directory name '{expected}'"))


def check_description(path: Path, metadata: Mapping[str, Any], issues: list[Issue]) -> None:
    """Validate that the Skill description is the shared v0.2 description."""

    if metadata.get("description") != EXPECTED_DESCRIPTION:
        issues.append(Issue(str(path), "description does not match the canonical description"))


def check_allowed_keys(
    path: Path,
    metadata: Mapping[str, Any],
    allowed: set[str],
    issues: list[Issue],
) -> None:
    """Reject frontmatter keys outside an allowed set."""

    extra = sorted(set(metadata) - allowed)
    if extra:
        joined = ", ".join(extra)
        issues.append(Issue(str(path), f"frontmatter contains disallowed key(s): {joined}"))


def check_required_keys(
    path: Path,
    metadata: Mapping[str, Any],
    required: set[str],
    issues: list[Issue],
) -> None:
    """Require specific frontmatter keys."""

    missing = sorted(required - set(metadata))
    if missing:
        joined = ", ".join(missing)
        issues.append(Issue(str(path), f"frontmatter is missing required key(s): {joined}"))


def check_openai_yaml(path: Path, data: Mapping[str, Any] | None, issues: list[Issue]) -> None:
    """Validate Codex openai.yaml policy and default prompt."""

    if data is None:
        return

    interface = data.get("interface")
    if not isinstance(interface, Mapping):
        issues.append(Issue(str(path), "interface must be a mapping"))
        return

    if interface.get("default_prompt") != EXPECTED_DEFAULT_PROMPT:
        issues.append(Issue(str(path), "interface.default_prompt must be $webapp-debug init"))

    policy = data.get("policy")
    if not isinstance(policy, Mapping):
        issues.append(Issue(str(path), "policy must be a mapping"))
        return

    if policy.get("allow_implicit_invocation") is not False:
        issues.append(Issue(str(path), "policy.allow_implicit_invocation must be false"))


def check_wrapper_path(path: Path, issues: list[Issue]) -> None:
    """Validate wrapper text points at the canonical Skill with a resolvable relative path."""

    if not path.exists():
        return

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        issues.append(Issue(str(path), "unable to read wrapper text"))
        return

    if WRAPPER_CANONICAL_RELATIVE_PATH not in text:
        issues.append(Issue(str(path), "canonical Skill relative path is missing or changed"))
        return

    resolved = (path.parent / WRAPPER_CANONICAL_RELATIVE_PATH).resolve()
    if not resolved.exists():
        issues.append(Issue(str(path), "canonical Skill relative path does not resolve"))


def check_readme_paths(root: Path, issues: list[Issue]) -> None:
    """Validate README documents the wrapper paths that exist in the repo."""

    readme_path = root / "README.md"
    if not readme_path.exists():
        issues.append(Issue(str(readme_path), "missing file"))
        return

    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError:
        issues.append(Issue(str(readme_path), "unable to read file"))
        return

    expected_paths = [
        ".agents/skills/webapp-debug/SKILL.md",
        ".claude/skills/webapp-debug/SKILL.md",
        "skills/webapp-debug/SKILL.md",
    ]
    for relative_path in expected_paths:
        if relative_path not in text:
            issues.append(Issue(str(readme_path), f"README does not mention {relative_path}"))
        if not (root / relative_path).exists():
            issues.append(Issue(str(root / relative_path), "README-documented path is missing"))

    if "git ls-files .agents .claude" not in text:
        issues.append(Issue(str(readme_path), "README must document dot-directory tracking check"))


def validate_root(root: Path) -> CliResult:
    """Validate Skill metadata under a repository root."""

    try:
        yaml_module = load_yaml_module()
    except DependencyMissingError as exc:
        return CliResult(
            ok=False,
            code="DEPENDENCY_MISSING",
            message=f"{exc.package} is required to parse YAML. Install project dependencies.",
            details=[Issue("dependency", exc.package)],
        )

    issues: list[Issue] = []
    canonical_skill = root / "skills/webapp-debug/SKILL.md"
    codex_skill = root / ".agents/skills/webapp-debug/SKILL.md"
    claude_skill = root / ".claude/skills/webapp-debug/SKILL.md"
    canonical_openai = root / "skills/webapp-debug/agents/openai.yaml"
    codex_openai = root / ".agents/skills/webapp-debug/agents/openai.yaml"

    canonical_metadata = parse_frontmatter(canonical_skill, yaml_module, issues)
    codex_metadata = parse_frontmatter(codex_skill, yaml_module, issues)
    claude_metadata = parse_frontmatter(claude_skill, yaml_module, issues)

    for path, metadata in [
        (canonical_skill, canonical_metadata),
        (codex_skill, codex_metadata),
        (claude_skill, claude_metadata),
    ]:
        if metadata is None:
            continue
        check_name_matches_directory(path, metadata, issues)
        check_description(path, metadata, issues)

    if canonical_metadata is not None:
        check_allowed_keys(canonical_skill, canonical_metadata, CANONICAL_ALLOWED_KEYS, issues)
        check_required_keys(canonical_skill, canonical_metadata, CANONICAL_ALLOWED_KEYS, issues)

    if codex_metadata is not None:
        check_allowed_keys(codex_skill, codex_metadata, CODEX_ALLOWED_KEYS, issues)
        check_required_keys(codex_skill, codex_metadata, CODEX_ALLOWED_KEYS, issues)

    if claude_metadata is not None:
        check_allowed_keys(claude_skill, claude_metadata, CLAUDE_ALLOWED_KEYS, issues)
        check_required_keys(claude_skill, claude_metadata, CLAUDE_REQUIRED_KEYS, issues)

    canonical_openai_data = load_yaml_file(canonical_openai, yaml_module, issues)
    codex_openai_data = load_yaml_file(codex_openai, yaml_module, issues)
    check_openai_yaml(canonical_openai, canonical_openai_data, issues)
    check_openai_yaml(codex_openai, codex_openai_data, issues)

    if canonical_openai_data is not None and codex_openai_data is not None:
        if canonical_openai_data != codex_openai_data:
            issues.append(
                Issue(str(codex_openai), "Codex openai.yaml differs from canonical openai.yaml")
            )

    check_wrapper_path(codex_skill, issues)
    check_wrapper_path(claude_skill, issues)
    check_readme_paths(root, issues)

    if issues:
        return CliResult(
            ok=False,
            code="SKILL_VALIDATION_FAILED",
            message="Skill metadata validation failed.",
            details=issues,
        )

    return CliResult(
        ok=True,
        code="OK",
        message="Skill metadata validation passed.",
        details=[],
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(description="Validate webapp-debug Skill metadata.")
    parser.add_argument("--root", type=Path, default=Path("."), help="Repository root to validate.")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the validator CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    result = validate_root(args.root.resolve())
    emit_result(result, args.format)
    if result.ok:
        return EXIT_OK
    if result.code == "DEPENDENCY_MISSING":
        return EXIT_UNEXPECTED
    return EXIT_ARGUMENT_OR_SCHEMA


if __name__ == "__main__":
    raise SystemExit(main())
