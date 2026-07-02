from __future__ import annotations

import tomllib
from pathlib import Path

import webapp_debug_skill

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_VERSION = "0.2.0"


def read_text(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_pyproject_and_package_version_match_target() -> None:
    data = tomllib.loads(read_text("pyproject.toml"))

    assert data["project"]["version"] == EXPECTED_VERSION
    assert webapp_debug_skill.__version__ == EXPECTED_VERSION


def test_release_note_filename_and_changelog_match_version() -> None:
    release_note = REPO_ROOT / f"docs/RELEASE_NOTES_v{EXPECTED_VERSION}.md"

    assert release_note.is_file()
    assert EXPECTED_VERSION in release_note.read_text(encoding="utf-8")
    assert f"v{EXPECTED_VERSION}" in release_note.read_text(encoding="utf-8")
    assert EXPECTED_VERSION in read_text("CHANGELOG.md")


def test_readme_and_install_include_major_cli_names() -> None:
    docs = read_text("README.md") + "\n" + read_text("INSTALL.md")

    for script in (
        "scripts/validate_skill.py",
        "scripts/validate_config.py",
        "scripts/validate_sheets_schema.py",
        "scripts/init_sheets.py",
        "scripts/redact_artifact.py",
        "scripts/evaluate_coverage.py",
        "scripts/export_sheets_snapshot.py",
        "scripts/discover_cakephp_inventory.py",
        "scripts/plan_inventory_sync.py",
        "scripts/apply_inventory_sync.py",
        "scripts/release_check.py",
        "scripts/plan_scenario_sync.py",
        "scripts/apply_scenario_sync.py",
        "scripts/bootstrap_playwright_project.py",
        "scripts/generate_playwright_tests.py",
    ):
        assert script in docs


def test_changelog_does_not_mark_future_engines_as_added() -> None:
    changelog = read_text("CHANGELOG.md")
    added = changelog.split("### Added", 1)[1].split("### Changed", 1)[0]

    assert "CakePHP static Inventory discovery" in added
    for future_feature in (
        "JavaScript parsing",
        "Playwright runner orchestration",
    ):
        assert future_feature not in added
        assert future_feature in changelog
