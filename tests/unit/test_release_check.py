from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "SECRET_MARKER_RELEASE_CHECK"


def load_release_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_check_module",
        REPO_ROOT / "scripts/release_check.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_check = load_release_check()


def write(path: Path, text: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_release_root(tmp_path: Path, *, version: str = "0.2.0") -> Path:
    root = tmp_path / "repo"
    write(root / "pyproject.toml", f'[project]\nname = "demo"\nversion = "{version}"\n')
    write(root / "src/webapp_debug_skill/__init__.py", f'__version__ = "{version}"\n')
    write(root / "AGENTS.md")
    write(root / ".agents/skills/webapp-debug/SKILL.md")
    write(root / ".claude/skills/webapp-debug/SKILL.md")
    write(root / "skills/webapp-debug/SKILL.md")
    write(root / "skills/webapp-debug/assets/google-sheets-schema.json", "{}\n")
    write(root / "skills/webapp-debug/assets/config.schema.json", "{}\n")
    for script in release_check.REQUIRED_SCRIPTS:
        write(root / script, "#!/usr/bin/env python3\n")
    commands = "\n".join(release_check.REQUIRED_SCRIPTS)
    write(root / "README.md", commands)
    write(root / "INSTALL.md", commands)
    write(
        root / "CHANGELOG.md",
        f"""# Changelog

## [{version}] - Unreleased

### Added

- Release preparation.

### Changed

- Package versioning is explicit.

### Known Limitations

- Dynamic browser discovery and Test Runs/Defects Sheets apply are not implemented.
- High-precision CakePHP AST adapters are not implemented.
- JavaScript parsing and Playwright Scenario generation are not implemented.
- Playwright runner orchestration is not implemented.
""",
    )
    write(
        root / "docs/RELEASE_CHECKLIST.md",
        "python scripts/release_check.py --version 0.2.0\n",
    )
    write(
        root / f"docs/RELEASE_NOTES_v{version}.md",
        f"""# Release Notes: v{version}

Target tag: `v{version}`

Dynamic browser discovery and Test Runs/Defects Sheets apply are not implemented.
High-precision CakePHP AST adapters are not implemented.
Playwright scenario generation is not implemented.
""",
    )
    write(root / ".github/workflows/ci.yml", safe_workflow())
    return root


def safe_workflow() -> str:
    return """name: CI
"on":
  push:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: python scripts/release_check.py --version 0.2.0
"""


def tracked_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def deps(root: Path, *, git_ls_files: Any | None = None) -> Any:
    return release_check.ReleaseCheckDependencies(
        repository_root=root,
        git_ls_files=git_ls_files or tracked_files,
    )


def run_cli(
    root: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = release_check.main(argv, deps(root))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_help_and_invalid_args(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as help_exit:
        release_check.main(["--help"])
    assert help_exit.value.code == 0
    assert "release readiness" in capsys.readouterr().out

    with pytest.raises(SystemExit) as invalid_exit:
        release_check.main(["--format", "xml"])
    assert invalid_exit.value.code == 2


def test_text_json_and_normal_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_release_root(tmp_path)

    text_code, text_out, text_err = run_cli(root, ["--version", "0.2.0"], capsys)
    json_code, json_out, json_err = run_cli(
        root,
        ["--version", "0.2.0", "--format", "json"],
        capsys,
    )

    payload = json.loads(json_out)
    assert text_code == 0
    assert json_code == 0
    assert "RELEASE_CHECK_OK" in text_out
    assert payload["ok"] is True
    assert payload["data"]["version"] == "0.2.0"
    assert text_err == ""
    assert json_err == ""


def test_version_mismatch_missing_files_and_unsafe_ci(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    version_root = make_release_root(tmp_path / "version", version="0.2.1")
    missing_note_root = make_release_root(tmp_path / "missing-note")
    (missing_note_root / "docs/RELEASE_NOTES_v0.2.0.md").unlink()
    missing_ci_root = make_release_root(tmp_path / "missing-ci")
    (missing_ci_root / ".github/workflows/ci.yml").unlink()
    unsafe_ci_root = make_release_root(tmp_path / "unsafe-ci")
    write(
        unsafe_ci_root / ".github/workflows/ci.yml",
        safe_workflow() + "      - run: echo ${{ secrets.GOOGLE_TOKEN }}\n",
    )

    cases = (
        (version_root, "VERSION_MISMATCH"),
        (missing_note_root, "MISSING"),
        (missing_ci_root, "MISSING"),
        (unsafe_ci_root, "FORBIDDEN_SECRET_OR_SERVICE"),
    )
    for root, reason in cases:
        code, out, err = run_cli(root, ["--version", "0.2.0"], capsys)
        assert code == 3
        assert reason in out
        assert err == ""


def test_tracked_credential_cache_and_secret_content_detection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_root = make_release_root(tmp_path / "credential")
    write(credential_root / "prod-service-account.json", "{}")
    cache_root = make_release_root(tmp_path / "cache")
    write(cache_root / "pkg.egg-info/PKG-INFO", "metadata")
    content_root = make_release_root(tmp_path / "content")
    write(content_root / "docs/leak.txt", "-----BEGIN PRIVATE KEY-----")

    cases = (
        (credential_root, "TRACKED_CREDENTIAL_FILE"),
        (cache_root, "TRACKED_CACHE_FILE"),
        (content_root, "SECRET_MARKER_PRESENT"),
    )
    for root, reason in cases:
        code, out, err = run_cli(root, ["--version", "0.2.0"], capsys)
        assert code == 3
        assert reason in out
        assert "PRIVATE KEY-----" not in out
        assert err == ""


def test_secret_marker_and_git_failure_are_safe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_root = make_release_root(tmp_path / "secret")
    write(secret_root / "docs/RELEASE_NOTES_v0.2.0.md", f"{SECRET}\n")

    code, out, err = run_cli(
        secret_root,
        ["--version", "0.2.0", "--format", "json"],
        capsys,
    )
    payload = json.loads(out)
    assert code == 3
    assert payload["ok"] is False
    assert SECRET not in out
    assert SECRET not in err

    def failing_git(_root: Path) -> list[str]:
        raise RuntimeError(SECRET)

    code = release_check.main(
        ["--version", "0.2.0"],
        release_check.ReleaseCheckDependencies(
            repository_root=secret_root,
            git_ls_files=failing_git,
        ),
    )
    captured = capsys.readouterr()
    assert code == 4
    assert SECRET not in captured.out
    assert SECRET not in captured.err
