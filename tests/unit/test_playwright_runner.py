from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import webapp_debug_skill.playwright_runner as runner
from webapp_debug_skill.playwright_project import GENERATED_MARKER, sha256_bytes
from webapp_debug_skill.playwright_runner import (
    EXECUTION_OPT_IN_ENV,
    PlaywrightRunnerError,
    RunnerDependencies,
    main,
    resolve_project_dir,
)

SECRET = "SECRET_MARKER_PLAYWRIGHT_RUNNER"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"


def run_cli(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    deps: RunnerDependencies | None = None,
) -> tuple[int, str, str]:
    code = main(argv, deps=deps)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def write_config(
    root: Path,
    *,
    complete_db_guard: bool = True,
    base_url: str = "http://127.0.0.1:8765",
    allowed_hosts: list[str] | None = None,
    storage_state_dir: str = ".webapp-debug/auth",
) -> Path:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["runtime"]["app"]["base_url"] = base_url
    data["operations"]["allowed_hosts"] = allowed_hosts or ["127.0.0.1", "localhost"]
    data["authentication"]["storage_state_dir"] = storage_state_dir
    if complete_db_guard:
        data["database"]["expected_host_pattern"] = r"^127\.0\.0\.1$"
        data["database"]["expected_database_pattern"] = r"^webapp_debug_test$"
        data["database"]["sentinel"]["query"] = "SELECT 1"
        data["database"]["sentinel"]["expected_value"] = 1
    path = root / ".webapp-debug/config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def write_generated_project(
    root: Path,
    *,
    scenario_status: str = "GENERATED",
) -> Path:
    project_dir = root / "tests/e2e"
    generated_dir = project_dir / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pages").mkdir(parents=True, exist_ok=True)
    package_json = json.dumps(
        {"private": True, "scripts": {"test": "playwright test"}, "type": "module"},
        sort_keys=True,
    ).encode("utf-8")
    config_ts = f"// {GENERATED_MARKER}\nexport default {{ workers: 1, retries: 1 }};\n".encode(
        "utf-8"
    )
    spec_ts = (
        f"// {GENERATED_MARKER}\n"
        "// scenario_id: SCN-000001\n"
        "import { test } from '@playwright/test';\n"
        "test('scenario', async ({ page }) => {\n"
        "  test.info().annotations.push({ type: 'scenario_id', description: 'SCN-000001' });\n"
        "  await page.goto('/users');\n"
        "});\n"
    ).encode("utf-8")
    scenarios: list[dict[str, Any]] = [
        {
            "scenario_id": "SCN-000001",
            "scenario_version": 1,
            "status": scenario_status,
            "reason_code": "READY" if scenario_status == "GENERATED" else "UNSUPPORTED_ACTION",
            "test_file": "tests/e2e/generated/scn-000001-v1.spec.ts",
            "test_name": "scenario",
            "locator_stability": "HIGH",
        }
    ]
    if scenario_status == "BLOCKED":
        scenarios[0]["test_file"] = ""
    generation_manifest = {
        "schema_version": 1,
        "generator": "webapp-debug",
        "project_dir": "tests/e2e",
        "scenarios": scenarios,
    }
    generation_bytes = (
        json.dumps(generation_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    files = {
        "tests/e2e/package.json": (package_json, False, {}),
        "tests/e2e/playwright.config.ts": (config_ts, True, {}),
        "tests/e2e/generated/scn-000001-v1.spec.ts": (
            spec_ts,
            True,
            {"scenario_id": "SCN-000001", "scenario_version": 1},
        ),
        "tests/e2e/generated/webapp-debug.generation-manifest.json": (
            generation_bytes,
            False,
            {},
        ),
    }
    for relative_path, (content, _marker, _metadata) in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    project_manifest = {
        "schema_version": 1,
        "generator": "webapp-debug",
        "project_dir": "tests/e2e",
        "package_manager": "npm",
        "files": [
            {
                "path": relative_path,
                "sha256": sha256_bytes(content),
                "size": len(content),
                "generated_marker_required": marker,
            }
            | ({"metadata": metadata} if metadata else {})
            for relative_path, (content, marker, metadata) in sorted(files.items())
        ],
    }
    (project_dir / "webapp-debug.generated-manifest.json").write_text(
        json.dumps(project_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return project_dir


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--config" in captured.out
    assert "--dry-run" in captured.out
    assert "--execute" in captured.out


def test_dry_run_text_and_json_do_not_launch_playwright(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external command attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr(subprocess, "run", fail_run)

    text_code, text_out, text_err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run"],
        capsys,
    )
    json_code, json_out, json_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--config",
            str(config),
            "--dry-run",
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(json_out)
    assert text_code == 0
    assert "PLAYWRIGHT_RUNNER_DRY_RUN" in text_out
    assert json_code == 0
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["execution_performed"] is False
    assert payload["data"]["generated_count"] == 1
    assert payload["data"]["db_guard"] == "READY_CONFIG_ONLY"
    assert payload["data"]["network_policy"] == "ALLOWLISTED"
    assert "http://127.0.0.1" not in json_out
    assert_no_secret(text_out, text_err, json_out, json_err)


def test_db_guard_missing_blocks_before_execution(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path, complete_db_guard=False)
    write_generated_project(tmp_path)

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external command attempted")

    monkeypatch.setattr(subprocess, "run", fail_run)

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--config",
            str(config),
            "--dry-run",
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_DB_GUARD_INCOMPLETE"
    assert_no_secret(out, err)


def test_network_policy_blocks_unallowlisted_base_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path, base_url="http://example.test:8765")
    write_generated_project(tmp_path)

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_NETWORK_POLICY_BLOCKED"
    assert payload["details"][0]["reason"] == "HOST_NOT_ALLOWLISTED"
    assert "example.test" not in out
    assert_no_secret(out, err)


def test_auth_state_path_outside_root_blocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path, storage_state_dir="../auth")
    write_generated_project(tmp_path)

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_AUTH_STATE_UNSAFE"
    assert payload["details"][0]["reason"] == "OUTSIDE_ROOT"
    assert_no_secret(out, err)


def test_generated_checksum_mismatch_blocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)
    spec = tmp_path / "tests/e2e/generated/scn-000001-v1.spec.ts"
    spec.write_text(spec.read_text(encoding="utf-8") + "// human edit\n", encoding="utf-8")

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID"
    assert payload["details"][0]["reason"] == "CHECKSUM_MISMATCH"
    assert_no_secret(out, err)


def test_generated_forbidden_token_blocks_even_with_matching_checksum(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    project_dir = write_generated_project(tmp_path)
    spec = tmp_path / "tests/e2e/generated/scn-000001-v1.spec.ts"
    spec.write_text(
        spec.read_text(encoding="utf-8") + "test.only('debug', async () => {});\n",
        encoding="utf-8",
    )
    manifest_path = project_dir / "webapp-debug.generated-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "tests/e2e/generated/scn-000001-v1.spec.ts":
            content = spec.read_bytes()
            entry["sha256"] = sha256_bytes(content)
            entry["size"] = len(content)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID"
    assert payload["details"][0]["reason"] == "FORBIDDEN_TOKEN"
    assert_no_secret(out, err)


def test_no_generated_scenarios_blocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path, scenario_status="BLOCKED")

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_RUNNER_GENERATED_CODE_INVALID"
    assert payload["details"][0]["reason"] == "NO_GENERATED_SCENARIOS"
    assert_no_secret(out, err)


def test_execute_requires_opt_in_and_runtime_guard_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external command attempted")

    monkeypatch.setattr(subprocess, "run", fail_run)
    missing_env_code, missing_env_out, missing_env_err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--execute", "--format", "json"],
        capsys,
        deps=RunnerDependencies(environ={}),
    )
    missing_confirm_code, missing_confirm_out, missing_confirm_err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--execute", "--format", "json"],
        capsys,
        deps=RunnerDependencies(environ={EXECUTION_OPT_IN_ENV: "1"}),
    )

    assert missing_env_code == 3
    assert json.loads(missing_env_out)["code"] == "PLAYWRIGHT_RUNNER_EXECUTION_OPT_IN_REQUIRED"
    assert missing_confirm_code == 3
    assert json.loads(missing_confirm_out)["code"] == "PLAYWRIGHT_RUNNER_DB_RUNTIME_UNCONFIRMED"
    assert_no_secret(missing_env_out, missing_env_err, missing_confirm_out, missing_confirm_err)


