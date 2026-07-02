from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/validate_config.py"
EXAMPLE_CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"


def load_example_config() -> dict[str, Any]:
    with EXAMPLE_CONFIG.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def run_config(
    config_path: Path,
    mode: str,
    *extra_args: str,
    output_format: str = "json",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config_path),
            "--mode",
            mode,
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


def with_complete_db_guard(config: dict[str, Any], expected_value: Any = "1") -> dict[str, Any]:
    database = config["database"]
    database["expected_host_pattern"] = r"^127\.0\.0\.1$"
    database["expected_database_pattern"] = r"^webapp_debug_test$"
    database["sentinel"]["query"] = "select 1"
    database["sentinel"]["expected_value"] = expected_value
    return config


def test_example_config_init_succeeds_with_json_output() -> None:
    process = run_config(EXAMPLE_CONFIG, "init")

    assert process.returncode == 0, process.stdout + process.stderr
    result = parse_json(process)
    assert result["ok"] is True
    assert result["code"] == "OK"
    assert result["data"]["mode"] == "init"
    assert (
        load_example_config()["sheets"]["service_account_credentials_env"]
        == "WEBAPP_DEBUG_GOOGLE_SERVICE_ACCOUNT"
    )


def test_text_output_for_success_is_redacted() -> None:
    process = run_config(EXAMPLE_CONFIG, "report", output_format="text")

    assert process.returncode == 0
    assert "OK: Config validation passed." in process.stdout
    assert process.stderr == ""


def test_required_sections_are_enforced(tmp_path: Path) -> None:
    mutations: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
        ("project", lambda cfg: cfg.pop("project")),
        ("runtime.app", lambda cfg: cfg["runtime"].pop("app")),
        ("operations", lambda cfg: cfg.pop("operations")),
        ("authentication", lambda cfg: cfg.pop("authentication")),
        ("artifacts", lambda cfg: cfg.pop("artifacts")),
        ("state", lambda cfg: cfg.pop("state")),
    ]

    for expected_path, mutate in mutations:
        config = load_example_config()
        mutate(config)
        path = write_config(tmp_path, config)

        process = run_config(path, "init")

        assert process.returncode == 2, expected_path
        assert expected_path in process.stdout
        assert "Traceback" not in process.stderr


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    config = load_example_config()
    config["project"]["typo"] = True
    path = write_config(tmp_path, config)

    process = run_config(path, "init")

    assert process.returncode == 2
    result = parse_json(process)
    assert result["code"] == "CONFIG_VALIDATION_FAILED"
    assert {"path": "project.*", "reason": "UNKNOWN_KEY"} in result["details"]


def test_shared_database_destructive_reset_is_rejected(tmp_path: Path) -> None:
    config = load_example_config()
    database = config["database"]
    database["classification"] = "shared"
    database["destructive_reset"] = True
    database["reset_scope"] = "suite"
    path = write_config(tmp_path, config)

    process = run_config(path, "init")

    assert process.returncode == 2
    result = parse_json(process)
    assert result["code"] == "CONFIG_VALIDATION_FAILED"


def test_empty_db_guard_is_diagnostic_for_base_and_blocked_for_browser(tmp_path: Path) -> None:
    config = load_example_config()
    path = write_config(tmp_path, config)

    base_process = run_config(path, "discover")
    browser_process = run_config(path, "discover", "--capability", "browser")

    assert base_process.returncode == 0
    base_result = parse_json(base_process)
    assert base_result["data"]["browser_discovery"]["status"] == "BLOCKED"

    assert browser_process.returncode == 3
    browser_result = parse_json(browser_process)
    assert browser_result["code"] == "CONFIG_DB_GUARD_INCOMPLETE"
    assert {"path": "database.expected_host_pattern", "reason": "EMPTY"} in browser_result[
        "details"
    ]
    assert {"path": "database.expected_database_pattern", "reason": "EMPTY"} in browser_result[
        "details"
    ]
    assert {"path": "database.sentinel.query", "reason": "EMPTY"} in browser_result["details"]


def test_test_mode_is_blocked_when_db_guard_is_empty() -> None:
    process = run_config(EXAMPLE_CONFIG, "test")

    assert process.returncode == 3
    result = parse_json(process)
    assert result["code"] == "CONFIG_DB_GUARD_INCOMPLETE"


def test_invalid_regex_is_schema_failure_not_policy_block(tmp_path: Path) -> None:
    config = load_example_config()
    config["database"]["expected_host_pattern"] = "["
    path = write_config(tmp_path, config)

    process = run_config(path, "init")

    assert process.returncode == 2
    assert "INVALID_REGEX" in process.stdout


def test_report_succeeds_when_db_guard_is_empty() -> None:
    process = run_config(EXAMPLE_CONFIG, "report")

    assert process.returncode == 0
    result = parse_json(process)
    assert result["ok"] is True
    assert result["data"]["capabilities"] == ["sheets-read"]


def test_sentinel_expected_value_zero_and_false_are_not_empty(tmp_path: Path) -> None:
    for expected_value in (0, False):
        config = with_complete_db_guard(load_example_config(), expected_value=expected_value)
        path = write_config(tmp_path, config)

        process = run_config(path, "test")

        assert process.returncode == 0, process.stdout
        result = parse_json(process)
        assert result["ok"] is True


def test_secret_marker_is_not_leaked_to_stdout_stderr_json_or_result(tmp_path: Path) -> None:
    secret_marker = "SECRET_MARKER_DO_NOT_LEAK"
    config = load_example_config()
    config["project"]["name"] = secret_marker
    path = write_config(tmp_path, config)

    process = run_config(path, "init")
    result = parse_json(process)

    assert process.returncode == 2
    assert secret_marker not in process.stdout
    assert secret_marker not in process.stderr
    assert secret_marker not in json.dumps(result, ensure_ascii=False)
    assert "Traceback" not in process.stderr
