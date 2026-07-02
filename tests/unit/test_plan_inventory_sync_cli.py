from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from webapp_debug_skill import inventory_sync
from webapp_debug_skill.inventory_sync import InventorySyncDependencies, main

FIXTURES = Path("tests/fixtures/inventory_sync")
SCHEMA = Path("skills/webapp-debug/assets/google-sheets-schema.json")
SECRET = "SECRET_MARKER_SYNC_CLI"


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(
        argv,
        InventorySyncDependencies(clock=lambda: datetime(2026, 7, 2, tzinfo=UTC)),
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def base_args(tmp_path: Path, output: Path | None = None) -> list[str]:
    return [
        "--discovery-json",
        str(FIXTURES / "discovery_new.json"),
        "--snapshot-json",
        str(FIXTURES / "snapshot_empty.json"),
        "--schema",
        str(SCHEMA),
        "--output",
        str(output or tmp_path / "plan.json"),
    ]


def test_help_text_json_force_and_atomic_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert "Inventory sync" in capsys.readouterr().out

    output = tmp_path / "plan.json"
    text_code, text_out, text_err = run_cli(base_args(tmp_path, output), capsys)
    exists_code, _, _ = run_cli(base_args(tmp_path, output), capsys)
    json_code, json_out, json_err = run_cli(
        [*base_args(tmp_path, output), "--force", "--format", "json"], capsys
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    result = json.loads(json_out)

    assert text_code == 0
    assert "INVENTORY_SYNC_OK" in text_out
    assert text_err == ""
    assert exists_code == 3
    assert json_code == 0
    assert json_err == ""
    assert result["ok"] is True
    assert payload["operations"][0]["operation"] == "APPEND_INVENTORY"


def test_input_json_output_safety_and_conflict_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_code, _, _ = run_cli(
        [
            "--discovery-json",
            str(tmp_path / "missing.json"),
            "--snapshot-json",
            str(FIXTURES / "snapshot_empty.json"),
            "--schema",
            str(SCHEMA),
            "--output",
            str(tmp_path / "plan.json"),
        ],
        capsys,
    )
    invalid = tmp_path / "bad.json"
    invalid.write_text("{bad", encoding="utf-8")
    invalid_code, _, _ = run_cli(
        [
            "--discovery-json",
            str(invalid),
            "--snapshot-json",
            str(FIXTURES / "snapshot_empty.json"),
            "--schema",
            str(SCHEMA),
            "--output",
            str(tmp_path / "bad-plan.json"),
        ],
        capsys,
    )
    output = tmp_path / "link.json"
    output.symlink_to(tmp_path / "target.json")
    symlink_code, out, err = run_cli(base_args(tmp_path, output), capsys)
    equal_input_code, _, _ = run_cli(base_args(tmp_path, FIXTURES / "discovery_new.json"), capsys)
    conflict_output = tmp_path / "conflict.json"
    conflict_code, conflict_out, _ = run_cli(
        [
            "--discovery-json",
            str(FIXTURES / "discovery_conflict.json"),
            "--snapshot-json",
            str(FIXTURES / "snapshot_empty.json"),
            "--schema",
            str(SCHEMA),
            "--output",
            str(conflict_output),
        ],
        capsys,
    )

    assert missing_code == 2
    assert invalid_code == 2
    assert symlink_code == 3
    assert equal_input_code == 3
    assert conflict_code == 3
    assert json.loads(conflict_output.read_text(encoding="utf-8"))["conflicts"]
    assert SECRET not in out
    assert SECRET not in err
    assert SECRET not in conflict_out


def test_max_operations_write_failure_and_no_external_access(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    max_code, _, _ = run_cli([*base_args(tmp_path), "--max-operations", "0"], capsys)
    double_discovery = tmp_path / "double.json"
    data = json.loads((FIXTURES / "discovery_new.json").read_text(encoding="utf-8"))
    second = dict(data["Inventory"][0])
    second["inventory_id"] = "INV-TEMP-SECOND"
    second["source_key"] = "controller|Users|add"
    second["source_fingerprint"] = "sha256:second"
    second["feature_name"] = "Users::add"
    second["name"] = "Users::add"
    second["route_or_url"] = "/users/add"
    data["Inventory"].append(second)
    double_discovery.write_text(json.dumps(data), encoding="utf-8")
    too_many_code, _, _ = run_cli(
        [
            "--discovery-json",
            str(double_discovery),
            "--snapshot-json",
            str(FIXTURES / "snapshot_empty.json"),
            "--schema",
            str(SCHEMA),
            "--output",
            str(tmp_path / "too-many.json"),
            "--max-operations",
            "1",
        ],
        capsys,
    )

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    network_code, _, _ = run_cli([*base_args(tmp_path, tmp_path / "network.json")], capsys)

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(SECRET)

    monkeypatch.setattr(inventory_sync.os, "replace", fail_replace)
    write_output = tmp_path / "write-fail.json"
    write_code, out, err = run_cli(base_args(tmp_path, write_output), capsys)

    assert max_code == 2
    assert too_many_code == 3
    assert network_code == 0
    assert write_code == 4
    assert not write_output.exists()
    assert not list(tmp_path.glob(".write-fail.json.*.tmp"))
    assert SECRET not in out
    assert SECRET not in err
