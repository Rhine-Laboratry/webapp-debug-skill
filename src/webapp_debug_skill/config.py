"""Config schema and semantic validation for webapp-debug."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_SCHEMA = REPO_ROOT / "skills/webapp-debug/assets/config.schema.json"

MODES = {"init", "discover", "test", "full", "resume", "report"}
CAPABILITIES = {
    "base",
    "sheets-read",
    "sheets-write",
    "browser",
    "seed",
    "cleanup",
    "destructive-reset",
}
DEFAULT_CAPABILITIES = {
    "init": ["base"],
    "discover": ["base"],
    "test": ["browser", "seed", "cleanup"],
    "full": ["browser", "seed", "cleanup"],
    "resume": ["base"],
    "report": ["sheets-read"],
}
DB_BACKED_CAPABILITIES = {"browser", "seed", "cleanup", "destructive-reset"}
SECRET_MARKERS = (
    "SECRET_MARKER",
    "BEGIN PRIVATE KEY",
    "PRIVATE KEY-----",
)


class DependencyMissingError(RuntimeError):
    """Raised when a required validation dependency is unavailable."""

    def __init__(self, package: str) -> None:
        super().__init__(package)
        self.package = package


def load_yaml_module() -> Any:
    """Load PyYAML with a deterministic dependency error."""

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) == "yaml":
            raise DependencyMissingError("PyYAML") from exc
        raise
    return yaml


def load_jsonschema_modules() -> tuple[Any, Any]:
    """Load jsonschema classes with a deterministic dependency error."""

    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) == "jsonschema":
            raise DependencyMissingError("jsonschema") from exc
        raise
    return Draft202012Validator, FormatChecker


def load_yaml_file(path: Path) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Load a YAML mapping without exposing parser internals."""

    try:
        yaml_module = load_yaml_module()
    except DependencyMissingError as exc:
        return None, [Issue("dependency", f"DEPENDENCY_MISSING:{exc.package}")]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, [Issue(str(path), "READ_FAILED")]

    try:
        data = yaml_module.safe_load(text)
    except yaml_module.YAMLError:
        return None, [Issue(str(path), "YAML_INVALID")]

    if not isinstance(data, dict):
        return None, [Issue(str(path), "YAML_MAPPING_REQUIRED")]

    return dict(data), []


def load_json_file(path: Path) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Load a JSON mapping without exposing parser internals."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, [Issue(str(path), "READ_FAILED")]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, [Issue(str(path), "JSON_INVALID")]

    if not isinstance(data, dict):
        return None, [Issue(str(path), "JSON_MAPPING_REQUIRED")]

    return data, []


def safe_path(parts: Iterable[Any]) -> str:
    """Convert a jsonschema path into a redacted dotted path."""

    converted: list[str] = []
    for part in parts:
        if isinstance(part, int):
            converted.append("[index]")
        else:
            converted.append(str(part))
    return ".".join(converted) if converted else "$"


def is_empty(value: Any) -> bool:
    """Return true only for missing/null/empty string, preserving 0 and false."""

    return value is None or (isinstance(value, str) and value == "")


def schema_error_reason(validator: str) -> str:
    """Map jsonschema validators to safe reason codes."""

    mapping = {
        "required": "REQUIRED",
        "additionalProperties": "UNKNOWN_KEY",
        "unevaluatedProperties": "UNKNOWN_KEY",
        "type": "INVALID_TYPE",
        "const": "INVALID_CONST",
        "enum": "INVALID_ENUM",
        "format": "INVALID_FORMAT",
        "minimum": "BELOW_MINIMUM",
        "maximum": "ABOVE_MAXIMUM",
        "minLength": "EMPTY",
        "minItems": "TOO_FEW_ITEMS",
        "uniqueItems": "DUPLICATE_ITEM",
        "allOf": "SCHEMA_RULE_FAILED",
        "anyOf": "SCHEMA_RULE_FAILED",
        "oneOf": "SCHEMA_RULE_FAILED",
    }
    return mapping.get(validator, "SCHEMA_RULE_FAILED")


