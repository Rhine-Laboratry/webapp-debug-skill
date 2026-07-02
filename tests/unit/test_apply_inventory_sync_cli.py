from __future__ import annotations

import json
import shutil
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.google_sheets_service import FakeGoogleSheetsService
from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    GoogleCredentialLoadResult,
    SHEETS_SCOPE,
)
from webapp_debug_skill.inventory_apply_cli import InventoryApplyDependencies, main
from webapp_debug_skill.inventory_sync import build_sync_plan, inventory_headers
from webapp_debug_skill.wal import AppendOnlyWal, WalError

EXAMPLE_CONFIG = Path("skills/webapp-debug/assets/webapp-debug.config.example.yml")
SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
FIXTURES = Path("tests/fixtures/inventory_sync")
SECRET = "SECRET_MARKER_APPLY_CLI"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def headers() -> tuple[str, ...]:
    return inventory_headers(SCHEMA)[0]


def write_config(tmp_path: Path, *, spreadsheet_id: str = "spreadsheet-123") -> Path:
    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["project"]["repository_root"] = "."
    data["project"]["project_id"] = "project-id"
    data["project"]["name"] = "Project Sheet"
    data["sheets"]["spreadsheet_id"] = spreadsheet_id
    data["sheets"]["service_account_credentials_env"] = "GOOGLE_CREDS"
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def write_plan(tmp_path: Path, payload: dict[str, Any] | None = None) -> Path:
    plan = payload or build_sync_plan(
        load_fixture("discovery_new.json"),
        load_fixture("snapshot_empty.json"),
        headers=headers(),
        schema_version=1,
        generated_at="2026-07-02T00:00:00Z",
    )
    target = tmp_path / "inventory-plan.json"
    target.write_text(json.dumps(plan, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return target


def canonical_sheets(
    *,
    spreadsheet_id: str = "spreadsheet-123",
    inventory_rows: list[dict[str, str]] | None = None,
) -> FakeGoogleSheetsService:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    sheets: dict[str, list[list[str]]] = {}
    for tab in schema["tabs"]:
        tab_headers = [column[0] for column in tab["columns"]]
        sheets[tab["name"]] = [tab_headers]
    if inventory_rows is not None:
        inventory_headers = sheets["Inventory"][0]
        sheets["Inventory"] = [
            inventory_headers,
            *[[str(row.get(column, "")) for column in inventory_headers] for row in inventory_rows],
        ]
    return FakeGoogleSheetsService(spreadsheet_id=spreadsheet_id, sheets=sheets)


def deps(service: FakeGoogleSheetsService) -> InventoryApplyDependencies:
    def credential_loader(**_kwargs: object) -> GoogleCredentialLoadResult:
        return GoogleCredentialLoadResult(credentials=object(), scopes=(SHEETS_SCOPE,))

    return InventoryApplyDependencies(
        credential_loader=credential_loader,
        service_builder=lambda _credentials: service,
        run_id_factory=lambda: "run-apply",
        clock=lambda: "2026-07-02T00:00:00Z",
        snapshot_clock=lambda: datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC),
    )


class PendingFailingWal(AppendOnlyWal):
    """WAL that rejects pending writes."""

    def append_pending(
        self,
        _operation: str,
        _payload: dict[str, Any],
        *,
        operation_id: str | None = None,
    ) -> object:
        raise WalError("WAL_WRITE_FAILED", "wal", "WRITE_FAILED")


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


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--confirm-spreadsheet-id" in captured.out
    assert "--dry-run" in captured.out


def test_dry_run_text_and_json_have_no_writes_or_wal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    service = canonical_sheets()

    text_code, text_out, text_err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--dry-run",
        ],
        service,
        capsys,
    )
    json_code, json_out, json_err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--dry-run",
            "--format",
            "json",
        ],
        service,
        capsys,
    )

    payload = json.loads(json_out)
    assert text_code == 0
    assert "INVENTORY_APPLY_PLAN" in text_out
    assert json_code == 0
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["lock_acquired"] is False
    assert payload["data"]["wal_pending_written"] is False
    assert service.batch_update_requests == []
    assert not list(tmp_path.glob("*.jsonl"))
    assert_no_secret(text_out, text_err, json_out, json_err)


