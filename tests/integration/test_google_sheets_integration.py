from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.google_credentials import (
    build_sheets_service,
    load_service_account_credentials,
)
from webapp_debug_skill.google_sheets_backend import GoogleSheetsBackend, METADATA_HEADERS
from webapp_debug_skill.sheets_bootstrap import CREATE_OPERATION
from webapp_debug_skill.sheets_init import INIT_OPERATION, SCHEMA_VERSION_METADATA_KEY
from webapp_debug_skill.wal import AppendOnlyWal

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/init_sheets.py"
SCHEMA = REPO_ROOT / "skills/webapp-debug/assets/google-sheets-schema.json"
EXAMPLE_CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"

RUN_ENV = "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION"
CREDENTIAL_FILE_ENV = "WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE"
SPREADSHEET_ID_ENV = "WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID"
CONFIRM_SPREADSHEET_ID_ENV = "WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID"
ALLOW_CREATE_ENV = "WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE"
CREATE_TITLE_ENV = "WEBAPP_DEBUG_GOOGLE_CREATE_TITLE"


@pytest.fixture(autouse=True)
def require_google_integration_opt_in() -> None:
    """Skip each integration test unless real Google access is explicitly enabled."""

    if os.environ.get(RUN_ENV) != "1":
        pytest.skip(
            "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION=1 is required for Google Sheets integration tests"
        )


def _missing_env(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if os.environ.get(name, "") == ""]


def require_existing_env() -> str:
    """Return confirmed spreadsheet id or skip before Google client construction."""

    missing = _missing_env((CREDENTIAL_FILE_ENV, SPREADSHEET_ID_ENV, CONFIRM_SPREADSHEET_ID_ENV))
    if missing:
        pytest.skip("missing required Google integration env: " + ", ".join(missing))
    spreadsheet_id = os.environ[SPREADSHEET_ID_ENV]
    if os.environ[CONFIRM_SPREADSHEET_ID_ENV] != spreadsheet_id:
        pytest.fail(
            "WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID must exactly match "
            "WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID"
        )
    return spreadsheet_id


def require_create_env() -> tuple[str, str]:
    """Return confirmed spreadsheet id and create title or skip."""

    spreadsheet_id = require_existing_env()
    if os.environ.get(ALLOW_CREATE_ENV) != "1":
        pytest.skip("WEBAPP_DEBUG_GOOGLE_ALLOW_CREATE=1 is required for create integration")
    if os.environ.get(CREATE_TITLE_ENV, "") == "":
        pytest.skip("WEBAPP_DEBUG_GOOGLE_CREATE_TITLE is required for create integration")
    return spreadsheet_id, os.environ[CREATE_TITLE_ENV]


