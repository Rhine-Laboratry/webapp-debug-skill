from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/validate_sheets_schema.py"
SCHEMA = REPO_ROOT / "skills/webapp-debug/assets/google-sheets-schema.json"
CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def load_config() -> dict[str, Any]:
    with CONFIG.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_schema(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "google-sheets-schema.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def run_sheets(
    schema_path: Path,
    *extra_args: str,
    output_format: str = "json",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--schema",
            str(schema_path),
            "--format",
            output_format,
            *extra_args,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def parse_json(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(process.stdout)


def find_tab(schema: dict[str, Any], name: str) -> dict[str, Any]:
    for tab in schema["tabs"]:
        if tab["name"] == name:
            return tab
    raise AssertionError(f"missing tab {name}")


def test_valid_sheets_schema_succeeds_with_default_config() -> None:
    process = run_sheets(SCHEMA)

    assert process.returncode == 0, process.stdout + process.stderr
    result = parse_json(process)
    assert result["ok"] is True
    assert result["data"]["tab_count"] == 7


def test_text_output_for_valid_sheets_schema() -> None:
    process = run_sheets(SCHEMA, output_format="text")

    assert process.returncode == 0
    assert "OK: Sheets schema validation passed." in process.stdout
    assert process.stderr == ""


def test_duplicate_tab_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    schema["tabs"].append(copy.deepcopy(schema["tabs"][0]))
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "DUPLICATE_TAB" in process.stdout


def test_duplicate_column_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    metadata = find_tab(schema, "Metadata")
    metadata["columns"].append(copy.deepcopy(metadata["columns"][0]))
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "DUPLICATE_COLUMN" in process.stdout


def test_column_tuple_length_is_rejected_by_meta_schema(tmp_path: Path) -> None:
    schema = load_schema()
    find_tab(schema, "Metadata")["columns"][0] = ["key", "string", True]
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "TOO_FEW_ITEMS" in process.stdout or "COLUMN_TUPLE_LENGTH" in process.stdout


def test_unknown_column_type_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    find_tab(schema, "Metadata")["columns"][0][1] = "secret-type"
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "INVALID_ENUM" in process.stdout or "COLUMN_TYPE_INVALID" in process.stdout


def test_required_tab_missing_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    schema["tabs"] = [tab for tab in schema["tabs"] if tab["name"] != "Metadata"]
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "REQUIRED_TAB_MISSING" in process.stdout


def test_append_only_identifier_missing_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    test_runs = find_tab(schema, "Test Runs")
    test_runs["columns"] = [column for column in test_runs["columns"] if column[0] != "attempt_id"]
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "APPEND_ONLY_IDENTIFIER_MISSING" in process.stdout


def test_scenario_contract_columns_missing_is_rejected(tmp_path: Path) -> None:
    schema = load_schema()
    scenarios = find_tab(schema, "Scenarios")
    scenarios["columns"] = [
        column for column in scenarios["columns"] if column[0] != "structured_actions"
    ]
    path = write_schema(tmp_path, schema)

    process = run_sheets(path)

    assert process.returncode == 2
    assert "SCENARIO_CONTRACT_COLUMNS_MISSING" in process.stdout


def test_human_editable_column_mismatch_is_rejected(tmp_path: Path) -> None:
    schema_path = write_schema(tmp_path, load_schema())
    config = load_config()
    config["sheets"]["human_editable_columns"] = ["notes"]
    config_path = write_config(tmp_path, config)

    process = run_sheets(schema_path, "--config", str(config_path))

    assert process.returncode == 2
    assert "HUMAN_EDITABLE_COLUMN_NOT_ALLOWED" in process.stdout