def schema_errors_to_issues(errors: Sequence[Any]) -> list[Issue]:
    """Convert jsonschema errors to redacted issues."""

    issues: list[Issue] = []
    for error in errors:
        path = safe_path(error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, Mapping):
            for missing in sorted(set(error.validator_value) - set(error.instance)):
                issues.append(
                    Issue(f"{path}.{missing}" if path != "$" else str(missing), "REQUIRED")
                )
            continue
        if error.validator in {"additionalProperties", "unevaluatedProperties"}:
            issues.append(Issue(f"{path}.*" if path != "$" else "*", "UNKNOWN_KEY"))
            continue
        issues.append(Issue(path, schema_error_reason(str(error.validator))))
    return issues


def validate_json_schema(instance: Mapping[str, Any], schema: Mapping[str, Any]) -> list[Issue]:
    """Validate config against JSON Schema Draft 2020-12."""

    Draft202012Validator, FormatChecker = load_jsonschema_modules()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    return schema_errors_to_issues(errors)


def find_secret_markers(value: Any, path: str = "$") -> list[Issue]:
    """Detect known secret fixture markers without returning their values."""

    issues: list[Issue] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path != "$" else str(key)
            issues.extend(find_secret_markers(child, child_path))
    elif isinstance(value, list):
        for child in value:
            issues.extend(find_secret_markers(child, f"{path}.[index]"))
    elif isinstance(value, str):
        if any(marker in value for marker in SECRET_MARKERS):
            issues.append(Issue(path, "SECRET_VALUE_PRESENT"))
    return issues


def regex_issues(config: Mapping[str, Any]) -> list[Issue]:
    """Validate non-empty DB guard regex syntax."""

    database = config.get("database", {})
    if not isinstance(database, Mapping):
        return []

    issues: list[Issue] = []
    for key in ("expected_host_pattern", "expected_database_pattern"):
        value = database.get(key)
        if isinstance(value, str) and value:
            try:
                re.compile(value)
            except re.error:
                issues.append(Issue(f"database.{key}", "INVALID_REGEX"))
    return issues


def db_guard_issues(config: Mapping[str, Any]) -> list[Issue]:
    """Return DB guard completeness issues without reading secrets."""

    database = config.get("database", {})
    if not isinstance(database, Mapping):
        return [Issue("database", "REQUIRED")]

    sentinel = database.get("sentinel", {})
    if not isinstance(sentinel, Mapping):
        sentinel = {}

    issues: list[Issue] = []
    if is_empty(database.get("expected_host_pattern")):
        issues.append(Issue("database.expected_host_pattern", "EMPTY"))
    if is_empty(database.get("expected_database_pattern")):
        issues.append(Issue("database.expected_database_pattern", "EMPTY"))
    if sentinel.get("required") is not True:
        issues.append(Issue("database.sentinel.required", "MUST_BE_TRUE"))
    if is_empty(sentinel.get("query")):
        issues.append(Issue("database.sentinel.query", "EMPTY"))
    if is_empty(sentinel.get("expected_value")):
        issues.append(Issue("database.sentinel.expected_value", "EMPTY"))
    candidates = database.get("local_config_candidates")
    if not isinstance(candidates, list) or len(candidates) == 0:
        issues.append(Issue("database.local_config_candidates", "EMPTY"))
    issues.extend(regex_issues(config))
    return issues


def destructive_reset_issues(config: Mapping[str, Any]) -> list[Issue]:
    """Return destructive reset capability issues."""

    database = config.get("database", {})
    if not isinstance(database, Mapping):
        return [Issue("database", "REQUIRED")]

    issues = db_guard_issues(config)
    if database.get("classification") != "dedicated":
        issues.append(Issue("database.classification", "MUST_BE_DEDICATED"))
    if database.get("destructive_reset") is not True:
        issues.append(Issue("database.destructive_reset", "MUST_BE_TRUE"))
    if database.get("reset_scope") not in {"suite", "manual"}:
        issues.append(Issue("database.reset_scope", "MUST_BE_SUITE_OR_MANUAL"))
    if is_empty(database.get("reset_command")):
        issues.append(Issue("database.reset_command", "EMPTY"))
    return issues


