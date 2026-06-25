from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/validate_skill.py"


def copy_validation_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for relative_path in [
        "README.md",
        "skills",
        ".agents",
        ".claude",
    ]:
        source = REPO_ROOT / relative_path
        destination = root / relative_path
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return root


def run_validator(
    root: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--format", "json"],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def read_json_result(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(process.stdout)


def insert_frontmatter_key(path: Path, line: str) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\n---\n", f"\n{line}\n---\n", 1), encoding="utf-8")


def test_valid_skill_metadata_passes(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)

    process = run_validator(root)

    assert process.returncode == 0, process.stdout + process.stderr
    result = read_json_result(process)
    assert result["ok"] is True


def test_codex_wrapper_rejects_argument_hint(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    insert_frontmatter_key(root / ".agents/skills/webapp-debug/SKILL.md", "argument-hint: bad")

    process = run_validator(root)

    assert process.returncode == 2
    assert "argument-hint" in process.stdout


def test_canonical_skill_rejects_disable_model_invocation(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    insert_frontmatter_key(root / "skills/webapp-debug/SKILL.md", "disable-model-invocation: true")

    process = run_validator(root)

    assert process.returncode == 2
    assert "disable-model-invocation" in process.stdout


def test_missing_wrapper_fails(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    (root / ".agents/skills/webapp-debug/SKILL.md").unlink()

    process = run_validator(root)

    assert process.returncode == 2
    assert "missing file" in process.stdout


def test_broken_wrapper_relative_path_fails(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    wrapper = root / ".claude/skills/webapp-debug/SKILL.md"
    text = wrapper.read_text(encoding="utf-8")
    wrapper.write_text(
        text.replace("../../../skills/webapp-debug/SKILL.md", "../../missing/SKILL.md"),
        encoding="utf-8",
    )

    process = run_validator(root)

    assert process.returncode == 2
    assert "canonical Skill relative path" in process.stdout


def test_missing_openai_policy_fails(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    openai_yaml = root / ".agents/skills/webapp-debug/agents/openai.yaml"
    text = openai_yaml.read_text(encoding="utf-8")
    openai_yaml.write_text(
        text.replace("  allow_implicit_invocation: false\n", ""),
        encoding="utf-8",
    )

    process = run_validator(root)

    assert process.returncode == 2
    assert "allow_implicit_invocation" in process.stdout or "differs" in process.stdout


def test_missing_yaml_parser_reports_dependency_without_traceback(tmp_path: Path) -> None:
    root = copy_validation_repo(tmp_path)
    block_yaml = tmp_path / "block_yaml"
    block_yaml.mkdir()
    (block_yaml / "sitecustomize.py").write_text(
        "\n".join(
            [
                "import builtins",
                "_real_import = builtins.__import__",
                "def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):",
                "    if name == 'yaml' or name.startswith('yaml.'):",
                "        raise ModuleNotFoundError(\"No module named 'yaml'\", name='yaml')",
                "    return _real_import(name, globals, locals, fromlist, level)",
                "builtins.__import__ = _blocked_import",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(block_yaml)

    process = run_validator(root, env=env)

    assert process.returncode == 10
    assert "DEPENDENCY_MISSING" in process.stdout
    assert "Traceback" not in process.stdout
    assert "Traceback" not in process.stderr
