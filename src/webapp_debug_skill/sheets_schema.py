"""Google Sheets schema meta-validation for webapp-debug."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from webapp_debug_skill.cli import CliResult, Issue
from webapp_debug_skill.config import (
    DEFAULT_CONFIG_SCHEMA,
    DependencyMissingError,
    load_json_file,
    load_jsonschema_modules,
    load_yaml_file,
    schema_errors_to_issues,
    validate_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHEETS_META_SCHEMA = (
    REPO_ROOT / "skills/webapp-debug/assets/google-sheets-schema.schema.json"
)
DEFAULT_CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"

REQUIRED_TABS = {
    "Metadata",
    "Configuration",
    "Inventory",
    "Scenarios",
    "Test Runs",
    "Defects",
    "Evidence",
}
DEFINED_COLUMN_TYPES = {
    "string",
    "multiline",
    "enum",
    "boolean",
    "integer",
    "datetime",
    "date",
    "string[]",
}
APPEND_ONLY_IDENTIFIERS = {
    "Test Runs": {"run_id", "attempt_id"},
    "Evidence": {"evidence_id", "run_id"},
}
METADATA_REQUIRED_COLUMNS = {"key", "value", "updated_at"}


def validate_json_schema(instance: Mapping[str, Any], schema: Mapping[str, Any]) -> list[Issue]:
    """Validate Sheets schema JSON against its meta-schema."""

    Draft202012Validator, _FormatChecker = load_jsonschema_modules()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    return schema_errors_to_issues(errors)


def column_names(columns: Sequence[Any]) -> list[str]:
    """Return column names from valid-looking tuples."""

    names: list[str] = []
    for column in columns:
        if isinstance(column, list) and len(column) >= 1 and isinstance(column[0], str):
            names.append(column[0])
    return names


def duplicate_values(values: Iterable[str]) -> set[str]:
    """Return duplicated string values."""

    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def semantic_schema_issues(schema: Mapping[str, Any]) -> list[Issue]:
    """Run semantic checks that JSON Schema cannot express cleanly."""

    issues: list[Issue] = []
    tabs = schema.get("tabs")
    if not isinstance(tabs, list):
        return [Issue("tabs", "INVALID_TYPE")]

    tab_names = [
        tab.get("name")
        for tab in tabs
        if isinstance(tab, Mapping) and isinstance(tab.get("name"), str)
    ]
    for duplicate in sorted(duplicate_values(tab_names)):
        issues.append(Issue("tabs", "DUPLICATE_TAB"))

    missing_tabs = sorted(REQUIRED_TABS - set(tab_names))
    for tab_name in missing_tabs:
        issues.append(Issue(f"tabs.{tab_name}", "REQUIRED_TAB_MISSING"))

    for index, tab in enumerate(tabs):
        if not isinstance(tab, Mapping):
            continue
        tab_name = tab.get("name")
        tab_path = f"tabs.[index:{index}]"
        columns = tab.get("columns")
        if not isinstance(columns, list):
            continue

        names = column_names(columns)
        for duplicate in sorted(duplicate_values(names)):
            issues.append(Issue(f"{tab_path}.columns", "DUPLICATE_COLUMN"))

        for column_index, column in enumerate(columns):
            path = f"{tab_path}.columns.[index:{column_index}]"
            if not isinstance(column, list) or len(column) != 4:
                issues.append(Issue(path, "COLUMN_TUPLE_LENGTH"))
                continue
            name, column_type, required, human_editable = column
            if not isinstance(name, str) or name == "":
                issues.append(Issue(f"{path}.name", "COLUMN_NAME_INVALID"))
            if column_type not in DEFINED_COLUMN_TYPES:
                issues.append(Issue(f"{path}.type", "COLUMN_TYPE_INVALID"))
            if not isinstance(required, bool):
                issues.append(Issue(f"{path}.required", "INVALID_TYPE"))
            if not isinstance(human_editable, bool):
                issues.append(Issue(f"{path}.human_editable", "INVALID_TYPE"))

        if tab.get("row_policy") == "append-only" and isinstance(tab_name, str):
            required_identifiers = APPEND_ONLY_IDENTIFIERS.get(tab_name)
            if required_identifiers is None:
                issues.append(Issue(f"{tab_path}.row_policy", "APPEND_ONLY_IDENTIFIER_UNDEFINED"))
            elif not required_identifiers.issubset(set(names)):
                issues.append(Issue(f"{tab_path}.columns", "APPEND_ONLY_IDENTIFIER_MISSING"))

        if tab_name == "Metadata" and not METADATA_REQUIRED_COLUMNS.issubset(set(names)):
            issues.append(Issue(f"{tab_path}.columns", "METADATA_LOCK_COLUMNS_MISSING"))

    return issues


def load_human_editable_columns(config_path: Path) -> tuple[set[str] | None, list[Issue]]:
    """Load the allowed human editable columns from config."""

    config_result = validate_config(config_path, mode="init", schema_path=DEFAULT_CONFIG_SCHEMA)
    if not config_result.ok and config_result.code == "DEPENDENCY_MISSING":
        return None, config_result.details
    if not config_result.ok and config_result.code == "CONFIG_VALIDATION_FAILED":
        return None, [Issue("config", "CONFIG_VALIDATION_FAILED")]

    config, issues = load_yaml_file(config_path)
    if issues:
        return None, issues
    assert config is not None

    sheets = config.get("sheets", {})
    if not isinstance(sheets, Mapping):
        return None, [Issue("sheets", "REQUIRED")]
    columns = sheets.get("human_editable_columns", [])
    if not isinstance(columns, list):
        return None, [Issue("sheets.human_editable_columns", "INVALID_TYPE")]
    return {str(column) for column in columns}, []


def human_editable_issues(
    schema: Mapping[str, Any],
    allowed_columns: set[str],
) -> list[Issue]:
    """Validate schema human-editable flags against config allow-list."""

    issues: list[Issue] = []
    tabs = schema.get("tabs", [])
    if not isinstance(tabs, list):
        return issues

    for tab_index, tab in enumerate(tabs):
        if not isinstance(tab, Mapping):
            continue
        columns = tab.get("columns", [])
        if not isinstance(columns, list):
            continue
        for column_index, column in enumerate(columns):
            if not isinstance(column, list) or len(column) != 4:
                continue
            name, _column_type, _required, human_editable = column
            if human_editable is True and name not in allowed_columns:
                issues.append(
                    Issue(
                        f"tabs.[index:{tab_index}].columns.[index:{column_index}]",
                        "HUMAN_EDITABLE_COLUMN_NOT_ALLOWED",
                    )
                )
    return issues


def validate_sheets_schema(
    schema_path: Path,
    config_path: Path = DEFAULT_CONFIG,
    meta_schema_path: Path = DEFAULT_SHEETS_META_SCHEMA,
) -> CliResult:
    """Validate Google Sheets schema structure and semantic safety rules."""

    schema, schema_load_issues = load_json_file(schema_path)
    if schema_load_issues:
        return CliResult(
            False, "SHEETS_SCHEMA_INVALID", "Sheets schema could not be loaded.", schema_load_issues
        )
    assert schema is not None

    meta_schema, meta_load_issues = load_json_file(meta_schema_path)
    if meta_load_issues:
        return CliResult(
            False,
            "SHEETS_META_SCHEMA_INVALID",
            "Sheets meta-schema could not be loaded.",
            meta_load_issues,
        )
    assert meta_schema is not None

    try:
        meta_issues = validate_json_schema(schema, meta_schema)
    except DependencyMissingError as exc:
        return CliResult(
            False,
            "DEPENDENCY_MISSING",
            "jsonschema is required to validate Sheets schema.",
            [Issue("dependency", exc.package)],
        )

    allowed_columns, config_issues = load_human_editable_columns(config_path)
    issues = meta_issues + semantic_schema_issues(schema)
    if config_issues:
        issues.extend(config_issues)
    elif allowed_columns is not None:
        issues.extend(human_editable_issues(schema, allowed_columns))

    if issues:
        return CliResult(
            ok=False,
            code="SHEETS_SCHEMA_VALIDATION_FAILED",
            message="Sheets schema validation failed.",
            details=issues,
        )

    tabs = schema.get("tabs", [])
    return CliResult(
        ok=True,
        code="OK",
        message="Sheets schema validation passed.",
        details=[],
        data={
            "tab_count": len(tabs) if isinstance(tabs, list) else 0,
        },
    )