def write_config(tmp_path: Path, *, spreadsheet_id: str, project_name: str) -> Path:
    """Write a tmp config that points at integration env names, not repo state."""

    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["project"]["repository_root"] = str(tmp_path)
    data["project"]["project_id"] = "webapp-debug-integration"
    data["project"]["name"] = project_name
    data["sheets"]["spreadsheet_id"] = spreadsheet_id
    data["sheets"]["service_account_credentials_env"] = CREDENTIAL_FILE_ENV
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def run_init_sheets(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the real CLI with a safe integration environment."""

    env = os.environ.copy()
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    process = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
    )
    assert_safe_output(process.stdout, process.stderr)
    return process


def assert_safe_output(*values: str) -> None:
    """Fail without echoing credential values if sensitive text appears."""

    credential_path = os.environ.get(CREDENTIAL_FILE_ENV, "")
    credential_markers = read_credential_markers()
    for text in values:
        if credential_path and credential_path in text:
            pytest.fail("credential path leaked to integration output")
        if "-----BEGIN PRIVATE KEY-----" in text:
            pytest.fail("private key marker leaked to integration output")
        if "client_email" in text or "private_key_id" in text:
            pytest.fail("credential field leaked to integration output")
        for label, marker in credential_markers:
            if marker and marker in text:
                pytest.fail(f"credential {label} leaked to integration output")


def read_credential_markers() -> tuple[tuple[str, str], ...]:
    """Read credential markers only for local non-printing leak checks."""

    credential_path = os.environ.get(CREDENTIAL_FILE_ENV, "")
    if credential_path == "":
        return ()
    try:
        parsed = json.loads(Path(credential_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    markers: list[tuple[str, str]] = []
    for key in ("client_email", "private_key_id", "private_key"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            markers.append((key, value))
    return tuple(markers)


def assert_success(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse a successful JSON CLI result."""

    assert_safe_output(process.stdout, process.stderr)
    if process.returncode != 0:
        pytest.fail(
            "init_sheets failed with safe output "
            f"rc={process.returncode} stdout={process.stdout!r} stderr={process.stderr!r}"
        )
    return json.loads(process.stdout)


def google_backend(spreadsheet_id: str) -> GoogleSheetsBackend:
    """Build the real Google Sheets backend only after opt-in env validation."""

    credential_result = load_service_account_credentials(
        env_name=CREDENTIAL_FILE_ENV,
        repository_root=REPO_ROOT,
    )
    service = build_sheets_service(credential_result.credentials)
    return GoogleSheetsBackend(spreadsheet_id=spreadsheet_id, service=service)


def canonical_headers() -> dict[str, list[str]]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return {tab["name"]: [column[0] for column in tab["columns"]] for tab in schema["tabs"]}


def assert_canonical_state(backend: GoogleSheetsBackend) -> None:
    state = backend.read_spreadsheet()
    tabs = state.tabs_dict()
    metadata = state.metadata_dict()
    for tab_name, headers in canonical_headers().items():
        assert tab_name in tabs
        assert tabs[tab_name][: len(headers)] == headers
    assert metadata.get(SCHEMA_VERSION_METADATA_KEY) == "1"


def test_existing_spreadsheet_dry_run_bootstrap_init_and_noop(tmp_path: Path) -> None:
    spreadsheet_id = require_existing_env()
    config = write_config(
        tmp_path,
        spreadsheet_id=spreadsheet_id,
        project_name="webapp-debug integration existing",
    )
    backend = google_backend(spreadsheet_id)
    inspection = backend.inspect_metadata_storage()

    dry_run = run_init_sheets(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--dry-run",
            "--format",
            "json",
        ],
        tmp_path,
    )
    dry_payload = assert_success(dry_run)
    assert dry_payload["data"]["dry_run"] is True
    assert not list(tmp_path.glob("*.wal"))

    init_args = [
        "--config",
        str(config),
        "--schema",
        str(SCHEMA),
        "--wal",
        str(tmp_path / "existing-init.wal"),
        "--run-id",
        "integration-existing",
        "--format",
        "json",
    ]
    if inspection.status == "MISSING":
        init_args.extend(
            [
                "--bootstrap-lock-storage",
                "--confirm-spreadsheet-id",
                spreadsheet_id,
            ]
        )
    first = run_init_sheets(init_args, tmp_path)
    first_payload = assert_success(first)
    assert first_payload["data"]["read_back_verified"] is True
    assert_canonical_state(backend)

    before_second = backend.read_spreadsheet().tabs_dict()
    second = run_init_sheets(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--wal",
            str(tmp_path / "existing-init-second.wal"),
            "--run-id",
            "integration-existing-second",
            "--format",
            "json",
        ],
        tmp_path,
    )
    second_payload = assert_success(second)
    after_second = backend.read_spreadsheet().tabs_dict()
    assert second_payload["data"]["initialization_noop"] is True
    assert before_second == after_second


def test_create_spreadsheet_with_metadata_and_tmp_config_write(tmp_path: Path) -> None:
    _, title = require_create_env()
    config = write_config(
        tmp_path,
        spreadsheet_id="",
        project_name="webapp-debug integration create",
    )
    wal_path = tmp_path / "create.wal"

    result = run_init_sheets(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--create",
            "--title",
            title,
            "--write-config",
            "--wal",
            str(wal_path),
            "--run-id",
            "integration-create",
            "--format",
            "json",
        ],
        tmp_path,
    )
    payload = assert_success(result)
    created_id = payload["data"]["spreadsheet_id"]
    assert payload["data"]["created"] is True
    assert payload["data"]["config_written"] is True

    entries = AppendOnlyWal(wal_path, "integration-create").read_entries()
    pending_operations = [entry.operation for entry in entries if entry.status == "pending"]
    assert pending_operations[0] == CREATE_OPERATION
    assert INIT_OPERATION in pending_operations

    yaml = load_yaml_module()
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert updated["sheets"]["spreadsheet_id"] == created_id

    backend = google_backend(created_id)
    assert backend.inspect_metadata_storage().status == "READY"
    state = backend.read_spreadsheet()
    assert "Sheet1" not in state.tabs_dict()
    assert state.tabs_dict()["Metadata"][: len(METADATA_HEADERS)] == list(METADATA_HEADERS)
    assert_canonical_state(backend)
