from __future__ import annotations

import itertools
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fakes.google_sheets_service import FakeGoogleApiError, FakeGoogleSheetsService
from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.google_credentials import (
    GoogleCredentialError,
    GoogleCredentialLoadResult,
    SHEETS_SCOPE,
)
from webapp_debug_skill.sheets_bootstrap import BOOTSTRAP_OPERATION, CREATE_OPERATION
from webapp_debug_skill.sheets_init import INIT_OPERATION
from webapp_debug_skill.sheets_init_cli import InitSheetsDependencies, main
from webapp_debug_skill.wal import AppendOnlyWal, WalError

EXAMPLE_CONFIG = Path("skills/webapp-debug/assets/webapp-debug.config.example.yml")
SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_INIT_CLI"


def copy_config(
    tmp_path: Path,
    *,
    spreadsheet_id: str = "spreadsheet-123",
    project_name: str = "Project Sheet",
) -> Path:
    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["project"]["repository_root"] = "."
    data["project"]["name"] = project_name
    data["project"]["project_id"] = "project-id"
    data["sheets"]["spreadsheet_id"] = spreadsheet_id
    data["sheets"]["service_account_credentials_env"] = "GOOGLE_CREDS"
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def service_with_all_tabs(spreadsheet_id: str = "spreadsheet-123") -> FakeGoogleSheetsService:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    sheets = {tab["name"]: [[column[0] for column in tab["columns"]]] for tab in schema["tabs"]}
    return FakeGoogleSheetsService(spreadsheet_id=spreadsheet_id, sheets=sheets)


def deps(service: FakeGoogleSheetsService) -> InitSheetsDependencies:
    counter = itertools.count(1)

    def credential_loader(**_kwargs: object) -> GoogleCredentialLoadResult:
        return GoogleCredentialLoadResult(credentials=object(), scopes=(SHEETS_SCOPE,))

    return InitSheetsDependencies(
        credential_loader=credential_loader,
        service_builder=lambda _credentials: service,
        run_id_factory=lambda: "run-1",
        operation_id_factory=lambda: f"op-{next(counter)}",
        clock=lambda: "2026-07-01T00:00:00Z",
    )


class AckFailingWal(AppendOnlyWal):
    """WAL that allows pending writes but rejects ack writes."""

    def append_ack(
        self,
        _operation_id: str,
        _payload: dict[str, object] | None = None,
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
    assert "--bootstrap-lock-storage" in captured.out
    assert "--resume" in captured.out


def test_text_and_json_output_for_existing_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()

    text_code, text_out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--dry-run"],
        service,
        capsys,
    )
    json_code, json_out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--dry-run", "--format", "json"],
        service,
        capsys,
    )

    assert text_code == 0
    assert "SHEETS_INIT_PLAN" in text_out
    assert json_code == 0
    payload = json.loads(json_out)
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert service.batch_update_requests == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--title", "Only Create May Use Title"],
        ["--write-config"],
        ["--create", "--resume"],
        ["--create", "--bootstrap-lock-storage"],
        ["--resume"],
        ["--run-id", "bad/run"],
    ],
)
def test_invalid_args_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()

    code, out, err = run_cli(["--config", str(config), *argv], service, capsys)

    assert code == 2
    assert service.request_log == []
    assert_no_secret(out, err)


def test_safety_block_external_failure_lock_conflict_and_unexpected_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_metadata = FakeGoogleSheetsService(
        spreadsheet_id="spreadsheet-123", sheets={"Features": [["A"]]}
    )
    config = copy_config(tmp_path)
    code, out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA)], missing_metadata, capsys
    )
    assert code == 3
    assert "SHEETS_INIT_BOOTSTRAP_REQUIRED" in out

    service = service_with_all_tabs()

    def failing_credential(**_kwargs: object) -> None:
        raise GoogleCredentialError(
            "GOOGLE_CREDENTIAL_LOAD_FAILED", "credential", SECRET, exit_code=4
        )

    fail_deps = InitSheetsDependencies(
        credential_loader=failing_credential,
        service_builder=lambda _credentials: service,
    )
    code = main(["--config", str(config), "--schema", str(SCHEMA)], fail_deps)
    captured = capsys.readouterr()
    assert code == 4
    assert_no_secret(captured.out, captured.err)

    locked = service_with_all_tabs()
    locked.rows["Metadata"].extend(
        [
            ["writer_lock_owner", "other", "", ""],
            ["writer_lock_run_id", "other-run", "", ""],
            ["writer_lock_acquired_at", "2026-07-01T00:00:00Z", "", ""],
            ["writer_lock_expires_at", "2099-01-01T00:00:00Z", "", ""],
            ["writer_lock_commit_sha", "abc", "", ""],
        ]
    )
    code, _, _ = run_cli(["--config", str(config), "--schema", str(SCHEMA)], locked, capsys)
    assert code == 5

    def unexpected(**_kwargs: object) -> None:
        raise RuntimeError(SECRET)

    code = main(
        ["--config", str(config), "--schema", str(SCHEMA)],
        InitSheetsDependencies(credential_loader=unexpected),
    )
    captured = capsys.readouterr()
    assert code == 10
    assert_no_secret(captured.out, captured.err)