def test_confirmation_required_before_google_or_wal_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    service = canonical_sheets()

    code, out, err = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--plan", str(plan)],
        service,
        capsys,
    )

    assert code == 3
    assert "INVENTORY_APPLY_CONFIRMATION_REQUIRED" in out
    assert service.request_log == []
    assert service.batch_update_requests == []
    assert not list(tmp_path.glob("*.jsonl"))
    assert_no_secret(out, err)


def test_invalid_args_external_failure_lock_conflict_and_unexpected_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    service = canonical_sheets()

    invalid_code, _, invalid_err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--run-id",
            "bad/run",
            "--dry-run",
        ],
        service,
        capsys,
    )
    assert invalid_code == 2
    assert_no_secret(invalid_err)

    def failing_credential(**_kwargs: object) -> None:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED", "credential", SECRET, exit_code=4
        )

    code = main(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
        ],
        replace(deps(service), credential_loader=failing_credential),
    )
    captured = capsys.readouterr()
    assert code == 4
    assert_no_secret(captured.out, captured.err)

    locked = canonical_sheets()
    locked.rows["Metadata"].extend(
        [
            ["writer_lock_owner", "other", "", ""],
            ["writer_lock_run_id", "other-run", "", ""],
            ["writer_lock_acquired_at", "2026-07-02T00:00:00Z", "", ""],
            ["writer_lock_expires_at", "2099-01-01T00:00:00Z", "", ""],
            ["writer_lock_commit_sha", "abc", "", ""],
        ]
    )
    lock_code, lock_out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
        ],
        locked,
        capsys,
    )
    assert lock_code == 5
    assert "SHEETS_LOCK_HELD" in lock_out
    assert locked.batch_update_requests == []

    def unexpected(**_kwargs: object) -> None:
        raise RuntimeError(SECRET)

    code = main(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--dry-run",
        ],
        replace(deps(service), credential_loader=unexpected),
    )
    captured = capsys.readouterr()
    assert code == 10
    assert_no_secret(captured.out, captured.err)


def test_apply_inventory_plan_with_lock_wal_and_readback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    wal_path = tmp_path / "inventory-apply.jsonl"
    service = canonical_sheets()

    code, out, err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
            "--wal",
            str(wal_path),
            "--format",
            "json",
        ],
        service,
        capsys,
    )

    payload = json.loads(out)
    inventory_rows = service.snapshot()["Inventory"]
    wal_text = wal_path.read_text(encoding="utf-8")
    assert code == 0
    assert payload["data"]["outcome"] == "APPLIED"
    assert payload["data"]["lock_released"] is True
    assert payload["data"]["wal_pending_written"] is True
    assert payload["data"]["wal_acknowledged"] is True
    assert payload["data"]["read_back_verified"] is True
    assert inventory_rows[1][0] == "INV-TEMP-NEW"
    assert [entry[0] for entry in service.request_log if entry[0] == "batchUpdate"] == [
        "batchUpdate",
        "batchUpdate",
        "batchUpdate",
    ]
    assert '"operation":"inventory.apply"' in wal_text
    assert '"status":"pending"' in wal_text
    assert '"status":"acknowledged"' in wal_text
    assert_no_secret(out, err, wal_text)


def test_wal_pending_failure_prevents_inventory_write_and_releases_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    service = canonical_sheets()
    failing_deps = replace(deps(service), wal_factory=PendingFailingWal)

    code = main(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
            "--wal",
            str(tmp_path / "inventory-apply.jsonl"),
        ],
        failing_deps,
    )
    captured = capsys.readouterr()

    assert code == 4
    assert "INVENTORY_APPLY_WAL_FAILED" in captured.out
    assert service.snapshot()["Inventory"] == [canonical_sheets().snapshot()["Inventory"][0]]
    assert len(service.batch_update_requests) == 2
    assert service.rows["Metadata"][1][1] == ""
    assert_no_secret(captured.out, captured.err)


def test_fake_cli_path_does_not_use_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    config = write_config(tmp_path)
    plan = write_plan(tmp_path)
    service = canonical_sheets()

    code, out, err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--plan",
            str(plan),
            "--dry-run",
        ],
        service,
        capsys,
    )

    assert code == 0
    assert_no_secret(out, err)
