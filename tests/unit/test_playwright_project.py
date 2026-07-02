from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from webapp_debug_skill.playwright_project import (
    GENERATED_MARKER,
    MANIFEST_NAME,
    main,
)

SECRET = "SECRET_MARKER_PLAYWRIGHT_BOOTSTRAP"


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def project_file(root: Path, relative: str) -> Path:
    return root / "tests/e2e" / relative


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--dry-run" in captured.out
    assert "--project-dir" in captured.out


def test_dry_run_text_and_json_do_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text_code, text_out, text_err = run_cli(["--root", str(tmp_path), "--dry-run"], capsys)
    json_code, json_out, json_err = run_cli(
        ["--root", str(tmp_path), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(json_out)
    assert text_code == 0
    assert "PLAYWRIGHT_BOOTSTRAP_PLAN" in text_out
    assert json_code == 0
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["project_dir"] == "tests/e2e"
    assert payload["data"]["planned_file_count"] == 6
    assert not (tmp_path / "tests").exists()
    assert_no_secret(text_out, text_err, json_out, json_err)


def test_apply_creates_generated_project_manifest_and_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_code, first_out, first_err = run_cli(
        ["--root", str(tmp_path), "--format", "json"], capsys
    )
    second_code, second_out, second_err = run_cli(
        ["--root", str(tmp_path), "--format", "json"],
        capsys,
    )

    first = json.loads(first_out)
    second = json.loads(second_out)
    manifest = json.loads(project_file(tmp_path, MANIFEST_NAME).read_text(encoding="utf-8"))
    config = project_file(tmp_path, "playwright.config.ts").read_text(encoding="utf-8")
    package_json = json.loads(project_file(tmp_path, "package.json").read_text(encoding="utf-8"))
    generated_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "tests/e2e").rglob("*")
        if path.is_file()
    )
    assert first_code == 0
    assert second_code == 0
    assert first["data"]["create_count"] == 6
    assert second["data"]["unchanged_count"] == 6
    assert manifest["schema_version"] == 1
    assert len(manifest["files"]) == 5
    assert GENERATED_MARKER in config
    assert package_json["private"] is True
    assert package_json["webappDebug"]["generated"] is True
    assert "storageState" not in generated_text
    assert "Cookie:" not in generated_text
    assert "Authorization:" not in generated_text
    assert ".auth" not in generated_text
    assert_no_secret(first_out, first_err, second_out, second_err)


def test_existing_non_generated_file_blocks_without_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = project_file(tmp_path, "playwright.config.ts")
    target.parent.mkdir(parents=True)
    target.write_text("manual config\n", encoding="utf-8")

    code, out, err = run_cli(["--root", str(tmp_path), "--format", "json"], capsys)

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_BOOTSTRAP_MANIFEST_REQUIRED"
    assert target.read_text(encoding="utf-8") == "manual config\n"
    assert_no_secret(out, err)


def test_invalid_root_or_template_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_root_code, missing_root_out, missing_root_err = run_cli(
        ["--root", str(tmp_path / "missing"), "--format", "json"],
        capsys,
    )
    missing_template_code, missing_template_out, missing_template_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--template",
            str(tmp_path / "missing-template.ts"),
            "--format",
            "json",
        ],
        capsys,
    )

    assert missing_root_code == 2
    assert json.loads(missing_root_out)["code"] == "PLAYWRIGHT_BOOTSTRAP_ROOT_INVALID"
    assert missing_template_code == 2
    assert json.loads(missing_template_out)["code"] == "PLAYWRIGHT_BOOTSTRAP_TEMPLATE_INVALID"
    assert_no_secret(
        missing_root_out,
        missing_root_err,
        missing_template_out,
        missing_template_err,
    )