def capability_list(mode: str, explicit_capabilities: Sequence[str] | None) -> list[str]:
    """Resolve default or explicit capabilities for a mode."""

    if explicit_capabilities:
        return list(explicit_capabilities)
    return list(DEFAULT_CAPABILITIES[mode])


def base_diagnostics(config: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Return non-blocking base diagnostics."""

    if mode == "report":
        return {}

    issues = db_guard_issues(config)
    if not issues:
        return {
            "db_guard": {
                "status": "READY",
                "reasons": [],
            }
        }

    data: dict[str, Any] = {
        "db_guard": {
            "status": "BLOCKED",
            "reasons": [issue.__dict__ for issue in issues],
        }
    }
    if mode == "discover":
        data["browser_discovery"] = {
            "status": "BLOCKED",
            "reasons": [issue.__dict__ for issue in issues],
        }
    return data


def validate_config(
    config_path: Path,
    mode: str,
    explicit_capabilities: Sequence[str] | None = None,
    schema_path: Path = DEFAULT_CONFIG_SCHEMA,
) -> CliResult:
    """Validate config structure and mode/capability-specific safety conditions."""

    if mode not in MODES:
        return CliResult(
            ok=False,
            code="ARGUMENT_INVALID",
            message="Invalid mode.",
            details=[Issue("mode", "INVALID_ENUM")],
        )

    capabilities = capability_list(mode, explicit_capabilities)
    invalid_capabilities = sorted(set(capabilities) - CAPABILITIES)
    if invalid_capabilities:
        return CliResult(
            ok=False,
            code="ARGUMENT_INVALID",
            message="Invalid capability.",
            details=[Issue("capability", "INVALID_ENUM")],
        )

    config, load_issues = load_yaml_file(config_path)
    if load_issues:
        code = (
            "DEPENDENCY_MISSING"
            if load_issues[0].reason.startswith("DEPENDENCY")
            else "CONFIG_INVALID"
        )
        return CliResult(False, code, "Config could not be loaded.", load_issues)
    assert config is not None

    schema, schema_load_issues = load_json_file(schema_path)
    if schema_load_issues:
        return CliResult(
            False, "CONFIG_SCHEMA_INVALID", "Config schema could not be loaded.", schema_load_issues
        )
    assert schema is not None

    try:
        schema_issues = validate_json_schema(config, schema)
    except DependencyMissingError as exc:
        return CliResult(
            False,
            "DEPENDENCY_MISSING",
            "jsonschema is required to validate config.",
            [Issue("dependency", exc.package)],
        )

    validation_issues = schema_issues + regex_issues(config) + find_secret_markers(config)
    if validation_issues:
        return CliResult(
            ok=False,
            code="CONFIG_VALIDATION_FAILED",
            message="Config validation failed.",
            details=validation_issues,
        )

    blocked_issues: list[Issue] = []
    if set(capabilities) & DB_BACKED_CAPABILITIES:
        blocked_issues.extend(db_guard_issues(config))
    if "destructive-reset" in capabilities:
        blocked_issues.extend(destructive_reset_issues(config))

    if blocked_issues:
        unique = {(issue.path, issue.reason): issue for issue in blocked_issues}
        return CliResult(
            ok=False,
            code="CONFIG_DB_GUARD_INCOMPLETE",
            message="Database-backed execution is blocked.",
            details=list(unique.values()),
            data={"capabilities": capabilities},
        )

    data: dict[str, Any] = {
        "mode": mode,
        "capabilities": capabilities,
    }
    if "base" in capabilities:
        data.update(base_diagnostics(config, mode))
    if "destructive-reset" in capabilities:
        data["runtime_confirmation"] = {
            "status": "REQUIRED",
            "reason": "EXPLICIT_CONFIRMATION_REQUIRED",
        }

    return CliResult(
        ok=True,
        code="OK",
        message="Config validation passed.",
        details=[],
        data=data,
    )
