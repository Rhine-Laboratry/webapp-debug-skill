from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_DIR = REPO_ROOT / "tests/integration"
SECRET_MARKER = "SECRET_MARKER_GOOGLE_INTEGRATION"


def run_integration(env_updates: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    for key in [
        "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION",
        "WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE",
        "WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID",
        "WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID",
        "WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE",
        "WEBAPP_DEBUG_GOOGLE_CREATE_TITLE",
        "WEBAPP_DEBUG_GOOGLE_ALLOW_INVENTORY_APPLY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]:
        env.pop(key, None)
    for key, value in env_updates.items():
        if value is not None:
            env[key] = value
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(INTEGRATION_DIR), "-q", "-rs"],
        check=False,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )


def assert_no_secret(process: subprocess.CompletedProcess[str]) -> None:
    assert SECRET_MARKER not in process.stdout
    assert SECRET_MARKER not in process.stderr
    assert "-----BEGIN PRIVATE KEY-----" not in process.stdout
    assert "-----BEGIN PRIVATE KEY-----" not in process.stderr


def test_integration_tests_skip_without_opt_in_and_do_not_build_google_client() -> None:
    process = run_integration({"GOOGLE_APPLICATION_CREDENTIALS": SECRET_MARKER})

    assert process.returncode == 0
    assert "skipped" in process.stdout.lower()
    assert "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1" in process.stdout
    assert_no_secret(process)


def test_integration_tests_skip_safely_when_required_env_is_missing() -> None:
    process = run_integration(
        {
            "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION": "1",
            "WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE": SECRET_MARKER,
        }
    )

    assert process.returncode == 0
    assert "missing required Google integration env" in process.stdout
    assert_no_secret(process)


def test_documented_commands_reference_existing_scripts() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8") + (
        REPO_ROOT / "INSTALL.md"
    ).read_text(encoding="utf-8")
    for script in [
        "scripts/validate_skill.py",
        "scripts/validate_config.py",
        "scripts/validate_sheets_schema.py",
        "scripts/init_sheets.py",
        "scripts/redact_artifact.py",
    ]:
        assert script in text
        assert (REPO_ROOT / script).is_file()
