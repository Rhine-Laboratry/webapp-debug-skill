from __future__ import annotations

import json
import os
import importlib.util
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.coverage import (
    CoverageMode,
    CoverageOutcome,
    CoveragePolicy,
    CoverageReason,
    evaluate_inventory,
)
from webapp_debug_skill.risk import RiskLevel

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/evaluate_coverage.py"
EXAMPLE_CONFIG = REPO_ROOT / "skills/webapp-debug/assets/webapp-debug.config.example.yml"
SECRET = "SECRET_MARKER_COVERAGE"


def policy(
    *,
    mode: CoverageMode = CoverageMode.STRICT,
    minimum: float = 100,
    maximum_gaps: int = 0,
    block: tuple[RiskLevel, ...] = (
        RiskLevel.CRITICAL,
        RiskLevel.HIGH,
        RiskLevel.MEDIUM,
        RiskLevel.LOW,
    ),
    current_pass: int = 1,
    max_passes: int = 3,
) -> CoveragePolicy:
    return CoveragePolicy(
        mode=mode,
        minimum_inventory_closure_percent=minimum,
        maximum_open_discovery_gaps=maximum_gaps,
        block_open_gap_risks=block,
        current_discovery_pass=current_pass,
        max_discovery_passes=max_passes,
    )


def row(
    status: str,
    risk: str = "LOW",
    *,
    inventory_id: str = "INV",
    feature_name: str = "page",
) -> dict[str, str]:
    return {
        "inventory_id": inventory_id,
        "status": status,
        "risk": risk,
        "feature_name": feature_name,
    }


def assert_no_secret(*values: object) -> None:
    for value in values:
        rendered = str(value)
        assert SECRET not in rendered
        assert "Traceback" not in rendered


def test_strict_all_mapped_passes() -> None:
    report = evaluate_inventory(
        [row("MAPPED", "HIGH"), row("EXCLUDED_WITH_REASON", "LOW")],
        policy(),
    )

    assert report.outcome is CoverageOutcome.PASS
    assert report.transition_allowed is True
    assert report.metrics.strict_completion is True
    assert report.metrics.closure_percent == 100.0
    assert report.reason_codes == (CoverageReason.PASS.value,)


def test_strict_gap_fails_and_excluded_with_reason_is_closed() -> None:
    report = evaluate_inventory(
        [row("MAPPED"), row("EXCLUDED_WITH_REASON"), row("DISCOVERY_GAP")],
        policy(),
    )

    assert report.outcome is CoverageOutcome.FAIL
    assert report.transition_allowed is False
    assert report.metrics.closed_count == 2
    assert report.metrics.open_gap_count == 1
    assert report.metrics.closure_percent == 66.67
    assert CoverageReason.STRICT_INCOMPLETE.value in report.reason_codes


def test_risk_gated_low_gap_passes_with_gaps() -> None:
    report = evaluate_inventory(
        [row("MAPPED", "HIGH"), row("DISCOVERY_GAP", "LOW")],
        policy(
            mode=CoverageMode.RISK_GATED,
            minimum=50,
            maximum_gaps=1,
            block=(RiskLevel.CRITICAL, RiskLevel.HIGH),
        ),
    )

    assert report.outcome is CoverageOutcome.PASS_WITH_GAPS
    assert report.transition_allowed is True
    assert report.metrics.strict_completion is False
    assert report.metrics.risk_gated_completion is True
    assert report.reason_codes == (CoverageReason.PASS_WITH_GAPS.value,)


@pytest.mark.parametrize("risk", ["HIGH", "CRITICAL"])
def test_risk_gated_blocking_risk_gap_fails(risk: str) -> None:
    report = evaluate_inventory(
        [row("MAPPED", "LOW"), row("DISCOVERY_GAP", risk)],
        policy(
            mode=CoverageMode.RISK_GATED,
            minimum=50,
            maximum_gaps=2,
            block=(RiskLevel.CRITICAL, RiskLevel.HIGH),
        ),
    )

    assert report.outcome is CoverageOutcome.FAIL
    assert report.transition_allowed is False
    assert CoverageReason.BLOCKING_RISK_GAPS.value in report.reason_codes


def test_risk_gated_closure_and_open_gap_thresholds_fail() -> None:
    low_closure = evaluate_inventory(
        [row("MAPPED"), row("DISCOVERY_GAP"), row("BLOCKED")],
        policy(mode=CoverageMode.RISK_GATED, minimum=80, maximum_gaps=3, block=()),
    )
    too_many_gaps = evaluate_inventory(
        [row("MAPPED"), row("DISCOVERY_GAP"), row("UNREACHABLE")],
        policy(mode=CoverageMode.RISK_GATED, minimum=30, maximum_gaps=1, block=()),
    )

    assert CoverageReason.CLOSURE_BELOW_MINIMUM.value in low_closure.reason_codes
    assert CoverageReason.OPEN_GAPS_EXCEEDED.value in too_many_gaps.reason_codes


def test_max_discovery_pass_reached_stops_loop_with_reason() -> None:
    report = evaluate_inventory(
        [row("MAPPED"), row("DISCOVERY_GAP")],
        policy(current_pass=3, max_passes=3),
    )
    exceeded = evaluate_inventory(
        [row("MAPPED"), row("DISCOVERY_GAP")],
        policy(current_pass=4, max_passes=3),
    )

    assert CoverageReason.MAX_PASSES_REACHED.value in report.reason_codes
    assert exceeded.outcome is CoverageOutcome.BLOCKED


def test_no_inventory_and_excluded_only_are_blocked() -> None:
    empty = evaluate_inventory([], policy())
    excluded_only = evaluate_inventory([row("RETIRED"), row("MERGED")], policy())

    assert empty.outcome is CoverageOutcome.BLOCKED
    assert excluded_only.outcome is CoverageOutcome.BLOCKED
    assert empty.reason_codes == (CoverageReason.NO_INVENTORY.value,)