def test_execute_uses_injected_runner_after_all_gates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    project_dir = write_generated_project(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(command, 0)

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--config",
            str(config),
            "--execute",
            "--confirm-db-runtime-guard",
            "--format",
            "json",
        ],
        capsys,
        deps=RunnerDependencies(
            environ={EXECUTION_OPT_IN_ENV: "1"},
            command_runner=fake_runner,
        ),
    )

    payload = json.loads(out)
    assert code == 0
    assert payload["code"] == "PLAYWRIGHT_RUNNER_EXECUTED"
    assert payload["data"]["execution_performed"] is True
    assert calls == [
        {
            "command": ["npm", "test"],
            "kwargs": {
                "cwd": project_dir,
                "check": False,
                "text": True,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": {
                    EXECUTION_OPT_IN_ENV: "1",
                    "WEBAPP_DEBUG_BASE_URL": "http://127.0.0.1:8765",
                },
            },
        }
    ]
    assert_no_secret(out, err)


def test_execute_external_failure_exit_4(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)

    def failing_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("raw command failure")

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--config",
            str(config),
            "--execute",
            "--confirm-db-runtime-guard",
            "--format",
            "json",
        ],
        capsys,
        deps=RunnerDependencies(
            environ={EXECUTION_OPT_IN_ENV: "1"},
            command_runner=failing_runner,
        ),
    )

    payload = json.loads(out)
    assert code == 4
    assert payload["code"] == "PLAYWRIGHT_RUNNER_EXECUTION_FAILED"
    assert "raw command failure" not in out
    assert_no_secret(out, err)


def test_argument_conflict_exit_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--config",
            str(config),
            "--dry-run",
            "--execute",
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 2
    assert payload["code"] == "PLAYWRIGHT_RUNNER_ARGUMENT_INVALID"
    assert_no_secret(out, err)


def test_unexpected_internal_error_exit_10(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = write_config(tmp_path)
    write_generated_project(tmp_path)

    def fail_build(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("unexpected internal raw text")

    monkeypatch.setattr(runner, "build_runner_plan", fail_build)

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--config", str(config), "--dry-run", "--format", "json"],
        capsys,
    )

    payload = json.loads(out)
    assert code == 10
    assert payload["code"] == "PLAYWRIGHT_RUNNER_UNEXPECTED"
    assert "unexpected internal raw text" not in out
    assert_no_secret(out, err)


def test_secret_marker_not_leaked_in_stdout_stderr_or_exception(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_cli(
        ["--root", str(tmp_path), "--project-dir", f"tests/{SECRET}", "--format", "json"],
        capsys,
    )

    with pytest.raises(PlaywrightRunnerError) as exc_info:
        resolve_project_dir(tmp_path.resolve(), Path(f"tests/{SECRET}"))

    assert code == 3
    assert SECRET not in out
    assert SECRET not in err
    assert SECRET not in str(exc_info.value)