def test_generated_file_can_update_only_when_manifest_checksum_matches(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    template_v1 = tmp_path / "template-v1.ts"
    template_v2 = tmp_path / "template-v2.ts"
    template_v1.write_text("export default { workers: 1 };\n", encoding="utf-8")
    template_v2.write_text("export default { workers: 1, retries: 1 };\n", encoding="utf-8")
    first_code, _, _ = run_cli(
        ["--root", str(tmp_path), "--template", str(template_v1)],
        capsys,
    )
    second_code, second_out, second_err = run_cli(
        ["--root", str(tmp_path), "--template", str(template_v2), "--format", "json"],
        capsys,
    )

    payload = json.loads(second_out)
    config = project_file(tmp_path, "playwright.config.ts").read_text(encoding="utf-8")
    assert first_code == 0
    assert second_code == 0
    assert payload["data"]["update_count"] == 2
    assert "retries: 1" in config
    assert_no_secret(second_out, second_err)


def test_manifest_checksum_mismatch_blocks_update(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first_code, _, _ = run_cli(["--root", str(tmp_path)], capsys)
    target = project_file(tmp_path, "pages/index.ts")
    target.write_text(target.read_text(encoding="utf-8") + "// human edit\n", encoding="utf-8")

    code, out, err = run_cli(["--root", str(tmp_path), "--format", "json"], capsys)

    payload = json.loads(out)
    assert first_code == 0
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_BOOTSTRAP_MANIFEST_MISMATCH"
    assert "human edit" in target.read_text(encoding="utf-8")
    assert_no_secret(out, err)


def test_lockfile_conflict_and_package_manager_detection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pnpm_root = tmp_path / "pnpm"
    pnpm_root.mkdir()
    (pnpm_root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    code, out, err = run_cli(
        ["--root", str(pnpm_root), "--dry-run", "--format", "json"],
        capsys,
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["data"]["package_manager"] == "pnpm"

    conflict_root = tmp_path / "conflict"
    conflict_root.mkdir()
    (conflict_root / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (conflict_root / "yarn.lock").write_text("# yarn\n", encoding="utf-8")
    conflict_code, conflict_out, conflict_err = run_cli(
        ["--root", str(conflict_root), "--dry-run", "--format", "json"],
        capsys,
    )
    conflict_payload = json.loads(conflict_out)
    assert conflict_code == 3
    assert conflict_payload["code"] == "PLAYWRIGHT_BOOTSTRAP_LOCKFILE_CONFLICT"
    assert not (conflict_root / "tests").exists()
    assert_no_secret(out, err, conflict_out, conflict_err)


def test_project_dir_escape_symlink_and_secret_marker_are_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    escape_code, escape_out, escape_err = run_cli(
        ["--root", str(tmp_path), "--project-dir", "../outside", "--format", "json"],
        capsys,
    )
    secret_code, secret_out, secret_err = run_cli(
        ["--root", str(tmp_path), "--project-dir", f"tests/{SECRET}", "--format", "json"],
        capsys,
    )
    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    (symlink_root / "tests").mkdir()
    (symlink_root / "target").mkdir()
    (symlink_root / "tests/e2e").symlink_to(symlink_root / "target")
    symlink_code, symlink_out, symlink_err = run_cli(
        ["--root", str(symlink_root), "--format", "json"],
        capsys,
    )

    assert escape_code == 3
    assert json.loads(escape_out)["code"] == "PLAYWRIGHT_BOOTSTRAP_PATH_UNSAFE"
    assert secret_code == 3
    assert json.loads(secret_out)["code"] == "PLAYWRIGHT_BOOTSTRAP_PROJECT_DIR_UNSAFE"
    assert symlink_code == 3
    assert json.loads(symlink_out)["code"] == "PLAYWRIGHT_BOOTSTRAP_TARGET_UNSAFE"
    assert_no_secret(escape_out, escape_err, secret_out, secret_err, symlink_out, symlink_err)


def test_no_network_or_external_commands_are_used(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external command attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr(subprocess, "run", fail_run)

    code, out, err = run_cli(["--root", str(tmp_path), "--dry-run"], capsys)

    assert code == 0
    assert_no_secret(out, err)