def test_retired_merged_excluded_and_unreachable_is_gap() -> None:
    report = evaluate_inventory(
        [
            row("MAPPED", "HIGH"),
            row("RETIRED", "CRITICAL"),
            row("MERGED", "HIGH"),
            row("UNREACHABLE", "LOW"),
        ],
        policy(),
    )

    assert report.metrics.inventory_total == 4
    assert report.metrics.effective_total == 2
    assert report.metrics.open_gap_count == 1
    assert report.metrics.gap_count_by_risk["LOW"] == 1
    assert report.metrics.total_count_by_risk["CRITICAL"] == 0


def test_unknown_status_and_unknown_risk_are_blocked_without_secret() -> None:
    bad_status = evaluate_inventory([row(f"BAD-{SECRET}")], policy())
    bad_risk = evaluate_inventory([row("MAPPED", f"BAD-{SECRET}")], policy())

    assert bad_status.outcome is CoverageOutcome.BLOCKED
    assert bad_status.reason_codes == (CoverageReason.UNKNOWN_STATUS.value,)
    assert bad_risk.reason_codes == (CoverageReason.UNKNOWN_RISK.value,)
    assert_no_secret(bad_status.to_dict(), bad_risk.to_dict())


def test_top_gaps_are_limited_to_ten_and_ids_are_redacted() -> None:
    rows = [row("DISCOVERY_GAP", "LOW", inventory_id=f"INV-{index}") for index in range(12)]
    rows[0]["inventory_id"] = SECRET

    report = evaluate_inventory(rows, policy())

    assert len(report.metrics.top_open_gaps) == 10
    assert report.metrics.top_open_gaps[0].inventory_id == "row-1"
    assert_no_secret(report.to_dict())


def write_config(tmp_path: Path, *, mode: str = "strict") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["coverage"]["mode"] = mode
    if mode == "risk-gated":
        data["coverage"]["minimum_inventory_closure_percent"] = 50
        data["coverage"]["maximum_open_discovery_gaps"] = 2
        data["coverage"]["block_open_gap_risks"] = ["CRITICAL", "HIGH"]
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def write_inventory(tmp_path: Path, payload: Any) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )


def parse_json(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(process.stdout)


def load_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location("evaluate_coverage_script", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_help_text_json_pass_and_pass_with_gaps(tmp_path: Path) -> None:
    help_process = run_cli("--help")
    strict_config = write_config(tmp_path)
    risk_config = write_config(tmp_path / "risk", mode="risk-gated")
    strict_inventory = write_inventory(tmp_path, [row("MAPPED")])
    risk_inventory = write_inventory(
        tmp_path / "risk",
        [row("MAPPED", "HIGH"), row("DISCOVERY_GAP", "LOW")],
    )

    text_process = run_cli(
        "--config",
        str(strict_config),
        "--inventory-json",
        str(strict_inventory),
    )
    json_process = run_cli(
        "--config",
        str(risk_config),
        "--inventory-json",
        str(risk_inventory),
        "--format",
        "json",
    )

    assert help_process.returncode == 0
    assert text_process.returncode == 0
    assert "PASS" in text_process.stdout
    assert json_process.returncode == 0
    result = parse_json(json_process)
    assert result["data"]["metrics"]["outcome"] == "PASS_WITH_GAPS"


def test_cli_failures_and_secret_non_leak(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    missing = run_cli("--config", str(config), "--inventory-json", str(tmp_path / "missing.json"))
    invalid_json_path = tmp_path / "bad.json"
    invalid_json_path.write_text("{not json " + SECRET, encoding="utf-8")
    invalid_json = run_cli("--config", str(config), "--inventory-json", str(invalid_json_path))
    empty_inventory = run_cli(
        "--config",
        str(config),
        "--inventory-json",
        str(write_inventory(tmp_path, [])),
        "--format",
        "json",
    )
    secret_inventory = run_cli(
        "--config",
        str(config),
        "--inventory-json",
        str(write_inventory(tmp_path, [row(f"BAD-{SECRET}")])),
        "--format",
        "json",
    )

    assert missing.returncode == 2
    assert invalid_json.returncode == 2
    assert empty_inventory.returncode == 3
    assert secret_inventory.returncode == 3
    assert_no_secret(
        missing.stdout,
        missing.stderr,
        invalid_json.stdout,
        invalid_json.stderr,
        empty_inventory.stdout,
        empty_inventory.stderr,
        secret_inventory.stdout,
        secret_inventory.stderr,
    )


def test_cli_invalid_config_from_sheets_and_network_not_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_config = tmp_path / "invalid.yml"
    invalid_config.write_text("not: valid", encoding="utf-8")
    inventory = write_inventory(tmp_path, [row("MAPPED")])
    from_sheets = run_cli("--config", str(invalid_config), "--from-sheets")
    invalid = run_cli("--config", str(invalid_config), "--inventory-json", str(inventory))

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", fail_socket)
    safe = run_cli(
        "--config",
        str(write_config(tmp_path / "safe")),
        "--inventory-json",
        str(write_inventory(tmp_path / "safe", [row("MAPPED")])),
    )
    direct_code = load_cli_module().main(
        [
            "--config",
            str(write_config(tmp_path / "direct")),
            "--inventory-json",
            str(write_inventory(tmp_path / "direct", [row("MAPPED")])),
        ]
    )
    captured = capsys.readouterr()

    assert from_sheets.returncode == 2
    assert invalid.returncode == 2
    assert safe.returncode == 0
    assert direct_code == 0
    assert "PASS" in captured.out
