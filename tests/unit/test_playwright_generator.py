from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import webapp_debug_skill.playwright_generator as generator
from webapp_debug_skill.playwright_generator import GENERATION_MANIFEST_NAME, main
from webapp_debug_skill.playwright_project import MANIFEST_NAME, main as bootstrap_main
from webapp_debug_skill.risk import RiskLevel
from webapp_debug_skill.scenario_model import (
    DataRequirement,
    DataRequirementKind,
    ExpectationSourceType,
    ExpectationStatus,
    ScenarioAction,
    ScenarioActionKind,
    ScenarioAssertion,
    ScenarioAssertionKind,
    ScenarioContract,
    ScenarioDepth,
    ScenarioGenerationStatus,
    ScenarioLatestTestStatus,
    ScenarioLifecycleStatus,
    ScenarioTestScope,
    SourceReference,
)

SECRET = "SECRET_MARKER_PLAYWRIGHT_GENERATOR"


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def bootstrap(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert bootstrap_main(["--root", str(root), "--format", "json"]) == 0
    capsys.readouterr()


def scenario(
    *,
    scenario_id: str = "SCN-000001",
    action: ScenarioAction | None = None,
    assertion: ScenarioAssertion | None = None,
    data_requirement: DataRequirement | None = None,
    test_scope: ScenarioTestScope = ScenarioTestScope.E2E_PLAYWRIGHT,
    lifecycle_status: ScenarioLifecycleStatus = ScenarioLifecycleStatus.ACTIVE,
    conflict_detected: bool = False,
) -> ScenarioContract:
    return ScenarioContract(
        scenario_id=scenario_id,
        scenario_version=1,
        feature_id="FEAT-000001",
        feature_name="ユーザー管理",
        story_id="STORY-000001",
        story_title="ユーザーを管理できる",
        scenario_title="ユーザー一覧を表示できる",
        actor="admin",
        inventory_ids=("INV-001",),
        preconditions=("adminとしてログインしている",),
        actions=(
            action
            or ScenarioAction(
                ScenarioActionKind.NAVIGATE,
                "ユーザー一覧を開く",
                target="/users",
            ),
        ),
        assertions=(
            assertion
            or ScenarioAssertion(
                ScenarioAssertionKind.VISIBLE,
                "ユーザー一覧が表示される",
                target="ユーザー一覧",
            ),
        ),
        data_requirements=(
            data_requirement
            or DataRequirement(
                DataRequirementKind.NONE,
                "追加データを必要としない",
            ),
        ),
        expectation_status=ExpectationStatus.PROVISIONAL_CODE,
        expectation_source_type=ExpectationSourceType.CODE,
        source_refs=(
            SourceReference(
                ExpectationSourceType.CODE,
                "src/Controller/UsersController.php",
                symbol="Users::index",
                line_start=12,
                line_end=12,
                fingerprint="sha256:source",
            ),
        ),
        conflict_detected=conflict_detected,
        test_scope=test_scope,
        scenario_depth=ScenarioDepth.B,
        risk=RiskLevel.MEDIUM,
        priority=RiskLevel.MEDIUM,
        route_or_url="/users",
        source_fingerprint="sha256:source",
        lifecycle_status=lifecycle_status,
        generation_status=ScenarioGenerationStatus.PENDING,
        latest_test_status=ScenarioLatestTestStatus.NOT_RUN,
    )


def write_scenarios(tmp_path: Path, *scenarios: ScenarioContract) -> Path:
    path = tmp_path / "scenarios.json"
    payload = {"tabs": {"Scenarios": [item.to_sheet_row() for item in scenarios]}}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def project_file(root: Path, relative: str) -> Path:
    return root / "tests/e2e" / relative


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    captured = capsys.readouterr()

    assert exc_info.value.code == 0
    assert "--scenario-json" in captured.out
    assert "--dry-run" in captured.out


def test_requires_bootstrap_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario_path = write_scenarios(tmp_path, scenario())

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_GENERATOR_BOOTSTRAP_REQUIRED"
    assert_no_secret(out, err)


def test_dry_run_text_and_json_do_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())

    text_code, text_out, text_err = run_cli(
        ["--root", str(tmp_path), "--scenario-json", str(scenario_path), "--dry-run"],
        capsys,
    )
    json_code, json_out, json_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--dry-run",
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(json_out)
    assert text_code == 0
    assert "PLAYWRIGHT_GENERATION_PLAN" in text_out
    assert json_code == 0
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["generated_count"] == 1
    assert payload["data"]["planned_file_count"] == 3
    assert not project_file(tmp_path, "generated/scn-000001-v1.spec.ts").exists()
    assert not project_file(tmp_path, f"generated/{GENERATION_MANIFEST_NAME}").exists()
    assert_no_secret(text_out, text_err, json_out, json_err)


def test_apply_generates_spec_manifests_and_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())

    first_code, first_out, first_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )
    second_code, second_out, second_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    first = json.loads(first_out)
    second = json.loads(second_out)
    spec = project_file(tmp_path, "generated/scn-000001-v1.spec.ts").read_text(encoding="utf-8")
    generation_manifest = json.loads(
        project_file(tmp_path, f"generated/{GENERATION_MANIFEST_NAME}").read_text(encoding="utf-8")
    )
    project_manifest = json.loads(project_file(tmp_path, MANIFEST_NAME).read_text("utf-8"))
    spec_entry = next(
        item
        for item in project_manifest["files"]
        if item["path"] == "tests/e2e/generated/scn-000001-v1.spec.ts"
    )
    assert first_code == 0
    assert second_code == 0
    assert first["data"]["generated_count"] == 1
    assert first["data"]["create_count"] == 2
    assert first["data"]["update_count"] == 1
    assert second["data"]["unchanged_count"] == 3
    assert "// scenario_id: SCN-000001" in spec
    assert 'await page.goto("/users");' in spec
    assert "test.only" not in spec
    assert "storageState" not in spec
    assert generation_manifest["scenarios"][0]["status"] == "GENERATED"
    assert spec_entry["metadata"]["scenario_id"] == "SCN-000001"
    assert spec_entry["metadata"]["scenario_version"] == 1
    assert spec_entry["metadata"]["source_fingerprint"] == "sha256:source"
    assert_no_secret(first_out, first_err, second_out, second_err, spec, generation_manifest)


