from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
SECRET_MARKER = "SECRET_MARKER_PHASE5A"


def read_text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def load_workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
    elif value is not None:
        yield str(value)


def run_commands(workflow: Mapping[str, Any]) -> str:
    jobs = workflow["jobs"]
    assert isinstance(jobs, Mapping)
    runs: list[str] = []
    for job in jobs.values():
        assert isinstance(job, Mapping)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            assert isinstance(step, Mapping)
            command = step.get("run")
            if command is not None:
                runs.append(str(command))
    return "\n".join(runs)


def test_ci_workflow_static_shape_and_required_commands() -> None:
    assert WORKFLOW.exists()
    workflow = load_workflow()

    assert workflow["name"] == "CI"
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    assert isinstance(job["timeout-minutes"], int)
    assert job["timeout-minutes"] > 0
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]

    steps = job["steps"]
    assert any(step.get("uses") == "actions/checkout@v4" for step in steps)
    setup = next(step for step in steps if step.get("uses") == "actions/setup-python@v5")
    assert setup["with"]["cache"] == "pip"
    cache_paths = setup["with"]["cache-dependency-path"]
    assert "pyproject.toml" in cache_paths
    assert "requirements.lock" in cache_paths

    commands = run_commands(workflow)
    for expected in (
        "python -m pip install --upgrade pip",
        'python -m pip install -e ".[dev]"',
        "python -m pip check",
        "python -m pytest -q",
        "python -m pytest tests/integration -q",
        "python -m ruff check .",
        "python -m ruff format --check .",
        "python scripts/validate_skill.py --root .",
        "python scripts/validate_sheets_schema.py",
        "python scripts/validate_config.py",
        "python scripts/init_sheets.py --help",
        "python scripts/evaluate_coverage.py --help",
        "python scripts/export_sheets_snapshot.py --help",
        "python scripts/release_check.py --version 0.2.0",
    ):
        assert expected in commands


def test_ci_workflow_has_no_secret_or_external_service_configuration() -> None:
    workflow = load_workflow()
    rendered = "\n".join(walk_strings(workflow))

    for forbidden in (
        "secrets.",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "WEBAPP_DEBUG_GOOGLE_CREDENTIALS_FILE",
        "WEBAPP_DEBUG_GOOGLE_SPREADSHEET_ID",
        "WEBAPP_DEBUG_GOOGLE_CONFIRM_SPREADSHEET_ID",
        "WEBAPP_DEBUG_RUN_GOOGLE_INTEGRATION",
        "gcloud",
        "permissions.create",
        "Drive API",
    ):
        assert forbidden not in rendered

    for job in workflow["jobs"].values():
        assert "env" not in job
        for step in job["steps"]:
            assert "env" not in step
            working_directory = step.get("working-directory")
            if working_directory is not None:
                rendered_directory = str(working_directory)
                assert not rendered_directory.startswith("/")
                assert ".." not in Path(rendered_directory).parts


def test_documented_script_commands_exist() -> None:
    docs = "\n".join(
        read_text(path)
        for path in (
            "README.md",
            "INSTALL.md",
            "docs/RELEASE_CHECKLIST.md",
        )
    )
    script_paths = set(re.findall(r"python\s+(scripts/[A-Za-z0-9_./-]+\.py)", docs))
    assert {
        "scripts/validate_skill.py",
        "scripts/validate_config.py",
        "scripts/validate_sheets_schema.py",
        "scripts/init_sheets.py",
        "scripts/redact_artifact.py",
        "scripts/evaluate_coverage.py",
        "scripts/export_sheets_snapshot.py",
        "scripts/release_check.py",
    }.issubset(script_paths)
    for script_path in script_paths:
        assert (REPO_ROOT / script_path).is_file(), script_path


def test_changelog_does_not_claim_future_engines_are_implemented() -> None:
    changelog = read_text("CHANGELOG.md")
    added = changelog.split("### Added", 1)[1].split("### Changed", 1)[0]
    for not_implemented in (
        "CakePHP discovery",
        "JavaScript parsing",
        "Playwright Scenario generation",
        "Playwright runner orchestration",
    ):
        assert not_implemented not in added
        assert not_implemented in changelog


def test_release_checklist_records_ci_and_opt_in_boundaries() -> None:
    checklist = read_text("docs/RELEASE_CHECKLIST.md")
    for expected in (
        "python -m pytest -q",
        "python -m pytest tests/integration -q",
        "python -m ruff check .",
        "python scripts/init_sheets.py --help",
        "python scripts/evaluate_coverage.py --help",
        "python scripts/export_sheets_snapshot.py --help",
        "python scripts/release_check.py --version 0.2.0",
        "skip real Google integration tests",
        "Drive API sharing or deletion",
        "Version bump and release automation policy is still undecided",
    ):
        assert expected in checklist


def test_docs_and_workflow_do_not_contain_secret_marker() -> None:
    for relative_path in (
        "README.md",
        "INSTALL.md",
        "CHANGELOG.md",
        "docs/RELEASE_CHECKLIST.md",
        ".github/workflows/ci.yml",
    ):
        assert SECRET_MARKER not in read_text(relative_path)
