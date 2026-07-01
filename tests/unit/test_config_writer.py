from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from webapp_debug_skill.config import load_yaml_module
from webapp_debug_skill.config_writer import ConfigWriteError, ConfigWriter

SECRET = "SECRET_MARKER_CONFIG_CONTENT"
EXAMPLE_CONFIG = Path("skills/webapp-debug/assets/webapp-debug.config.example.yml")


def copy_config(tmp_path: Path, *, spreadsheet_id: str = "") -> Path:
    target = tmp_path / "config.yml"
    shutil.copyfile(EXAMPLE_CONFIG, target)
    yaml = load_yaml_module()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    data["sheets"]["spreadsheet_id"] = spreadsheet_id
    data["x-test-extension"] = {"keep": True}
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def writer(**kwargs: object) -> ConfigWriter:
    return ConfigWriter(
        repository_root=Path.cwd(),
        clock=lambda: "20260701T000000Z",
        **kwargs,
    )


def assert_no_secret(*values: object) -> None:
    for value in values:
        assert SECRET not in str(value)


def test_sets_empty_id_with_backup_and_preserves_extension(tmp_path: Path) -> None:
    config = copy_config(tmp_path)
    original = config.read_bytes()

    result = writer().write_spreadsheet_id(config, "spreadsheet-123")

    yaml = load_yaml_module()
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert result.config_written is True
    assert result.backup_created is True
    assert result.backup_name is not None
    assert (tmp_path / result.backup_name).read_bytes() == original
    assert data["sheets"]["spreadsheet_id"] == "spreadsheet-123"
    assert data["x-test-extension"] == {"keep": True}


def test_same_id_is_noop_and_dry_run_writes_nothing(tmp_path: Path) -> None:
    config = copy_config(tmp_path, spreadsheet_id="spreadsheet-123")
    original = config.read_bytes()
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    empty_config = copy_config(empty_dir)

    noop = writer().write_spreadsheet_id(config, "spreadsheet-123")
    dry_original = empty_config.read_bytes()
    dry = writer().write_spreadsheet_id(empty_config, "spreadsheet-456", dry_run=True)

    assert noop.noop is True
    assert dry.config_written is False
    assert config.read_bytes() == original
    assert empty_config.read_bytes() == dry_original
    assert not list(tmp_path.glob("*.bak.*"))


def test_existing_different_id_is_rejected(tmp_path: Path) -> None:
    config = copy_config(tmp_path, spreadsheet_id="spreadsheet-old")

    with pytest.raises(ConfigWriteError) as exc_info:
        writer().write_spreadsheet_id(config, "spreadsheet-new")

    assert exc_info.value.code == "CONFIG_SPREADSHEET_ID_CONFLICT"
    assert config.read_text(encoding="utf-8").count("spreadsheet-old") == 1


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "asset"])
def test_unsafe_targets_are_rejected(tmp_path: Path, kind: str) -> None:
    if kind == "missing":
        target = tmp_path / "missing.yml"
    elif kind == "directory":
        target = tmp_path / "dir"
        target.mkdir()
    elif kind == "symlink":
        real = copy_config(tmp_path)
        target = tmp_path / "link.yml"
        target.symlink_to(real)
    else:
        target = EXAMPLE_CONFIG.resolve()

    with pytest.raises(ConfigWriteError) as exc_info:
        writer().write_spreadsheet_id(target, "spreadsheet-123")

    assert exc_info.value.code == "CONFIG_TARGET_UNSAFE"


def test_backup_existing_name_is_not_overwritten(tmp_path: Path) -> None:
    config = copy_config(tmp_path)
    original_hash = __import__("hashlib").sha256(config.read_bytes()).hexdigest()[:8]
    existing = tmp_path / f"config.yml.bak.20260701T000000Z.{original_hash}"
    existing.write_text("keep", encoding="utf-8")

    result = writer().write_spreadsheet_id(config, "spreadsheet-123")

    assert existing.read_text(encoding="utf-8") == "keep"
    assert result.backup_name != existing.name
    assert (tmp_path / result.backup_name).exists()


def test_backup_failure_and_replace_failure_leave_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = copy_config(tmp_path)
    original = config.read_bytes()

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError(SECRET)

    monkeypatch.setattr("webapp_debug_skill.config_writer.shutil.copyfile", fail_copy)
    with pytest.raises(ConfigWriteError) as backup_exc:
        writer().write_spreadsheet_id(config, "spreadsheet-123")
    assert backup_exc.value.code == "CONFIG_BACKUP_FAILED"
    assert_no_secret(backup_exc.value, backup_exc.value.reason)
    assert config.read_bytes() == original

    monkeypatch.undo()

    def fail_replace(_src: Path, _dst: Path) -> None:
        raise OSError(SECRET)

    with pytest.raises(ConfigWriteError) as replace_exc:
        writer(replace_func=fail_replace).write_spreadsheet_id(config, "spreadsheet-123")
    assert replace_exc.value.code == "CONFIG_WRITE_FAILED"
    assert_no_secret(replace_exc.value, replace_exc.value.reason)
    assert config.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_concurrent_modification_detected_before_replace(tmp_path: Path) -> None:
    config = copy_config(tmp_path)
    original = config.read_bytes()
    calls = 0

    def fsync(_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            with config.open("ab") as handle:
                handle.write(b"\n# concurrent\n")

    with pytest.raises(ConfigWriteError) as exc_info:
        writer(fsync_func=fsync).write_spreadsheet_id(config, "spreadsheet-123")

    assert exc_info.value.code == "CONFIG_CONCURRENT_MODIFICATION"
    assert config.read_bytes().startswith(original)


def test_no_temp_file_left_after_success(tmp_path: Path) -> None:
    config = copy_config(tmp_path)

    writer().write_spreadsheet_id(config, "spreadsheet-123")

    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]
    assert all(os.path.isfile(path) for path in tmp_path.iterdir())