def test_existing_initializer_execution_and_second_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()
    wal_path = tmp_path / "init.wal"

    code, out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--wal", str(wal_path)],
        service,
        capsys,
    )
    code2, out2, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--wal", str(tmp_path / "init2.wal")],
        service,
        capsys,
    )

    assert code == 0
    assert "SHEETS_INIT_OK" in out
    assert code2 == 0
    assert "NOOP" in out2
    assert any(
        entry.operation == INIT_OPERATION
        for entry in AppendOnlyWal(wal_path, "run-1").read_entries()
    )
    assert service.snapshot()["Metadata"][0] == ["key", "value", "updated_at", "notes"]


def test_existing_initializer_wal_ack_failure_is_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()
    failing_deps = replace(
        deps(service),
        wal_factory=AckFailingWal,
    )

    code = main(
        ["--config", str(config), "--schema", str(SCHEMA), "--wal", str(tmp_path / "init.wal")],
        failing_deps,
    )
    captured = capsys.readouterr()

    assert code == 4
    assert "SHEETS_INIT_WAL_FAILED" in captured.out
    assert "WAL_ACK_FAILED" in captured.out
    assert_no_secret(captured.out, captured.err)


def test_existing_initializer_lock_release_failure_is_exit_5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()

    def steal_lock_after_initializer(fake: FakeGoogleSheetsService) -> None:
        if len(fake.batch_update_requests) < 2:
            return
        for row in fake.rows["Metadata"]:
            if row and row[0] == "writer_lock_owner":
                row[1] = "other"
            if row and row[0] == "writer_lock_run_id":
                row[1] = "other-run"

    service.after_batch_apply_hook = steal_lock_after_initializer

    code, out, err = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--wal", str(tmp_path / "init.wal")],
        service,
        capsys,
    )

    assert code == 5
    assert "SHEETS_LOCK_OWNER_MISMATCH" in out
    assert "LOCK_RELEASE_FAILED" in out
    assert_no_secret(out, err)


def test_bootstrap_confirmation_and_execution_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(
        spreadsheet_id="spreadsheet-123", sheets={"Features": [["Feature ID", "Title"]]}
    )

    no_confirm, _, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--bootstrap-lock-storage"],
        service,
        capsys,
    )
    mismatch, _, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--bootstrap-lock-storage",
            "--confirm-spreadsheet-id",
            "wrong-id",
        ],
        service,
        capsys,
    )
    wal_path = tmp_path / "bootstrap.wal"
    ok, out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--bootstrap-lock-storage",
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
            "--wal",
            str(wal_path),
        ],
        service,
        capsys,
    )

    entries = AppendOnlyWal(wal_path, "run-1").read_entries()
    operations = [entry.operation for entry in entries if entry.status == "pending"]
    assert no_confirm == 3
    assert mismatch == 3
    assert ok == 0
    assert "SHEETS_INIT_OK" in out
    assert operations[0] == BOOTSTRAP_OPERATION
    assert INIT_OPERATION in operations


def test_bootstrap_wal_ack_failure_stops_before_initializer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = FakeGoogleSheetsService(
        spreadsheet_id="spreadsheet-123", sheets={"Features": [["Feature ID", "Title"]]}
    )
    wal_path = tmp_path / "bootstrap-ack-fail.wal"
    failing_deps = replace(deps(service), wal_factory=AckFailingWal)

    code = main(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--bootstrap-lock-storage",
            "--confirm-spreadsheet-id",
            "spreadsheet-123",
            "--wal",
            str(wal_path),
        ],
        failing_deps,
    )
    captured = capsys.readouterr()
    entries = AppendOnlyWal(wal_path, "run-1").read_entries()
    pending_ops = [entry.operation for entry in entries if entry.status == "pending"]

    assert code == 4
    assert "SHEETS_INIT_WAL_FAILED" in captured.out
    assert "WAL_ACK_FAILED" in captured.out
    assert pending_ops == [BOOTSTRAP_OPERATION]
    assert INIT_OPERATION not in pending_ops
    assert_no_secret(captured.out, captured.err)


def test_create_dry_run_and_create_with_config_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path, spreadsheet_id="")
    dry_service = service_with_all_tabs()

    dry_code, dry_out, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--create", "--dry-run"],
        dry_service,
        capsys,
    )
    assert dry_code == 0
    assert "SHEETS_INIT_PLAN" in dry_out
    assert dry_service.create_requests == []

    service = FakeGoogleSheetsService(sheets={})
    wal_path = tmp_path / "create.wal"
    code, out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--create",
            "--write-config",
            "--wal",
            str(wal_path),
        ],
        service,
        capsys,
    )

    yaml = load_yaml_module()
    updated = yaml.safe_load(config.read_text(encoding="utf-8"))
    entries = AppendOnlyWal(wal_path, "run-1").read_entries()
    pending_ops = [entry.operation for entry in entries if entry.status == "pending"]
    assert code == 0
    assert "created-1" in out
    assert updated["sheets"]["spreadsheet_id"] == "created-1"
    assert service.create_requests
    assert list(service.snapshot()) == [
        "Metadata",
        "Configuration",
        "Inventory",
        "Scenarios",
        "Test Runs",
        "Defects",
        "Evidence",
    ]
    assert pending_ops[0] == CREATE_OPERATION
    assert INIT_OPERATION in pending_ops
    assert list(tmp_path.glob("config.yml.bak.*"))


