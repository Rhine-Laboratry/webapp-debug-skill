from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

from webapp_debug_skill import discovery_cli
from webapp_debug_skill.discovery_cli import DiscoveryCliDependencies, main

FIXTURES = Path("tests/fixtures/cakephp_apps")
SECRET = "SECRET_MARKER_DISCOVERY_CLI"


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_text_json_and_force_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert "CakePHP Inventory" in capsys.readouterr().out

    output = tmp_path / "inventory.json"
    text_code, text_out, text_err = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(output),
        ],
        capsys,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert text_code == 0
    assert "DISCOVERY_OK" in text_out
    assert text_err == ""
    assert payload["Inventory"]

    exists_code, _, _ = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(output),
        ],
        capsys,
    )
    json_code, json_out, json_err = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(output),
            "--force",
            "--format",
            "json",
        ],
        capsys,
    )
    result = json.loads(json_out)
    assert exists_code == 3
    assert json_code == 0
    assert result["ok"] is True
    assert json_err == ""


def test_invalid_roots_output_symlink_max_files_and_non_cake(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing"
    file_root = tmp_path / "file"
    file_root.write_text("x", encoding="utf-8")
    output = tmp_path / "inventory.json"
    symlink = tmp_path / "link.json"
    symlink.symlink_to(output)

    cases = [
        (["--root", str(missing), "--output", str(output)], 2),
        (["--root", str(file_root), "--output", str(output)], 2),
        (["--root", str(FIXTURES / "cakephp4_basic"), "--output", str(symlink)], 3),
        (
            [
                "--root",
                str(FIXTURES / "cakephp4_basic"),
                "--output",
                str(output),
                "--max-files",
                "1",
            ],
            3,
        ),
        (["--root", str(FIXTURES / "non_cakephp"), "--output", str(output)], 3),
    ]
    for argv, expected_code in cases:
        if output.exists():
            output.unlink()
        code, out, err = run_cli(argv, capsys)
        assert code == expected_code
        assert SECRET not in out
        assert SECRET not in err


def test_source_output_rejected_and_filesystem_failure_is_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app_root = tmp_path / "app"
    shutil.copytree(FIXTURES / "cakephp4_basic", app_root)
    source_output = app_root / "templates/Users/index.php"
    original = source_output.read_text(encoding="utf-8")

    source_code, source_out, source_err = run_cli(
        [
            "--root",
            str(app_root),
            "--output",
            str(source_output),
            "--force",
        ],
        capsys,
    )

    def fail_discovery(*_args: object, **_kwargs: object) -> object:
        raise OSError(SECRET)

    failure_code = main(
        [
            "--root",
            str(app_root),
            "--output",
            str(tmp_path / "failed.json"),
        ],
        DiscoveryCliDependencies(discover=fail_discovery),
    )
    captured = capsys.readouterr()

    assert source_code == 3
    assert "SOURCE_PATH_REJECTED" in source_out
    assert source_err == ""
    assert source_output.read_text(encoding="utf-8") == original
    assert failure_code == 4
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_cakephp_version_override_plugins_and_coverage_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "inventory.json"
    code, out, err = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_plugin"),
            "--output",
            str(output),
            "--include-plugins",
            "--cakephp-version",
            "4",
        ],
        capsys,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert "Reports" in json.dumps(payload)
    assert payload["source"]["cakephp_version"] == "4"
    assert all(not row["source_path"].startswith("/") for row in payload["Inventory"])
    assert str(Path.home()) not in output.read_text(encoding="utf-8")
    assert err == ""
    assert SECRET not in out


def test_atomic_write_failure_leaves_no_partial_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "inventory.json"

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(SECRET)

    monkeypatch.setattr(discovery_cli.os, "replace", fail_replace)

    code, out, err = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(output),
        ],
        capsys,
    )

    assert code == 4
    assert not output.exists()
    assert not list(tmp_path.glob(".inventory.json.*.tmp"))
    assert SECRET not in out
    assert SECRET not in err


def test_no_network_or_external_runtime_and_unexpected_is_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network should not be used")

    monkeypatch.setattr(socket, "socket", fail_socket)
    output = tmp_path / "inventory.json"
    code, _, _ = run_cli(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(output),
        ],
        capsys,
    )
    assert code == 0

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(SECRET)

    code = main(
        [
            "--root",
            str(FIXTURES / "cakephp4_basic"),
            "--output",
            str(tmp_path / "unexpected.json"),
        ],
        DiscoveryCliDependencies(discover=boom),
    )
    captured = capsys.readouterr()
    assert code == 10
    assert SECRET not in captured.out
    assert SECRET not in captured.err
