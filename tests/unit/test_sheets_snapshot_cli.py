from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

from tests.fakes.google_sheets_service import FakeGoogleApiError, FakeGoogleSheetsService
from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    GoogleCredentialLoadResult,
    SHEETS_SCOPE,
)
from webapp_debug_skill.sheets_init import load_canonical_schema
from webapp_debug_skill.sheets_snapshot_cli import SnapshotCliDependencies, main

EXAMPLE_CONFIG = Path("skills/webapp-debug/assets/webapp-debug.config.example.yml")
SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SNAPSHOT_CLI"


def copy_config(tmp_path: Path, *, spreadsheet_id: str = "spreadsheet-123") -> Path:
    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["project"]["repository_root"] = "."
    data["sheets"]["spreadsheet_id"] = spreadsheet_id
    data["sheets"]["service_account_credentials_env"] = "GOOGLE_CREDS"
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def canonical_sheets() -> dict[str, list[list[str]]]:
    result: dict[str, list[list[str]]] = {}
    schema = load_canonical_schema(SCHEMA)
    for tab in schema.tabs:
        result[tab.name] = [list(tab.headers)]
    inventory = {header: "" for header in result["Inventory"][0]}
    inventory.update(
        {
            "inventory_id": "INV-001",
            "feature_area": "Accounts",
            "item_type": "UI_PAGE",
            "name": "Login",
            "source_path": "src/login.php",
            "source_fingerprint": "sha256:test",
            "test_scope": "UI_PAGE",
            "discovery_status": "MAPPED",
            "risk": "HIGH",
            "discovered_at": "2026-07-01T00:00:00Z",
            "last_seen_commit": "abc",
            "last_seen_at": "2026-07-01T00:00:00Z",
            "notes": SECRET,
        }
    )
    result["Inventory"].append([inventory[header] for header in result["Inventory"][0]])
    return result


def deps(service: FakeGoogleSheetsService) -> SnapshotCliDependencies:
    def credential_loader(**_kwargs: object) -> GoogleCredentialLoadResult:
        return GoogleCredentialLoadResult(credentials=object(), scopes=(SHEETS_SCOPE,))

    return SnapshotCliDependencies(
        credential_loader=credential_loader,
        service_builder=lambda _credentials: service,
    )


def run_cli(
    argv: list[str],
    service: FakeGoogleSheetsService,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    code = main(argv, deps(service))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)
        assert "Traceback" not in str(value)


def test_help_text_json_output_and_evaluate_coverage_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exc:
        main(["--help"])
    assert help_exc.value.code == 0
    capsys.readouterr()

    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(spreadsheet_id="spreadsheet-123", sheets=canonical_sheets())
    output = tmp_path / "snapshot.json"

    text_code, text_out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(output)],
        service,
        capsys,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert text_code == 0
    assert "SHEETS_SNAPSHOT" in text_out
    assert payload["Inventory"][0]["status"] == "MAPPED"
    assert payload["tabs"]["Inventory"] == payload["Inventory"]
    assert_no_secret(text_out, payload)

    json_output = tmp_path / "snapshot-json.json"
    json_code, json_out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--output",
            str(json_output),
            "--tabs",
            "Inventory,Scenarios",
            "--format",
            "json",
        ],
        service,
        capsys,
    )
    result = json.loads(json_out)
    assert json_code == 0
    assert result["ok"] is True
    assert result["data"]["tabs"] == ["Inventory", "Scenarios"]


def test_output_safety_force_parent_creation_and_symlink(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(spreadsheet_id="spreadsheet-123", sheets=canonical_sheets())
    output = tmp_path / "nested" / "snapshot.json"

    ok, _, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(output)],
        service,
        capsys,
    )
    exists, _, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(output)],
        service,
        capsys,
    )
    forced, _, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--output",
            str(output),
            "--force",
        ],
        service,
        capsys,
    )
    symlink = tmp_path / "link.json"
    symlink.symlink_to(output)
    symlink_code, out, err = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(symlink), "--force"],
        service,
        capsys,
    )
    same_config, _, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(config), "--force"],
        service,
        capsys,
    )

    assert ok == 0
    assert exists == 3
    assert forced == 0
    assert symlink_code == 3
    assert same_config == 3
    assert_no_secret(out, err)


def test_cli_failures_are_safe_and_do_not_write_partial(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(spreadsheet_id="spreadsheet-123", sheets=canonical_sheets())
    for _ in range(3):
        service.add_failure("values.batchGet", FakeGoogleApiError(500, SECRET))
    output = tmp_path / "snapshot.json"

    read_fail, out, err = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(output)],
        service,
        capsys,
    )

    def failing_credential(**_kwargs: object) -> None:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED", "credential", SECRET, exit_code=4
        )

    credential_code = main(
        ["--config", str(config), "--schema", str(SCHEMA), "--output", str(output)],
        SnapshotCliDependencies(credential_loader=failing_credential),
    )
    captured = capsys.readouterr()

    assert read_fail == 4
    assert output.exists() is False
    assert credential_code == 4
    assert_no_secret(out, err, captured.out, captured.err)


def test_no_network_or_write_api_in_unit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(spreadsheet_id="spreadsheet-123", sheets=canonical_sheets())
    code, _, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--output",
            str(tmp_path / "snapshot.json"),
        ],
        service,
        capsys,
    )

    assert code == 0
    assert service.batch_update_requests == []
    assert service.create_requests == []