def test_create_rejections_and_outcome_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()
    configured = copy_config(configured_dir, spreadsheet_id="spreadsheet-123")
    service = service_with_all_tabs()
    code, _, _ = run_cli(
        ["--config", str(configured), "--schema", str(SCHEMA), "--create"], service, capsys
    )
    assert code == 2
    assert service.create_requests == []

    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir()
    unsafe = copy_config(unsafe_dir, spreadsheet_id="", project_name="Safe Project")
    code, out, err = run_cli(
        ["--config", str(unsafe), "--schema", str(SCHEMA), "--create", "--title", SECRET],
        service,
        capsys,
    )
    assert code == 3
    assert_no_secret(out, err)

    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()
    unknown = copy_config(unknown_dir, spreadsheet_id="")
    fail_service = FakeGoogleSheetsService(sheets={})
    fail_service.add_failure("create", FakeGoogleApiError(500, SECRET))
    code, out, err = run_cli(
        [
            "--config",
            str(unknown),
            "--schema",
            str(SCHEMA),
            "--create",
            "--wal",
            str(tmp_path / "unknown.wal"),
        ],
        fail_service,
        capsys,
    )
    assert code == 4
    assert len(fail_service.create_requests) == 1
    assert_no_secret(out, err)


def test_resume_bootstrap_initializer_and_create_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    bootstrap_service = service_with_all_tabs()
    bootstrap_wal = AppendOnlyWal(
        tmp_path / "bootstrap.jsonl", "run-1", clock=lambda: "2026-07-01T00:00:00Z"
    )
    bootstrap_wal.append_pending(
        BOOTSTRAP_OPERATION,
        {
            "spreadsheet_id": "spreadsheet-123",
            "target_tab": "Metadata",
            "canonical_header": ["key", "value", "updated_at", "notes"],
            "plan_fingerprint": "sha256:test",
        },
        operation_id="op-bootstrap",
    )
    code, out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--resume",
            "--wal",
            str(tmp_path / "bootstrap.jsonl"),
        ],
        bootstrap_service,
        capsys,
    )
    assert code == 0
    assert "ALREADY_APPLIED" in out

    init_wal = AppendOnlyWal(tmp_path / "init.jsonl", "run-1", clock=lambda: "2026-07-01T00:00:00Z")
    init_wal.append_pending(
        INIT_OPERATION,
        {
            "schema_version": 1,
            "mutation_count": 1,
            "mutations": [],
            "postconditions": {
                "schema_version": "1",
                "tabs": [{"name": "Metadata", "headers": ["key", "value", "updated_at", "notes"]}],
            },
        },
        operation_id="op-init",
    )
    bootstrap_service.rows["Metadata"].append(["webapp_debug_schema_version", "1", "", ""])
    code, out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--resume",
            "--wal",
            str(tmp_path / "init.jsonl"),
        ],
        bootstrap_service,
        capsys,
    )
    assert code == 0
    assert "ALREADY_APPLIED" in out

    create_wal = AppendOnlyWal(
        tmp_path / "create.jsonl", "run-1", clock=lambda: "2026-07-01T00:00:00Z"
    )
    create_wal.append_pending(
        CREATE_OPERATION, {"title_fingerprint": "sha256:x"}, operation_id="op-create"
    )
    code, out, _ = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--resume",
            "--wal",
            str(tmp_path / "create.jsonl"),
        ],
        bootstrap_service,
        capsys,
    )
    assert code == 3
    assert "MANUAL_RECONCILIATION" in out


def test_resume_retry_required_is_not_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = copy_config(tmp_path)
    service = service_with_all_tabs()
    wal_path = tmp_path / "retry-required.jsonl"
    retry_wal = AppendOnlyWal(wal_path, "run-1", clock=lambda: "2026-07-01T00:00:00Z")
    retry_wal.append_pending(
        INIT_OPERATION,
        {
            "schema_version": 99,
            "mutation_count": 1,
            "mutations": [],
            "postconditions": {
                "schema_version": "99",
                "tabs": [{"name": "MissingTab", "headers": ["id"]}],
            },
        },
        operation_id="op-init-retry",
    )

    code, out, err = run_cli(
        [
            "--config",
            str(config),
            "--schema",
            str(SCHEMA),
            "--resume",
            "--wal",
            str(wal_path),
        ],
        service,
        capsys,
    )

    assert code == 3
    assert "RETRY_REQUIRED" in out
    assert_no_secret(out, err)


def test_fake_service_cli_does_not_use_socket(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.socket", fail_socket)
    config = copy_config(tmp_path)
    service = service_with_all_tabs()

    code, _, _ = run_cli(
        ["--config", str(config), "--schema", str(SCHEMA), "--dry-run"], service, capsys
    )

    assert code == 0