def test_unsupported_and_unsafe_scenarios_are_blocked_without_runnable_spec(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap(tmp_path, capsys)
    click = replace(
        scenario(scenario_id="SCN-000001"),
        actions=(
            ScenarioAction(
                ScenarioActionKind.CLICK,
                "保存をクリックする",
                target="保存",
            ),
        ),
    )
    db_required = replace(
        scenario(scenario_id="SCN-000002"),
        data_requirements=(
            DataRequirement(
                DataRequirementKind.TEST_RECORD,
                "このtest_run_idが所有するユーザーが存在する",
            ),
        ),
    )
    non_e2e = scenario(
        scenario_id="SCN-000003",
        test_scope=ScenarioTestScope.MANUAL_REVIEW,
    )
    scenario_path = write_scenarios(tmp_path, click, db_required, non_e2e)

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    status_manifest = json.loads(
        project_file(tmp_path, f"generated/{GENERATION_MANIFEST_NAME}").read_text(encoding="utf-8")
    )
    reasons = {item["scenario_id"]: item["reason_code"] for item in status_manifest["scenarios"]}
    assert code == 0
    assert payload["data"]["generated_count"] == 0
    assert payload["data"]["blocked_count"] == 3
    assert reasons == {
        "SCN-000001": "UNSUPPORTED_ACTION",
        "SCN-000002": "UNSAFE_DATA_REQUIREMENT",
        "SCN-000003": "NON_E2E_SCOPE",
    }
    assert not project_file(tmp_path, "generated/scn-000001-v1.spec.ts").exists()
    assert_no_secret(out, err, status_manifest)


def test_existing_non_generated_spec_blocks_without_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())
    target = project_file(tmp_path, "generated/scn-000001-v1.spec.ts")
    target.write_text("manual spec\n", encoding="utf-8")

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_GENERATOR_FILE_CONFLICT"
    assert target.read_text(encoding="utf-8") == "manual spec\n"
    assert_no_secret(out, err)


def test_manifest_checksum_mismatch_blocks_update(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())
    assert run_cli(["--root", str(tmp_path), "--scenario-json", str(scenario_path)], capsys)[0] == 0
    target = project_file(tmp_path, "generated/scn-000001-v1.spec.ts")
    target.write_text(target.read_text(encoding="utf-8") + "// human edit\n", encoding="utf-8")

    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 3
    assert payload["code"] == "PLAYWRIGHT_GENERATOR_FILE_CONFLICT"
    assert "human edit" in target.read_text(encoding="utf-8")
    assert_no_secret(out, err)


def test_invalid_json_and_secret_marker_are_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap(tmp_path, capsys)
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json " + SECRET, encoding="utf-8")
    secret_row = scenario().to_sheet_row()
    secret_row["structured_actions"] = json.dumps(
        [
            {
                "kind": "NAVIGATE",
                "description": "open",
                "target": f"/users?token={SECRET}",
                "value": "",
            }
        ],
        ensure_ascii=False,
    )
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(
        json.dumps({"Scenarios": [secret_row]}, ensure_ascii=False),
        encoding="utf-8",
    )

    invalid_code, invalid_out, invalid_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(invalid),
            "--format",
            "json",
        ],
        capsys,
    )
    secret_code, secret_out, secret_err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(secret_path),
            "--format",
            "json",
        ],
        capsys,
    )

    assert invalid_code == 2
    assert json.loads(invalid_out)["code"] == "PLAYWRIGHT_GENERATOR_JSON_INVALID"
    assert secret_code == 2
    assert json.loads(secret_out)["code"] == "SCENARIO_SECRET_DETECTED"
    assert_no_secret(invalid_out, invalid_err, secret_out, secret_err)


def test_write_failure_is_exit_4_and_leaves_no_new_spec(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())

    def fail_write(_path: Path, _content: bytes) -> None:
        raise OSError("raw failure")

    monkeypatch.setattr(generator, "atomic_write", fail_write)
    code, out, err = run_cli(
        [
            "--root",
            str(tmp_path),
            "--scenario-json",
            str(scenario_path),
            "--format",
            "json",
        ],
        capsys,
    )

    payload = json.loads(out)
    assert code == 4
    assert payload["code"] == "PLAYWRIGHT_GENERATOR_WRITE_FAILED"
    assert not project_file(tmp_path, "generated/scn-000001-v1.spec.ts").exists()
    assert "raw failure" not in out
    assert_no_secret(out, err)


def test_no_network_or_external_commands_are_used(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap(tmp_path, capsys)
    scenario_path = write_scenarios(tmp_path, scenario())

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external command attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    monkeypatch.setattr(subprocess, "run", fail_run)

    code, out, err = run_cli(
        ["--root", str(tmp_path), "--scenario-json", str(scenario_path), "--dry-run"],
        capsys,
    )

    assert code == 0
    assert_no_secret(out, err)
